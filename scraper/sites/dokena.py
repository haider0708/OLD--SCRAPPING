#!/usr/bin/env python3
"""
Dokena.tn scraper - custom Next.js marketplace backed by JSON APIs.
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
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlunparse

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, get_date_folder, save_json, save_text_atomic
from scraper.product_utils import (
    absolute_url,
    clean_text,
    dedupe_products,
    finalize_product_record,
    html_product_metadata,
    normalize_gtin,
    normalize_url,
    parse_price,
)


class DokenaScraper(FastScraper):
    """HTTP/API scraper for Dokena."""

    PRODUCT_ID_RE = re.compile(r"/products/([0-9a-f]{24})(?:/?|[?#].*)$", re.I)
    CATEGORY_ID_RE = re.compile(r"/products/explore/(?:[^/]+/)?([0-9a-f]{24})(?:/?|[?#].*)$", re.I)
    _MIN_INTERVAL = 0.2

    def __init__(self, logger: logging.Logger):
        super().__init__("dokena", logger)
        self.api_base_url = self.config.get("api_base_url", "https://backend.dokena.tn").rstrip("/")
        self.page_size = int(self.config.get("settings", {}).get("page_size", 12))
        self.max_pages = int(self.config.get("settings", {}).get("max_pages", 200))
        self._api_sem = asyncio.Semaphore(6)
        self._last_api_request = 0.0

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @property
    def web_origin(self) -> str:
        parsed = urlparse(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _api_url(self, endpoint: str) -> str:
        return f"{self.api_base_url}/{endpoint.lstrip('/')}"

    def _clean_html(self, value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if "<" in text and ">" in text:
            tree = HTMLParser(f"<div>{text}</div>")
            text = tree.body.text(separator=" ", strip=True) if tree.body else re.sub(r"<[^>]+>", " ", text)
        return clean_text(html_lib.unescape(text))

    def _abs(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.web_origin)

    def _product_url(self, product_id: Any) -> Optional[str]:
        product_id = clean_text(product_id)
        if not product_id:
            return None
        return urljoin(self.base_url.rstrip("/") + "/", f"products/{product_id}")

    def _category_url(self, category: Dict[str, Any]) -> Optional[str]:
        category_id = clean_text(category.get("_id"))
        name = clean_text(category.get("name"))
        if not category_id or not name:
            return None
        encoded_name = quote(name, safe="")
        return urljoin(self.base_url.rstrip("/") + "/", f"products/explore/{encoded_name}/{category_id}")

    def _category_id_from_url(self, url: str) -> Optional[str]:
        path = urlparse(url).path
        match = self.CATEGORY_ID_RE.search(path)
        return match.group(1) if match else None

    def _product_id_from_url(self, url: str) -> Optional[str]:
        path = urlparse(url).path
        match = self.PRODUCT_ID_RE.search(path)
        return match.group(1) if match else None

    def _page_from_url(self, url: str) -> int:
        parsed = urlparse(url)
        raw = parse_qs(parsed.query).get("page", ["1"])[0]
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    async def _api_json(
        self,
        method: str,
        endpoint: str,
        *,
        json_payload: Optional[Dict[str, Any]] = None,
        expected_not_found: bool = False,
    ) -> Tuple[int, Any]:
        async with self._api_sem:
            now = time.monotonic()
            wait = self._MIN_INTERVAL - (now - self._last_api_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_api_request = time.monotonic()

            client = await self.get_client()
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": self.web_origin,
                "Referer": self.base_url,
            }
            url = self._api_url(endpoint)
            response = await client.request(method, url, headers=headers, json=json_payload)
            if response.status_code == 404 and expected_not_found:
                try:
                    return response.status_code, response.json()
                except Exception:
                    return response.status_code, {}
            response.raise_for_status()
            return response.status_code, response.json()

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        output_path = self.html_dir / "frontpage.html"
        html = await super().fetch_html(self.base_url, raise_on_error=True)
        save_text_atomic(html, output_path, self.logger)
        return output_path

    async def _fetch_categories(self) -> List[Dict[str, Any]]:
        endpoint = self.config.get("endpoints", {}).get("categories", "/shop_category/getAllCategorys?lang=en")
        _, data = await self._api_json("GET", endpoint)
        return data if isinstance(data, list) else []

    def _build_categories_data(self, categories_api: List[Dict[str, Any]]) -> Dict[str, Any]:
        categories = []
        for item in categories_api:
            url = self._category_url(item)
            name = clean_text(item.get("name"))
            category_id = clean_text(item.get("_id"))
            if not url or not name or not category_id:
                continue
            categories.append(
                {
                    "name": name,
                    "url": url,
                    "level": "top",
                    "category_id": category_id,
                    "description": clean_text(item.get("description")),
                    "image": self._abs(item.get("horizontalImage") or item.get("verticalImage")),
                    "low_level_categories": [],
                }
            )

        stats = {
            "top_level": len(categories),
            "low_level": 0,
            "subcategory": 0,
            "total_urls": len(categories),
        }
        return {"categories": categories, "stats": stats}

    def extract_categories_from_html(self, html: str) -> dict:
        """Fallback parser for saved API category JSON or visible category links."""
        try:
            data = json.loads(html)
            if isinstance(data, list):
                return self._build_categories_data(data)
        except Exception:
            pass

        tree = HTMLParser(html)
        categories = []
        seen = set()
        for node in tree.css("a[href*='/products/explore/']"):
            href = self._abs(node.attributes.get("href"), self.web_origin)
            category_id = self._category_id_from_url(href or "")
            name = clean_text(node.text(strip=True))
            if not href or not category_id or not name or category_id in seen:
                continue
            seen.add(category_id)
            categories.append(
                {
                    "name": name,
                    "url": href,
                    "level": "top",
                    "category_id": category_id,
                    "low_level_categories": [],
                }
            )

        stats = {
            "top_level": len(categories),
            "low_level": 0,
            "subcategory": 0,
            "total_urls": len(categories),
        }
        return {"categories": categories, "stats": stats}

    async def scrape_categories_async(self) -> dict:
        categories_api = await self._fetch_categories()
        save_text_atomic(
            json.dumps(categories_api, ensure_ascii=False, indent=2),
            self.html_dir / "categories_api.json",
            self.logger,
        )
        data = self._build_categories_data(categories_api)
        data["site"] = self.site_name
        data["shop"] = self.site_name
        data["base_url"] = self.base_url
        data["extracted_at"] = datetime.now().isoformat()
        data["date"] = get_date_folder()

        output_path = self.data_dir / "categories.json"
        save_json(data, output_path, self.logger)
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
        return urlunparse(parsed._replace(query=query))

    async def _fetch_listing_payload(self, url: str) -> Dict[str, Any]:
        category_id = self._category_id_from_url(url)
        page_num = self._page_from_url(url)
        skip = (page_num - 1) * self.page_size
        payload = {"shopCategory": category_id, "limit": self.page_size, "skip": skip}
        endpoint = self.config.get("endpoints", {}).get("products", "/product/getProducts")
        status, data = await self._api_json(
            "POST",
            endpoint,
            json_payload=payload,
            expected_not_found=True,
        )
        products = []
        error = None
        if isinstance(data, dict):
            products = data.get("products") or []
            error = data.get("message") or data.get("error")
        if status == 404:
            products = []
        return {
            "__dokena_listing__": True,
            "url": url,
            "category_id": category_id,
            "page": page_num,
            "limit": self.page_size,
            "skip": skip,
            "has_next": len(products) >= self.page_size,
            "error": error,
            "products": products,
        }

    def _listing_payload_to_html(self, payload: Dict[str, Any]) -> str:
        product_cards = []
        for product in payload.get("products", []):
            product_id = clean_text(product.get("_id")) or ""
            url = self._product_url(product_id) or ""
            name = html_lib.escape(clean_text(product.get("name")) or "")
            image = html_lib.escape(self._first_image(product) or "")
            price, old_price, discount_percent = self._prices(product)
            availability_text, available = self._availability(product)
            product_cards.append(
                "\n".join(
                    [
                        f'<article data-id-product="{html_lib.escape(product_id)}">',
                        f'  <a class="product-link" href="{html_lib.escape(url)}"><span class="product-name">{name}</span></a>',
                        f'  <img class="product-image" src="{image}" alt="{name}"/>',
                        f'  <span class="product-price">{price if price is not None else ""}</span>',
                        f'  <span class="product-old-price">{old_price if old_price is not None else ""}</span>',
                        f'  <span class="product-discount">{discount_percent if discount_percent is not None else ""}</span>',
                        f'  <span class="product-availability" data-available="{str(bool(available)).lower()}">{html_lib.escape(availability_text or "")}</span>',
                        "</article>",
                    ]
                )
            )

        payload_json = html_lib.escape(json.dumps(payload, ensure_ascii=False), quote=False)
        return (
            "<!doctype html><html><body>"
            f'<script id="dokena-listing-data" type="application/json">{payload_json}</script>'
            '<section id="dokena-products">'
            + "\n".join(product_cards)
            + "</section></body></html>"
        )

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> Dict[str, Any]:
        if self._category_id_from_url(url):
            started = time.monotonic()
            try:
                payload = await self._fetch_listing_payload(url)
                html = self._listing_payload_to_html(payload)
                if payload.get("products") and not (self.html_dir / "listing_sample_1.html").exists():
                    save_text_atomic(html, self.html_dir / "listing_sample_1.html", self.logger)
                return {
                    "html": html,
                    "status_code": 200 if not payload.get("error") else 404,
                    "final_url": url,
                    "content_type": "text/html; charset=utf-8",
                    "content_encoding": None,
                    "attempts": 1,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "blocked_signals": [],
                    "error": None if not payload.get("error") else payload.get("error"),
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
        try:
            data = json.loads(html)
            if isinstance(data, dict) and data.get("__dokena_listing__"):
                return data
        except Exception:
            pass

        tree = HTMLParser(html)
        node = tree.css_first("#dokena-listing-data")
        if node:
            try:
                return json.loads(html_lib.unescape(node.text()))
            except Exception:
                return {}
        return {}

    def _variant_prices(self, product: Dict[str, Any]) -> List[Tuple[float, Optional[str], Optional[bool], Optional[Any]]]:
        other = product.get("otherPricing")
        if not isinstance(other, dict):
            return []
        prices = other.get("price") or []
        params = other.get("params") or []
        availabilities = other.get("availability") or []
        stocks = other.get("stock") or []
        out: List[Tuple[float, Optional[str], Optional[bool], Optional[Any]]] = []
        for index, raw_price in enumerate(prices):
            price = parse_price(raw_price)
            if price is None:
                continue
            raw_available = availabilities[index] if index < len(availabilities) else None
            available = None
            if isinstance(raw_available, bool):
                available = raw_available
            elif isinstance(raw_available, str):
                available = raw_available.lower() == "true"
            stock = stocks[index] if index < len(stocks) else None
            param = clean_text(params[index]) if index < len(params) else None
            out.append((price, param, available, stock))
        return out

    def _selected_variant_price(self, product: Dict[str, Any]) -> Optional[float]:
        variants = self._variant_prices(product)
        available_prices = [price for price, _, available, _ in variants if available is not False]
        if available_prices:
            return min(available_prices)
        if variants:
            return min(price for price, _, _, _ in variants)
        return None

    def _prices(self, product: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        base_price = parse_price(product.get("price")) or 0.0
        variant_price = self._selected_variant_price(product)
        if variant_price is not None:
            original = base_price + variant_price
        else:
            original = parse_price(product.get("sortingPrice"))
            if original is None:
                original = base_price if base_price > 0 else None

        discount_percent = parse_price(product.get("discount"))
        if discount_percent is not None and discount_percent <= 0:
            discount_percent = None

        if original is None:
            return None, None, discount_percent
        if discount_percent:
            current = round(original - (original * discount_percent / 100), 2)
            return current, original, discount_percent
        return original, None, None

    def _availability(self, product: Dict[str, Any]) -> Tuple[str, Optional[bool]]:
        made_to_order = bool(product.get("made_to_order"))
        raw_available = product.get("availability")
        variants = self._variant_prices(product)
        variant_available = [available for _, _, available, _ in variants if available is not None]
        available = bool(raw_available) or made_to_order
        if variant_available:
            available = available and any(variant_available)
        stock = product.get("stock")
        if isinstance(stock, (int, float)) and stock <= 0 and not made_to_order:
            available = False
        if made_to_order:
            return "Made to order", True
        return ("In stock" if available else "Out of stock"), available

    def _first_image(self, product: Dict[str, Any]) -> Optional[str]:
        gallery = product.get("gallery")
        if isinstance(gallery, list):
            for image in gallery:
                url = self._abs(image)
                if url:
                    return url
        return self._abs(product.get("image"))

    def _product_from_api(self, product: Dict[str, Any]) -> Dict[str, Any]:
        product_id = clean_text(product.get("_id") or product.get("id"))
        price, old_price, discount_percent = self._prices(product)
        availability_text, available = self._availability(product)
        url = self._product_url(product_id)
        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": normalize_url(url) or url,
            "name": clean_text(product.get("name")),
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": self._first_image(product),
            "availability": availability_text,
            "available": available,
            "short_description": self._clean_html(product.get("description")),
            "shop_category_id": clean_text(product.get("shopCategory")),
            "product_category_id": clean_text(product.get("productCategory")),
            "in_shop_subcategory_id": clean_text(product.get("InShopSubCategory")),
            "gender": clean_text(product.get("gender")),
            "rating": product.get("rating"),
            "stock": product.get("stock"),
            "made_to_order": product.get("made_to_order"),
        }

        for key in ("sku", "reference", "barcode", "brand"):
            value = clean_text(product.get(key))
            if value:
                record[key] = value

        if normalize_gtin(record.get("reference")):
            record["barcode"] = record["reference"]
            record.pop("reference", None)

        variants = self._variants_summary(product)
        if variants:
            record["variants"] = variants

        return finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})

    def _variants_summary(self, product: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        other = product.get("otherPricing")
        if not isinstance(other, dict):
            return None
        variant_type = clean_text(other.get("name"))
        variants = []
        for price, param, available, stock in self._variant_prices(product):
            row = {"price": price}
            if variant_type:
                row["type"] = variant_type
            if param:
                row["value"] = param
            if available is not None:
                row["available"] = available
            if stock is not None:
                row["stock"] = stock
            variants.append(row)
        return variants or None

    def extract_products_from_html(self, html: str) -> List[dict]:
        listing = self._listing_data_from_html(html)
        if listing:
            products = [self._product_from_api(p) for p in listing.get("products", []) if isinstance(p, dict)]
            return dedupe_products(products, self.logger, "dokena listing")

        tree = HTMLParser(html)
        products = []
        for item in tree.css("article[data-id-product]"):
            product_id = clean_text(item.attributes.get("data-id-product"))
            link = item.css_first("a.product-link")
            url = self._abs(link.attributes.get("href") if link else None, self.web_origin)
            name = clean_text(item.css_first(".product-name").text(strip=True) if item.css_first(".product-name") else None)
            price = parse_price(item.css_first(".product-price").text(strip=True) if item.css_first(".product-price") else None)
            old_price = parse_price(item.css_first(".product-old-price").text(strip=True) if item.css_first(".product-old-price") else None)
            img = item.css_first("img.product-image")
            availability_node = item.css_first(".product-availability")
            available = None
            if availability_node:
                raw = availability_node.attributes.get("data-available")
                if raw in {"true", "false"}:
                    available = raw == "true"
            products.append(
                finalize_product_record(
                    {
                        "id": product_id,
                        "product_id": product_id,
                        "url": normalize_url(url) or url,
                        "name": name,
                        "price": price,
                        "old_price": old_price,
                        "image": self._abs(img.attributes.get("src") if img else None),
                        "availability": clean_text(availability_node.text(strip=True) if availability_node else None),
                        "available": available,
                    }
                )
            )
        return dedupe_products(products, self.logger, "dokena listing fallback")

    def extract_pagination_from_html(self, html: str) -> dict:
        listing = self._listing_data_from_html(html)
        if listing:
            current = int(listing.get("page") or 1)
            has_next = bool(listing.get("has_next"))
            return {
                "current_page": current,
                "total_pages": current + 1 if has_next else current,
                "has_next": has_next,
            }
        return {"current_page": 1, "total_pages": 1, "has_next": False}

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        all_products: List[dict] = []
        page = 1
        while page <= self.max_pages:
            page_url = self.build_page_url(category_url, page)
            result = await self.scrape_category_page(page_url)
            products = result.get("products") or []
            if not products:
                break
            all_products.extend(products)
            if limit and len(all_products) >= limit:
                return all_products[:limit]
            pagination = result.get("pagination") or {}
            if not pagination.get("has_next"):
                break
            page += 1
        return all_products[:limit] if limit else dedupe_products(all_products, self.logger, "dokena category")

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    def _decode_next_flight(self, html: str) -> str:
        tree = HTMLParser(html)
        chunks = []
        for script in tree.css("script"):
            raw = script.text() or ""
            if "self.__next_f.push" not in raw:
                continue
            match = re.search(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)\s*$', raw, re.S)
            if not match:
                continue
            try:
                chunks.append(json.loads(match.group(1)))
            except Exception:
                continue
        return "\n".join(chunks)

    def _balanced_object_from(self, text: str, search_start: int) -> Optional[str]:
        start = text.find("{", search_start)
        if start < 0:
            return None
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return None

    def _product_payload_from_html(self, html: str) -> Dict[str, Any]:
        decoded = self._decode_next_flight(html)
        for text in (decoded, html):
            marker = '"product":'
            marker_index = 0
            while True:
                marker_index = text.find(marker, marker_index)
                if marker_index < 0:
                    break
                raw = self._balanced_object_from(text, marker_index + len(marker))
                marker_index += len(marker)
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict) and data.get("_id"):
                        return data
                except Exception:
                    continue
        return {}

    def _images_from_html(self, tree: HTMLParser) -> List[str]:
        images = []
        for img in tree.css("img[src*='ShopProductImages'], .image-gallery img"):
            url = self._abs(img.attributes.get("src") or img.attributes.get("data-src"))
            if url and url not in images:
                images.append(url)
        return images

    def _specifications(self, product: Dict[str, Any]) -> Dict[str, Any]:
        specs: Dict[str, Any] = {}
        if product.get("gender"):
            specs["Gender"] = product.get("gender")
        if product.get("stock") is not None:
            specs["Stock"] = product.get("stock")
        if product.get("made_to_order") is not None:
            specs["Made to order"] = product.get("made_to_order")
        if product.get("estimated_delivery_date") not in (None, "", "-1"):
            specs["Estimated delivery date"] = product.get("estimated_delivery_date")
        if product.get("weight") not in (None, ""):
            specs["Weight"] = product.get("weight")
        dimensions = product.get("dimensions")
        if isinstance(dimensions, dict):
            for key in ("width", "height", "length"):
                if dimensions.get(key) not in (None, ""):
                    specs[key.title()] = dimensions.get(key)
        colors = product.get("color")
        if isinstance(colors, list) and colors:
            specs["Colors"] = ", ".join(str(c) for c in colors)
        variants = self._variants_summary(product)
        if variants:
            variant_type = variants[0].get("type")
            if variant_type:
                specs["Variant type"] = variant_type
            specs["Variants"] = "; ".join(
                " ".join(
                    str(part)
                    for part in [
                        row.get("value"),
                        row.get("price"),
                        "available" if row.get("available") else "unavailable" if row.get("available") is False else None,
                    ]
                    if part not in (None, "")
                )
                for row in variants
            )
        return specs

    def _detail_from_payload(self, product: Dict[str, Any], url: str) -> Dict[str, Any]:
        data = self._product_from_api(product)
        data["url"] = normalize_url(url) or url
        if data.get("name"):
            data.setdefault("title", data["name"])

        description = self._clean_html(product.get("descriptionEdited")) or self._clean_html(product.get("description"))
        if description:
            data["description"] = description
            data.setdefault("short_description", description)

        gallery = product.get("gallery")
        if isinstance(gallery, list):
            images = []
            for image in gallery:
                url_value = self._abs(image)
                if url_value and url_value not in images:
                    images.append(url_value)
            if images:
                data["images"] = images
                data["image"] = images[0]

        shop = product.get("shopId")
        if isinstance(shop, dict):
            data["seller_id"] = clean_text(shop.get("_id"))
            data["seller_name"] = clean_text(shop.get("shopTitle"))
            data["seller_logo"] = self._abs(shop.get("shopLogo"))
            data["seller_description"] = clean_text(shop.get("shopDescription"))
            if shop.get("rating") is not None:
                data["seller_rating"] = shop.get("rating")
        else:
            data["seller_id"] = clean_text(shop)

        specs = self._specifications(product)
        if specs:
            data["specifications"] = specs
        return {k: v for k, v in data.items() if v not in (None, "", [], {})}

    async def scrape_product_details(self, url: str) -> dict:
        meta = await super().fetch_html_with_meta(url)
        html = meta.get("html")
        if not html:
            return {"url": url, "error": meta.get("error") or "Failed to fetch"}

        sample_path = self.html_dir / "detail_sample_1.html"
        if not sample_path.exists():
            save_text_atomic(html, sample_path, self.logger)

        final_url = normalize_url(meta.get("final_url") or url) or url
        tree = HTMLParser(html)
        product = self._product_payload_from_html(html)
        data: Dict[str, Any] = {"url": final_url}

        if product:
            data.update(self._detail_from_payload(product, final_url))
        else:
            product_id = self._product_id_from_url(final_url)
            if product_id:
                data["product_id"] = product_id
                data["id"] = product_id

        metadata = html_product_metadata(html, final_url, self.base_url)
        for key, value in metadata.items():
            if value not in (None, "", [], {}):
                data.setdefault(key, value)

        title = clean_text(tree.css_first("h1").text(strip=True) if tree.css_first("h1") else None)
        if title:
            data.setdefault("title", title)
            data.setdefault("name", title)

        images = data.get("images") or []
        for image in self._images_from_html(tree):
            if image not in images:
                images.append(image)
        if images:
            data["images"] = images
            data.setdefault("image", images[0])

        return finalize_product_record({k: v for k, v in data.items() if v not in (None, "", [], {})})


def get_scraper(logger: logging.Logger) -> DokenaScraper:
    return DokenaScraper(logger)
