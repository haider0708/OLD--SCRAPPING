#!/usr/bin/env python3
"""
OpticBox Edge scraper - Converty public JSON API with productData detail pages.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

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


class OpticBoxEdgeScraper(FastScraper):
    """HTTP scraper for opticboxedge.shop using the Converty JSON API."""

    CATEGORY_NAME = "Toutes les lunettes"
    CURRENCY = "TND"
    PRODUCT_PATH_PREFIX = "/product/"

    def __init__(self, logger: logging.Logger):
        super().__init__("opticboxedge", logger)
        self.headers.update(self.config.get("headers", {}))
        api_config = self.config.get("api", {})
        self.page_limit = int(api_config.get("page_limit", 12) or 12)
        self.products_endpoint_template = api_config.get(
            "products_endpoint_template",
            f"{self.base_url}/api/v1/products?page={{page}}&limit={self.page_limit}&categoryIds=",
        )
        self.categories_endpoint = api_config.get(
            "categories_endpoint",
            f"{self.base_url}/api/v1/categories?page=1&limit=20",
        )
        self.product_url_template = api_config.get(
            "product_url_template",
            f"{self.base_url}/product/{{slug}}",
        )
        self._category_map: Dict[str, str] = {}
        self._product_cache_by_slug: Dict[str, Dict[str, Any]] = {}

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
            try:
                return json.loads(html_lib.unescape(stripped))
            except (TypeError, json.JSONDecodeError):
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

    @staticmethod
    def _computed_discount(price: Optional[float], old_price: Optional[float]) -> Optional[int]:
        if price is None or old_price is None or old_price <= 0 or old_price <= price:
            return None
        return int(round((1 - (price / old_price)) * 100))

    def _product_api_url(self, page: int = 1) -> str:
        return self.products_endpoint_template.format(page=max(1, int(page)))

    def _product_url(self, product_or_slug: Any) -> Optional[str]:
        if isinstance(product_or_slug, dict):
            slug = clean_text(product_or_slug.get("slug"))
        else:
            slug = clean_text(product_or_slug)
            if slug and slug.startswith(("http://", "https://", "/")):
                return self._normalize_product_url(slug)
        if not slug:
            return None
        return self.product_url_template.format(slug=quote(slug, safe=""))

    def _normalize_product_url(self, value: Any) -> Optional[str]:
        url = self._absolute_url(value)
        if not url:
            return None
        parts = urlsplit(url)
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        if parts.netloc.lower().removeprefix("www.") != base_host:
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
        if "/product/" not in path:
            return None
        slug = path.rsplit("/product/", 1)[-1].strip("/")
        return clean_text(slug)

    def _image_url(self, value: Any) -> Optional[str]:
        url = self._absolute_url(value)
        if not url:
            return None
        low = url.lower()
        if "cdn.converty.shop/images/" not in low:
            return None
        if not re.search(r"\.(?:jpe?g|png|webp|gif|avif)(?:\?|$)", low):
            return None
        return url

    def _description_images(self, description_html: Any) -> List[str]:
        text = clean_text(description_html)
        if not text or "<img" not in text.lower():
            return []
        tree = HTMLParser(f"<div>{text}</div>")
        return self._dedupe_values(
            self._image_url(self._attr(img, "src") or self._attr(img, "data-src"))
            for img in tree.css("img")
        )

    def _product_images(self, product: Dict[str, Any]) -> List[str]:
        images = []
        raw_images = product.get("images") or []
        if isinstance(raw_images, list):
            for item in raw_images:
                if isinstance(item, dict):
                    url = item.get("lg") or item.get("md") or item.get("sm") or item.get("url")
                else:
                    url = item
                image_url = self._image_url(url)
                if image_url:
                    images.append(image_url)
        images.extend(self._description_images(product.get("description")))
        return self._dedupe_values(images)

    async def _fetch_json(self, url: str) -> Optional[Any]:
        text = await self.fetch_html(url)
        data = self._json_payload(text)
        if data is None:
            self.logger.debug(f"OpticBox Edge API response was not JSON: {url}")
        return data

    async def _ensure_categories(self) -> Dict[str, str]:
        if self._category_map:
            return self._category_map
        data = await self._fetch_json(self.categories_endpoint)
        categories = data.get("data") if isinstance(data, dict) else data
        if not isinstance(categories, list):
            self._category_map = {}
            return self._category_map
        for category in categories:
            if not isinstance(category, dict):
                continue
            category_id = clean_text(category.get("_id") or category.get("id"))
            name = clean_text(category.get("name"))
            if category_id and name:
                self._category_map[category_id] = name
        return self._category_map

    def _source_category_names(self, product: Dict[str, Any]) -> List[str]:
        names = []
        for category_id in product.get("categories") or []:
            text_id = clean_text(category_id)
            if text_id and self._category_map.get(text_id):
                names.append(self._category_map[text_id])
        return self._dedupe_values(names)

    def _availability_from_api(self, product: Dict[str, Any]) -> Tuple[Optional[str], Optional[bool]]:
        status = clean_text(product.get("status"))
        if status and status.lower() not in {"shown", "active"}:
            return status, False
        new_stock = product.get("newStock") if isinstance(product.get("newStock"), dict) else {}
        if product.get("trackStock") and new_stock.get("outOfStock"):
            if new_stock.get("continueSellingWhenOutOfStock"):
                return "Sur commande", True
            return "Rupture de stock", False
        if status and status.lower() in {"shown", "active"}:
            return "En stock", True
        return None, None

    def _source_metadata(self, product: Dict[str, Any]) -> Dict[str, Any]:
        specs: Dict[str, Any] = {}
        category_ids = [clean_text(value) for value in (product.get("categories") or []) if clean_text(value)]
        category_names = self._source_category_names(product)
        if category_names:
            specs["source_categories"] = category_names
        if category_ids:
            specs["source_category_ids"] = category_ids
        for source, dest in (
            ("deliveryPrice", "delivery_price"),
            ("fakeStock", "fake_stock"),
            ("fakeViews", "fake_views"),
            ("trackStock", "track_stock"),
            ("status", "source_status"),
        ):
            if product.get(source) not in (None, "", [], {}):
                specs[dest] = product.get(source)
        new_stock = product.get("newStock")
        if isinstance(new_stock, dict) and new_stock:
            specs["stock"] = new_stock
        for key in ("options", "variants", "newVariants", "clientCombinations", "defaultCombination", "discounts"):
            if product.get(key) not in (None, "", [], {}):
                specs[key] = product.get(key)
        related_slugs = []
        for item in product.get("relatedProducts") or []:
            if isinstance(item, dict) and clean_text(item.get("slug")):
                related_slugs.append(clean_text(item.get("slug")))
        if related_slugs:
            specs["related_product_slugs"] = self._dedupe_values(related_slugs)
        reviews = product.get("reviews")
        if isinstance(reviews, list):
            specs["reviews_count"] = len(reviews)
        return specs

    def _record_from_api_product(
        self,
        product: Dict[str, Any],
        include_description: bool = False,
        page_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(product, dict):
            return None
        slug = clean_text(product.get("slug"))
        name = clean_text(product.get("name"))
        product_id = clean_text(product.get("_id") or product.get("id"))
        if not slug or not name:
            return None

        price = parse_price(product.get("price"))
        old_price = parse_price(product.get("comparePrice"))
        if old_price is not None and (price is None or old_price <= price):
            old_price = None
        discount_percent = self._computed_discount(price, old_price)
        images = self._product_images(product)
        availability, available = self._availability_from_api(product)
        reference = clean_text(product.get("reference"))
        description = self._clean_html_text(product.get("description")) if include_description else None
        source_categories = self._source_category_names(product)
        source_category_ids = [
            clean_text(value)
            for value in (product.get("categories") or [])
            if clean_text(value)
        ]
        specs = self._source_metadata(product)

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "slug": slug,
            "url": self._product_url(slug),
            "name": name,
            "title": name,
            "shop": self.site_name,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "currency": self.CURRENCY,
            "reference": reference,
            "sku": reference,
            "image": images[0] if images else None,
            "images": images or None,
            "delivery_price": parse_price(product.get("deliveryPrice")),
            "shipping_fee": parse_price(product.get("deliveryPrice")),
            "availability": availability,
            "available": available,
            "source_categories": source_categories or None,
            "source_category_ids": source_category_ids or None,
            "description": description,
            "short_description": description,
            "overview": description,
            "full_description": description,
            "specifications": specs or None,
            "categories": [{"name": self.CATEGORY_NAME, "url": self._product_api_url(1)}],
            "breadcrumbs": [self.CATEGORY_NAME] + source_categories,
        }
        if page_metadata:
            for key, value in page_metadata.items():
                if value in (None, "", [], {}):
                    continue
                if record.get(key) not in (None, "", [], {}):
                    continue
                record[key] = value
            record.setdefault("title", name)
            record.setdefault("name", name)

        self._product_cache_by_slug[slug] = dict(product)
        return finalize_product_record({key: value for key, value in record.items() if value is not None})

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        categories = [
            {
                "name": self.CATEGORY_NAME,
                "url": self._product_api_url(1),
                "level": "top",
                "category_id": "all-products",
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
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            products = [self._record_from_api_product(item) for item in data["data"]]
            return dedupe_products([p for p in products if p], self.logger, "opticboxedge api listing")
        if isinstance(data, list):
            products = [self._record_from_api_product(item) for item in data]
            return dedupe_products([p for p in products if p], self.logger, "opticboxedge api listing")
        return self._extract_products_from_rendered_html(html)

    def _extract_products_from_rendered_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products: List[dict] = []
        for link in tree.css("a[href^='/product/'], a[href*='/product/']"):
            url = self._normalize_product_url(self._attr(link, "href"))
            if not url:
                continue
            slug = self._slug_from_url(url)
            img = link.css_first("img[alt]")
            name = self._attr(img, "alt") or self._text(link)
            if not name:
                continue
            body_text = self._text(link) or ""
            prices = [parse_price(value) for value in re.findall(r"\d+(?:[.,]\d+)?\s*(?:TND|DT)", body_text, flags=re.I)]
            prices = [value for value in prices if value is not None]
            price = prices[0] if prices else None
            old_price = None
            for candidate in prices[1:]:
                if price is not None and candidate > price:
                    old_price = candidate
                    break
            image = self._image_url(self._attr(img, "src"))
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
                "currency": self.CURRENCY,
                "image": image,
                "shop": self.site_name,
                "categories": [{"name": self.CATEGORY_NAME, "url": self._product_api_url(1)}],
                "breadcrumbs": [self.CATEGORY_NAME],
            }
            products.append(finalize_product_record({key: value for key, value in record.items() if value is not None}))
        return dedupe_products(products, self.logger, "opticboxedge rendered listing")

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        await self._ensure_categories()
        all_products: List[dict] = []
        reported_total: Optional[int] = None
        max_pages = int(self.config.get("settings", {}).get("max_pagination_pages", 10) or 10)

        for page in range(1, max_pages + 1):
            data = await self._fetch_json(self.build_page_url(category_url, page))
            if not isinstance(data, dict):
                break
            items = data.get("data")
            if not isinstance(items, list) or not items:
                break
            reported_total = self._safe_int(data.get("count")) or reported_total
            for item in items:
                record = self._record_from_api_product(item)
                if record:
                    all_products.append(record)
            deduped = dedupe_products(all_products, self.logger, "opticboxedge category")
            if limit and len(deduped) >= limit:
                return deduped[:limit]
            if reported_total is not None and len(deduped) >= reported_total:
                return deduped[:limit] if limit else deduped

        deduped = dedupe_products(all_products, self.logger, "opticboxedge category")
        return deduped[:limit] if limit else deduped

    def extract_pagination_from_html(self, html: str) -> dict:
        data = self._json_payload(html)
        if isinstance(data, dict):
            total_results = self._safe_int(data.get("count"))
            items = data.get("data") if isinstance(data.get("data"), list) else []
            if total_results is None:
                total_results = len(items)
            total_pages = max(1, math.ceil(total_results / self.page_limit)) if total_results else 1
            return {
                "current_page": 1,
                "total_pages": total_pages,
                "has_next": bool(items) and total_pages > 1,
                "total_results": total_results,
            }
        products = self.extract_products_from_html(html)
        return {
            "current_page": 1,
            "total_pages": 1,
            "has_next": False,
            "total_results": len(products),
        }

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url or self._product_api_url(1))
        pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "page"]
        keys = {key for key, _ in pairs}
        if "limit" not in keys:
            pairs.append(("limit", str(self.page_limit)))
        if "categoryIds" not in keys:
            pairs.append(("categoryIds", ""))
        pairs.insert(0, ("page", str(max(1, int(page_num)))))
        query = urlencode(pairs, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        await self._ensure_categories()
        product_url = self._normalize_product_url(url) or url
        slug = self._slug_from_url(product_url)
        if not slug:
            return {"url": url, "error": "Missing product slug"}

        html = await self.fetch_html(product_url)
        if html:
            product = self._product_data_from_html(html)
            if isinstance(product, dict):
                metadata = html_product_metadata(html, product_url, self.base_url)
                record = self._record_from_api_product(product, include_description=True, page_metadata=metadata)
                if record:
                    return record

        fallback = await self._detail_from_products_cache(slug)
        if fallback:
            return fallback
        return {"url": product_url, "error": "Product detail not found"}

    async def _detail_from_products_cache(self, slug: str) -> Optional[dict]:
        product = self._product_cache_by_slug.get(slug)
        if product:
            return self._record_from_api_product(product, include_description=True)

        max_pages = int(self.config.get("settings", {}).get("max_pagination_pages", 10) or 10)
        for page in range(1, max_pages + 1):
            data = await self._fetch_json(self._product_api_url(page))
            if not isinstance(data, dict) or not isinstance(data.get("data"), list) or not data["data"]:
                break
            for item in data["data"]:
                if isinstance(item, dict) and clean_text(item.get("slug")) == slug:
                    return self._record_from_api_product(item, include_description=True)
        return None

    def _product_data_from_html(self, html: str) -> Optional[Dict[str, Any]]:
        tree = HTMLParser(html)
        script = tree.css_first("script#productData[type='application/json'], script#productData")
        if script:
            data = self._json_payload(script.text())
            if isinstance(data, dict):
                return data
        match = re.search(
            r"<script[^>]*id=[\"']productData[\"'][^>]*>(.*?)</script>",
            html,
            flags=re.I | re.S,
        )
        if match:
            data = self._json_payload(match.group(1))
            if isinstance(data, dict):
                return data
        return None


def get_scraper(logger: logging.Logger) -> OpticBoxEdgeScraper:
    return OpticBoxEdgeScraper(logger)
