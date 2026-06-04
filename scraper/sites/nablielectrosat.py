#!/usr/bin/env python3
"""
Nabli Electro Sat scraper - WordPress/WooCommerce + Woodmart, HTTP/selectolax.
"""

import html as html_lib
import json
import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    normalize_url,
    parse_price,
)


class NabliElectroSatScraper(FastScraper):
    """HTTP scraper for nablielectrosat.com."""

    BLOCKED_CATEGORY_PARTS = (
        "/mon-compte",
        "/my-account",
        "/panier",
        "/cart",
        "/checkout",
        "/wishlist",
        "/compare",
        "/contact",
        "/blog",
        "/actualite",
        "/wp-content/",
        "/wp-json/",
        "/feed",
        "/author/",
        "/tag/",
        "/product-tag/",
        "/marque/",
        "/brand/",
    )

    CATEGORY_PATH_PREFIX = "/categorie-produit/"
    PRODUCT_PATH_PREFIX = "/produit/"

    def __init__(self, logger: logging.Logger):
        super().__init__("nablielectrosat", logger)
        self._category_api_cache: Optional[List[Dict[str, Any]]] = None
        self._category_by_url: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, href: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(href, base_url or self.base_url)

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
            return int(value)
        except (TypeError, ValueError):
            return None

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
    def _first_srcset_url(value: Optional[str], prefer_largest: bool = False) -> Optional[str]:
        if not value:
            return None
        entries = [chunk.strip() for chunk in value.split(",") if chunk.strip()]
        if not entries:
            return None
        chosen = entries[-1] if prefer_largest else entries[0]
        return chosen.split(" ", 1)[0] if chosen else None

    @staticmethod
    def _post_id_from_class(class_name: str) -> Optional[str]:
        match = re.search(r"(?:^|\s)(?:post|postid)-(\d+)(?:\s|$)", class_name or "")
        return match.group(1) if match else None

    @classmethod
    def _body_post_id(cls, tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        return cls._post_id_from_class(body.attributes.get("class", "") if body else "")

    @staticmethod
    def _direct_link(node: Any) -> Optional[Any]:
        for link in node.css("a[href]"):
            if link.parent == node:
                return link
        return node.css_first("a[href]")

    @staticmethod
    def _name_from_url(url: str) -> str:
        slug = urlsplit(url).path.strip("/").split("/")[-1]
        return re.sub(r"[-_]+", " ", slug).strip().title() or slug

    @staticmethod
    def _type_names(value: Dict[str, Any]) -> List[str]:
        raw = value.get("@type")
        if isinstance(raw, list):
            return [str(item).lower() for item in raw]
        return [str(raw).lower()] if raw is not None else []

    @classmethod
    def _walk_json(cls, value: Any) -> Iterable[Any]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from cls._walk_json(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._walk_json(child)

    def _sync_headers(self) -> Dict[str, str]:
        headers = dict(self.headers)
        headers["Accept-Encoding"] = "gzip, deflate"
        headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
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
        if not path.startswith(self.CATEGORY_PATH_PREFIX):
            return False
        if path == self.CATEGORY_PATH_PREFIX.rstrip("/"):
            return False
        if "." in path.rsplit("/", 1)[-1]:
            return False
        if any(token in low for token in self.BLOCKED_CATEGORY_PARTS):
            return False
        if any(token in low for token in ("?add-to-cart", "mailto:", "tel:", "javascript:", "#")):
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
        return path.startswith(self.PRODUCT_PATH_PREFIX) and path != self.PRODUCT_PATH_PREFIX.rstrip("/")

    def _html_text(self, value: Any) -> Optional[str]:
        raw = self._clean(value)
        if not raw:
            return None
        tree = HTMLParser(raw)
        try:
            return clean_text(tree.body.text(separator=" ", strip=True) if tree.body else tree.text(separator=" ", strip=True))
        except TypeError:
            return clean_text(tree.text(strip=True))

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        categories = self._extract_categories_from_api()
        if not categories:
            tree = HTMLParser(html)
            categories = self._extract_categories_from_menu(tree)
        if not categories:
            categories = self._extract_categories_from_sitemap()

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _fetch_product_categories(self) -> List[Dict[str, Any]]:
        if self._category_api_cache is not None:
            return self._category_api_cache

        fp = self.selectors.get("frontpage", {})
        endpoint = fp.get("product_cat_api", f"{self.base_url.rstrip('/')}/wp-json/wp/v2/product_cat")
        items: List[Dict[str, Any]] = []
        page = 1

        try:
            with httpx.Client(
                headers=self._sync_headers(),
                follow_redirects=True,
                timeout=self.request_timeout,
            ) as client:
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
            self.logger.warning(f"Failed Nabli product_cat API category extraction: {exc}")
            items = []

        self._category_api_cache = items
        return items

    def _extract_categories_from_api(self) -> List[Dict[str, Any]]:
        by_id: Dict[int, Dict[str, Any]] = {}
        children: Dict[int, List[Dict[str, Any]]] = {}

        for item in self._fetch_product_categories():
            item_id = self._safe_int(item.get("id"))
            if item_id is None:
                continue
            url = self._strip_url(item.get("link") or "")
            if not self._is_category_url(url):
                continue
            name = self._clean(item.get("name")) or self._name_from_url(url)
            if not name or self._skip_category_name(name):
                continue

            parent_id = self._safe_int(item.get("parent")) or 0
            normalized = {
                "id": item_id,
                "parent": parent_id,
                "name": name,
                "url": url,
                "count": self._safe_int(item.get("count")) or 0,
            }
            by_id[item_id] = normalized
            children.setdefault(parent_id, []).append(normalized)

        self._category_by_url = {row["url"]: row for row in by_id.values()}
        for child_list in children.values():
            child_list.sort(key=lambda row: (row.get("name") or "").lower())

        categories: List[Dict[str, Any]] = []
        for root in children.get(0, []):
            if not self._has_products_or_children(root, children):
                continue

            top_cat = self._category_node(root, "top")
            top_cat["low_level_categories"] = []

            direct_children = [
                child for child in children.get(root["id"], []) if self._has_products_or_children(child, children)
            ]
            for low in direct_children:
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
            "name": item["name"],
            "url": item["url"],
            "level": level,
            "category_id": str(item["id"]),
            "product_count_hint": item.get("count", 0),
        }

    def _has_products_or_children(self, item: Dict[str, Any], children: Dict[int, List[Dict[str, Any]]]) -> bool:
        if item.get("count", 0) > 0:
            return True
        return any(self._has_products_or_children(child, children) for child in children.get(item["id"], []))

    def _extract_categories_from_menu(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        fp = self.selectors.get("frontpage", {})
        out: List[Dict[str, Any]] = []
        seen = set()
        selectors = [
            fp.get("top_level_items", "ul#menu-main-navigation > li.menu-item"),
            fp.get("mobile_items", "ul#menu-main-navigation-1 li.menu-item, .wd-nav-mobile li.menu-item"),
        ]

        for selector in selectors:
            for node in tree.css(selector):
                link = self._direct_link(node)
                meta = self._category_from_link(link)
                if meta and meta["url"] not in seen:
                    seen.add(meta["url"])
                    out.append(
                        {
                            "name": meta["name"],
                            "url": meta["url"],
                            "level": "top",
                            "low_level_categories": [],
                        }
                    )

        if out:
            return out

        for link in tree.css(fp.get("fallback_links", "header a[href], nav a[href]")):
            meta = self._category_from_link(link)
            if meta and meta["url"] not in seen:
                seen.add(meta["url"])
                out.append(
                    {
                        "name": meta["name"],
                        "url": meta["url"],
                        "level": "top",
                        "low_level_categories": [],
                    }
                )
        return out

    def _extract_categories_from_sitemap(self) -> List[Dict[str, Any]]:
        sitemap_url = self.selectors.get("frontpage", {}).get(
            "category_sitemap", f"{self.base_url.rstrip('/')}/product_cat-sitemap.xml"
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
            self.logger.warning(f"Failed Nabli category sitemap fallback: {exc}")
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

    def _category_from_link(self, link: Any, name_override: Optional[str] = None) -> Optional[Dict[str, str]]:
        if not link:
            return None
        url = self._absolute_url(link.attributes.get("href"))
        if url:
            url = self._strip_url(url)
        if not self._is_category_url(url):
            return None

        text_node = link.css_first(".nav-link-text")
        name = (
            self._clean(name_override)
            or self._clean(link.attributes.get("title"))
            or self._clean(text_node.text(strip=True) if text_node else None)
            or self._clean(link.text(strip=True))
            or self._name_from_url(url)
        )
        if not name or len(name) > 120 or name.lower() in {"accueil", "home", "menu"}:
            return None
        if self._skip_category_name(name):
            return None
        return {"name": name, "url": url}

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
            self._extract_categories_from_api()
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

        for card in tree.css(cp.get("item_selector", "div.product-grid-item.product, div.product.type-product")):
            class_name = card.attributes.get("class", "")
            if "product-category" in class_name:
                continue

            url, name = self._listing_link_and_name(card, cp)
            if not url or not name:
                continue

            product_id = self._listing_product_id(card, cp)
            sku = self._listing_sku(card, cp, product_id)
            price = self._extract_price(
                card,
                cp.get("item_current_price", "ins .woocommerce-Price-amount bdi"),
                cp.get("item_price", ".price .woocommerce-Price-amount bdi"),
            )
            old_price_node = card.css_first(cp.get("item_old_price", "del .woocommerce-Price-amount bdi"))
            old_price = parse_price(old_price_node.text(strip=True) if old_price_node else None)
            availability, available = self._availability_from_listing(card)

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "url": url,
                "name": name,
                "price": price,
            }
            if sku:
                product["reference"] = sku
                product["sku"] = sku
            if old_price is not None:
                product["old_price"] = old_price
            discount = self._extract_discount(card, cp, price, old_price)
            if discount is not None:
                product["discount_percent"] = discount
            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available

            brand = self._extract_listing_brand(card, cp)
            if brand:
                product["brand"] = brand

            image = self._extract_listing_image(card, cp)
            if image:
                product["image"] = image

            listing_categories = self._listing_categories(card, cp)
            if listing_categories:
                product["listing_categories"] = listing_categories

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "nablielectrosat listing")

    def _listing_link_and_name(self, card: Any, cp: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        chosen = None
        for link in card.css(cp.get("item_url", ".wd-entities-title a[href], a.product-image-link[href]")):
            href = self._absolute_url(link.attributes.get("href"))
            if not self._is_product_url(href):
                continue
            chosen = link
            if self._clean(link.text(strip=True)):
                break
        if not chosen:
            return None, None

        url = self._strip_url(self._absolute_url(chosen.attributes.get("href")) or "")
        name = self._clean(chosen.text(strip=True))
        if not name:
            img = card.css_first("img[alt], img[title]")
            name = self._clean(img.attributes.get("alt") if img else None) or self._clean(
                img.attributes.get("title") if img else None
            )
        return url, name

    def _listing_product_id(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        product_id = self._post_id_from_class(card.attributes.get("class", ""))
        if product_id:
            return product_id
        node = card.css_first(cp.get("item_id", "[data-product_id], [data-id]"))
        if not node:
            return None
        return self._clean(
            node.attributes.get("data-product_id")
            or node.attributes.get("data-product-id")
            or node.attributes.get("data-id")
        )

    def _listing_sku(self, card: Any, cp: Dict[str, Any], product_id: Optional[str]) -> Optional[str]:
        node = card.css_first(cp.get("item_sku", "[data-product_sku]"))
        sku = self._clean(node.attributes.get("data-product_sku") if node else None)
        if not self._meaningful_sku(sku, product_id):
            return None
        return sku

    def _extract_price(self, root: Any, current_selector: str, fallback_selector: str) -> Optional[float]:
        node = root.css_first(current_selector)
        if node is None:
            node = root.css_first(fallback_selector)
        return parse_price(node.text(strip=True) if node else None)

    def _extract_discount(
        self,
        root: Any,
        cp: Dict[str, Any],
        price: Optional[float],
        old_price: Optional[float],
    ) -> Optional[int]:
        node = root.css_first(cp.get("item_discount", ".onsale.product-label"))
        text = self._clean(node.text(strip=True) if node else None)
        if text:
            match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", text)
            if match:
                return round(float(match.group(1).replace(",", ".")))
        if price and old_price and old_price != price:
            return round((1 - price / old_price) * 100)
        return None

    def _availability_from_listing(self, card: Any) -> Tuple[Optional[str], Optional[bool]]:
        class_name = (card.attributes.get("class") or "").lower()
        if "outofstock" in class_name or "out-of-stock" in class_name:
            return "Rupture de stock", False

        stock_node = card.css_first(".out-of-stock.product-label, .out-of-stock, .stock, .product-labels")
        stock_text = self._text(stock_node)
        if stock_text:
            availability, available = availability_from_text(stock_text)
            if available is not None:
                return availability, available
            if "sold out" in stock_text.lower():
                return stock_text, False

        if "instock" in class_name:
            return "En stock", True
        add_to_cart = card.css_first("a.add_to_cart_button[data-product_id], button.add_to_cart_button")
        if add_to_cart:
            return "En stock", True
        return None, None

    def _extract_listing_brand(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        node = card.css_first(cp.get("item_brand", ".product-labels [class*='label-attribute-pa_brand']"))
        if not node:
            return None
        brand = self._clean(node.attributes.get("title")) or self._text(node)
        if not brand:
            img = node.css_first("img[title], img[alt]")
            brand = self._clean(img.attributes.get("title") if img else None) or self._clean(
                img.attributes.get("alt") if img else None
            )
        if brand and brand.lower() not in {"sold out"} and not re.search(r"^\-?\d+%", brand):
            return brand
        return None

    def _listing_categories(self, card: Any, cp: Dict[str, Any]) -> List[str]:
        categories: List[str] = []
        for node in card.css(cp.get("item_categories", ".wd-product-cats a, .wd-product-cats")):
            text = self._text(node)
            if text and text not in categories:
                categories.append(text)
        return categories

    def _extract_listing_image(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        selector = cp.get("item_image", "a.product-image-link img, img.wp-post-image")
        attrs = cp.get("item_image_attrs", ["data-large_image", "data-src", "src", "srcset"])
        fallback = None
        for img in card.css(selector):
            if self._is_inside_classes(img, ("product-labels", "attribute-label", "label-attribute-pa_brand")):
                continue
            image = self._image_from_node(img, attrs)
            if not image:
                continue
            if self._looks_like_logo_image(image, img):
                fallback = fallback or image
                continue
            return image
        return fallback

    def _image_from_node(self, img: Any, attrs: List[str], prefer_largest_srcset: bool = False) -> Optional[str]:
        for attr in attrs:
            value = img.attributes.get(attr)
            if attr == "srcset":
                value = self._first_srcset_url(value, prefer_largest=prefer_largest_srcset)
            image = self._absolute_url(value)
            if image and not image.startswith("data:"):
                return image
        return None

    @staticmethod
    def _is_inside_classes(node: Any, tokens: Tuple[str, ...]) -> bool:
        parent = node.parent
        while parent is not None:
            class_name = parent.attributes.get("class", "") if hasattr(parent, "attributes") else ""
            if any(token in class_name for token in tokens):
                return True
            parent = parent.parent
        return False

    @classmethod
    def _looks_like_logo_image(cls, url: str, img: Any) -> bool:
        low = url.lower()
        if "logo" in low:
            return True
        width = cls._safe_int(img.attributes.get("width")) or 0
        height = cls._safe_int(img.attributes.get("height")) or 0
        if width and height and width > height * 1.8:
            return True
        return False

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        query_items = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "product-page"]
        if page_num > 1:
            query_items.append(("product-page", str(page_num)))
        query = urlencode(query_items)
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1

        current_node = tree.css_first(".page-numbers.current, span.current, input[name='paged'][value]")
        if current_node:
            current_page = self._safe_int(current_node.attributes.get("value") or current_node.text(strip=True)) or 1

        unique_products = {
            self._listing_product_id(card, cp) or normalize_url(self._listing_link_and_name(card, cp)[0])
            for card in tree.css(cp.get("item_selector", "div.product-grid-item.product, div.product.type-product"))
            if "product-category" not in card.attributes.get("class", "")
        }
        unique_products.discard(None)

        total_results = None
        result_node = tree.css_first(cp.get("result_count", ".woocommerce-result-count"))
        result_text = self._text(result_node)
        if result_text:
            numbers = [int(num.replace(" ", "")) for num in re.findall(r"\d[\d\s]*", result_text)]
            if numbers:
                total_results = max(numbers)

        total_pages = 1
        if total_results and unique_products:
            total_pages = max(1, math.ceil(total_results / len(unique_products)))

        for link in tree.css(cp.get("pagination_pages", "a.page-numbers[href]")):
            text_page = self._safe_int(link.text(strip=True))
            if text_page:
                total_pages = max(total_pages, text_page)
            href = self._absolute_url(link.attributes.get("href"))
            if href:
                query = dict(parse_qsl(urlsplit(href).query))
                page_num = self._safe_int(query.get("product-page") or query.get("paged") or query.get("page"))
                if page_num:
                    total_pages = max(total_pages, page_num)

        next_link = tree.css_first(cp.get("pagination_next", "a.next.page-numbers[href]"))
        next_url = self._absolute_url(next_link.attributes.get("href")) if next_link else None
        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": bool(next_url) or current_page < total_pages,
            "next_url": next_url,
        }

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        category_id = self._category_id_for_url(category_url)
        if not category_id:
            self.logger.debug(f"No Store API category id for {category_url}; falling back to HTML pagination")
            return await super().scrape_all_pages(category_url, limit=limit)

        cp = self.selectors.get("category_page", {})
        endpoint = cp.get("store_products_api", f"{self.base_url.rstrip('/')}/wp-json/wc/store/v1/products")
        per_page = 100
        max_pages = self.config.get("settings", {}).get("max_pagination_pages", 80)
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
                self.logger.warning(f"Nabli Store API listing failed page={page} category={category_id}: {exc}")
                if page == 1:
                    return await super().scrape_all_pages(category_url, limit=limit)
                break

            if not isinstance(payload, list) or not payload:
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

        products = dedupe_products(products, self.logger, "nablielectrosat store api")
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
            "price": price,
        }
        if sku:
            product["reference"] = sku
            product["sku"] = sku
        if regular_price is not None and price is not None and regular_price > price:
            product["old_price"] = regular_price
            product["discount_percent"] = round((1 - price / regular_price) * 100)

        if item.get("is_in_stock") is True:
            product["availability"] = "En stock"
            product["available"] = True
        elif item.get("is_in_stock") is False:
            product["availability"] = "Rupture de stock"
            product["available"] = False

        image = self._store_api_image(item)
        if image:
            product["image"] = image

        brand = self._store_api_brand(item)
        if brand:
            product["brand"] = brand

        categories = []
        for category in item.get("categories") or []:
            if isinstance(category, dict):
                category_name = self._clean(category.get("name"))
                if category_name and category_name not in categories:
                    categories.append(category_name)
        if categories:
            product["listing_categories"] = categories

        return finalize_product_record(product)

    def _store_api_price(self, value: Any, prices: Dict[str, Any]) -> Optional[float]:
        price = parse_price(value)
        if price is None:
            return None
        minor_unit = self._safe_int(prices.get("currency_minor_unit"))
        if minor_unit and minor_unit > 0:
            price = price / (10**minor_unit)
        return price

    def _store_api_image(self, item: Dict[str, Any]) -> Optional[str]:
        for image in item.get("images") or []:
            if not isinstance(image, dict):
                continue
            for key in ("src", "thumbnail", "full_src"):
                url = self._absolute_url(image.get(key))
                if url:
                    return url
        return None

    def _store_api_brand(self, item: Dict[str, Any]) -> Optional[str]:
        for key in ("brands", "brand"):
            value = item.get(key)
            if isinstance(value, list):
                for brand in value:
                    if isinstance(brand, dict):
                        name = self._clean(brand.get("name"))
                        if name:
                            return name
                    else:
                        name = self._clean(brand)
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
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = {"url": self._strip_url(url)}

        metadata = html_product_metadata(html, url, self.base_url)
        data.update(metadata)
        data["url"] = self._strip_url(data.get("url") or url)

        product_json = self._jsonld_product(tree, data["url"])
        if product_json:
            self._apply_jsonld_product(data, product_json)

        product_id = self._detail_product_id(tree, pp)
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        title_node = tree.css_first(pp.get("title", "h1.product_title, h1.entry-title"))
        title = self._clean_title(self._text(title_node))
        if title:
            data["title"] = title
            data.setdefault("name", title)

        sku_node = tree.css_first(pp.get("sku", ".sku_wrapper .sku, span.sku"))
        sku = self._clean(sku_node.text(strip=True) if sku_node else None)
        if self._meaningful_sku(sku, product_id):
            data["reference"] = sku
            data["sku"] = sku

        price = self._detail_price(tree, pp)
        if price is not None:
            data["price"] = price

        old_price = self._detail_old_price(tree, pp)
        if old_price is not None:
            data["old_price"] = old_price
            if data.get("price") and old_price != data["price"]:
                data["discount_percent"] = round((1 - data["price"] / old_price) * 100)

        availability, available = self._detail_availability(tree, pp, data)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        short_description = self._detail_short_description(tree)
        description = self._detail_description(tree, pp, data)
        if short_description:
            data["short_description"] = short_description
        if description:
            data["description"] = description

        specs = self._extract_detail_specs(tree, pp)
        if specs:
            data["specifications"] = specs
            brand = self._brand_from_specs(specs)
            if brand:
                data["brand"] = brand

        brand = self._detail_brand(tree, pp, data)
        if brand:
            data["brand"] = brand

        images = self._detail_images(tree, pp, data)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = self._detail_categories(tree, pp, product_json)
        if categories:
            data["categories"] = categories

        return finalize_product_record(data)

    def _detail_product_id(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("product_id", "button[name='add-to-cart'][value]"))
        if node:
            for attr in ("value", "data-product_id", "data-product-id"):
                product_id = self._clean(node.attributes.get(attr))
                if product_id:
                    return product_id
        return self._body_post_id(tree)

    @staticmethod
    def _meaningful_sku(sku: Optional[str], product_id: Optional[str]) -> bool:
        if not sku:
            return False
        if sku.upper() in {"N/A", "NA", "SKU", "ND"}:
            return False
        if product_id and sku == product_id:
            return False
        return True

    @staticmethod
    def _clean_title(value: Any) -> Optional[str]:
        title = clean_text(value)
        if not title:
            return None
        title = re.sub(r"\s*-\s*Nabli Electro Sat\s*$", "", title, flags=re.I)
        title = re.sub(r"\s*\|\s*Nabli Electro Sat\s*$", "", title, flags=re.I)
        return clean_text(title)

    def _detail_price(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[float]:
        return self._extract_price(
            tree,
            pp.get("current_price", "p.price ins .woocommerce-Price-amount bdi"),
            pp.get("price", "p.price .woocommerce-Price-amount bdi"),
        )

    def _detail_old_price(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[float]:
        node = tree.css_first(pp.get("old_price", "p.price del .woocommerce-Price-amount bdi"))
        return parse_price(node.text(strip=True) if node else None)

    def _detail_availability(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        data: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[bool]]:
        node = tree.css_first(
            pp.get(
                "availability",
                ".summary .stock, .entry-summary .stock, .product-image-summary .stock, form.cart .stock",
            )
        )
        if node:
            text = self._text(node)
            availability, available = availability_from_text(text)
            if available is None and text and "sold out" in text.lower():
                return text, False
            return availability, available

        if data.get("availability"):
            return availability_from_text(data.get("availability"))

        add_to_cart = tree.css_first("button[name='add-to-cart'], .single_add_to_cart_button")
        if add_to_cart:
            classes = add_to_cart.attributes.get("class", "").lower()
            if "disabled" not in classes and "disabled" not in add_to_cart.attributes:
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
        return self._text(node, separator="\n") if node else None

    def _detail_description(self, tree: HTMLParser, pp: Dict[str, Any], data: Dict[str, Any]) -> Optional[str]:
        fragments: List[str] = []
        for node in tree.css(pp.get("description", ".woocommerce-product-details__short-description, #tab-description")):
            text = self._text(node, separator="\n")
            if text and text not in fragments:
                fragments.append(text)
        if not fragments and data.get("description"):
            fragments.append(str(data["description"]))
        return clean_text("\n\n".join(fragments))

    def _extract_detail_specs(self, tree: HTMLParser, pp: Dict[str, Any]) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for row in tree.css(pp.get("specs_rows", "table.shop_attributes tr, table.woocommerce-product-attributes tr")):
            key_node = row.css_first("th, .woocommerce-product-attributes-item__label")
            value_node = row.css_first("td, .woocommerce-product-attributes-item__value")
            key = self._text(key_node)
            value = self._text(value_node)
            if key and value:
                specs[key] = value
        return specs

    @staticmethod
    def _brand_from_specs(specs: Dict[str, str]) -> Optional[str]:
        for key, value in specs.items():
            normalized = re.sub(r"[^a-z]+", " ", key.lower()).strip()
            if normalized.startswith("marque") or normalized == "brand":
                return clean_text(value)
        return None

    def _detail_brand(self, tree: HTMLParser, pp: Dict[str, Any], data: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("brand", ".wd-product-brands a, .product-brands a, .brand a"))
        brand = self._clean(node.text(strip=True) if node else None)
        if brand:
            return brand
        return self._clean(data.get("brand"))

    def _detail_images(self, tree: HTMLParser, pp: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        selector = pp.get(
            "image_gallery",
            "img.wp-post-image, .woocommerce-product-gallery__image img, .woocommerce-product-gallery img",
        )
        for img in tree.css(selector):
            image = self._image_from_node(
                img,
                ["data-large_image", "data-src", "srcset", "src"],
                prefer_largest_srcset=True,
            )
            if not image or image in images:
                continue
            if self._looks_like_logo_image(image, img) and "wp-post-image" not in img.attributes.get("class", ""):
                continue
            images.append(image)

        for image in data.get("images") or []:
            image_url = image.get("url") if isinstance(image, dict) else image
            image_url = self._absolute_url(image_url)
            if image_url and image_url not in images:
                images.append(image_url)
        return images[:20]

    def _detail_categories(self, tree: HTMLParser, pp: Dict[str, Any], product_json: Dict[str, Any]) -> List[str]:
        categories: List[str] = []

        raw_category = product_json.get("category") if product_json else None
        if isinstance(raw_category, str):
            for part in re.split(r"\s*>\s*", raw_category):
                name = self._clean(part)
                if name and name not in categories:
                    categories.append(name)

        for link in tree.css(pp.get("breadcrumbs", ".woocommerce-breadcrumb a, .breadcrumb a")):
            name = self._clean(link.text(strip=True))
            href = self._absolute_url(link.attributes.get("href"))
            if not name or name.lower() in {"accueil", "home"}:
                continue
            if href and self._is_category_url(href) and name not in categories:
                categories.append(name)
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
        main_page = product.get("mainEntityOfPage")
        if isinstance(main_page, dict):
            for key in ("url", "@id"):
                if isinstance(main_page.get(key), str):
                    urls.append(main_page[key])
        offers = product.get("offers")
        offer_items = offers if isinstance(offers, list) else [offers]
        for offer in offer_items:
            if isinstance(offer, dict) and isinstance(offer.get("url"), str):
                urls.append(offer["url"])
        return urls

    def _apply_jsonld_product(self, data: Dict[str, Any], product: Dict[str, Any]) -> None:
        title = self._clean_title(product.get("name"))
        if title:
            data.setdefault("title", title)
            data.setdefault("name", title)

        description = self._clean(product.get("description"))
        if description:
            data.setdefault("description", description)

        sku = self._clean(product.get("sku"))
        if sku:
            data.setdefault("reference", sku)
            data.setdefault("sku", sku)

        category = self._clean(product.get("category"))
        if category:
            data.setdefault("categories", [part for part in re.split(r"\s*>\s*", category) if part])

        offers = product.get("offers")
        offer_items = offers if isinstance(offers, list) else [offers]
        offer = offer_items[0] if offer_items and isinstance(offer_items[0], dict) else {}
        if offer:
            price = parse_price(offer.get("price"))
            if price is not None:
                data.setdefault("price", price)
            availability, available = availability_from_text(offer.get("availability"))
            if availability:
                data.setdefault("availability", availability)
            if available is not None:
                data.setdefault("available", available)


def get_scraper(logger: logging.Logger) -> NabliElectroSatScraper:
    return NabliElectroSatScraper(logger)
