#!/usr/bin/env python3
"""
Shopingo scraper - WordPress/WooCommerce + Martfury, HTTP/selectolax.
"""

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from selectolax.parser import HTMLParser

from scraper.base import CategoryInfo, FastScraper
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


class ShopingoScraper(FastScraper):
    """HTTP scraper for shopingo.tn WooCommerce pages."""

    BAD_CATEGORY_PARTS = (
        "/cart",
        "/checkout",
        "/contact",
        "/mon-compte",
        "/my-account",
        "/account",
        "/login",
        "/search",
        "/wishlist",
        "/compare",
        "/blog",
        "/qui-sommes-nous",
        "/conditions-generales",
        "/wp-content/",
        "/wp-json/",
        "/feed",
        "/author/",
        "/tag/",
        "/product-tag/",
        "/marque/",
        "/brand/",
    )

    CATEGORY_PATH_PREFIX = "/categorie/"
    PRODUCT_PATH_PREFIX = "/produit/"
    ALL_PRODUCTS_PATH = "/boutique"

    def __init__(self, logger: logging.Logger):
        super().__init__("shopingo", logger)
        self._category_api_cache: Optional[List[Dict[str, Any]]] = None
        self._category_by_url: Dict[str, Dict[str, Any]] = {}
        self._store_ids_cache: Dict[int, set] = {}

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
        if path == self.ALL_PRODUCTS_PATH:
            return True
        if not path.startswith(self.CATEGORY_PATH_PREFIX):
            return False
        if path == self.CATEGORY_PATH_PREFIX.rstrip("/"):
            return False
        if "." in path.rsplit("/", 1)[-1]:
            return False
        if any(token in low for token in self.BAD_CATEGORY_PARTS):
            return False
        if any(token in low for token in ("?add-to-cart", "mailto:", "tel:", "javascript:", "#")):
            return False
        if "non-classe" in low or "uncategorized" in low:
            return False
        return True

    def _is_all_products_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        url = self._absolute_url(url)
        if not url or not self._is_site_url(url):
            return False
        return (urlsplit(url).path.rstrip("/") or "/") == self.ALL_PRODUCTS_PATH

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
        # The Arabic currency abbreviation contains a dot; remove it before
        # shared parsing so "29,900 د.ت" remains 29.9 instead of 29900.
        text = re.sub(r"د\s*\.?\s*ت\.?", "", text)
        text = re.sub(r"\b(?:TND|DT)\b", "", text, flags=re.I)
        return parse_price(text)

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        categories = self._extract_categories_from_api()
        if not categories:
            categories = self._extract_categories_from_store_categories()
        if not categories:
            tree = HTMLParser(html)
            categories = self._extract_categories_from_menu(tree)
        if not categories:
            categories = self._extract_categories_from_sitemap()
        all_products = self._all_products_category()
        if categories:
            categories = [all_products] + [
                category for category in categories if self._strip_url(category.get("url", "")) != all_products["url"]
            ]
        else:
            categories = [all_products]

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _all_products_category(self) -> Dict[str, Any]:
        fp = self.selectors.get("frontpage", {})
        url = self._strip_url(fp.get("all_products_url") or f"{self.base_url.rstrip('/')}/boutique/")
        return {
            "name": fp.get("all_products_name", "Tous les produits"),
            "url": url,
            "level": "top",
            "all_products": True,
            "product_count_hint": self._store_total_products(),
            "low_level_categories": [],
        }

    def _store_total_products(self) -> int:
        cp = self.selectors.get("category_page", {})
        endpoint = cp.get("store_products_api", f"{self.base_url.rstrip('/')}/wp-json/wc/store/v1/products")
        try:
            response = httpx.get(
                endpoint,
                params={"per_page": 1, "page": 1},
                headers=self._sync_headers(),
                follow_redirects=True,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            total = self._safe_int(response.headers.get("x-wp-total"))
            if total is not None:
                return total
            payload = response.json()
            return len(payload) if isinstance(payload, list) else 0
        except Exception as exc:
            self.logger.debug(f"Failed Shopingo Store API total products check: {exc}")
            return 0

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
            self.logger.warning(f"Failed Shopingo product_cat API category extraction: {exc}")
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
            child_list.sort(key=lambda row: (-(row.get("count") or 0), (row.get("name") or "").lower()))

        categories: List[Dict[str, Any]] = []
        for root in children.get(0, []):
            if not self._has_products_or_children(root, children):
                continue

            top_cat = self._category_node(root, "top")
            top_cat["low_level_categories"] = []

            direct_children = [
                child for child in children.get(root["id"], []) if self._has_products_or_children(child, children)
            ]
            if direct_children:
                if self._has_direct_products_outside_children(root, direct_children):
                    self_low = self._category_node(root, "low")
                    self_low["name"] = f"Tous {root['name']}"
                    self_low["subcategories"] = []
                    top_cat["low_level_categories"].append(self_low)

                for low in direct_children:
                    low_cat = self._category_node(low, "low")
                    low_cat["subcategories"] = []
                    low_children = [
                        child for child in children.get(low["id"], []) if self._has_products_or_children(child, children)
                    ]
                    if low_children and self._has_direct_products_outside_children(low, low_children):
                        self_sub = self._category_node(low, "subcategory")
                        self_sub["name"] = f"Tous {low['name']}"
                        low_cat["subcategories"].append(self_sub)
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

    def _has_direct_products_outside_children(
        self,
        item: Dict[str, Any],
        direct_children: List[Dict[str, Any]],
    ) -> bool:
        if item.get("count", 0) <= 0 or not direct_children:
            return False
        parent_ids = self._store_product_ids_for_category(item["id"])
        if not parent_ids:
            return False
        child_ids = set()
        for child in direct_children:
            child_ids.update(self._store_product_ids_for_category(child["id"]))
        return bool(parent_ids - child_ids)

    def _store_product_ids_for_category(self, category_id: int) -> set:
        if category_id in self._store_ids_cache:
            return self._store_ids_cache[category_id]

        cp = self.selectors.get("category_page", {})
        endpoint = cp.get("store_products_api", f"{self.base_url.rstrip('/')}/wp-json/wc/store/v1/products")
        ids = set()
        page = 1
        try:
            with httpx.Client(
                headers=self._sync_headers(),
                follow_redirects=True,
                timeout=self.request_timeout,
            ) as client:
                while True:
                    response = client.get(
                        endpoint,
                        params={"category": category_id, "per_page": 100, "page": page},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, list) or not payload:
                        break
                    for item in payload:
                        product_id = self._safe_int(item.get("id")) if isinstance(item, dict) else None
                        if product_id is not None:
                            ids.add(product_id)
                    total_pages = self._safe_int(response.headers.get("x-wp-totalpages"))
                    if total_pages and page >= total_pages:
                        break
                    if len(payload) < 100 and not total_pages:
                        break
                    page += 1
        except Exception as exc:
            self.logger.debug(f"Failed Shopingo product id comparison for category {category_id}: {exc}")
            ids = set()

        self._store_ids_cache[category_id] = ids
        return ids

    def _extract_categories_from_store_categories(self) -> List[Dict[str, Any]]:
        fp = self.selectors.get("frontpage", {})
        endpoint = fp.get("store_categories_api", f"{self.base_url.rstrip('/')}/wp-json/wc/store/v1/products/categories")
        try:
            response = httpx.get(
                endpoint,
                headers=self._sync_headers(),
                follow_redirects=True,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            self.logger.warning(f"Failed Shopingo Store API category fallback: {exc}")
            return []

        if not isinstance(payload, list):
            return []

        by_id: Dict[int, Dict[str, Any]] = {}
        children: Dict[int, List[Dict[str, Any]]] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            item_id = self._safe_int(item.get("id"))
            if item_id is None:
                continue
            url = self._strip_url(item.get("permalink") or "")
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
        categories: List[Dict[str, Any]] = []
        seen = set()

        for selector in (
            fp.get("top_level_items", ".site-header .main-menu li.menu-item"),
            fp.get("fallback_links", ".site-header .main-menu a[href], header a[href], nav a[href]"),
        ):
            for node in tree.css(selector):
                link = node if getattr(node, "tag", "") == "a" else self._direct_link(node)
                meta = self._category_from_link(link)
                if not meta or meta["url"] in seen:
                    continue
                seen.add(meta["url"])
                categories.append(
                    {
                        "name": meta["name"],
                        "url": meta["url"],
                        "level": "top",
                        "low_level_categories": [],
                    }
                )
        return categories

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
            self.logger.warning(f"Failed Shopingo category sitemap fallback: {exc}")
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

        name = (
            self._clean(name_override)
            or self._clean(link.attributes.get("title"))
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
        if self._is_all_products_url(category_url):
            return None
        if not self._category_by_url:
            self._extract_categories_from_api()
        url = self._strip_url(category_url)
        category = self._category_by_url.get(url)
        return str(category["id"]) if category else None

    def build_scrape_queue(self, categories_data: dict) -> List[CategoryInfo]:
        for top_idx, top in enumerate(categories_data.get("categories", [])):
            if top.get("all_products") and top.get("url"):
                return [
                    CategoryInfo(
                        url=top["url"],
                        name=top.get("name", "Tous les produits"),
                        location=(top_idx,),
                        level="top",
                        parent_names=[],
                    )
                ]
        return super().build_scrape_queue(categories_data)

    # ------------------------------------------------------------------
    # Listings and pagination
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products: List[Dict[str, Any]] = []

        for card in tree.css(cp.get("item_selector", "li.product.type-product, ul.products li.product")):
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
                cp.get("item_current_price", "ins .woocommerce-Price-amount"),
                cp.get("item_price", ".price .woocommerce-Price-amount"),
            )
            old_price_node = card.css_first(cp.get("item_old_price", "del .woocommerce-Price-amount"))
            old_price = self._parse_price(old_price_node.text(strip=True) if old_price_node else None)
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

            image = self._extract_listing_image(card, cp)
            if image:
                product["image"] = image

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "shopingo listing")

    def _listing_link_and_name(self, card: Any, cp: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        selectors = cp.get(
            "item_url",
            "h2.woocommerce-loop-product__title a[href], h2 a[href], .mf-product-thumbnail a[href]",
        )
        chosen = None
        for link in card.css(selectors):
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
        if not name or re.search(r"^-?\s*\d+(?:[,.]\d+)?\s*%$", name):
            title = card.css_first("h2.woocommerce-loop-product__title a[href], h2 a[href]")
            name = self._clean(title.text(strip=True) if title else None)
        if not name:
            add_to_cart = card.css_first("a.add_to_cart_button[data-title]")
            name = self._clean(add_to_cart.attributes.get("data-title") if add_to_cart else None)
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
        node = card.css_first(cp.get("item_id", "[data-product_id]"))
        if not node:
            return None
        return self._clean(node.attributes.get("data-product_id") or node.attributes.get("data-product-id"))

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
        return self._parse_price(node.text(strip=True) if node else None)

    def _extract_discount(
        self,
        root: Any,
        cp: Dict[str, Any],
        price: Optional[float],
        old_price: Optional[float],
    ) -> Optional[int]:
        node = root.css_first(cp.get("item_discount", ".onsale.ribbon, .onsale"))
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
            availability, available = availability_from_text(stock_text)
            return availability, available
        if "instock" in class_name:
            return "En stock", True
        add_to_cart = card.css_first("a.add_to_cart_button[data-product_id], button.add_to_cart_button")
        if add_to_cart:
            return "En stock", True
        return None, None

    def _extract_listing_image(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        selector = cp.get("item_image", ".mf-product-thumbnail img, img.wp-post-image, .products img")
        attrs = cp.get("item_image_attrs", ["data-large_image", "data-src", "data-lazy-src", "src", "srcset"])
        fallback = None
        for img in card.css(selector):
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

    @classmethod
    def _looks_like_logo_image(cls, url: str, img: Any) -> bool:
        low = url.lower()
        if "logo" in low:
            return True
        width = cls._safe_int(img.attributes.get("width")) or 0
        height = cls._safe_int(img.attributes.get("height")) or 0
        return bool(width and height and width > height * 1.8)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        path = re.sub(r"/page/\d+/?$", "", parts.path or "/").rstrip("/")
        if page_num > 1:
            path = f"{path}/page/{page_num}"
        path = path or "/"
        if path != "/" and not path.endswith("/"):
            path += "/"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1

        current = tree.css_first(".page-numbers.current, span.current")
        if current:
            current_page = self._safe_int(current.text(strip=True)) or current_page
            total_pages = max(total_pages, current_page)

        next_url = None
        for link in tree.css(cp.get("pagination_pages", "a.page-numbers[href]")):
            text_page = self._safe_int(link.text(strip=True))
            if text_page:
                total_pages = max(total_pages, text_page)
            href = self._absolute_url(link.attributes.get("href"))
            if not href:
                continue
            match = re.search(r"/page/(\d+)/?", urlsplit(href).path)
            if match:
                total_pages = max(total_pages, int(match.group(1)))

        next_link = tree.css_first(cp.get("pagination_next", "a.next.page-numbers[href]"))
        if next_link:
            next_url = self._absolute_url(next_link.attributes.get("href"))
        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": bool(next_url) or current_page < total_pages,
            "next_url": next_url,
        }

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        is_all_products = self._is_all_products_url(category_url)
        category_id = None if is_all_products else self._category_id_for_url(category_url)
        if not category_id and not is_all_products:
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
                params = {"per_page": per_page, "page": page}
                if category_id:
                    params["category"] = category_id
                response = await client.get(
                    endpoint,
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                self.logger.warning(f"Shopingo Store API listing failed page={page} category={category_id}: {exc}")
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

        products = dedupe_products(products, self.logger, "shopingo store api")
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

        categories = []
        for category in item.get("categories") or []:
            if isinstance(category, dict):
                category_name = self._clean(category.get("name"))
                if category_name and category_name not in categories:
                    categories.append(category_name)
        if categories:
            product["listing_categories"] = categories

        brand = self._store_api_brand(item)
        if brand:
            product["brand"] = brand

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
            if data.get("price") and old_price > data["price"]:
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

        options = self._extract_variation_options(tree, pp)
        if options:
            data["options"] = options
            data.setdefault("specifications", {}).update({f"Option {key}": ", ".join(values) for key, values in options.items()})

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
        node = tree.css_first(pp.get("product_id", "input[name='product_id'][value], form.variations_form[data-product_id]"))
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
        title = re.sub(r"\s*-\s*Shopingo\.tn\s*$", "", title, flags=re.I)
        title = re.sub(r"\s*\|\s*Shopingo\.tn\s*$", "", title, flags=re.I)
        return clean_text(title)

    def _detail_price(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[float]:
        return self._extract_price(
            tree,
            pp.get("current_price", "p.price ins .woocommerce-Price-amount"),
            pp.get("price", "p.price .woocommerce-Price-amount"),
        )

    def _detail_old_price(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[float]:
        node = tree.css_first(pp.get("old_price", "p.price del .woocommerce-Price-amount"))
        return self._parse_price(node.text(strip=True) if node else None)

    def _detail_availability(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        data: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[bool]]:
        node = tree.css_first(pp.get("availability", ".summary .stock, .entry-summary .stock, form.cart .stock"))
        if node:
            text = self._text(node)
            return availability_from_text(text)

        if data.get("availability"):
            return availability_from_text(data.get("availability"))

        form = tree.css_first("form.cart, form.variations_form")
        add_to_cart = tree.css_first("button[name='add-to-cart'], .single_add_to_cart_button")
        if add_to_cart or form:
            classes = add_to_cart.attributes.get("class", "").lower() if add_to_cart else ""
            if "disabled" not in classes:
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
        for row in tree.css(pp.get("specs_rows", "table.variations tr, table.shop_attributes tr")):
            key_node = row.css_first("th, .label, label, .woocommerce-product-attributes-item__label")
            value_node = row.css_first("td, .value, .woocommerce-product-attributes-item__value")
            key = self._text(key_node)
            value = self._text(value_node)
            if key and value:
                value = re.sub(r"\bClear\b", "", value).strip()
                specs[key.rstrip(":")] = clean_text(value) or value
        return specs

    def _extract_variation_options(self, tree: HTMLParser, pp: Dict[str, Any]) -> Dict[str, List[str]]:
        options: Dict[str, List[str]] = {}

        for select in tree.css(".variations select[name]"):
            key = self._clean(select.attributes.get("name")) or self._clean(select.attributes.get("id"))
            if key:
                key = re.sub(r"^attribute_pa_", "", key).replace("_", " ")
            values = []
            for option in select.css("option"):
                value = self._clean(option.text(strip=True))
                raw_value = self._clean(option.attributes.get("value"))
                if value and raw_value and value.lower() not in {"choisir une option", "choose an option"}:
                    values.append(value)
            if key and values:
                options[key] = sorted(set(values), key=values.index)

        form = tree.css_first(pp.get("variations_form", "form.variations_form[data-product_variations]"))
        raw = form.attributes.get("data-product_variations") if form else None
        if raw:
            try:
                variations = json.loads(html_lib.unescape(raw))
            except (TypeError, json.JSONDecodeError):
                variations = []
            if isinstance(variations, list):
                for variation in variations:
                    attrs = variation.get("attributes") if isinstance(variation, dict) else {}
                    if not isinstance(attrs, dict):
                        continue
                    for key, value in attrs.items():
                        clean_key = re.sub(r"^attribute_pa_", "", str(key)).replace("_", " ")
                        clean_value = self._clean(value)
                        if not clean_key or not clean_value:
                            continue
                        bucket = options.setdefault(clean_key, [])
                        if clean_value not in bucket:
                            bucket.append(clean_value)
        return options

    @staticmethod
    def _brand_from_specs(specs: Dict[str, str]) -> Optional[str]:
        for key, value in specs.items():
            normalized = re.sub(r"[^a-z]+", " ", key.lower()).strip()
            if normalized.startswith("marque") or normalized == "brand":
                return clean_text(value)
        return None

    def _detail_brand(self, tree: HTMLParser, pp: Dict[str, Any], data: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("brand", ".mf-brands a, .product-brands a, .brand a"))
        brand = self._clean(node.text(strip=True) if node else None)
        if brand:
            return brand
        return self._clean(data.get("brand"))

    def _detail_images(self, tree: HTMLParser, pp: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        selector = pp.get("image_gallery", "img.wp-post-image, .woocommerce-product-gallery img")
        for img in tree.css(selector):
            image = self._image_from_node(
                img,
                ["data-large_image", "data-src", "data-lazy-src", "srcset", "src"],
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

        title = self._clean_title(product_json.get("name") if product_json else None)
        skip_names = {"accueil", "home", "boutique", "shop"}
        if title:
            skip_names.add(title.lower())

        raw_category = product_json.get("category") if product_json else None
        if isinstance(raw_category, str):
            for part in re.split(r"\s*>\s*", raw_category):
                name = self._clean(part)
                if name and name.lower() not in skip_names and name not in categories:
                    categories.append(name)

        for name in self._jsonld_breadcrumb_names(tree):
            if name and name.lower() not in skip_names and name not in categories:
                categories.append(name)

        for link in tree.css(pp.get("breadcrumbs", ".product_meta a[href*='/categorie/'], .woocommerce-breadcrumb a")):
            name = self._clean(link.text(strip=True))
            href = self._absolute_url(link.attributes.get("href"))
            if not name or name.lower() in skip_names:
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

    def _jsonld_breadcrumb_names(self, tree: HTMLParser) -> List[str]:
        names: List[str] = []
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
                    name = self._clean(entry.get("name"))
                    if name and name not in names:
                        names.append(name)
        return names

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

        category = self._clean(product.get("category"))
        if category:
            data.setdefault("categories", [part for part in re.split(r"\s*>\s*", category) if part])

        offer_data = self._jsonld_offer_metadata(product.get("offers"))
        for key, value in offer_data.items():
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


def get_scraper(logger: logging.Logger) -> ShopingoScraper:
    return ShopingoScraper(logger)
