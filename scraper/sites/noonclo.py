#!/usr/bin/env python3
"""
NoonClo scraper - Shopify storefront, HTTP/selectolax + Shopify JSON.
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
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

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


class NoonCloScraper(FastScraper):
    """HTTP scraper for noonclo.com Shopify pages and JSON endpoints."""

    SKIP_COLLECTION_HANDLES = {"", "all", "frontpage"}
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
        "mailto:",
        "tel:",
        "javascript:",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("noonclo", logger)
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
        url = url.replace("{width}", "900")
        url = re.sub(r"_(?:\{width\}|[0-9]+)x(\.[a-zA-Z0-9]+)(\?|$)", r"_900x\1\2", url)
        return url

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
        match = re.search(r"/products/([^/?#]+?)(?:\.json)?/?$", parsed.path, re.I)
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
            self.logger.debug(f"NoonClo collections API evidence save failed: {exc}")
        return output_path

    async def scrape_categories_async(self) -> dict:
        data = None
        try:
            collections = await self._fetch_json(self.collections_api)
            save_text_atomic(
                json.dumps(collections, ensure_ascii=False, indent=2),
                self.html_dir / "collections_api.json",
                self.logger,
            )
            data = self._build_categories_from_collections(collections.get("collections") or [])
        except Exception as exc:
            self.logger.warning(f"NoonClo collections API failed, falling back to frontpage links: {exc}")

        if not data:
            html_path = self.html_dir / "frontpage.html"
            if not html_path.exists():
                await self.download_frontpage()
            data = self.extract_categories_from_html(html_path.read_text(encoding="utf-8"))

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
                return self._build_categories_from_collections(data["collections"])
        except Exception:
            pass
        return self._build_categories_from_links(html)

    def _category_bucket(self, title: str) -> Tuple[str, str]:
        name = clean_text(title) or "Collection"
        lower = name.lower()
        if lower == "femme":
            return "Femme", "All"
        if lower.startswith("femme "):
            return "Femme", clean_text(name[6:]) or name
        if lower == "homme":
            return "Homme", "All"
        if lower.startswith("homme "):
            return "Homme", clean_text(name[6:]) or name
        return "Collections", name

    def _build_categories_from_collections(self, collections: List[Dict[str, Any]]) -> dict:
        buckets: Dict[str, Dict[str, Any]] = {}
        seen = set()

        for item in collections:
            if not isinstance(item, dict):
                continue
            handle = clean_text(item.get("handle"))
            if not handle or handle in self.SKIP_COLLECTION_HANDLES or handle in seen:
                continue
            count = item.get("products_count")
            try:
                if count is not None and int(count) <= 0:
                    continue
            except (TypeError, ValueError):
                pass

            title = clean_text(item.get("title")) or handle.replace("-", " ").title()
            top_name, low_name = self._category_bucket(title)
            top_node = buckets.setdefault(
                top_name,
                {
                    "name": top_name,
                    "url": None,
                    "level": "top",
                    "low_level_categories": [],
                },
            )
            top_node["low_level_categories"].append(
                {
                    "name": low_name,
                    "url": self._collection_url(handle),
                    "level": "low",
                    "collection_id": clean_text(item.get("id")),
                    "handle": handle,
                    "product_count": count,
                    "subcategories": [],
                }
            )
            seen.add(handle)

        order = ["Femme", "Homme", "Collections"]
        categories = [buckets[name] for name in order if name in buckets]
        categories.extend(node for name, node in buckets.items() if name not in order)
        return {"categories": categories, "stats": self._category_stats(categories)}

    def _build_categories_from_links(self, html: str) -> dict:
        tree = HTMLParser(html)
        selector = self.selectors.get("frontpage", {}).get(
            "collection_links",
            "a[href*='/collections/']",
        )
        fake_collections = []
        seen = set()
        for node in tree.css(selector):
            handle = self._collection_handle_from_url(self._attr(node, "href") or "")
            if not handle or handle in seen:
                continue
            title = self._text(node) or handle.replace("-", " ").title()
            fake_collections.append(
                {
                    "id": None,
                    "title": title,
                    "handle": handle,
                    "products_count": None,
                }
            )
            seen.add(handle)
        return self._build_categories_from_collections(fake_collections)

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
                "__noonclo_listing__": True,
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
                "__noonclo_listing__": True,
                "url": url,
                "handle": handle,
                "page": page,
                "limit": self.page_size,
                "products": products if isinstance(products, list) else [],
                "error": None,
            }
        except Exception as exc:
            return {
                "__noonclo_listing__": True,
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
                        f'<div class="grid__item grid-product" data-product-id="{html_lib.escape(str(record.get("id") or ""))}" data-product-handle="{html_lib.escape(str(product.get("handle") or ""))}">',
                        f'  <a class="grid-product__link" href="{url}">',
                        f'    <img src="{image}" alt="{name}">',
                        f'    <div class="grid-product__title">{name}</div>',
                        "  </a>",
                        f'  <div class="grid-product__price">{record.get("price") if record.get("price") is not None else ""}</div>',
                        f'  <div class="grid-product__price--original">{record.get("old_price") if record.get("old_price") is not None else ""}</div>',
                        "</div>",
                    ]
                )
            )

        payload_json = html_lib.escape(json.dumps(payload, ensure_ascii=False), quote=False)
        return (
            "<!doctype html><html><body>"
            f'<script id="noonclo-listing-data" type="application/json">{payload_json}</script>'
            '<section id="noonclo-products">'
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
                data.setdefault("__noonclo_listing__", True)
                return data
        except Exception:
            pass

        tree = HTMLParser(html)
        node = tree.css_first("#noonclo-listing-data")
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
            return dedupe_products([p for p in products if p.get("url")], self.logger, "noonclo listing")

        return self._extract_products_from_cards(html)

    def _extract_products_from_cards(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products = []
        for card in tree.css(cp.get("item_selector", ".grid__item.grid-product, .grid-product")):
            link = card.css_first(cp.get("item_url", ".grid-product__link[href], a[href*='/products/']"))
            href = self._attr(link, "href")
            url = self._abs(href)
            if not url or "/products/" not in url:
                continue
            handle = self._product_handle_from_url(url)
            if not handle:
                continue
            url = self._product_url(handle)

            name = self._text(card.css_first(cp.get("item_name", ".grid-product__title")))
            if not name:
                name = clean_text(link.attributes.get("title") if link else None)
            if not name:
                continue

            old_text = self._text(card.css_first(cp.get("item_old_price", ".grid-product__price--original")))
            old_price = parse_price(old_text)
            price_node = card.css_first(cp.get("item_price", ".grid-product__price"))
            price_text = self._text(price_node)
            if price_text and old_text:
                price_text = clean_text(price_text.replace(old_text, " "))
            price = parse_price(price_text)
            if old_price is not None and price is not None and old_price <= price:
                old_price = None

            discount_percent = None
            if old_price and price and old_price > price:
                discount_percent = round((1 - price / old_price) * 100, 2)

            image = None
            for img in card.css(cp.get("item_image", "img")):
                image = self._image_url(
                    img.attributes.get("data-original")
                    or img.attributes.get("data-src")
                    or img.attributes.get("data-lazy-src")
                    or img.attributes.get("src")
                    or self._first_srcset_url(img.attributes.get("srcset"))
                )
                if image:
                    break

            badge_text = " ".join(
                self._text(node) or ""
                for node in card.css(cp.get("item_badges", ".grid-product__tag"))
            )
            availability, available = availability_from_text(badge_text)
            if "sold-out" in (card.attributes.get("class") or "").lower() or "epuise" in badge_text.lower() or "épuis" in badge_text.lower():
                availability, available = "Epuisé", False

            product_id = clean_text(card.attributes.get(cp.get("item_id_attr", "data-product-id")))
            record = {
                "id": product_id,
                "product_id": product_id,
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

        return dedupe_products(products, self.logger, "noonclo listing fallback")

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
        for link in tree.css(cp.get("pagination_pages", ".pagination a[href], a[href*='?page=']")):
            href = self._attr(link, "href") or ""
            query_page = dict(parse_qsl(urlsplit(href).query)).get("page")
            text_page = self._text(link)
            page = None
            for value in (query_page, text_page):
                try:
                    page = int(str(value).strip())
                    break
                except (TypeError, ValueError):
                    continue
            if page:
                total_pages = max(total_pages, page)
                class_name = (link.attributes.get("class") or "").lower()
                if "current" in class_name or "active" in class_name:
                    current_page = page

        next_node = tree.css_first(cp.get("pagination_next", "link[rel='next'][href], a[rel='next'][href]"))
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
        return products[:limit] if limit else dedupe_products(products, self.logger, "noonclo category")

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
            "brand": clean_text(product.get("vendor")),
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

        for script in tree.css("script[type='application/ld+json']"):
            raw = script.text()
            if not raw:
                continue
            try:
                parsed = json.loads(html_lib.unescape(raw.strip()))
            except Exception:
                continue
            for obj in self._walk_json(parsed):
                if not isinstance(obj, dict):
                    continue
                if "availability" not in obj:
                    continue
                availability_text, available = availability_from_text(obj.get("availability"))
                if available is None and availability_text:
                    continue
                sku = clean_text(obj.get("sku"))
                if sku:
                    availability["by_sku"][sku] = bool(available)
                offer_url = clean_text(obj.get("url"))
                if offer_url:
                    variant_id = dict(parse_qsl(urlsplit(offer_url).query)).get("variant")
                    if variant_id:
                        availability["by_id"][variant_id] = bool(available)

        return availability

    def _breadcrumbs_from_html(self, html: Optional[str]) -> List[str]:
        if not html:
            return []
        tree = HTMLParser(html)
        crumbs = []
        for node in tree.css("nav.breadcrumb a[href], .breadcrumb a[href], .breadcrumbs a[href]"):
            text = self._text(node)
            href = self._attr(node, "href")
            if text and href and "/products/" not in href and text not in crumbs:
                crumbs.append(text)
        return crumbs

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
            self.logger.debug(f"NoonClo product JSON failed for {handle}: {exc}")
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


def get_scraper(logger: logging.Logger) -> NoonCloScraper:
    return NoonCloScraper(logger)
