#!/usr/bin/env python3
"""
Electrosifi scraper - custom PHP storefront, Playwright/selectolax.
"""

import asyncio
import logging
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Page, async_playwright
from selectolax.parser import HTMLParser

from scraper.base import (
    BaseScraper,
    CategoryInfo,
    ScrapeStats,
    TorPool,
    detect_blocked_signals,
    playwright_launch_args,
    proxy_url_to_playwright,
    save_text_atomic,
)
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    clean_text,
    dedupe_products,
    finalize_product_record,
    html_product_metadata,
    normalize_url,
    parse_price,
)
from scraper.stealth import random_ua


class ElectrosifiScraper(BaseScraper):
    """Playwright scraper for electrosifi.com custom PHP pages."""

    BAD_LINK_PARTS = (
        "mon-compte",
        "mes-favoris",
        "mon-panier",
        "checkout",
        "contact",
        "web-contact",
        "faq",
        "a-propos",
        "politique",
        "livraison",
        "retour",
        "reclamation",
        "facebook",
        "instagram",
        "whatsapp",
        "mailto:",
        "tel:",
        "javascript:",
    )
    BAD_QUERY_KEYS = {
        "query",
        "MarqueArticle",
        "trie_produit",
        "prix_min",
        "prix_max",
        "note",
        "couleur",
        "garantie",
        "page",
    }

    def __init__(self, logger: logging.Logger):
        super().__init__("electrosifi", logger)
        self._playwright = None
        self._browser = None
        self._browser_context = None
        self._browser_lock = asyncio.Lock()
        self._page_sem = asyncio.Semaphore(4)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

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

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _name_from_slug(slug: Any) -> Optional[str]:
        text = clean_text(slug)
        if not text:
            return None
        return re.sub(r"[-_]+", " ", text).strip().title() or text

    @staticmethod
    def _first_srcset_url(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        first = value.split(",", 1)[0].strip()
        return first.split(" ", 1)[0] if first else None

    @staticmethod
    def _strip_url(url: str, keep_query: bool = True) -> str:
        parts = urlsplit(url)
        path = parts.path or "/"
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                path,
                parts.query if keep_query else "",
                "",
            )
        )

    @staticmethod
    def _query_value(url: str, key: str) -> Optional[str]:
        for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
            if name == key:
                return clean_text(value)
        return None

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        parsed = urlsplit(url)
        host = parsed.netloc.lower().removeprefix("www.")
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        return host == base_host

    def _category_url(self, href: Any) -> Optional[str]:
        url = self._absolute_url(href)
        if not url:
            return None
        parsed = urlsplit(url)
        if parsed.netloc.lower().startswith("127.0.0.1"):
            return None
        if not self._is_site_url(url):
            return None
        if parsed.path.rstrip("/") != "/rayon.php":
            return None
        low_url = url.lower()
        if any(part in low_url for part in self.BAD_LINK_PARTS):
            return None

        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        values = {key: clean_text(value) for key, value in pairs}
        if not values.get("rayon"):
            return None
        if any(key in self.BAD_QUERY_KEYS for key, _ in pairs):
            return None
        allowed = [("rayon", values["rayon"])]
        if values.get("categorie"):
            allowed.append(("categorie", values["categorie"]))
        return urlunsplit((parsed.scheme, parsed.netloc, "/rayon.php", urlencode(allowed), ""))

    def _product_url(self, href: Any) -> Optional[str]:
        url = self._absolute_url(href)
        if not url or not self._is_site_url(url):
            return None
        parsed = urlsplit(url)
        if parsed.path.rstrip("/") != "/details-produit.php":
            return None
        article = self._query_value(url, "article")
        if not article:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, "/details-produit.php", urlencode({"article": article}), ""))

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

    def _price_from_node(self, node: Any) -> Optional[float]:
        if not node:
            return None
        return parse_price(
            self._attr(node, "content")
            or self._attr(node, "value")
            or self._text(node)
        )

    def _first_value(self, root: Any, selectors: Iterable[str], attrs: Iterable[str] = ("content", "href", "title", "alt")) -> Optional[str]:
        for selector in selectors:
            node = root.css_first(selector)
            if not node:
                continue
            for attr in attrs:
                value = clean_text(node.attributes.get(attr))
                if value:
                    return value
            value = self._text(node)
            if value:
                return value
        return None

    def _image_from_node(self, node: Any) -> Optional[str]:
        if not node:
            return None
        for attr in ("href", "src", "data-src", "data-lazy-src"):
            value = self._absolute_url(node.attributes.get(attr))
            if value:
                return value
        srcset = self._first_srcset_url(node.attributes.get("srcset"))
        return self._absolute_url(srcset) if srcset else None

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
    def _tree_text(tree: HTMLParser) -> str:
        try:
            return tree.body.text(separator=" ", strip=True) if tree.body else tree.text(separator=" ", strip=True)
        except TypeError:
            return tree.text(strip=True)

    @staticmethod
    def _availability_snippet(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        match = re.search(r"(en\s+stock|rupture(?:\s+de\s+stock)?|indisponible|disponible)", text, re.I)
        return clean_text(match.group(1)) if match else None

    @staticmethod
    def _has_browser_error_marker(html: Optional[str]) -> bool:
        lowered = (html or "").lower()
        browser_error_markers = (
            "err_connection_timed_out",
            "err_name_not_resolved",
            "this site can't be reached",
            "dns_probe",
            "site can't be reached",
        )
        return any(marker in lowered for marker in browser_error_markers)

    def _looks_like_usable_html(self, html: Optional[str], selector: Optional[str] = None) -> bool:
        text = (html or "").strip()
        if len(text) < 1000:
            return False

        lowered = text.lower()
        if self._has_browser_error_marker(text):
            return False
        if "un instant" in lowered and "rayon.php?rayon" not in lowered and "details-produit.php" not in lowered:
            return False

        if (
            "rayon.php?rayon" in lowered
            or "details-produit.php" in lowered
            or "single-item-pd" in lowered
            or "product__details-content" in lowered
        ):
            return True

        if selector:
            try:
                return HTMLParser(text).css_first(selector) is not None
            except Exception:
                return False
        return False

    async def _set_page_headers(self, page: Page) -> None:
        try:
            await page.set_extra_http_headers(
                {
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
            )
        except Exception:
            pass

    async def _ensure_browser_context(self):
        async with self._browser_lock:
            if self._browser_context:
                return self._browser_context

            self._playwright = await async_playwright().start()
            pool = TorPool.get()
            slot = await pool.next_slot() if pool.active else 0
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=playwright_launch_args(),
            )
            self._browser_context = await self._browser.new_context(
                user_agent=random_ua(),
                proxy=pool.pw_proxy(slot) or proxy_url_to_playwright(self.proxy_url),
            )
            return self._browser_context

    async def _close_browser(self) -> None:
        if self._browser_context:
            try:
                await self._browser_context.close()
            except Exception:
                pass
            self._browser_context = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    async def _page_html_from_shared_context(self, url: str, selector: str) -> str:
        ctx = await self._ensure_browser_context()
        page = await ctx.new_page()
        try:
            await self._set_page_headers(page)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.page_timeout)
            except Exception as exc:
                self.logger.debug(f"Electrosifi goto did not settle for {url}: {exc}")
                partial_html = ""
                try:
                    partial_html = await page.content()
                except Exception:
                    pass
                if len((partial_html or "").strip()) < 1000 or self._has_browser_error_marker(partial_html):
                    raise RuntimeError(f"electrosifi_navigation_failed: {exc}") from exc
            await self._wait_for_real_page(page, selector, timeout_ms=self.page_timeout)
            await page.wait_for_timeout(int(self.wait_after_load * 1000))
            html = await page.content()
            if not self._looks_like_usable_html(html, selector):
                raise RuntimeError("electrosifi_unusable_page")
            return html
        finally:
            await page.close()

    @staticmethod
    def _browser_from_context(context: Any) -> Optional[Any]:
        browser = getattr(context, "browser", None)
        if callable(browser):
            try:
                return browser()
            except Exception:
                return None
        return browser

    async def _wait_for_real_page(self, page: Page, selector: str, timeout_ms: Optional[int] = None) -> None:
        deadline = time.monotonic() + ((timeout_ms or self.page_timeout) / 1000)
        while time.monotonic() < deadline:
            try:
                title = await page.title()
                html = await page.content()
                if "Un instant" not in title and "rayon.php?rayon" in html:
                    try:
                        if await page.locator(selector).count() > 0:
                            return
                    except Exception:
                        return
                if "details-produit.php" in html and "product__details-content" in html:
                    return
            except Exception:
                pass
            await page.wait_for_timeout(1000)

        try:
            await page.wait_for_selector(selector, timeout=5000)
        except Exception:
            self.logger.warning(f"Electrosifi selector not found after challenge wait: {selector}")

    def _wait_selector_for_url(self, url: str) -> str:
        if "/details-produit.php" in url:
            return self.selectors.get("product_page", {}).get("wait_selector", ".product-details, .product__details-content")
        if "/rayon.php" in url:
            return self.get_wait_selector()
        return self.selectors.get("frontpage", {}).get(
            "fallback_links",
            ".main-menu a[href*='rayon.php'], .offcanvas__area a[href*='rayon.php']",
        )

    # ------------------------------------------------------------------
    # Browser download and navigation
    # ------------------------------------------------------------------

    async def download_frontpage(self) -> Path:
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"Downloading (Playwright): {self.base_url}")

        html = await self._page_html_from_shared_context(
            self.base_url,
            self.selectors.get("frontpage", {}).get(
                "fallback_links",
                ".main-menu a[href*='rayon.php'], .offcanvas__area a[href*='rayon.php']",
            ),
        )
        save_text_atomic(html, output_path, self.logger)
        return output_path

    async def fetch_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        meta = await self.fetch_html_with_meta(url, raise_on_error=raise_on_error)
        return meta.get("html")

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

        pool = TorPool.get()
        slot = await pool.next_slot() if pool.active else 0
        attempts = 0
        last_error = None
        last_html = None
        last_status = None
        final_url = url
        content_type = None
        content_encoding = None

        for attempt in range(1, self.retry_config.max_retries + 1):
            attempts = attempt
            try:
                async with self._page_sem:
                    ctx = await self._ensure_browser_context()
                    page = await ctx.new_page()
                    try:
                        await self._set_page_headers(page)
                        response = None
                        try:
                            response = await page.goto(url, wait_until="domcontentloaded", timeout=self.page_timeout)
                        except Exception as exc:
                            last_error = str(exc) or exc.__class__.__name__
                            self.logger.debug(f"Electrosifi fetch goto did not settle for {url}: {last_error}")
                            partial_html = ""
                            try:
                                partial_html = await page.content()
                            except Exception:
                                pass
                            if len((partial_html or "").strip()) < 1000 or self._has_browser_error_marker(partial_html):
                                raise RuntimeError(f"electrosifi_navigation_failed: {last_error}") from exc
                        if response:
                            last_status = response.status
                            content_type = response.headers.get("content-type")
                            content_encoding = response.headers.get("content-encoding")
                        await self._wait_for_real_page(page, self._wait_selector_for_url(url), timeout_ms=self.page_timeout)
                        await page.wait_for_timeout(int(self.wait_after_load * 1000))
                        final_url = page.url
                        last_html = await page.content()
                    finally:
                        await page.close()

                if not last_html or not last_html.strip():
                    last_error = "empty_response"
                    raise RuntimeError(last_error)
                if not self._looks_like_usable_html(last_html, self._wait_selector_for_url(url)):
                    last_error = "electrosifi_unusable_page"
                    raise RuntimeError(last_error)

                return {
                    "html": last_html,
                    "status_code": last_status,
                    "final_url": final_url,
                    "content_type": content_type,
                    "content_encoding": content_encoding,
                    "attempts": attempts,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "blocked_signals": detect_blocked_signals(last_html, last_status),
                    "error": None,
                }
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                if attempt < self.retry_config.max_retries:
                    await asyncio.sleep(self.retry_config.get_delay(attempt))
                elif raise_on_error:
                    raise

        return {
            "html": None,
            "status_code": last_status,
            "final_url": final_url,
            "content_type": content_type,
            "content_encoding": content_encoding,
            "attempts": attempts,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "blocked_signals": detect_blocked_signals(last_html, last_status),
            "error": last_error or "fetch_failed",
        }

    def get_wait_selector(self) -> str:
        return self.selectors.get("category_page", {}).get(
            "wait_selector",
            ".single-item-pd[itemtype*='Product'], [itemscope][itemtype*='Product'], .basic-pagination",
        )

    async def scrape_single_page(self, page: Page, url: str) -> dict:
        try:
            await self._set_page_headers(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=self.page_timeout)
            await self._wait_for_real_page(page, self.get_wait_selector(), timeout_ms=self.page_timeout)
            await page.wait_for_timeout(int(self.wait_after_load * 1000))
            html = await page.content()
        except asyncio.TimeoutError:
            return {"url": url, "products": [], "error": "Page load timeout"}
        except Exception as exc:
            return {"url": url, "products": [], "error": str(exc)}

        products = self.extract_products_from_html(html)
        pagination = self.extract_pagination_from_html(html)
        return {
            "url": url,
            "products": products,
            "product_count": len(products),
            "pagination": pagination,
        }

    async def scrape_category_all_pages(
        self, context: BrowserContext, category: CategoryInfo, stats: ScrapeStats
    ) -> List[dict]:
        products = []
        async with self._page_sem:
            first = await self._scrape_category_url(category.url)
        if first.get("error"):
            self.logger.warning(f"Electrosifi category failed: {category.url} ({first['error']})")
            return []

        products.extend(first.get("products", []))
        stats.total_pages += 1
        total_pages = first.get("pagination", {}).get("total_pages", 1)

        for page_num in range(2, total_pages + 1):
            page_url = self.build_page_url(category.url, page_num)
            async with self._page_sem:
                result = await self._scrape_category_url(page_url)
            if result.get("error"):
                self.logger.debug(f"Electrosifi page failed: {page_url} ({result['error']})")
                break
            page_products = result.get("products", [])
            if not page_products:
                break
            products.extend(page_products)
            stats.total_pages += 1
        return dedupe_products(products, self.logger, "electrosifi category")

    async def _scrape_category_url(self, url: str) -> dict:
        try:
            html = await self._page_html_from_shared_context(url, self.get_wait_selector())
        except Exception as exc:
            return {"products": [], "pagination": {"total_pages": 1}, "error": str(exc)}
        products = self.extract_products_from_html(html)
        pagination = self.extract_pagination_from_html(html)
        return {"products": products, "pagination": pagination}

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = self._extract_categories_from_main_menu(tree)
        if not categories:
            categories = self._extract_categories_from_fallback_links(tree)

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _extract_categories_from_main_menu(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        fp = self.selectors.get("frontpage", {})
        categories: List[Dict[str, Any]] = []
        seen_top = set()

        for block in tree.css(fp.get("top_level_blocks", ".main-menu nav > ul > li.has-mega")):
            top_link = block.css_first(fp.get("top_level_link", "a[href*='rayon.php?rayon=']"))
            top_url = self._category_url(self._attr(top_link, "href"))
            top_name = self._text(top_link)
            if top_name:
                top_name = re.sub(r"\s+", " ", top_name).replace("  ", " ").strip()
            if not top_url or not top_name or top_url in seen_top:
                continue

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }
            seen_low = set()
            for link in block.css(fp.get("low_level_links", ".mega-menu a[href*='rayon.php'][href*='categorie=']")):
                low_url = self._category_url(self._attr(link, "href"))
                low_name = self._text(link) or self._name_from_slug(self._query_value(low_url or "", "categorie"))
                if not low_url or not low_name or low_url in seen_low:
                    continue
                if low_url == top_url:
                    continue
                top_cat["low_level_categories"].append(
                    {
                        "name": low_name,
                        "url": low_url,
                        "level": "low",
                        "subcategories": [],
                    }
                )
                seen_low.add(low_url)

            categories.append(top_cat)
            seen_top.add(top_url)
        return categories

    def _extract_categories_from_fallback_links(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        fp = self.selectors.get("frontpage", {})
        selector = fp.get(
            "fallback_links",
            ".offcanvas__area a[href*='rayon.php'], .mean-nav a[href*='rayon.php'], header a[href*='rayon.php'], nav a[href*='rayon.php']",
        )
        by_rayon: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []

        for link in tree.css(selector):
            url = self._category_url(self._attr(link, "href"))
            if not url:
                continue
            rayon = self._query_value(url, "rayon")
            categorie = self._query_value(url, "categorie")
            if not rayon:
                continue
            text = self._text(link) or self._name_from_slug(categorie or rayon)
            if not text:
                continue

            if rayon not in by_rayon:
                by_rayon[rayon] = {
                    "name": self._name_from_slug(rayon) or rayon,
                    "url": urlunsplit(
                        (
                            urlsplit(self.base_url).scheme,
                            urlsplit(self.base_url).netloc,
                            "/rayon.php",
                            urlencode({"rayon": rayon}),
                            "",
                        )
                    ),
                    "level": "top",
                    "low_level_categories": [],
                    "_seen_low": set(),
                }
                order.append(rayon)

            top = by_rayon[rayon]
            if not categorie:
                top["name"] = text
                top["url"] = url
                continue

            if url in top["_seen_low"]:
                continue
            top["low_level_categories"].append(
                {
                    "name": text,
                    "url": url,
                    "level": "low",
                    "subcategories": [],
                }
            )
            top["_seen_low"].add(url)

        categories = []
        for rayon in order:
            cat = dict(by_rayon[rayon])
            cat.pop("_seen_low", None)
            categories.append(cat)
        return categories

    @staticmethod
    def _category_stats(categories: List[Dict[str, Any]]) -> Dict[str, int]:
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

    # ------------------------------------------------------------------
    # Listings and pagination
    # ------------------------------------------------------------------

    async def extract_products_from_page(self, page: Page) -> List[dict]:
        html = await page.content()
        return self.extract_products_from_html(html)

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products: List[Dict[str, Any]] = []

        for card in tree.css(cp.get("item_selector", ".single-item-pd[itemtype*='Product'], [itemscope][itemtype*='Product']")):
            product = self._product_from_card(card)
            if product:
                products.append(product)

        return dedupe_products(products, self.logger, "electrosifi listing")

    def _product_from_card(self, card: Any) -> Optional[Dict[str, Any]]:
        cp = self.selectors.get("category_page", {})
        link = card.css_first(cp.get("item_url", ".product_name_detail a[href*='details-produit.php'], a[href*='details-produit.php'][title]"))
        url = self._product_url(self._attr(link, "href"))
        if not url:
            return None

        name = self._text(link)
        if not name:
            name_node = card.css_first(cp.get("item_name", ".product_name_detail a, [itemprop='name']"))
            name = self._text(name_node)
        if not name or name.lower() in {"accueil"}:
            name = self._attr(link, "title") or self._name_from_slug(self._query_value(url, "article"))
        if not name:
            return None

        product_id = self._attr(card.css_first(cp.get("item_id", ".love-box[data-love]")), "data-love")
        article = self._query_value(url, "article")
        product_id = product_id or article

        sku = self._first_value(
            card,
            [
                "[itemprop='sku']",
                ".sku",
                "img[src*='barcode'][alt]",
            ],
            attrs=("content", "alt", "title"),
        )

        brand = None
        brand_node = card.css_first(".filter_item_db[data-name='MarqueArticle'][data-value]")
        if brand_node:
            brand = self._attr(brand_node, "data-value") or self._attr(brand_node, "title")
        if not brand:
            brand = self._first_value(card, ["[itemprop='brand'] img[alt]", "[itemprop='brand'] img[title]"], attrs=("content", "alt", "title"))

        price = self._price_from_node(card.css_first(cp.get("item_price", "[itemprop='price'][content], .single_price strong, .single_price")))
        old_price = self._price_from_node(card.css_first(cp.get("item_old_price", "del")))
        discount_percent = self._discount_percent(self._text(card.css_first(cp.get("item_discount", ".discount"))))
        if discount_percent is None:
            discount_percent = self._computed_discount(price, old_price)

        image = None
        for img_node in card.css(cp.get("item_image", "link[itemprop='image'][href], .features-product-image img, img[src*='/product/']")):
            image = self._image_from_node(img_node)
            if image and "/brand/" not in image and "/icons/" not in image:
                break

        availability_text = self._availability_snippet(self._text(card))
        availability, available = availability_from_text(availability_text)

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": url,
            "name": name,
            "title": name,
            "shop": self.site_name,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": image,
            "sku": sku,
            "reference": sku,
            "brand": brand,
            "availability": availability,
            "available": available,
        }
        return finalize_product_record({k: v for k, v in record.items() if v is not None})

    async def extract_pagination_info(self, page: Page) -> dict:
        html = await page.content()
        return self.extract_pagination_from_html(html)

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1

        active = tree.css_first(cp.get("pagination_active", ".basic-pagination a.active"))
        if active:
            parent = getattr(active, "parent", None)
            current_page = (
                self._safe_int(self._attr(parent, "data-value") if parent else None)
                or self._safe_int(self._text(active))
                or 1
            )

        page_values = []
        for item in tree.css(cp.get("pagination_items", ".basic-pagination li.filter_item_db[data-name='page'][data-value]")):
            page_num = self._safe_int(self._attr(item, "data-value"))
            if page_num:
                page_values.append(page_num)

        if page_values:
            total_pages = max(page_values)

        text = self._tree_text(tree)
        result_match = re.search(r"Affichage\s+page\s+(\d+)\s*-\s*(\d+)\s+sur\s+(\d+)\s+r\S*sultats", text, re.I)
        total_results = None
        if result_match:
            current_page = self._safe_int(result_match.group(1)) or current_page
            total_pages = max(total_pages, self._safe_int(result_match.group(2)) or total_pages)
            total_results = self._safe_int(result_match.group(3))

        product_count = len(self.extract_products_from_html(html))
        if total_results and product_count:
            total_pages = max(total_pages, math.ceil(total_results / product_count))

        has_next = any(page_num > current_page for page_num in page_values)
        if not has_next:
            for item in tree.css(".basic-pagination li.filter_item_db[data-name='page'][data-value]"):
                title = (self._attr(item.css_first("a"), "title") or "").lower()
                page_num = self._safe_int(self._attr(item, "data-value"))
                if "suiv" in title and page_num and page_num > current_page:
                    has_next = True
                    break

        return {
            "current_page": current_page,
            "total_pages": max(1, total_pages),
            "has_next": bool(has_next),
            "total_results": total_results,
        }

    def build_page_url(self, base_url: str, page_num: int) -> str:
        url = self._absolute_url(base_url) or base_url
        parts = urlsplit(url)
        pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "page"]
        if page_num > 1:
            pairs.append(("page", str(page_num)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/rayon.php", urlencode(pairs), ""))

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, page: Page, product_url: str) -> dict:
        try:
            selector = self.selectors.get("product_page", {}).get("wait_selector", ".product-details, .product__details-content")
            async with self._page_sem:
                html = await self._page_html_from_shared_context(product_url, selector)
        except Exception as exc:
            return {
                "url": product_url,
                "error": str(exc),
                "title": None,
                "price": None,
                "availability": None,
                "available": None,
                "sku": None,
                "brand": None,
                "description": None,
                "specifications": {},
                "images": [],
            }

        return self.extract_product_details_from_html(html, product_url)

    def extract_product_details_from_html(self, html: str, product_url: str) -> dict:
        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = html_product_metadata(html, product_url, self.base_url)
        data["url"] = self._product_url(product_url) or product_url
        article = self._query_value(data["url"], "article")

        title = self._first_value(
            tree,
            [
                pp.get("title", ".product__details-content [itemprop='name'], meta[property='og:title'], h1"),
                ".product__details-content [itemprop='name']",
                "meta[property='og:title']",
                "h1",
            ],
            attrs=("content",),
        )
        if title:
            title = re.sub(r"\s+pas\s+cher\s+en\s+tunisie\s*$", "", title, flags=re.I)
            data["title"] = title
            data["name"] = title

        product_id = self._attr(tree.css_first(pp.get("product_id", ".love-box[data-love]")), "data-love") or article
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        sku = self._first_value(tree, [pp.get("sku", "[itemprop='sku'], .sku_wrapper .sku"), "[itemprop='sku']", ".sku_wrapper .sku"], attrs=("content",))
        if sku:
            data["sku"] = sku
            data["reference"] = sku

        brand = self._first_value(
            tree,
            [
                pp.get("brand", "[itemprop='brand'] img[content], [itemprop='brand'] img[alt], [itemprop='brand'] img[title]"),
                "[itemprop='brand'] img[content]",
                "[itemprop='brand'] img[alt]",
                "[itemprop='brand'] img[title]",
            ],
            attrs=("content", "alt", "title"),
        )
        if brand:
            data["brand"] = brand

        price = self._price_from_node(tree.css_first(pp.get("price", "[itemprop='price'][content], .single_price strong, .single_price")))
        if price is not None:
            data["price"] = price
        old_price = self._price_from_node(tree.css_first(pp.get("old_price", ".product__details-content del, .price del, del")))
        if old_price is not None:
            data["old_price"] = old_price
        discount = self._computed_discount(data.get("price"), data.get("old_price"))
        if discount is not None:
            data["discount_percent"] = discount

        availability_node = tree.css_first(pp.get("availability", ".product-stock, [itemprop='availability']"))
        availability_text = self._availability_snippet(self._text(availability_node))
        if not availability_text:
            availability_text = self._attr(availability_node, "href") or self._attr(availability_node, "content")
        availability, available = availability_from_text(availability_text)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        description = None
        description_node = tree.css_first(pp.get("description", ".features-des[itemprop='description'], meta[property='og:description'], meta[name='description']"))
        if description_node:
            description = self._text(description_node) or self._attr(description_node, "content")
        if description:
            data["description"] = description
            data["overview"] = description
            data["short_description"] = description

        full_description = self._text(tree.css_first(pp.get("full_description", "#des .article-description")))
        if full_description:
            data["full_description"] = full_description

        specifications = self._extract_specs(tree, pp.get("specs_rows", "#aditional .product__desc-info li, .product__desc-info li"))
        if specifications:
            data["specifications"] = specifications

        images = []
        for node in tree.css(pp.get("image_gallery", "link[itemprop='image'][href], .product-details img[src*='/product/'], .product__details-nav-thumb img")):
            image = self._image_from_node(node)
            if image and "/brand/" not in image and "/icons/" not in image and "/page/" not in image:
                images.append(image)
        images = self._dedupe_list(images)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = []
        for link in tree.css(pp.get("breadcrumbs", ".breadcrumb a[href*='rayon.php'], .product_info a[href*='rayon.php']")):
            url = self._category_url(self._attr(link, "href"))
            name = self._text(link)
            if url and name:
                categories.append({"name": name, "url": url})
        if categories:
            seen = set()
            deduped_categories = []
            for category in categories:
                key = normalize_url(category["url"]) or category["url"]
                if key in seen:
                    continue
                seen.add(key)
                deduped_categories.append(category)
            data["categories"] = deduped_categories
            data["breadcrumbs"] = [cat["name"] for cat in deduped_categories]

        data["shop"] = self.site_name
        return finalize_product_record({k: v for k, v in data.items() if v is not None})

    def _extract_specs(self, tree: HTMLParser, selector: str) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for row in tree.css(selector):
            key = self._text(row.css_first("h3, .title, th, strong"))
            value = self._text(row.css_first("span:not(.title), td:last-child"))
            if not key or not value or key == value:
                continue
            key = key.rstrip(":")
            specs[key] = value
        return specs


def get_scraper(logger: logging.Logger) -> ElectrosifiScraper:
    """Factory function used by scraper registry."""
    return ElectrosifiScraper(logger)
