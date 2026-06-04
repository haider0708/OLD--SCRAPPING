#!/usr/bin/env python3
"""
Sasio scraper - PrestaShop storefront, HTTP/selectolax.
"""

import asyncio
import html as html_lib
import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, detect_blocked_signals, is_blocked_response, save_text_atomic
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    clean_text,
    dedupe_products,
    extract_gtins_from_text,
    finalize_product_record,
    html_product_metadata,
    parse_price,
)


class SasioScraper(FastScraper):
    """HTTP scraper for sasio.com.tn PrestaShop pages."""

    BAD_CATEGORY_PARTS = (
        "account",
        "adresse",
        "addresses",
        "authentication",
        "brand",
        "brands",
        "cart",
        "checkout",
        "cms",
        "commande",
        "contact",
        "content",
        "connexion",
        "fabricant",
        "identity",
        "login",
        "magasins",
        "manufacturer",
        "module",
        "mon-compte",
        "order",
        "panier",
        "password",
        "recherche",
        "search",
        "sitemap",
        "social",
        "supplier",
        "wishlist",
        "blog",
    )
    PROMO_CATEGORY_PARTS = (
        "best-sales",
        "meilleures-ventes",
        "new-products",
        "nouveaux-produits",
        "promotions",
        "prices-drop",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("sasio", logger)
        self.headers.update(self.config.get("headers", {}))
        self._html_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

    @staticmethod
    def _text(node: Any) -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=" ", strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

    @staticmethod
    def _attr(node: Any, name: str) -> Optional[str]:
        if not node:
            return None
        return clean_text(node.attributes.get(name))

    @staticmethod
    def _first(root: Any, selectors: Iterable[str]) -> Any:
        for selector in selectors:
            node = root.css_first(selector)
            if node:
                return node
        return None

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

    @staticmethod
    def _clean_reference(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"^(r[e\u00e9]f[e\u00e9]rence|reference|sku)\s*:?", "", text, flags=re.I)
        return clean_text(text.strip("[] :"))

    def _category_url(self, href: Any, allow_promo: bool = False) -> Optional[str]:
        url = self._absolute_url(href)
        if not url:
            return None

        parsed = urlsplit(url)
        host = parsed.netloc.lower()
        path = parsed.path.rstrip("/")
        lower_url = f"{path}?{parsed.query}".lower()
        if host not in {"sasio.com.tn", "www.sasio.com.tn"}:
            return None
        if path.endswith(".html"):
            return None
        if any(part in lower_url for part in self.BAD_CATEGORY_PARTS):
            return None
        if any(part in lower_url for part in self.PROMO_CATEGORY_PARTS) and not allow_promo:
            return None
        if not re.search(r"^/\d+-", path, re.I):
            return None

        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    def _product_url(self, href: Any) -> Optional[str]:
        url = self._absolute_url(href)
        if not url:
            return None
        parsed = urlsplit(url)
        if parsed.netloc.lower() not in {"sasio.com.tn", "www.sasio.com.tn"}:
            return None
        if not parsed.path.endswith(".html"):
            return None
        if not re.search(r"/\d+(?:-\d+)?-[^/]+\.html$", parsed.path):
            return None
        return url

    @staticmethod
    def _fetch_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))

    def _cache_key(self, url: str) -> str:
        parts = urlsplit(self._fetch_url(url))
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))

    @staticmethod
    def _product_ids_from_url(url: Any) -> Tuple[Optional[str], Optional[str]]:
        path = urlsplit(str(url or "")).path
        match = re.search(r"/(\d+)(?:-(\d+))?-[^/]+\.html$", path)
        if not match:
            return None, None
        return match.group(1), match.group(2)

    def _price_from_node(self, node: Any) -> Optional[float]:
        if not node:
            return None
        return parse_price(
            self._attr(node, "content")
            or self._attr(node, "value")
            or self._text(node)
        )

    def _image_from_node(self, node: Any) -> Optional[str]:
        if not node:
            return None
        for attr in ("data-full-size-image-url", "data-image-large-src", "data-src", "src"):
            url = self._absolute_url(node.attributes.get(attr))
            if url:
                return url
        return None

    @staticmethod
    def _discount_percent(value: Any) -> Optional[int]:
        text = clean_text(value)
        if not text:
            return None
        match = re.search(r"-?\s*(\d+(?:[.,]\d+)?)\s*%", text)
        if not match:
            return None
        parsed = parse_price(match.group(1))
        return int(round(parsed)) if parsed is not None else None

    @staticmethod
    def _computed_discount(price: Optional[float], old_price: Optional[float]) -> Optional[int]:
        if price is None or old_price is None or old_price <= 0 or old_price <= price:
            return None
        return int(round((1 - (price / old_price)) * 100))

    def _availability(self, value: Any, classes: Any = "", quantity: Any = None) -> Tuple[Optional[str], Optional[bool]]:
        text = clean_text(value)
        if text:
            text = clean_text(re.sub(r"[\ue000-\uf8ff]", " ", text))
        cls = str(classes or "").lower()
        combined = f"{text or ''} {cls}".lower()
        if any(token in combined for token in ("out_of_stock", "out-of-stock", "rupture", "indisponible", "unavailable")):
            return text or "Rupture de stock", False
        if any(
            token in combined
            for token in (
                "en stock",
                "in stock",
                "instock",
                "in_stock",
                "available",
                "disponible",
                "last_remaining_items",
                "dernier",
            )
        ):
            return text or "En stock", True

        parsed_quantity = parse_price(quantity)
        if parsed_quantity is not None:
            return ("En stock" if parsed_quantity > 0 else "Rupture de stock"), parsed_quantity > 0

        return availability_from_text(text)

    def _category_stats(self, categories: List[Dict[str, Any]]) -> Dict[str, int]:
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["top_level"] += 1
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top.get("low_level_categories", []):
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
                for sub in low.get("subcategories", []):
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1
        return stats

    def _valid_storefront_html(self, html: Optional[str]) -> bool:
        if not html:
            return False
        tree = HTMLParser(html)
        title = self._text(tree.css_first("title")) or ""
        if "500 server error" in title.lower():
            return False
        return bool(tree.css_first("#top-menu, #js-product-list, #product-details[data-product]"))

    def _server_error_html(self, html: Optional[str]) -> bool:
        if not html:
            return False
        if len(html) < 5000 and "500 server error" in html.lower():
            return True
        tree = HTMLParser(html)
        title = self._text(tree.css_first("title")) or ""
        return "500 server error" in title.lower()

    async def fetch_html_with_meta(
        self, url: str, raise_on_error: bool = False
    ) -> Dict[str, Any]:
        """Fetch Sasio pages with short retries for intermittent PrestaShop 500s."""
        started = time.monotonic()
        base_result = {
            "html": None,
            "status_code": None,
            "final_url": url,
            "content_type": None,
            "content_encoding": None,
            "attempts": 0,
            "elapsed_ms": 0,
            "blocked_signals": [],
            "error": None,
        }
        if not isinstance(url, str) or not url.strip():
            return {**base_result, "error": "empty_url"}
        if not url.startswith(("http://", "https://")):
            return {**base_result, "error": "invalid_url"}

        client = await self.get_client(0)
        last_exception: Optional[Exception] = None
        last_error = None
        last_status_code = None
        final_url = url
        content_type = None
        content_encoding = None
        blocked_signals: List[str] = []
        attempts = 0

        for attempt in range(1, self.retry_config.max_retries + 1):
            attempts = attempt
            try:
                response = await client.get(url)
                html = response.text
                last_status_code = response.status_code
                final_url = str(response.url)
                content_type = response.headers.get("content-type")
                content_encoding = response.headers.get("content-encoding")
                blocked_signals = detect_blocked_signals(html, response.status_code)

                if (
                    response.status_code == 200
                    and html
                    and html.strip()
                    and not self._server_error_html(html)
                    and not is_blocked_response(html, response.status_code)
                ):
                    result = {
                        "html": html,
                        "status_code": response.status_code,
                        "final_url": final_url,
                        "content_type": content_type,
                        "content_encoding": content_encoding,
                        "attempts": attempts,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "blocked_signals": blocked_signals,
                        "error": None,
                    }
                    self._html_cache[self._cache_key(url)] = html
                    return result

                last_error = f"HTTP {response.status_code}"
                if self._server_error_html(html):
                    last_error = "server_error_html"
                if response.status_code and 400 <= response.status_code < 500 and response.status_code != 429:
                    break
            except Exception as exc:
                last_exception = exc
                last_error = str(exc) or exc.__class__.__name__
                self.logger.debug("Error fetching %s: %s (attempt %s)", url, exc, attempt)

            if attempt < self.retry_config.max_retries:
                await asyncio.sleep(min(0.75 * attempt, 3.0))

        if raise_on_error:
            if last_exception:
                raise last_exception
            raise RuntimeError(last_error or "fetch_failed")

        cached = self._html_cache.get(self._cache_key(url))
        if cached:
            self.logger.warning("Using cached Sasio HTML for %s after fetch failure: %s", url, last_error)
            return {
                "html": cached,
                "status_code": last_status_code,
                "final_url": final_url,
                "content_type": content_type,
                "content_encoding": content_encoding,
                "attempts": attempts,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "blocked_signals": blocked_signals,
                "error": None,
            }

        self.logger.warning("Failed to fetch %s: %s", url, last_error or last_exception)
        return {
            "html": None,
            "status_code": last_status_code,
            "final_url": final_url,
            "content_type": content_type,
            "content_encoding": content_encoding,
            "attempts": attempts,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "blocked_signals": blocked_signals,
            "error": last_error or (str(last_exception) if last_exception else "fetch_failed"),
        }

    # ------------------------------------------------------------------
    # Frontpage/category discovery
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        output_path = self.html_dir / "frontpage.html"
        candidates = [self.base_url]
        for seed in self.config.get("seed_categories", []):
            url = seed.get("url") if isinstance(seed, dict) else seed
            if isinstance(url, str) and url not in candidates:
                candidates.append(url)

        last_error = None
        for url in candidates:
            self.logger.info("Downloading Sasio frontpage candidate: %s", url)
            meta = await self.fetch_html_with_meta(url)
            html = meta.get("html")
            if self._valid_storefront_html(html):
                save_text_atomic(html, output_path, self.logger)
                return output_path
            last_error = meta.get("error") or f"HTTP {meta.get('status_code')}"
            self.logger.debug("Sasio frontpage candidate failed: %s (%s)", url, last_error)

        raise RuntimeError(f"Failed to fetch a valid Sasio frontpage/menu page: {last_error}")

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories: List[Dict[str, Any]] = []
        seen_urls = set()

        for seed in self.config.get("seed_categories", []):
            if not isinstance(seed, dict):
                continue
            name = clean_text(seed.get("name"))
            url = self._category_url(seed.get("url"), allow_promo=True)
            if not name or not url or url in seen_urls:
                continue
            categories.append(
                {"name": name, "url": url, "level": "top", "low_level_categories": []}
            )
            seen_urls.add(url)

        top_selector = fp.get(
            "top_level_category",
            "#top-menu > li.category > a.dropdown-item[data-depth='0'][href]",
        )
        for top_link in tree.css(top_selector):
            top_name = self._text(top_link)
            top_url = self._category_url(self._attr(top_link, "href"))
            if not top_name:
                continue

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }
            if top_url:
                seen_urls.add(top_url)

            top_li = getattr(top_link, "parent", None)
            seen_low_urls = set()
            child_selector = fp.get(
                "nested_category",
                "li.category .popover.sub-menu a.dropdown-submenu[href], #top-menu ul.top-menu[data-depth='1'] > li.category > a[href]",
            )
            for low_link in top_li.css(child_selector) if top_li else []:
                low_name = self._text(low_link)
                low_url = self._category_url(self._attr(low_link, "href"))
                if not low_name or not low_url or low_url in seen_low_urls:
                    continue
                low_cat = {
                    "name": low_name,
                    "url": low_url,
                    "level": "low",
                    "subcategories": [],
                }
                top_cat["low_level_categories"].append(low_cat)
                seen_low_urls.add(low_url)
                seen_urls.add(low_url)

            if (top_url or top_cat["low_level_categories"]) and top_url not in seen_urls - {top_url}:
                duplicate_top = top_url and any(cat.get("url") == top_url for cat in categories)
                if not duplicate_top:
                    categories.append(top_cat)

        if not categories:
            fallback_lows = []
            for link in tree.css(fp.get("fallback_links", "a[href]")):
                name = self._text(link)
                url = self._category_url(self._attr(link, "href"), allow_promo=True)
                if not name or not url or url in seen_urls:
                    continue
                fallback_lows.append(
                    {"name": name, "url": url, "level": "low", "subcategories": []}
                )
                seen_urls.add(url)
            if fallback_lows:
                categories.append(
                    {
                        "name": "Catalogue",
                        "url": None,
                        "level": "top",
                        "low_level_categories": fallback_lows,
                    }
                )

        stats = self._category_stats(categories)
        self.logger.info(
            "Found %s Sasio top categories (%s queued URLs)",
            stats["top_level"],
            stats["total_urls"],
        )
        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if page_num <= 1:
            query.pop("page", None)
        else:
            query["page"] = str(page_num)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    async def scrape_all_pages(
        self,
        category_url: str,
        limit: int = None,
    ) -> List[dict]:
        all_products: List[dict] = []

        result = await self.scrape_category_page(category_url)
        if result.get("error"):
            return []

        all_products.extend(result["products"])
        if limit and len(all_products) >= limit:
            return all_products[:limit]

        total_pages = result.get("pagination", {}).get("total_pages", 1)
        for page_num in range(2, total_pages + 1):
            page_url = self.build_page_url(category_url, page_num)
            page_result = await self.scrape_category_page(page_url)
            if page_result.get("error"):
                self.logger.debug("Sasio page %s failed for %s: %s", page_num, category_url, page_result.get("error"))
                continue
            all_products.extend(page_result["products"])
            if limit and len(all_products) >= limit:
                break

        return all_products[:limit] if limit else all_products

    async def scrape_category_page(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {
                "products": [],
                "pagination": {"total_pages": 1},
                "error": "Failed to fetch",
            }

        sample_path = self.html_dir / "listing_sample_1.html"
        if not sample_path.exists() and "product-miniature" in html:
            save_text_atomic(html, sample_path, self.logger)

        try:
            products = self.extract_products_from_html(html)
            pagination = self.extract_pagination_from_html(html)
        except Exception as exc:
            self.logger.debug("Error parsing %s: %s", url, exc)
            return {"products": [], "pagination": {"total_pages": 1}, "error": str(exc)}

        return {"products": products, "pagination": pagination}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        items = tree.css(
            cp.get(
                "item_selector",
                "#js-product-list .product-miniature.js-product-miniature, .product-miniature",
            )
        )
        if not items:
            items = tree.css(".product-miniature, article.product-miniature")

        products: List[Dict[str, Any]] = []
        for item in items:
            product_id = clean_text(item.attributes.get("data-id-product"))
            attribute_id = clean_text(item.attributes.get("data-id-product-attribute"))

            link = self._first(
                item,
                [
                    "a.thumbnail.product-thumbnail[href]",
                    "h3.product-title a[href]",
                    ".product-title a[href]",
                    "a[href$='.html']",
                ],
            )
            product_url = self._product_url(self._attr(link, "href"))
            if not product_url:
                continue

            url_product_id, url_attribute_id = self._product_ids_from_url(product_url)
            product_id = product_id or url_product_id
            attribute_id = attribute_id or url_attribute_id

            name_node = self._first(
                item,
                [
                    "h3.product-title a",
                    ".product-title a",
                    "h3 a",
                    "a.thumbnail.product-thumbnail",
                ],
            )
            name = self._text(name_node) or self._attr(link, "title")

            price = self._price_from_node(
                self._first(item, [".product-price-and-shipping .price", ".product-price", ".price"])
            )
            old_price = self._price_from_node(
                self._first(item, [".product-price-and-shipping .regular-price", ".regular-price", ".old-price"])
            )
            discount_percent = self._discount_percent(
                self._text(
                    self._first(
                        item,
                        [".discount-percentage", ".discount-amount", ".product-flags .discount", ".product-flag.discount"],
                    )
                )
            )
            discount_percent = discount_percent or self._computed_discount(price, old_price)

            reference = self._clean_reference(
                self._text(
                    self._first(
                        item,
                        [
                            ".product-reference [itemprop='sku']",
                            ".product-reference span",
                            ".product-reference",
                        ],
                    )
                )
            )
            brand_node = self._first(item, [".product-brand a", ".product-manufacturer a", ".brand img[alt]"])
            brand = self._attr(brand_node, "alt") or self._text(brand_node)

            availability_node = self._first(
                item,
                [
                    ".product-availability",
                    ".product-last-items",
                    ".product-unavailable",
                    ".highlighted-informations .product-availability",
                ],
            )
            availability, available = self._availability(
                self._text(availability_node),
                self._attr(availability_node, "class"),
            )

            image = self._image_from_node(
                self._first(item, ["a.thumbnail.product-thumbnail img", ".thumbnail-container img", "img"])
            )

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "url": product_url,
                "name": name,
                "price": price,
                "old_price": old_price,
                "discount_percent": discount_percent,
                "availability": availability,
                "available": available,
            }
            if attribute_id:
                product["product_attribute_id"] = attribute_id
            if image:
                product["image"] = image
            if reference:
                product["reference"] = reference
            if brand:
                product["brand"] = brand

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "sasio listing")

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        pagination = tree.css_first("nav.pagination, .pagination")
        current_page = 1
        total_pages = 1
        has_next = False

        if not pagination:
            return {
                "current_page": current_page,
                "total_pages": total_pages,
                "has_next": has_next,
            }

        for node in pagination.css("li, a[href], span"):
            classes = (self._attr(node, "class") or "").lower()
            if "current" in classes or "active" in classes:
                label = self._text(node) or ""
                if label.isdigit():
                    current_page = int(label)

        for link in pagination.css(".page-list a[href], a.js-search-link[href], a[href]"):
            href = self._attr(link, "href") or ""
            label = self._text(link) or ""
            classes = (self._attr(link, "class") or "").lower()
            rel = (self._attr(link, "rel") or "").lower()
            lower_label = label.lower()

            if "next" in classes or rel == "next" or "suivant" in lower_label:
                if "disabled" not in classes:
                    has_next = True

            page_num = None
            if label.isdigit():
                page_num = int(label)
            else:
                query = dict(parse_qsl(urlsplit(href).query))
                if query.get("page", "").isdigit():
                    page_num = int(query["page"])

            if page_num:
                total_pages = max(total_pages, page_num)
                if "disabled" in classes or "current" in classes or "active" in classes:
                    current_page = page_num

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        fetch_url = self._fetch_url(url)
        html = await self.fetch_html(fetch_url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        sample_path = self.html_dir / "detail_sample_1.html"
        if not sample_path.exists() and "data-product" in html:
            save_text_atomic(html, sample_path, self.logger)

        tree = HTMLParser(html)
        data = html_product_metadata(html, url, self.base_url)
        data["url"] = url

        product_json = self._product_json(tree)
        if product_json:
            self._apply_product_json(data, product_json)

        self._apply_detail_dom(data, tree, url)
        return finalize_product_record(data)

    def _product_json(self, tree: HTMLParser) -> Dict[str, Any]:
        node = tree.css_first("#product-details[data-product]")
        raw = self._attr(node, "data-product")
        if not raw:
            return {}
        try:
            parsed = json.loads(html_lib.unescape(raw))
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def _apply_product_json(self, data: Dict[str, Any], product: Dict[str, Any]) -> None:
        product_id = product.get("id_product") or product.get("id")
        if product_id is not None:
            data["product_id"] = str(product_id)
            data["id"] = str(product_id)

        attribute_id = product.get("id_product_attribute")
        if attribute_id:
            data["product_attribute_id"] = str(attribute_id)

        title = clean_text(product.get("name"))
        if title:
            data["title"] = title
            data.setdefault("name", title)

        reference = self._clean_reference(product.get("reference"))
        if reference:
            data["reference"] = reference

        primary_gtin = None
        for key in ("ean13", "upc", "isbn"):
            gtins = extract_gtins_from_text(product.get(key))
            if gtins:
                primary_gtin = gtins[0]
                break

        if not primary_gtin:
            gtins = extract_gtins_from_text(json.dumps(product.get("attributes") or {}, ensure_ascii=False))
            if gtins:
                primary_gtin = gtins[0]
        if primary_gtin:
            data["barcode"] = primary_gtin

        brand = clean_text(product.get("manufacturer_name"))
        if brand and brand.lower() not in {"false", "none", "null"}:
            data["brand"] = brand

        price = parse_price(
            product.get("price_amount")
            or product.get("price_tax_exc")
            or product.get("price")
        )
        if price is not None:
            data["price"] = price

        old_price = parse_price(product.get("price_without_reduction"))
        if old_price is not None and price is not None and old_price > price:
            data["old_price"] = old_price
        elif old_price == price:
            data.pop("old_price", None)

        discount = self._discount_percent(product.get("discount_percentage"))
        data["discount_percent"] = discount or self._computed_discount(
            data.get("price"), data.get("old_price")
        )

        quantity = parse_price(product.get("quantity"))
        if quantity is not None:
            data["quantity"] = int(quantity)

        availability_raw = clean_text(product.get("availability"))
        availability_message = clean_text(product.get("availability_message") or product.get("available_now"))
        availability, available = self._availability(availability_raw or availability_message, quantity=quantity)
        if availability_raw == "last_remaining_items":
            availability, available = availability_message or "Derniers articles en stock", True
        elif availability_raw == "available":
            availability, available = availability_message or "En stock", True
        elif availability_raw in {"unavailable", "out_of_stock"}:
            availability, available = availability_message or "Rupture de stock", False
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        if product.get("category_name"):
            data["detail_category"] = clean_text(product.get("category_name"))
        if product.get("category"):
            data["detail_category_slug"] = clean_text(product.get("category"))
        if product.get("condition"):
            data["condition"] = clean_text(product.get("condition"))

        short_description = self._html_to_text(product.get("description_short"))
        if short_description:
            data["short_description"] = short_description
            data["overview"] = short_description

        full_description = self._html_to_text(product.get("description"))
        if full_description:
            data["description"] = full_description
            data["full_description"] = full_description

        images = self._images_from_product_json(product)
        if images:
            data["images"] = images
            data["image"] = images[0]

        specs = self._specs_from_product_json(product)
        if specs:
            data["specifications"] = specs

        variants = self._variants_from_product_json(product)
        if variants:
            data["variants"] = variants
            sizes = sorted({v["name"] for v in variants if (v.get("group") or "").lower() in {"taille", "size"}})
            colors = sorted({v["name"] for v in variants if (v.get("group") or "").lower() in {"color", "couleur"}})
            if sizes:
                data["sizes"] = sizes
            if colors:
                data["colors"] = colors

    def _apply_detail_dom(self, data: Dict[str, Any], tree: HTMLParser, url: str) -> None:
        title = self._text(
            self._first(tree, ["h1.h1.productpage_title", "h1.h1", "h1[itemprop='name']", "h1"])
        )
        if title:
            data.setdefault("title", title)
            data.setdefault("name", title)

        if not data.get("product_id"):
            product_id = self._attr(
                self._first(
                    tree,
                    [
                        "input[name='id_product'][value]",
                        "#product_page_product_id[value]",
                        "[data-product-id]",
                    ],
                ),
                "value",
            )
            product_id = product_id or self._product_ids_from_url(url)[0]
            if product_id:
                data["product_id"] = product_id
                data["id"] = product_id

        attribute_id = self._product_ids_from_url(url)[1]
        if attribute_id:
            data.setdefault("product_attribute_id", attribute_id)

        reference = self._clean_reference(
            self._text(
                self._first(
                    tree,
                    [
                        ".product-reference span[itemprop='sku']",
                        ".product-reference span",
                        "[itemprop='sku']",
                    ],
                )
            )
        )
        if reference:
            data.setdefault("reference", reference)

        if not data.get("brand"):
            brand = self._brand_from_dom(tree)
            if brand:
                data["brand"] = brand

        price = self._price_from_node(
            self._first(
                tree,
                [
                    ".current-price-value[content]",
                    ".current-price [content]",
                    ".product-price[content]",
                    "meta[property='product:price:amount']",
                ],
            )
        )
        if price is not None:
            data.setdefault("price", price)

        old_price = self._price_from_node(
            self._first(tree, [".product-prices .regular-price", ".regular-price", ".old-price"])
        )
        if old_price is not None and old_price > (data.get("price") or 0):
            data.setdefault("old_price", old_price)
        data["discount_percent"] = data.get("discount_percent") or self._computed_discount(
            data.get("price"), data.get("old_price")
        )

        availability_node = self._first(tree, ["#product-availability", ".product-availability"])
        availability, available = self._availability(
            self._text(availability_node),
            self._attr(availability_node, "class"),
        )
        if availability:
            data.setdefault("availability", availability)
        if available is not None:
            data.setdefault("available", available)

        short_description = self._text(
            self._first(tree, ["#product-description-short", ".product-description-short"])
        )
        if short_description:
            data.setdefault("short_description", short_description)
            data.setdefault("overview", short_description)

        description = self._text(
            self._first(
                tree,
                [
                    "#description .product-description",
                    ".product-tabs .product-description",
                    ".product-description",
                ],
            )
        )
        if description:
            data.setdefault("description", description)
            data.setdefault("full_description", description)
        elif data.get("short_description"):
            data.setdefault("description", data["short_description"])
            data.setdefault("full_description", data["short_description"])

        specs = dict(data.get("specifications") or {})
        specs.update(self._specs_from_dom(tree))
        if specs:
            data["specifications"] = specs
            for label, value in specs.items():
                if re.search(r"ean|gtin|code\s*bar|barcode", label, re.I):
                    gtins = extract_gtins_from_text(value)
                    if gtins:
                        data.setdefault("barcode", gtins[0])

        breadcrumbs = [
            crumb
            for crumb in (self._text(node) for node in tree.css(".breadcrumb a[href]"))
            if crumb and crumb.lower() not in {"accueil", "domicile", "home"}
        ]
        if breadcrumbs:
            data["breadcrumbs"] = breadcrumbs

        images = self._dedupe_urls(
            list(data.get("images") or [])
            + [
                self._image_from_node(node)
                for node in tree.css(".product-cover img, .product-images img, .js-thumb, img[itemprop='image']")
            ]
        )
        if images:
            data["images"] = images
            data["image"] = data.get("image") or images[0]

    def _images_from_product_json(self, product: Dict[str, Any]) -> List[str]:
        urls = []
        images = product.get("images")
        if not isinstance(images, list):
            return []
        for image in images:
            if not isinstance(image, dict):
                continue
            by_size = image.get("bySize") if isinstance(image.get("bySize"), dict) else {}
            for size in ("large_default", "thickbox_default", "home_default", "medium_default", "cart_default"):
                size_info = by_size.get(size)
                if isinstance(size_info, dict):
                    url = self._absolute_url(size_info.get("url"))
                    if url:
                        urls.append(url)
                        break
            else:
                for key in ("large", "medium", "small"):
                    value = image.get(key)
                    url = self._absolute_url(value.get("url")) if isinstance(value, dict) else self._absolute_url(value)
                    if url:
                        urls.append(url)
                        break
        return self._dedupe_urls(urls)

    def _specs_from_product_json(self, product: Dict[str, Any]) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        features = product.get("features")
        if isinstance(features, list):
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                label = clean_text(feature.get("name") or feature.get("feature_name"))
                value = clean_text(feature.get("value") or feature.get("feature_value"))
                if label and value:
                    specs[label] = value
        return specs

    def _variants_from_product_json(self, product: Dict[str, Any]) -> List[Dict[str, str]]:
        attrs = product.get("attributes")
        if not isinstance(attrs, dict):
            return []
        variants = []
        for attr in attrs.values():
            if not isinstance(attr, dict):
                continue
            variant = {
                "group": clean_text(attr.get("group")),
                "name": clean_text(attr.get("name")),
            }
            for key in ("id_attribute", "id_attribute_group", "reference"):
                value = clean_text(attr.get(key))
                if value:
                    variant[key] = value
            gtins = extract_gtins_from_text(attr.get("ean13") or attr.get("upc") or attr.get("isbn"))
            if gtins:
                variant["barcode"] = gtins[0]
            if variant.get("group") or variant.get("name"):
                variants.append({k: v for k, v in variant.items() if v})
        return variants

    def _brand_from_dom(self, tree: HTMLParser) -> Optional[str]:
        node = self._first(
            tree,
            [
                ".product-manufacturer img[alt]",
                ".product-manufacturer a",
                ".product-manufacturer",
            ],
        )
        brand = self._attr(node, "alt") or self._text(node)
        if not brand:
            return None
        brand = re.sub(r"^marque\s*:?", "", brand, flags=re.I)
        return clean_text(brand.strip(" :|-"))

    def _specs_from_dom(self, tree: HTMLParser) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for block in tree.css(".product-features dl, .data-sheet dl, dl.data-sheet"):
            labels = block.css("dt")
            values = block.css("dd")
            for label_node, value_node in zip(labels, values):
                label = (self._text(label_node) or "").rstrip(":")
                value = self._text(value_node)
                if label and value:
                    specs[label] = value

        for row in tree.css("table.product-features tr, table.product-attributes tr"):
            cells = row.css("th, td")
            if len(cells) < 2:
                continue
            label = (self._text(cells[0]) or "").rstrip(":")
            value = self._text(cells[-1])
            if label and value:
                specs[label] = value

        return specs


def get_scraper(logger: logging.Logger) -> SasioScraper:
    """Factory used by scraper.sites registry."""
    return SasioScraper(logger)
