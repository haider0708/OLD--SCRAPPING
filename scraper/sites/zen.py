"""
Zen.com.tn scraper backed by the storefront's public cache/API endpoints.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import math
import re
import time
import unicodedata
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx
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


class ZenScraper(FastScraper):
    """HTTP scraper for Zen's Angular storefront and cache API."""

    CATEGORY_RE = re.compile(r"/fr/(?:home/)?(\d+)-[^/?#]+", re.I)
    PRODUCT_RE = re.compile(r"/fr/produit/(\d+)-[^/?#]+", re.I)
    _MIN_API_INTERVAL = 0.12

    def __init__(self, logger: logging.Logger):
        super().__init__("zen", logger)
        settings = self.config.get("settings", {})
        self.web_origin = self.config.get("web_origin", "https://zen.com.tn").rstrip("/")
        self.cache_api_base = self.config.get("cache_api_base", "https://cache-v3.zen.com.tn/indexes").rstrip("/")
        self.main_api_base = self.config.get("main_api_base", "https://api-v3.zen.com.tn/api").rstrip("/")
        self.cache_token = clean_text(self.config.get("cache_token"))
        self.page_size = int(settings.get("page_size", 12))
        self.max_pages = int(settings.get("max_pages", 200))
        self._api_sem = asyncio.Semaphore(6)
        self._last_api_request = 0.0
        self._category_map_cache: Optional[Dict[str, Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _abs(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.web_origin)

    def _cache_url(self, endpoint: str) -> str:
        return f"{self.cache_api_base}/{endpoint.lstrip('/')}"

    def _main_url(self, endpoint: str) -> str:
        return f"{self.main_api_base}/{endpoint.lstrip('/')}"

    def _cache_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": self.web_origin,
            "Referer": self.base_url,
        }
        if self.cache_token:
            headers["Authorization"] = f"Bearer {self.cache_token}"
        return headers

    async def _throttle_api(self):
        now = time.monotonic()
        wait = self._MIN_API_INTERVAL - (now - self._last_api_request)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_api_request = time.monotonic()

    async def _cache_get(self, endpoint: str) -> Any:
        async with self._api_sem:
            await self._throttle_api()
            client = await self.get_client()
            response = await client.get(self._cache_url(endpoint), headers=self._cache_headers())
            response.raise_for_status()
            return response.json()

    async def _cache_post(self, endpoint: str, payload: Dict[str, Any]) -> Any:
        async with self._api_sem:
            await self._throttle_api()
            client = await self.get_client()
            response = await client.post(
                self._cache_url(endpoint),
                headers=self._cache_headers(),
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def _main_get(self, endpoint: str) -> Any:
        async with self._api_sem:
            await self._throttle_api()
            client = await self.get_client()
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Origin": self.web_origin,
                "Referer": self.base_url,
            }
            response = await client.get(self._main_url(endpoint), headers=headers)
            response.raise_for_status()
            return response.json()

    def _clean_html_text(self, value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if self._is_empty_placeholder(text):
            return None
        if "<" in text and ">" in text:
            tree = HTMLParser(f"<div>{text}</div>")
            text = tree.body.text(separator=" ", strip=True) if tree.body else re.sub(r"<[^>]+>", " ", text)
        return clean_text(html_lib.unescape(text))

    def _is_empty_placeholder(self, value: Any) -> bool:
        text = clean_text(value)
        if not text:
            return True
        ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
        return ascii_text in {"aucune donnee trouvee", "none", "null", "n/a", "-"}

    def _product_id_from_url(self, url: str) -> Optional[str]:
        match = self.PRODUCT_RE.search(urlparse(url).path)
        return match.group(1) if match else None

    def _category_id_from_url(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        if "/produit/" in parsed.path:
            return None
        match = self.CATEGORY_RE.search(parsed.path)
        return match.group(1) if match else None

    def _page_from_url(self, url: str) -> int:
        raw = parse_qs(urlparse(url).query).get("page", ["1"])[0]
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _product_url(self, product: Dict[str, Any]) -> Optional[str]:
        link = clean_text(product.get("link"))
        product_id = clean_text(product.get("id"))
        if link:
            return f"{self.web_origin}/fr/produit/{link.lstrip('/')}"
        if product_id:
            title = clean_text(product.get("title")) or "produit"
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "produit"
            return f"{self.web_origin}/fr/produit/{product_id}-{slug}"
        return None

    def _category_url(self, category: Dict[str, Any]) -> Optional[str]:
        link = clean_text(category.get("link"))
        category_id = clean_text(category.get("id"))
        if link:
            return f"{self.web_origin}/fr/{link.lstrip('/')}"
        if category_id:
            return f"{self.web_origin}/fr/{category_id}-category"
        return None

    def _category_filter(self, category_id: str) -> str:
        return f"categories.id = {category_id}"

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        output_path = self.html_dir / "frontpage.html"
        html = await super().fetch_html(self.base_url, raise_on_error=True)
        save_text_atomic(html, output_path, self.logger)
        return output_path

    async def _fetch_categories_payload(self) -> List[Dict[str, Any]]:
        data = await self._cache_get("categories/search")
        hits = data.get("hits") if isinstance(data, dict) else data
        return hits if isinstance(hits, list) else []

    def _valid_category(self, category: Dict[str, Any]) -> bool:
        name = clean_text(category.get("name"))
        url = self._category_url(category)
        category_id = clean_text(category.get("id"))
        if not name or not url or not category_id:
            return False
        low = f"{name} {url}".lower()
        blocked = (
            "login",
            "account",
            "cart",
            "checkout",
            "search",
            "contact",
            "blog",
            "cms",
            "module",
            "seller",
            "brand",
            "marque",
            "manufacturer",
            "produit/",
            "zen-looks",
            "hope-machine",
        )
        return not any(token in low for token in blocked)

    def _category_node(self, category: Dict[str, Any], level: str) -> Dict[str, Any]:
        return {
            "name": clean_text(category.get("name")),
            "url": self._category_url(category),
            "level": level,
            "category_id": clean_text(category.get("id")),
            "id_origin": clean_text(category.get("idOrigin")),
            "link": clean_text(category.get("link")),
            "image": self._first_image(category),
        }

    def _flatten_descendants(self, category: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for child in category.get("subCategories") or []:
            if not isinstance(child, dict) or not self._valid_category(child):
                continue
            out.append(self._category_node(child, "subcategory"))
            out.extend(self._flatten_descendants(child))
        return out

    def _build_categories_data(self, categories_api: List[Dict[str, Any]]) -> Dict[str, Any]:
        categories: List[Dict[str, Any]] = []
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}

        for top in categories_api:
            if not isinstance(top, dict) or not self._valid_category(top):
                continue
            top_node = self._category_node(top, "top")
            top_node["low_level_categories"] = []
            stats["top_level"] += 1
            stats["total_urls"] += 1

            for low in top.get("subCategories") or []:
                if not isinstance(low, dict) or not self._valid_category(low):
                    continue
                low_node = self._category_node(low, "low")
                low_node["subcategories"] = self._flatten_descendants(low)
                stats["low_level"] += 1
                stats["subcategory"] += len(low_node["subcategories"])
                stats["total_urls"] += 1 + len(low_node["subcategories"])
                top_node["low_level_categories"].append(low_node)

            categories.append(top_node)

        return {"categories": categories, "stats": stats}

    def extract_categories_from_html(self, html: str) -> dict:
        """Fallback parser for visible homepage nav when the API is unavailable."""
        try:
            data = json.loads(html)
            if isinstance(data, list):
                return self._build_categories_data(data)
            if isinstance(data, dict) and isinstance(data.get("hits"), list):
                return self._build_categories_data(data["hits"])
        except Exception:
            pass

        tree = HTMLParser(html)
        seen: set[str] = set()
        categories: List[Dict[str, Any]] = []
        selectors = self.selectors.get("frontpage", {})
        link_selectors = [
            selectors.get("desktop_menu"),
            selectors.get("mobile_menu"),
            selectors.get("category_links"),
        ]
        for selector in [s for s in link_selectors if s]:
            for node in tree.css(selector):
                href = self._abs(node.attributes.get("href"), self.web_origin)
                category_id = self._category_id_from_url(href or "")
                name = clean_text(node.text(strip=True) or node.attributes.get("title"))
                if not href or not category_id or not name or category_id in seen:
                    continue
                seen.add(category_id)
                categories.append(
                    {
                        "name": name,
                        "url": normalize_url(href) or href,
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
        categories_api = await self._fetch_categories_payload()
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
        save_json(data, self.data_dir / "categories.json", self.logger)
        return data

    async def _category_name_map(self) -> Dict[str, Dict[str, Any]]:
        if self._category_map_cache is not None:
            return self._category_map_cache

        mapping: Dict[str, Dict[str, Any]] = {}

        def walk(items: Iterable[Dict[str, Any]], parents: Optional[List[str]] = None):
            for item in items:
                if not isinstance(item, dict):
                    continue
                category_id = clean_text(item.get("id"))
                name = clean_text(item.get("name"))
                if category_id:
                    mapping[category_id] = {
                        "name": name,
                        "parents": list(parents or []),
                        "link": clean_text(item.get("link")),
                    }
                children = item.get("subCategories") or []
                walk(children, [*(parents or []), name] if name else parents or [])

        try:
            walk(await self._fetch_categories_payload())
        except Exception as exc:
            self.logger.debug(f"Failed to load Zen category map: {exc}")
        self._category_map_cache = mapping
        return mapping

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
        page = self._page_from_url(url)
        if not category_id:
            return {
                "__zen_listing__": True,
                "url": url,
                "page": page,
                "limit": self.page_size,
                "offset": 0,
                "products": [],
                "estimatedTotalHits": 0,
                "total_pages": 1,
                "has_next": False,
                "error": "missing_category_id",
            }

        offset = (page - 1) * self.page_size
        payload = {
            "filter": self._category_filter(category_id),
            "limit": self.page_size,
            "offset": offset,
            "sort": [f"pos_in_cat_{category_id}:asc"],
        }
        try:
            data = await self._cache_post("products/search", payload)
            products = data.get("hits") if isinstance(data, dict) else []
            total = int(data.get("estimatedTotalHits") or len(products) or 0) if isinstance(data, dict) else len(products)
            total_pages = max(1, math.ceil(total / self.page_size)) if total else 1
            return {
                "__zen_listing__": True,
                "url": url,
                "category_id": category_id,
                "page": page,
                "limit": self.page_size,
                "offset": offset,
                "products": products if isinstance(products, list) else [],
                "estimatedTotalHits": total,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "error": None,
            }
        except Exception as exc:
            return {
                "__zen_listing__": True,
                "url": url,
                "category_id": category_id,
                "page": page,
                "limit": self.page_size,
                "offset": offset,
                "products": [],
                "estimatedTotalHits": 0,
                "total_pages": 1,
                "has_next": False,
                "error": str(exc) or exc.__class__.__name__,
            }

    def _listing_payload_to_html(self, payload: Dict[str, Any]) -> str:
        cards = []
        for product in payload.get("products", []):
            if not isinstance(product, dict):
                continue
            product_id = clean_text(product.get("id")) or ""
            name = html_lib.escape(clean_text(product.get("title")) or "")
            url = html_lib.escape(self._product_url(product) or "")
            image = html_lib.escape(self._first_image(product) or "")
            price, old_price, discount_percent = self._prices(product)
            availability, available = self._availability(product)
            cards.append(
                "\n".join(
                    [
                        f'<article class="product-box" data-id-product="{html_lib.escape(product_id)}">',
                        f'  <div class="carousel-wrapper"><a href="{url}" title="{name}"><img src="{image}" alt="{name}"></a></div>',
                        f'  <h2 class="product-title">{name}</h2>',
                        '  <div class="product-price">',
                        f'    <span class="current-price"><span>{price if price is not None else ""}</span></span>',
                        f'    <span class="old-price">{old_price if old_price is not None else ""}</span>',
                        f'    <span class="discount">{discount_percent if discount_percent is not None else ""}</span>',
                        "  </div>",
                        f'  <span class="availability" data-available="{str(bool(available)).lower()}">{html_lib.escape(availability or "")}</span>',
                        "</article>",
                    ]
                )
            )

        payload_json = html_lib.escape(json.dumps(payload, ensure_ascii=False), quote=False)
        return (
            "<!doctype html><html><body>"
            f'<script id="zen-listing-data" type="application/json">{payload_json}</script>'
            '<section id="js-product-list">'
            + "\n".join(cards)
            + "</section></body></html>"
        )

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> Dict[str, Any]:
        if self._category_id_from_url(url):
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

    def _listing_data_from_html(self, html: str) -> Dict[str, Any]:
        try:
            data = json.loads(html)
            if isinstance(data, dict) and data.get("__zen_listing__"):
                return data
        except Exception:
            pass

        tree = HTMLParser(html)
        node = tree.css_first("#zen-listing-data")
        if node:
            try:
                return json.loads(html_lib.unescape(node.text()))
            except Exception:
                return {}
        return {}

    def _first_attr(self, root: Any, selectors: Iterable[str], attrs: Iterable[str]) -> Optional[str]:
        for selector in selectors:
            node = root.css_first(selector)
            if not node:
                continue
            for attr in attrs:
                value = clean_text(node.attributes.get(attr))
                if value:
                    return value
        return None

    def _prices(self, product: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        current = parse_price(product.get("currentPrice"))
        regular = parse_price(product.get("price"))
        if current is None:
            current = regular

        discount_percent = parse_price(product.get("discountPercent") or product.get("discount_percentage"))
        discount_value = parse_price(product.get("discountValue"))
        if discount_percent is None and discount_value and 0 < discount_value <= 100:
            discount_percent = discount_value

        old_price = None
        if regular is not None and current is not None and regular > current:
            old_price = regular
            discount_percent = discount_percent or round((regular - current) * 100 / regular, 2)
        elif product.get("discount") and regular is not None and current is not None and regular != current:
            old_price = regular

        return current, old_price, discount_percent

    def _availability(self, product: Dict[str, Any]) -> Tuple[str, Optional[bool]]:
        flags = product.get("flags") if isinstance(product.get("flags"), dict) else {}
        if flags.get("rupture") is True:
            return "Rupture de stock", False
        in_stock = product.get("inStock")
        if isinstance(in_stock, bool):
            return ("En stock" if in_stock else "Rupture de stock"), in_stock
        quantity = parse_price(product.get("quantity") or product.get("stock"))
        if quantity is not None:
            return ("En stock" if quantity > 0 else "Rupture de stock"), quantity > 0
        return "En stock", None

    def _collect_images(self, value: Any) -> List[str]:
        images: List[str] = []

        def add(url: Any):
            abs_url = self._abs(url)
            if abs_url and abs_url not in images:
                images.append(abs_url)

        def walk(obj: Any):
            if isinstance(obj, str):
                add(obj)
            elif isinstance(obj, dict):
                for key in ("url", "src", "path", "image"):
                    if key in obj:
                        walk(obj.get(key))
                for key in ("large", "medium", "small", "thumb", "thumbnail", "original"):
                    if key in obj:
                        walk(obj.get(key))
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(value)
        return images

    def _first_image(self, product: Dict[str, Any]) -> Optional[str]:
        for key in ("firstImg", "secondImg", "image", "images"):
            images = self._collect_images(product.get(key))
            if images:
                return images[0]
        for declination in product.get("declinaisons") or []:
            if isinstance(declination, dict):
                images = self._collect_images(declination.get("images"))
                if images:
                    return images[0]
        return None

    def _all_images(self, product: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        for key in ("firstImg", "secondImg", "image", "images"):
            for image in self._collect_images(product.get(key)):
                if image not in images:
                    images.append(image)
        for declination in product.get("declinaisons") or []:
            if not isinstance(declination, dict):
                continue
            for image in self._collect_images(declination.get("images")):
                if image not in images:
                    images.append(image)
        return images

    def _colors(self, product: Dict[str, Any]) -> List[str]:
        colors: List[str] = []
        for declination in product.get("declinaisons") or []:
            if not isinstance(declination, dict):
                continue
            value = clean_text(declination.get("libellet") or declination.get("couleur") or declination.get("color"))
            if value and value not in colors:
                colors.append(value)
        return colors

    def _product_from_api(self, product: Dict[str, Any]) -> Dict[str, Any]:
        price, old_price, discount_percent = self._prices(product)
        availability, available = self._availability(product)
        product_id = clean_text(product.get("id"))
        reference = clean_text(product.get("sku") or product.get("reference"))
        url = self._product_url(product)
        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "product_origin_id": clean_text(product.get("idOrigin")),
            "url": normalize_url(url) or url,
            "name": clean_text(product.get("title")),
            "title": clean_text(product.get("title")),
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": self._first_image(product),
            "reference": reference,
            "sku": reference,
            "availability": availability,
            "available": available,
            "category_ids": [clean_text(c.get("id")) for c in product.get("categories") or [] if isinstance(c, dict) and clean_text(c.get("id"))],
            "family": clean_text(product.get("Famille")),
            "line": clean_text(product.get("Ligne")),
            "collection": clean_text(product.get("Collection")),
            "persona": clean_text(product.get("Persona")),
        }
        brand = clean_text(product.get("brand") or product.get("manufacturer") or product.get("marque"))
        if brand:
            record["brand"] = brand
        colors = self._colors(product)
        if colors:
            record["colors"] = colors
        return finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})

    def extract_products_from_html(self, html: str) -> List[dict]:
        listing = self._listing_data_from_html(html)
        if listing:
            products = [self._product_from_api(p) for p in listing.get("products", []) if isinstance(p, dict)]
            return dedupe_products(products, self.logger, "zen listing")

        tree = HTMLParser(html)
        products: List[Dict[str, Any]] = []
        selectors = self.selectors.get("category_page", {})
        for card in tree.css(selectors.get("product_card", ".product-box")):
            link = card.css_first(selectors.get("product_url", "a[href*='/fr/produit/']"))
            href = self._abs(link.attributes.get("href") if link else None, self.web_origin)
            product_id = clean_text(card.attributes.get("data-id-product")) or self._product_id_from_url(href or "")
            name_node = card.css_first(selectors.get("name", ".product-title"))
            name = clean_text(name_node.text(strip=True) if name_node else None)
            if not name and link:
                name = clean_text(link.attributes.get("title") or link.text(strip=True))
            price_node = card.css_first(selectors.get("price", ".current-price span"))
            old_node = card.css_first(selectors.get("old_price", ".old-price"))
            image = self._first_attr(
                card,
                [selectors.get("image", "img"), "img"],
                ["data-full-size-image-url", "data-src", "src"],
            )
            availability_node = card.css_first(".availability, .product-availability")
            availability, available = availability_from_text(availability_node.text(strip=True) if availability_node else None)
            colors = [
                clean_text(node.attributes.get("title"))
                for node in card.css(selectors.get("color", ".product-variants .color-box[title]"))
            ]
            colors = [c for c in colors if c]
            products.append(
                finalize_product_record(
                    {
                        "id": product_id,
                        "product_id": product_id,
                        "url": normalize_url(href) or href,
                        "name": name,
                        "price": parse_price(price_node.text(strip=True) if price_node else None),
                        "old_price": parse_price(old_node.text(strip=True) if old_node else None),
                        "image": self._abs(image, self.web_origin),
                        "availability": availability,
                        "available": available,
                        "colors": colors or None,
                    }
                )
            )
        return dedupe_products([p for p in products if p.get("url") and p.get("name")], self.logger, "zen listing fallback")

    def extract_pagination_from_html(self, html: str) -> dict:
        listing = self._listing_data_from_html(html)
        if listing:
            current = int(listing.get("page") or 1)
            total_pages = int(listing.get("total_pages") or current)
            return {
                "current_page": current,
                "total_pages": max(current, total_pages),
                "has_next": bool(listing.get("has_next")),
                "total_products": int(listing.get("estimatedTotalHits") or 0),
                "method": "api_offset",
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
                return products[:limit]
            pagination = result.get("pagination") or {}
            if not pagination.get("has_next"):
                break
            page += 1
        return products[:limit] if limit else dedupe_products(products, self.logger, "zen category")

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def _fetch_product_by_id(self, product_id: str, product_link: Optional[str] = None) -> Optional[Dict[str, Any]]:
        filters = [f"id = {product_id}"]
        if product_link:
            filters.append(f"link = {product_link}")
        for filter_expr in filters:
            try:
                data = await self._cache_post("products/search", {"filter": filter_expr, "limit": 1, "offset": 0})
            except httpx.HTTPError:
                continue
            hits = data.get("hits") if isinstance(data, dict) else []
            if isinstance(hits, list) and hits:
                return hits[0]
        return None

    async def _fetch_product_meta(self, product_id: str) -> Dict[str, Any]:
        try:
            data = await self._cache_post(
                "product-meta-info/search",
                {"filter": f"product = {product_id} AND lang = fr", "limit": 5, "offset": 0},
            )
        except Exception as exc:
            self.logger.debug(f"Failed to fetch Zen product meta {product_id}: {exc}")
            return {}
        hits = data.get("hits") if isinstance(data, dict) else []
        return hits[0] if isinstance(hits, list) and hits else {}

    async def _fetch_declination_stock(self, declination_id: Any) -> List[Dict[str, Any]]:
        declination_id = clean_text(declination_id)
        if not declination_id:
            return []
        try:
            data = await self._main_get(f"getDeclinaisonStock/{declination_id}")
        except Exception as exc:
            self.logger.debug(f"Failed to fetch Zen stock for declination {declination_id}: {exc}")
            return []
        rows = data.get("data") if isinstance(data, dict) else data
        return rows if isinstance(rows, list) else []

    async def _variant_details(self, product: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        variants: List[Dict[str, Any]] = []
        stock_rows: List[Dict[str, Any]] = []
        for declination in product.get("declinaisons") or []:
            if not isinstance(declination, dict):
                continue
            declination_id = clean_text(declination.get("id"))
            stock = await self._fetch_declination_stock(declination_id)
            stock_rows.extend(stock)
            quantity = 0
            sizes = []
            for row in stock:
                qte = parse_price(row.get("qte"))
                if qte is not None:
                    quantity += int(qte)
                size = clean_text(row.get("size"))
                if size and size not in sizes:
                    sizes.append(size)
            variant = {
                "id": declination_id,
                "color": clean_text(declination.get("libellet") or declination.get("couleur")),
                "active": declination.get("active"),
                "sizes": sizes,
                "quantity": quantity,
                "images": self._collect_images(declination.get("images")),
            }
            variants.append({k: v for k, v in variant.items() if v not in (None, "", [], {})})
        return variants, stock_rows

    def _stock_summary(self, stock_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        total_quantity = 0
        references: List[str] = []
        sizes: List[str] = []
        barcodes: List[str] = []
        for row in stock_rows:
            qte = parse_price(row.get("qte"))
            if qte is not None:
                total_quantity += int(qte)
            reference = clean_text(row.get("reference"))
            if reference and reference not in references:
                references.append(reference)
            size = clean_text(row.get("size"))
            if size and size not in sizes:
                sizes.append(size)
            for key in ("ean13", "barcode", "ean", "gtin"):
                gtin = normalize_gtin(row.get(key))
                if gtin and gtin not in barcodes:
                    barcodes.append(gtin)
        return {
            "quantity": total_quantity,
            "references": references,
            "sizes": sizes,
            "barcodes": barcodes,
        }

    async def scrape_product_details(self, url: str) -> dict:
        html = await super().fetch_html(url)
        if html and not (self.html_dir / "detail_sample_1.html").exists():
            save_text_atomic(html, self.html_dir / "detail_sample_1.html", self.logger)

        metadata = html_product_metadata(html or "", url, self.web_origin) if html else {}
        product_id = self._product_id_from_url(url) or clean_text(metadata.get("product_id"))
        product_link = None
        path_part = urlparse(url).path.rsplit("/", 1)[-1]
        if path_part:
            product_link = path_part

        product = await self._fetch_product_by_id(product_id, product_link) if product_id else None
        if not product:
            fallback = {
                "url": url,
                "title": metadata.get("title"),
                "name": metadata.get("title") or metadata.get("name"),
                **metadata,
            }
            return finalize_product_record({k: v for k, v in fallback.items() if v not in (None, "", [], {})})

        product_id = clean_text(product.get("id")) or product_id
        meta = await self._fetch_product_meta(product_id) if product_id else {}
        variants, stock_rows = await self._variant_details(product)
        stock = self._stock_summary(stock_rows)

        price, old_price, discount_percent = self._prices(product)
        availability, available = self._availability(product)
        if stock_rows:
            available = stock["quantity"] > 0
            availability = "En stock" if available else "Rupture de stock"

        category_map = await self._category_name_map()
        category_ids = [clean_text(c.get("id")) for c in product.get("categories") or [] if isinstance(c, dict) and clean_text(c.get("id"))]
        breadcrumbs = []
        for category_id in category_ids:
            info = category_map.get(category_id or "")
            name = clean_text(info.get("name")) if info else None
            if name and name not in breadcrumbs:
                breadcrumbs.append(name)

        images = self._all_images(product)
        if not images and metadata.get("images"):
            images = [self._abs(image, self.web_origin) for image in metadata.get("images") or []]
            images = [image for image in images if image]

        short_description = self._clean_html_text(meta.get("shortDesc") or metadata.get("description"))
        full_description = self._clean_html_text(meta.get("longDesc"))
        composition = self._clean_html_text(meta.get("composition"))
        details = self._clean_html_text(meta.get("details"))
        sizes_guide = self._clean_html_text(meta.get("sizesGuide"))

        reference = clean_text(product.get("sku") or product.get("reference"))
        if stock.get("references"):
            reference = stock["references"][0] or reference
        barcode = stock["barcodes"][0] if stock.get("barcodes") else None
        if not barcode:
            joined_stock = json.dumps(stock_rows, ensure_ascii=False)
            gtins = extract_gtins_from_text(joined_stock)
            barcode = gtins[0] if gtins else None

        specs: Dict[str, Any] = {
            "Famille": clean_text(product.get("Famille")),
            "Ligne": clean_text(product.get("Ligne")),
            "Collection": clean_text(product.get("Collection")),
            "Persona": clean_text(product.get("Persona")),
            "Composition": composition,
            "Details": details,
            "Sizes guide": sizes_guide,
            "Colors": self._colors(product),
            "Sizes": stock.get("sizes"),
            "Quantity": stock.get("quantity") if stock_rows else None,
        }
        specs = {key: value for key, value in specs.items() if value not in (None, "", [], {})}

        record: Dict[str, Any] = {
            "url": normalize_url(self._product_url(product) or url) or url,
            "id": product_id,
            "product_id": product_id,
            "product_origin_id": clean_text(product.get("idOrigin")),
            "title": clean_text(product.get("title") or metadata.get("title")),
            "name": clean_text(product.get("title") or metadata.get("title")),
            "sku": reference,
            "reference": reference,
            "barcode": barcode,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "availability": availability,
            "available": available,
            "short_description": short_description,
            "description": full_description or short_description or metadata.get("description"),
            "full_description": full_description,
            "specifications": specs,
            "images": images,
            "image": images[0] if images else self._first_image(product),
            "category_ids": category_ids,
            "breadcrumbs": breadcrumbs,
            "variants": variants,
            "stock": stock if stock_rows else None,
            "family": clean_text(product.get("Famille")),
            "line": clean_text(product.get("Ligne")),
            "collection": clean_text(product.get("Collection")),
        }

        brand = clean_text(product.get("brand") or product.get("manufacturer") or product.get("marque") or metadata.get("brand"))
        if brand:
            record["brand"] = brand

        return finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})


def get_scraper(logger: logging.Logger) -> ZenScraper:
    return ZenScraper(logger)
