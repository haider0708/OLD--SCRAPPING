#!/usr/bin/env python3
"""
Kieslect scraper - WordPress/WooCommerce storefront, HTTP/selectolax.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
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


class KieslectScraper(FastScraper):
    """HTTP scraper for kieslect.tn WooCommerce pages and Store API."""

    CATEGORY_PATH_PREFIX = "/product-category/"
    PRODUCT_PATH_PREFIX = "/product/"
    SITE_HOSTS = {"kieslect.tn", "www.kieslect.tn"}
    LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
    BAD_CATEGORY_PARTS = (
        "/account",
        "/author/",
        "/blog",
        "/cart",
        "/checkout",
        "/contact",
        "/feed",
        "/login",
        "/mon-compte",
        "/my-account",
        "/order",
        "/page/",
        "/panier",
        "/product/",
        "/product-tag/",
        "/search",
        "/tag/",
        "/wishlist",
        "/wp-content/",
        "/wp-json/",
        "add-to-cart",
        "mailto:",
        "tel:",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("kieslect", logger)
        self.headers.update(self.config.get("headers", {}))
        self._category_cache: Optional[Dict[str, Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

    @staticmethod
    def _clean(value: Any) -> Optional[str]:
        return clean_text(value)

    @staticmethod
    def _as_selectors(value: Any) -> List[str]:
        if isinstance(value, list):
            out: List[str] = []
            for item in value:
                out.extend(KieslectScraper._as_selectors(item))
            return out
        if not value:
            return []
        return [part.strip() for part in str(value).split(",") if part.strip()]

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

    @classmethod
    def _first(cls, root: Any, selectors: Iterable[Any]) -> Any:
        for selector in selectors:
            for css in cls._as_selectors(selector):
                node = root.css_first(css)
                if node:
                    return node
        return None

    @staticmethod
    def _first_srcset_url(value: Any, prefer_largest: bool = True) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        entries = [chunk.strip() for chunk in text.split(",") if chunk.strip()]
        if not entries:
            return None
        if prefer_largest:
            parsed = []
            for entry in entries:
                parts = entry.split()
                width = 0
                if len(parts) > 1:
                    match = re.match(r"(\d+)w$", parts[1])
                    if match:
                        width = int(match.group(1))
                parsed.append((width, parts[0]))
            chosen = max(parsed, key=lambda item: item[0])[1] if parsed else entries[-1]
            return chosen
        chosen = entries[0]
        return chosen.split(" ", 1)[0] if chosen else None

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
    def _clean_html_text(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = html_lib.unescape(text)
        text = re.sub(r"<[^>]+>", " ", text)
        return clean_text(text)

    @staticmethod
    def _post_id_from_class(class_name: Any) -> Optional[str]:
        match = re.search(r"(?:^|\s)(?:post|postid)-(\d+)(?:\s|$)", str(class_name or ""))
        return match.group(1) if match else None

    @classmethod
    def _body_product_id(cls, tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        return cls._post_id_from_class(body.attributes.get("class", "") if body else "")

    @staticmethod
    def _name_from_url(url: str) -> str:
        slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        return re.sub(r"[-_]+", " ", slug).strip().title() or slug

    @staticmethod
    def _discount_text(value: Any) -> Optional[float]:
        text = clean_text(value)
        if not text:
            return None
        match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", text)
        if not match:
            return None
        return parse_price(match.group(1))

    @staticmethod
    def _computed_discount(price: Optional[float], old_price: Optional[float]) -> Optional[float]:
        if price is None or old_price is None or old_price <= 0 or price >= old_price:
            return None
        return round(((old_price - price) / old_price) * 100, 2)

    @staticmethod
    def _meaningful_reference(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text or text.upper() in {"N/A", "NA", "SKU"}:
            return None
        if len(text) > 80:
            return None
        return text.strip(" :;|-")

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        return urlsplit(url).netloc.lower() in self.SITE_HOSTS

    def _canonical_site_url(self, parts: Any, path: str, query: str = "") -> str:
        scheme = parts.scheme.lower() if parts.scheme else "https"
        return urlunsplit((scheme, "kieslect.tn", path, query, ""))

    def _product_url(self, href: Any) -> Optional[str]:
        url = self._absolute_url(href)
        if not url:
            return None
        parts = urlsplit(url)
        if parts.netloc.lower() not in self.SITE_HOSTS:
            return None
        if "add-to-cart" in (parts.query or "").lower():
            return None
        path = parts.path.rstrip("/")
        if not path.startswith(self.PRODUCT_PATH_PREFIX) or path == self.PRODUCT_PATH_PREFIX.rstrip("/"):
            return None
        if "." in path.rsplit("/", 1)[-1]:
            return None
        return self._canonical_site_url(parts, path + "/")

    def _category_url(self, href: Any) -> Optional[str]:
        url = self._absolute_url(href)
        if not url:
            return None
        parts = urlsplit(url)
        if parts.netloc.lower() not in self.SITE_HOSTS:
            return None
        path = parts.path.rstrip("/")
        low = f"{path}?{parts.query}".lower()
        if not path.startswith(self.CATEGORY_PATH_PREFIX):
            return None
        if path == self.CATEGORY_PATH_PREFIX.rstrip("/"):
            return None
        if any(token in low for token in self.BAD_CATEGORY_PARTS):
            return None
        match = re.match(r"^/product-category/([^/]+)/?$", path, flags=re.I)
        if not match:
            return None
        slug = match.group(1).lower()
        if slug in {"uncategorized", "non-classe"}:
            return None
        return self._canonical_site_url(parts, f"/product-category/{slug}/")

    def _category_slug(self, url: Any) -> Optional[str]:
        category_url = self._category_url(url)
        if not category_url:
            return None
        return urlsplit(category_url).path.rstrip("/").rsplit("/", 1)[-1].lower()

    def _page_num_from_url(self, url: Any) -> int:
        parts = urlsplit(str(url or ""))
        match = re.search(r"/page/(\d+)/?", parts.path)
        if match:
            return max(1, int(match.group(1)))
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in {"page", "paged", "product-page"} and str(value).isdigit():
                return max(1, int(value))
        return 1

    def _price_from_node(self, node: Any) -> Optional[float]:
        if not node:
            return None
        return parse_price(
            self._attr(node, "content")
            or self._attr(node, "value")
            or self._text(node)
        )

    def _price_pair(
        self,
        root: Any,
        current_selectors: Iterable[Any],
        old_selectors: Iterable[Any],
        fallback_selectors: Iterable[Any],
    ) -> Tuple[Optional[float], Optional[float]]:
        current = None
        old = None
        for selector in current_selectors:
            for css in self._as_selectors(selector):
                current = self._price_from_node(root.css_first(css))
                if current is not None:
                    break
            if current is not None:
                break
        for selector in old_selectors:
            for css in self._as_selectors(selector):
                old = self._price_from_node(root.css_first(css))
                if old is not None:
                    break
            if old is not None:
                break
        if current is None:
            for selector in fallback_selectors:
                for css in self._as_selectors(selector):
                    node = root.css_first(css)
                    if not node or node.css_first("ins") or node.css_first("del"):
                        continue
                    current = self._price_from_node(node)
                    if current is not None:
                        break
                if current is not None:
                    break
        return current, old

    def _image_from_node(self, node: Any) -> Optional[str]:
        if not node:
            return None

        for attr in (
            "data-large_image",
            "data-src",
            "data-lazy-src",
            "srcset",
            "data-srcset",
            "src",
            "content",
        ):
            candidate = node.attributes.get(attr)
            if attr in {"srcset", "data-srcset"}:
                candidate = self._first_srcset_url(candidate, prefer_largest=True)
            url = self._absolute_url(candidate)
            if not url:
                continue
            low = url.lower()
            if low.startswith("data:") or "placeholder" in low or "logo" in low:
                continue
            return url
        return None

    def _availability(
        self,
        text: Any = None,
        classes: Any = "",
        add_button: Any = None,
        api_available: Optional[bool] = None,
    ) -> Tuple[Optional[str], Optional[bool]]:
        stock_text = clean_text(text)
        cls = str(classes or "").lower()
        combined = f"{stock_text or ''} {cls}".lower()
        if "outofstock" in combined or "out-of-stock" in combined or "rupture" in combined:
            return stock_text or "Rupture de stock", False
        if "instock" in combined or "in-stock" in combined or "en stock" in combined:
            return stock_text or "En stock", True

        if add_button:
            disabled = self._attr(add_button, "disabled") or self._attr(add_button, "aria-disabled")
            button_text = self._text(add_button)
            if disabled:
                return button_text or stock_text or "Rupture de stock", False
            if button_text and re.search(r"ajouter|add to cart|commander|acheter", button_text, re.I):
                return stock_text or "En stock", True

        availability, available = availability_from_text(stock_text)
        if available is not None or availability:
            return availability, available
        if api_available is True:
            return "En stock", True
        if api_available is False:
            return "Rupture de stock", False
        return None, None

    # ------------------------------------------------------------------
    # Frontpage/category discovery
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"Downloading Kieslect frontpage: {self.base_url}")
        html = await self.fetch_html(self.base_url, raise_on_error=True)
        save_text_atomic(html, output_path, self.logger)

        frontpage = self.selectors.get("frontpage", {})
        extra_fetches = {
            "robots.txt": frontpage.get("robots_url"),
            "sitemap_index.xml": frontpage.get("sitemap_index_url"),
            "product_cat-sitemap.xml": frontpage.get("product_cat_sitemap_url"),
            "store_categories.json": self.config.get("api", {}).get("store_categories"),
        }
        for filename, url in extra_fetches.items():
            if not url:
                continue
            meta = await self.fetch_html_with_meta(url)
            text = meta.get("html")
            if not text:
                self.logger.debug(f"Failed Kieslect auxiliary fetch: {url} ({meta.get('error')})")
                continue
            save_text_atomic(text, self.html_dir / filename, self.logger)

        return output_path

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        category_map = self._load_category_map()
        discovered = self._category_links_from_html(tree)
        sitemap_links = self._category_links_from_sitemaps()

        categories: List[Dict[str, Any]] = []
        seen = set()

        for slug, item in category_map.items():
            url = self._category_url(item.get("permalink") or item.get("link")) or (
                f"https://kieslect.tn/product-category/{slug}/"
            )
            if not url or url in seen:
                continue
            seen.add(url)
            categories.append(
                {
                    "name": clean_text(item.get("name")) or self._name_from_url(url),
                    "url": url,
                    "level": "top",
                    "category_id": str(item.get("id")),
                    "slug": slug,
                    "product_count_hint": item.get("count"),
                    "low_level_categories": [],
                    "discovery_method": "woocommerce_store_api",
                    "validated_on_frontpage": url in discovered,
                }
            )

        for url, name in discovered + sitemap_links:
            if url in seen:
                continue
            slug = self._category_slug(url)
            if not slug or (category_map and slug not in category_map):
                continue
            seen.add(url)
            categories.append(
                {
                    "name": name or self._name_from_url(url),
                    "url": url,
                    "level": "top",
                    "category_id": str(category_map.get(slug, {}).get("id") or ""),
                    "slug": slug,
                    "low_level_categories": [],
                    "discovery_method": "frontpage_or_sitemap",
                }
            )

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _load_category_map(self) -> Dict[str, Dict[str, Any]]:
        if self._category_cache is not None:
            return self._category_cache

        payload: Any = None
        cache_path = self.html_dir / "store_categories.json"
        if cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self.logger.debug(f"Failed reading Kieslect Store API category cache: {exc}")

        if payload is None:
            api_url = self.config.get("api", {}).get("store_categories")
            if api_url:
                try:
                    with httpx.Client(headers=self.headers, follow_redirects=True, timeout=20) as client:
                        response = client.get(api_url, headers={"Accept": "application/json,*/*;q=0.8"})
                        response.raise_for_status()
                        payload = response.json()
                except Exception as exc:
                    self.logger.debug(f"Kieslect Store API category fallback failed: {exc}")
                    payload = []

        category_map: Dict[str, Dict[str, Any]] = {}
        for item in payload if isinstance(payload, list) else []:
            slug = clean_text(item.get("slug"))
            name = clean_text(item.get("name"))
            count = item.get("count") or 0
            try:
                count = int(count)
            except (TypeError, ValueError):
                count = 0
            if not slug or slug.lower() in {"uncategorized", "non-classe"} or count <= 0:
                continue
            if name and "uncategorized" in name.lower():
                continue
            category_map[slug.lower()] = item

        self._category_cache = category_map
        return category_map

    def _category_links_from_html(self, tree: HTMLParser) -> List[Tuple[str, str]]:
        selector = self.selectors.get("frontpage", {}).get(
            "category_links",
            "header a[href*='/product-category/'], footer a[href*='/product-category/']",
        )
        links: List[Tuple[str, str]] = []
        seen = set()
        for link in tree.css(selector):
            url = self._category_url(link.attributes.get("href"))
            if not url or url in seen:
                continue
            seen.add(url)
            name = self._text(link) or self._name_from_url(url)
            links.append((url, name))
        return links

    def _category_links_from_sitemaps(self) -> List[Tuple[str, str]]:
        links: List[Tuple[str, str]] = []
        seen = set()
        for path in self.html_dir.glob("*sitemap*.xml"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for loc in self.LOC_RE.findall(text or ""):
                url = self._category_url(html_lib.unescape(loc.strip()))
                if not url or url in seen:
                    continue
                seen.add(url)
                links.append((url, self._name_from_url(url)))
        return links

    @staticmethod
    def _category_stats(categories: List[Dict[str, Any]]) -> Dict[str, int]:
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["top_level"] += 1
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top.get("low_level_categories", []) or []:
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
                for sub in low.get("subcategories", []) or []:
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1
        return stats

    # ------------------------------------------------------------------
    # Product listings
    # ------------------------------------------------------------------

    async def scrape_category_page(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            api_result = await self._scrape_store_api_category_page(url)
            if api_result["products"]:
                return api_result
            return {
                "products": [],
                "pagination": {"current_page": self._page_num_from_url(url), "total_pages": 1, "has_next": False},
                "error": "Failed to fetch",
            }

        sample_path = self.html_dir / "listing_sample_1.html"
        if not sample_path.exists() and self._category_slug(url):
            save_text_atomic(html, sample_path, self.logger)

        try:
            products = self.extract_products_from_html(html)
            pagination = self.extract_pagination_from_html(html)
        except Exception as exc:
            self.logger.debug(f"Error parsing Kieslect category {url}: {exc}")
            return {
                "products": [],
                "pagination": {"current_page": self._page_num_from_url(url), "total_pages": 1, "has_next": False},
                "error": str(exc),
            }

        if products:
            return {"products": products, "pagination": pagination}

        api_result = await self._scrape_store_api_category_page(url)
        if api_result["products"]:
            return api_result
        return {"products": products, "pagination": pagination}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        selectors = cp.get("item_selectors") or [
            ".products .product.product-item",
            ".product-warp-item > .product",
            ".nasa-products-page-wrap .product-item.product",
            "div.product-item.product",
        ]

        cards = []
        for selector in self._as_selectors(selectors):
            selected = tree.css(selector)
            if selected:
                cards.extend(selected)
        if not cards:
            cards = tree.css(".products .product")

        products: List[Dict[str, Any]] = []
        for card in cards:
            product = self._product_from_card(card, cp)
            if product:
                products.append(product)

        return dedupe_products(products, self.logger, "kieslect listing")

    def _product_from_card(self, card: Any, selectors: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        class_name = card.attributes.get("class", "")
        product_id = self._post_id_from_class(class_name)

        id_node = self._first(card, selectors.get("item_id", []))
        if id_node:
            product_id = (
                self._attr(id_node, "data-product_id")
                or self._attr(id_node, "data-product-id")
                or self._attr(id_node, "value")
                or product_id
            )
        product_id = self._clean(product_id)

        link = self._first(card, selectors.get("item_url", []))
        url = self._product_url(self._attr(link, "href"))
        if not url:
            return None

        title = self._text(self._first(card, selectors.get("item_title", [])))
        if not title:
            title = self._attr(link, "title") or self._text(link)
        if not title:
            return None

        price, old_price = self._price_pair(
            card,
            selectors.get("item_current_price", []),
            selectors.get("item_old_price", []),
            [selectors.get("item_price", "span.price"), "span.price .woocommerce-Price-amount", "span.price"],
        )

        discount_text = self._text(self._first(card, selectors.get("item_discount", [])))
        discount_percent = self._discount_text(discount_text)
        if discount_percent is None:
            discount_percent = self._computed_discount(price, old_price)

        image_node = self._first(card, selectors.get("item_image", []))
        image = self._image_from_node(image_node)

        add_button = self._first(card, ["a.add_to_cart_button", "button[data-product_id]", "button"])
        availability, available = self._availability(classes=class_name, add_button=add_button)

        product: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": url,
            "name": title,
            "title": title,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": image,
            "brand": "Kieslect",
            "availability": availability,
            "available": available,
            "shop": self.site_name,
        }
        return finalize_product_record({k: v for k, v in product.items() if v is not None})

    async def _fetch_json_response(self, url: str) -> Tuple[Any, Dict[str, str], Optional[int]]:
        try:
            client = await self.get_client()
            response = await client.get(url, headers={"Accept": "application/json,*/*;q=0.8"})
            if response.status_code >= 400:
                self.logger.debug(f"Kieslect API HTTP {response.status_code}: {url}")
                return None, dict(response.headers), response.status_code
            return response.json(), dict(response.headers), response.status_code
        except Exception as exc:
            self.logger.debug(f"Kieslect API fetch failed {url}: {exc}")
            return None, {}, None

    async def _scrape_store_api_category_page(self, category_url: str) -> dict:
        slug = self._category_slug(category_url)
        category = self._load_category_map().get(slug or "")
        if not category:
            return {
                "products": [],
                "pagination": {"current_page": self._page_num_from_url(category_url), "total_pages": 1, "has_next": False},
            }

        page = self._page_num_from_url(category_url)
        per_page = int(self.config.get("settings", {}).get("api_per_page", 16))
        template = self.config.get("api", {}).get("products_by_category")
        api_url = template.format(category_id=category.get("id"), per_page=per_page, page=page)
        payload, headers, _status = await self._fetch_json_response(api_url)
        products = [
            product
            for product in (self._product_from_store_api(item) for item in payload if isinstance(payload, list))
            if product
        ]

        try:
            total_pages = int(headers.get("x-wp-totalpages") or headers.get("X-WP-TotalPages") or 1)
        except (TypeError, ValueError):
            total_pages = page + 1 if len(products) >= per_page else page
        total_pages = max(1, total_pages)

        return {
            "products": dedupe_products(products, self.logger, "kieslect api listing"),
            "pagination": {
                "current_page": page,
                "total_pages": total_pages,
                "has_next": page < total_pages,
            },
        }

    def _api_price_values(self, prices: Any) -> Tuple[Optional[float], Optional[float]]:
        if not isinstance(prices, dict):
            return None, None
        try:
            minor_unit = int(prices.get("currency_minor_unit", 0) or 0)
        except (TypeError, ValueError):
            minor_unit = 0
        divisor = 10 ** minor_unit if minor_unit > 0 else 1

        def parse_minor(key: str) -> Optional[float]:
            raw = prices.get(key)
            if raw in (None, ""):
                return None
            parsed = parse_price(raw)
            if parsed is None:
                return None
            return round(parsed / divisor, minor_unit if minor_unit <= 3 else 3)

        price = parse_minor("price") or parse_minor("sale_price") or parse_minor("regular_price")
        regular = parse_minor("regular_price")
        sale = parse_minor("sale_price")
        old_price = regular if regular and price and regular > price else None
        if old_price is None and sale and regular and regular > sale:
            price = sale
            old_price = regular
        return price, old_price

    def _product_from_store_api(self, item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        product_id = self._clean(item.get("id"))
        name = clean_text(item.get("name"))
        url = self._product_url(item.get("permalink"))
        if not name or not url:
            return None

        price, old_price = self._api_price_values(item.get("prices"))
        discount_percent = self._computed_discount(price, old_price)

        images = []
        for image in item.get("images") or []:
            if isinstance(image, dict):
                image_url = self._absolute_url(image.get("src") or image.get("thumbnail"))
                if image_url:
                    images.append(image_url)
        images = self._dedupe_values(images)

        stock = item.get("stock_availability") if isinstance(item.get("stock_availability"), dict) else {}
        availability_text = stock.get("text") or stock.get("class")
        availability, available = self._availability(
            text=availability_text,
            classes=stock.get("class"),
            api_available=item.get("is_in_stock"),
        )

        sku = self._meaningful_reference(item.get("sku"))
        barcode = normalize_gtin(sku) if sku else None
        reference = None if barcode else sku

        brands = item.get("brands") if isinstance(item.get("brands"), list) else []
        brand = None
        for brand_item in brands:
            if isinstance(brand_item, dict):
                brand = clean_text(brand_item.get("name"))
                if brand:
                    break
        brand = brand or "Kieslect"

        categories = []
        category_urls = []
        for cat in item.get("categories") or []:
            if not isinstance(cat, dict):
                continue
            cat_name = clean_text(cat.get("name"))
            cat_url = self._category_url(cat.get("link"))
            if cat_name:
                categories.append(cat_name)
            if cat_url:
                category_urls.append(cat_url)

        specs = self._specs_from_api_attributes(item.get("attributes"))
        short_description = self._clean_html_text(item.get("short_description"))
        description = self._clean_html_text(item.get("description")) or short_description

        product: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": url,
            "name": name,
            "title": name,
            "sku": reference or barcode,
            "reference": reference,
            "barcode": barcode,
            "brand": brand,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": images[0] if images else None,
            "images": images or None,
            "availability": availability,
            "available": available,
            "short_description": short_description,
            "description": description,
            "specifications": specs or None,
            "categories": self._dedupe_values(categories) or None,
            "category_urls": self._dedupe_values(category_urls) or None,
            "tags": [clean_text(t.get("name")) for t in item.get("tags") or [] if isinstance(t, dict) and clean_text(t.get("name"))],
            "product_type": clean_text(item.get("type")),
            "shop": self.site_name,
        }
        if item.get("variations"):
            product["variation_ids"] = item.get("variations")
        return finalize_product_record({k: v for k, v in product.items() if v not in (None, "", [], {})})

    @staticmethod
    def _specs_from_api_attributes(attributes: Any) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for attr in attributes if isinstance(attributes, list) else []:
            if not isinstance(attr, dict):
                continue
            label = clean_text(attr.get("name") or attr.get("taxonomy"))
            terms = attr.get("terms")
            values = []
            for term in terms if isinstance(terms, list) else []:
                if isinstance(term, dict):
                    values.append(clean_text(term.get("name") or term.get("slug")))
                else:
                    values.append(clean_text(term))
            value = ", ".join([v for v in values if v])
            if label and value:
                specs[label] = value
        return specs

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        url = self._absolute_url(base_url) or base_url
        parts = urlsplit(url)
        path = re.sub(r"/page/\d+/?$", "/", parts.path)
        path = path.rstrip("/") + "/"

        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            lower = key.lower()
            if lower in {"page", "paged", "product-page", "srsltid"} or lower.startswith("utm_"):
                continue
            query.append((key, value))

        if page_num and page_num > 1:
            path = path.rstrip("/") + f"/page/{page_num}/"

        return urlunsplit((parts.scheme or "https", parts.netloc or "kieslect.tn", path, urlencode(query), ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        page_numbers = {1}
        current_page = 1

        current = tree.css_first(cp.get("pagination_current", ".page-numbers.current"))
        current_text = self._text(current)
        if current_text and current_text.isdigit():
            current_page = int(current_text)
            page_numbers.add(current_page)

        has_next = False
        for link in tree.css(cp.get("pagination_pages", "a.page-numbers[href], link[rel='next']")):
            text = self._text(link) or ""
            href = self._attr(link, "href") or ""
            class_name = link.attributes.get("class", "") or ""
            rel = link.attributes.get("rel", "") or ""
            if "next" in class_name.lower() or "next" in rel.lower():
                has_next = True
            if text.isdigit():
                page_numbers.add(int(text))
            match = re.search(r"/page/(\d+)/?", href)
            if match:
                page_numbers.add(int(match.group(1)))

        if tree.css_first(cp.get("pagination_next", "a.next.page-numbers[href], link[rel='next']")):
            has_next = True

        total_pages = max(page_numbers) if page_numbers else current_page
        max_pages = int(self.config.get("settings", {}).get("max_pagination_pages", 80))
        total_pages = min(max(total_pages, current_page), max_pages)
        if current_page >= total_pages:
            has_next = False

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next or current_page < total_pages,
        }

    # ------------------------------------------------------------------
    # Product detail
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch product"}

        sample_path = self.html_dir / "detail_sample_1.html"
        if not sample_path.exists():
            save_text_atomic(html, sample_path, self.logger)

        tree = HTMLParser(html)
        data: Dict[str, Any] = html_product_metadata(html, url, self.base_url)
        data["url"] = self._product_url(data.get("url") or url) or url
        data["shop"] = self.site_name

        product_id = self._detail_product_id(tree, data)
        api_product = await self._fetch_product_api(product_id) if product_id else None
        if api_product:
            self._apply_api_product(api_product, data)

        self._apply_detail_identifiers(tree, data)
        self._apply_detail_title_price(tree, data)
        self._apply_detail_availability(tree, data)
        self._apply_detail_descriptions(tree, data)
        self._apply_detail_specs_and_variants(tree, data)
        self._apply_detail_images(tree, data)
        self._apply_detail_categories(tree, data)

        data.setdefault("brand", "Kieslect")
        gtins = extract_gtins_from_text(
            " ".join(
                str(value)
                for value in [
                    data.get("description"),
                    json.dumps(data.get("specifications") or {}, ensure_ascii=True),
                    json.dumps(data.get("variants") or [], ensure_ascii=True),
                ]
            )
        )
        if gtins:
            data.setdefault("barcode", gtins[0])

        return finalize_product_record({k: v for k, v in data.items() if v not in (None, "", [], {})})

    def _detail_product_id(self, tree: HTMLParser, data: Dict[str, Any]) -> Optional[str]:
        pp = self.selectors.get("product_page", {})
        product_id = self._body_product_id(tree)
        id_node = self._first(tree, pp.get("product_id", []))
        if id_node:
            product_id = (
                self._attr(id_node, "value")
                or self._attr(id_node, "data-product_id")
                or self._attr(id_node, "data-product-id")
                or product_id
            )
        product_id = clean_text(product_id or data.get("product_id") or data.get("id"))
        if product_id:
            data["id"] = product_id
            data["product_id"] = product_id
        return product_id

    async def _fetch_product_api(self, product_id: Optional[str]) -> Optional[dict]:
        if not product_id:
            return None
        template = self.config.get("api", {}).get("product_by_id")
        if not template:
            return None
        payload, _headers, _status = await self._fetch_json_response(template.format(product_id=product_id))
        return payload if isinstance(payload, dict) else None

    def _apply_api_product(self, api_product: Dict[str, Any], data: Dict[str, Any]) -> None:
        api_data = self._product_from_store_api(api_product)
        if not api_data:
            return
        for key, value in api_data.items():
            if value in (None, "", [], {}):
                continue
            if key in {
                "id",
                "product_id",
                "name",
                "title",
                "brand",
                "price",
                "old_price",
                "discount_percent",
                "availability",
                "available",
                "image",
                "images",
                "categories",
                "category_urls",
                "specifications",
                "short_description",
                "description",
            }:
                data[key] = value
            else:
                data.setdefault(key, value)

    def _apply_detail_identifiers(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        sku_node = self._first(tree, pp.get("sku", []))
        sku = self._meaningful_reference(self._attr(sku_node, "content") or self._text(sku_node))
        if sku:
            gtin = normalize_gtin(sku)
            if gtin:
                data.setdefault("barcode", gtin)
            else:
                data["reference"] = sku
                data["sku"] = sku

    def _apply_detail_title_price(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        title = self._text(self._first(tree, pp.get("title", [])))
        if title:
            data["title"] = title
            data["name"] = title

        price, old_price = self._price_pair(
            tree,
            pp.get("current_price", []),
            pp.get("old_price", []),
            [pp.get("price", ""), "p.price .woocommerce-Price-amount", ".summary .price .woocommerce-Price-amount", "p.price", ".summary .price"],
        )
        if price is not None:
            data["price"] = price
        if old_price is not None:
            data["old_price"] = old_price
        discount_percent = self._computed_discount(data.get("price"), data.get("old_price"))
        if discount_percent is not None:
            data["discount_percent"] = discount_percent

    def _apply_detail_availability(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        availability_node = self._first(tree, pp.get("availability", []))
        add_button = self._first(tree, ["button[name='add-to-cart']", ".single_add_to_cart_button"])
        body = tree.css_first("body")
        body_classes = body.attributes.get("class", "") if body else ""
        availability, available = self._availability(
            text=self._text(availability_node) or data.get("availability"),
            classes=body_classes,
            add_button=add_button,
            api_available=data.get("available"),
        )
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

    def _apply_detail_descriptions(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        short_description = self._text(tree.css_first(pp.get("short_description", "")))
        descriptions = []
        for selector in self._as_selectors(pp.get("description", "")):
            for node in tree.css(selector):
                text = self._text(node)
                if text:
                    descriptions.append(text)
        descriptions = self._dedupe_values(descriptions)
        if short_description:
            data["short_description"] = short_description
        if descriptions:
            data["description"] = descriptions[0]
        elif short_description:
            data.setdefault("description", short_description)

    def _apply_detail_specs_and_variants(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        specs: Dict[str, str] = dict(data.get("specifications") or {})

        for row in tree.css(pp.get("specs_rows", "table.shop_attributes tr")):
            cells = row.css("th, td")
            if len(cells) < 2:
                continue
            key = self._text(cells[0])
            value = self._text(cells[-1])
            if not key or not value or key == value:
                continue
            key = key.rstrip(":")
            specs[key] = value
            self._apply_identifier_from_spec(key, value, data)

        options = self._variant_options(tree)
        if options:
            specs.update({f"Option {key}": ", ".join(values) for key, values in options.items() if values})
            data["options"] = options

        variants = self._parse_variations(tree)
        if variants:
            data["variants"] = variants
            data["variations"] = variants
            for variant in variants:
                if variant.get("sku") and not data.get("reference"):
                    data["reference"] = variant["sku"]
                    data["sku"] = variant["sku"]
                if variant.get("barcode") and not data.get("barcode"):
                    data["barcode"] = variant["barcode"]

        if specs:
            data["specifications"] = specs
            data["specs"] = specs

    def _apply_identifier_from_spec(self, key: str, value: str, data: Dict[str, Any]) -> None:
        if re.search(r"brand|marque", key, re.I):
            data.setdefault("brand", value)
        elif re.search(r"ean|gtin|code\s*bar|barcode", key, re.I):
            gtin = normalize_gtin(value)
            if gtin:
                data.setdefault("barcode", gtin)
        elif re.search(r"sku|mpn|model|reference|ref", key, re.I):
            gtin = normalize_gtin(value)
            if gtin:
                data.setdefault("barcode", gtin)
            else:
                reference = self._meaningful_reference(value)
                if reference:
                    data.setdefault("reference", reference)
                    data.setdefault("sku", reference)

    def _variant_options(self, tree: HTMLParser) -> Dict[str, List[str]]:
        options: Dict[str, List[str]] = {}
        for select in tree.css("form.variations_form select[name^='attribute_']"):
            raw_name = self._attr(select, "name") or ""
            label = re.sub(r"^attribute_(?:pa_)?", "", raw_name).replace("-", " ").replace("_", " ").title()
            values = []
            for option in select.css("option"):
                value = self._text(option)
                if value and not re.search(r"choisir|choose|select", value, re.I):
                    values.append(value)
            if label and values:
                options[label] = self._dedupe_values(values)
        return options

    def _parse_variations(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        form = tree.css_first("form.variations_form[data-product_variations]")
        raw = self._attr(form, "data-product_variations")
        if not raw:
            return []
        try:
            parsed = json.loads(html_lib.unescape(raw))
        except (TypeError, json.JSONDecodeError):
            return []
        variants: List[Dict[str, Any]] = []
        for item in parsed if isinstance(parsed, list) else []:
            if not isinstance(item, dict):
                continue
            sku = self._meaningful_reference(item.get("sku"))
            barcode = normalize_gtin(sku) if sku else None
            image = item.get("image") if isinstance(item.get("image"), dict) else {}
            image_url = self._absolute_url(
                image.get("full_src")
                or image.get("src")
                or image.get("url")
                or image.get("thumb_src")
            )
            attrs = {}
            for key, value in (item.get("attributes") or {}).items():
                label = re.sub(r"^attribute_(?:pa_)?", "", str(key)).replace("-", " ").replace("_", " ").title()
                attrs[label] = clean_text(value)
            availability_text = self._clean_html_text(item.get("availability_html"))
            variant = {
                "id": clean_text(item.get("variation_id")),
                "sku": None if barcode else sku,
                "barcode": barcode,
                "attributes": {k: v for k, v in attrs.items() if v},
                "price": parse_price(item.get("display_price")),
                "old_price": parse_price(item.get("display_regular_price")),
                "available": item.get("is_in_stock"),
                "availability": availability_text,
                "image": image_url,
            }
            variants.append({k: v for k, v in variant.items() if v not in (None, "", [], {})})
        return variants

    def _apply_detail_images(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        images = list(data.get("images") or [])
        for node in tree.css(pp.get("image_gallery", "img.wp-post-image, .woocommerce-product-gallery img")):
            image = self._image_from_node(node)
            if image:
                images.append(image)
        for variant in data.get("variants") or []:
            if isinstance(variant, dict) and variant.get("image"):
                images.append(variant["image"])
        images = self._dedupe_values(images)
        if images:
            data["images"] = images
            data["image"] = images[0]

    def _apply_detail_categories(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        categories = list(data.get("categories") or [])
        category_urls = list(data.get("category_urls") or [])
        breadcrumbs = []
        seen_breadcrumbs = set()
        for link in tree.css(pp.get("breadcrumbs", "")):
            name = self._text(link)
            href = self._category_url(link.attributes.get("href"))
            if not name or name.lower() in {"accueil", "home"}:
                continue
            key = (name, href)
            if key in seen_breadcrumbs:
                continue
            seen_breadcrumbs.add(key)
            breadcrumbs.append({"name": name, "url": href})
            if href:
                categories.append(name)
                category_urls.append(href)
        if breadcrumbs:
            data["breadcrumbs"] = breadcrumbs
        if categories:
            data["categories"] = self._dedupe_values(categories)
        if category_urls:
            data["category_urls"] = self._dedupe_values(category_urls)


def get_scraper(logger: logging.Logger) -> KieslectScraper:
    """Factory used by scraper.sites registry."""
    return KieslectScraper(logger)
