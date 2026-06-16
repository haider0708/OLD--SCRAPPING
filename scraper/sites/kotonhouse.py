#!/usr/bin/env python3
"""
Koton House scraper - TikTak PRO / Nuxt storefront with public JSON APIs.
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
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlunparse

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
    normalize_gtin,
    normalize_url,
    parse_price,
)


class KotonHouseScraper(FastScraper):
    """HTTP/API scraper for the Koton House TikTak storefront."""

    PAYLOAD_SELECTOR = "script#kotonhouse-listing-data"
    PRODUCT_LIST_RE = re.compile(r"/product-list/(\d+)/([^/?#]+)", re.I)

    def __init__(self, logger: logging.Logger):
        super().__init__("kotonhouse", logger)
        self.headers.update(self.config.get("headers", {}))
        self.api_base_url = self.config.get("api_base_url", "https://api.tiktak.space/api/v1").rstrip("/")
        self.web_api_base_url = self.config.get("web_api_base_url", "https://www.kotonhouse.tn/api").rstrip("/")
        self.company_id = self.config.get("company_id", "zjWmyEG")
        settings = self.config.get("settings", {})
        self.page_size = int(settings.get("page_size", 20))
        self.max_pages = int(settings.get("max_pages", 100))

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _abs(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

    def _public_api_url(self, endpoint: str) -> str:
        return f"{self.api_base_url}/{endpoint.lstrip('/')}"

    def _web_api_url(self, endpoint: str) -> str:
        return f"{self.web_api_base_url}/{endpoint.lstrip('/')}"

    async def _api_get(
        self,
        base: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        referer: Optional[str] = None,
    ) -> Any:
        client = await self.get_client()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base_url.rstrip("/"),
            "Referer": referer or self.base_url,
        }
        url = f"{base.rstrip('/')}/{endpoint.lstrip('/')}"
        response = await client.get(url, headers=headers, params=params)
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
    def _dedupe_values(values: Iterable[Any]) -> List[str]:
        seen = set()
        out = []
        for value in values:
            text = clean_text(value)
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

    @staticmethod
    def _slug_segment(value: Any) -> str:
        text = clean_text(value) or ""
        return quote(text.strip("/"), safe="-_.:")

    def _category_from_url(self, url: str) -> Optional[Dict[str, str]]:
        parsed = urlparse(url)
        match = self.PRODUCT_LIST_RE.search(parsed.path)
        if not match:
            return None
        return {
            "category_id": match.group(1),
            "slug": match.group(2),
            "url": normalize_url(urlunparse(parsed._replace(query="", fragment=""))) or url,
        }

    def _category_href(self, href: Any) -> Optional[str]:
        absolute = self._abs(href, self.base_url)
        if not absolute:
            return None
        parsed = urlparse(absolute)
        if not self.PRODUCT_LIST_RE.search(parsed.path):
            return None
        lowered = absolute.lower()
        blocked_fragments = (
            "/page/",
            "/account",
            "/cart",
            "/checkout",
            "/wishlist",
            "/search",
            "facebook.com",
            "instagram.com",
            "tiktok.com",
            "wa.me",
            "mailto:",
            "tel:",
        )
        if any(fragment in lowered for fragment in blocked_fragments):
            return None
        return normalize_url(urlunparse(parsed._replace(fragment=""))) or absolute

    def _page_from_url(self, url: str) -> int:
        raw = parse_qs(urlparse(url).query).get("page", ["1"])[0]
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _product_id_from_url(self, url: str) -> Optional[str]:
        parts = [part for part in urlparse(url).path.split("/") if part]
        if not parts or parts[0].lower() != "product":
            return None
        if len(parts) >= 4 and parts[1].isdigit() and parts[3].isdigit():
            return parts[3]
        if len(parts) >= 2 and parts[1].isdigit():
            return parts[1]
        for part in parts[1:]:
            if part.isdigit():
                return part
        return None

    def _category_info(self, product: Dict[str, Any]) -> Dict[str, Optional[str]]:
        category = product.get("_category") if isinstance(product.get("_category"), dict) else {}
        category_id = clean_text(category.get("id") or product.get("category"))
        category_slug = clean_text(category.get("seo_slug")) or category_id
        category_name = clean_text(category.get("name"))
        return {
            "category_id": category_id,
            "category_slug": category_slug,
            "category_name": category_name,
        }

    def _product_url(self, product: Dict[str, Any]) -> Optional[str]:
        product_id = clean_text(product.get("id"))
        if not product_id:
            return None
        category = self._category_info(product)
        category_id = category.get("category_id")
        category_slug = category.get("category_slug")
        product_slug = clean_text(product.get("seo_slug")) or clean_text(product.get("name")) or product_id
        if category_id and category_slug:
            path = (
                f"/product/{self._slug_segment(category_id)}/{self._slug_segment(category_slug)}/"
                f"{self._slug_segment(product_id)}/{self._slug_segment(product_slug)}"
            )
        else:
            path = f"/product/{self._slug_segment(product_id)}/{self._slug_segment(product_slug)}"
        return normalize_url(urljoin(self.base_url, path))

    def _image_urls(self, product: Dict[str, Any]) -> List[str]:
        urls: List[str] = []

        def add(value: Any) -> None:
            url = self._abs(value, self.base_url)
            if url and url not in urls:
                urls.append(url)

        images = product.get("images")
        if isinstance(images, dict):
            add(images.get("image"))
            add(images.get("image_thumb"))
        elif isinstance(images, list):
            for item in images:
                if isinstance(item, dict):
                    add(item.get("image"))
                    add(item.get("image_thumb"))
                else:
                    add(item)
        add(product.get("photo"))
        add(product.get("photo_thumb"))
        return urls

    def _brand_value(self, value: Any) -> Optional[str]:
        if isinstance(value, dict):
            return clean_text(value.get("name") or value.get("title") or value.get("label"))
        return clean_text(value)

    def _discounted_prices(self, product: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        original = parse_price(product.get("price"))
        discount = parse_price(product.get("discount"))
        discount_type = (clean_text(product.get("discount_type")) or "").lower()
        if original is None:
            return None, None, None
        if not discount or discount <= 0:
            return original, None, None
        if discount_type == "fixed_amount":
            current = max(0.0, original - discount)
            percent = round((discount * 100 / original), 2) if original > 0 else None
        elif discount_type in {"percentage", "percent"}:
            current = max(0.0, original * (1 - discount / 100))
            percent = round(discount, 2)
        else:
            current = original
            percent = None
        current = round(current, 3)
        old_price = original if current < original else None
        return current, old_price, percent

    def _stock_status(self, product: Dict[str, Any]) -> Tuple[Optional[str], Optional[bool], Optional[int]]:
        total_stock = parse_price(product.get("total_stock"))
        stock = parse_price(product.get("stock"))
        quantity = int(total_stock if total_stock is not None else stock) if (total_stock is not None or stock is not None) else None
        active_stock = product.get("active_stock")
        order_without_stock = product.get("order_without_stock")
        active = product.get("active")
        if active is False or product.get("display_on_website") is False:
            return "Indisponible", False, quantity
        if order_without_stock is True:
            return "En stock", True, quantity
        if active_stock is True:
            available = (quantity or 0) > 0
            return ("En stock" if available else "Rupture de stock"), available, quantity
        if quantity is not None:
            available = quantity > 0
            return ("En stock" if available else "Rupture de stock"), available, quantity
        return availability_from_text(product.get("availability")) + (quantity,)

    def _variants(self, product: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str], List[str], int]:
        variants: List[Dict[str, Any]] = []
        colors: List[str] = []
        sizes: List[str] = []
        stock_total = 0

        for row in product.get("declinaisons") or []:
            if not isinstance(row, dict):
                continue
            attrs = []
            variant_color = None
            variant_size = None
            for attr in row.get("_attributs") or []:
                if not isinstance(attr, dict):
                    continue
                option_name = clean_text(attr.get("productoption_name"))
                name = clean_text(attr.get("name"))
                attrs.append(
                    {
                        "option": option_name,
                        "name": name,
                        "value": clean_text(attr.get("value")),
                        "is_color": attr.get("is_color"),
                    }
                )
                if option_name and re.search(r"couleur|color", option_name, re.I):
                    variant_color = name
                    if name and name not in colors:
                        colors.append(name)
                if option_name and re.search(r"taille|size", option_name, re.I):
                    variant_size = name
                    if name and name not in sizes:
                        sizes.append(name)
            stock = parse_price(row.get("stock"))
            stock_int = int(stock) if stock is not None else 0
            stock_total += stock_int
            price, old_price, discount_percent = self._discounted_prices(row)
            variant = {
                "id": clean_text(row.get("id")),
                "reference": clean_text(row.get("reference")),
                "sku": clean_text(row.get("reference")),
                "stock": stock_int,
                "price": price,
                "old_price": old_price,
                "discount_percent": discount_percent,
                "color": variant_color,
                "size": variant_size,
                "attributes": [a for a in attrs if any(v not in (None, "", [], {}) for v in a.values())],
            }
            variants.append({k: v for k, v in variant.items() if v not in (None, "", [], {})})

        return variants, colors, sizes, stock_total

    def _barcode(self, product: Dict[str, Any], *extra_text: Any) -> Optional[str]:
        for key in ("bar_code", "barcode", "ean", "gtin", "gtin13"):
            gtin = normalize_gtin(product.get(key))
            if gtin:
                return gtin
        found = extract_gtins_from_text(" ".join(str(value or "") for value in extra_text))
        return found[0] if found else None

    def _product_from_api(self, product: Dict[str, Any]) -> Dict[str, Any]:
        price, old_price, discount_percent = self._discounted_prices(product)
        images = self._image_urls(product)
        availability, available, quantity = self._stock_status(product)
        variants, colors, sizes, variant_stock = self._variants(product)
        category = self._category_info(product)
        reference = clean_text(product.get("reference"))
        product_id = clean_text(product.get("id"))
        description = self._html_to_text(product.get("description")) or clean_text(product.get("seo_description"))
        brand = self._brand_value(product.get("brand"))

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": self._product_url(product),
            "name": clean_text(product.get("name")),
            "title": clean_text(product.get("seo_title") or product.get("name")),
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": images[0] if images else None,
            "images": images,
            "reference": reference,
            "sku": reference,
            "brand": brand,
            "availability": availability,
            "available": available,
            "stock_quantity": quantity,
            "variant_stock_quantity": variant_stock if variants else None,
            "short_description": description,
            "description": description,
            "category_id": category.get("category_id"),
            "category_name": category.get("category_name"),
            "category_slug": category.get("category_slug"),
            "categories": product.get("categories") or None,
            "colors": colors,
            "sizes": sizes,
            "variants": variants,
            "seo_keywords": clean_text(product.get("seo_keywords")),
            "rating": parse_price(product.get("seo_stars")),
            "review_count": parse_price(product.get("seo_reviews")),
        }
        barcode = self._barcode(product, description)
        if barcode:
            record["barcode"] = barcode
        return finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def _category_node(self, name: Any, url: Any, level: str, method: str) -> Optional[Dict[str, Any]]:
        href = self._category_href(url)
        if not href:
            return None
        info = self._category_from_url(href)
        if not info:
            return None
        label = clean_text(name) or info["slug"].replace("-", " ").replace("_", " ").title()
        return {
            "name": label,
            "url": href,
            "level": level,
            "category_id": info["category_id"],
            "category_slug": info["slug"],
            "discovery_method": method,
        }

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories: List[Dict[str, Any]] = []
        seen_urls = set()

        for link in tree.css("header nav a.nav-item[href*='/product-list/']"):
            node = self._category_node(self._text(link), link.attributes.get("href"), "top", "header_nav")
            if not node or node["url"] in seen_urls:
                continue
            seen_urls.add(node["url"])
            node["low_level_categories"] = []
            categories.append(node)

        for container in tree.css("header nav .dropdown-container"):
            top_name = self._text(container.css_first("span.nav-item")) or self._text(container.css_first("span"))
            low_nodes: List[Dict[str, Any]] = []
            for link in container.css(".dropdown a.dropdown-link[href*='/product-list/']"):
                node = self._category_node(self._text(link), link.attributes.get("href"), "low", "header_dropdown")
                if not node or node["url"] in seen_urls:
                    continue
                seen_urls.add(node["url"])
                node["subcategories"] = []
                low_nodes.append(node)
            if low_nodes:
                categories.append(
                    {
                        "name": top_name or "Produits",
                        "url": None,
                        "level": "top",
                        "low_level_categories": low_nodes,
                        "discovery_method": "header_dropdown_group",
                    }
                )

        if not categories:
            for link in tree.css("a[href*='/product-list/']"):
                node = self._category_node(self._text(link), link.attributes.get("href"), "top", "fallback_all_links")
                if not node or node["url"] in seen_urls:
                    continue
                seen_urls.add(node["url"])
                node["low_level_categories"] = []
                categories.append(node)

        low_count = sum(len(top.get("low_level_categories") or []) for top in categories)
        top_url_count = sum(1 for top in categories if top.get("url"))
        return {
            "categories": categories,
            "stats": {
                "top_level": len(categories),
                "low_level": low_count,
                "subcategory": 0,
                "total_urls": top_url_count + low_count,
            },
        }

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
                "__kotonhouse_listing__": True,
                "url": url,
                "page": page,
                "page_size": self.page_size,
                "count": 0,
                "total_pages": 1,
                "results": [],
                "error": "not_category_url",
            }

        try:
            payload = await self._api_get(
                self.api_base_url,
                "/products-read/",
                {
                    "company": self.company_id,
                    "page": page,
                    "ordering": "",
                    "size": self.page_size,
                    "no_parent": "true",
                    "active": "true",
                    "show-children": "false",
                    "has_attributs": "",
                    "has_category": category["category_id"],
                },
                referer=url,
            )
            if not isinstance(payload, dict):
                payload = {}
            payload["__kotonhouse_listing__"] = True
            payload["url"] = url
            payload["page"] = page
            payload["page_size"] = self.page_size
            payload["category"] = category
            payload["error"] = None
            return payload
        except Exception as exc:
            return {
                "__kotonhouse_listing__": True,
                "url": url,
                "page": page,
                "page_size": self.page_size,
                "count": 0,
                "total_pages": 1,
                "results": [],
                "category": category,
                "error": str(exc) or exc.__class__.__name__,
            }

    def _listing_payload_to_html(self, payload: Dict[str, Any]) -> str:
        cards = []
        for product in payload.get("results") or []:
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
            f'<script id="kotonhouse-listing-data" type="application/json">{payload_json}</script>'
            '<section id="kotonhouse-products">'
            + "\n".join(cards)
            + "</section></body></html>"
        )

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> Dict[str, Any]:
        if self._category_from_url(url):
            started = time.monotonic()
            payload = await self._fetch_listing_payload(url)
            html = self._listing_payload_to_html(payload)
            if payload.get("results") and not (self.html_dir / "listing_sample_1.html").exists():
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
            if isinstance(data, dict) and data.get("__kotonhouse_listing__"):
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
                for product in listing.get("results", [])
                if isinstance(product, dict)
            ]
            return dedupe_products(products, self.logger, "kotonhouse listing")

        tree = HTMLParser(html)
        products = []
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
        return dedupe_products([p for p in products if p.get("url")], self.logger, "kotonhouse listing fallback")

    def extract_pagination_from_html(self, html: str) -> dict:
        listing = self._listing_data_from_html(html)
        if listing:
            current = int(listing.get("current_page") or listing.get("page") or 1)
            total_pages = int(listing.get("total_pages") or current)
            count = int(listing.get("count") or 0)
            if not total_pages and count:
                total_pages = math.ceil(count / self.page_size)
            return {
                "current_page": current,
                "total_pages": max(1, total_pages),
                "has_next": bool(listing.get("next")) or current < total_pages,
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
                return dedupe_products(products[:limit], self.logger, "kotonhouse limited listing")
            pagination = result.get("pagination") or {}
            if not pagination.get("has_next"):
                break
            page += 1
        deduped = dedupe_products(products, self.logger, "kotonhouse category")
        return deduped[:limit] if limit else deduped

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def _fetch_product_payload(self, product_id: str, referer: str) -> Optional[Dict[str, Any]]:
        try:
            payload = await self._api_get(
                self.web_api_base_url,
                f"/product/{product_id}",
                {"company": self.company_id},
                referer=referer,
            )
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            self.logger.debug(f"KotonHouse product API failed id={product_id}: {exc}")
            return None

    async def _fetch_product_extra(self, product_id: str, referer: str) -> List[Dict[str, Any]]:
        try:
            payload = await self._api_get(
                self.web_api_base_url,
                f"/product-extra/{product_id}",
                {"company": self.company_id},
                referer=referer,
            )
            return payload if isinstance(payload, list) else []
        except Exception as exc:
            self.logger.debug(f"KotonHouse product-extra API failed id={product_id}: {exc}")
            return []

    def _extra_sections(self, payload: List[Dict[str, Any]]) -> Dict[str, str]:
        sections: Dict[str, str] = {}
        for block in payload or []:
            if not isinstance(block, dict):
                continue
            for item in block.get("data") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("slug") != "accordion":
                    continue
                for row in item.get("content") or []:
                    if not isinstance(row, dict):
                        continue
                    title = clean_text(row.get("title"))
                    text = self._html_to_text(row.get("description"))
                    if title and text:
                        sections[title] = text
        return sections

    def _detail_record(
        self,
        product: Dict[str, Any],
        url: str,
        html: Optional[str],
        extra_payload: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        record = self._product_from_api(product)
        metadata = html_product_metadata(html or "", url, self.base_url) if html else {}
        sections = self._extra_sections(extra_payload)
        description = (
            self._html_to_text(product.get("description"))
            or sections.get("Description")
            or clean_text(product.get("seo_description"))
            or metadata.get("description")
        )
        full_parts = []
        if description:
            full_parts.append(description)
        for title, text in sections.items():
            if title == "Description":
                continue
            full_parts.append(f"{title}: {text}")
        full_description = "\n\n".join(full_parts) if full_parts else description
        variants, colors, sizes, variant_stock = self._variants(product)
        category = self._category_info(product)
        specs: Dict[str, Any] = {
            "Reference": clean_text(product.get("reference")),
            "Category": category.get("category_name"),
            "Category ID": category.get("category_id"),
            "Stock total": record.get("stock_quantity"),
            "Variant stock total": variant_stock if variants else None,
            "Colors": colors,
            "Sizes": sizes,
        }
        for title, text in sections.items():
            if title != "Description":
                specs[title] = text
        for feature in product.get("features") or []:
            if isinstance(feature, dict):
                key = clean_text(feature.get("name") or feature.get("title"))
                value = clean_text(feature.get("value"))
                if key and value:
                    specs[key] = value
        specs = {k: v for k, v in specs.items() if v not in (None, "", [], {})}

        brand = record.get("brand") or metadata.get("brand")
        barcode = self._barcode(product, description, full_description, json.dumps(specs, ensure_ascii=False))

        record.update(
            {
                "url": normalize_url(record.get("url") or url) or url,
                "title": clean_text(product.get("seo_title") or product.get("name") or metadata.get("title")),
                "name": clean_text(product.get("name") or metadata.get("title")),
                "brand": brand,
                "description": description,
                "short_description": description,
                "full_description": full_description,
                "specifications": specs,
                "specs": specs,
                "variants": variants,
                "colors": colors,
                "sizes": sizes,
                "breadcrumbs": [
                    value
                    for value in (
                        category.get("category_name"),
                    )
                    if value
                ],
                "barcode": barcode,
            }
        )
        for key, value in metadata.items():
            if value not in (None, "", [], {}) and key not in record:
                record[key] = value
        return finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})

    async def scrape_product_details(self, url: str) -> dict:
        product_id = self._product_id_from_url(url)
        html = await super().fetch_html(url)
        detail_path = self.html_dir / "detail_sample_1.html"
        if html and not detail_path.exists():
            save_text_atomic(html, detail_path, self.logger)

        if not product_id and html:
            metadata = html_product_metadata(html, url, self.base_url)
            product_id = clean_text(metadata.get("product_id"))

        if not product_id:
            fallback = html_product_metadata(html or "", url, self.base_url) if html else {}
            fallback["url"] = normalize_url(url) or url
            fallback["shop"] = self.site_name
            fallback["error"] = "missing_product_id"
            return finalize_product_record(fallback)

        product = await self._fetch_product_payload(product_id, url)
        extra_payload = await self._fetch_product_extra(product_id, url)
        if not product:
            fallback = html_product_metadata(html or "", url, self.base_url) if html else {}
            fallback["url"] = normalize_url(url) or url
            fallback["product_id"] = product_id
            fallback["id"] = product_id
            fallback["shop"] = self.site_name
            fallback["error"] = "product_not_found"
            return finalize_product_record(fallback)

        record = self._detail_record(product, url, html, extra_payload)
        record["shop"] = self.site_name
        return record


def get_scraper(logger: logging.Logger) -> KotonHouseScraper:
    """Factory used by scraper.sites registry."""
    return KotonHouseScraper(logger)
