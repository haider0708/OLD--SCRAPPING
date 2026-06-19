#!/usr/bin/env python3
"""
Mabrouk scraper - WordPress/WooCommerce + Stockie, HTTP/selectolax.
"""

import html as html_lib
import json
import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
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
    normalize_url,
    parse_price,
)


class MabroukScraper(FastScraper):
    """HTTP scraper for mabrouk.tn WooCommerce pages."""

    CATEGORY_PREFIX = "/categorie-produit/"
    PRODUCT_PREFIX = "/produit/"
    ALL_PRODUCTS_PATH = "/boutique"
    BAD_CATEGORY_PARTS = (
        "/produit/",
        "/mon-compte",
        "/my-account",
        "/account",
        "/cart",
        "/panier",
        "/checkout",
        "/commander",
        "/search",
        "/wishlist",
        "/compare",
        "/contact",
        "/blog",
        "/faq",
        "/category/",
        "/etiquette-produit/",
        "/product-tag/",
        "/wp-content/",
        "/wp-json/",
        "/feed",
        "facebook",
        "instagram",
        "tiktok",
        "mailto:",
        "tel:",
        "javascript:",
    )

    TAB_LABELS = {
        "description_produit": "Description",
        "additional_information": "Composition",
        "composition": "Composition",
        "matiere": "Matiere",
        "description": "Matiere",
        "entretien_produit": "Entretien",
    }

    def __init__(self, logger: logging.Logger):
        super().__init__("mabrouk", logger)
        self._store_category_cache: Optional[List[Dict[str, Any]]] = None
        self._wp_category_cache: Optional[List[Dict[str, Any]]] = None
        self._category_by_url: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

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
    def _safe_int(value: Any) -> Optional[int]:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _strip_url(url: Any, keep_query: bool = False) -> str:
        text = clean_text(url) or ""
        parts = urlsplit(text)
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
    def _first_srcset_url(value: Optional[str], prefer_largest: bool = False) -> Optional[str]:
        if not value:
            return None
        entries = [chunk.strip() for chunk in value.split(",") if chunk.strip()]
        if not entries:
            return None
        chosen = entries[-1] if prefer_largest else entries[0]
        return chosen.split(" ", 1)[0] if chosen else None

    @staticmethod
    def _dedupe_list(values: Iterable[Any]) -> List[str]:
        seen = set()
        out: List[str] = []
        for value in values:
            text = clean_text(value)
            if not text:
                continue
            key = normalize_url(text) or text
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
        return out

    @staticmethod
    def _post_id_from_class(class_name: str) -> Optional[str]:
        match = re.search(r"(?:^|\s)(?:post|postid)-(\d+)(?:\s|$)", class_name or "")
        return match.group(1) if match else None

    @classmethod
    def _body_post_id(cls, tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        return cls._post_id_from_class(body.attributes.get("class", "") if body else "")

    @staticmethod
    def _name_from_url(url: str) -> str:
        slug = urlsplit(url).path.strip("/").split("/")[-1]
        return re.sub(r"[-_]+", " ", slug).strip().title() or slug

    @classmethod
    def _walk_json(cls, value: Any) -> Iterable[Any]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from cls._walk_json(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._walk_json(child)

    @staticmethod
    def _type_names(value: Dict[str, Any]) -> List[str]:
        raw = value.get("@type")
        if isinstance(raw, list):
            return [str(item).lower() for item in raw]
        return [str(raw).lower()] if raw is not None else []

    def _sync_headers(self) -> Dict[str, str]:
        headers = dict(self.headers)
        headers["Accept-Encoding"] = "gzip, deflate"
        headers.setdefault("Accept-Language", "fr-FR,fr;q=0.9,en;q=0.8")
        headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        return headers

    def _json_headers(self) -> Dict[str, str]:
        headers = self._sync_headers()
        headers["Accept"] = "application/json,text/plain,*/*"
        return headers

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        return host == base_host

    def _is_category_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        url = self._absolute_url(url)
        if not url or not self._is_site_url(url):
            return False
        parts = urlsplit(url)
        path = parts.path.rstrip("/") or "/"
        low = url.lower()
        if not path.startswith(self.CATEGORY_PREFIX):
            return False
        if path == self.CATEGORY_PREFIX.rstrip("/"):
            return False
        if "." in path.rsplit("/", 1)[-1]:
            return False
        if any(token in low for token in self.BAD_CATEGORY_PARTS):
            return False
        if "non-classe" in low or "uncategorized" in low:
            return False
        return True

    def _is_product_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        url = self._absolute_url(url)
        if not url or not self._is_site_url(url):
            return False
        path = urlsplit(url).path.rstrip("/")
        return path.startswith(self.PRODUCT_PREFIX) and path != self.PRODUCT_PREFIX.rstrip("/")

    def _html_text(self, value: Any) -> Optional[str]:
        raw = self._clean(value)
        if not raw:
            return None
        if "<" not in raw or ">" not in raw:
            return raw
        tree = HTMLParser(raw)
        try:
            return clean_text(tree.body.text(separator=" ", strip=True) if tree.body else tree.text(separator=" ", strip=True))
        except TypeError:
            return clean_text(tree.text(strip=True))

    @staticmethod
    def _parse_price(value: Any) -> Optional[float]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"\b(?:TND|DT)\b", "", text, flags=re.I)
        return parse_price(text)

    @staticmethod
    def _meaningful_sku(sku: Optional[str], product_id: Optional[str]) -> bool:
        if not sku:
            return False
        if sku.upper() in {"N/A", "NA", "SKU", "ND", "VIDE"}:
            return False
        if product_id and sku == product_id:
            return False
        return True

    @staticmethod
    def _clean_title(value: Any) -> Optional[str]:
        title = clean_text(value)
        if not title:
            return None
        title = re.sub(r"\s*-\s*Mabrouk\s*$", "", title, flags=re.I)
        title = re.sub(r"\s*\|\s*Mabrouk\s*$", "", title, flags=re.I)
        return clean_text(title)

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        categories = self._extract_categories_from_store_categories()
        if not categories:
            categories = self._extract_categories_from_wp_api()
        if not categories:
            categories = self._extract_categories_from_sitemap()
        if not categories:
            categories = self._extract_categories_from_menu(HTMLParser(html))

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _fetch_store_categories(self) -> List[Dict[str, Any]]:
        if self._store_category_cache is not None:
            return self._store_category_cache
        endpoint = self.selectors.get("frontpage", {}).get(
            "store_categories_api",
            f"{self.base_url.rstrip('/')}/wp-json/wc/store/v1/products/categories",
        )
        items: List[Dict[str, Any]] = []
        try:
            response = httpx.get(
                endpoint,
                headers=self._json_headers(),
                follow_redirects=True,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                items = [item for item in payload if isinstance(item, dict)]
        except Exception as exc:
            self.logger.warning(f"Failed Mabrouk Store API category extraction: {exc}")
        self._store_category_cache = items
        return items

    def _fetch_wp_categories(self) -> List[Dict[str, Any]]:
        if self._wp_category_cache is not None:
            return self._wp_category_cache
        endpoint = self.selectors.get("frontpage", {}).get(
            "product_cat_api",
            f"{self.base_url.rstrip('/')}/wp-json/wp/v2/product_cat",
        )
        items: List[Dict[str, Any]] = []
        page = 1
        try:
            with httpx.Client(headers=self._json_headers(), follow_redirects=True, timeout=self.request_timeout) as client:
                while True:
                    response = client.get(endpoint, params={"per_page": 100, "page": page})
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, list) or not payload:
                        break
                    items.extend([item for item in payload if isinstance(item, dict)])
                    total_pages = self._safe_int(response.headers.get("x-wp-totalpages")) or 1
                    if page >= total_pages:
                        break
                    page += 1
        except Exception as exc:
            self.logger.warning(f"Failed Mabrouk wp/v2 product_cat fallback: {exc}")
        self._wp_category_cache = items
        return items

    def _extract_categories_from_store_categories(self) -> List[Dict[str, Any]]:
        rows = []
        for item in self._fetch_store_categories():
            url = self._strip_url(item.get("permalink") or "")
            rows.append(
                {
                    "id": self._safe_int(item.get("id")),
                    "parent": self._safe_int(item.get("parent")) or 0,
                    "name": self._clean(item.get("name")),
                    "url": url,
                    "count": self._safe_int(item.get("count")) or 0,
                }
            )
        return self._build_category_hierarchy(rows)

    def _extract_categories_from_wp_api(self) -> List[Dict[str, Any]]:
        rows = []
        for item in self._fetch_wp_categories():
            url = self._strip_url(item.get("link") or "")
            rows.append(
                {
                    "id": self._safe_int(item.get("id")),
                    "parent": self._safe_int(item.get("parent")) or 0,
                    "name": self._clean(item.get("name")),
                    "url": url,
                    "count": self._safe_int(item.get("count")) or 0,
                }
            )
        return self._build_category_hierarchy(rows)

    def _build_category_hierarchy(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        by_id: Dict[int, Dict[str, Any]] = {}
        children: Dict[int, List[Dict[str, Any]]] = {}

        for row in rows:
            item_id = self._safe_int(row.get("id"))
            url = self._strip_url(row.get("url") or "")
            if item_id is None or not self._is_category_url(url):
                continue
            name = self._clean(row.get("name")) or self._name_from_url(url)
            if not name or self._skip_category_name(name):
                continue
            normalized = {
                "id": item_id,
                "parent": self._safe_int(row.get("parent")) or 0,
                "name": html_lib.unescape(name),
                "url": url,
                "count": self._safe_int(row.get("count")) or 0,
            }
            by_id[item_id] = normalized
            children.setdefault(normalized["parent"], []).append(normalized)

        self._category_by_url = {row["url"]: row for row in by_id.values()}
        for child_list in children.values():
            child_list.sort(key=lambda row: (-(row.get("count") or 0), (row.get("name") or "").lower()))

        categories: List[Dict[str, Any]] = []
        for root in children.get(0, []):
            if not self._has_products_or_children(root, children):
                continue
            top_cat = self._category_node(root, "top")
            top_cat["low_level_categories"] = []

            for low in children.get(root["id"], []):
                if not self._has_products_or_children(low, children):
                    continue
                low_cat = self._category_node(low, "low")
                low_cat["subcategories"] = []
                self._append_descendant_subcategories(low, children, low_cat["subcategories"])
                top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)
        return categories

    def _append_descendant_subcategories(
        self,
        parent: Dict[str, Any],
        children: Dict[int, List[Dict[str, Any]]],
        out: List[Dict[str, Any]],
    ) -> None:
        for child in children.get(parent["id"], []):
            if not self._has_products_or_children(child, children):
                continue
            out.append(self._category_node(child, "subcategory"))
            self._append_descendant_subcategories(child, children, out)

    @staticmethod
    def _skip_category_name(name: str) -> bool:
        slug = re.sub(r"[^a-z0-9]+", "-", html_lib.unescape(name).lower()).strip("-")
        return slug in {"non-classe", "uncategorized"}

    @staticmethod
    def _category_node(item: Dict[str, Any], level: str) -> Dict[str, Any]:
        return {
            "name": html_lib.unescape(item["name"]),
            "url": item["url"],
            "level": level,
            "category_id": str(item["id"]),
            "product_count_hint": item.get("count", 0),
        }

    def _has_products_or_children(self, item: Dict[str, Any], children: Dict[int, List[Dict[str, Any]]]) -> bool:
        if item.get("count", 0) > 0:
            return True
        return any(self._has_products_or_children(child, children) for child in children.get(item["id"], []))

    def _extract_categories_from_sitemap(self) -> List[Dict[str, Any]]:
        sitemap_url = self.selectors.get("frontpage", {}).get(
            "category_sitemap",
            f"{self.base_url.rstrip('/')}/product_cat-sitemap.xml",
        )
        try:
            response = httpx.get(
                sitemap_url,
                headers=self._sync_headers(),
                follow_redirects=True,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
        except Exception as exc:
            self.logger.warning(f"Failed Mabrouk category sitemap fallback: {exc}")
            return []

        categories: List[Dict[str, Any]] = []
        seen = set()
        for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", response.text, flags=re.I | re.S):
            url = self._strip_url(html_lib.unescape(loc.strip()))
            if not self._is_category_url(url) or url in seen:
                continue
            seen.add(url)
            categories.append(
                {
                    "name": self._name_from_url(url),
                    "url": url,
                    "level": "top",
                    "low_level_categories": [],
                }
            )
        return categories

    def _extract_categories_from_menu(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        fp = self.selectors.get("frontpage", {})
        categories: List[Dict[str, Any]] = []
        seen = set()
        for link in tree.css(fp.get("fallback_links", "header nav a[href], .site-header a[href], .menu a[href], footer a[href]")):
            url = self._strip_url(self._absolute_url(link.attributes.get("href")) or "")
            if not self._is_category_url(url) or url in seen:
                continue
            name = self._clean(link.attributes.get("title")) or self._clean(link.text(strip=True)) or self._name_from_url(url)
            if not name or name.lower() in {"none", "tout voir"} or self._skip_category_name(name):
                continue
            seen.add(url)
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

    def _category_id_for_url(self, category_url: str) -> Optional[str]:
        if not self._category_by_url:
            self._extract_categories_from_store_categories()
            if not self._category_by_url:
                self._extract_categories_from_wp_api()
        url = self._strip_url(category_url)
        category = self._category_by_url.get(url)
        return str(category["id"]) if category else None

    # ------------------------------------------------------------------
    # Listings and pagination
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products: List[Dict[str, Any]] = []

        for card in tree.css(cp.get("item_selector", "ul.products li.product, .products .product")):
            class_name = card.attributes.get("class", "")
            if "product-category" in class_name:
                continue
            product = self._product_from_listing_card(card, cp)
            if product:
                products.append(product)
        return dedupe_products(products, self.logger, "mabrouk listing")

    def _product_from_listing_card(self, card: Any, cp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = None
        for link in card.css(cp.get("item_url", "a[href*='/produit/']")):
            candidate = self._strip_url(self._absolute_url(link.attributes.get("href")) or "")
            if self._is_product_url(candidate):
                url = candidate
                break
        if not url:
            return None

        product_id = self._listing_product_id(card, cp)
        name = self._listing_name(card, product_id, url)
        if not name:
            return None
        sku = self._listing_sku(card, cp, product_id)

        price = self._extract_price_from_selectors(card, ["ins .woocommerce-Price-amount", ".price .woocommerce-Price-amount", ".price"])
        old_price = self._extract_price_from_selectors(card, ["del .woocommerce-Price-amount"])
        if old_price is not None and price is not None and old_price <= price:
            old_price = None

        availability, available = self._availability_from_listing(card)
        image = self._extract_listing_image(card, cp)

        product: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": url,
            "name": name,
            "title": name,
            "shop": self.site_name,
            "price": price,
            "old_price": old_price,
            "image": image,
            "availability": availability,
            "available": available,
        }
        if sku:
            product["reference"] = sku
            product["sku"] = sku
        discount = self._extract_discount(card, cp, price, old_price)
        if discount is not None:
            product["discount_percent"] = discount
        return finalize_product_record({k: v for k, v in product.items() if v is not None})

    def _listing_name(self, card: Any, product_id: Optional[str], url: str) -> Optional[str]:
        for selector in (
            "h2.woocommerce-loop-product__title",
            ".woocommerce-loop-product__title",
            "h3",
            ".product-title",
        ):
            name = self._text(card.css_first(selector))
            if name:
                return name
        img = card.css_first("img[alt], img[title]")
        name = self._clean(img.attributes.get("alt") if img else None) or self._clean(img.attributes.get("title") if img else None)
        if name:
            return name
        button = card.css_first("a.add_to_cart_button")
        label = self._clean(button.attributes.get("aria-label") if button else None)
        if label:
            match = re.search(r"[“\"](.+?)[”\"]", label)
            return self._clean(match.group(1) if match else label)
        return self._name_from_url(url)

    def _listing_product_id(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        product_id = self._post_id_from_class(card.attributes.get("class", ""))
        if product_id:
            return product_id
        node = card.css_first(cp.get("item_id", "[data-product_id]"))
        if not node:
            return None
        return self._clean(node.attributes.get("data-product_id") or node.attributes.get("data-product-id"))

    def _listing_sku(self, card: Any, cp: Dict[str, Any], product_id: Optional[str]) -> Optional[str]:
        node = card.css_first(cp.get("item_sku", "[data-product_sku]"))
        sku = self._clean(node.attributes.get("data-product_sku") if node else None)
        return sku if self._meaningful_sku(sku, product_id) else None

    def _extract_price_from_selectors(self, root: Any, selectors: List[str]) -> Optional[float]:
        for selector in selectors:
            node = root.css_first(selector)
            price = self._parse_price(node.text(separator=" ", strip=True) if node else None)
            if price is not None:
                return price
        return None

    def _extract_discount(
        self,
        root: Any,
        cp: Dict[str, Any],
        price: Optional[float],
        old_price: Optional[float],
    ) -> Optional[int]:
        node = root.css_first(cp.get("item_discount", ".onsale, .discount"))
        text = self._clean(node.text(strip=True) if node else None)
        if text:
            match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", text)
            if match:
                return round(float(match.group(1).replace(",", ".")))
        if price and old_price and old_price > price:
            return round((1 - price / old_price) * 100)
        return None

    def _availability_from_listing(self, card: Any) -> Tuple[Optional[str], Optional[bool]]:
        class_name = (card.attributes.get("class") or "").lower()
        if "outofstock" in class_name or "out-of-stock" in class_name:
            return "Rupture de stock", False
        stock_node = card.css_first(".out-of-stock, .stock, .availability")
        stock_text = self._text(stock_node)
        if stock_text:
            return availability_from_text(stock_text)
        if "instock" in class_name:
            return "En stock", True
        add_to_cart = card.css_first("a.add_to_cart_button[data-product_id], button.add_to_cart_button")
        if add_to_cart:
            return "En stock", True
        return None, None

    def _extract_listing_image(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        selector = cp.get("item_image", "img.wp-post-image, img.attachment-woocommerce_thumbnail, img")
        attrs = cp.get("item_image_attrs", ["data-large_image", "data-src", "data-lazy-src", "src", "srcset"])
        for img in card.css(selector):
            image = self._image_from_node(img, attrs)
            if image:
                return image
        return None

    def _image_from_node(self, node: Any, attrs: List[str], prefer_largest_srcset: bool = False) -> Optional[str]:
        if not node:
            return None
        for attr in attrs:
            value = node.attributes.get(attr)
            if attr == "srcset":
                value = self._first_srcset_url(value, prefer_largest=prefer_largest_srcset)
            image = self._absolute_url(value)
            if image and not image.startswith("data:"):
                return image
        return None

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        path = re.sub(r"/page/\d+/?$", "", parts.path or "/").rstrip("/")
        if page_num > 1:
            path = f"{path}/page/{page_num}"
        path = path or "/"
        if path != "/" and not path.endswith("/"):
            path += "/"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1
        total_results = None
        next_url = None

        body = tree.css_first("body")
        body_class = body.attributes.get("class", "") if body else ""
        body_page = re.search(r"(?:^|\s)paged-(\d+)(?:\s|$)", body_class)
        if body_page:
            current_page = self._safe_int(body_page.group(1)) or current_page

        current = tree.css_first(".page-numbers.current, span.current")
        if current:
            current_page = self._safe_int(current.text(strip=True)) or current_page

        for link in tree.css(cp.get("pagination_pages", ".page-numbers[href], a.page-numbers[href]")):
            href = self._absolute_url(link.attributes.get("href"))
            text_page = self._safe_int(link.text(strip=True))
            if text_page:
                total_pages = max(total_pages, text_page)
            if href:
                match = re.search(r"/page/(\d+)/?", urlsplit(href).path)
                if match:
                    total_pages = max(total_pages, int(match.group(1)))

        next_link = tree.css_first(cp.get("pagination_next", ".next.page-numbers[href], a.next.page-numbers[href]"))
        if next_link:
            next_url = self._absolute_url(next_link.attributes.get("href"))

        result_text = " ".join(self._text(node) or "" for node in tree.css(cp.get("result_count", ".woocommerce-result-count")))
        result_match = re.search(r"sur\s+(\d[\d\s.,]*)\s+r[ée]sultats?", result_text, re.I)
        if result_match:
            total_results = self._safe_int(re.sub(r"\D", "", result_match.group(1)))
            product_count = len(self.extract_products_from_html(html))
            if total_results and product_count:
                total_pages = max(total_pages, math.ceil(total_results / product_count))

        return {
            "current_page": current_page,
            "total_pages": max(1, total_pages),
            "has_next": bool(next_url) or current_page < total_pages,
            "next_url": next_url,
            "total_results": total_results,
        }

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        category_id = self._category_id_for_url(category_url)
        if not category_id:
            self.logger.debug(f"No Store API category id for {category_url}; falling back to HTML pagination")
            return await super().scrape_all_pages(category_url, limit=limit)

        cp = self.selectors.get("category_page", {})
        endpoint = cp.get("store_products_api", f"{self.base_url.rstrip('/')}/wp-json/wc/store/v1/products")
        per_page = 100
        max_pages = self.config.get("settings", {}).get("max_pagination_pages", 120)
        products: List[Dict[str, Any]] = []
        client = await self.get_client()

        for page in range(1, max_pages + 1):
            try:
                response = await client.get(
                    endpoint,
                    params={"category": category_id, "per_page": per_page, "page": page},
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                self.logger.warning(f"Mabrouk Store API listing failed page={page} category={category_id}: {exc}")
                if page == 1:
                    return await super().scrape_all_pages(category_url, limit=limit)
                break

            if not isinstance(payload, list):
                if page == 1:
                    return await super().scrape_all_pages(category_url, limit=limit)
                break
            if not payload:
                break

            for item in payload:
                if isinstance(item, dict):
                    product = self._product_from_store_api(item)
                    if product:
                        products.append(product)
            if limit and len(products) >= limit:
                break

            total_pages = self._safe_int(response.headers.get("x-wp-totalpages"))
            if total_pages and page >= total_pages:
                break
            if len(payload) < per_page and not total_pages:
                break

        products = dedupe_products(products, self.logger, "mabrouk store api")
        return products[:limit] if limit else products

    def _product_from_store_api(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = self._strip_url(item.get("permalink") or "")
        if not self._is_product_url(url):
            return None

        product_id = self._clean(item.get("id"))
        name = self._html_text(item.get("name")) or self._clean(item.get("name"))
        if not name:
            return None

        prices = item.get("prices") if isinstance(item.get("prices"), dict) else {}
        price = self._store_api_price(prices.get("price"), prices)
        regular_price = self._store_api_price(prices.get("regular_price"), prices)
        sale_price = self._store_api_price(prices.get("sale_price"), prices)
        if sale_price is not None and (price is None or sale_price < price):
            price = sale_price

        sku = self._clean(item.get("sku"))
        if not self._meaningful_sku(sku, product_id):
            sku = None

        product: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": url,
            "name": name,
            "title": name,
            "shop": self.site_name,
            "price": price,
        }
        if sku:
            product["reference"] = sku
            product["sku"] = sku
        if regular_price is not None and price is not None and regular_price > price:
            product["old_price"] = regular_price
            product["discount_percent"] = round((1 - price / regular_price) * 100)

        availability = self._store_api_availability(item)
        if availability[0]:
            product["availability"] = availability[0]
        if availability[1] is not None:
            product["available"] = availability[1]

        images = self._store_api_images(item)
        if images:
            product["image"] = images[0]
            product["images"] = images

        categories = []
        for category in item.get("categories") or []:
            if isinstance(category, dict):
                category_name = self._clean(category.get("name"))
                if category_name and category_name not in categories:
                    categories.append(category_name)
        if categories:
            product["listing_categories"] = categories

        options = self._store_api_options(item)
        if options:
            product["options"] = options

        brand = self._store_api_brand(item)
        if brand:
            product["brand"] = brand

        return finalize_product_record({k: v for k, v in product.items() if v is not None})

    def _store_api_price(self, value: Any, prices: Dict[str, Any]) -> Optional[float]:
        price = parse_price(value)
        if price is None:
            return None
        minor_unit = self._safe_int(prices.get("currency_minor_unit"))
        if minor_unit and minor_unit > 0:
            price = price / (10**minor_unit)
        return price

    def _store_api_availability(self, item: Dict[str, Any]) -> Tuple[Optional[str], Optional[bool]]:
        stock = item.get("stock_availability")
        if isinstance(stock, dict):
            text = self._clean(stock.get("text") or stock.get("class"))
            if text:
                lowered = text.lower()
                if "out-of-stock" in lowered or "outofstock" in lowered:
                    return "Rupture de stock", False
                if "in-stock" in lowered or "instock" in lowered:
                    return "En stock", True
                availability, available = availability_from_text(text)
                if availability:
                    return availability, available
        if item.get("is_in_stock") is True:
            return "En stock", True
        if item.get("is_in_stock") is False:
            return "Rupture de stock", False
        add_to_cart = item.get("add_to_cart")
        if isinstance(add_to_cart, dict):
            return availability_from_text(add_to_cart.get("text") or add_to_cart.get("description"))
        return None, None

    def _store_api_images(self, item: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        for image in item.get("images") or []:
            if not isinstance(image, dict):
                continue
            for key in ("src", "full_src", "thumbnail"):
                url = self._absolute_url(image.get(key))
                if url and url not in images:
                    images.append(url)
                    break
        return images

    def _store_api_options(self, item: Dict[str, Any]) -> Dict[str, List[str]]:
        options: Dict[str, List[str]] = {}
        for attribute in item.get("attributes") or []:
            if not isinstance(attribute, dict):
                continue
            name = self._clean(attribute.get("name"))
            values = []
            for term in attribute.get("terms") or []:
                value = self._clean(term.get("name") if isinstance(term, dict) else term)
                if value and value not in values:
                    values.append(value)
            if name and values:
                options[name] = values
        return options

    def _store_api_brand(self, item: Dict[str, Any]) -> Optional[str]:
        for key in ("brands", "brand"):
            value = item.get(key)
            if isinstance(value, list):
                for brand in value:
                    name = self._clean(brand.get("name") if isinstance(brand, dict) else brand)
                    if name:
                        return name
            elif isinstance(value, dict):
                name = self._clean(value.get("name"))
                if name:
                    return name
            else:
                name = self._clean(value)
                if name:
                    return name
        return None

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        product_url = self._strip_url(url)
        html = await self.fetch_html(product_url)
        if not html:
            return {"url": product_url, "error": "Failed to fetch product detail"}

        try:
            return self.extract_product_details_from_html(html, product_url)
        except Exception as exc:
            self.logger.debug(f"Mabrouk detail parse failed: {product_url} ({exc})")
            return {"url": product_url, "error": str(exc)}

    def extract_product_details_from_html(self, html: str, product_url: str) -> dict:
        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = html_product_metadata(html, product_url, self.base_url)
        data["url"] = self._strip_url(data.get("url") or product_url)

        product_json = self._jsonld_product(tree, data["url"])
        if product_json:
            self._apply_jsonld_product(data, product_json)

        variations = self._detail_variations(tree, pp)

        product_id = self._detail_product_id(tree, pp)
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        title = self._clean_title(self._text(tree.css_first(pp.get("title", "h1.product_title, h1.entry-title"))))
        if title:
            data["title"] = title
            data["name"] = title

        sku = self._clean(tree.css_first(pp.get("sku", ".sku_wrapper .sku, span.sku")).text(strip=True) if tree.css_first(pp.get("sku", ".sku_wrapper .sku, span.sku")) else None)
        if self._meaningful_sku(sku, product_id):
            data["reference"] = sku
            data["sku"] = sku

        price = self._detail_price(tree, pp, variations)
        if price is not None:
            data["price"] = price
        old_price = self._detail_old_price(tree, pp, variations)
        if old_price is not None and data.get("price") is not None and old_price > data["price"]:
            data["old_price"] = old_price
            data["discount_percent"] = round((1 - data["price"] / old_price) * 100)

        availability, available = self._detail_availability(tree, pp, data, variations)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        tabs = self._detail_tabs(tree)
        short_description = self._detail_short_description(tree)
        description = tabs.get("Description") or short_description or self._html_text(data.get("description"))
        if short_description:
            data["short_description"] = short_description
        if description and description.upper() != "VIDE":
            data["description"] = description
            data["overview"] = description
            data["full_description"] = description

        specs = self._extract_detail_specs(tree, pp, tabs, variations)
        if specs:
            data["specifications"] = specs
            brand = self._brand_from_specs(specs)
            if brand:
                data["brand"] = brand

        options = self._extract_variation_options(tree, pp, variations)
        if options:
            data["options"] = options

        variation_meta = self._variation_metadata(variations)
        for key, value in variation_meta.items():
            if key == "images":
                continue
            data[key] = value

        brand = self._detail_brand(tree, pp, data)
        if brand:
            data["brand"] = brand

        images = self._detail_images(tree, pp, data, variations)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = self._detail_categories(tree, pp, product_json)
        if categories:
            data["categories"] = categories

        data["shop"] = self.site_name
        return finalize_product_record({k: v for k, v in data.items() if v is not None})

    def _detail_product_id(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("product_id", "form.variations_form[data-product_id], input[name='product_id'][value]"))
        if node:
            for attr in ("data-product_id", "data-product-id", "value"):
                product_id = self._clean(node.attributes.get(attr))
                if product_id:
                    return product_id
        return self._body_post_id(tree)

    def _detail_price(self, tree: HTMLParser, pp: Dict[str, Any], variations: List[Dict[str, Any]]) -> Optional[float]:
        price = self._extract_price_from_selectors(
            tree,
            [
                ".summary .price ins .woocommerce-Price-amount",
                "p.price ins .woocommerce-Price-amount",
                ".summary .price > .woocommerce-Price-amount",
                "p.price > .woocommerce-Price-amount",
                ".summary .price",
                "p.price",
            ],
        )
        if price is not None:
            return price
        variation_prices = [self._parse_price(v.get("display_price")) for v in variations if isinstance(v, dict)]
        variation_prices = [p for p in variation_prices if p is not None]
        return min(variation_prices) if variation_prices else None

    def _detail_old_price(self, tree: HTMLParser, pp: Dict[str, Any], variations: List[Dict[str, Any]]) -> Optional[float]:
        old_price = self._extract_price_from_selectors(
            tree,
            [
                ".summary .price del .woocommerce-Price-amount",
                "p.price del .woocommerce-Price-amount",
            ],
        )
        if old_price is not None:
            return old_price
        variation_prices = [self._parse_price(v.get("display_regular_price")) for v in variations if isinstance(v, dict)]
        variation_prices = [p for p in variation_prices if p is not None]
        return max(variation_prices) if variation_prices else None

    def _detail_availability(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        data: Dict[str, Any],
        variations: List[Dict[str, Any]],
    ) -> Tuple[Optional[str], Optional[bool]]:
        node = tree.css_first(pp.get("availability", ".summary .stock, form.variations_form .stock, form.cart .stock"))
        if node:
            return availability_from_text(self._text(node))

        form = tree.css_first("form.variations_form, form.cart")
        form_text = self._text(form)
        availability, available = availability_from_text(form_text)
        if available is not None:
            return availability, available

        if variations:
            stock_values = [variation.get("is_in_stock") for variation in variations if isinstance(variation, dict)]
            if any(value is True for value in stock_values):
                return "En stock", True
            if stock_values and all(value is False for value in stock_values):
                return "Rupture de stock", False
            availability_text = " ".join(
                self._html_text(variation.get("availability_html")) or ""
                for variation in variations
                if isinstance(variation, dict)
            )
            availability, available = availability_from_text(availability_text)
            if available is not None:
                return availability, available

        if data.get("availability"):
            availability, available = availability_from_text(data.get("availability"))
            if available is not None:
                return availability, available

        add_to_cart = tree.css_first("button[name='add-to-cart'], .single_add_to_cart_button")
        if add_to_cart and "disabled" not in add_to_cart.attributes.get("class", "").lower():
            return "En stock", True

        body = tree.css_first("body")
        class_name = (body.attributes.get("class", "") if body else "").lower()
        if "outofstock" in class_name:
            return "Rupture de stock", False
        if "instock" in class_name:
            return "En stock", True
        return None, None

    def _detail_short_description(self, tree: HTMLParser) -> Optional[str]:
        node = tree.css_first(".woocommerce-product-details__short-description")
        text = self._text(node, separator="\n") if node else None
        return text if text and text.upper() != "VIDE" else None

    def _detail_tabs(self, tree: HTMLParser) -> Dict[str, str]:
        tabs: Dict[str, str] = {}
        for node in tree.css(".tabItems_item[data-stockie-tab-content]"):
            raw_key = self._clean(node.attributes.get("data-stockie-tab-content"))
            key = self.TAB_LABELS.get(raw_key or "", raw_key or "")
            text = self._text(node.css_first(".wrap") or node, separator=" ")
            if key and text and text.upper() != "VIDE":
                if key in tabs and tabs[key] != text:
                    tabs[key] = clean_text(f"{tabs[key]} | {text}") or tabs[key]
                else:
                    tabs[key] = text
        return tabs

    def _extract_detail_specs(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        tabs: Dict[str, str],
        variations: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for row in tree.css(pp.get("specs_rows", "table.variations tr, table.shop_attributes tr")):
            key_node = row.css_first("th, .label, label, .woocommerce-product-attributes-item__label")
            value_node = row.css_first("td, .value, .woocommerce-product-attributes-item__value")
            key = self._text(key_node)
            value = self._text(value_node)
            if key and value and value.upper() != "VIDE":
                value = re.sub(r"\bClear\b", "", value).strip()
                specs[key.rstrip(":")] = clean_text(value) or value

        for key, value in tabs.items():
            if key != "Description" and value and value.upper() != "VIDE":
                specs.setdefault(key, value)

        if variations:
            max_qty = [
                self._safe_int(variation.get("max_qty"))
                for variation in variations
                if isinstance(variation, dict) and self._safe_int(variation.get("max_qty")) is not None
            ]
            if max_qty:
                specs.setdefault("Stock max variante", str(max(max_qty)))
        return specs

    def _extract_variation_options(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        variations: List[Dict[str, Any]],
    ) -> Dict[str, List[str]]:
        options: Dict[str, List[str]] = {}

        form = tree.css_first(pp.get("variations_form", "form.variations_form[data-product_variations]"))
        variation_root = form or tree
        for item in variation_root.css(pp.get("variation_items", ".variable-items-wrapper li[data-attribute_name]")):
            key = self._clean(item.attributes.get("data-attribute_name"))
            value = self._clean(item.attributes.get("data-title") or item.attributes.get("title") or item.attributes.get("data-value"))
            if not key or not value:
                continue
            key = re.sub(r"^attribute_pa_", "", key).replace("_", " ").title()
            bucket = options.setdefault(key, [])
            if value not in bucket:
                bucket.append(value)

        for variation in variations:
            attrs = variation.get("attributes") if isinstance(variation, dict) else {}
            if not isinstance(attrs, dict):
                continue
            for key, value in attrs.items():
                key_text = re.sub(r"^attribute_pa_", "", str(key)).replace("_", " ").title()
                value_text = self._clean(value)
                if not key_text or not value_text:
                    continue
                bucket = options.setdefault(key_text, [])
                if value_text.upper() not in {v.upper() for v in bucket}:
                    bucket.append(value_text)
        return options

    def _detail_variations(self, tree: HTMLParser, pp: Dict[str, Any]) -> List[Dict[str, Any]]:
        form = tree.css_first(pp.get("variations_form", "form.variations_form[data-product_variations]"))
        raw = form.attributes.get("data-product_variations") if form else None
        if not raw:
            return []
        try:
            parsed = json.loads(html_lib.unescape(raw))
        except (TypeError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []

    def _variation_metadata(self, variations: List[Dict[str, Any]]) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        variation_ids: List[str] = []
        variation_skus: List[str] = []
        barcodes: List[str] = []
        for variation in variations:
            if not isinstance(variation, dict):
                continue
            variation_id = self._clean(variation.get("variation_id"))
            if variation_id and variation_id not in variation_ids:
                variation_ids.append(variation_id)
            sku = self._clean(variation.get("sku"))
            if sku and sku not in variation_skus:
                variation_skus.append(sku)
            gtin = normalize_gtin(sku)
            if gtin and gtin not in barcodes:
                barcodes.append(gtin)
        if variation_ids:
            data["variation_ids"] = variation_ids
        if variation_skus:
            data["variation_skus"] = variation_skus
        if barcodes:
            data["barcodes"] = barcodes
            if len(barcodes) == 1:
                data["barcode"] = barcodes[0]
        return data

    @staticmethod
    def _brand_from_specs(specs: Dict[str, str]) -> Optional[str]:
        for key, value in specs.items():
            normalized = re.sub(r"[^a-z]+", " ", key.lower()).strip()
            if normalized.startswith("marque") or normalized == "brand":
                return clean_text(value)
        return None

    def _detail_brand(self, tree: HTMLParser, pp: Dict[str, Any], data: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("brand", ".product-brands a, .brand a"))
        brand = self._clean(node.text(strip=True) if node else None)
        if brand:
            return brand
        return self._clean(data.get("brand"))

    def _detail_images(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        data: Dict[str, Any],
        variations: List[Dict[str, Any]],
    ) -> List[str]:
        images: List[str] = []
        selector = pp.get("image_gallery", ".woocommerce-product-gallery img, .woo-product-gallery img, .gimg, img.wp-post-image, meta[property='og:image']")
        for node in tree.css(selector):
            image = self._image_from_node(
                node,
                ["data-large_image", "data-src", "data-lazy-src", "content", "srcset", "src"],
                prefer_largest_srcset=True,
            )
            if image and image not in images:
                images.append(image)

        for variation in variations:
            image_payload = variation.get("image") if isinstance(variation, dict) else None
            if not isinstance(image_payload, dict):
                continue
            for key in ("full_src", "src", "url", "thumb_src"):
                image = self._absolute_url(image_payload.get(key))
                if image and image not in images:
                    images.append(image)
                    break

        for image in data.get("images") or []:
            image_url = image.get("url") if isinstance(image, dict) else image
            image_url = self._absolute_url(image_url)
            if image_url and image_url not in images:
                images.append(image_url)
        return images[:30]

    def _detail_categories(self, tree: HTMLParser, pp: Dict[str, Any], product_json: Dict[str, Any]) -> List[Dict[str, str]]:
        categories: List[Dict[str, str]] = []
        seen = set()
        for link in tree.css(pp.get("breadcrumbs", ".product_meta a[href*='/categorie-produit/'], .breadcrumbs a[href*='/categorie-produit/']")):
            href = self._strip_url(self._absolute_url(link.attributes.get("href")) or "")
            name = self._clean(link.text(strip=True))
            if not href or not name or not self._is_category_url(href) or href in seen:
                continue
            seen.add(href)
            categories.append({"name": name, "url": href})

        for crumb in self._jsonld_breadcrumbs(tree):
            href = self._strip_url(self._absolute_url(crumb.get("url")) or "")
            name = self._clean(crumb.get("name"))
            if not href or not name or not self._is_category_url(href) or href in seen:
                continue
            seen.add(href)
            categories.append({"name": name, "url": href})
        return categories

    def _jsonld_product(self, tree: HTMLParser, product_url: Optional[str]) -> Dict[str, Any]:
        target = normalize_url(product_url)
        products: List[Dict[str, Any]] = []
        for script in tree.css("script[type='application/ld+json']"):
            raw = script.text()
            if not raw:
                continue
            try:
                parsed = json.loads(html_lib.unescape(raw.strip()))
            except (TypeError, json.JSONDecodeError):
                continue
            for obj in self._walk_json(parsed):
                if isinstance(obj, dict) and "product" in self._type_names(obj):
                    products.append(obj)
        if not products:
            return {}
        if target:
            for product in products:
                for candidate in self._jsonld_urls(product):
                    if normalize_url(candidate) == target:
                        return product
        return products[0]

    @staticmethod
    def _jsonld_urls(product: Dict[str, Any]) -> List[str]:
        urls: List[str] = []
        for key in ("url", "@id"):
            if isinstance(product.get(key), str):
                urls.append(product[key])
        offers = product.get("offers")
        offer_items = offers if isinstance(offers, list) else [offers]
        for offer in offer_items:
            if isinstance(offer, dict) and isinstance(offer.get("url"), str):
                urls.append(offer["url"])
        return urls

    def _jsonld_breadcrumbs(self, tree: HTMLParser) -> List[Dict[str, str]]:
        crumbs: List[Dict[str, str]] = []
        for script in tree.css("script[type='application/ld+json']"):
            raw = script.text()
            if not raw:
                continue
            try:
                parsed = json.loads(html_lib.unescape(raw.strip()))
            except (TypeError, json.JSONDecodeError):
                continue
            for obj in self._walk_json(parsed):
                if not isinstance(obj, dict) or "breadcrumblist" not in self._type_names(obj):
                    continue
                for entry in obj.get("itemListElement") or []:
                    if not isinstance(entry, dict):
                        continue
                    item = entry.get("item")
                    if isinstance(item, dict):
                        name = self._clean(item.get("name") or entry.get("name"))
                        url = self._clean(item.get("@id") or item.get("url") or item.get("item"))
                    else:
                        name = self._clean(entry.get("name"))
                        url = self._clean(item)
                    if name and url:
                        crumbs.append({"name": name, "url": url})
        return crumbs

    def _apply_jsonld_product(self, data: Dict[str, Any], product: Dict[str, Any]) -> None:
        title = self._clean_title(product.get("name"))
        if title:
            data.setdefault("title", title)
            data.setdefault("name", title)
        description = self._html_text(product.get("description"))
        if description and description.upper() != "VIDE":
            data.setdefault("description", description)
        sku = self._clean(product.get("sku"))
        if sku:
            data.setdefault("reference", sku)
            data.setdefault("sku", sku)
        brand = self._jsonld_brand(product.get("brand"))
        if brand:
            data.setdefault("brand", brand)
        image = product.get("image")
        images = image if isinstance(image, list) else [image]
        cleaned_images = []
        for value in images:
            image_url = self._absolute_url(value.get("url") if isinstance(value, dict) else value)
            if image_url and image_url not in cleaned_images:
                cleaned_images.append(image_url)
        if cleaned_images:
            data.setdefault("images", cleaned_images)
            data.setdefault("image", cleaned_images[0])
        for key, value in self._jsonld_offer_metadata(product.get("offers")).items():
            data.setdefault(key, value)

    @staticmethod
    def _jsonld_brand(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            return clean_text(value.get("name") or value.get("@id"))
        return clean_text(value)

    def _jsonld_offer_metadata(self, offers: Any) -> Dict[str, Any]:
        offer_items = offers if isinstance(offers, list) else [offers]
        offer = offer_items[0] if offer_items and isinstance(offer_items[0], dict) else {}
        if not offer:
            return {}
        data: Dict[str, Any] = {}
        price = self._parse_price(offer.get("price"))
        price_specs = offer.get("priceSpecification")
        spec_items = price_specs if isinstance(price_specs, list) else [price_specs]
        for spec in spec_items:
            if price is None and isinstance(spec, dict):
                price = self._parse_price(spec.get("price"))
            if isinstance(spec, dict) and spec.get("priceCurrency"):
                data.setdefault("currency", self._clean(spec.get("priceCurrency")))
        if price is not None:
            data["price"] = price
        if offer.get("priceCurrency"):
            data.setdefault("currency", self._clean(offer.get("priceCurrency")))
        availability, available = availability_from_text(offer.get("availability"))
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available
        return data


def get_scraper(logger: logging.Logger) -> MabroukScraper:
    return MabroukScraper(logger)
