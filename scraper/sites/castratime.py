#!/usr/bin/env python3
"""
CastraTime scraper - Boutiki public JSON API, HTTP/selectolax fallback.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from scraper.base import FastScraper
from scraper.product_utils import (
    absolute_url,
    clean_text,
    dedupe_products,
    finalize_product_record,
    html_product_metadata,
    normalize_url,
    parse_price,
)


class CastraTimeScraper(FastScraper):
    """HTTP scraper for castratime.online using the public Boutiki API."""

    CATEGORY_NAME = "Toutes les montres"
    CURRENCY = "TND"
    PRODUCT_PATH_PREFIX = "/p/"

    def __init__(self, logger: logging.Logger):
        super().__init__("castratime", logger)
        self.headers.update(self.config.get("headers", {}))
        api_config = self.config.get("api", {})
        self.store_slug = api_config.get("store_slug", "castra-time")
        self.products_endpoint = api_config.get(
            "products_endpoint",
            f"{self.base_url}/api/v1/public/stores/{self.store_slug}/products?",
        )
        self.detail_endpoint_template = api_config.get(
            "detail_endpoint_template",
            f"{self.base_url}/api/v1/public/stores/{self.store_slug}/products/{{slug}}/detail",
        )
        self._products_cache: Optional[List[dict]] = None

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

    @staticmethod
    def _text(node: Any, separator: str = " ") -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=separator, strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

    @staticmethod
    def _attr(node: Any, name: str) -> Optional[str]:
        if not node:
            return None
        return clean_text(node.attributes.get(name))

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _dedupe_values(values: Iterable[Any]) -> List[str]:
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
    def _json_payload(text: Any) -> Optional[Any]:
        if not isinstance(text, str):
            return None
        stripped = text.strip()
        if not stripped or stripped[0] not in "[{":
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _clean_html_text(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if "<" not in text or ">" not in text:
            return text
        tree = HTMLParser(f"<div>{text}</div>")
        node = tree.css_first("div")
        return clean_text(node.text(separator=" ", strip=True) if node else text)

    def _api_image_url(self, value: Any) -> Optional[str]:
        url = self._absolute_url(value)
        if not url:
            return None
        low = url.lower()
        if "/api/public/uploads/" not in low:
            return None
        if not re.search(r"\.(?:jpe?g|png|webp|gif|avif)(?:\?|$)", low):
            return None
        return url

    def _product_url(self, value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if text.startswith(self.PRODUCT_PATH_PREFIX):
            return self._absolute_url(text)
        url = self._absolute_url(text)
        if not url:
            return None
        parts = urlsplit(url)
        if parts.netloc.lower().removeprefix("www.") != urlsplit(self.base_url).netloc.lower().removeprefix("www."):
            return None
        path = parts.path.rstrip("/")
        if not path.startswith(self.PRODUCT_PATH_PREFIX) or path == self.PRODUCT_PATH_PREFIX.rstrip("/"):
            return None
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))

    @staticmethod
    def _slug_from_url(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        path = urlsplit(text).path.rstrip("/")
        if "/p/" not in path:
            return None
        slug = path.rsplit("/p/", 1)[-1].strip("/")
        return clean_text(slug)

    @staticmethod
    def _computed_discount(price: Optional[float], old_price: Optional[float]) -> Optional[int]:
        if price is None or old_price is None or old_price <= 0 or old_price <= price:
            return None
        return int(round((1 - (price / old_price)) * 100))

    def _product_images(self, product: Dict[str, Any]) -> List[str]:
        raw_images = product.get("images") or []
        if not isinstance(raw_images, list):
            return []

        def sort_key(item: Any) -> Tuple[int, int]:
            if not isinstance(item, dict):
                return (9999, 9999)
            return (
                self._safe_int(item.get("sortOrder")) if self._safe_int(item.get("sortOrder")) is not None else 9999,
                self._safe_int(item.get("id")) if self._safe_int(item.get("id")) is not None else 9999,
            )

        images = []
        for item in sorted(raw_images, key=sort_key):
            url = self._api_image_url(item.get("url") if isinstance(item, dict) else item)
            if url:
                images.append(url)
        return self._dedupe_values(images)

    def _availability_from_api(self, product: Dict[str, Any]) -> Tuple[Optional[str], Optional[bool]]:
        status = clean_text(product.get("status"))
        stock_enabled = bool(product.get("stockEnabled"))
        stock_qty = self._safe_int(product.get("stockQty"))
        if status and status.upper() != "ACTIVE":
            return status, False
        if stock_enabled and (stock_qty is None or stock_qty <= 0):
            return "Rupture de stock", False
        if status and status.upper() == "ACTIVE":
            return "En stock", True
        return None, None

    def _source_metadata(self, product: Dict[str, Any]) -> Dict[str, Any]:
        specs: Dict[str, Any] = {}
        category_name = clean_text(product.get("categoryName"))
        category_id = product.get("categoryId")
        if category_name:
            specs["source_collection"] = category_name
        if category_id is not None:
            specs["source_collection_id"] = category_id
        if product.get("shippingFee") is not None:
            specs["shipping_fee"] = product.get("shippingFee")
        if product.get("stockEnabled") is not None:
            specs["stock_enabled"] = bool(product.get("stockEnabled"))
        if product.get("stockQty") is not None:
            specs["stock_qty"] = product.get("stockQty")
        if product.get("options"):
            specs["options"] = product.get("options")
        if product.get("variants"):
            specs["variants"] = product.get("variants")
        return specs

    def _record_from_api_product(self, product: Dict[str, Any], include_description: bool = False) -> Optional[Dict[str, Any]]:
        if not isinstance(product, dict):
            return None
        slug = clean_text(product.get("slug"))
        name = clean_text(product.get("name"))
        if not slug or not name:
            return None

        price = parse_price(product.get("price"))
        old_price = parse_price(product.get("compareAtPrice"))
        if old_price is not None and (price is None or old_price <= price):
            old_price = None
        discount_percent = self._computed_discount(price, old_price)
        images = self._product_images(product)
        availability, available = self._availability_from_api(product)
        product_id = clean_text(product.get("id"))
        description = self._clean_html_text(product.get("description")) if include_description else None
        specs = self._source_metadata(product)

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "slug": slug,
            "url": self._absolute_url(f"/p/{slug}"),
            "name": name,
            "title": name,
            "shop": self.site_name,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "currency": self.CURRENCY,
            "image": images[0] if images else None,
            "images": images or None,
            "shipping_fee": product.get("shippingFee"),
            "availability": availability,
            "available": available,
            "source_category": clean_text(product.get("categoryName")),
            "source_category_id": product.get("categoryId"),
            "description": description,
            "short_description": description,
            "overview": description,
            "full_description": description,
            "specifications": specs or None,
            "categories": [{"name": self.CATEGORY_NAME, "url": self.products_endpoint}],
            "breadcrumbs": [self.CATEGORY_NAME],
        }
        return finalize_product_record({key: value for key, value in record.items() if value is not None})

    async def _fetch_json(self, url: str) -> Optional[Any]:
        text = await self.fetch_html(url)
        data = self._json_payload(text)
        if data is None:
            self.logger.debug(f"CastraTime API response was not JSON: {url}")
        return data

    async def _fetch_products(self) -> List[dict]:
        if self._products_cache is not None:
            return list(self._products_cache)
        data = await self._fetch_json(self.products_endpoint)
        if isinstance(data, list):
            self._products_cache = data
        elif isinstance(data, dict) and isinstance(data.get("products"), list):
            self._products_cache = data["products"]
        elif isinstance(data, dict) and isinstance(data.get("items"), list):
            self._products_cache = data["items"]
        else:
            self._products_cache = []
        return list(self._products_cache)

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        categories = [
            {
                "name": self.CATEGORY_NAME,
                "url": self.products_endpoint,
                "level": "top",
                "category_id": "frontpage",
                "low_level_categories": [],
            }
        ]
        return {
            "categories": categories,
            "stats": {"top_level": 1, "low_level": 0, "subcategory": 0, "total_urls": 1},
        }

    # ------------------------------------------------------------------
    # Listings and pagination
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        data = self._json_payload(html)
        if isinstance(data, list):
            products = [self._record_from_api_product(item) for item in data]
            return dedupe_products([p for p in products if p], self.logger, "castratime api listing")
        if isinstance(data, dict) and isinstance(data.get("products"), list):
            products = [self._record_from_api_product(item) for item in data["products"]]
            return dedupe_products([p for p in products if p], self.logger, "castratime api listing")
        return self._extract_products_from_rendered_html(html)

    def _extract_products_from_rendered_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products: List[dict] = []
        for link in tree.css("a[href^='/p/'], a[href*='/p/']"):
            url = self._product_url(self._attr(link, "href"))
            if not url:
                continue
            slug = self._slug_from_url(url)
            img = link.css_first("img[alt]")
            name = self._attr(img, "alt") or self._text(link)
            if not name:
                continue
            prices = [parse_price(value) for value in re.findall(r"\d+(?:[.,]\d+)?\s*TND", self._text(link) or "")]
            prices = [value for value in prices if value is not None]
            price = prices[0] if prices else None
            old_price = prices[1] if len(prices) > 1 and prices[1] > price else None
            image = self._api_image_url(self._attr(img, "src"))
            record = {
                "id": slug,
                "product_id": slug,
                "slug": slug,
                "url": url,
                "name": name,
                "title": name,
                "price": price,
                "old_price": old_price,
                "discount_percent": self._computed_discount(price, old_price),
                "image": image,
                "currency": self.CURRENCY,
                "shop": self.site_name,
            }
            products.append(finalize_product_record({key: value for key, value in record.items() if value is not None}))
        return dedupe_products(products, self.logger, "castratime rendered listing")

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        products = [
            self._record_from_api_product(item)
            for item in await self._fetch_products()
        ]
        deduped = dedupe_products([p for p in products if p], self.logger, "castratime category")
        return deduped[:limit] if limit else deduped

    def extract_pagination_from_html(self, html: str) -> dict:
        products = self.extract_products_from_html(html)
        return {
            "current_page": 1,
            "total_pages": 1,
            "has_next": False,
            "total_results": len(products),
        }

    def build_page_url(self, base_url: str, page_num: int) -> str:
        return self.products_endpoint if page_num <= 1 else self.products_endpoint

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        slug = self._slug_from_url(url) or clean_text(url)
        if not slug:
            return {"url": url, "error": "Missing product slug"}
        detail_url = self.detail_endpoint_template.format(slug=quote(slug, safe=""))
        data = await self._fetch_json(detail_url)
        product = data.get("product") if isinstance(data, dict) else None
        if isinstance(product, dict):
            record = self._record_from_api_product(product, include_description=True)
            if record:
                return record

        fallback = await self._detail_from_products_cache(slug)
        if fallback:
            return fallback

        html = await self.fetch_html(self._absolute_url(f"/p/{slug}") or url)
        if html:
            rendered = self.extract_product_details_from_html(html, self._absolute_url(f"/p/{slug}") or url)
            if rendered and not rendered.get("error"):
                return rendered
        return {"url": url, "error": "Product detail not found"}

    async def _detail_from_products_cache(self, slug: str) -> Optional[dict]:
        for product in await self._fetch_products():
            if clean_text(product.get("slug")) == slug:
                return self._record_from_api_product(product, include_description=True)
        return None

    def extract_product_details_from_html(self, html: str, product_url: str) -> dict:
        data: Dict[str, Any] = html_product_metadata(html, product_url, self.base_url)
        tree = HTMLParser(html)
        url = self._product_url(product_url) or product_url
        slug = self._slug_from_url(url)
        title = self._text(tree.css_first("h1")) or data.get("title")
        body_text = self._text(tree.css_first("body")) or ""
        prices = [parse_price(value) for value in re.findall(r"\d+(?:[.,]\d+)?\s*TND", body_text)]
        prices = [value for value in prices if value is not None]
        price = prices[0] if prices else data.get("price")
        old_price = None
        for candidate in prices[1:]:
            if price is not None and candidate > price:
                old_price = candidate
                break
        images = self._dedupe_values(
            self._api_image_url(self._attr(node, "src") or self._attr(node, "content"))
            for node in tree.css("img[src*='/api/public/uploads/'], meta[property='og:image']")
        )
        record = {
            **data,
            "id": data.get("product_id") or slug,
            "product_id": data.get("product_id") or slug,
            "slug": slug,
            "url": url,
            "title": title,
            "name": title,
            "price": price,
            "old_price": old_price,
            "discount_percent": self._computed_discount(price, old_price),
            "currency": self.CURRENCY,
            "image": images[0] if images else data.get("image"),
            "images": images or data.get("images"),
            "categories": [{"name": self.CATEGORY_NAME, "url": self.products_endpoint}],
            "breadcrumbs": [self.CATEGORY_NAME],
            "shop": self.site_name,
        }
        return finalize_product_record({key: value for key, value in record.items() if value is not None})


def get_scraper(logger: logging.Logger) -> CastraTimeScraper:
    return CastraTimeScraper(logger)
