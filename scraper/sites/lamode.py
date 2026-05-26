#!/usr/bin/env python3
"""
Lamode.tn scraper — PrestaShop, Cloudflare-blocks httpx. Uses Playwright with a
shared browser context across all fetches in a single scrape run.
Categories at /{id}-{slug}, pagination ?page=N.
"""

import asyncio
import json as _json
import logging
import re
import time
from typing import List, Optional

from selectolax.parser import HTMLParser

from scraper.base import (
    FastScraper,
    detect_blocked_signals,
    playwright_launch_args,
    save_text_atomic,
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class LamodeScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("lamode", logger)
        self._pw = None
        self._browser = None
        self._ctx = None
        # Allow up to 4 concurrent Playwright pages — each using a fresh
        # context which makes Cloudflare treat them as independent visitors.
        self._fetch_lock = asyncio.Semaphore(4)

    # ------------------------------------------------------------------
    # Shared Playwright browser (lazy init)
    # ------------------------------------------------------------------

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True, args=playwright_launch_args()
        )
        self._ctx = await self._browser.new_context(user_agent=UA)

    async def close(self):
        await super().close()
        if self._ctx:
            await self._ctx.close()
            self._ctx = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def fetch_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        meta = await self.fetch_html_with_meta(url, raise_on_error=raise_on_error)
        return meta.get("html")

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> dict:
        started = time.monotonic()
        await self._ensure_browser()
        status_code = None
        final_url = url
        html = None
        error = None
        # Cloudflare on lamode.tn 403s subsequent requests through the same
        # browser context. Use a fresh context per fetch (treated as a new
        # visitor) and retry once on failure.
        async with self._fetch_lock:
            for attempt in range(1, 3):
                ctx = await self._browser.new_context(user_agent=UA)
                page = await ctx.new_page()
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    status_code = resp.status if resp else None
                    final_url = page.url
                    try:
                        await page.wait_for_selector(
                            "article.product-miniature, article.js-product-miniature, "
                            "h1.product-name, .product-details, h1",
                            timeout=8000,
                        )
                    except Exception:
                        pass
                    html = await page.content()
                finally:
                    await page.close()
                    await ctx.close()

                if status_code and status_code >= 400:
                    error = f"HTTP {status_code}"
                    if attempt < 2:
                        await asyncio.sleep(2)
                        continue
                    break
                if not html or len(html) < 500:
                    error = "empty_response"
                    if attempt < 2:
                        await asyncio.sleep(1)
                        continue
                    break
                error = None
                break
        return {
            "html": None if error else html,
            "status_code": status_code,
            "final_url": final_url,
            "content_type": None,
            "content_encoding": None,
            "attempts": 1,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "blocked_signals": detect_blocked_signals(html, status_code),
            "error": error,
        }

    # ------------------------------------------------------------------
    # Pagination — PrestaShop ?page=N
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    # NOTE: we removed the sequential override — the base class fires up to
    # 4 concurrent page fetches per category, which is fine because each
    # fetch uses a fresh browser context (Cloudflare treats them independently)
    # and the per-instance semaphore caps total concurrency.

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, href: str) -> str:
        if not href:
            return href
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return f"https://www.lamode.tn{href}"
        return f"https://www.lamode.tn/{href}"

    @staticmethod
    def _is_category_url(href: str) -> bool:
        if not href:
            return False
        path = href.split("?")[0].split("#")[0]
        path = re.sub(r"^https?://[^/]+", "", path)
        if path.endswith(".html"):
            return False
        return bool(re.match(r"^/\d+[-_][a-z]", path))

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        seen_urls = set()
        categories = []

        # Scan all PrestaShop /id-slug/ links across the page.
        # Use a flat top-level structure (Playwright frontpage exposes 200+ categories;
        # without a clear menu hierarchy we treat them as top-level).
        for a in tree.css("a[href]"):
            href = self._absolute_url(a.attributes.get("href") or "")
            if not self._is_category_url(href):
                continue
            # Strip query string from URL (filtered category variants)
            href = href.split("?")[0]
            if href in seen_urls:
                continue
            name = a.text(strip=True)
            if not name or len(name) > 80:
                continue
            seen_urls.add(href)
            categories.append({
                "name": name, "url": href, "level": "top", "low_level_categories": [],
            })

        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": len(categories)}
        self.logger.info(f"Extracted {stats['top_level']} categories")
        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Products on category page
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        for card in tree.css("article.js-product-miniature, article.product-miniature, div.js-product-miniature"):
            # URL — prefer a.thumbnail.product-thumbnail or h3.product-name a (lamode markup)
            link = card.css_first(
                "a.thumbnail.product-thumbnail, h3.product-name a, "
                "h2.product-title a, h3.product-title a"
            )
            if not link:
                # Fallback: any anchor pointing to a product page (.html suffix, not a manufacturer link)
                for a in card.css("a[href*='.html']"):
                    h = a.attributes.get("href", "")
                    if "/manufacturer/" not in h and h not in seen_urls:
                        link = a
                        break
            url = self._absolute_url(link.attributes.get("href", "")) if link else None
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            # Name — h3.product-name (lamode) or h2/h3.product-title (other themes)
            name_el = card.css_first(
                "h3.product-name, h2.product-title, h3.product-title, .product-name, .product-title"
            )
            name = name_el.text(strip=True) if name_el else (link.text(strip=True) if link else "")
            if not name or name.endswith("..."):
                img = card.css_first("img[alt]")
                if img:
                    alt = (img.attributes.get("alt") or "").strip()
                    if alt:
                        name = alt

            # Brand — lamode shows p.product-manufacturer-title above the name
            brand = None
            brand_el = card.css_first(".product-manufacturer-title, .product-manufacturer span, .product-manufacturer a")
            if brand_el:
                brand = brand_el.text(strip=True) or None

            # Price
            price_el = card.css_first(".product-price-and-shipping span.price, span.price, .price")
            old_el = card.css_first("span.regular-price, .regular-price")
            price = self._parse_price(price_el.text() if price_el else None)
            old_price = self._parse_price(old_el.text() if old_el else None)

            # Image
            img = card.css_first("picture img, img.product_image, img.product-thumbnail, img.replace-2x, img")
            image = None
            if img:
                image = (
                    img.attributes.get("data-src")
                    or img.attributes.get("data-full-size-image-url")
                    or img.attributes.get("src")
                )
                if image and image.startswith("data:"):
                    image = None

            pid = card.attributes.get("data-id-product")
            products.append({
                "id": pid, "url": url, "name": name, "brand": brand,
                "price": price, "old_price": old_price, "image": image,
            })
        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css(".page-list a, ul.pagination a"):
            try:
                n = int(a.text(strip=True))
                if n > max_page:
                    max_page = n
            except ValueError:
                pass
        has_next = tree.css_first("a.next, a[rel='next']") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", str(text)).strip()
        if not cleaned:
            return None
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        elif "." in cleaned:
            parts = cleaned.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
                cleaned = cleaned.replace(".", "")
        try:
            return float(cleaned)
        except ValueError:
            return None

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        title_el = tree.css_first("h1[itemprop='name'], h1.h1, h1.product-name, h1.page-title, h1")
        data["title"] = title_el.text(strip=True) if title_el else None

        sku_el = tree.css_first(".product-reference span, span[itemprop='sku'], div.product-reference span")
        data["sku"] = sku_el.text(strip=True) if sku_el else None

        # Price — multiple PS variants
        price = None
        for el in tree.css(
            "span[itemprop='price'], "
            ".current-price span[itemprop='price'], "
            ".current-price span, span.current-price, "
            ".product-prices .current-price span"
        ):
            content = el.attributes.get("content")
            v = self._parse_price(content) if content else self._parse_price(el.text())
            if v:
                price = v
                break
        if price is None:
            for el in tree.css('meta[property="product:price:amount"], meta[itemprop="price"]'):
                v = self._parse_price(el.attributes.get("content"))
                if v:
                    price = v
                    break
        data["price"] = price

        old_el = tree.css_first(".regular-price, span.regular-price")
        data["old_price"] = self._parse_price(old_el.text() if old_el else None)

        if data.get("old_price") and data.get("price") and data["old_price"] != data["price"]:
            data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)

        # Brand from JSON-LD or .product-manufacturer
        brand = None
        for script in tree.css('script[type="application/ld+json"]'):
            raw = (script.text() or "").strip()
            if not raw:
                continue
            try:
                d = _json.loads(raw)
            except Exception:
                continue
            blocks = d if isinstance(d, list) else [d]
            for b in blocks:
                if isinstance(b, dict) and b.get("@type") == "Product":
                    br = b.get("brand")
                    if isinstance(br, dict):
                        brand = br.get("name")
                    elif isinstance(br, str):
                        brand = br
                    if brand:
                        break
            if brand:
                break
        if not brand:
            brand_el = tree.css_first(".product-manufacturer a, .product-manufacturer img, .product-manufacturer span")
            if brand_el:
                brand = brand_el.attributes.get("alt") or brand_el.text(strip=True) or None
        data["brand"] = brand

        # Availability
        avail_el = tree.css_first("#product-availability, #stock_availability, .product-availability span")
        if avail_el:
            txt = avail_el.text(strip=True)
            data["availability"] = txt
            data["available"] = bool(re.search(r"stock|disponible|in stock", txt or "", re.I))
        else:
            data["availability"] = None
            data["available"] = None

        # Description
        desc_el = tree.css_first(
            "#description, .product-description, div[itemprop='description'], "
            "#product-description, div.product-description-short"
        )
        if desc_el:
            data["description"] = re.sub(r"\s+", " ", desc_el.text(strip=True))[:2000]
        else:
            data["description"] = None

        # Specs
        specs = {}
        for row in tree.css("section.product-features dl.data-sheet div, table.data-sheet tr, dl.data-sheet > div"):
            dt = row.css_first("dt")
            dd = row.css_first("dd")
            if dt and dd:
                k = dt.text(strip=True)
                v = dd.text(strip=True)
                if k and v:
                    specs[k] = v
        data["specifications"] = specs

        # Images
        images = []
        for img in tree.css(".product-cover img, .js-thumbnails img, .thumb-container img, .product-images img"):
            src = img.attributes.get("data-image-large-src") or img.attributes.get("src")
            if src and not src.startswith("data:") and src not in images:
                images.append(self._absolute_url(src))
        data["images"] = images[:10]
        return data


def get_scraper(logger: logging.Logger) -> LamodeScraper:
    return LamodeScraper(logger)
