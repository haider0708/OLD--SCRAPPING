"""
Hamadi Abid scraper for ha.com.tn.

The public site is a Vue SPA shell, while catalog data is served by JSON API
endpoints used by the storefront.
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
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlunparse

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


class HamadiAbidScraper(FastScraper):
    """HTTP/API scraper for Hamadi Abid."""

    PRODUCT_REF_RE = re.compile(r"/([0-9A-Za-z_-]+)-article(?:-|$)", re.I)
    _MIN_API_INTERVAL = 0.15

    def __init__(self, logger: logging.Logger):
        super().__init__("hamadiabid", logger)
        settings = self.config.get("settings", {})
        self.web_origin = self.config.get("web_origin", "https://ha.com.tn").rstrip("/")
        self.api_base_url = self.config.get("api_base_url", f"{self.web_origin}/api").rstrip("/")
        self.page_size = int(settings.get("page_size", 24))
        self.max_pages = int(settings.get("max_pages", 200))
        self._api_sem = asyncio.Semaphore(6)
        self._last_api_request = 0.0

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _abs(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.web_origin)

    def _api_url(self, endpoint: str) -> str:
        return f"{self.api_base_url}/{endpoint.lstrip('/')}"

    async def _throttle_api(self) -> None:
        now = time.monotonic()
        wait = self._MIN_API_INTERVAL - (now - self._last_api_request)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_api_request = time.monotonic()

    async def _api_get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        async with self._api_sem:
            await self._throttle_api()
            client = await self.get_client()
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Origin": self.web_origin,
                "Referer": self.base_url,
            }
            response = await client.get(self._api_url(endpoint), headers=headers, params=params)
            response.raise_for_status()
            return response.json()

    def _encode_segment(self, value: Any) -> str:
        text = clean_text(value) or ""
        return quote(text.strip("/"), safe="")

    def _decode_segment(self, value: str) -> str:
        return clean_text(unquote(value.strip("/"))) or ""

    def _is_visible(self, item: Dict[str, Any]) -> bool:
        return item.get("isVisible") is not False

    def _image_url(self, value: Any, *, size: Optional[str] = None) -> Optional[str]:
        image_id = clean_text(value)
        if not image_id:
            return None
        if image_id.startswith(("http://", "https://", "/", "data:")):
            return self._abs(image_id, self.web_origin)
        middle = f"product/{size}/" if size else "product/"
        return f"{self.web_origin}/api/image/get/{middle}{image_id}"

    def _product_url(self, product: Dict[str, Any]) -> Optional[str]:
        raw = clean_text(product.get("url"))
        if not raw:
            return None
        if raw.startswith(("http://", "https://")):
            return raw
        if raw.startswith("/catalogue/"):
            return urljoin(self.web_origin, raw)
        return urljoin(self.web_origin, "/catalogue" + (raw if raw.startswith("/") else f"/{raw}"))

    def _product_ref_from_url(self, url: str) -> Optional[str]:
        match = self.PRODUCT_REF_RE.search(urlparse(url).path)
        return clean_text(match.group(1)) if match else None

    def _catalog_parts_from_url(self, url: str) -> Optional[List[str]]:
        parsed = urlparse(url)
        parts = [self._decode_segment(part) for part in parsed.path.split("/") if part]
        if not parts or parts[0].lower() != "catalogue":
            return None
        catalog_parts = parts[1:]
        if not catalog_parts:
            return None
        if len(catalog_parts) > 3 or self._product_ref_from_url(url):
            return None
        return catalog_parts

    def _product_context_from_url(self, url: str) -> Dict[str, Optional[str]]:
        parsed = urlparse(url)
        parts = [self._decode_segment(part) for part in parsed.path.split("/") if part]
        if parts and parts[0].lower() == "catalogue":
            parts = parts[1:]
        if parts and self.PRODUCT_REF_RE.search(parts[-1]):
            parts = parts[:-1]
        return {
            "section": parts[0] if len(parts) >= 1 else None,
            "group": parts[1] if len(parts) >= 2 else None,
            "subgroup": parts[2] if len(parts) >= 3 else None,
        }

    def _page_from_url(self, url: str) -> int:
        raw = parse_qs(urlparse(url).query).get("page", ["1"])[0]
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _listing_params_from_url(self, url: str) -> Optional[Dict[str, Any]]:
        parts = self._catalog_parts_from_url(url)
        if not parts:
            return None
        params: Dict[str, Any] = {
            "page": self._page_from_url(url),
            "pageSize": self.page_size,
        }
        if len(parts) >= 1:
            params["selectedSection"] = parts[0]
        if len(parts) >= 2:
            params["selectedGroupName"] = parts[1]
        if len(parts) >= 3:
            params["selectedSubGroupName"] = parts[2]
        return params

    def _price_tuple(self, product: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        current = parse_price(product.get("priceDiscounted"))
        regular = parse_price(product.get("price"))
        if current is None:
            current = regular

        discount_percent = parse_price(product.get("discount"))
        if discount_percent is None:
            discount_percent = parse_price(product.get("discountStr"))
        if discount_percent is not None and discount_percent <= 0:
            discount_percent = None

        old_price = None
        if regular is not None and current is not None and regular > current:
            old_price = regular
            if discount_percent is None:
                discount_percent = round((regular - current) * 100 / regular, 2)
        elif discount_percent and regular is not None:
            old_price = regular
        return current, old_price, discount_percent

    def _brand_value(self, value: Any) -> Optional[str]:
        if isinstance(value, dict):
            return clean_text(value.get("name") or value.get("label") or value.get("title"))
        return clean_text(value)

    def _first_gtin(self, product: Dict[str, Any]) -> Optional[str]:
        explicit_keys = ("barcode", "barCode", "ean", "ean13", "gtin", "gtin13")
        for key in explicit_keys:
            value = product.get(key)
            gtin = normalize_gtin(value)
            if gtin:
                return gtin
            found = extract_gtins_from_text(value)
            if found:
                return found[0]
        return None

    def _stock_summary(self, product: Dict[str, Any]) -> Dict[str, Any]:
        sizes_data = product.get("sizes") if isinstance(product.get("sizes"), dict) else {}
        variants: List[Dict[str, Any]] = []
        images: List[str] = []
        colors: List[str] = []
        sizes: List[str] = []
        total_stock = 0
        has_stock_rows = False

        for size_name, rows in sizes_data.items():
            size_text = clean_text(size_name)
            if size_text and size_text not in sizes:
                sizes.append(size_text)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                has_stock_rows = True
                stock = parse_price(row.get("stock"))
                stock_int = int(stock) if stock is not None else 0
                total_stock += stock_int
                color = row.get("color") if isinstance(row.get("color"), dict) else {}
                color_name = clean_text(color.get("name"))
                if color_name and color_name not in colors:
                    colors.append(color_name)
                for image_id in row.get("itemsVariantImages") or []:
                    image = self._image_url(image_id)
                    if image and image not in images:
                        images.append(image)
                variant = {
                    "id": clean_text(row.get("id")),
                    "size": size_text,
                    "stock": stock_int,
                    "color": color_name,
                    "color_rgb": clean_text(color.get("rgb")),
                    "images": [self._image_url(img) for img in row.get("itemsVariantImages") or []],
                }
                variant["images"] = [img for img in variant["images"] if img]
                variants.append({k: v for k, v in variant.items() if v not in (None, "", [], {})})

        main_color = product.get("color") if isinstance(product.get("color"), dict) else {}
        main_color_name = clean_text(main_color.get("name"))
        if main_color_name and main_color_name not in colors:
            colors.append(main_color_name)

        return {
            "has_stock_rows": has_stock_rows,
            "quantity": total_stock,
            "available": total_stock > 0 if has_stock_rows else None,
            "availability": "En stock" if total_stock > 0 else ("Rupture de stock" if has_stock_rows else None),
            "sizes": sizes,
            "colors": colors,
            "variants": variants,
            "images": images,
        }

    def _all_images(self, product: Dict[str, Any], stock: Optional[Dict[str, Any]] = None) -> List[str]:
        images: List[str] = []
        for image in (
            self._image_url(product.get("image")),
            self._image_url(product.get("image"), size="small"),
        ):
            if image and image not in images:
                images.append(image)
        for image in (stock or {}).get("images") or []:
            if image and image not in images:
                images.append(image)
        return images

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        output_path = self.html_dir / "frontpage.html"
        html = await super().fetch_html(self.base_url, raise_on_error=True)
        save_text_atomic(html, output_path, self.logger)
        return output_path

    async def _fetch_menu(self) -> List[Dict[str, Any]]:
        data = await self._api_get("/items/menu")
        return data if isinstance(data, list) else []

    def _category_url(self, *segments: Any) -> str:
        encoded = [self._encode_segment(segment) for segment in segments if clean_text(segment)]
        return f"{self.web_origin}/catalogue/" + "/".join(encoded)

    def _image_from_menu(self, item: Dict[str, Any]) -> Optional[str]:
        return self._image_url(item.get("image") or item.get("landingPageImage"))

    def _build_categories_data(self, menu: List[Dict[str, Any]]) -> Dict[str, Any]:
        categories: List[Dict[str, Any]] = []
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}

        for section in menu:
            if not isinstance(section, dict) or not self._is_visible(section):
                continue
            section_slug = clean_text(section.get("seoName"))
            section_name = clean_text(section.get("name"))
            groups = section.get("groups") if isinstance(section.get("groups"), list) else []
            if not section_slug or not section_name or not groups:
                continue

            top_node = {
                "name": section_name,
                "url": self._category_url(section_slug),
                "level": "top",
                "category_id": clean_text(section.get("id")),
                "functional_id": section.get("functionalId"),
                "seo_name": section_slug,
                "image": self._image_from_menu(section),
                "low_level_categories": [],
            }
            stats["top_level"] += 1
            stats["total_urls"] += 1

            for group in groups:
                if not isinstance(group, dict) or not self._is_visible(group):
                    continue
                group_slug = clean_text(group.get("seoName"))
                group_name = clean_text(group.get("name"))
                if not group_slug or not group_name:
                    continue

                low_node = {
                    "name": group_name,
                    "url": self._category_url(section_slug, group_slug),
                    "level": "low",
                    "category_id": clean_text(group.get("id")),
                    "seo_name": group_slug,
                    "ext_id": group.get("extId"),
                    "subcategories": [],
                }
                stats["low_level"] += 1
                stats["total_urls"] += 1

                for subgroup in group.get("subGroups") or []:
                    if not isinstance(subgroup, dict) or not self._is_visible(subgroup):
                        continue
                    subgroup_slug = clean_text(subgroup.get("seoName"))
                    subgroup_name = clean_text(subgroup.get("name"))
                    if not subgroup_slug or not subgroup_name:
                        continue
                    low_node["subcategories"].append(
                        {
                            "name": subgroup_name,
                            "url": self._category_url(section_slug, group_slug, subgroup_slug),
                            "level": "subcategory",
                            "category_id": clean_text(subgroup.get("id")),
                            "seo_name": subgroup_slug,
                            "ext_id": subgroup.get("extId"),
                        }
                    )
                    stats["subcategory"] += 1
                    stats["total_urls"] += 1

                top_node["low_level_categories"].append(low_node)

            categories.append(top_node)

        return {"categories": categories, "stats": stats}

    def _build_categories_from_sitemap(self, text: str) -> Dict[str, Any]:
        tree: Dict[str, Dict[str, Any]] = {}
        for match in re.finditer(r"<loc>(.*?)</loc>", text, re.I | re.S):
            loc = html_lib.unescape(match.group(1).strip())
            parts = self._catalog_parts_from_url(loc)
            if not parts:
                continue
            if len(parts) > 3:
                continue
            section = parts[0]
            section_node = tree.setdefault(
                section,
                {
                    "name": section.replace("-", " ").title(),
                    "url": self._category_url(section),
                    "level": "top",
                    "seo_name": section,
                    "low_level_categories": {},
                },
            )
            if len(parts) >= 2:
                group = parts[1]
                group_node = section_node["low_level_categories"].setdefault(
                    group,
                    {
                        "name": group.replace("-", " ").title(),
                        "url": self._category_url(section, group),
                        "level": "low",
                        "seo_name": group,
                        "subcategories": {},
                    },
                )
                if len(parts) == 3:
                    subgroup = parts[2]
                    group_node["subcategories"].setdefault(
                        subgroup,
                        {
                            "name": subgroup.replace("-", " ").title(),
                            "url": self._category_url(section, group, subgroup),
                            "level": "subcategory",
                            "seo_name": subgroup,
                        },
                    )

        categories = []
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for section_node in tree.values():
            lows = []
            for low_node in section_node.pop("low_level_categories").values():
                subs = list(low_node.pop("subcategories").values())
                stats["subcategory"] += len(subs)
                stats["total_urls"] += len(subs)
                low_node["subcategories"] = subs
                lows.append(low_node)
            section_node["low_level_categories"] = lows
            stats["top_level"] += 1
            stats["low_level"] += len(lows)
            stats["total_urls"] += 1 + len(lows)
            categories.append(section_node)
        return {"categories": categories, "stats": stats}

    def extract_categories_from_html(self, html: str) -> dict:
        try:
            data = json.loads(html)
            if isinstance(data, list):
                return self._build_categories_data(data)
        except Exception:
            pass

        if "<urlset" in html and "/catalogue/" in html:
            return self._build_categories_from_sitemap(html)

        tree = HTMLParser(html)
        categories = []
        seen = set()
        for node in tree.css("a[href^='/catalogue/'], a[href*='ha.com.tn/catalogue/']"):
            href = self._abs(node.attributes.get("href"), self.web_origin)
            parts = self._catalog_parts_from_url(href or "")
            if not href or not parts:
                continue
            key = tuple(parts)
            if key in seen:
                continue
            seen.add(key)
            categories.append(
                {
                    "name": clean_text(node.text(strip=True)) or parts[-1].replace("-", " ").title(),
                    "url": normalize_url(href) or href,
                    "level": "top",
                    "seo_name": parts[-1],
                    "low_level_categories": [],
                }
            )
        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": len(categories)}
        return {"categories": categories, "stats": stats}

    async def scrape_categories_async(self) -> dict:
        try:
            menu = await self._fetch_menu()
            save_text_atomic(
                json.dumps(menu, ensure_ascii=False, indent=2),
                self.html_dir / "menu_api.json",
                self.logger,
            )
            data = self._build_categories_data(menu)
        except Exception as exc:
            self.logger.warning(f"Menu API failed for hamadiabid, falling back to sitemap: {exc}")
            html = await super().fetch_html(urljoin(self.web_origin, "/sitemap.xml"), raise_on_error=True)
            save_text_atomic(html, self.html_dir / "sitemap.xml", self.logger)
            data = self._build_categories_from_sitemap(html)

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
        return urlunparse(parsed._replace(query=query))

    async def _fetch_listing_payload(self, url: str) -> Dict[str, Any]:
        params = self._listing_params_from_url(url)
        page = self._page_from_url(url)
        if not params:
            return {
                "__hamadiabid_listing__": True,
                "url": url,
                "page": page,
                "pageSize": self.page_size,
                "maxPagesCount": 1,
                "total": 0,
                "list": [],
                "error": "not_category_url",
            }
        try:
            data = await self._api_get("/items/list", params=params)
            if not isinstance(data, dict):
                data = {}
            data["__hamadiabid_listing__"] = True
            data["url"] = url
            data["error"] = None
            return data
        except Exception as exc:
            return {
                "__hamadiabid_listing__": True,
                "url": url,
                "page": page,
                "pageSize": self.page_size,
                "maxPagesCount": 1,
                "total": 0,
                "list": [],
                "error": str(exc) or exc.__class__.__name__,
            }

    def _listing_payload_to_html(self, payload: Dict[str, Any]) -> str:
        cards = []
        for product in payload.get("list") or []:
            if not isinstance(product, dict):
                continue
            product_id = clean_text(product.get("id")) or ""
            reference = clean_text(product.get("ref")) or ""
            name = html_lib.escape(clean_text(product.get("title")) or "")
            url = html_lib.escape(self._product_url(product) or "")
            image = html_lib.escape(self._image_url(product.get("image"), size="small") or "")
            price, old_price, discount_percent = self._price_tuple(product)
            stock = self._stock_summary(product)
            cards.append(
                "\n".join(
                    [
                        f'<article class="card" data-id-product="{html_lib.escape(product_id)}" data-reference="{html_lib.escape(reference)}">',
                        f'  <div class="cardInner"><a href="{url}" title="{name}"><img class="product-image" src="{image}" alt="{name}"></a></div>',
                        f'  <h2 class="product-title">{name}</h2>',
                        f'  <span class="price">{price if price is not None else ""}</span>',
                        f'  <span class="old-price">{old_price if old_price is not None else ""}</span>',
                        f'  <span class="discount">{discount_percent if discount_percent is not None else ""}</span>',
                        f'  <span class="availability" data-available="{str(bool(stock.get("available"))).lower()}">{html_lib.escape(stock.get("availability") or "")}</span>',
                        "</article>",
                    ]
                )
            )

        payload_json = html_lib.escape(json.dumps(payload, ensure_ascii=False), quote=False)
        return (
            "<!doctype html><html><body>"
            f'<script id="hamadiabid-listing-data" type="application/json">{payload_json}</script>'
            '<section id="hamadiabid-products">'
            + "\n".join(cards)
            + "</section></body></html>"
        )

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> Dict[str, Any]:
        if self._listing_params_from_url(url):
            started = time.monotonic()
            payload = await self._fetch_listing_payload(url)
            html = self._listing_payload_to_html(payload)
            if payload.get("list") and not (self.html_dir / "listing_sample_1.html").exists():
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
            if isinstance(data, dict) and data.get("__hamadiabid_listing__"):
                return data
        except Exception:
            pass

        tree = HTMLParser(html)
        node = tree.css_first("#hamadiabid-listing-data")
        if node:
            try:
                return json.loads(html_lib.unescape(node.text()))
            except Exception:
                return {}
        return {}

    def _product_from_api(self, product: Dict[str, Any]) -> Dict[str, Any]:
        price, old_price, discount_percent = self._price_tuple(product)
        stock = self._stock_summary(product)
        product_id = clean_text(product.get("id"))
        reference = clean_text(product.get("ref"))
        url = self._product_url(product)
        images = self._all_images(product, stock)
        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": normalize_url(url) or url,
            "name": clean_text(product.get("title")),
            "title": clean_text(product.get("title")),
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": images[0] if images else self._image_url(product.get("image")),
            "reference": reference,
            "sku": reference,
            "availability": stock.get("availability"),
            "available": stock.get("available"),
            "short_description": clean_text(product.get("description")),
            "season": clean_text(product.get("itemSeasonName")),
            "section": clean_text((product.get("section") or {}).get("name")) if isinstance(product.get("section"), dict) else None,
            "section_slug": clean_text((product.get("section") or {}).get("seoName")) if isinstance(product.get("section"), dict) else None,
            "subgroup": clean_text((product.get("subGroup") or {}).get("name")) if isinstance(product.get("subGroup"), dict) else None,
            "subgroup_slug": clean_text((product.get("subGroup") or {}).get("seoName")) if isinstance(product.get("subGroup"), dict) else None,
            "colors": stock.get("colors"),
            "sizes": stock.get("sizes"),
            "stock_quantity": stock.get("quantity") if stock.get("has_stock_rows") else None,
            "variants": stock.get("variants"),
        }
        brand = self._brand_value(product.get("brand"))
        if brand:
            record["brand"] = brand
        barcode = self._first_gtin(product)
        if barcode:
            record["barcode"] = barcode
        return finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})

    def extract_products_from_html(self, html: str) -> List[dict]:
        listing = self._listing_data_from_html(html)
        if listing:
            products = [self._product_from_api(p) for p in listing.get("list", []) if isinstance(p, dict)]
            return dedupe_products(products, self.logger, "hamadiabid listing")

        tree = HTMLParser(html)
        products = []
        for card in tree.css(".card"):
            link = card.css_first(".cardInner a[href*='article'], a[href*='article']")
            href = self._abs(link.attributes.get("href") if link else None, self.web_origin)
            image_node = card.css_first("img.product-image, img")
            price_node = card.css_first(".price, [class*='price']")
            availability_node = card.css_first(".availability, [class*='stock']")
            availability, available = availability_from_text(availability_node.text(strip=True) if availability_node else None)
            products.append(
                finalize_product_record(
                    {
                        "id": clean_text(card.attributes.get("data-id-product")),
                        "product_id": clean_text(card.attributes.get("data-id-product")),
                        "url": normalize_url(href) or href,
                        "name": clean_text(link.attributes.get("title") if link else None),
                        "price": parse_price(price_node.text(strip=True) if price_node else None),
                        "image": self._abs(image_node.attributes.get("src") if image_node else None, self.web_origin),
                        "reference": clean_text(card.attributes.get("data-reference")),
                        "availability": availability,
                        "available": available,
                    }
                )
            )
        return dedupe_products([p for p in products if p.get("url")], self.logger, "hamadiabid listing fallback")

    def extract_pagination_from_html(self, html: str) -> dict:
        listing = self._listing_data_from_html(html)
        if listing:
            current = int(listing.get("page") or 1)
            total_pages = int(listing.get("maxPagesCount") or current)
            return {
                "current_page": current,
                "total_pages": max(current, total_pages),
                "has_next": current < total_pages,
                "total_products": int(listing.get("total") or 0),
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
                return products[:limit]
            pagination = result.get("pagination") or {}
            if not pagination.get("has_next"):
                break
            page += 1
        return products[:limit] if limit else dedupe_products(products, self.logger, "hamadiabid category")

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def _fetch_product_detail(self, url: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        reference = self._product_ref_from_url(url)
        context = self._product_context_from_url(url)
        if not reference:
            return None, {}

        params = {"reference": reference}
        if context.get("section"):
            params["selectedSection"] = context["section"]
        if context.get("group"):
            params["selectedGroupName"] = context["group"]
        if context.get("subgroup"):
            params["selectedSubGroupName"] = context["subgroup"]

        try:
            data = await self._api_get("/items/ref", params=params)
            if isinstance(data, dict) and data.get("list"):
                return data["list"][0], data
        except Exception as exc:
            self.logger.debug(f"Failed Hamadi Abid detail endpoint for {reference}: {exc}")

        try:
            fallback_params = {
                "page": 1,
                "pageSize": 1,
                "reference": reference,
            }
            fallback_params.update({k: v for k, v in params.items() if k != "reference" and v})
            data = await self._api_get("/items/list", params=fallback_params)
            if isinstance(data, dict) and data.get("list"):
                return data["list"][0], data
        except Exception as exc:
            self.logger.debug(f"Failed Hamadi Abid list fallback for {reference}: {exc}")
        return None, {}

    def _breadcrumbs(self, product: Dict[str, Any], container: Dict[str, Any]) -> List[str]:
        breadcrumbs = []
        for source_key in ("section", "group", "subGroup"):
            source = container.get(source_key)
            if not isinstance(source, dict):
                source = product.get(source_key)
            if isinstance(source, dict):
                name = clean_text(source.get("name"))
                if name and name not in breadcrumbs:
                    breadcrumbs.append(name)
        return breadcrumbs

    async def scrape_product_details(self, url: str) -> dict:
        html = await super().fetch_html(url)
        if html and not (self.html_dir / "detail_sample_1.html").exists():
            save_text_atomic(html, self.html_dir / "detail_sample_1.html", self.logger)

        metadata = html_product_metadata(html or "", url, self.web_origin) if html else {}
        product, container = await self._fetch_product_detail(url)
        if not product:
            fallback = {
                "url": url,
                "title": metadata.get("title"),
                "name": metadata.get("title") or metadata.get("name"),
                **metadata,
            }
            return finalize_product_record({k: v for k, v in fallback.items() if v not in (None, "", [], {})})

        price, old_price, discount_percent = self._price_tuple(product)
        stock = self._stock_summary(product)
        images = self._all_images(product, stock)
        reference = clean_text(product.get("ref"))
        description = clean_text(product.get("description") or metadata.get("description"))
        composition = clean_text(product.get("composition"))
        breadcrumbs = self._breadcrumbs(product, container)
        barcode = self._first_gtin(product)

        specs = {
            "Ref Produit": reference,
            "Season": clean_text(product.get("itemSeasonName")),
            "Composition": composition,
            "Section": breadcrumbs[0] if len(breadcrumbs) > 0 else None,
            "Group": breadcrumbs[1] if len(breadcrumbs) > 1 else None,
            "Subgroup": breadcrumbs[2] if len(breadcrumbs) > 2 else None,
            "Colors": stock.get("colors"),
            "Sizes": stock.get("sizes"),
            "Stock total": stock.get("quantity") if stock.get("has_stock_rows") else None,
        }
        specs = {key: value for key, value in specs.items() if value not in (None, "", [], {})}

        record: Dict[str, Any] = {
            "url": normalize_url(self._product_url(product) or url) or url,
            "id": clean_text(product.get("id")),
            "product_id": clean_text(product.get("id")),
            "title": clean_text(product.get("title") or metadata.get("title")),
            "name": clean_text(product.get("title") or metadata.get("title")),
            "sku": reference,
            "reference": reference,
            "barcode": barcode,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "availability": stock.get("availability"),
            "available": stock.get("available"),
            "short_description": description,
            "description": description,
            "full_description": description,
            "composition": composition,
            "season": clean_text(product.get("itemSeasonName")),
            "specifications": specs,
            "images": images,
            "image": images[0] if images else self._image_url(product.get("image")),
            "breadcrumbs": breadcrumbs,
            "colors": stock.get("colors"),
            "sizes": stock.get("sizes"),
            "variants": stock.get("variants"),
            "stock": {
                "quantity": stock.get("quantity"),
                "sizes": stock.get("sizes"),
                "colors": stock.get("colors"),
            }
            if stock.get("has_stock_rows")
            else None,
        }
        brand = self._brand_value(product.get("brand"))
        if brand:
            record["brand"] = brand

        return finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})


def get_scraper(logger: logging.Logger) -> HamadiAbidScraper:
    return HamadiAbidScraper(logger)
