#!/usr/bin/env python3
"""
Bershka.com/tn scraper - Inditex SPA/API storefront, direct HTTP API.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from scraper.base import FastScraper, get_date_folder, save_json, save_text_atomic
from scraper.product_utils import (
    availability_from_text,
    clean_text,
    dedupe_products,
    finalize_product_record,
    normalize_gtin,
    normalize_url,
    parse_price,
)


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class BershkaScraper(FastScraper):
    """Direct API scraper for Bershka Tunisia."""

    CATEGORY_RE = re.compile(r"-c(?!0p)(\d+)(?:\.html)?(?:$|[?#])", re.I)
    PRODUCT_RE = re.compile(r"c0p(\d+)(?:\.html)?(?:$|[?#])", re.I)
    CATEGORY_ID_RE = re.compile(r"^\d{6,}$")
    SKIP_TOPS = {"footer", "by influencers"}
    SKIP_NAMES = {"footer", "gift card", "newsletter", "help", "espacio"}

    def __init__(self, logger: logging.Logger):
        super().__init__("bershka", logger)
        self.api_base_url = self.config.get("api_base_url", "https://www.bershka.com").rstrip("/")
        self.store_id = str(self.config.get("store_id", 45009578))
        self.section_id = str(self.config.get("section_id", 40259549))
        self.language_id = str(self.config.get("language_id", -15))
        self.locale = str(self.config.get("locale", "en_GB"))
        settings = self.config.get("settings", {})
        self.product_chunk_size = int(settings.get("product_chunk_size", 30))
        self.probe_product_limit = int(settings.get("probe_product_limit", 8))
        self._menu_cache: Optional[Dict[str, Any]] = None
        self._category_product_ids_cache: Dict[str, List[str]] = {}
        self._product_cache: Dict[str, Dict[str, Any]] = {}

        self.headers.update(self._api_headers())

    # ------------------------------------------------------------------
    # URL and API helpers
    # ------------------------------------------------------------------

    def _api_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-GB,en;q=0.9,en-US;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Referer": self.base_url,
            "Origin": self.api_base_url,
        }

    def _endpoint(self, name: str, **extra: Any) -> str:
        endpoints = self.config.get("endpoints", {})
        template = endpoints.get(name, "")
        values = {
            "store_id": self.store_id,
            "section_id": self.section_id,
            "language_id": self.language_id,
            "locale": self.locale,
            **extra,
        }
        return urljoin(self.api_base_url, template.format(**values))

    async def _api_json(self, url: str) -> Optional[Dict[str, Any]]:
        client = await self.get_client()
        response = await client.get(url, headers=self._api_headers())
        if response.status_code >= 400:
            self.logger.debug(f"Bershka API HTTP {response.status_code}: {url}")
            return None
        try:
            data = response.json()
        except json.JSONDecodeError:
            self.logger.debug(f"Bershka API non-JSON response: {url}")
            return None
        return data if isinstance(data, dict) else None

    def _api_json_sync(self, url: str, client: Optional[httpx.Client] = None) -> Optional[Dict[str, Any]]:
        try:
            if client is not None:
                response = client.get(url)
                if response.status_code >= 400:
                    self.logger.debug(f"Bershka API HTTP {response.status_code}: {url}")
                    return None
                data = response.json()
                return data if isinstance(data, dict) else None

            with httpx.Client(
                headers=self._api_headers(),
                follow_redirects=True,
                timeout=self.request_timeout,
            ) as client:
                response = client.get(url)
            if response.status_code >= 400:
                self.logger.debug(f"Bershka API HTTP {response.status_code}: {url}")
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except Exception as exc:
            self.logger.debug(f"Bershka API fetch failed {url}: {exc}")
            return None

    @staticmethod
    def _slugify(value: Any) -> str:
        text = clean_text(value) or "product"
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
        return text or "product"

    @staticmethod
    def _clean(value: Any) -> Optional[str]:
        return clean_text(value)

    @staticmethod
    def _price(value: Any) -> Optional[float]:
        parsed = parse_price(value)
        if parsed is None:
            return None
        return round(parsed / 100, 2)

    def _product_url(self, product: Dict[str, Any]) -> Optional[str]:
        product_id = self._clean(product.get("id"))
        if not product_id:
            return None
        slug = self._slugify(product.get("nameEn") or product.get("name") or product.get("productUrl"))
        return urljoin(self.base_url.rstrip("/") + "/", f"{slug}-c0p{product_id}.html")

    def _category_url(self, category_id: str, name_path: List[str], node: Dict[str, Any]) -> str:
        seo = node.get("seo") or {}
        keyword = clean_text(seo.get("keyword"))
        if keyword:
            path = keyword.strip("/")
        else:
            path = "products/" + "/".join(self._slugify(part) for part in name_path if part)
        return urljoin(self.base_url.rstrip("/") + "/", f"{path}-c{category_id}.html")

    def _category_id_from_url(self, url: str) -> Optional[str]:
        match = self.CATEGORY_RE.search(url or "")
        if match:
            return match.group(1)
        parsed = urlsplit(url or "")
        query = parse_qs(parsed.query)
        raw = query.get("category_id", [None])[0] or query.get("categoryId", [None])[0]
        raw = self._clean(raw)
        return raw if raw and self.CATEGORY_ID_RE.match(raw) else None

    def _product_id_from_url(self, url: str) -> Optional[str]:
        match = self.PRODUCT_RE.search(url or "")
        if match:
            return match.group(1)
        parsed = urlsplit(url or "")
        query = parse_qs(parsed.query)
        raw = query.get("product_id", [None])[0] or query.get("productId", [None])[0]
        raw = self._clean(raw)
        return raw if raw and self.CATEGORY_ID_RE.match(raw) else None

    def _strip_url(self, url: str) -> str:
        parts = urlsplit(url)
        path = parts.path.rstrip("/") if parts.path != "/" else parts.path
        query = urlencode([(k, v) for k, v in parse_qs(parts.query, keep_blank_values=True).items() for v in v])
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path or "/", query, ""))

    # ------------------------------------------------------------------
    # Frontpage and categories
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        output_path = self.html_dir / "frontpage.html"
        meta = await super().fetch_html_with_meta(self.base_url)
        html = meta.get("html")
        if not html:
            html = json.dumps(
                {
                    "url": self.base_url,
                    "status_code": meta.get("status_code"),
                    "error": meta.get("error"),
                    "blocked_signals": meta.get("blocked_signals") or [],
                },
                ensure_ascii=False,
                indent=2,
            )
        save_text_atomic(html, output_path, self.logger)

        menu = await self._fetch_menu_async()
        if menu:
            save_json(menu, self.html_dir / "menu_api.json", self.logger)
        return output_path

    async def _fetch_menu_async(self) -> Optional[Dict[str, Any]]:
        if self._menu_cache is not None:
            return self._menu_cache
        menu = await self._api_json(self._endpoint("menu"))
        self._menu_cache = menu
        return menu

    def _fetch_menu_sync(self) -> Optional[Dict[str, Any]]:
        if self._menu_cache is not None:
            return self._menu_cache
        menu_path = self.html_dir / "menu_api.json"
        if menu_path.exists():
            try:
                self._menu_cache = json.loads(menu_path.read_text(encoding="utf-8"))
                return self._menu_cache
            except (OSError, json.JSONDecodeError):
                pass
        self._menu_cache = self._api_json_sync(self._endpoint("menu"))
        if self._menu_cache:
            save_json(self._menu_cache, menu_path, self.logger)
        return self._menu_cache

    def extract_categories_from_html(self, html: str) -> dict:
        menu = self._fetch_menu_sync()
        if not menu:
            return {"categories": [], "stats": {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}}

        candidates = self._collect_category_candidates(menu)
        categories = self._build_category_tree(candidates)
        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _collect_category_candidates(self, menu: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        seen = set()

        for top in menu.get("items", []) or []:
            top_name = self._clean(top.get("name"))
            if not top_name or top_name.lower() in self.SKIP_TOPS or top_name.upper() not in {"WOMEN", "MEN"}:
                continue
            for child in top.get("children", []) or top.get("items", []) or []:
                self._collect_category_node(child, top_name, [], candidates, seen)

        return candidates

    def _collect_category_node(
        self,
        node: Dict[str, Any],
        top_name: str,
        path: List[str],
        out: List[Dict[str, Any]],
        seen: set,
    ) -> None:
        name = self._clean(node.get("canonicalName") or node.get("name"))
        if not name or name.lower() in self.SKIP_NAMES or node.get("isHidden"):
            return

        next_path = [*path, name]
        children = node.get("children", []) or node.get("items", []) or []
        for child in children:
            self._collect_category_node(child, top_name, next_path, out, seen)

        # Parent menu groups often have huge overlapping product APIs. Keep the
        # shoppable leaves and explicit "View all" leaves to avoid redundant
        # category probing while preserving catalog coverage.
        if children and name.lower() != "view all":
            return

        category_id = self._resolved_category_id(node)
        if not category_id or category_id in seen:
            return

        display_path = self._dedupe_path(next_path)
        seen.add(category_id)
        out.append(
            {
                "id": category_id,
                "name": name,
                "url": self._category_url(category_id, [top_name, *display_path], node),
                "top_category": top_name,
                "path": display_path,
                "product_count_hint": None,
            }
        )

    @staticmethod
    def _dedupe_path(path: List[str]) -> List[str]:
        out: List[str] = []
        for item in path:
            if out and out[-1].strip().lower() == item.strip().lower():
                continue
            out.append(item)
        return out

    def _resolved_category_id(self, node: Dict[str, Any]) -> Optional[str]:
        content = node.get("content") or {}
        content_id = self._clean(content.get("id"))
        if content.get("type") == "redirection" and content_id and self.CATEGORY_ID_RE.match(content_id):
            return content_id
        node_id = self._clean(node.get("id"))
        return node_id if node_id and self.CATEGORY_ID_RE.match(node_id) else None

    def _fetch_category_product_ids_sync(self, category_id: str, client: Optional[httpx.Client] = None) -> List[str]:
        category_id = str(category_id)
        if category_id in self._category_product_ids_cache:
            return self._category_product_ids_cache[category_id]

        url = self._endpoint("category_products", category_id=category_id)
        data = self._api_json_sync(url, client)
        ids = self._product_ids_from_category_payload(data)
        self._category_product_ids_cache[category_id] = ids
        return ids

    async def _fetch_category_product_ids(self, category_id: str) -> List[str]:
        category_id = str(category_id)
        if category_id in self._category_product_ids_cache:
            return self._category_product_ids_cache[category_id]

        url = self._endpoint("category_products", category_id=category_id)
        data = await self._api_json(url)
        ids = self._product_ids_from_category_payload(data)
        self._category_product_ids_cache[category_id] = ids
        return ids

    @staticmethod
    def _product_ids_from_category_payload(data: Optional[Dict[str, Any]]) -> List[str]:
        if not isinstance(data, dict):
            return []
        out: List[str] = []
        seen = set()

        def add(value: Any) -> None:
            text = clean_text(value)
            if text and text.isdigit() and text not in seen:
                seen.add(text)
                out.append(text)

        for value in data.get("productIds", []) or []:
            add(value)
        for value in data.get("sortedProductIds", []) or []:
            add(value)
        for grid in data.get("gridElements", []) or []:
            for key in ("commercialComponents", "commercialComponentIds"):
                for component in grid.get(key, []) or []:
                    add(component.get("id") or component.get("ccId"))
        return out

    def _build_category_tree(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        top_map: Dict[str, Dict[str, Any]] = {}

        for cat in candidates:
            top_name = cat.get("top_category") or "Bershka"
            path = [p for p in cat.get("path", []) if p]
            low_name = path[0] if path else cat["name"]
            is_direct_low = len(path) <= 1

            top = top_map.setdefault(
                top_name,
                {"name": top_name, "url": "", "level": "top", "low_level_categories": [], "_low_map": {}},
            )
            low_map = top["_low_map"]
            low = low_map.setdefault(
                low_name,
                {
                    "name": low_name,
                    "url": cat["url"] if is_direct_low else "",
                    "level": "low",
                    "category_id": cat["id"] if is_direct_low else None,
                    "product_count_hint": cat.get("product_count_hint", 0) if is_direct_low else 0,
                    "subcategories": [],
                    "_sub_ids": set(),
                },
            )

            if is_direct_low:
                low["url"] = cat["url"]
                low["category_id"] = cat["id"]
                low["product_count_hint"] = cat.get("product_count_hint", 0)
                continue

            sub_name = " / ".join(path[1:]) if len(path) > 2 else path[-1]
            if cat["id"] in low["_sub_ids"]:
                continue
            low["_sub_ids"].add(cat["id"])
            low["subcategories"].append(
                {
                    "name": sub_name,
                    "url": cat["url"],
                    "level": "subcategory",
                    "category_id": cat["id"],
                    "product_count_hint": cat.get("product_count_hint", 0),
                }
            )

        categories: List[Dict[str, Any]] = []
        for top in top_map.values():
            low_categories = []
            for low in top.pop("_low_map").values():
                low.pop("_sub_ids", None)
                if not low.get("url") and low.get("subcategories"):
                    low["url"] = low["subcategories"][0].get("url", "")
                if low.get("url") or low.get("subcategories"):
                    low_categories.append(low)
            top["low_level_categories"] = low_categories
            if low_categories:
                top["url"] = low_categories[0].get("url", "")
            categories.append(top)
        return categories

    @staticmethod
    def _category_stats(categories: List[Dict[str, Any]]) -> Dict[str, int]:
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["top_level"] += 1
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top.get("low_level_categories", []) or []:
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
                for sub in low.get("subcategories", []) or []:
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1
        return stats

    # ------------------------------------------------------------------
    # Synthetic HTML bridge and listings
    # ------------------------------------------------------------------

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> Dict[str, Any]:
        category_id = self._category_id_from_url(url)
        if category_id:
            products = await self._fetch_category_products(category_id, limit=self.probe_product_limit)
            html = json.dumps({"type": "bershka_listing", "category_id": category_id, "products": products}, ensure_ascii=False)
            return {
                "html": html,
                "status_code": 200,
                "final_url": url,
                "content_type": "application/json",
                "content_encoding": None,
                "attempts": 1,
                "elapsed_ms": 0,
                "blocked_signals": [],
                "error": None,
            }

        product_id = self._product_id_from_url(url)
        if product_id:
            product = await self._fetch_product_payload(product_id)
            html = json.dumps({"type": "bershka_detail", "product": product or {}}, ensure_ascii=False)
            return {
                "html": html,
                "status_code": 200 if product else 404,
                "final_url": url,
                "content_type": "application/json",
                "content_encoding": None,
                "attempts": 1,
                "elapsed_ms": 0,
                "blocked_signals": [],
                "error": None if product else "product_not_found",
            }

        return await super().fetch_html_with_meta(url, raise_on_error=raise_on_error)

    async def scrape_category_page(self, url: str) -> dict:
        category_id = self._category_id_from_url(url)
        if not category_id:
            return {"products": [], "pagination": self.extract_pagination_from_html(""), "error": "missing_category_id"}

        products = await self._fetch_category_products(category_id)
        sample_path = self.html_dir / "listing_sample_1.html"
        if products and not sample_path.exists():
            save_text_atomic(
                json.dumps({"type": "bershka_listing", "category_id": category_id, "products": products[:12]}, ensure_ascii=False, indent=2),
                sample_path,
                self.logger,
            )
        return {"products": products, "pagination": self.extract_pagination_from_html("")}

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        category_id = self._category_id_from_url(category_url)
        if not category_id:
            return []
        return await self._fetch_category_products(category_id, limit=limit)

    def extract_products_from_html(self, html: str) -> List[dict]:
        if not html:
            return []
        try:
            data = json.loads(html)
        except json.JSONDecodeError:
            return []
        products = data.get("products", []) if isinstance(data, dict) else []
        if not isinstance(products, list):
            return []
        return dedupe_products([finalize_product_record(p) for p in products if isinstance(p, dict)], self.logger, self.site_name)

    async def _fetch_category_products(self, category_id: str, limit: Optional[int] = None) -> List[dict]:
        product_ids = await self._fetch_category_product_ids(category_id)
        if limit:
            product_ids = product_ids[:limit]
        products = await self._fetch_products_by_ids(product_ids, category_id=category_id)
        return dedupe_products(products, self.logger, self.site_name)

    async def _fetch_products_by_ids(self, product_ids: Iterable[Any], category_id: Optional[str] = None) -> List[dict]:
        ids = [str(pid) for pid in product_ids if clean_text(pid)]
        if not ids:
            return []

        raw_products: List[Dict[str, Any]] = []
        missing = [pid for pid in ids if pid not in self._product_cache]
        for chunk in self._chunks(missing, self.product_chunk_size):
            url = self._endpoint("products_array", product_ids=",".join(chunk))
            data = await self._api_json(url)
            for product in (data or {}).get("products", []) or []:
                product_id = self._clean(product.get("id"))
                if product_id:
                    self._product_cache[product_id] = product

        for pid in ids:
            product = self._product_cache.get(pid)
            if product:
                raw_products.append(product)

        return [self._normalize_product(product, category_id=category_id, detail=False) for product in raw_products]

    @staticmethod
    def _chunks(items: List[str], size: int) -> Iterable[List[str]]:
        for index in range(0, len(items), max(size, 1)):
            yield items[index : index + size]

    def extract_pagination_from_html(self, html: str) -> dict:
        return {"current_page": 1, "total_pages": 1, "has_next": False}

    def build_page_url(self, base_url: str, page_num: int) -> str:
        if page_num <= 1:
            return base_url
        parts = urlsplit(base_url)
        query = parse_qs(parts.query, keep_blank_values=True)
        query["page"] = [str(page_num)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))

    # ------------------------------------------------------------------
    # Product normalization and details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        product_id = self._product_id_from_url(url) or self._clean(url)
        if not product_id:
            return {"url": url, "error": "missing_product_id"}

        product = await self._fetch_product_payload(product_id)
        if not product:
            return {"url": url, "error": "product_not_found"}

        sample_path = self.html_dir / "detail_sample_1.html"
        if not sample_path.exists():
            save_text_atomic(
                json.dumps({"type": "bershka_detail", "product": product}, ensure_ascii=False, indent=2),
                sample_path,
                self.logger,
            )

        data = self._normalize_product(product, detail=True)
        data["url"] = url if self._product_id_from_url(url) else data.get("url")
        return finalize_product_record(data)

    async def _fetch_product_payload(self, product_id: str) -> Optional[Dict[str, Any]]:
        product_id = str(product_id)
        if product_id in self._product_cache:
            return self._product_cache[product_id]
        url = self._endpoint("products_array", product_ids=product_id)
        data = await self._api_json(url)
        products = (data or {}).get("products", []) or []
        if not products:
            return None
        product = products[0]
        normalized_id = self._clean(product.get("id")) or product_id
        self._product_cache[normalized_id] = product
        return product

    def _normalize_product(
        self,
        product: Dict[str, Any],
        *,
        category_id: Optional[str] = None,
        detail: bool = False,
    ) -> Dict[str, Any]:
        product_id = self._clean(product.get("id"))
        name = self._clean(product.get("name") or product.get("nameEn"))
        title = name or f"Bershka product {product_id}"
        colors = list(self._iter_colors(product))
        sizes = list(self._iter_sizes(colors))
        price, old_price = self._first_price(product, colors, sizes)
        availability, available = self._availability(product, sizes)
        reference = self._reference(product, colors)
        sku = self._first_sku(sizes)
        barcode = self._first_gtin(sizes)
        images = self._images(product, colors)
        specs = self._specifications(product, colors, sizes)
        short_description, description = self._descriptions(product)

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": self._product_url(product),
            "name": title,
            "title": title,
            "brand": "Bershka",
            "price": price,
            "old_price": old_price,
            "discount_percent": self._discount_percent(price, old_price),
            "image": images[0] if images else None,
            "images": images or None,
            "reference": reference,
            "sku": sku,
            "barcode": barcode,
            "availability": availability,
            "available": available,
            "shop": self.site_name,
            "category_id": category_id,
            "currency": "TND",
        }

        if detail:
            record.update(
                {
                    "short_description": short_description,
                    "description": description or short_description,
                    "specifications": specs or None,
                    "variants": self._variants(colors),
                    "categories": self._product_categories(product),
                    "family": self._clean(product.get("familyName") or product.get("familyNameEN")),
                    "subfamily": self._clean(product.get("subFamilyName") or product.get("subFamilyNameEN")),
                    "section": self._clean(product.get("sectionNameEN")),
                }
            )

        return finalize_product_record({k: v for k, v in record.items() if v is not None})

    def _iter_colors(self, product: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        detail = product.get("detail") or {}
        for color in detail.get("colors", []) or []:
            if isinstance(color, dict):
                yield color

        for summary in product.get("bundleProductSummaries", []) or []:
            detail = summary.get("detail") or {}
            for color in detail.get("colors", []) or []:
                if isinstance(color, dict):
                    yield color

    @staticmethod
    def _iter_sizes(colors: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
        for color in colors:
            for size in color.get("sizes", []) or []:
                if isinstance(size, dict):
                    yield size

    def _first_price(
        self,
        product: Dict[str, Any],
        colors: List[Dict[str, Any]],
        sizes: List[Dict[str, Any]],
    ) -> Tuple[Optional[float], Optional[float]]:
        for size in sizes:
            price = self._price(size.get("price"))
            old_price = self._price(size.get("oldPrice"))
            if price is not None:
                return price, old_price
        for color in colors:
            price = self._price(color.get("price"))
            old_price = self._price(color.get("oldPrice"))
            if price is not None:
                return price, old_price
        return self._price(product.get("price")), self._price(product.get("oldPrice"))

    @staticmethod
    def _discount_percent(price: Optional[float], old_price: Optional[float]) -> Optional[float]:
        if price is None or old_price is None or old_price <= 0 or price >= old_price:
            return None
        return round((1 - price / old_price) * 100, 2)

    def _availability(self, product: Dict[str, Any], sizes: List[Dict[str, Any]]) -> Tuple[str, Optional[bool]]:
        if sizes:
            for size in sizes:
                visibility = self._clean(size.get("visibilityValue") or "").upper()
                is_buyable = size.get("isBuyable")
                back_soon = str(size.get("backSoon", "0"))
                if visibility == "SHOW" and is_buyable is not False and back_soon != "1":
                    return "En stock", True
            if any((self._clean(size.get("visibilityValue") or "").upper() == "COMING_SOON") for size in sizes):
                return "Coming soon", None
            return "Rupture de stock", False

        if product.get("isBuyable") is True and self._clean(product.get("state")) == "visible":
            return "En stock", True
        availability, available = availability_from_text(product.get("visibilityValue"))
        return availability or "Availability unknown", available

    def _reference(self, product: Dict[str, Any], colors: List[Dict[str, Any]]) -> Optional[str]:
        detail = product.get("detail") or {}
        for value in (detail.get("reference"), detail.get("displayReference"), product.get("id")):
            text = self._clean(value)
            if text:
                return text
        for color in colors:
            text = self._clean(color.get("reference"))
            if text:
                return text
        return None

    def _first_sku(self, sizes: List[Dict[str, Any]]) -> Optional[str]:
        for size in sizes:
            sku = self._clean(size.get("partnumber") or size.get("sku"))
            if sku:
                return sku
        return None

    def _first_gtin(self, sizes: List[Dict[str, Any]]) -> Optional[str]:
        for size in sizes:
            for attr in size.get("attributes", []) or []:
                if str(attr.get("type", "")).upper() == "GTINCODE":
                    gtin = normalize_gtin(attr.get("name") or attr.get("value"))
                    if gtin:
                        return gtin
        return None

    def _images(self, product: Dict[str, Any], colors: List[Dict[str, Any]]) -> List[str]:
        images: List[str] = []
        seen = set()

        def add(url: Any) -> None:
            text = self._clean(url)
            if not text:
                return
            if text.startswith("//"):
                text = "https:" + text
            if text.startswith("assets/"):
                text = f"https://static.bershka.net/{text}"
            if not text.startswith(("http://", "https://")):
                return
            normalized = normalize_url(text) or text
            if normalized not in seen:
                seen.add(normalized)
                images.append(text)

        details = [product.get("detail") or {}]
        details.extend((summary.get("detail") or {}) for summary in product.get("bundleProductSummaries", []) or [])
        for detail in details:
            for xmedia in detail.get("xmedia", []) or []:
                for item in xmedia.get("xmediaItems", []) or []:
                    for media in item.get("medias", []) or []:
                        extra = media.get("extraInfo") or {}
                        add(media.get("url") or extra.get("deliveryUrl") or extra.get("url"))

        for color in colors:
            image = color.get("image") or {}
            if isinstance(image, dict):
                path = image.get("url")
                if path:
                    add(f"https://static.bershka.net{path}.jpg")

        return images

    def _descriptions(self, product: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        detail = product.get("detail") or {}
        short = self._clean(detail.get("description"))
        full = self._clean(detail.get("longDescription"))
        tags = []
        for attr in product.get("attributes", []) or []:
            if attr.get("type") == "PRODUCT_TAG":
                value = self._clean(attr.get("value") or attr.get("name"))
                if value and value not in tags:
                    tags.append(value)
        if not short and tags:
            short = ", ".join(tags)
        if not short:
            fallback = self._product_categories(product)
            if fallback:
                short = ", ".join(fallback)
        return short, full or short

    def _composition(self, detail: Dict[str, Any]) -> List[str]:
        values: List[str] = []
        for part in detail.get("composition", []) or []:
            for comp in part.get("composition", []) or []:
                material = self._clean(comp.get("name"))
                percentage = self._clean(comp.get("percentage") or comp.get("description"))
                if material and percentage:
                    values.append(f"{material} {percentage}%")
                elif material:
                    values.append(material)
        return values

    def _care(self, detail: Dict[str, Any]) -> List[str]:
        values = []
        for row in detail.get("care", []) or []:
            text = self._clean(row.get("description") or row.get("name"))
            if text and text not in values:
                values.append(text)
        return values

    def _specifications(
        self,
        product: Dict[str, Any],
        colors: List[Dict[str, Any]],
        sizes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        detail = product.get("detail") or {}
        specs: Dict[str, Any] = {
            "brand": "Bershka",
            "product_type": self._clean(product.get("productType")),
            "family": self._clean(product.get("familyName") or product.get("familyNameEN")),
            "subfamily": self._clean(product.get("subFamilyName") or product.get("subFamilyNameEN")),
            "section": self._clean(product.get("sectionNameEN")),
            "reference": self._clean(detail.get("reference")),
            "display_reference": self._clean(detail.get("displayReference")),
        }
        color_names = [self._clean(color.get("name")) for color in colors if self._clean(color.get("name"))]
        if color_names:
            specs["colors"] = sorted(set(color_names))
        size_names = [self._clean(size.get("name")) for size in sizes if self._clean(size.get("name"))]
        if size_names:
            specs["sizes"] = sorted(set(size_names))
        composition = self._composition(detail)
        if composition:
            specs["composition"] = composition
        care = self._care(detail)
        if care:
            specs["care"] = care
        return {k: v for k, v in specs.items() if v}

    def _variants(self, colors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        variants = []
        for color in colors:
            sizes = []
            for size in color.get("sizes", []) or []:
                sizes.append(
                    {
                        "sku": self._clean(size.get("sku")),
                        "partnumber": self._clean(size.get("partnumber")),
                        "name": self._clean(size.get("name")),
                        "price": self._price(size.get("price")),
                        "old_price": self._price(size.get("oldPrice")),
                        "visibility": self._clean(size.get("visibilityValue")),
                        "available": self._clean(size.get("visibilityValue")) == "SHOW" and size.get("isBuyable") is not False,
                    }
                )
            variants.append(
                {
                    "color_id": self._clean(color.get("id")),
                    "color": self._clean(color.get("name")),
                    "reference": self._clean(color.get("reference")),
                    "sizes": [row for row in sizes if any(v is not None for v in row.values())],
                }
            )
        return [variant for variant in variants if variant.get("color") or variant.get("sizes")]

    def _product_categories(self, product: Dict[str, Any]) -> List[str]:
        values = []
        for key in ("sectionNameEN", "familyName", "familyNameEN", "subFamilyName", "subFamilyNameEN"):
            value = self._clean(product.get(key))
            if value and value not in values:
                values.append(value)
        return values


def get_scraper(logger: logging.Logger) -> BershkaScraper:
    return BershkaScraper(logger)
