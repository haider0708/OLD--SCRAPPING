#!/usr/bin/env python3
"""
iSpace Services scraper - WordPress/WooCommerce + Elementor, HTTP/selectolax.
"""

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from scraper.base import FastScraper
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    clean_text,
    dedupe_products,
    finalize_product_record,
    html_product_metadata,
    parse_price,
)


class IspaceScraper(FastScraper):
    """HTTPX/selectolax scraper for ispaceservices.com."""

    CATEGORY_BLUEPRINT = [
        (
            "Mac",
            "/mac-tunisie/",
            [("Mac", "/mac-tunisie/"), ("Accessoires Mac", "/accesoires-mac/")],
        ),
        (
            "iPhone",
            "/iphone/",
            [("iPhone", "/iphone/"), ("Accessoires iPhone", "/accesoires-iphone/")],
        ),
        (
            "iPad",
            "/ipad/",
            [("iPad", "/ipad/"), ("Accessoires iPad", "/accesoires_ipad/")],
        ),
        ("Accessoires", "/accessoires/", []),
    ]

    CATEGORY_PATHS = {
        "/mac-tunisie",
        "/accesoires-mac",
        "/accessoires",
        "/iphone",
        "/accesoires-iphone",
        "/ipad",
        "/accesoires_ipad",
    }

    def __init__(self, logger: logging.Logger):
        super().__init__("ispace", logger)

    def _absolute_url(self, href: Any) -> Optional[str]:
        return absolute_url(href, self.base_url)

    @staticmethod
    def _clean(value: Any) -> Optional[str]:
        return clean_text(value)

    @staticmethod
    def _path_key(url: str) -> str:
        path = urlsplit(url).path or "/"
        return path.rstrip("/") if path != "/" else path

    @staticmethod
    def _strip_url(url: str) -> str:
        parts = urlsplit(url)
        path = parts.path.rstrip("/") if parts.path != "/" else parts.path
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    @staticmethod
    def _post_id_from_class(class_name: str) -> Optional[str]:
        match = re.search(r"(?:^|\s)(?:post|postid)-(\d+)(?:\s|$)", class_name or "")
        return match.group(1) if match else None

    @staticmethod
    def _body_post_id(tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        return IspaceScraper._post_id_from_class(body.attributes.get("class", "") if body else "")

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        path = re.sub(r"/page/\d+/?$", "", parts.path or "/").rstrip("/")
        if page_num <= 1:
            path = path or "/"
        else:
            path = f"{path}/page/{page_num}"
        if path != "/" and not path.endswith("/"):
            path += "/"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    def _category_url(self, path: str) -> str:
        return self._strip_url(f"{self.base_url.rstrip('/')}/{path.strip('/')}/")

    def _is_category_candidate(self, url: Optional[str]) -> bool:
        if not url:
            return False
        low = url.lower()
        if any(
            token in low
            for token in (
                "/product/",
                "add-to-cart",
                "cart",
                "panier",
                "account",
                "mon-compte",
                "checkout",
                "elementor-action",
                "cdn-cgi",
                "#",
                "mailto:",
            )
        ):
            return False
        return self._path_key(url) in self.CATEGORY_PATHS

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})

        nav_urls: Dict[str, str] = {}
        for link in tree.css(fp.get("nav_links", ".elementor-nav-menu a[href]")):
            url = self._absolute_url(link.attributes.get("href"))
            if url:
                url = self._strip_url(url)
            if self._is_category_candidate(url):
                nav_urls[self._path_key(url)] = url

        categories: List[Dict[str, Any]] = []
        seen_urls = set()
        for top_name, top_path, lows in self.CATEGORY_BLUEPRINT:
            top_url = nav_urls.get(top_path.rstrip("/")) or self._category_url(top_path)
            top_cat: Dict[str, Any] = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }
            seen_urls.add(top_url)

            for low_name, low_path in lows:
                low_url = nav_urls.get(low_path.rstrip("/")) or self._category_url(low_path)
                low_cat = {
                    "name": low_name,
                    "url": low_url,
                    "level": "low",
                    "subcategories": [],
                }
                top_cat["low_level_categories"].append(low_cat)
                seen_urls.add(low_url)

            categories.append(top_cat)

        for link in tree.css(fp.get("nav_links", ".elementor-nav-menu a[href]")):
            url = self._absolute_url(link.attributes.get("href"))
            if url:
                url = self._strip_url(url)
            if not self._is_category_candidate(url) or url in seen_urls:
                continue
            name = self._clean(link.text(strip=True))
            if not name:
                continue
            categories.append(
                {
                    "name": name,
                    "url": url,
                    "level": "top",
                    "low_level_categories": [],
                }
            )
            seen_urls.add(url)

        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top.get("low_level_categories", []):
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
                for sub in low.get("subcategories", []):
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1

        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _link_and_name(self, card, selector: str) -> Tuple[Optional[str], Optional[str]]:
        links = card.css(selector)
        chosen = None
        for link in links:
            if self._clean(link.text(strip=True)):
                chosen = link
                break
        if chosen is None and links:
            chosen = links[0]
        if chosen is None:
            return None, None

        url = self._absolute_url(chosen.attributes.get("href"))
        name = self._clean(chosen.text(strip=True))
        if not name:
            img = card.css_first("img[alt]")
            name = self._clean(img.attributes.get("alt") if img else None)
        if not name:
            button = card.css_first("a.add_to_cart_button[aria-label]")
            label = button.attributes.get("aria-label", "") if button else ""
            match = re.search(r"[\"“](.*?)[\"”]", label)
            name = self._clean(match.group(1) if match else label)
        return (self._strip_url(url) if url else None), name

    def _extract_card_id(self, card) -> Optional[str]:
        button = card.css_first("a.button[data-product_id], a.add_to_cart_button[data-product_id]")
        product_id = button.attributes.get("data-product_id") if button else None
        return self._clean(product_id) or self._post_id_from_class(card.attributes.get("class", ""))

    def _extract_card_sku(self, card) -> Optional[str]:
        button = card.css_first("a.button[data-product_sku], a.add_to_cart_button[data-product_sku]")
        return self._clean(button.attributes.get("data-product_sku") if button else None)

    def _extract_price(self, root, current_selector: str, fallback_selector: str) -> Optional[float]:
        node = root.css_first(current_selector) or root.css_first(fallback_selector)
        if node is None:
            node = root.css_first(".woocommerce-Price-amount bdi, bdi")
        return parse_price(node.text(strip=True) if node else None)

    def _availability_from_card(self, card) -> Tuple[Optional[str], Optional[bool]]:
        class_name = (card.attributes.get("class") or "").lower()
        text = card.text(strip=True)
        if "outofstock" in class_name or "rupture" in text.lower():
            return "Rupture de stock", False
        if "available-on-backorder" in class_name or "arrivage" in text.lower():
            return "En Arrivage", None
        if "instock" in class_name:
            return "En stock", True
        return availability_from_text(text)

    def _extract_image(self, root, selector: str, attrs: List[str]) -> Optional[str]:
        for img in root.css(selector):
            for attr in attrs:
                value = img.attributes.get(attr)
                if attr == "srcset" and value:
                    value = value.split(",", 1)[0].strip().split(" ", 1)[0]
                image = self._absolute_url(value)
                if image and not image.startswith("data:"):
                    return image
        return None

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products: List[Dict[str, Any]] = []

        for card in tree.css(cp.get("item_selector", "div.product.type-product")):
            url, name = self._link_and_name(
                card,
                cp.get("item_url", "a.woocommerce-loop-product__link[href*='/product/']"),
            )
            if not url or "/product/" not in url or not name:
                continue

            product_id = self._extract_card_id(card)
            sku = self._extract_card_sku(card)
            price = self._extract_price(
                card,
                cp.get("item_current_price", "ins .woocommerce-Price-amount bdi"),
                cp.get("item_price", ".price .woocommerce-Price-amount bdi"),
            )
            old_price_node = card.css_first(cp.get("item_old_price", "del .woocommerce-Price-amount bdi"))
            old_price = parse_price(old_price_node.text(strip=True) if old_price_node else None)
            availability, available = self._availability_from_card(card)

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "url": url,
                "name": name,
                "price": price,
            }

            if old_price is not None:
                product["old_price"] = old_price
                if price and old_price != price:
                    product["discount_percent"] = round((1 - price / old_price) * 100)
            if sku:
                product["reference"] = sku
                product["sku"] = sku
            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available

            image = self._extract_image(
                card,
                cp.get("item_image", "img"),
                cp.get("item_image_attrs", ["data-src", "src", "data-large_image", "srcset"]),
            )
            if image:
                product["image"] = image

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "ispace listing")

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1

        current = tree.css_first("nav.woocommerce-pagination .page-numbers.current")
        if current:
            try:
                current_page = int(current.text(strip=True))
            except (TypeError, ValueError):
                pass

        for link in tree.css(cp.get("pagination_pages", "nav.woocommerce-pagination a.page-numbers")):
            text = self._clean(link.text(strip=True))
            href = link.attributes.get("href", "")
            page_num = None
            if text and text.isdigit():
                page_num = int(text)
            else:
                match = re.search(r"/page/(\d+)/?", href)
                if match:
                    page_num = int(match.group(1))
            if page_num:
                total_pages = max(total_pages, page_num)

        next_link = tree.css_first(cp.get("pagination_next", "nav.woocommerce-pagination a.next.page-numbers"))
        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": bool(next_link),
        }

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = {"url": self._strip_url(url)}

        metadata = html_product_metadata(html, url, self.base_url)
        data.update(metadata)

        product_id = self._extract_detail_product_id(tree, pp)
        if product_id:
            data["product_id"] = product_id

        title_node = tree.css_first(pp.get("title", "h1.product_title, h1.entry-title, h1"))
        title = self._clean(title_node.text(strip=True) if title_node else None)
        if title:
            data["title"] = title

        variants = self._extract_variants(tree, pp)
        if variants:
            data["variants"] = variants

        sku = self._extract_detail_sku(tree, pp, html, variants)
        if sku:
            data["reference"] = sku
            data["sku"] = sku

        price = self._detail_price(tree, pp, variants)
        if price is not None:
            data["price"] = price

        old_price_node = tree.css_first(pp.get("old_price", "p.price del .woocommerce-Price-amount bdi"))
        old_price = parse_price(old_price_node.text(strip=True) if old_price_node else None)
        if old_price is not None:
            data["old_price"] = old_price
            if data.get("price") and old_price != data["price"]:
                data["discount_percent"] = round((1 - data["price"] / old_price) * 100)

        availability, available = self._detail_availability(tree, pp, variants)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        overview = self._node_text(tree, pp.get("overview", ".woocommerce-product-details__short-description"))
        if overview:
            data["overview"] = overview
            data.setdefault("description", overview)

        description = self._node_text(
            tree,
            pp.get("description", "#tab-description, .woocommerce-Tabs-panel--description"),
        )
        if description:
            data["description"] = description

        specs = self._extract_specifications(tree, pp, variants)
        if specs:
            data["specifications"] = specs

        images = self._extract_detail_images(tree, pp, variants)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = [
            self._clean(node.text(strip=True))
            for node in tree.css(".posted_in a")
            if self._clean(node.text(strip=True))
        ]
        if categories:
            data["categories"] = categories

        return finalize_product_record(data)

    def _node_text(self, tree: HTMLParser, selector: str) -> Optional[str]:
        node = tree.css_first(selector)
        return self._clean(node.text(strip=True) if node else None)

    def _extract_detail_product_id(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("product_id", "input[name='product_id'][value]"))
        if node:
            product_id = (
                node.attributes.get("value")
                or node.attributes.get("data-product_id")
                or node.attributes.get("data-product-id")
            )
            if product_id:
                return self._clean(product_id)
        return self._body_post_id(tree)

    def _extract_detail_sku(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        html: str,
        variants: List[Dict[str, Any]],
    ) -> Optional[str]:
        sku_node = tree.css_first(pp.get("sku", ".sku_wrapper .sku"))
        sku = self._clean(sku_node.text(strip=True) if sku_node else None)
        if sku and sku.upper() != "ND":
            return sku

        data_layer = self._extract_datalayer_item(html)
        sku = self._clean(data_layer.get("sku") or data_layer.get("item_id"))
        if sku:
            return sku

        for variant in variants:
            sku = self._clean(variant.get("sku"))
            if sku:
                return sku
        return None

    def _detail_price(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        variants: List[Dict[str, Any]],
    ) -> Optional[float]:
        prices = [variant.get("price") for variant in variants if variant.get("price") is not None]
        if prices:
            return min(prices)
        node = tree.css_first(pp.get("price", "p.price .woocommerce-Price-amount bdi"))
        return parse_price(node.text(strip=True) if node else None)

    def _detail_availability(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        variants: List[Dict[str, Any]],
    ) -> Tuple[Optional[str], Optional[bool]]:
        node = tree.css_first(pp.get("availability", ".stock"))
        if node:
            return availability_from_text(node.text(strip=True))
        if not variants:
            return None, None
        if any(variant.get("available") is True for variant in variants):
            return "En stock", True
        if all(variant.get("available") is False for variant in variants):
            return "Rupture de stock", False
        if any("arrivage" in str(variant.get("availability", "")).lower() for variant in variants):
            return "En Arrivage", None
        return None, None

    def _extract_variants(self, tree: HTMLParser, pp: Dict[str, Any]) -> List[Dict[str, Any]]:
        form = tree.css_first(pp.get("variations_form", "form.variations_form[data-product_variations]"))
        raw = form.attributes.get("data-product_variations") if form else None
        if not raw:
            return []

        try:
            parsed = json.loads(html_lib.unescape(raw))
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(parsed, list):
            return []

        variants = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            availability_text, available = self._availability_from_variant(item)
            price = parse_price(item.get("display_price"))
            regular_price = parse_price(item.get("display_regular_price"))
            variant: Dict[str, Any] = {
                "variation_id": str(item.get("variation_id")) if item.get("variation_id") else None,
                "sku": self._clean(item.get("sku")),
                "attributes": {
                    key.replace("attribute_", ""): value
                    for key, value in (item.get("attributes") or {}).items()
                    if value
                },
                "price": price,
                "regular_price": regular_price,
                "availability": availability_text,
            }
            if available is not None:
                variant["available"] = available
            image = self._variant_image(item)
            if image:
                variant["image"] = image
            variants.append({k: v for k, v in variant.items() if v not in (None, "", {}, [])})
        return variants

    def _availability_from_variant(self, item: Dict[str, Any]) -> Tuple[Optional[str], Optional[bool]]:
        availability_html = item.get("availability_html") or ""
        text = HTMLParser(availability_html).body.text(strip=True) if availability_html else ""
        availability, available = availability_from_text(text)
        if available is None and isinstance(item.get("is_in_stock"), bool):
            available = item.get("is_in_stock")
            availability = availability or ("En stock" if available else "Rupture de stock")
        if availability and "arrivage" in availability.lower():
            available = None
        return availability, available

    def _variant_image(self, item: Dict[str, Any]) -> Optional[str]:
        image = item.get("image")
        if not isinstance(image, dict):
            return None
        for key in ("full_src", "url", "src"):
            value = self._absolute_url(image.get(key))
            if value:
                return value
        return None

    def _extract_specifications(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        variants: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        specs: Dict[str, Any] = {}
        for row in tree.css(pp.get("specs_rows", ".woocommerce-product-attributes tr")):
            key_node = row.css_first("th, .woocommerce-product-attributes-item__label")
            value_node = row.css_first("td, .woocommerce-product-attributes-item__value")
            key = self._clean(key_node.text(strip=True) if key_node else None)
            value = self._clean(value_node.text(strip=True) if value_node else None)
            if key and value:
                specs[key] = value

        for row in tree.css(pp.get("variation_options", "table.variations tr")):
            label = self._clean(row.css_first("label").text(strip=True) if row.css_first("label") else None)
            options = [
                self._clean(option.text(strip=True))
                for option in row.css("select option")
                if option.attributes.get("value") and self._clean(option.text(strip=True))
            ]
            if label and options:
                specs[f"Options {label}"] = options

        if variants:
            attribute_keys = sorted(
                {
                    key
                    for variant in variants
                    for key in (variant.get("attributes") or {}).keys()
                }
            )
            if attribute_keys:
                specs["Variation attributes"] = attribute_keys
        return specs

    def _extract_detail_images(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        variants: List[Dict[str, Any]],
    ) -> List[str]:
        images: List[str] = []
        selector = pp.get("image_gallery", ".woocommerce-product-gallery img, img.wp-post-image")
        for img in tree.css(selector):
            for attr in ("data-large_image", "data-src", "src", "srcset"):
                value = img.attributes.get(attr)
                if attr == "srcset" and value:
                    value = value.split(",", 1)[0].strip().split(" ", 1)[0]
                image = self._absolute_url(value)
                if image and not image.startswith("data:") and image not in images:
                    images.append(image)
                    break
        for variant in variants:
            image = self._absolute_url(variant.get("image"))
            if image and image not in images:
                images.append(image)
        return images[:20]

    def _extract_datalayer_item(self, html: str) -> Dict[str, Any]:
        for match in re.finditer(r"dataLayer\.push\((\{.*?\})\);", html, re.S):
            raw = match.group(1)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            items = ((data.get("ecommerce") or {}).get("items") or [])
            if items and isinstance(items[0], dict):
                return items[0]
        return {}


def get_scraper(logger: logging.Logger) -> IspaceScraper:
    return IspaceScraper(logger)
