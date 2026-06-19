#!/usr/bin/env python3
"""
Para El Farabi scraper - WordPress/WooCommerce, HTTP/selectolax.
"""

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
    normalize_gtin,
    parse_price,
)


class ElFarabiScraper(FastScraper):
    """HTTPX/selectolax scraper for paraelfarabi.com."""

    ROOT_SLUGS = (
        "visage",
        "corps",
        "cheveux",
        "bebe-et-maman",
        "solaires",
        "complements-alimentaires",
        "materiel-medical",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("el_farabi", logger)

    def _absolute_url(self, href: Any) -> Optional[str]:
        return absolute_url(href, self.base_url)

    @staticmethod
    def _clean(value: Any) -> Optional[str]:
        return clean_text(value)

    @staticmethod
    def _text(node: Any, separator: str = " ") -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=separator, strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

    @staticmethod
    def _strip_url(url: str, keep_query: bool = False) -> str:
        parts = urlsplit(url)
        path = parts.path.rstrip("/") if parts.path != "/" else parts.path
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                path or "/",
                parts.query if keep_query else "",
                "",
            )
        )

    @staticmethod
    def _post_id_from_class(class_name: str) -> Optional[str]:
        match = re.search(r"(?:^|\s)(?:post|postid)-(\d+)(?:\s|$)", class_name or "")
        return match.group(1) if match else None

    @classmethod
    def _body_post_id(cls, tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        return cls._post_id_from_class(body.attributes.get("class", "") if body else "")

    @staticmethod
    def _first_srcset_url(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        first = value.split(",", 1)[0].strip()
        return first.split(" ", 1)[0] if first else None

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        return urlsplit(url).netloc.lower() == urlsplit(self.base_url).netloc.lower()

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
            "mon-espace",
            "wishlist",
            "search",
            "contact",
            "blog",
            "mailto:",
            "tel:",
            "javascript:",
            "#",
        )
        return "/categorie-produit/" in low and not any(token in low for token in blocked)

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
        menu_order = self._menu_category_order(tree, fp)

        categories = self._extract_categories_from_api(menu_order)
        if not categories:
            categories = self._extract_categories_from_links(
                tree.css(fp.get("fallback_links", "a[href*='/categorie-produit/']"))
            )

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _menu_category_order(self, tree: HTMLParser, fp: Dict[str, Any]) -> Dict[str, str]:
        order: Dict[str, str] = {}
        for link in tree.css(fp.get("nav_links", "nav a[href*='/categorie-produit/']")):
            url = self._absolute_url(link.attributes.get("href"))
            if url:
                url = self._strip_url(url)
            if not self._is_category_url(url):
                continue
            name = self._clean(link.text(strip=True))
            if name and url not in order:
                order[url] = name
        return order

    def _extract_categories_from_api(self, menu_order: Dict[str, str]) -> List[Dict[str, Any]]:
        items = self._fetch_category_api_items()
        if not items:
            return []

        by_id: Dict[int, Dict[str, Any]] = {}
        children: Dict[int, List[Dict[str, Any]]] = {}
        by_url: Dict[str, Dict[str, Any]] = {}

        for item in items:
            if not isinstance(item, dict):
                continue
            url = self._strip_url(item.get("link") or "")
            if not self._is_category_url(url):
                continue
            try:
                item_id = int(item.get("id"))
                parent_id = int(item.get("parent") or 0)
            except (TypeError, ValueError):
                continue
            normalized = {
                "id": item_id,
                "parent": parent_id,
                "name": self._clean(item.get("name")) or self._name_from_url(url),
                "slug": self._clean(item.get("slug")) or self._name_from_url(url).lower(),
                "url": url,
                "count": self._safe_int(item.get("count")),
            }
            by_id[item_id] = normalized
            by_url[url] = normalized
            children.setdefault(parent_id, []).append(normalized)

        for child_list in children.values():
            child_list.sort(key=lambda row: (row.get("name") or "").lower())

        roots = self._ordered_roots(menu_order, by_url, by_id)
        categories: List[Dict[str, Any]] = []
        seen_top_urls = set()
        for root in roots:
            if root["url"] in seen_top_urls or not self._has_product_bearing_node(root, children):
                continue
            seen_top_urls.add(root["url"])

            top_cat = {
                "name": menu_order.get(root["url"]) or root["name"],
                "url": root["url"],
                "level": "top",
                "category_id": str(root["id"]),
                "product_count_hint": root["count"],
                "low_level_categories": [],
            }

            for low in children.get(root["id"], []):
                if not self._has_product_bearing_node(low, children):
                    continue
                low_cat = {
                    "name": low["name"],
                    "url": low["url"],
                    "level": "low",
                    "category_id": str(low["id"]),
                    "product_count_hint": low["count"],
                    "subcategories": [],
                }

                for sub in self._flatten_product_bearing_descendants(low, children):
                    if sub["id"] == low["id"]:
                        continue
                    low_cat["subcategories"].append(
                        {
                            "name": sub["name"],
                            "url": sub["url"],
                            "level": "subcategory",
                            "category_id": str(sub["id"]),
                            "product_count_hint": sub["count"],
                        }
                    )

                top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)
        return categories

    def _fetch_category_api_items(self) -> List[Dict[str, Any]]:
        endpoint = self.selectors.get("frontpage", {}).get(
            "category_api", f"{self.base_url.rstrip('/')}/wp-json/wp/v2/product_cat"
        )
        items: List[Dict[str, Any]] = []
        headers = dict(self.headers)
        headers["Accept"] = "application/json"
        headers["Accept-Encoding"] = "gzip, deflate"
        try:
            with httpx.Client(headers=headers, follow_redirects=True, timeout=self.request_timeout) as client:
                page = 1
                total_pages = 1
                while page <= total_pages:
                    response = client.get(endpoint, params={"per_page": 100, "page": page})
                    response.raise_for_status()
                    page_items = response.json()
                    if not isinstance(page_items, list):
                        break
                    items.extend(page_items)
                    total_pages = self._safe_int(response.headers.get("x-wp-totalpages")) or total_pages
                    page += 1
        except Exception as exc:
            self.logger.warning(f"Failed to load WooCommerce categories API: {exc}")
            return []
        return items

    def _ordered_roots(
        self,
        menu_order: Dict[str, str],
        by_url: Dict[str, Dict[str, Any]],
        by_id: Dict[int, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        roots: List[Dict[str, Any]] = []
        seen_ids = set()

        for url in menu_order:
            item = by_url.get(url)
            if item and item["id"] not in seen_ids:
                roots.append(item)
                seen_ids.add(item["id"])

        for slug in self.ROOT_SLUGS:
            for item in by_id.values():
                if item.get("slug") == slug and item["id"] not in seen_ids:
                    roots.append(item)
                    seen_ids.add(item["id"])
                    break
        return roots

    def _has_product_bearing_node(
        self,
        item: Dict[str, Any],
        children: Dict[int, List[Dict[str, Any]]],
    ) -> bool:
        if item.get("count", 0) > 0:
            return True
        return any(self._has_product_bearing_node(child, children) for child in children.get(item["id"], []))

    def _flatten_product_bearing_descendants(
        self,
        item: Dict[str, Any],
        children: Dict[int, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if item.get("count", 0) > 0:
            out.append(item)
        for child in children.get(item["id"], []):
            if self._has_product_bearing_node(child, children):
                out.extend(self._flatten_product_bearing_descendants(child, children))
        return out

    def _extract_categories_from_links(self, links: List[Any]) -> List[Dict[str, Any]]:
        categories: List[Dict[str, Any]] = []
        seen_urls = set()
        for link in links:
            url = self._absolute_url(link.attributes.get("href"))
            if url:
                url = self._strip_url(url)
            if not self._is_category_url(url) or url in seen_urls:
                continue
            name = self._clean(link.text(strip=True)) or self._name_from_url(url)
            if not name:
                continue
            seen_urls.add(url)
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

    @staticmethod
    def _name_from_url(url: str) -> str:
        slug = urlsplit(url).path.strip("/").split("/")[-1]
        return re.sub(r"[-_]+", " ", slug).strip().title() or slug

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # Product listings
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products: List[Dict[str, Any]] = []

        for card in tree.css(cp.get("item_selector", "li.product.type-product")):
            url, name = self._link_and_name(card, cp)
            if not url or "/produit/" not in url or not name:
                continue

            product_id = self._extract_card_id(card, cp)
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

            identifier = self._extract_card_sku(card, cp)
            self._merge_identifier_fields(product, identifier, product_id)

            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available

            image = self._extract_image(
                card,
                cp.get("item_image", "img"),
                cp.get("item_image_attrs", ["data-src", "data-large_image", "src", "srcset"]),
            )
            if image:
                product["image"] = image

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "el_farabi listing")

    def _link_and_name(self, card: Any, cp: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        link = card.css_first(cp.get("item_url", "a.woocommerce-LoopProduct-link[href*='/produit/']"))
        url = self._absolute_url(link.attributes.get("href") if link else None)

        name_node = card.css_first(cp.get("item_name", "h2.woocommerce-loop-product__title"))
        name = self._text(name_node)
        if not name:
            img = card.css_first("img[alt]")
            name = self._clean(img.attributes.get("alt") if img else None)
        if not name:
            button = card.css_first("a.add_to_cart_button[aria-label]")
            label = button.attributes.get("aria-label", "") if button else ""
            match = re.search(r"[\"“](.*?)[\"”]", label)
            name = self._clean(match.group(1) if match else label)
        return (self._strip_url(url) if url else None), name

    def _extract_card_id(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        node = card.css_first(cp.get("item_id", "[data-product_id]"))
        product_id = self._clean(node.attributes.get("data-product_id") if node else None)
        return product_id or self._post_id_from_class(card.attributes.get("class", ""))

    def _extract_card_sku(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        node = card.css_first(cp.get("item_sku", "[data-product_sku]"))
        return self._clean(node.attributes.get("data-product_sku") if node else None)

    def _extract_price(self, root: Any, current_selector: str, fallback_selector: str) -> Optional[float]:
        node = root.css_first(current_selector) or root.css_first(fallback_selector)
        if node is None:
            node = root.css_first(".woocommerce-Price-amount bdi, bdi")
        return parse_price(node.text(strip=True) if node else None)

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

        for link in tree.css(cp.get("pagination_pages", "nav.woocommerce-pagination a.page-numbers")):
            href = link.attributes.get("href", "")
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

        next_link = tree.css_first(cp.get("pagination_next", "nav.woocommerce-pagination a.next.page-numbers"))
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

        self._clean_metadata_identifier(data, product_id)

        title_node = tree.css_first(pp.get("title", "h1.product_title, h1.entry-title, h1"))
        title = self._valid_title(self._text(title_node))
        if title:
            data["title"] = title
        elif not self._valid_title(data.get("title")):
            fallback_title = self._valid_title(self._image_alt_title(tree)) or self._title_from_url(url)
            if fallback_title:
                data["title"] = fallback_title

        sku_node = tree.css_first(pp.get("sku", ".sku_wrapper .sku"))
        self._merge_identifier_fields(data, self._text(sku_node), product_id)

        price = self._detail_price(tree, pp)
        if price is not None:
            data["price"] = price

        old_price_node = tree.css_first(pp.get("old_price", "p.price del .woocommerce-Price-amount bdi"))
        old_price = parse_price(old_price_node.text(strip=True) if old_price_node else None)
        if old_price is not None:
            data["old_price"] = old_price
            if data.get("price") and old_price != data["price"]:
                data["discount_percent"] = round((1 - data["price"] / old_price) * 100)

        availability, available = self._detail_availability(tree, pp, data)
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
        description = self._clean_description_text(self._text(description_node, separator="\n"))
        if description:
            data["description"] = description

        specs = self._extract_specifications(tree, pp, overview_node, description_node)
        if specs:
            data["specifications"] = specs

        images = self._extract_detail_images(tree, pp)
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

        return finalize_product_record(data)

    def _extract_detail_product_id(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("product_id", "button[name='add-to-cart'][value]"))
        if node:
            for attr in ("value", "data-product_id", "data-product-id"):
                product_id = self._clean(node.attributes.get(attr))
                if product_id:
                    return product_id
        return self._body_post_id(tree)

    def _detail_price(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[float]:
        node = tree.css_first(pp.get("current_price", "p.price ins .woocommerce-Price-amount bdi"))
        if node is None:
            node = tree.css_first(pp.get("price", "p.price .woocommerce-Price-amount bdi"))
        return parse_price(node.text(strip=True) if node else None)

    def _detail_availability(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        data: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[bool]]:
        node = tree.css_first(pp.get("availability", ".stock, .out-of-stock, .in-stock"))
        if node:
            return availability_from_text(node.text(strip=True))
        if data.get("availability"):
            return availability_from_text(data.get("availability"))
        body = tree.css_first("body")
        class_name = (body.attributes.get("class", "") if body else "").lower()
        if "outofstock" in class_name:
            return "Rupture de stock", False
        if "instock" in class_name:
            return "En stock", True
        return None, None

    def _extract_specifications(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        overview_node: Any,
        description_node: Any,
    ) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for row in tree.css(pp.get("specs_rows", ".woocommerce-product-attributes tr")):
            key_node = row.css_first("th, .woocommerce-product-attributes-item__label")
            value_node = row.css_first("td, .woocommerce-product-attributes-item__value")
            key = self._text(key_node)
            value = self._text(value_node)
            if key and value:
                specs[key] = value

        for node in (overview_node, description_node):
            if node:
                for key, value in self._specs_from_text_node(node).items():
                    specs.setdefault(key, value)
        return specs

    def _specs_from_text_node(self, node: Any) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        candidates = node.css("li, p")
        if not candidates:
            candidates = [node]
        for child in candidates:
            text = self._text(child)
            if not text or ":" not in text or len(text) > 180:
                continue
            key, value = text.split(":", 1)
            key = self._clean(key)
            value = self._clean(value)
            if key and value and len(key) <= 60 and key.lower() not in {"description"}:
                specs[key] = value
        return specs

    def _extract_detail_images(self, tree: HTMLParser, pp: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        selector = pp.get("image_gallery", ".woocommerce-product-gallery img, img.wp-post-image")
        for img in tree.css(selector):
            for attr in ("data-large_image", "data-src", "src", "srcset"):
                value = img.attributes.get(attr)
                if attr == "srcset":
                    value = self._first_srcset_url(value)
                image = self._absolute_url(value)
                if image and not image.startswith("data:") and image not in images:
                    images.append(image)
                    break
        return images[:20]

    @staticmethod
    def _clean_description_text(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"^(?:Description\s*){2,}", "Description ", text, flags=re.I)
        text = re.sub(
            r"\s*Quality:\s*1\s*2\s*3\s*4\s*5\s*Title:\s*\*\s*Comment:\s*\*\s*\*\s*Required fields\s*Submit or Cancel\s*$",
            "",
            text,
            flags=re.I,
        )
        return clean_text(text)

    # ------------------------------------------------------------------
    # Identifier cleanup
    # ------------------------------------------------------------------

    def _clean_metadata_identifier(self, data: Dict[str, Any], product_id: Optional[str]) -> None:
        raw = data.get("reference") or data.get("sku")
        if raw:
            self._merge_identifier_fields(data, raw, product_id, replace_existing=True)

    def _merge_identifier_fields(
        self,
        data: Dict[str, Any],
        raw_identifier: Any,
        product_id: Optional[str],
        replace_existing: bool = True,
    ) -> None:
        fields = self._identifier_fields(raw_identifier, product_id)
        if replace_existing:
            data.pop("reference", None)
            data.pop("sku", None)

        data_quality = fields.pop("data_quality", None)
        if data_quality:
            data.setdefault("data_quality", {}).update(data_quality)
        data.update(fields)

    def _identifier_fields(self, raw_identifier: Any, product_id: Optional[str]) -> Dict[str, Any]:
        value = self._normalize_identifier_value(raw_identifier)
        if not value or (product_id and value == str(product_id)):
            return {}

        gtin = normalize_gtin(value)
        if gtin:
            return {"barcode": gtin}

        if re.fullmatch(r"\d{8}|\d{12,14}", value):
            return {"data_quality": {"invalid_barcode": value}}

        if value.isdigit():
            return {}

        return {"reference": value, "sku": value}

    @staticmethod
    def _normalize_identifier_value(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if re.fullmatch(r"\d+\.0", text):
            text = text[:-2]
        return text

    # ------------------------------------------------------------------
    # Detail fallbacks
    # ------------------------------------------------------------------

    @staticmethod
    def _valid_title(value: Any) -> Optional[str]:
        title = clean_text(value)
        if not title:
            return None
        stripped = title.strip(" -")
        if not stripped or stripped.lower() == "parafarabi":
            return None
        return title

    def _image_alt_title(self, tree: HTMLParser) -> Optional[str]:
        img = tree.css_first(".woocommerce-product-gallery img[alt], img.wp-post-image[alt]")
        return self._clean(img.attributes.get("alt") if img else None)

    @staticmethod
    def _title_from_url(url: str) -> Optional[str]:
        slug = urlsplit(url).path.strip("/").split("/")[-1]
        if not slug or slug.isdigit():
            return None
        return re.sub(r"[-_]+", " ", slug).strip().title()


def get_scraper(logger: logging.Logger) -> ElFarabiScraper:
    return ElFarabiScraper(logger)
