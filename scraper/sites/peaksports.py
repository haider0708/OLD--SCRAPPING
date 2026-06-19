#!/usr/bin/env python3
"""
PeakSports scraper - Shopify storefront, HTTP/selectolax + Shopify JSON.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, get_date_folder, save_json, save_text_atomic
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


class PeakSportsScraper(FastScraper):
    """HTTP scraper for peaksports.tn Shopify pages and JSON endpoints."""

    SKIP_COLLECTION_HANDLES = {"", "all", "all-product", "frontpage"}
    BAD_LINK_PARTS = (
        "/account",
        "/cart",
        "/checkout",
        "/contact",
        "/pages/",
        "/policies/",
        "/search",
        "/blogs/",
        "/products/",
        "facebook.",
        "instagram.",
        "tiktok.",
        "youtube.",
        "linkedin.",
        "mailto:",
        "tel:",
        "javascript:",
    )
    BAD_COLLECTION_TEXT = (
        "home page",
        "all product",
        "produit a traiter",
        "prouduit a traiter",
        "a traiter",
        "test",
    )
    MENU_TOP_ORDER = ["Homme", "Femme", "Enfant", "Accessoires", "Bomi", "Valise", "Collections"]

    def __init__(self, logger: logging.Logger):
        super().__init__("peaksports", logger)
        settings = self.config.get("settings", {})
        endpoints = self.config.get("endpoints", {})
        self.page_size = int(settings.get("page_size", 250))
        self.max_pages = int(settings.get("max_pages", 50))
        self.collections_api = endpoints.get(
            "collections_api",
            f"{self.base_url.rstrip('/')}/collections.json?limit=250",
        )
        self.collection_products_template = endpoints.get(
            "collection_products_template",
            f"{self.base_url.rstrip('/')}/collections/{{handle}}/products.json?limit={{limit}}&page={{page}}",
        )
        self.product_json_template = endpoints.get(
            "product_json_template",
            f"{self.base_url.rstrip('/')}/products/{{handle}}.json",
        )
        self._request_sem = asyncio.Semaphore(6)
        self.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _abs(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

    @staticmethod
    def _attr(node: Any, name: str) -> Optional[str]:
        if not node:
            return None
        return clean_text(node.attributes.get(name))

    @staticmethod
    def _text(node: Any) -> Optional[str]:
        if not node:
            return None
        return clean_text(node.text(separator=" ", strip=True))

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
    def _clean_description(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if "<" in text and ">" in text:
            tree = HTMLParser(text)
            try:
                text = clean_text(tree.text(separator=" ", strip=True))
            except TypeError:
                text = clean_text(tree.text(strip=True))
        return text

    @staticmethod
    def _first_srcset_url(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        entries = [chunk.strip() for chunk in value.split(",") if chunk.strip()]
        if not entries:
            return None
        return entries[-1].split(" ", 1)[0]

    def _image_url(self, value: Any) -> Optional[str]:
        url = self._abs(value)
        if not url:
            return None
        return url.replace("{width}", "900")

    def _same_host_url(self, value: Any) -> Optional[str]:
        url = self._abs(value)
        if not url:
            return None
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        return url if host == base_host else None

    def _collection_handle_from_url(self, url: str) -> Optional[str]:
        clean_url = self._same_host_url(url)
        if not clean_url:
            return None
        parsed = urlsplit(clean_url)
        path = parsed.path.rstrip("/")
        low = clean_url.lower()
        if any(part in low for part in self.BAD_LINK_PARTS):
            return None
        match = re.search(r"/collections/([^/?#]+)(?:/products\.json)?/?$", path, re.I)
        if not match:
            return None
        handle = clean_text(match.group(1))
        if not handle or handle in self.SKIP_COLLECTION_HANDLES:
            return None
        return handle

    def _product_handle_from_url(self, url: str) -> Optional[str]:
        clean_url = self._same_host_url(url) or url
        parsed = urlsplit(clean_url)
        match = re.search(r"(?:/collections/[^/]+)?/products/([^/?#]+?)(?:\.json)?/?$", parsed.path, re.I)
        if not match:
            return None
        handle = clean_text(match.group(1))
        if not handle:
            return None
        return re.sub(r"\.json$", "", handle, flags=re.I)

    def _collection_url(self, handle: str) -> str:
        return f"{self.base_url.rstrip('/')}/collections/{handle}"

    def _product_url(self, handle: str) -> str:
        return f"{self.base_url.rstrip('/')}/products/{handle}"

    def _collection_products_url(self, handle: str, page: int = 1) -> str:
        return self.collection_products_template.format(
            handle=handle,
            limit=self.page_size,
            page=max(1, int(page or 1)),
        )

    def _product_json_url(self, handle: str) -> str:
        return self.product_json_template.format(handle=handle)

    def _page_from_url(self, url: str) -> int:
        query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        try:
            return max(1, int(query.get("page") or 1))
        except (TypeError, ValueError):
            return 1

    def _is_valid_collection(self, item: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(item, dict):
            return False
        handle = clean_text(item.get("handle"))
        if not handle or handle in self.SKIP_COLLECTION_HANDLES:
            return False
        title = (clean_text(item.get("title")) or "").lower()
        if any(token in title for token in self.BAD_COLLECTION_TEXT):
            return False
        try:
            return int(item.get("products_count") or 0) > 0
        except (TypeError, ValueError):
            return False

    async def _fetch_json(self, url: str) -> Dict[str, Any]:
        async with self._request_sem:
            client = await self.get_client()
            response = await client.get(
                url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": self.base_url,
                },
            )
            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        output_path = self.html_dir / "frontpage.html"
        html = await super().fetch_html(self.base_url, raise_on_error=True)
        save_text_atomic(html, output_path, self.logger)
        try:
            collections = await self._fetch_json(self.collections_api)
            save_text_atomic(
                json.dumps(collections, ensure_ascii=False, indent=2),
                self.html_dir / "collections_api.json",
                self.logger,
            )
        except Exception as exc:
            self.logger.debug(f"PeakSports collections API evidence save failed: {exc}")
        return output_path

    async def scrape_categories_async(self) -> dict:
        html_path = self.html_dir / "frontpage.html"
        if not html_path.exists():
            await self.download_frontpage()
        html = html_path.read_text(encoding="utf-8")

        collections = []
        try:
            data = await self._fetch_json(self.collections_api)
            collections = data.get("collections") or []
            save_text_atomic(
                json.dumps(data, ensure_ascii=False, indent=2),
                self.html_dir / "collections_api.json",
                self.logger,
            )
        except Exception as exc:
            self.logger.warning(f"PeakSports collections API failed, using menu only: {exc}")

        data = self._build_categories(html, collections)
        data["site"] = self.site_name
        data["shop"] = self.site_name
        data["base_url"] = self.base_url
        data["extracted_at"] = datetime.now().isoformat()
        data["date"] = get_date_folder()
        save_json(data, self.data_dir / "categories.json", self.logger)
        return data

    def extract_categories_from_html(self, html: str) -> dict:
        try:
            data = json.loads(html)
            if isinstance(data, dict) and isinstance(data.get("collections"), list):
                return self._build_categories("", data["collections"])
        except Exception:
            pass
        return self._build_categories(html, [])

    def _collection_info_map(self, collections: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out = {}
        for item in collections:
            if self._is_valid_collection(item):
                out[clean_text(item.get("handle"))] = item
        return out

    def _collection_node(self, handle: str, name: Optional[str], level: str, info: Dict[str, Any]) -> Dict[str, Any]:
        title = clean_text(name) or clean_text(info.get("title")) or handle.replace("-", " ").title()
        node = {
            "name": title,
            "url": self._collection_url(handle),
            "level": level,
            "collection_id": clean_text(info.get("id")),
            "handle": handle,
            "product_count": info.get("products_count"),
        }
        if level == "low":
            node["subcategories"] = []
        return {k: v for k, v in node.items() if v not in (None, "", [], {})}

    def _handle_from_node(self, node: Any) -> Optional[str]:
        return self._collection_handle_from_url(self._attr(node, "href") or "")

    def _build_categories(self, html: str, collections: List[Dict[str, Any]]) -> dict:
        valid = self._collection_info_map(collections)
        categories = self._build_menu_categories(html, valid) if html else []
        used_handles = self._category_handles(categories)

        fallback_lows = []
        for handle, info in valid.items():
            if handle in used_handles:
                continue
            fallback_lows.append(self._collection_node(handle, None, "low", info))

        if fallback_lows:
            categories.append(
                {
                    "name": "Collections",
                    "url": None,
                    "level": "top",
                    "low_level_categories": fallback_lows,
                }
            )

        if not categories and valid:
            categories = [
                {
                    "name": "Collections",
                    "url": None,
                    "level": "top",
                    "low_level_categories": [
                        self._collection_node(handle, None, "low", info)
                        for handle, info in valid.items()
                    ],
                }
            ]

        categories = self._sort_categories(categories)
        return {"categories": categories, "stats": self._category_stats(categories)}

    def _build_menu_categories(self, html: str, valid: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        top_selector = fp.get("top_menu_items", "header ul.main-nav > li > details")
        categories = []

        for details in tree.css(top_selector):
            top_summary = details.css_first("summary")
            top_link = top_summary.css_first("a[href*='/collections/']") if top_summary else details.css_first("a[href*='/collections/']")
            top_handle = self._handle_from_node(top_link)
            top_name = self._text(top_link) or (valid.get(top_handle or "") or {}).get("title")
            if not top_name:
                continue

            top_node = {
                "name": top_name,
                "url": self._collection_url(top_handle) if top_handle in valid else None,
                "level": "top",
                "handle": top_handle,
                "low_level_categories": [],
            }
            seen_top_handles = set()

            for group in details.css(fp.get("child_groups", "nav-menu.js-mega-nav")):
                low_summary = group.css_first("summary")
                low_link = low_summary.css_first("a[href*='/collections/']") if low_summary else group.css_first("a[href*='/collections/']")
                low_handle = self._handle_from_node(low_link)
                if low_handle not in valid:
                    continue

                low_node = self._collection_node(low_handle, self._text(low_link), "low", valid[low_handle])
                seen_top_handles.add(low_handle)
                sub_seen = set()
                for sub_link in group.css(fp.get("grandchild_links", "ul.main-nav__grandchild a[href*='/collections/']")):
                    sub_handle = self._handle_from_node(sub_link)
                    if sub_handle not in valid or sub_handle in sub_seen:
                        continue
                    low_node.setdefault("subcategories", []).append(
                        self._collection_node(sub_handle, self._text(sub_link), "subcategory", valid[sub_handle])
                    )
                    seen_top_handles.add(sub_handle)
                    sub_seen.add(sub_handle)
                top_node["low_level_categories"].append(low_node)

            for child_link in details.css(fp.get("child_dropdown_links", "ul.child-nav a[href*='/collections/']")):
                child_handle = self._handle_from_node(child_link)
                if child_handle not in valid or child_handle in seen_top_handles:
                    continue
                child_name = self._text(child_link)
                if child_handle == top_handle and child_name == top_name:
                    child_name = "Tous"
                top_node["low_level_categories"].append(
                    self._collection_node(child_handle, child_name, "low", valid[child_handle])
                )
                seen_top_handles.add(child_handle)

            if top_handle in valid and top_handle not in seen_top_handles:
                top_node["low_level_categories"].insert(
                    0,
                    self._collection_node(top_handle, "Tous", "low", valid[top_handle]),
                )
                seen_top_handles.add(top_handle)

            if top_node["url"] or top_node["low_level_categories"]:
                categories.append({k: v for k, v in top_node.items() if v not in (None, "", [], {})})

        if categories:
            return categories

        return self._build_categories_from_links(tree, valid)

    def _build_categories_from_links(self, tree: HTMLParser, valid: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        fp = self.selectors.get("frontpage", {})
        selector = fp.get("menu_links", "a[href*='/collections/']")
        seen = set()
        lows = []
        for node in tree.css(selector):
            handle = self._handle_from_node(node)
            if handle not in valid or handle in seen:
                continue
            lows.append(self._collection_node(handle, self._text(node), "low", valid[handle]))
            seen.add(handle)
        return [
            {
                "name": "Collections",
                "url": None,
                "level": "top",
                "low_level_categories": lows,
            }
        ] if lows else []

    @staticmethod
    def _category_handles(categories: List[Dict[str, Any]]) -> set:
        handles = set()
        for top in categories:
            if top.get("handle"):
                handles.add(top["handle"])
            for low in top.get("low_level_categories") or []:
                if low.get("handle"):
                    handles.add(low["handle"])
                for sub in low.get("subcategories") or []:
                    if sub.get("handle"):
                        handles.add(sub["handle"])
        return handles

    def _sort_categories(self, categories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        order = {name: idx for idx, name in enumerate(self.MENU_TOP_ORDER)}
        return sorted(categories, key=lambda item: order.get(item.get("name"), len(order)))

    @staticmethod
    def _category_stats(categories: List[Dict[str, Any]]) -> Dict[str, int]:
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["top_level"] += 1
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top.get("low_level_categories") or []:
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
                for sub in low.get("subcategories") or []:
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1
        return stats

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        url = self._abs(base_url) or base_url
        parts = urlsplit(url)
        pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "page"]
        if page_num > 1:
            pairs.append(("page", str(page_num)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", urlencode(pairs), ""))

    async def _fetch_listing_payload(self, url: str) -> Dict[str, Any]:
        handle = self._collection_handle_from_url(url)
        page = self._page_from_url(url)
        if not handle:
            return {
                "__peaksports_listing__": True,
                "url": url,
                "page": page,
                "limit": self.page_size,
                "products": [],
                "error": "not_collection_url",
            }
        try:
            data = await self._fetch_json(self._collection_products_url(handle, page))
            products = data.get("products") if isinstance(data, dict) else []
            return {
                "__peaksports_listing__": True,
                "url": url,
                "handle": handle,
                "page": page,
                "limit": self.page_size,
                "products": products if isinstance(products, list) else [],
                "error": None,
            }
        except Exception as exc:
            return {
                "__peaksports_listing__": True,
                "url": url,
                "handle": handle,
                "page": page,
                "limit": self.page_size,
                "products": [],
                "error": str(exc) or exc.__class__.__name__,
            }

    def _listing_payload_to_html(self, payload: Dict[str, Any]) -> str:
        cards = []
        for product in payload.get("products") or []:
            if not isinstance(product, dict):
                continue
            record = self._product_from_shopify(product)
            if not record.get("url"):
                continue
            image = html_lib.escape(record.get("image") or "")
            name = html_lib.escape(record.get("name") or "")
            url = html_lib.escape(record.get("url") or "")
            cards.append(
                "\n".join(
                    [
                        f'<product-card class="card card--product" data-product-id="{html_lib.escape(str(record.get("id") or ""))}" data-product-handle="{html_lib.escape(str(product.get("handle") or ""))}">',
                        f'  <a class="card-link js-prod-link" href="{url}">{name}</a>',
                        f'  <img class="card__main-image" src="{image}" alt="{name}">',
                        f'  <p class="card__title">{name}</p>',
                        f'  <span class="price__current"><span class="js-value">{record.get("price") if record.get("price") is not None else ""}</span></span>',
                        f'  <span class="price__was"><span class="js-value">{record.get("old_price") if record.get("old_price") is not None else ""}</span></span>',
                        "</product-card>",
                    ]
                )
            )

        payload_json = html_lib.escape(json.dumps(payload, ensure_ascii=False), quote=False)
        return (
            "<!doctype html><html><body>"
            f'<script id="peaksports-listing-data" type="application/json">{payload_json}</script>'
            '<section id="peaksports-products">'
            + "\n".join(cards)
            + "</section></body></html>"
        )

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> Dict[str, Any]:
        if self._collection_handle_from_url(url):
            started = time.monotonic()
            payload = await self._fetch_listing_payload(url)
            html = self._listing_payload_to_html(payload)
            if payload.get("products") and not (self.html_dir / "listing_sample_1.html").exists():
                save_text_atomic(html, self.html_dir / "listing_sample_1.html", self.logger)
            return {
                "html": html,
                "status_code": 200 if not payload.get("error") else None,
                "final_url": url,
                "content_type": "text/html; charset=utf-8",
                "content_encoding": None,
                "attempts": 1,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "blocked_signals": [],
                "error": payload.get("error"),
            }
        return await super().fetch_html_with_meta(url, raise_on_error=raise_on_error)

    def _listing_payload_from_html(self, html: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(html)
            if isinstance(data, dict) and isinstance(data.get("products"), list):
                data.setdefault("__peaksports_listing__", True)
                return data
        except Exception:
            pass

        tree = HTMLParser(html)
        node = tree.css_first("#peaksports-listing-data")
        if node:
            try:
                data = json.loads(html_lib.unescape(node.text()))
                if isinstance(data, dict):
                    return data
            except Exception:
                return None
        return None

    def extract_products_from_html(self, html: str) -> List[dict]:
        payload = self._listing_payload_from_html(html)
        if payload:
            products = [
                self._product_from_shopify(product)
                for product in payload.get("products") or []
                if isinstance(product, dict)
            ]
            return dedupe_products([p for p in products if p.get("url")], self.logger, "peaksports listing")

        return self._extract_products_from_cards(html)

    def _extract_products_from_cards(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products = []
        for card in tree.css(cp.get("item_selector", "product-card.card--product, product-card, .card.card--product")):
            link = card.css_first(cp.get("item_url", "a.card-link[href*='/products/'], a.js-prod-link[href*='/products/'], a[href*='/products/']"))
            href = self._attr(link, "href")
            handle = self._product_handle_from_url(href or "")
            if not handle:
                continue
            url = self._product_url(handle)

            name = self._text(card.css_first(cp.get("item_name", ".card__title a, .card__title, a.card-link")))
            if not name:
                name = clean_text(link.attributes.get("aria-label") if link else None)
            if not name:
                continue

            price = parse_price(self._text(card.css_first(cp.get("item_price", ".price__current .js-value, .price__current"))))
            old_price = parse_price(self._text(card.css_first(cp.get("item_old_price", ".price__was .js-value, .price__was"))))
            if old_price is not None and price is not None and old_price <= price:
                old_price = None
            discount_percent = self._discount_percent(self._text(card.css_first(cp.get("item_discount", ".product-label--sale"))))
            if discount_percent is None and old_price and price and old_price > price:
                discount_percent = round((1 - price / old_price) * 100, 2)

            image = None
            for img in card.css(cp.get("item_image", "img")):
                image = self._image_url(
                    img.attributes.get("data-src")
                    or img.attributes.get("src")
                    or self._first_srcset_url(img.attributes.get("srcset"))
                )
                if image:
                    break

            badge_text = " ".join(
                self._text(node) or ""
                for node in card.css(cp.get("item_badges", ".product-label, .product-label--sold-out"))
            )
            availability, available = availability_from_text(badge_text)
            class_text = card.attributes.get("class", "")
            if "sold-out" in class_text.lower() or "épuis" in badge_text.lower() or "epuise" in badge_text.lower():
                availability, available = "Épuisé", False

            record = {
                "url": normalize_url(url) or url,
                "name": name,
                "title": name,
                "shop": self.site_name,
                "price": price,
                "old_price": old_price,
                "discount_percent": discount_percent,
                "image": image,
                "availability": availability,
                "available": available,
            }
            products.append(finalize_product_record({k: v for k, v in record.items() if v is not None}))

        return dedupe_products(products, self.logger, "peaksports listing fallback")

    @staticmethod
    def _discount_percent(value: Any) -> Optional[float]:
        text = clean_text(value)
        if not text:
            return None
        match = re.search(r"-?\s*(\d+(?:[.,]\d+)?)\s*%", text)
        if not match:
            return None
        parsed = parse_price(match.group(1))
        return parsed if parsed is not None else None

    def extract_pagination_from_html(self, html: str) -> dict:
        payload = self._listing_payload_from_html(html)
        if payload:
            current = int(payload.get("page") or 1)
            limit = int(payload.get("limit") or self.page_size)
            count = len(payload.get("products") or [])
            has_next = count >= limit and limit > 0
            return {
                "current_page": current,
                "total_pages": current + 1 if has_next else current,
                "has_next": has_next,
                "method": "shopify_products_json",
            }

        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1
        for link in tree.css("a[href*='?page=']"):
            query_page = dict(parse_qsl(urlsplit(self._attr(link, "href") or "").query)).get("page")
            try:
                page = int(query_page)
                total_pages = max(total_pages, page)
            except (TypeError, ValueError):
                pass
        next_node = tree.css_first(cp.get("pagination_next", "link[rel='next'][href], a[rel='next'][href]"))
        if next_node:
            next_page = dict(parse_qsl(urlsplit(self._attr(next_node, "href") or "").query)).get("page")
            try:
                total_pages = max(total_pages, int(next_page))
            except (TypeError, ValueError):
                pass
        has_next = next_node is not None or total_pages > current_page
        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
            "method": "html_page_query",
        }

    async def scrape_category_page(self, url: str) -> dict:
        meta = await self.fetch_html_with_meta(url)
        html = meta.get("html")
        if not html:
            return {"products": [], "pagination": {"total_pages": 1}, "error": meta.get("error") or "Failed to fetch"}
        try:
            products = self.extract_products_from_html(html)
            pagination = self.extract_pagination_from_html(html)
            return {"products": products, "pagination": pagination}
        except Exception as exc:
            return {"products": [], "pagination": {"total_pages": 1}, "error": str(exc)}

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        products: List[dict] = []
        page = 1
        while page <= self.max_pages:
            page_url = self.build_page_url(category_url, page)
            result = await self.scrape_category_page(page_url)
            if result.get("error"):
                break
            page_products = result.get("products") or []
            if not page_products:
                break
            products.extend(page_products)
            if limit and len(products) >= limit:
                return products[:limit]
            pagination = result.get("pagination") or {}
            if not pagination.get("has_next") or len(page_products) < self.page_size:
                break
            page += 1
        return products[:limit] if limit else dedupe_products(products, self.logger, "peaksports category")

    # ------------------------------------------------------------------
    # Shopify product mapping
    # ------------------------------------------------------------------

    def _price_tuple(self, product: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float], Dict[str, Any]]:
        variants = [v for v in product.get("variants") or [] if isinstance(v, dict)]
        priced = []
        for variant in variants:
            price = parse_price(variant.get("price"))
            if price is None:
                continue
            compare = parse_price(variant.get("compare_at_price"))
            priced.append((price, compare, variant))

        if not priced:
            return None, None, None, variants[0] if variants else {}

        price, compare, variant = min(priced, key=lambda row: row[0])
        old_price = compare if compare is not None and compare > price else None
        discount_percent = round((1 - price / old_price) * 100, 2) if old_price else None
        return price, old_price, discount_percent, variant

    def _variant_availability(self, variant: Dict[str, Any], availability: Optional[Dict[str, Dict[str, bool]]] = None) -> Tuple[Optional[str], Optional[bool]]:
        availability = availability or {}
        variant_id = clean_text(variant.get("id"))
        sku = clean_text(variant.get("sku"))
        available = None
        if variant_id and variant_id in availability.get("by_id", {}):
            available = availability["by_id"][variant_id]
        elif sku and sku in availability.get("by_sku", {}):
            available = availability["by_sku"][sku]
        elif isinstance(variant.get("available"), bool):
            available = variant["available"]

        if available is True:
            return "En stock", True
        if available is False:
            return "Rupture de stock", False
        return None, None

    def _product_availability(self, variants: List[Dict[str, Any]], availability: Optional[Dict[str, Dict[str, bool]]] = None) -> Tuple[Optional[str], Optional[bool]]:
        known = []
        for variant in variants:
            _, available = self._variant_availability(variant, availability)
            if available is not None:
                known.append(available)
        if any(known):
            return "En stock", True
        if known and not any(known):
            return "Rupture de stock", False
        return None, None

    def _images_from_product(self, product: Dict[str, Any]) -> List[str]:
        images = []
        for image in product.get("images") or []:
            if isinstance(image, dict):
                url = self._image_url(image.get("src"))
            else:
                url = self._image_url(image)
            if url:
                images.append(url)
        primary = product.get("image")
        if isinstance(primary, dict):
            primary = primary.get("src")
        primary_url = self._image_url(primary)
        if primary_url:
            images.insert(0, primary_url)
        return self._dedupe_list(images)

    def _first_valid_barcode(self, variants: List[Dict[str, Any]]) -> Optional[str]:
        for variant in variants:
            barcode = normalize_gtin(variant.get("barcode"))
            if barcode:
                return barcode
        return None

    def _first_sku(self, variants: List[Dict[str, Any]]) -> Optional[str]:
        for variant in variants:
            sku = clean_text(variant.get("sku"))
            if sku:
                return sku
        return None

    def _options_map(self, product: Dict[str, Any]) -> Dict[str, List[str]]:
        out = {}
        for option in product.get("options") or []:
            if not isinstance(option, dict):
                continue
            name = clean_text(option.get("name"))
            values = [clean_text(value) for value in option.get("values") or []]
            values = [value for value in values if value]
            if name and values:
                out[name] = values
        return out

    @staticmethod
    def _tag_list(value: Any) -> List[str]:
        if isinstance(value, str):
            raw = value.split(",")
        elif isinstance(value, list):
            raw = value
        else:
            raw = []
        tags = []
        for item in raw:
            tag = clean_text(item)
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    def _product_variants(self, product: Dict[str, Any], availability: Optional[Dict[str, Dict[str, bool]]] = None) -> List[Dict[str, Any]]:
        variants = []
        for variant in product.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            price = parse_price(variant.get("price"))
            old_price = parse_price(variant.get("compare_at_price"))
            if old_price is not None and price is not None and old_price <= price:
                old_price = None
            availability_text, available = self._variant_availability(variant, availability)
            row = {
                "id": clean_text(variant.get("id")),
                "title": clean_text(variant.get("title")),
                "sku": clean_text(variant.get("sku")),
                "barcode": normalize_gtin(variant.get("barcode")),
                "price": price,
                "old_price": old_price,
                "option1": clean_text(variant.get("option1")),
                "option2": clean_text(variant.get("option2")),
                "option3": clean_text(variant.get("option3")),
                "availability": availability_text,
                "available": available,
            }
            variants.append({k: v for k, v in row.items() if v not in (None, "", [], {})})
        return variants

    def _product_from_shopify(
        self,
        product: Dict[str, Any],
        *,
        source_url: Optional[str] = None,
        availability: Optional[Dict[str, Dict[str, bool]]] = None,
        include_detail: bool = False,
    ) -> Dict[str, Any]:
        handle = clean_text(product.get("handle")) or (self._product_handle_from_url(source_url or "") if source_url else None)
        product_url = self._product_url(handle) if handle else source_url
        variants = [v for v in product.get("variants") or [] if isinstance(v, dict)]
        price, old_price, discount_percent, chosen_variant = self._price_tuple(product)
        availability_text, available = self._product_availability(variants, availability)
        images = self._images_from_product(product)
        sku = self._first_sku(variants)
        barcode = self._first_valid_barcode(variants)
        description = self._clean_description(product.get("body_html") or product.get("description"))
        options = self._options_map(product)

        record: Dict[str, Any] = {
            "id": clean_text(product.get("id")),
            "product_id": clean_text(product.get("id")),
            "url": normalize_url(product_url) or product_url,
            "name": clean_text(product.get("title")),
            "title": clean_text(product.get("title")),
            "shop": self.site_name,
            "brand": clean_text(product.get("vendor")) or "PEAK",
            "vendor": clean_text(product.get("vendor")),
            "product_type": clean_text(product.get("product_type")),
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": images[0] if images else None,
            "sku": sku,
            "reference": sku,
            "barcode": barcode,
            "availability": availability_text,
            "available": available,
            "short_description": description,
        }

        if include_detail:
            tags = self._tag_list(product.get("tags"))
            detail_variants = self._product_variants(product, availability)
            specs = {
                "Vendor": clean_text(product.get("vendor")),
                "Product type": clean_text(product.get("product_type")),
                "Handle": handle,
                "Tags": tags,
                "Options": options,
                "Colors": options.get("Color") or options.get("Couleur"),
                "Sizes": options.get("Taille") or options.get("Size"),
                "Variant count": len(variants),
                "Published at": clean_text(product.get("published_at")),
                "Updated at": clean_text(product.get("updated_at")),
            }
            specs = {k: v for k, v in specs.items() if v not in (None, "", [], {})}
            record.update(
                {
                    "description": description,
                    "full_description": description,
                    "overview": description,
                    "images": images,
                    "specifications": specs,
                    "variants": detail_variants,
                    "options": options,
                    "tags": tags,
                    "colors": specs.get("Colors"),
                    "sizes": specs.get("Sizes"),
                    "selected_variant_id": clean_text(chosen_variant.get("id")) if chosen_variant else None,
                }
            )

        return finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    def _walk_json(self, value: Any) -> Iterable[Any]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from self._walk_json(child)
        elif isinstance(value, list):
            for item in value:
                yield from self._walk_json(item)

    def _availability_from_detail_html(self, html: Optional[str]) -> Dict[str, Dict[str, bool]]:
        availability = {"by_id": {}, "by_sku": {}}
        if not html:
            return availability
        tree = HTMLParser(html)

        for option in tree.css(self.selectors.get("product_page", {}).get("variants", "select[name='id'] option")):
            variant_id = clean_text(option.attributes.get("value"))
            if not variant_id:
                continue
            text = self._text(option) or ""
            is_disabled = "disabled" in option.attributes
            available = not is_disabled
            if "epuise" in text.lower() or "épuis" in text.lower() or "rupture" in text.lower():
                available = False
            availability["by_id"][variant_id] = available

        for script in tree.css(self.selectors.get("product_page", {}).get("jsonld", "script[type='application/ld+json']")):
            raw = script.text()
            if not raw:
                continue
            try:
                parsed = json.loads(html_lib.unescape(raw.strip()))
            except Exception:
                continue
            for obj in self._walk_json(parsed):
                if not isinstance(obj, dict) or "availability" not in obj:
                    continue
                _, available = availability_from_text(obj.get("availability"))
                if available is None:
                    continue
                sku = clean_text(obj.get("sku"))
                if sku:
                    availability["by_sku"][sku] = available
                offer_url = clean_text(obj.get("url"))
                if offer_url:
                    variant_id = dict(parse_qsl(urlsplit(offer_url).query)).get("variant")
                    if variant_id:
                        availability["by_id"][variant_id] = available

        return availability

    def _breadcrumbs_from_html(self, html: Optional[str]) -> List[str]:
        if not html:
            return []
        tree = HTMLParser(html)
        crumbs = []
        for obj in self._jsonld_objects(tree):
            if not isinstance(obj, dict) or obj.get("@type") != "BreadcrumbList":
                continue
            for item in obj.get("itemListElement") or []:
                name = clean_text(item.get("name") if isinstance(item, dict) else None)
                if name and name.lower() != "accueil" and name not in crumbs:
                    crumbs.append(name)
        if crumbs:
            return crumbs
        for node in tree.css("nav.breadcrumb a[href], .breadcrumb a[href], .breadcrumbs a[href]"):
            text = self._text(node)
            href = self._attr(node, "href")
            if text and href and "/products/" not in href and text not in crumbs:
                crumbs.append(text)
        return crumbs

    def _jsonld_objects(self, tree: HTMLParser) -> List[Any]:
        out = []
        for script in tree.css(self.selectors.get("product_page", {}).get("jsonld", "script[type='application/ld+json']")):
            raw = script.text()
            if not raw:
                continue
            try:
                out.append(json.loads(html_lib.unescape(raw.strip())))
            except Exception:
                continue
        return out

    async def scrape_product_details(self, url: str) -> dict:
        handle = self._product_handle_from_url(url)
        if not handle:
            return {"url": url, "error": "Could not extract product handle"}

        product_url = self._product_url(handle)
        html = await super().fetch_html(product_url)
        if html and not (self.html_dir / "detail_sample_1.html").exists():
            save_text_atomic(html, self.html_dir / "detail_sample_1.html", self.logger)

        metadata = html_product_metadata(html or "", product_url, self.base_url) if html else {}

        try:
            data = await self._fetch_json(self._product_json_url(handle))
            product = data.get("product") if isinstance(data, dict) else None
        except Exception as exc:
            self.logger.debug(f"PeakSports product JSON failed for {handle}: {exc}")
            product = None

        if not isinstance(product, dict):
            fallback = {
                "url": product_url,
                "title": metadata.get("title"),
                "name": metadata.get("title") or metadata.get("name"),
                **metadata,
                "shop": self.site_name,
            }
            return finalize_product_record({k: v for k, v in fallback.items() if v not in (None, "", [], {})})

        availability = self._availability_from_detail_html(html)
        record = self._product_from_shopify(
            product,
            source_url=product_url,
            availability=availability,
            include_detail=True,
        )

        if html:
            tree = HTMLParser(html)
            description_node = tree.css_first(self.selectors.get("product_page", {}).get("description", ".product-description"))
            description = self._text(description_node)
            if description:
                record["description"] = description
                record["full_description"] = description
                record["overview"] = description
                record["short_description"] = description
            images = self._detail_images(tree)
            if images:
                record["images"] = images
                record["image"] = images[0]

        for key, value in metadata.items():
            if value not in (None, "", [], {}) and key not in record:
                record[key] = value

        breadcrumbs = self._breadcrumbs_from_html(html)
        if breadcrumbs:
            record["breadcrumbs"] = breadcrumbs
            record["categories"] = [{"name": crumb} for crumb in breadcrumbs]

        record["url"] = normalize_url(product_url) or product_url
        record["shop"] = self.site_name
        return finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})

    def _detail_images(self, tree: HTMLParser) -> List[str]:
        pp = self.selectors.get("product_page", {})
        images = []
        for img in tree.css(pp.get("image_gallery", ".media-viewer img.product-image, .product-media img.product-image, img[itemprop='image']")):
            src = (
                img.attributes.get("data-src")
                or img.attributes.get("src")
                or self._first_srcset_url(img.attributes.get("srcset"))
            )
            image = self._image_url(src)
            if image:
                images.append(image)
        return self._dedupe_list(images)


def get_scraper(logger: logging.Logger) -> PeakSportsScraper:
    return PeakSportsScraper(logger)
