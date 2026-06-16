#!/usr/bin/env python3
"""
TechnoWatch scraper - Converty storefront, HTTP/API selectolax bridge.
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
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, get_date_folder, save_json, save_text_atomic
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    clean_text,
    dedupe_products,
    extract_gtins_from_text,
    finalize_product_record,
    html_product_metadata,
    normalize_url,
    parse_price,
)


class TechnoWatchScraper(FastScraper):
    """HTTP/API scraper for the Technowatch Converty storefront."""

    PAYLOAD_SELECTOR = "script#techno-watch-listing-data"
    CATEGORY_BY_SLUG = {
        "vetements": {
            "name": "Montres Homme",
            "category_id": "6976325f94e1d170e96e2ed6",
        },
        "sacs": {
            "name": "Montres Femme",
            "category_id": "6976325f94e1d170e96e2ed2",
        },
    }
    CATEGORY_BY_ID = {
        value["category_id"]: {"slug": slug, **value}
        for slug, value in CATEGORY_BY_SLUG.items()
    }

    def __init__(self, logger: logging.Logger):
        super().__init__("techno-watch", logger)
        self.headers.update(self.config.get("headers", {}))
        self.api_base_url = self.config.get(
            "api_base_url",
            "https://technowatch-tn.converty.shop/api/v1",
        ).rstrip("/")
        settings = self.config.get("settings", {})
        self.page_size = int(settings.get("page_size", 10))
        self.max_pages = int(settings.get("max_pages", 100))

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _abs(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

    def _api_url(self, endpoint: str) -> str:
        return f"{self.api_base_url}/{endpoint.lstrip('/')}"

    async def _api_get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        client = await self.get_client()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base_url.rstrip("/"),
            "Referer": self.base_url,
        }
        response = await client.get(self._api_url(endpoint), headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _text(node: Any) -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=" ", strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

    @staticmethod
    def _dedupe_urls(urls: Iterable[Any]) -> List[str]:
        seen = set()
        out = []
        for url in urls:
            text = clean_text(url)
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out

    @staticmethod
    def _html_to_text(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if "<" not in text or ">" not in text:
            return text
        try:
            return clean_text(HTMLParser(f"<div>{text}</div>").text(separator=" ", strip=True))
        except Exception:
            return clean_text(re.sub(r"<[^>]+>", " ", text))

    def _category_from_url(self, url: str) -> Optional[Dict[str, str]]:
        parsed = urlparse(url)
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) < 2 or path_parts[0].lower() != "category":
            return None
        slug = path_parts[1].lower()
        category = self.CATEGORY_BY_SLUG.get(slug)
        if not category:
            return None
        return {"slug": slug, **category}

    def _page_from_url(self, url: str) -> int:
        raw = parse_qs(urlparse(url).query).get("page", ["1"])[0]
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _product_url(self, value: Any) -> Optional[str]:
        slug = clean_text(value)
        if not slug:
            return None
        if slug.startswith(("http://", "https://")):
            return normalize_url(slug) or slug
        return normalize_url(urljoin(self.base_url, f"/product/{slug.strip('/')}"))

    def _slug_from_url(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0].lower() == "product":
            return clean_text(parts[1])
        return None

    def _category_url(self, slug: str) -> str:
        return urljoin(self.base_url, f"/category/{slug}")

    @staticmethod
    def _numeric_reference(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        return text

    @staticmethod
    def _discount_percent(price: Optional[float], old_price: Optional[float]) -> Optional[int]:
        if price is None or old_price is None or old_price <= 0 or old_price <= price:
            return None
        return int(round((1 - (price / old_price)) * 100))

    def _image_urls(self, product: Dict[str, Any]) -> List[str]:
        urls: List[str] = []
        for image in product.get("images") or []:
            if isinstance(image, dict):
                for key in ("lg", "md", "sm", "url", "src"):
                    url = self._abs(image.get(key))
                    if url:
                        urls.append(url)
                        break
            else:
                url = self._abs(image)
                if url:
                    urls.append(url)
        return self._dedupe_urls(urls)

    def _stock_status(self, product: Dict[str, Any]) -> Tuple[Optional[str], Optional[bool]]:
        new_stock = product.get("newStock") if isinstance(product.get("newStock"), dict) else {}
        out_of_stock = product.get("outOfStock")
        if out_of_stock is None:
            out_of_stock = new_stock.get("outOfStock")
        if out_of_stock is True:
            return "Rupture de stock", False
        if out_of_stock is False:
            return "En stock", True
        status = clean_text(product.get("status"))
        if status and status.lower() not in {"shown", "active", "published"}:
            return status, False
        if product.get("price") is not None:
            return "En stock", True
        return availability_from_text(status)

    def _description_lines(self, description: Any) -> List[str]:
        if not description:
            return []
        fragment = HTMLParser(f"<div>{description}</div>")
        lines: List[str] = []
        for node in fragment.css("p, li"):
            text = self._text(node)
            if text and text not in lines:
                lines.append(text)
        if not lines:
            text = self._html_to_text(description)
            if text:
                lines.append(text)
        return lines

    def _specs_from_description(self, description: Any) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for line in self._description_lines(description):
            match = re.search(r"marque\s*:\s*(.+)$", line, re.I)
            if match:
                specs["Marque"] = clean_text(match.group(1))
                continue
            match = re.match(r"([^:]{2,80})\s*:\s*(.+)$", line)
            if not match:
                continue
            label = clean_text(match.group(1).strip(" ."))
            value = clean_text(match.group(2).strip(" ."))
            if not label or not value:
                continue
            if re.search(r"r[e\u00e9]f[e\u00e9]rence|reference", label, re.I):
                label = "Reference"
            specs[label] = value
        return specs

    def _brand_and_reference(self, product: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Dict[str, str]]:
        specs = self._specs_from_description(product.get("description"))
        brand = clean_text(specs.get("Marque"))
        reference = None
        for key, value in specs.items():
            if re.search(r"r[e\u00e9]f[e\u00e9]rence|reference", key, re.I):
                reference = clean_text(value)
                break
        return brand, reference, specs

    def _category_records(self, category_ids: Iterable[Any]) -> List[Dict[str, str]]:
        records = []
        for raw_id in category_ids or []:
            category_id = clean_text(raw_id)
            category = self.CATEGORY_BY_ID.get(category_id)
            if not category:
                continue
            records.append(
                {
                    "name": category["name"],
                    "url": self._category_url(category["slug"]),
                    "category_id": category_id,
                }
            )
        return records

    def _variants(self, product: Dict[str, Any]) -> List[Dict[str, Any]]:
        variants: List[Dict[str, Any]] = []
        for variant in product.get("variants") or []:
            if isinstance(variant, dict):
                variants.append(variant)
        for variant in product.get("newVariants") or []:
            if isinstance(variant, dict):
                variants.append(variant)
        return variants

    def _product_from_api(self, product: Dict[str, Any]) -> Dict[str, Any]:
        price = parse_price(product.get("price"))
        old_price = parse_price(product.get("comparePrice"))
        discount = self._discount_percent(price, old_price)
        images = self._image_urls(product)
        availability, available = self._stock_status(product)
        brand, reference, specs = self._brand_and_reference(product)
        product_id = clean_text(product.get("_id"))
        url = self._product_url(product.get("slug"))
        description_text = self._html_to_text(product.get("description"))
        categories = self._category_records(product.get("categories") or [])

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": url,
            "name": clean_text(product.get("name")),
            "title": clean_text(product.get("name")),
            "price": price,
            "old_price": old_price,
            "discount_percent": discount,
            "image": images[0] if images else None,
            "images": images,
            "brand": brand,
            "reference": reference,
            "sku": reference,
            "platform_reference": self._numeric_reference(product.get("reference")),
            "availability": availability,
            "available": available,
            "description": description_text,
            "full_description": description_text,
            "description_html": clean_text(product.get("description")),
            "specifications": specs,
            "specs": specs,
            "categories": categories,
            "breadcrumbs": categories,
            "status": clean_text(product.get("status")),
            "variants": self._variants(product),
            "options": product.get("options") or None,
            "new_stock": product.get("newStock") if isinstance(product.get("newStock"), dict) else None,
        }

        gtins = extract_gtins_from_text(
            " ".join(
                str(value or "")
                for value in (
                    product.get("description"),
                    product.get("extraFields"),
                    product.get("integrationValues"),
                )
            )
        )
        if gtins:
            record["barcode"] = gtins[0]

        return finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})

    # ------------------------------------------------------------------
    # Frontpage/category discovery
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        output_path = await super().download_frontpage()
        try:
            payload = await self._api_get("/categories", {"page": 1, "limit": 20})
            save_text_atomic(
                json.dumps(payload, ensure_ascii=False, indent=2),
                self.html_dir / "categories_api.json",
                self.logger,
            )
        except Exception as exc:
            self.logger.debug(f"TechnoWatch categories API evidence fetch failed: {exc}")
        return output_path

    def extract_categories_from_html(self, html: str) -> dict:
        api_categories = {}
        api_path = self.html_dir / "categories_api.json"
        if api_path.exists():
            try:
                payload = json.loads(api_path.read_text(encoding="utf-8"))
                for item in payload.get("data") or []:
                    if isinstance(item, dict):
                        api_categories[clean_text(item.get("_id"))] = item
            except Exception:
                api_categories = {}

        configured = self.config.get("fixed_categories") or []
        categories = []
        seen = set()
        for item in configured:
            slug = clean_text(item.get("slug"))
            category_id = clean_text(item.get("category_id"))
            if slug not in self.CATEGORY_BY_SLUG or category_id not in self.CATEGORY_BY_ID:
                continue
            if category_id in seen:
                continue
            seen.add(category_id)
            api_item = api_categories.get(category_id) or {}
            categories.append(
                {
                    "name": clean_text(api_item.get("name")) or clean_text(item.get("name")) or self.CATEGORY_BY_SLUG[slug]["name"],
                    "url": self._category_url(slug),
                    "category_id": category_id,
                    "slug": slug,
                    "low_level_categories": [],
                    "discovery_method": "fixed_converty_category",
                    "validated_by_api": bool(api_item),
                    "product_count_hint": api_item.get("count"),
                }
            )

        return {
            "categories": categories,
            "stats": {
                "top_level": len(categories),
                "low_level": 0,
                "subcategory": 0,
                "total_urls": len(categories),
            },
        }

    async def scrape_categories_async(self) -> dict:
        await self.download_frontpage()
        data = self.extract_categories_from_html("")
        data["site"] = self.site_name
        data["shop"] = self.site_name
        data["base_url"] = self.base_url
        data["extracted_at"] = datetime.now().isoformat()
        data["date"] = get_date_folder()
        save_json(data, self.data_dir / "categories.json", self.logger)
        return data

    # ------------------------------------------------------------------
    # Listing API bridge
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parsed = urlparse(base_url)
        params = parse_qs(parsed.query)
        if page_num <= 1:
            params.pop("page", None)
        else:
            params["page"] = [str(page_num)]
        query = urlencode({key: values[-1] for key, values in params.items()})
        return urlunparse(parsed._replace(query=query, fragment=""))

    async def _fetch_listing_payload(self, url: str) -> Dict[str, Any]:
        category = self._category_from_url(url)
        page = self._page_from_url(url)
        if not category:
            return {
                "__techno_watch_listing__": True,
                "url": url,
                "page": page,
                "page_size": self.page_size,
                "count": 0,
                "data": [],
                "error": "not_category_url",
            }
        try:
            payload = await self._api_get(
                "/products",
                {
                    "page": page,
                    "limit": self.page_size,
                    "categoryId": category["category_id"],
                },
            )
            if not isinstance(payload, dict):
                payload = {}
            payload["__techno_watch_listing__"] = True
            payload["url"] = url
            payload["page"] = page
            payload["page_size"] = self.page_size
            payload["category"] = category
            payload["error"] = None
            return payload
        except Exception as exc:
            return {
                "__techno_watch_listing__": True,
                "url": url,
                "page": page,
                "page_size": self.page_size,
                "count": 0,
                "data": [],
                "category": category,
                "error": str(exc) or exc.__class__.__name__,
            }

    def _listing_payload_to_html(self, payload: Dict[str, Any]) -> str:
        cards = []
        for product in payload.get("data") or []:
            if not isinstance(product, dict):
                continue
            record = self._product_from_api(product)
            name = html_lib.escape(record.get("name") or "")
            url = html_lib.escape(record.get("url") or "")
            image = html_lib.escape(record.get("image") or "")
            cards.append(
                "\n".join(
                    [
                        f'<article class="product-card" data-id-product="{html_lib.escape(record.get("product_id") or "")}">',
                        f'  <a class="product-link" href="{url}"><img class="product-image" src="{image}" alt="{name}"></a>',
                        f'  <h2 class="product-title">{name}</h2>',
                        f'  <span class="price">{record.get("price", "")}</span>',
                        f'  <span class="old-price">{record.get("old_price", "")}</span>',
                        f'  <span class="discount">{record.get("discount_percent", "")}</span>',
                        f'  <span class="availability" data-available="{str(record.get("available")).lower()}">{html_lib.escape(record.get("availability") or "")}</span>',
                        "</article>",
                    ]
                )
            )

        payload_json = html_lib.escape(json.dumps(payload, ensure_ascii=False), quote=False)
        return (
            "<!doctype html><html><body>"
            f'<script id="techno-watch-listing-data" type="application/json">{payload_json}</script>'
            '<section id="techno-watch-products">'
            + "\n".join(cards)
            + "</section></body></html>"
        )

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> Dict[str, Any]:
        if self._category_from_url(url):
            started = time.monotonic()
            payload = await self._fetch_listing_payload(url)
            html = self._listing_payload_to_html(payload)
            if payload.get("data") and not (self.html_dir / "listing_sample_1.html").exists():
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

    def _listing_data_from_html(self, html: str) -> Dict[str, Any]:
        try:
            data = json.loads(html)
            if isinstance(data, dict) and data.get("__techno_watch_listing__"):
                return data
        except Exception:
            pass

        tree = HTMLParser(html)
        node = tree.css_first(self.PAYLOAD_SELECTOR)
        if not node:
            return {}
        try:
            return json.loads(html_lib.unescape(node.text()))
        except Exception:
            return {}

    def extract_products_from_html(self, html: str) -> List[dict]:
        listing = self._listing_data_from_html(html)
        if listing:
            products = [
                self._product_from_api(product)
                for product in listing.get("data", [])
                if isinstance(product, dict)
            ]
            return dedupe_products(products, self.logger, "techno-watch listing")

        tree = HTMLParser(html)
        products: List[Dict[str, Any]] = []
        for card in tree.css("article.product-card"):
            link = card.css_first("a.product-link[href]")
            image = card.css_first("img.product-image")
            availability_node = card.css_first(".availability")
            availability, available = availability_from_text(self._text(availability_node))
            products.append(
                finalize_product_record(
                    {
                        "id": clean_text(card.attributes.get("data-id-product")),
                        "product_id": clean_text(card.attributes.get("data-id-product")),
                        "url": normalize_url(link.attributes.get("href") if link else None),
                        "name": self._text(card.css_first(".product-title")),
                        "price": parse_price(self._text(card.css_first(".price"))),
                        "old_price": parse_price(self._text(card.css_first(".old-price"))),
                        "discount_percent": parse_price(self._text(card.css_first(".discount"))),
                        "image": self._abs(image.attributes.get("src") if image else None),
                        "availability": availability,
                        "available": available,
                    }
                )
            )
        return dedupe_products([p for p in products if p.get("url")], self.logger, "techno-watch listing fallback")

    def extract_pagination_from_html(self, html: str) -> dict:
        listing = self._listing_data_from_html(html)
        if listing:
            current = int(listing.get("page") or 1)
            page_size = int(listing.get("page_size") or self.page_size)
            count = int(listing.get("count") or 0)
            total_pages = max(1, math.ceil(count / page_size)) if page_size > 0 else 1
            return {
                "current_page": current,
                "total_pages": total_pages,
                "has_next": current < total_pages,
                "total_products": count,
                "method": "api_page",
            }
        return {"current_page": 1, "total_pages": 1, "has_next": False, "method": "none"}

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
                return dedupe_products(products[:limit], self.logger, "techno-watch limited listing")
            pagination = result.get("pagination") or {}
            if not pagination.get("has_next"):
                break
            page += 1
        deduped = dedupe_products(products, self.logger, "techno-watch category")
        return deduped[:limit] if limit else deduped

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def _fetch_product_payload(self, slug: str) -> Optional[Dict[str, Any]]:
        try:
            payload = await self._api_get(f"/product/{slug}")
            data = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(data, dict):
                return data
        except Exception as exc:
            self.logger.debug(f"TechnoWatch product API failed slug={slug}: {exc}")
        return None

    async def _fetch_product_html(self, slug: str) -> Optional[str]:
        url = self._product_url(slug)
        if not url:
            return None
        meta = await super().fetch_html_with_meta(url)
        return meta.get("html")

    def _product_data_from_html(self, html: str) -> Optional[Dict[str, Any]]:
        tree = HTMLParser(html)
        node = tree.css_first("script#productData")
        if node:
            try:
                data = json.loads(html_lib.unescape(node.text()))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return None

    async def scrape_product_details(self, url: str) -> dict:
        slug = self._slug_from_url(url)
        if not slug:
            return {"url": url, "error": "missing_product_slug"}

        product = await self._fetch_product_payload(slug)
        product_html = None

        detail_path = self.html_dir / "detail_sample_1.html"
        if not detail_path.exists() or product is None:
            product_html = await self._fetch_product_html(slug)
            if product_html and not detail_path.exists():
                save_text_atomic(product_html, detail_path, self.logger)
            if product is None and product_html:
                product = self._product_data_from_html(product_html)

        if not product:
            fallback = html_product_metadata(product_html or "", url, self.base_url)
            fallback["url"] = normalize_url(url) or url
            fallback["shop"] = self.site_name
            fallback["error"] = "product_not_found"
            return finalize_product_record(fallback)

        record = self._product_from_api(product)
        if product_html:
            meta = html_product_metadata(product_html, record.get("url") or url, self.base_url)
            for key, value in meta.items():
                if value not in (None, "", [], {}) and key not in record:
                    record[key] = value
        record["url"] = record.get("url") or normalize_url(url) or url
        record["shop"] = self.site_name
        return finalize_product_record(record)


def get_scraper(logger: logging.Logger) -> TechnoWatchScraper:
    """Factory used by scraper.sites registry."""
    return TechnoWatchScraper(logger)
