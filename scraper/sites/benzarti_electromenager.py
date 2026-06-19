#!/usr/bin/env python3
"""
Benzarti Electromenager scraper - WordPress/WooCommerce + Elementor/OceanWP.
"""

import html as html_lib
import json
import logging
import re
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx
from selectolax.parser import HTMLParser

from scraper.base import CategoryInfo, FastScraper, detect_blocked_signals, is_blocked_response
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    clean_text,
    dedupe_products,
    finalize_product_record,
    html_product_metadata,
    parse_price,
)


class BenzartiElectromenagerScraper(FastScraper):
    """HTTPX/selectolax scraper for benzarti-electromenager.com."""

    FULL_HTML_STATUS = 415
    CATEGORY_PREFIX = "/categorie-produit/"
    PRODUCT_PREFIX = "/boutique/"
    BLOCKED_URL_TOKENS = (
        "add-to-cart",
        "/cart",
        "/panier",
        "/checkout",
        "/commande",
        "/account",
        "/mon-compte",
        "/my-account",
        "/wishlist",
        "/search",
        "/recherche",
        "/contact",
        "/apropos",
        "/a-propos",
        "/blog",
        "/wp-content/",
        "/wp-json/",
        "mailto:",
        "tel:",
        "javascript:",
        "#",
        "facebook.",
        "instagram.",
        "youtube.",
        "linkedin.",
        "twitter.",
        "x-twitter",
    )
    SPEC_KEY_RE = re.compile(
        r"([A-Za-z0-9À-ÿ][A-Za-z0-9À-ÿ ()'’./+\-]{1,60})\s*:\s*"
        r"(.*?)(?=\s+[A-Za-z0-9À-ÿ][A-Za-z0-9À-ÿ ()'’./+\-]{1,60}\s*:|$)",
        re.S,
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("benzarti-electromenager", logger)
        self.headers = self._http_headers()

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    @staticmethod
    def _http_headers() -> Dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> Dict[str, Any]:
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
        last_error = None
        last_exception: Optional[Exception] = None
        last_status_code = None
        final_url = url
        content_type = None
        content_encoding = None
        blocked_signals: List[str] = []
        attempts = 0

        for attempt in range(1, self.retry_config.max_retries + 1):
            attempts = attempt
            try:
                response = await client.get(url, headers=self._http_headers())
                html = response.text or ""
                last_status_code = response.status_code
                final_url = str(response.url)
                content_type = response.headers.get("content-type")
                content_encoding = response.headers.get("content-encoding")
                blocked_signals = detect_blocked_signals(html, response.status_code)

                if self._is_usable_response(html, response.status_code, content_type, final_url):
                    return {
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

                last_error = self._response_error(html, response.status_code)
                if response.status_code and 400 <= response.status_code < 500:
                    break
            except Exception as exc:
                last_exception = exc
                last_error = str(exc) or exc.__class__.__name__

            if attempt < self.retry_config.max_retries:
                await self._sleep_between_attempts(attempt)

        if raise_on_error:
            raise RuntimeError(last_error or str(last_exception) or "fetch_failed")

        self.logger.warning(f"Failed to fetch {url}: {last_error or last_exception}")
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

    async def _sleep_between_attempts(self, attempt: int) -> None:
        import asyncio

        await asyncio.sleep(self.retry_config.get_delay(attempt))

    def _is_usable_response(
        self,
        html: str,
        status_code: Optional[int],
        content_type: Optional[str],
        url: str,
    ) -> bool:
        if not html or not html.strip():
            return False
        low = html[:5000].lower()
        if "access denied by imunify360" in low:
            return False
        if is_blocked_response(html, status_code):
            return False
        if status_code and status_code >= 400 and status_code != self.FULL_HTML_STATUS:
            return False

        content_type = (content_type or "").lower()
        url_path = urlsplit(url).path.lower()
        is_sitemap = url_path.endswith(".xml") or "xml" in content_type
        if is_sitemap:
            return "<urlset" in low or "<sitemapindex" in low

        page_markers = (
            "woocommerce",
            "wp-content",
            self.CATEGORY_PREFIX,
            self.PRODUCT_PREFIX,
            "product type-product",
            "elementskit-navbar-nav",
        )
        return any(marker in html.lower() for marker in page_markers)

    @staticmethod
    def _response_error(html: str, status_code: Optional[int]) -> str:
        if not html or not html.strip():
            return "empty_response"
        if "access denied by imunify360" in html[:5000].lower():
            return "imunify360_access_denied"
        if status_code:
            return f"HTTP {status_code}"
        return "unusable_response"

    def _fetch_sync(self, url: str) -> Optional[str]:
        try:
            with httpx.Client(
                headers=self._http_headers(),
                follow_redirects=True,
                timeout=httpx.Timeout(self.request_timeout, connect=10.0),
            ) as client:
                response = client.get(url)
        except Exception as exc:
            self.logger.debug(f"Sync fetch failed {url}: {exc}")
            return None

        if self._is_usable_response(
            response.text,
            response.status_code,
            response.headers.get("content-type"),
            str(response.url),
        ):
            return response.text
        return None

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, href: Any) -> Optional[str]:
        return absolute_url(href, self.base_url)

    @staticmethod
    def _clean(value: Any) -> Optional[str]:
        return clean_text(value)

    @staticmethod
    def _text(node: Any, separator: str = " ") -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=separator, strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

    @staticmethod
    def _strip_url(url: str, keep_query: bool = False) -> str:
        parts = urlsplit(url)
        path = parts.path.rstrip("/") if parts.path != "/" else parts.path
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                path or "/",
                parts.query if keep_query else "",
                "",
            )
        )

    @staticmethod
    def _post_id_from_class(class_name: str) -> Optional[str]:
        match = re.search(r"(?:^|\s)(?:post|postid)-(\d+)(?:\s|$)", class_name or "")
        return match.group(1) if match else None

    @classmethod
    def _body_post_id(cls, tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        return cls._post_id_from_class(body.attributes.get("class", "") if body else "")

    @staticmethod
    def _first_srcset_url(value: Optional[str], prefer_largest: bool = False) -> Optional[str]:
        if not value:
            return None
        entries = [chunk.strip() for chunk in value.split(",") if chunk.strip()]
        if not entries:
            return None
        chosen = entries[-1] if prefer_largest else entries[0]
        return chosen.split(" ", 1)[0] if chosen else None

    @staticmethod
    def _ascii_fold(value: Any) -> str:
        text = clean_text(value) or ""
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        return text.lower()

    @staticmethod
    def _same_identifier(left: Any, right: Any) -> bool:
        return bool(left and right and str(left).strip() == str(right).strip())

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        return host == base_host

    def _is_category_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        url = self._absolute_url(url)
        if not url or not self._is_site_url(url):
            return False
        low = url.lower()
        parts = urlsplit(url)
        if parts.query:
            return False
        if self.PRODUCT_PREFIX in parts.path.lower():
            return False
        if any(token in low for token in self.BLOCKED_URL_TOKENS):
            return False
        return self.CATEGORY_PREFIX in parts.path.lower()

    def _is_product_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        url = self._absolute_url(url)
        if not url or not self._is_site_url(url):
            return False
        parts = urlsplit(url)
        low = url.lower()
        return self.PRODUCT_PREFIX in parts.path.lower() and not any(
            token in low for token in ("add-to-cart", "wishlist", "quickview", "#")
        )

    def _category_from_link(self, link: Any) -> Optional[Dict[str, str]]:
        if not link:
            return None
        url = self._absolute_url(link.attributes.get("href"))
        if url:
            url = self._strip_url(url)
        if not self._is_category_url(url):
            return None
        name = self._text(link)
        if not name:
            name = self._name_from_category_url(url)
        if not name or len(name) > 100:
            return None
        return {"name": name, "url": url}

    def _category_parts(self, url: str) -> List[str]:
        path = urlsplit(url).path.strip("/")
        if not path.startswith("categorie-produit/"):
            return []
        return [part for part in path.split("/")[1:] if part]

    @staticmethod
    def _name_from_slug(slug: str) -> str:
        return re.sub(r"[-_]+", " ", html_lib.unescape(slug)).strip().title()

    def _name_from_category_url(self, url: str) -> str:
        parts = self._category_parts(url)
        return self._name_from_slug(parts[-1]) if parts else "Category"

    @staticmethod
    def _category_stats(categories: List[Dict[str, Any]]) -> Dict[str, int]:
        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
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

    def build_scrape_queue(self, categories_data: dict) -> List[CategoryInfo]:
        queue: List[CategoryInfo] = []
        for top_idx, top in enumerate(categories_data.get("categories", [])):
            top_name = top.get("name", "")
            lows = top.get("low_level_categories") or []
            if not lows:
                if top.get("url"):
                    queue.append(
                        CategoryInfo(
                            url=top["url"],
                            name=top_name,
                            location=(top_idx,),
                            level="top",
                            parent_names=[],
                        )
                    )
                continue

            for low_idx, low in enumerate(lows):
                # The nested semi-encastrable page has only one product, and the
                # parent L'ENCASTRABLE page already exposes that product.
                if low.get("url") != top.get("url"):
                    continue
                queue.append(
                    CategoryInfo(
                        url=low.get("url", ""),
                        name=low.get("name", ""),
                        location=(top_idx, low_idx),
                        level="low",
                        parent_names=[top_name],
                    )
                )
        return queue

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        top_by_slug: Dict[str, Dict[str, Any]] = {}
        top_order: List[str] = []
        candidate_sources = [
            fp.get("menu_links", "nav a[href*='/categorie-produit/']"),
            fp.get("widget_links", ".jet-woo-category-title a[href*='/categorie-produit/']"),
            fp.get("fallback_links", "header a[href*='/categorie-produit/'], footer a[href*='/categorie-produit/']"),
        ]

        for index, selector in enumerate(candidate_sources):
            for link in tree.css(selector):
                meta = self._category_from_link(link)
                if not meta:
                    continue
                if self._category_known(meta["url"], top_by_slug):
                    continue
                parts = self._category_parts(meta["url"])
                known_child_parent = len(parts) > 1 and parts[0] in top_by_slug
                if index > 0 and not known_child_parent and not self._should_keep_non_menu_category(meta["url"]):
                    continue
                self._add_category(meta, top_by_slug, top_order)

        if not top_order:
            for meta in self._extract_categories_from_sitemap():
                self._add_category(meta, top_by_slug, top_order)

        categories = [top_by_slug[slug] for slug in top_order]
        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _category_known(self, url: str, top_by_slug: Dict[str, Dict[str, Any]]) -> bool:
        parts = self._category_parts(url)
        if not parts:
            return True
        top = top_by_slug.get(parts[0])
        if not top:
            return False
        stripped = self._strip_url(url)
        if len(parts) == 1:
            return True
        return any(low.get("url") == stripped for low in top.get("low_level_categories", []))

    def _add_category(
        self,
        meta: Dict[str, str],
        top_by_slug: Dict[str, Dict[str, Any]],
        top_order: List[str],
    ) -> None:
        parts = self._category_parts(meta["url"])
        if not parts:
            return

        top_slug = parts[0]
        top_url = self._category_url_from_parts([top_slug])
        top_name = meta["name"] if len(parts) == 1 else self._existing_or_slug_name(top_by_slug, top_slug)
        if top_slug not in top_by_slug:
            top_by_slug[top_slug] = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }
            top_order.append(top_slug)
        elif len(parts) == 1 and not top_by_slug[top_slug].get("name"):
            top_by_slug[top_slug]["name"] = meta["name"]

        if len(parts) <= 1:
            return

        top_cat = top_by_slug[top_slug]
        if not any(low.get("url") == top_cat["url"] for low in top_cat["low_level_categories"]):
            top_cat["low_level_categories"].append(
                {
                    "name": top_cat["name"],
                    "url": top_cat["url"],
                    "level": "low",
                    "subcategories": [],
                }
            )

        child_url = self._strip_url(meta["url"])
        if not any(low.get("url") == child_url for low in top_cat["low_level_categories"]):
            top_cat["low_level_categories"].append(
                {
                    "name": meta["name"],
                    "url": child_url,
                    "level": "low",
                    "subcategories": [],
                }
            )

    def _existing_or_slug_name(self, top_by_slug: Dict[str, Dict[str, Any]], slug: str) -> str:
        existing = top_by_slug.get(slug, {}).get("name")
        return existing or self._name_from_slug(slug)

    def _category_url_from_parts(self, parts: List[str]) -> str:
        return f"{self.base_url.rstrip('/')}/categorie-produit/{'/'.join(parts)}"

    def _should_keep_non_menu_category(self, url: str) -> bool:
        parts = self._category_parts(url)
        if len(parts) > 1:
            return self._category_has_products(url)
        return self._category_has_products(url)

    def _category_has_products(self, url: str) -> bool:
        html = self._fetch_sync(url)
        if not html:
            return False
        tree = HTMLParser(html)
        for card in tree.css(self.selectors.get("category_page", {}).get("item_selector", "li.product.type-product")):
            if "product-category" not in (card.attributes.get("class") or ""):
                return True
        return False

    def _extract_categories_from_sitemap(self) -> List[Dict[str, str]]:
        sitemap_url = self.selectors.get("frontpage", {}).get(
            "sitemap_url", f"{self.base_url.rstrip('/')}/product_cat-sitemap.xml"
        )
        html = self._fetch_sync(sitemap_url)
        if not html:
            self.logger.warning(f"Failed category sitemap fallback {sitemap_url}")
            return []

        categories: List[Dict[str, str]] = []
        seen = set()
        for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", html, flags=re.I | re.S):
            url = self._strip_url(html_lib.unescape(loc.strip()))
            if not self._is_category_url(url) or url in seen:
                continue
            seen.add(url)
            if not self._category_has_products(url):
                continue
            categories.append({"name": self._name_from_category_url(url), "url": url})
        return categories

    # ------------------------------------------------------------------
    # Product listings
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products: List[Dict[str, Any]] = []

        for card in tree.css(cp.get("item_selector", "li.product.type-product")):
            class_name = card.attributes.get("class", "") or ""
            if "product-category" in class_name:
                continue

            url, name = self._listing_url_and_name(card, cp)
            if not url or not name:
                continue

            product_id = self._listing_product_id(card, cp)
            sku = self._listing_sku(card, cp, product_id)
            price = self._extract_price(card, cp.get("item_current_price", ""), cp.get("item_price", ""))
            old_price = self._node_price(card.css_first(cp.get("item_old_price", "del .woocommerce-Price-amount bdi")))
            discount = self._discount_percent(
                price,
                old_price,
                self._text(card.css_first(cp.get("item_discount", ".onsale"))),
            )
            availability, available = self._listing_availability(card)
            specs = self._extract_specs_from_text(
                self._text(card.css_first(cp.get("item_description", "li.woo-desc")), separator=" ")
            )

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "shop": self.site_name,
                "url": url,
                "name": name,
                "price": price,
            }
            if sku:
                product["reference"] = sku
                product["sku"] = sku
            if old_price is not None:
                product["old_price"] = old_price
            if discount is not None:
                product["discount_percent"] = discount
            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available
            if specs:
                product["specifications"] = specs
                brand = specs.get("Marque") or specs.get("marque")
                if brand:
                    product["brand"] = brand

            image = self._extract_image(
                card,
                cp.get("item_image", "img"),
                cp.get("item_image_attrs", ["data-src", "data-srcset", "srcset", "src"]),
            )
            if image:
                product["image"] = image

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "benzarti-electromenager listing")

    def _listing_url_and_name(self, card: Any, cp: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        chosen = None
        for link in card.css(cp.get("item_url", "a[href*='/boutique/']")):
            href = self._absolute_url(link.attributes.get("href"))
            if not self._is_product_url(href):
                continue
            if self._text(link):
                chosen = link
                break
            chosen = chosen or link

        if chosen is None:
            return None, None

        url = self._absolute_url(chosen.attributes.get("href"))
        name_node = card.css_first(cp.get("item_name", "li.title a, h2"))
        name = self._text(name_node)
        if not name:
            image_node = card.css_first("img[alt]")
            name = self._clean(image_node.attributes.get("alt") if image_node else None)
        return (self._strip_url(url) if url else None), name

    def _listing_product_id(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        product_id = self._post_id_from_class(card.attributes.get("class", ""))
        if product_id:
            return product_id
        node = card.css_first(cp.get("item_id", "[data-product_id]"))
        return self._clean(node.attributes.get("data-product_id") if node else None)

    def _listing_sku(self, card: Any, cp: Dict[str, Any], product_id: Optional[str]) -> Optional[str]:
        node = card.css_first(cp.get("item_sku", "[data-product_sku]"))
        sku = self._clean(node.attributes.get("data-product_sku") if node else None)
        if not sku or self._same_identifier(sku, product_id):
            return None
        return sku

    def _extract_price(self, root: Any, current_selector: str, fallback_selector: str) -> Optional[float]:
        node = root.css_first(current_selector) if current_selector else None
        if node is None and fallback_selector:
            node = root.css_first(fallback_selector)
        if node is None:
            node = root.css_first(".woocommerce-Price-amount bdi, bdi, .price")
        return self._node_price(node)

    @staticmethod
    def _node_price(node: Any) -> Optional[float]:
        if not node:
            return None
        price = parse_price(node.attributes.get("content"))
        if price is None:
            price = parse_price(node.text(strip=True))
        return price

    @staticmethod
    def _discount_percent(
        price: Optional[float],
        old_price: Optional[float],
        text: Optional[str] = None,
    ) -> Optional[int]:
        if text and "%" in text:
            match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", text)
            if match:
                return int(round(float(match.group(1).replace(",", "."))))
        if price is not None and old_price and old_price > price:
            return int(round((1 - price / old_price) * 100))
        return None

    def _listing_availability(self, card: Any) -> Tuple[Optional[str], Optional[bool]]:
        class_name = (card.attributes.get("class") or "").lower()
        text = (self._text(card) or "").lower()
        if "outofstock" in class_name or "rupture" in text:
            return "Rupture de stock", False
        if "available-on-backorder" in class_name or "onbackorder" in class_name:
            return "Sur commande", True
        if "instock" in class_name:
            return "En stock", True
        if card.css_first("a.add_to_cart_button, button[name='add-to-cart']"):
            return "En stock", True
        return availability_from_text(text)

    def _extract_image(self, root: Any, selector: str, attrs: List[str]) -> Optional[str]:
        for img in root.css(selector):
            image = self._image_from_node(img, attrs)
            if image and self._is_useful_image(image):
                return image
        return None

    def _image_from_node(self, img: Any, attrs: List[str]) -> Optional[str]:
        for attr in attrs:
            value = img.attributes.get(attr)
            if attr in {"srcset", "data-srcset"}:
                value = self._first_srcset_url(value, prefer_largest=True)
            image = self._absolute_url(value)
            if image and not image.startswith("data:"):
                return image
        return None

    @staticmethod
    def _is_useful_image(image: str) -> bool:
        low = image.lower()
        if low.startswith("data:"):
            return False
        blocked = ("logo", "whatsapp", "facebook", "instagram", "youtube", "placeholder")
        return not any(token in low for token in blocked)

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        path = re.sub(r"/page/\d+/?$", "", parts.path or "/").rstrip("/")
        if page_num > 1:
            path = f"{path}/page/{page_num}"
        path = path or "/"
        if path != "/" and not path.endswith("/"):
            path += "/"
        query = "&".join(
            item
            for item in (parts.query or "").split("&")
            if item and not item.startswith(("paged=", "product-page="))
        )
        return urlunsplit((parts.scheme, parts.netloc, path, query, ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1

        current = tree.css_first(
            cp.get("pagination_current", "nav.woocommerce-pagination span.page-numbers.current")
        )
        current_text = self._text(current)
        if current_text and current_text.isdigit():
            current_page = int(current_text)
            total_pages = max(total_pages, current_page)

        for link in tree.css(cp.get("pagination_pages", "nav.woocommerce-pagination a.page-numbers, a.page-numbers")):
            href = link.attributes.get("href", "")
            if "products-per-page=" in href or "product-page=" in href:
                continue
            page_num = self._page_number_from_link(link)
            if page_num:
                total_pages = max(total_pages, page_num)

        next_link = tree.css_first(cp.get("pagination_next", "a.next.page-numbers"))
        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": bool(next_link),
        }

    def _page_number_from_link(self, link: Any) -> Optional[int]:
        text = self._text(link)
        if text and text.isdigit():
            return int(text)
        href = link.attributes.get("href") or ""
        match = re.search(r"/page/(\d+)/?", href) or re.search(r"[?&]paged=(\d+)", href)
        return int(match.group(1)) if match else None

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = {"url": self._strip_url(url), "shop": self.site_name}
        data.update(html_product_metadata(html, url, self.base_url))
        data["url"] = self._strip_url(data.get("url") or url)

        product_id = self._extract_detail_product_id(tree, pp)
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        title = self._extract_detail_title(tree, pp)
        if title and not self._is_truncated_title(title):
            data["title"] = title
            data.setdefault("name", title)
        elif data.get("title"):
            clean_title = self._clean_detail_title(data.get("title"))
            if clean_title:
                data["title"] = clean_title
                data.setdefault("name", clean_title)

        price = self._extract_price(
            tree,
            pp.get("current_price", "p.price ins .woocommerce-Price-amount bdi"),
            pp.get("price", "p.price .woocommerce-Price-amount bdi"),
        )
        if price is not None:
            data["price"] = price

        old_price = self._node_price(tree.css_first(pp.get("old_price", "p.price del .woocommerce-Price-amount bdi")))
        if old_price is not None:
            data["old_price"] = old_price
            discount = self._discount_percent(data.get("price"), old_price)
            if discount is not None:
                data["discount_percent"] = discount

        sku = self._extract_detail_sku(tree, pp, product_id)
        if sku:
            data["reference"] = sku
            data["sku"] = sku

        availability, available = self._detail_availability(tree, pp)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        description = self._detail_description(tree, pp)
        if description:
            data["description"] = description
            data.setdefault("overview", description)

        specs = self._extract_specs_from_text(description)
        if specs:
            data["specifications"] = specs
            brand = specs.get("Marque") or specs.get("marque")
            if brand:
                data["brand"] = brand

        jsonld_product = self._jsonld_product(html)
        brand = data.get("brand") or self._brand_from_jsonld(jsonld_product)
        if brand:
            data["brand"] = brand

        images = self._detail_images(tree, pp)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = self._detail_categories(html, url, data.get("title"))
        if categories:
            data["categories"] = categories

        self._remove_product_id_identifiers(data)
        return finalize_product_record(data)

    def _extract_detail_product_id(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("product_id", "button[name='add-to-cart'][value]"))
        if node:
            for attr in ("value", "data-product_id", "data-product-id"):
                product_id = self._clean(node.attributes.get(attr))
                if product_id:
                    return product_id
        return self._body_post_id(tree)

    def _extract_detail_title(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        for node in tree.css(pp.get("title", ".elementor-heading-title, .entry-title, h1")):
            title = self._text(node)
            folded = self._ascii_fold(title)
            if not title:
                continue
            if any(token in folded for token in ("realise par", "panier", "reference")):
                continue
            if title.upper() == "BENZARTI CIE ELECTROMENAGER":
                continue
            return title.replace("...", "").strip()
        return None

    @staticmethod
    def _is_truncated_title(title: str) -> bool:
        return "…" in title or "..." in title

    def _clean_detail_title(self, title: Any) -> Optional[str]:
        value = self._clean(title)
        if not value:
            return None
        value = re.sub(r"\s*-\s*BENZARTI CIE ELECTROMENAGER\s*$", "", value, flags=re.I)
        return value or None

    def _extract_detail_sku(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        product_id: Optional[str],
    ) -> Optional[str]:
        node = tree.css_first(pp.get("sku", ".sku_wrapper .sku"))
        sku = self._clean(node.text(strip=True) if node else None)
        if sku and sku.upper() not in {"N/A", "ND", "SKU"} and not self._same_identifier(sku, product_id):
            return sku
        return None

    def _detail_availability(self, tree: HTMLParser, pp: Dict[str, Any]) -> Tuple[Optional[str], Optional[bool]]:
        node = tree.css_first(pp.get("availability", ".stock, .out-of-stock, .in-stock"))
        if node:
            availability, available = availability_from_text(self._text(node))
            if availability:
                return availability, available
        body = tree.css_first("body")
        class_name = (body.attributes.get("class") if body else "") or ""
        if "outofstock" in class_name.lower():
            return "Rupture de stock", False
        if "instock" in class_name.lower():
            return "En stock", True
        if tree.css_first("form.cart button[name='add-to-cart'], button[name='add-to-cart']"):
            return "En stock", True
        return None, None

    def _detail_description(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        for node in tree.css(pp.get("description_meta", "meta[property='og:description'][content]")):
            description = self._clean(node.attributes.get("content"))
            if description:
                return description
        return None

    def _detail_images(self, tree: HTMLParser, pp: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        seen = set()

        for node in tree.css(pp.get("image_meta", "meta[property='og:image'][content]")):
            image = self._absolute_url(node.attributes.get("content"))
            if image and self._is_useful_image(image) and image not in seen:
                seen.add(image)
                images.append(image)

        if images:
            return images

        attrs = pp.get("image_attrs", ["data-large_image", "data-src", "data-srcset", "srcset", "src"])
        for img in tree.css(pp.get("images", "img")):
            image = self._image_from_node(img, attrs)
            if image and self._is_useful_image(image) and image not in seen:
                seen.add(image)
                images.append(image)

        return images

    def _extract_specs_from_text(self, text: Optional[str]) -> Dict[str, str]:
        if not text:
            return {}
        known_specs = self._extract_known_specs(text)
        if known_specs:
            return known_specs

        specs: Dict[str, str] = {}
        normalized = clean_text(text.replace("\xa0", " ")) or ""
        for match in self.SPEC_KEY_RE.finditer(normalized):
            key = self._clean(match.group(1))
            value = self._clean(match.group(2))
            if not key or not value:
                continue
            if len(key) > 70 or len(value) > 250:
                continue
            if key.lower() in {"vue rapide", "ajouter au panier"}:
                continue
            specs[key] = value
        return specs

    def _extract_known_specs(self, text: str) -> Dict[str, str]:
        normalized = clean_text(text.replace("\xa0", " ")) or ""
        labels = [
            "Marque",
            "Type",
            "Capacité",
            "Mode",
            "Diagonale de l'écran (pouces)",
            "Résolution maximale",
            "Technologie",
            "Couleur",
            "Classe énergétique",
            "Dimensions",
            "Référence",
        ]
        matches = []
        for label in labels:
            match = re.search(rf"{re.escape(label)}\s*:", normalized, flags=re.I)
            if match:
                matches.append((match.start(), match.end(), label))
        if not matches:
            return {}

        matches.sort(key=lambda item: item[0])
        specs: Dict[str, str] = {}
        for index, (_, value_start, label) in enumerate(matches):
            value_end = matches[index + 1][0] if index + 1 < len(matches) else len(normalized)
            value = self._clean(normalized[value_start:value_end])
            if value:
                specs[label] = value
        return specs

    def _jsonld_product(self, html: str) -> Dict[str, Any]:
        for item in self._jsonld_items(html):
            types = item.get("@type")
            if isinstance(types, str):
                types = [types]
            if any(str(kind).lower() == "product" for kind in (types or [])):
                return item
        return {}

    def _jsonld_items(self, html: str) -> Iterable[Dict[str, Any]]:
        tree = HTMLParser(html)
        for script in tree.css("script[type='application/ld+json']"):
            raw = script.text(strip=True)
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            yield from self._walk_json(parsed)

    def _walk_json(self, value: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from self._walk_json(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_json(child)

    def _brand_from_jsonld(self, product: Dict[str, Any]) -> Optional[str]:
        brand = product.get("brand") if isinstance(product, dict) else None
        if isinstance(brand, dict):
            return self._clean(brand.get("name"))
        return self._clean(brand)

    def _detail_categories(self, html: str, url: str, title: Optional[str] = None) -> List[str]:
        categories: List[str] = []
        folded_title = self._ascii_fold(title)
        for item in self._jsonld_items(html):
            if item.get("@type") != "BreadcrumbList":
                continue
            elements = [element for element in (item.get("itemListElement") or []) if isinstance(element, dict)]
            for element in elements[:-1]:
                if not isinstance(element, dict):
                    continue
                name = self._clean(element.get("name"))
                folded = self._ascii_fold(name)
                if not name or folded in {"accueil", "home", "boutique", "wishsuite"}:
                    continue
                if folded_title and folded == folded_title:
                    continue
                if name not in categories:
                    categories.append(name)

        parts = urlsplit(url).path.strip("/").split("/")
        if len(parts) > 1 and parts[0] == "boutique":
            fallback = self._name_from_slug(parts[1])
            if fallback and fallback not in categories:
                categories.append(fallback)
        return categories

    def _remove_product_id_identifiers(self, data: Dict[str, Any]) -> None:
        product_id = self._clean(data.get("product_id"))
        for key in ("sku", "reference"):
            if self._same_identifier(data.get(key), product_id):
                data.pop(key, None)


def get_scraper(logger: logging.Logger) -> BenzartiElectromenagerScraper:
    """Factory entrypoint used by scraper.sites."""
    return BenzartiElectromenagerScraper(logger)
