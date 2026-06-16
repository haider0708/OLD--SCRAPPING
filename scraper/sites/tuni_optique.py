#!/usr/bin/env python3
"""
Tuni Optique scraper - Custom Tiktak/CloudTiktak storefront.

The public category pages are Vue/API-backed, so this scraper uses the
Tiktak JSON endpoints and returns synthetic listing HTML for the shared
probe/parsing contract.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import math
import re
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, save_text_atomic
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    clean_text,
    dedupe_products,
    extract_gtins_from_text,
    finalize_product_record,
    html_product_metadata,
    normalize_gtin,
    normalize_url,
    parse_price,
)


class TuniOptiqueScraper(FastScraper):
    """HTTP/API scraper for tuni-optique.com."""

    CURRENCY = "TND"
    CATEGORY_PATH_RE = re.compile(r"/product-list/(\d+)(?:/|$)", re.I)
    PRODUCT_PATH_RE = re.compile(r"/product/(\d+)(?:/|$)", re.I)
    LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
    BAD_CATEGORY_PARTS = (
        "/account",
        "/blog",
        "/cart",
        "/checkout",
        "/contact",
        "/login",
        "/pages/",
        "/product/",
        "/search",
        "facebook.com",
        "instagram.com",
        "mailto:",
        "tel:",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("tuni-optique", logger)
        self.headers.update(self.config.get("headers", {}))
        self.api_base_url = str(self.config.get("api_base_url", "")).rstrip("/")
        self.media_base_url = str(self.config.get("media_base_url", "https://cloudtiktak.com")).rstrip("/")
        self.company_id = str(self.config.get("company_id", "MLwZmXG"))
        settings = self.config.get("settings", {})
        self.page_size = int(settings.get("page_size", 12) or 12)
        self.max_pages = int(settings.get("max_pagination_pages", 20) or 20)
        self._category_cache: Optional[List[Dict[str, Any]]] = None
        self._category_counts: Dict[str, int] = {}
        self._category_map: Dict[str, Dict[str, Any]] = {}
        self._product_cache: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Common helpers
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
            return int(float(str(value).strip()))
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
        root = tree.css_first("div")
        return clean_text(root.text(separator=" ", strip=True) if root else text)

    @staticmethod
    def _computed_discount(price: Optional[float], old_price: Optional[float]) -> Optional[float]:
        if price is None or old_price is None or old_price <= 0 or price >= old_price:
            return None
        return round(((old_price - price) / old_price) * 100, 2)

    @staticmethod
    def _meaningful_reference(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text or text.upper() in {"N/A", "NA", "ND", "SKU", "REF"}:
            return None
        if len(text) > 80:
            return None
        return text.strip(" :;|-")

    def _api_url(self, key: str, **kwargs: Any) -> str:
        endpoints = self.config.get("endpoints", {})
        template = endpoints.get(key)
        if not template:
            raise KeyError(f"Missing Tuni Optique endpoint config: {key}")
        values = {
            "company_id": self.company_id,
            "page_size": self.page_size,
            **kwargs,
        }
        return template.format(**values)

    async def _fetch_plain_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        meta = await super().fetch_html_with_meta(url, raise_on_error=raise_on_error)
        return meta.get("html")

    async def _fetch_json_url(self, url: str) -> Optional[Any]:
        text = await self._fetch_plain_html(url)
        data = self._json_payload(text)
        if data is None:
            self.logger.debug(f"Tuni Optique response was not JSON: {url}")
        return data

    def _same_host_url(self, value: Any) -> Optional[str]:
        url = self._absolute_url(value)
        if not url:
            return None
        parts = urlsplit(url)
        if parts.netloc.lower().removeprefix("www.") != urlsplit(self.base_url).netloc.lower().removeprefix("www."):
            return None
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, ""))

    def _category_id_from_url(self, url: Any) -> Optional[str]:
        text = clean_text(url)
        if not text:
            return None
        match = self.CATEGORY_PATH_RE.search(urlsplit(text).path)
        if match:
            return match.group(1)
        params = dict(parse_qsl(urlsplit(text).query, keep_blank_values=True))
        return clean_text(params.get("has_category") or params.get("category_id"))

    def _product_id_from_url(self, url: Any) -> Optional[str]:
        text = clean_text(url)
        if not text:
            return None
        match = self.PRODUCT_PATH_RE.search(urlsplit(text).path)
        if match:
            return match.group(1)
        params = dict(parse_qsl(urlsplit(text).query, keep_blank_values=True))
        candidate = clean_text(params.get("product_id") or params.get("id"))
        return candidate if candidate and candidate.isdigit() else None

    def _page_from_url(self, url: Any) -> int:
        params = dict(parse_qsl(urlsplit(clean_text(url) or "").query, keep_blank_values=True))
        return max(1, self._safe_int(params.get("page")) or 1)

    def _category_public_url(self, category: Dict[str, Any]) -> Optional[str]:
        category_id = clean_text(category.get("id"))
        if not category_id:
            return None
        slug = clean_text(category.get("seo_slug") or category.get("slug") or category.get("name")) or category_id
        return f"{self.base_url.rstrip()}/product-list/{category_id}/{quote(slug.strip('/'), safe='-_~')}/"

    def _product_url(self, product_or_id: Any) -> Optional[str]:
        product_id = clean_text(product_or_id.get("id")) if isinstance(product_or_id, dict) else clean_text(product_or_id)
        if not product_id:
            return None
        return f"{self.base_url.rstrip('/')}/product/{product_id}/"

    def _image_url(self, value: Any) -> Optional[str]:
        if isinstance(value, dict):
            value = value.get("image") or value.get("url") or value.get("src") or value.get("photo")
        text = clean_text(value)
        if not text:
            return None
        if text.startswith("/media/"):
            return absolute_url(text, self.media_base_url)
        return absolute_url(text, self.base_url)

    def _description_images(self, description_html: Any) -> List[str]:
        text = clean_text(description_html)
        if not text or "<img" not in text.lower():
            return []
        tree = HTMLParser(f"<div>{text}</div>")
        return self._dedupe_values(
            self._image_url(img.attributes.get("src") or img.attributes.get("data-src"))
            for img in tree.css("img")
        )

    def _product_images(self, product: Dict[str, Any], include_description: bool = False) -> List[str]:
        values: List[Any] = [product.get("photo"), product.get("image")]
        for item in product.get("images") or []:
            if isinstance(item, dict):
                values.append(item.get("image") or item.get("url"))
            else:
                values.append(item)
        images = [self._image_url(value) for value in values]
        if include_description:
            images.extend(self._description_images(product.get("description")))
        return self._dedupe_values(images)

    def _prices(self, product: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        raw_price = parse_price(product.get("price"))
        discount = parse_price(product.get("discount"))
        discount_type = (clean_text(product.get("discount_type")) or "").lower()
        if raw_price is None:
            return None, None, None
        if discount is None or discount <= 0:
            return raw_price, None, None
        sale_price = raw_price
        if "percent" in discount_type or "%" in discount_type or "percentage" in discount_type:
            sale_price = raw_price * (1 - discount / 100)
        else:
            sale_price = raw_price - discount
        if sale_price < 0:
            sale_price = raw_price
        sale_price = round(sale_price, 3)
        old_price = raw_price if sale_price < raw_price else None
        return sale_price, old_price, self._computed_discount(sale_price, old_price)

    def _availability(self, product: Dict[str, Any], fallback_text: Any = None) -> Tuple[Optional[str], Optional[bool]]:
        text, available = availability_from_text(fallback_text)
        if text or available is not None:
            return text, available
        if product.get("active") is False or product.get("display_on_website") is False:
            return "Inactive", False
        stock = self._safe_int(product.get("total_stock"))
        if stock is None:
            stock = self._safe_int(product.get("stock"))
        if bool(product.get("active_stock")):
            if stock is not None and stock > 0:
                return f"En stock ({stock})", True
            if product.get("order_without_stock"):
                return "Commande possible", True
            # Published Tiktak product pages expose InStock JSON-LD even when
            # internal stock counters are zero, so keep public availability true.
            return "En stock", True
        return "En stock", True

    def _brand_from_product(self, product: Dict[str, Any]) -> Optional[str]:
        brand = product.get("brand")
        if isinstance(brand, dict):
            return clean_text(brand.get("name") or brand.get("title"))
        return clean_text(brand)

    def _category_from_product(self, product: Dict[str, Any]) -> Dict[str, Any]:
        category = product.get("_category")
        if isinstance(category, dict):
            return category
        category_id = clean_text(product.get("category"))
        return self._category_map.get(category_id, {}) if category_id else {}

    def _specifications(self, product: Dict[str, Any]) -> Dict[str, Any]:
        specs: Dict[str, Any] = {}
        category = self._category_from_product(product)
        if category:
            specs["source_category"] = {
                key: category.get(key)
                for key in ("id", "name", "seo_slug")
                if category.get(key) not in (None, "", [], {})
            }
        source_category_ids = [clean_text(value) for value in (product.get("categories") or []) if clean_text(value)]
        if source_category_ids:
            specs["source_category_ids"] = source_category_ids
        for source, dest in (
            ("features", "features"),
            ("attributs", "attributes"),
            ("product_type", "product_type"),
            ("taxe_rate", "tax_rate"),
            ("delivery_price", "delivery_price"),
            ("custom_delivery_price", "custom_delivery_price"),
            ("stock", "stock"),
            ("total_stock", "total_stock"),
            ("active_stock", "active_stock"),
            ("order_without_stock", "order_without_stock"),
            ("seo_slug", "seo_slug"),
            ("seo_title", "seo_title"),
        ):
            value = product.get(source)
            if value not in (None, "", [], {}):
                specs[dest] = value
        return specs

    def _variants(self, product: Dict[str, Any]) -> List[Dict[str, Any]]:
        variants: List[Dict[str, Any]] = []
        for item in product.get("declinaisons") or []:
            if not isinstance(item, dict):
                continue
            price, old_price, discount = self._prices(item)
            if price is None:
                price, old_price, discount = self._prices(product)
            availability, available = self._availability(item)
            reference = self._meaningful_reference(item.get("reference") or item.get("sku"))
            barcode = normalize_gtin(item.get("bar_code") or item.get("barcode"))
            variant = {
                "id": clean_text(item.get("id")),
                "name": clean_text(item.get("name") or item.get("label") or item.get("value")),
                "reference": reference,
                "sku": reference,
                "barcode": barcode,
                "price": price,
                "old_price": old_price,
                "discount_percent": discount,
                "availability": availability,
                "available": available,
                "image": self._image_url(item.get("photo") or item.get("image")),
            }
            variant = {key: value for key, value in finalize_product_record(variant).items() if value not in (None, "", [], {})}
            if variant:
                variants.append(variant)
        return variants

    def _record_from_product(
        self,
        product: Dict[str, Any],
        include_description: bool = False,
        page_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(product, dict):
            return None
        product_id = clean_text(product.get("id"))
        name = clean_text(product.get("name"))
        if not product_id or not name:
            return None

        price, old_price, discount = self._prices(product)
        images = self._product_images(product, include_description=include_description)
        availability, available = self._availability(product)
        reference = self._meaningful_reference(product.get("reference"))
        barcode = normalize_gtin(product.get("bar_code")) or None
        description = self._clean_html_text(product.get("description")) if include_description else None
        if include_description and not barcode:
            gtins = extract_gtins_from_text(product.get("description"))
            barcode = gtins[0] if gtins else None
        category = self._category_from_product(product)
        category_name = clean_text(category.get("name"))
        category_id = clean_text(category.get("id"))

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": self._product_url(product_id),
            "name": name,
            "title": name,
            "shop": self.site_name,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount,
            "currency": self.CURRENCY,
            "reference": reference,
            "sku": reference,
            "barcode": barcode,
            "brand": self._brand_from_product(product),
            "image": images[0] if images else None,
            "images": images,
            "availability": availability,
            "available": available,
            "category": category_name,
            "category_id": category_id,
            "source_category_id": category_id,
            "source_category_name": category_name,
            "description": description,
            "short_description": description,
            "overview": description,
            "full_description": description,
            "specifications": self._specifications(product),
            "variants": self._variants(product),
            "breadcrumbs": [category_name] if category_name else None,
            "categories": [
                {
                    "id": category_id,
                    "name": category_name,
                    "url": self._category_public_url(category),
                }
            ]
            if category_name
            else None,
        }

        if page_metadata:
            for key, value in page_metadata.items():
                if value in (None, "", [], {}):
                    continue
                if key == "images" and value:
                    merged = self._dedupe_values((record.get("images") or []) + list(value))
                    record["images"] = merged
                    record["image"] = record.get("image") or (merged[0] if merged else None)
                    continue
                if key not in record or record.get(key) in (None, "", [], {}):
                    record[key] = value

        self._product_cache[product_id] = dict(product)
        cleaned = finalize_product_record(record)
        return {
            key: value
            for key, value in cleaned.items()
            if value is not None and value != "" and value != [] and value != {}
        }

    # ------------------------------------------------------------------
    # Evidence and categories
    # ------------------------------------------------------------------

    async def _fetch_categories_from_api(self) -> List[Dict[str, Any]]:
        if self._category_cache is not None:
            return self._category_cache
        categories: List[Dict[str, Any]] = []
        page = 1
        while page <= 20:
            data = await self._fetch_json_url(self._api_url("categories", page=page))
            if not isinstance(data, dict):
                break
            items = data.get("results")
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if isinstance(item, dict) and clean_text(item.get("id")):
                    categories.append(item)
                    self._category_map[clean_text(item.get("id"))] = item
            total_pages = self._safe_int(data.get("total_pages")) or page
            if page >= total_pages:
                break
            page += 1
        self._category_cache = categories
        return categories

    async def _probe_category_counts(self, categories: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        probes: Dict[str, Dict[str, Any]] = {}
        for category in categories:
            category_id = clean_text(category.get("id"))
            if not category_id:
                continue
            data = await self._fetch_json_url(self._listing_api_url(category_id, 1))
            if not isinstance(data, dict):
                probes[category_id] = {"count": 0, "total_pages": 1, "sample_product_id": None}
                continue
            first_product_id = None
            results = data.get("results")
            if isinstance(results, list) and results:
                first_product_id = clean_text(results[0].get("id")) if isinstance(results[0], dict) else None
            count = self._safe_int(data.get("count")) or 0
            self._category_counts[category_id] = count
            probes[category_id] = {
                "count": count,
                "total_pages": self._safe_int(data.get("total_pages")) or 1,
                "sample_product_id": first_product_id,
            }
        return probes

    async def download_frontpage(self):
        html = await self._fetch_plain_html(self.base_url, raise_on_error=True)
        save_text_atomic(html, self.html_dir / "frontpage.html", self.logger)

        for key, filename in (("robots_url", "robots.txt"), ("sitemap_url", "sitemap.xml")):
            url = self.config.get("endpoints", {}).get(key)
            if not url:
                continue
            try:
                text = await self._fetch_plain_html(url)
                if text:
                    save_text_atomic(text, self.html_dir / filename, self.logger)
            except Exception as exc:
                self.logger.debug(f"Tuni Optique evidence fetch failed for {url}: {exc}")

        categories = await self._fetch_categories_from_api()
        save_text_atomic(
            json.dumps({"results": categories}, ensure_ascii=False, indent=2),
            self.html_dir / "categories_api.json",
            self.logger,
        )
        probes = await self._probe_category_counts(categories)
        save_text_atomic(
            json.dumps(probes, ensure_ascii=False, indent=2),
            self.html_dir / "category_product_probe.json",
            self.logger,
        )

        first_product_url = None
        for category in categories:
            category_id = clean_text(category.get("id"))
            if not category_id or self._category_counts.get(category_id, 0) <= 0:
                continue
            payload = await self._fetch_listing_payload(self._category_public_url(category) or "")
            listing_html = self._listing_payload_to_html(payload)
            save_text_atomic(listing_html, self.html_dir / "listing_sample_1.html", self.logger)
            products = payload.get("products") or []
            if products:
                first_product_url = self._product_url(products[0])
            break

        if first_product_url:
            try:
                detail_html = await self._fetch_plain_html(first_product_url)
                if detail_html:
                    save_text_atomic(detail_html, self.html_dir / "detail_sample_1.html", self.logger)
            except Exception as exc:
                self.logger.debug(f"Tuni Optique detail sample save failed for {first_product_url}: {exc}")
        return self.html_dir / "frontpage.html"

    def _load_json_file(self, filename: str) -> Any:
        path = self.html_dir / filename
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _menu_categories_from_html(self, html: str) -> Dict[str, Dict[str, Any]]:
        tree = HTMLParser(html or "")
        out: Dict[str, Dict[str, Any]] = {}
        selector = self.selectors.get("frontpage", {}).get("category_links") or "a[href*='/product-list/']"
        for node in tree.css(selector):
            href = self._attr(node, "href")
            category_id = self._category_id_from_url(href)
            if not category_id:
                continue
            url = self._same_host_url(href) or self._absolute_url(href)
            low = (url or href or "").lower()
            if any(token in low for token in self.BAD_CATEGORY_PARTS):
                continue
            name = self._text(node) or category_id
            out.setdefault(category_id, {"id": category_id, "name": name, "url": url})
        return out

    def _sitemap_categories(self) -> Dict[str, Dict[str, Any]]:
        text = clean_text((self.html_dir / "sitemap.xml").read_text(encoding="utf-8")) if (self.html_dir / "sitemap.xml").exists() else None
        out: Dict[str, Dict[str, Any]] = {}
        if not text:
            return out
        for raw_url in self.LOC_RE.findall(text):
            url = html_lib.unescape(raw_url.strip())
            category_id = self._category_id_from_url(url)
            if not category_id:
                continue
            path = urlsplit(url).path
            if not re.match(r"^/product-list/\d+/[^/?#]+/?$", path, flags=re.I):
                continue
            slug = path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
            out.setdefault(category_id, {"id": category_id, "name": slug, "url": url})
        return out

    def extract_categories_from_html(self, html: str) -> dict:
        api_data = self._load_json_file("categories_api.json")
        raw_categories = api_data.get("results") if isinstance(api_data, dict) else None
        categories = raw_categories if isinstance(raw_categories, list) else []
        probes = self._load_json_file("category_product_probe.json")
        probes = probes if isinstance(probes, dict) else {}
        menu = self._menu_categories_from_html(html)
        sitemap = self._sitemap_categories()

        ordered_ids: List[str] = []
        for source in (menu, {clean_text(c.get("id")): c for c in categories if isinstance(c, dict)}, sitemap):
            for category_id in source:
                if category_id and category_id not in ordered_ids:
                    ordered_ids.append(category_id)

        by_id: Dict[str, Dict[str, Any]] = {}
        for category in categories:
            if isinstance(category, dict) and clean_text(category.get("id")):
                by_id[clean_text(category.get("id"))] = category
        for source in (menu, sitemap):
            for category_id, category in source.items():
                by_id.setdefault(category_id, category)

        output = []
        for category_id in ordered_ids:
            category = by_id.get(category_id)
            if not isinstance(category, dict):
                continue
            count = self._safe_int((probes.get(category_id) or {}).get("count"))
            if count is not None and count <= 0:
                continue
            url = self._category_public_url(category) or menu.get(category_id, {}).get("url") or sitemap.get(category_id, {}).get("url")
            if not url:
                continue
            name = clean_text(category.get("name") or menu.get(category_id, {}).get("name") or sitemap.get(category_id, {}).get("name"))
            if not name:
                continue
            output.append(
                {
                    "name": name,
                    "url": url,
                    "level": "top",
                    "category_id": category_id,
                    "source_category_id": category_id,
                    "product_count": count,
                    "low_level_categories": [],
                }
            )

        return {
            "categories": output,
            "stats": {
                "top_level": len(output),
                "low_level": 0,
                "subcategory": 0,
                "total_urls": len(output),
            },
        }

    # ------------------------------------------------------------------
    # Listings and pagination
    # ------------------------------------------------------------------

    def _listing_api_url(self, category_id: str, page: int) -> str:
        return self._api_url("products", category_id=category_id, page=max(1, int(page)))

    def _is_category_url(self, url: Any) -> bool:
        return bool(self._category_id_from_url(url))

    async def _fetch_listing_payload(self, url: str) -> Dict[str, Any]:
        category_id = self._category_id_from_url(url)
        if not category_id:
            return {"__tuni_optique_listing__": True, "products": [], "page": 1, "total_pages": 1, "has_next": False}
        page = self._page_from_url(url)
        data = await self._fetch_json_url(self._listing_api_url(category_id, page))
        if not isinstance(data, dict):
            return {"__tuni_optique_listing__": True, "products": [], "page": page, "total_pages": 1, "has_next": False}
        products = data.get("results") if isinstance(data.get("results"), list) else []
        for product in products:
            if isinstance(product, dict) and clean_text(product.get("id")):
                self._product_cache[clean_text(product.get("id"))] = product
        total_pages = self._safe_int(data.get("total_pages")) or 1
        current_page = self._safe_int(data.get("current_page")) or page
        total_products = self._safe_int(data.get("count")) or len(products)
        return {
            "__tuni_optique_listing__": True,
            "category_id": category_id,
            "url": self.build_page_url(url, current_page),
            "page": current_page,
            "total_pages": max(1, total_pages),
            "total_products": total_products,
            "has_next": bool(data.get("next")) or current_page < max(1, total_pages),
            "products": products,
        }

    def _listing_payload_to_html(self, payload: Dict[str, Any]) -> str:
        cards = []
        for product in payload.get("products", []):
            if not isinstance(product, dict):
                continue
            product_id = clean_text(product.get("id")) or ""
            name = html_lib.escape(clean_text(product.get("name")) or "")
            url = html_lib.escape(self._product_url(product_id) or "")
            image = html_lib.escape((self._product_images(product) or [""])[0])
            price, old_price, discount = self._prices(product)
            availability, available = self._availability(product)
            cards.append(
                "\n".join(
                    [
                        f'<article class="tuni-optique-product-card" data-id-product="{html_lib.escape(product_id)}">',
                        f'  <a class="product-link" href="{url}"><span class="product-name">{name}</span></a>',
                        f'  <img class="product-image" src="{image}" alt="{name}"/>',
                        f'  <span class="product-price">{price if price is not None else ""}</span>',
                        f'  <span class="product-old-price">{old_price if old_price is not None else ""}</span>',
                        f'  <span class="product-discount">{discount if discount is not None else ""}</span>',
                        f'  <span class="product-availability" data-available="{str(available).lower() if available is not None else ""}">{html_lib.escape(availability or "")}</span>',
                        "</article>",
                    ]
                )
            )
        payload_json = html_lib.escape(json.dumps(payload, ensure_ascii=False), quote=False)
        return (
            "<!doctype html><html><body>"
            f'<script id="tuni-optique-listing-data" type="application/json">{payload_json}</script>'
            '<section id="tuni-optique-products">'
            + "\n".join(cards)
            + "</section></body></html>"
        )

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> Dict[str, Any]:
        if self._is_category_url(url):
            started = time.monotonic()
            try:
                payload = await self._fetch_listing_payload(url)
                html = self._listing_payload_to_html(payload)
                if payload.get("products"):
                    save_text_atomic(html, self.html_dir / "listing_sample_1.html", self.logger)
                return {
                    "html": html,
                    "status_code": 200,
                    "final_url": url,
                    "content_type": "text/html; charset=utf-8",
                    "content_encoding": None,
                    "attempts": 1,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "blocked_signals": [],
                    "error": None,
                }
            except Exception as exc:
                if raise_on_error:
                    raise
                return {
                    "html": None,
                    "status_code": None,
                    "final_url": url,
                    "content_type": None,
                    "content_encoding": None,
                    "attempts": 1,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "blocked_signals": [],
                    "error": str(exc) or exc.__class__.__name__,
                }
        return await super().fetch_html_with_meta(url, raise_on_error=raise_on_error)

    def _listing_data_from_html(self, html: str) -> Dict[str, Any]:
        data = self._json_payload(html)
        if isinstance(data, dict) and data.get("__tuni_optique_listing__"):
            return data
        tree = HTMLParser(html or "")
        node = tree.css_first("#tuni-optique-listing-data")
        if node:
            data = self._json_payload(html_lib.unescape(node.text()))
            return data if isinstance(data, dict) else {}
        return {}

    def extract_products_from_html(self, html: str) -> List[dict]:
        listing = self._listing_data_from_html(html)
        if listing.get("products"):
            products = [
                self._record_from_product(item)
                for item in listing.get("products", [])
                if isinstance(item, dict)
            ]
            return dedupe_products([p for p in products if p and p.get("url")], self.logger, "tuni-optique api listing")

        tree = HTMLParser(html or "")
        products: List[dict] = []
        for card in tree.css("article.tuni-optique-product-card"):
            product_id = self._attr(card, "data-id-product")
            link = card.css_first("a.product-link[href]")
            url = self._same_host_url(self._attr(link, "href")) if link else self._product_url(product_id)
            name = self._text(card.css_first(".product-name"))
            price = parse_price(self._text(card.css_first(".product-price")))
            old_price = parse_price(self._text(card.css_first(".product-old-price")))
            discount = parse_price(self._text(card.css_first(".product-discount")))
            image = self._image_url(self._attr(card.css_first("img.product-image"), "src"))
            availability_node = card.css_first(".product-availability")
            availability, available = availability_from_text(self._text(availability_node))
            attr_available = self._attr(availability_node, "data-available")
            if available is None and attr_available in {"true", "false"}:
                available = attr_available == "true"
            products.append(
                finalize_product_record(
                    {
                        "id": product_id,
                        "product_id": product_id,
                        "url": url,
                        "name": name,
                        "title": name,
                        "shop": self.site_name,
                        "price": price,
                        "old_price": old_price,
                        "discount_percent": discount or self._computed_discount(price, old_price),
                        "image": image,
                        "images": [image] if image else [],
                        "availability": availability,
                        "available": available,
                    }
                )
            )
        return dedupe_products([p for p in products if p.get("url")], self.logger, "tuni-optique synthetic listing")

    def extract_pagination_from_html(self, html: str) -> dict:
        listing = self._listing_data_from_html(html)
        if listing:
            current = self._safe_int(listing.get("page")) or 1
            total_pages = max(1, self._safe_int(listing.get("total_pages")) or 1)
            total_products = self._safe_int(listing.get("total_products")) or len(listing.get("products") or [])
            return {
                "current_page": current,
                "total_pages": total_pages,
                "has_next": bool(listing.get("has_next")) or current < total_pages,
                "total_products": total_products,
            }
        return {"current_page": 1, "total_pages": 1, "has_next": False, "total_products": 0}

    def build_page_url(self, base_url: str, page_num: int) -> str:
        page = max(1, int(page_num or 1))
        parts = urlsplit(base_url)
        pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "page"]
        if page > 1:
            pairs.append(("page", str(page)))
        query = urlencode(pairs, doseq=True)
        path = parts.path or "/"
        return urlunsplit((parts.scheme or "https", parts.netloc or urlsplit(self.base_url).netloc, path, query, ""))

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        all_products: List[dict] = []
        page = 1
        while page <= self.max_pages:
            result = await self.scrape_category_page(self.build_page_url(category_url, page))
            if result.get("error"):
                break
            products = result.get("products") or []
            if not products and page > 1:
                break
            all_products.extend(products)
            deduped = dedupe_products(all_products, self.logger, "tuni-optique category")
            if limit and len(deduped) >= limit:
                return deduped[:limit]
            pagination = result.get("pagination") or {}
            total_pages = self._safe_int(pagination.get("total_pages")) or 1
            if not pagination.get("has_next") or page >= total_pages:
                break
            page += 1
        deduped = dedupe_products(all_products, self.logger, "tuni-optique category")
        return deduped[:limit] if limit else deduped

    # ------------------------------------------------------------------
    # Detail extraction
    # ------------------------------------------------------------------

    async def _fetch_product_detail(self, product_id: str) -> Optional[Dict[str, Any]]:
        data = await self._fetch_json_url(self._api_url("product_detail", product_id=product_id))
        if isinstance(data, dict) and clean_text(data.get("id")):
            self._product_cache[clean_text(data.get("id"))] = data
            return data
        return None

    @staticmethod
    def _extract_balanced_json_after(html: str, marker: str) -> Optional[Dict[str, Any]]:
        idx = html.find(marker)
        if idx < 0:
            return None
        start = html.find("{", idx)
        if start < 0:
            return None
        depth = 0
        in_string: Optional[str] = None
        escape = False
        for pos in range(start, len(html)):
            char = html[pos]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == in_string:
                    in_string = None
                continue
            if char in {'"', "'"}:
                in_string = char
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    raw = html[start : pos + 1]
                    try:
                        return json.loads(html_lib.unescape(raw))
                    except json.JSONDecodeError:
                        return None
        return None

    def _product_from_html(self, html: str) -> Optional[Dict[str, Any]]:
        for marker in ("this.product =", "this.product="):
            data = self._extract_balanced_json_after(html, marker)
            if isinstance(data, dict):
                return data
        return None

    def _detail_fallbacks_from_html(self, html: str, url: str) -> Dict[str, Any]:
        tree = HTMLParser(html or "")
        meta = html_product_metadata(html, product_url=url, base_url=self.base_url)

        title = self._text(tree.css_first("h1"))
        if title:
            meta.setdefault("title", title)
            meta.setdefault("name", title)

        images = self._dedupe_values(
            self._image_url(img.attributes.get("src") or img.attributes.get("data-src"))
            for img in tree.css("img[src*='cloudtiktak.com/media/static/media'], img[data-src*='cloudtiktak.com/media/static/media']")
        )
        if images:
            meta.setdefault("images", images)
            meta.setdefault("image", images[0])
        return meta

    async def scrape_product_details(self, url: str) -> dict:
        product_id = self._product_id_from_url(url)
        if not product_id:
            return {"url": url, "error": "Missing product id"}

        public_url = self._product_url(product_id) or url
        html = await self._fetch_plain_html(public_url)
        if html and not (self.html_dir / "detail_sample_1.html").exists():
            save_text_atomic(html, self.html_dir / "detail_sample_1.html", self.logger)

        product = await self._fetch_product_detail(product_id)
        if not product and html:
            product = self._product_from_html(html)
        if not product:
            product = self._product_cache.get(product_id)

        metadata = self._detail_fallbacks_from_html(html, public_url) if html else {}
        record: Dict[str, Any] = {}
        if product:
            source_category = self._category_from_product(product)
            if source_category and clean_text(source_category.get("id")):
                self._category_map[clean_text(source_category.get("id"))] = source_category
            built = self._record_from_product(product, include_description=True, page_metadata=metadata)
            if built:
                record.update(built)

        for key, value in metadata.items():
            if value not in (None, "", [], {}) and key not in record:
                record[key] = value

        record.setdefault("id", product_id)
        record.setdefault("product_id", product_id)
        record.setdefault("url", public_url)
        record.setdefault("shop", self.site_name)
        record.setdefault("scraped_at", datetime.now().isoformat())
        return {
            key: value
            for key, value in finalize_product_record(record).items()
            if value is not None and value != "" and value != [] and value != {}
        }


def get_scraper(logger: logging.Logger) -> TuniOptiqueScraper:
    return TuniOptiqueScraper(logger)
