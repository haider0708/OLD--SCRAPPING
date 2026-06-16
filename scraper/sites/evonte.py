#!/usr/bin/env python3
"""
Evonte scraper - custom IG60/Next.js storefront, HTTP/selectolax.
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
    extract_gtins_from_text,
    finalize_product_record,
    html_product_metadata,
    normalize_gtin,
    normalize_url,
    parse_price,
)


class EvonteScraper(FastScraper):
    """HTTP scraper for evonte.shop Next.js product pages and data payloads."""

    PRODUCT_PATH_PREFIX = "/products/"
    LISTING_PATH = "/products"
    BAD_CATEGORY_PARTS = (
        "/cart",
        "/checkout",
        "/thankyou",
        "/search",
        "/pages/",
        "/explore",
        "/products/",
        "facebook.",
        "instagram.",
        "tiktok.",
        "mailto:",
        "tel:",
        "javascript:",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("evonte", logger)
        settings = self.config.get("settings", {})
        self.cdn_base_url = self.config.get("cdn_base_url", "https://cdn.ig60.com").rstrip("/")
        self.page_size = int(settings.get("page_size", 50))
        self.max_pages = int(settings.get("max_pages", 50))
        self._build_id: Optional[str] = None
        self._request_sem = asyncio.Semaphore(6)
        self.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @property
    def web_origin(self) -> str:
        parsed = urlsplit(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _text(node: Any) -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=" ", strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

    @staticmethod
    def _attr(node: Any, attr: str) -> Optional[str]:
        if not node:
            return None
        return clean_text(node.attributes.get(attr))

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
    def _clean_html_text(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if "<" not in text or ">" not in text:
            return text
        tree = HTMLParser(f"<div>{text}</div>")
        body = tree.css_first("div")
        if not body:
            return clean_text(re.sub(r"<[^>]+>", " ", html_lib.unescape(text)))
        try:
            return clean_text(body.text(separator=" ", strip=True))
        except TypeError:
            return clean_text(body.text(strip=True))

    @staticmethod
    def _computed_discount(price: Optional[float], old_price: Optional[float]) -> Optional[float]:
        if price is None or old_price is None or old_price <= 0 or price >= old_price:
            return None
        return round(((old_price - price) / old_price) * 100, 2)

    def _abs_site(self, value: Any) -> Optional[str]:
        return absolute_url(value, self.base_url)

    def _asset_url(self, value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if text.startswith("/api/files/"):
            return absolute_url(text, self.cdn_base_url)
        return absolute_url(text, self.base_url)

    def _same_host_url(self, value: Any) -> Optional[str]:
        url = self._abs_site(value)
        if not url:
            return None
        parsed = urlsplit(url)
        base = urlsplit(self.base_url)
        if parsed.netloc.lower().removeprefix("www.") != base.netloc.lower().removeprefix("www."):
            return None
        return url

    def _product_url(self, slug: Any) -> Optional[str]:
        slug = clean_text(slug)
        if not slug:
            return None
        slug = slug.strip("/")
        if slug.startswith("products/"):
            slug = slug.split("/", 1)[1]
        if not slug or "/" in slug:
            return None
        return f"{self.base_url.rstrip('/')}/products/{slug}"

    def _product_slug_from_url(self, url: Any) -> Optional[str]:
        site_url = self._same_host_url(url) or clean_text(url)
        if not site_url:
            return None
        path = urlsplit(site_url).path.rstrip("/")
        match = re.search(r"/products/([^/?#]+)$", path, re.I)
        if not match:
            return None
        return clean_text(match.group(1))

    def _is_listing_url(self, url: Any) -> bool:
        site_url = self._same_host_url(url)
        if not site_url:
            return False
        return urlsplit(site_url).path.rstrip("/") == self.LISTING_PATH

    def _page_from_url(self, url: str) -> int:
        params = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        try:
            return max(1, int(params.get("page") or 1))
        except (TypeError, ValueError):
            return 1

    def _next_data_from_html(self, html: str) -> Dict[str, Any]:
        tree = HTMLParser(html)
        node = tree.css_first("script#__NEXT_DATA__")
        if not node:
            return {}
        try:
            data = json.loads(node.text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _page_props_from_html(self, html: str) -> Dict[str, Any]:
        data = self._next_data_from_html(html)
        props = data.get("props") if isinstance(data, dict) else None
        page_props = props.get("pageProps") if isinstance(props, dict) else None
        return page_props if isinstance(page_props, dict) else {}

    def _build_id_from_html(self, html: str) -> Optional[str]:
        return clean_text(self._next_data_from_html(html).get("buildId"))

    async def _get_build_id(self, force: bool = False) -> str:
        if self._build_id and not force:
            return self._build_id
        html = await self._fetch_plain_html(f"{self.base_url.rstrip('/')}/products", raise_on_error=True)
        build_id = self._build_id_from_html(html or "")
        if not build_id:
            raise RuntimeError("Evonte Next buildId not found")
        self._build_id = build_id
        return build_id

    async def _fetch_plain_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        meta = await super().fetch_html_with_meta(url, raise_on_error=raise_on_error)
        return meta.get("html")

    def _listing_next_url(self, build_id: str, page: int) -> str:
        template = self.config.get("endpoints", {}).get(
            "next_products_template",
            f"{self.base_url.rstrip('/')}/_next/data/{{build_id}}/products.json?page={{page}}",
        )
        return template.format(build_id=build_id, page=max(1, int(page or 1)))

    def _detail_next_url(self, build_id: str, slug: str) -> str:
        template = self.config.get("endpoints", {}).get(
            "next_product_template",
            f"{self.base_url.rstrip('/')}/_next/data/{{build_id}}/products/{{slug}}.json",
        )
        return template.format(build_id=build_id, slug=slug)

    async def _fetch_json_url(self, url: str) -> Dict[str, Any]:
        async with self._request_sem:
            client = await self.get_client()
            response = await client.get(
                url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"{self.base_url.rstrip('/')}/products",
                    "x-nextjs-data": "1",
                },
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

    async def _fetch_next_json(self, url_builder, *, force_build_refresh: bool = False) -> Dict[str, Any]:
        build_id = await self._get_build_id(force=force_build_refresh)
        url = url_builder(build_id)
        try:
            return await self._fetch_json_url(url)
        except Exception:
            if force_build_refresh:
                raise
            build_id = await self._get_build_id(force=True)
            return await self._fetch_json_url(url_builder(build_id))

    # ------------------------------------------------------------------
    # Evidence and categories
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        html = await self._fetch_plain_html(self.base_url, raise_on_error=True)
        save_text_atomic(html, self.html_dir / "frontpage.html", self.logger)

        products_html = await self._fetch_plain_html(f"{self.base_url.rstrip('/')}/products", raise_on_error=True)
        save_text_atomic(products_html, self.html_dir / "products.html", self.logger)
        build_id = self._build_id_from_html(products_html or "")
        if build_id:
            self._build_id = build_id

        for key, filename in (("robots_url", "robots.txt"), ("sitemap_url", "sitemap.xml")):
            url = self.config.get("endpoints", {}).get(key)
            if not url:
                continue
            try:
                text = await self._fetch_plain_html(url)
                if text:
                    save_text_atomic(text, self.html_dir / filename, self.logger)
            except Exception as exc:
                self.logger.debug(f"Evonte evidence fetch failed for {url}: {exc}")

        try:
            payload = await self._fetch_listing_payload(f"{self.base_url.rstrip('/')}/products")
            products = payload.get("products") or []
            if products:
                detail_url = self._product_url(products[0].get("slug"))
                if detail_url:
                    detail_html = await self._fetch_plain_html(detail_url)
                    if detail_html:
                        save_text_atomic(detail_html, self.html_dir / "detail_sample_1.html", self.logger)
        except Exception as exc:
            self.logger.debug(f"Evonte detail sample save failed: {exc}")
        return self.html_dir / "frontpage.html"

    def extract_categories_from_html(self, html: str) -> dict:
        category_url = f"{self.base_url.rstrip('/')}/products"
        categories = [
            {
                "name": "Produits",
                "url": category_url,
                "level": "top",
                "category_id": "products",
                "slug": "products",
                "low_level_categories": [],
                "discovery_method": "next_products_listing",
            }
        ]
        return {
            "categories": categories,
            "stats": {
                "top_level": 1,
                "low_level": 0,
                "subcategory": 0,
                "total_urls": 1,
            },
        }

    # ------------------------------------------------------------------
    # Listing extraction
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parsed = urlsplit(base_url)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if page_num <= 1:
            params.pop("page", None)
        else:
            params["page"] = str(page_num)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))

    async def _fetch_listing_payload(self, url: str) -> Dict[str, Any]:
        page = self._page_from_url(url)
        data = await self._fetch_next_json(lambda build_id: self._listing_next_url(build_id, page))
        props = data.get("pageProps") if isinstance(data.get("pageProps"), dict) else {}
        products = props.get("productsdata") if isinstance(props.get("productsdata"), list) else []
        total_pages = int(parse_price(props.get("totalPages")) or 1)
        current_page = int(parse_price(props.get("currentPage")) or page)
        total_products = int(parse_price(props.get("totalProducts")) or len(products))
        return {
            "__evonte_listing__": True,
            "url": self.build_page_url(f"{self.base_url.rstrip('/')}/products", page),
            "page": current_page,
            "total_pages": max(1, total_pages),
            "total_products": total_products,
            "has_next": current_page < max(1, total_pages) and bool(products),
            "products": products,
        }

    def _listing_payload_to_html(self, payload: Dict[str, Any]) -> str:
        cards = []
        for product in payload.get("products", []):
            product_id = clean_text(product.get("_id")) or ""
            slug = clean_text(product.get("slug")) or ""
            url = self._product_url(slug) or ""
            name = html_lib.escape(clean_text(product.get("name")) or "")
            image = html_lib.escape(self._first_product_image(product) or "")
            price, old_price, discount = self._prices(product)
            availability, available = self._availability(product)
            cards.append(
                "\n".join(
                    [
                        f'<article class="evonte-product-card" data-id-product="{html_lib.escape(product_id)}" data-product-slug="{html_lib.escape(slug)}">',
                        f'  <a class="product-link" href="{html_lib.escape(url)}"><span class="product-name">{name}</span></a>',
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
            f'<script id="evonte-listing-data" type="application/json">{payload_json}</script>'
            '<section id="evonte-products">'
            + "\n".join(cards)
            + "</section></body></html>"
        )

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> Dict[str, Any]:
        if self._is_listing_url(url):
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
        try:
            data = json.loads(html)
            if isinstance(data, dict) and data.get("__evonte_listing__"):
                return data
        except Exception:
            pass
        tree = HTMLParser(html)
        node = tree.css_first("#evonte-listing-data")
        if node:
            try:
                data = json.loads(html_lib.unescape(node.text()))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        props = self._page_props_from_html(html)
        if isinstance(props.get("productsdata"), list):
            return {
                "__evonte_listing__": True,
                "page": props.get("currentPage") or 1,
                "total_pages": props.get("totalPages") or 1,
                "total_products": props.get("totalProducts") or len(props.get("productsdata") or []),
                "products": props.get("productsdata") or [],
            }
        return {}

    def _prices(self, product: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        price = parse_price(product.get("price"))
        old_price = parse_price(product.get("original_price"))
        if old_price is not None and price is not None and old_price <= price:
            old_price = None
        return price, old_price, self._computed_discount(price, old_price)

    def _availability(self, product: Dict[str, Any], fallback_text: Any = None) -> Tuple[Optional[str], Optional[bool]]:
        text, available = availability_from_text(fallback_text)
        if text or available is not None:
            return text, available

        stock_value = product.get("maxQuantityInStock")
        try:
            stock = int(stock_value) if stock_value is not None else None
        except (TypeError, ValueError):
            stock = None

        stockmanagement = bool(product.get("stockmanagement"))
        hide_out = bool(product.get("hideproductoutofstock"))
        if stockmanagement and stock is not None:
            if stock > 0:
                return f"En stock ({stock})", True
            if stock == 0 and hide_out:
                return "Rupture de stock", False
            return "Stock gere par la boutique", True
        return "En stock", True

    def _brand_from_product(self, product: Dict[str, Any], description_text: Optional[str] = None) -> Optional[str]:
        for source in (
            product.get("brand"),
            product.get("manufacturer"),
            product.get("vendor"),
            description_text,
            product.get("name"),
        ):
            text = clean_text(source)
            if not text:
                continue
            match = re.search(r"\bmarque\s+([A-Za-z0-9][A-Za-z0-9_-]{1,40})", text, re.I)
            if match:
                return match.group(1).strip().title()
            match = re.search(r"\bpoe?dagar\b", text, re.I)
            if match:
                return "Poedegar"
        return None

    def _image_urls_from_html(self, value: Any) -> List[str]:
        text = clean_text(value)
        if not text or "<img" not in text.lower():
            return []
        tree = HTMLParser(f"<div>{text}</div>")
        return self._dedupe_values(self._asset_url(img.attributes.get("src")) for img in tree.css("img[src]"))

    def _first_product_image(self, product: Dict[str, Any]) -> Optional[str]:
        images = self._product_images(product, include_description=False)
        return images[0] if images else None

    def _product_images(self, product: Dict[str, Any], include_description: bool = True) -> List[str]:
        values: List[Any] = [product.get("image")]
        for item in product.get("images") or []:
            if isinstance(item, dict):
                values.extend([item.get("url"), item.get("small")])
            else:
                values.append(item)
        images = [self._asset_url(value) for value in values]
        if include_description:
            images.extend(self._image_urls_from_html(product.get("description")))
        return self._dedupe_values(images)

    def _options_and_specs(self, product: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        specs: Dict[str, Any] = {}
        variants: List[Dict[str, Any]] = []

        colors = []
        for color in product.get("colors") or []:
            if not isinstance(color, dict):
                continue
            name = clean_text(color.get("name"))
            code = clean_text(color.get("code"))
            if name:
                colors.append({"name": name, "code": code})
        if colors:
            specs["colors"] = colors
            variants.extend({"type": "color", **color} for color in colors)

        sizes = []
        for size in product.get("size") or []:
            if isinstance(size, dict):
                value = clean_text(size.get("name") or size.get("value") or size.get("label"))
            else:
                value = clean_text(size)
            if value:
                sizes.append(value)
        if sizes:
            specs["sizes"] = sizes
            variants.extend({"type": "size", "name": size} for size in sizes)

        custom_fields = {}
        for field in product.get("customFields") or []:
            if not isinstance(field, dict):
                continue
            key = clean_text(field.get("name") or field.get("label") or field.get("key"))
            value = clean_text(field.get("value") or field.get("text"))
            if key and value:
                custom_fields[key] = value
        if custom_fields:
            specs.update(custom_fields)

        return specs, variants

    def _product_record_from_payload(self, product: Dict[str, Any]) -> Dict[str, Any]:
        price, old_price, discount = self._prices(product)
        availability, available = self._availability(product)
        slug = clean_text(product.get("slug"))
        images = self._product_images(product, include_description=False)
        description = self._clean_html_text(product.get("description"))
        specs, variants = self._options_and_specs(product)
        record = {
            "id": clean_text(product.get("_id")),
            "product_id": clean_text(product.get("_id")),
            "url": self._product_url(slug),
            "name": clean_text(product.get("name")),
            "title": clean_text(product.get("name")),
            "price": price,
            "old_price": old_price,
            "discount_percent": discount,
            "image": images[0] if images else None,
            "images": images,
            "sku": clean_text(product.get("sku")),
            "reference": clean_text(product.get("sku")),
            "brand": self._brand_from_product(product, description),
            "availability": availability,
            "available": available,
            "description": description,
            "specifications": specs or None,
            "variants": variants or None,
            "colors": specs.get("colors") if specs else None,
            "sizes": specs.get("sizes") if specs else None,
            "category": product.get("category"),
            "collections": product.get("collections") or None,
            "tags": product.get("tags") or None,
            "shop": self.site_name,
        }
        return {k: v for k, v in finalize_product_record(record).items() if v is not None and v != [] and v != {}}

    def extract_products_from_html(self, html: str) -> List[dict]:
        listing = self._listing_data_from_html(html)
        products = []
        if listing.get("products"):
            products = [self._product_record_from_payload(item) for item in listing.get("products", [])]
            return dedupe_products([p for p in products if p.get("url")], self.logger, "evonte listing")

        tree = HTMLParser(html)
        for card in tree.css("article.evonte-product-card"):
            product_id = self._attr(card, "data-id-product")
            slug = self._attr(card, "data-product-slug")
            link = card.css_first("a.product-link[href]")
            url = self._same_host_url(self._attr(link, "href")) if link else self._product_url(slug)
            name = self._text(card.css_first(".product-name"))
            price = parse_price(self._text(card.css_first(".product-price")))
            old_price = parse_price(self._text(card.css_first(".product-old-price")))
            discount = parse_price(self._text(card.css_first(".product-discount")))
            image = self._asset_url(self._attr(card.css_first("img.product-image"), "src"))
            avail_node = card.css_first(".product-availability")
            availability, available = availability_from_text(self._text(avail_node))
            attr_available = self._attr(avail_node, "data-available")
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
                        "price": price,
                        "old_price": old_price,
                        "discount_percent": discount or self._computed_discount(price, old_price),
                        "image": image,
                        "images": [image] if image else [],
                        "availability": availability,
                        "available": available,
                        "shop": self.site_name,
                    }
                )
            )
        return dedupe_products([p for p in products if p.get("url")], self.logger, "evonte listing fallback")

    def extract_pagination_from_html(self, html: str) -> dict:
        listing = self._listing_data_from_html(html)
        if listing:
            current = int(parse_price(listing.get("page")) or 1)
            total_pages = int(parse_price(listing.get("total_pages")) or 1)
            return {
                "current_page": current,
                "total_pages": max(1, total_pages),
                "has_next": bool(listing.get("has_next") or current < max(1, total_pages)),
                "total_products": int(parse_price(listing.get("total_products")) or 0),
            }

        props = self._page_props_from_html(html)
        current = int(parse_price(props.get("currentPage")) or 1)
        total_pages = int(parse_price(props.get("totalPages")) or 1)
        return {
            "current_page": current,
            "total_pages": max(1, total_pages),
            "has_next": current < max(1, total_pages),
            "total_products": int(parse_price(props.get("totalProducts")) or 0),
        }

    async def scrape_category_page(self, url: str) -> dict:
        meta = await self.fetch_html_with_meta(url)
        html = meta.get("html")
        if not html:
            return {"products": [], "pagination": {"total_pages": 1}, "error": meta.get("error") or "fetch_failed"}
        products = self.extract_products_from_html(html)
        pagination = self.extract_pagination_from_html(html)
        return {"products": products, "pagination": pagination}

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
            if limit and len(all_products) >= limit:
                return dedupe_products(all_products, self.logger, "evonte limited listing")[:limit]
            pagination = result.get("pagination") or {}
            if not pagination.get("has_next") or page >= int(pagination.get("total_pages") or 1):
                break
            page += 1
        return dedupe_products(all_products, self.logger, "evonte category")

    # ------------------------------------------------------------------
    # Detail extraction
    # ------------------------------------------------------------------

    async def _fetch_detail_payload(self, slug: str) -> Dict[str, Any]:
        data = await self._fetch_next_json(lambda build_id: self._detail_next_url(build_id, slug))
        props = data.get("pageProps") if isinstance(data.get("pageProps"), dict) else {}
        product = props.get("product")
        return product if isinstance(product, dict) else {}

    def _detail_fallbacks_from_html(self, html: str, url: str) -> Dict[str, Any]:
        tree = HTMLParser(html)
        meta = html_product_metadata(html, product_url=url, base_url=self.base_url)

        title = self._text(tree.css_first("h1.product-title, h1"))
        if title:
            meta.setdefault("title", title)
            meta.setdefault("name", title)

        price = parse_price(self._text(tree.css_first(".after-price")))
        old_price = parse_price(self._text(tree.css_first(".before-price")))
        if price is not None:
            meta["price"] = price
        if old_price is not None and (price is None or old_price > price):
            meta["old_price"] = old_price
            meta["discount_percent"] = self._computed_discount(price, old_price)

        stock_text = self._text(tree.css_first(".customStockContainer")) or self._text(tree.css_first(".stockCounterValue"))
        availability, available = availability_from_text(stock_text)
        if availability:
            meta["availability"] = availability
        if available is not None:
            meta["available"] = available
        elif stock_text:
            meta["availability"] = stock_text
            meta["available"] = True

        images = [
            self._asset_url(img.attributes.get("src") or img.attributes.get("data-src"))
            for img in tree.css("img[src*='products'], img[data-src*='products']")
        ]
        if images:
            images = self._dedupe_values(images)
            meta.setdefault("images", images)
            meta.setdefault("image", images[0])
        return meta

    async def scrape_product_details(self, url: str) -> dict:
        slug = self._product_slug_from_url(url)
        if not slug:
            return {}

        html = await self._fetch_plain_html(url)
        if html and not (self.html_dir / "detail_sample_1.html").exists():
            save_text_atomic(html, self.html_dir / "detail_sample_1.html", self.logger)

        product: Dict[str, Any] = {}
        try:
            product = await self._fetch_detail_payload(slug)
        except Exception as exc:
            self.logger.debug(f"Evonte detail Next payload failed for {url}: {exc}")
            if html:
                product = self._page_props_from_html(html).get("product") or {}

        record: Dict[str, Any] = {}
        if product:
            record.update(self._product_record_from_payload(product))
            record["description"] = self._clean_html_text(product.get("description")) or record.get("description")
            record["full_description"] = record.get("description")
            images = self._product_images(product, include_description=True)
            if images:
                record["images"] = images
                record["image"] = images[0]
            gtins = extract_gtins_from_text(product.get("description"))
            if gtins:
                record["barcode"] = gtins[0]

        if html:
            fallback = self._detail_fallbacks_from_html(html, url)
            for key, value in fallback.items():
                if value is not None and key not in record:
                    record[key] = value
            if not record.get("brand"):
                record["brand"] = self._brand_from_product(product, record.get("description"))

        record.setdefault("url", self._product_url(slug) or url)
        record.setdefault("shop", self.site_name)
        record.setdefault("scraped_at", datetime.now().isoformat())

        return {k: v for k, v in finalize_product_record(record).items() if v is not None and v != [] and v != {}}

    async def scrape_categories_async(self) -> dict:
        data = self.extract_categories_from_html("")
        data["site"] = self.site_name
        data["shop"] = self.site_name
        data["base_url"] = self.base_url
        data["extracted_at"] = datetime.now().isoformat()
        data["date"] = get_date_folder()
        save_json(data, self.data_dir / "categories.json", self.logger)
        return data


def get_scraper(logger: logging.Logger) -> EvonteScraper:
    return EvonteScraper(logger)
