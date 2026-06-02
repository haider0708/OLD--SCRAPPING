#!/usr/bin/env python3
"""
Pstore.tn scraper - WordPress/WooCommerce + Woodmart, HTTP/selectolax.
"""

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx
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


class PsstoreScraper(FastScraper):
    """HTTPX/selectolax scraper for pstore.tn."""

    def __init__(self, logger: logging.Logger):
        super().__init__("psstore", logger)

    # ------------------------------------------------------------------
    # URL and text helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, href: Any) -> Optional[str]:
        return absolute_url(href, self.base_url)

    @staticmethod
    def _clean(value: Any) -> Optional[str]:
        return clean_text(value)

    @staticmethod
    def _strip_url(url: str, keep_query: bool = False) -> str:
        parts = urlsplit(url)
        path = parts.path.rstrip("/") if parts.path != "/" else parts.path
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                path or "/",
                parts.query if keep_query else "",
                "",
            )
        )

    @staticmethod
    def _text(node: Any, separator: str = " ") -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=separator, strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

    @staticmethod
    def _post_id_from_class(class_name: str) -> Optional[str]:
        match = re.search(r"(?:^|\s)(?:post|postid)-(\d+)(?:\s|$)", class_name or "")
        return match.group(1) if match else None

    @classmethod
    def _body_post_id(cls, tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        return cls._post_id_from_class(body.attributes.get("class", "") if body else "")

    @staticmethod
    def _direct_children(node: Any, selector: str) -> List[Any]:
        return [child for child in node.css(selector) if child.parent == node]

    @staticmethod
    def _first_direct_link(node: Any) -> Optional[Any]:
        for link in node.css("a[href]"):
            if link.parent == node:
                return link
        return node.css_first("a[href]")

    @staticmethod
    def _first_srcset_url(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        first = value.split(",", 1)[0].strip()
        return first.split(" ", 1)[0] if first else None

    @staticmethod
    def _same_identifier(left: Any, right: Any) -> bool:
        return bool(left and right and str(left).strip() == str(right).strip())

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        host = urlsplit(url).netloc.lower()
        return host == urlsplit(self.base_url).netloc.lower()

    def _is_category_url(self, url: Optional[str]) -> bool:
        if not url or not self._is_site_url(url):
            return False
        low = url.lower()
        blocked = (
            "/produit/",
            "add-to-cart",
            "cart",
            "panier",
            "checkout",
            "account",
            "mon-compte",
            "wishlist",
            "search",
            "recherche",
            "contact",
            "blog",
            "mailto:",
            "tel:",
            "javascript:",
            "#",
            "facebook.",
            "instagram.",
            "maps.",
        )
        return "/product-category/" in low and not any(token in low for token in blocked)

    def _category_from_link(self, link: Any) -> Optional[Dict[str, str]]:
        if not link:
            return None
        url = self._absolute_url(link.attributes.get("href"))
        if url:
            url = self._strip_url(url)
        if not self._is_category_url(url):
            return None
        name = self._clean(link.text(strip=True))
        if not name or len(name) > 100:
            return None
        return {"name": name, "url": url}

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        path = re.sub(r"/page/\d+/?$", "", parts.path or "/").rstrip("/")
        if page_num > 1:
            path = f"{path}/page/{page_num}"
        path = path or "/"
        if path != "/" and not path.endswith("/"):
            path += "/"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories: List[Dict[str, Any]] = []
        seen_urls = set()

        for root in tree.css(fp.get("mobile_menu", "ul.mobile-pages-menu.menu")):
            top_items = self._direct_children(root, "li")
            for top_li in top_items:
                top_meta = self._category_from_link(self._first_direct_link(top_li))
                if not top_meta or top_meta["url"] in seen_urls:
                    continue
                seen_urls.add(top_meta["url"])

                top_cat = {
                    "name": top_meta["name"],
                    "url": top_meta["url"],
                    "level": "top",
                    "low_level_categories": [],
                }

                for low_li in self._direct_child_category_items(top_li):
                    low_meta = self._category_from_link(self._first_direct_link(low_li))
                    if not low_meta or low_meta["url"] in seen_urls:
                        continue
                    seen_urls.add(low_meta["url"])
                    low_cat = {
                        "name": low_meta["name"],
                        "url": low_meta["url"],
                        "level": "low",
                        "subcategories": [],
                    }

                    for sub_meta in self._descendant_category_links(low_li):
                        if sub_meta["url"] in seen_urls:
                            continue
                        seen_urls.add(sub_meta["url"])
                        low_cat["subcategories"].append(
                            {
                                "name": sub_meta["name"],
                                "url": sub_meta["url"],
                                "level": "subcategory",
                            }
                        )

                    top_cat["low_level_categories"].append(low_cat)

                categories.append(top_cat)

        if not categories:
            categories = self._extract_categories_from_links(
                tree.css(fp.get("fallback_links", ".menu a[href], header a[href]")),
                seen_urls,
            )

        if not categories:
            categories = self._extract_categories_from_sitemap(seen_urls)

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _direct_child_category_items(self, node: Any) -> List[Any]:
        out: List[Any] = []
        for submenu in self._direct_children(node, "ul"):
            out.extend(self._direct_children(submenu, "li"))
        return out

    def _descendant_category_links(self, node: Any) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for submenu in self._direct_children(node, "ul"):
            for child_li in self._direct_children(submenu, "li"):
                meta = self._category_from_link(self._first_direct_link(child_li))
                if meta:
                    out.append(meta)
                out.extend(self._descendant_category_links(child_li))
        return out

    def _extract_categories_from_links(self, links: List[Any], seen_urls: set) -> List[Dict[str, Any]]:
        categories: List[Dict[str, Any]] = []
        for link in links:
            meta = self._category_from_link(link)
            if not meta or meta["url"] in seen_urls:
                continue
            seen_urls.add(meta["url"])
            categories.append(
                {
                    "name": meta["name"],
                    "url": meta["url"],
                    "level": "top",
                    "low_level_categories": [],
                }
            )
        return categories

    def _extract_categories_from_sitemap(self, seen_urls: set) -> List[Dict[str, Any]]:
        sitemap_url = self.selectors.get("frontpage", {}).get(
            "sitemap_url", f"{self.base_url.rstrip('/')}/product_cat-sitemap.xml"
        )
        categories: List[Dict[str, Any]] = []
        try:
            response = httpx.get(
                sitemap_url,
                headers={"User-Agent": self.headers.get("User-Agent", "Mozilla/5.0")},
                follow_redirects=True,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
        except Exception as exc:
            self.logger.warning(f"Failed category sitemap fallback {sitemap_url}: {exc}")
            return categories

        for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", response.text, flags=re.I | re.S):
            url = self._strip_url(html_lib.unescape(loc.strip()))
            if not self._is_category_url(url) or url in seen_urls:
                continue
            seen_urls.add(url)
            name = self._name_from_category_url(url)
            categories.append(
                {
                    "name": name,
                    "url": url,
                    "level": "top",
                    "low_level_categories": [],
                }
            )
        return categories

    @staticmethod
    def _name_from_category_url(url: str) -> str:
        slug = urlsplit(url).path.strip("/").split("/")[-1]
        return re.sub(r"[-_]+", " ", slug).strip().title() or slug

    @staticmethod
    def _category_stats(categories: List[Dict[str, Any]]) -> Dict[str, int]:
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
        return stats

    # ------------------------------------------------------------------
    # Product listings
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products: List[Dict[str, Any]] = []

        for card in tree.css(cp.get("item_selector", "div.product.type-product")):
            url, name = self._link_and_name(card, cp.get("item_url", "a[href*='/produit/']"))
            if not url or "/produit/" not in url or not name:
                continue

            product_id = self._extract_card_id(card, cp)
            sku = self._extract_card_sku(card, cp, product_id)
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

            discount = self._extract_discount(card, cp, price, old_price)
            if old_price is not None:
                product["old_price"] = old_price
            if discount is not None:
                product["discount_percent"] = discount
            if sku:
                product["reference"] = sku
                product["sku"] = sku
            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available

            brand = self._extract_listing_brand(card, cp)
            if brand:
                product["brand"] = brand

            categories = self._extract_listing_categories(card, cp)
            if categories:
                product["listing_categories"] = categories

            image = self._extract_image(
                card,
                cp.get("item_image", "img"),
                cp.get("item_image_attrs", ["data-src", "data-large_image", "src", "srcset"]),
            )
            if image:
                product["image"] = image

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "psstore listing")

    def _link_and_name(self, card: Any, selector: str) -> Tuple[Optional[str], Optional[str]]:
        chosen = None
        for link in card.css(selector):
            href = self._absolute_url(link.attributes.get("href"))
            if not href or "/produit/" not in href:
                continue
            if self._clean(link.text(strip=True)):
                chosen = link
                break
            chosen = chosen or link
        if chosen is None:
            return None, None

        url = self._absolute_url(chosen.attributes.get("href"))
        name = self._clean(chosen.text(strip=True))
        if not name:
            img = card.css_first("img[alt]")
            name = self._clean(img.attributes.get("alt") if img else None)
        return (self._strip_url(url) if url else None), name

    def _extract_card_id(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        node = card.css_first(cp.get("item_id", "[data-product_id]"))
        product_id = node.attributes.get("data-product_id") if node else None
        return self._clean(product_id) or self._post_id_from_class(card.attributes.get("class", ""))

    def _extract_card_sku(
        self,
        card: Any,
        cp: Dict[str, Any],
        product_id: Optional[str],
    ) -> Optional[str]:
        node = card.css_first(cp.get("item_sku", "[data-product_sku]"))
        sku = self._clean(node.attributes.get("data-product_sku") if node else None)
        if not sku or self._same_identifier(sku, product_id):
            return None
        return sku

    def _extract_price(self, root: Any, current_selector: str, fallback_selector: str) -> Optional[float]:
        node = root.css_first(current_selector) or root.css_first(fallback_selector)
        if node is None:
            node = root.css_first(".woocommerce-Price-amount bdi, bdi")
        return parse_price(node.text(strip=True) if node else None)

    def _extract_discount(
        self,
        card: Any,
        cp: Dict[str, Any],
        price: Optional[float],
        old_price: Optional[float],
    ) -> Optional[int]:
        node = card.css_first(cp.get("item_discount", ".onsale.product-label"))
        text = self._clean(node.text(strip=True) if node else None)
        if text:
            match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", text)
            if match:
                return round(float(match.group(1).replace(",", ".")))
        if price and old_price and old_price != price:
            return round((1 - price / old_price) * 100)
        return None

    def _availability_from_card(self, card: Any) -> Tuple[Optional[str], Optional[bool]]:
        class_name = (card.attributes.get("class") or "").lower()
        text = self._text(card) or ""
        low_text = text.lower()
        if "outofstock" in class_name or "rupture" in low_text:
            return "Rupture de stock", False
        if "available-on-backorder" in class_name or "arrivage" in low_text:
            return "En Arrivage", None
        if "instock" in class_name:
            return "En stock", True
        return availability_from_text(text)

    def _extract_listing_brand(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        node = card.css_first(cp.get("item_brand", ".wd-product-brands a"))
        return self._clean(node.text(strip=True) if node else None)

    def _extract_listing_categories(self, card: Any, cp: Dict[str, Any]) -> List[str]:
        categories: List[str] = []
        for node in card.css(cp.get("item_categories", ".wd-product-cats a")):
            name = self._clean(node.text(strip=True))
            if name and name not in categories:
                categories.append(name)
        return categories

    def _extract_image(self, root: Any, selector: str, attrs: List[str]) -> Optional[str]:
        for img in root.css(selector):
            for attr in attrs:
                value = img.attributes.get(attr)
                if attr == "srcset":
                    value = self._first_srcset_url(value)
                image = self._absolute_url(value)
                if image and not image.startswith("data:"):
                    return image
        return None

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1

        current = tree.css_first("nav.woocommerce-pagination .page-numbers.current, .page-numbers.current")
        if current:
            try:
                current_page = int(current.text(strip=True))
            except (TypeError, ValueError):
                pass

        for link in tree.css(cp.get("pagination_pages", "a.page-numbers")):
            href = link.attributes.get("href", "")
            if "per_page=" in href:
                continue
            text = self._clean(link.text(strip=True))
            page_num = None
            if text and text.isdigit():
                page_num = int(text)
            else:
                match = re.search(r"/page/(\d+)/?", href)
                if match:
                    page_num = int(match.group(1))
            if page_num:
                total_pages = max(total_pages, page_num)

        next_link = tree.css_first(cp.get("pagination_next", "a.next.page-numbers"))
        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": bool(next_link),
        }

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = {"url": self._strip_url(url)}

        product_id = self._extract_detail_product_id(tree, pp)
        metadata = html_product_metadata(html, url, self.base_url)
        data.update(metadata)
        data["url"] = self._strip_url(data.get("url") or url)
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        variants = self._extract_variants(tree, pp)
        if variants:
            data["variants"] = variants

        title_node = tree.css_first(pp.get("title", "h1.product_title, h1.entry-title, h1"))
        title = self._text(title_node)
        if title:
            data["title"] = title

        brand = self._extract_detail_brand(tree, pp)
        if brand:
            data["brand"] = brand

        sku = self._extract_detail_sku(tree, pp, html, variants, product_id)
        if sku:
            data["reference"] = sku
            data["sku"] = sku

        price = self._detail_price(tree, pp, variants)
        if price is not None:
            data["price"] = price

        old_price = self._detail_old_price(tree, pp, html)
        if old_price is not None:
            data["old_price"] = old_price
            if data.get("price") and old_price != data["price"]:
                data["discount_percent"] = round((1 - data["price"] / old_price) * 100)

        availability, available = self._detail_availability(tree, pp, variants, html)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        overview_node = tree.css_first(pp.get("overview", ".woocommerce-product-details__short-description"))
        overview = self._text(overview_node, separator="\n")
        if overview:
            data["overview"] = overview
            data.setdefault("description", overview)

        description_node = tree.css_first(pp.get("description", "#tab-description, .woocommerce-Tabs-panel--description"))
        description = self._text(description_node, separator="\n")
        if description:
            data["description"] = description

        specs = self._extract_specifications(tree, pp, overview_node, variants)
        if specs:
            data["specifications"] = specs

        images = self._extract_detail_images(tree, pp, variants)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = [
            self._clean(node.text(strip=True))
            for node in tree.css(pp.get("categories", ".posted_in a"))
            if self._clean(node.text(strip=True))
        ]
        if categories:
            data["categories"] = categories

        self._remove_product_id_as_sku(data)
        return finalize_product_record(data)

    def _extract_detail_product_id(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("product_id", "button[name='add-to-cart'][value]"))
        if node:
            for attr in ("value", "data-product_id", "data-product-id"):
                product_id = self._clean(node.attributes.get(attr))
                if product_id:
                    return product_id
        body_id = self._body_post_id(tree)
        if body_id:
            return body_id
        form = tree.css_first("form.variations_form[data-product_id]")
        return self._clean(form.attributes.get("data-product_id") if form else None)

    def _extract_detail_brand(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("brand", ".wd-product-brands a"))
        brand = self._clean(node.text(strip=True) if node else None)
        if brand:
            return brand
        for node in tree.css(".posted_in a"):
            text = self._clean(node.text(strip=True))
            href = node.attributes.get("href", "").lower()
            if text and any(token in href for token in ("/brands/", "/samsung/", "/apple/")):
                return text
        return None

    def _extract_detail_sku(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        html: str,
        variants: List[Dict[str, Any]],
        product_id: Optional[str],
    ) -> Optional[str]:
        sku_node = tree.css_first(pp.get("sku", ".sku_wrapper .sku"))
        sku = self._clean(sku_node.text(strip=True) if sku_node else None)
        if self._is_meaningful_sku(sku, product_id):
            return sku

        data_layer = self._extract_datalayer_item(html)
        for key in ("sku", "item_id"):
            sku = self._clean(data_layer.get(key))
            if self._is_meaningful_sku(sku, product_id):
                return sku

        for variant in variants:
            sku = self._clean(variant.get("sku"))
            if self._is_meaningful_sku(sku, product_id):
                return sku
        return None

    @staticmethod
    def _is_meaningful_sku(sku: Optional[str], product_id: Optional[str]) -> bool:
        if not sku:
            return False
        if sku.upper() in {"ND", "N/A", "NA", "SKU"}:
            return False
        if product_id and sku == product_id:
            return False
        return not (sku.isdigit() and product_id and sku == product_id)

    def _detail_price(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        variants: List[Dict[str, Any]],
    ) -> Optional[float]:
        variant_prices = [v.get("price") for v in variants if v.get("price") is not None]
        if variant_prices:
            return min(variant_prices)
        node = tree.css_first(pp.get("current_price", "p.price ins .woocommerce-Price-amount bdi"))
        if node is None:
            node = tree.css_first(pp.get("price", "p.price .woocommerce-Price-amount bdi"))
        return parse_price(node.text(strip=True) if node else None)

    def _detail_old_price(self, tree: HTMLParser, pp: Dict[str, Any], html: str) -> Optional[float]:
        node = tree.css_first(pp.get("old_price", "p.price del .woocommerce-Price-amount bdi"))
        old_price = parse_price(node.text(strip=True) if node else None)
        if old_price is not None:
            return old_price
        price_data = self._jsonld_price_data(html)
        return price_data.get("old_price")

    def _detail_availability(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        variants: List[Dict[str, Any]],
        html: str,
    ) -> Tuple[Optional[str], Optional[bool]]:
        node = tree.css_first(pp.get("availability", ".stock, .out-of-stock, .in-stock"))
        if node:
            return availability_from_text(node.text(strip=True))
        if variants:
            if any(variant.get("available") is True for variant in variants):
                return "En stock", True
            if all(variant.get("available") is False for variant in variants):
                return "Rupture de stock", False
        body = tree.css_first("body")
        class_name = body.attributes.get("class", "") if body else ""
        if "outofstock" in class_name:
            return "Rupture de stock", False
        if "instock" in class_name:
            return "En stock", True
        return availability_from_text(html_product_metadata(html, base_url=self.base_url).get("availability"))

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

        variants: List[Dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            availability, available = self._availability_from_variant(item)
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
                "availability": availability,
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
        overview_node: Any,
        variants: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        specs: Dict[str, Any] = {}
        for row in tree.css(pp.get("specs_rows", ".woocommerce-product-attributes tr")):
            key_node = row.css_first("th, .woocommerce-product-attributes-item__label")
            value_node = row.css_first("td, .woocommerce-product-attributes-item__value")
            key = self._text(key_node)
            value = self._text(value_node)
            if key and value:
                specs[key] = value

        if overview_node:
            for key, value in self._specs_from_overview(overview_node).items():
                specs.setdefault(key, value)

        for row in tree.css(pp.get("variation_options", "table.variations tr")):
            label = self._text(row.css_first("label"))
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

    def _specs_from_overview(self, overview_node: Any) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        nodes = overview_node.css("li, p, div")
        if not nodes:
            nodes = [overview_node]
        for node in nodes:
            text = self._text(node, separator=" ")
            if not text or ":" not in text:
                continue
            if len(text) > 160:
                continue
            key, value = text.split(":", 1)
            key = self._clean(key)
            value = self._clean(value)
            if key and value and len(key) <= 60:
                specs[key] = value
        return specs

    def _extract_detail_images(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        variants: List[Dict[str, Any]],
    ) -> List[str]:
        images: List[str] = []
        selector = pp.get("image_gallery", ".woocommerce-product-gallery img, .product-image-summary img")
        for img in tree.css(selector):
            for attr in ("data-large_image", "data-src", "src", "srcset"):
                value = img.attributes.get(attr)
                if attr == "srcset":
                    value = self._first_srcset_url(value)
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

    def _jsonld_price_data(self, html: str) -> Dict[str, Optional[float]]:
        out: Dict[str, Optional[float]] = {"price": None, "old_price": None}
        for script in HTMLParser(html).css("script[type='application/ld+json']"):
            raw = script.text()
            if not raw:
                continue
            try:
                parsed = json.loads(html_lib.unescape(raw.strip()))
            except (TypeError, json.JSONDecodeError):
                continue
            for obj in self._walk_json(parsed):
                if not isinstance(obj, dict) or "product" not in self._type_names(obj):
                    continue
                offers = obj.get("offers")
                offer_items = offers if isinstance(offers, list) else [offers]
                for offer in offer_items:
                    if not isinstance(offer, dict):
                        continue
                    price = parse_price(offer.get("price"))
                    if price is not None:
                        out["price"] = out["price"] or price
                    specs = offer.get("priceSpecification")
                    spec_items = specs if isinstance(specs, list) else [specs]
                    for spec in spec_items:
                        if not isinstance(spec, dict):
                            continue
                        spec_price = parse_price(spec.get("price"))
                        price_type = str(spec.get("priceType") or "").lower()
                        if spec_price is None:
                            continue
                        if "listprice" in price_type or "strikethrough" in price_type:
                            out["old_price"] = spec_price
                        else:
                            out["price"] = out["price"] or spec_price
        return out

    def _remove_product_id_as_sku(self, data: Dict[str, Any]) -> None:
        product_id = self._clean(data.get("product_id") or data.get("id"))
        if not product_id:
            return
        for key in ("reference", "sku"):
            value = self._clean(data.get(key))
            if value and value == product_id and value.isdigit():
                data.pop(key, None)

    @staticmethod
    def _walk_json(value: Any) -> List[Any]:
        out: List[Any] = []
        if isinstance(value, dict):
            out.append(value)
            for child in value.values():
                out.extend(PsstoreScraper._walk_json(child))
        elif isinstance(value, list):
            for item in value:
                out.extend(PsstoreScraper._walk_json(item))
        return out

    @staticmethod
    def _type_names(value: Dict[str, Any]) -> List[str]:
        raw = value.get("@type")
        if isinstance(raw, list):
            return [str(item).lower() for item in raw]
        return [str(raw).lower()] if raw is not None else []


def get_scraper(logger: logging.Logger) -> PsstoreScraper:
    return PsstoreScraper(logger)
