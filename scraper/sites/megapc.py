#!/usr/bin/env python3
"""
MegaPC.tn scraper — Next.js client-rendered SPA.
Categories under /shop/{Parent}/{Child}, products under /shop/product/.../...
Requires Playwright (httpx returns the empty SSR shell with skeleton loaders).
"""

import asyncio
import logging
import re
import time
from typing import List, Optional
from urllib.parse import unquote, quote

from selectolax.parser import HTMLParser

from scraper.base import (
    FastScraper,
    detect_blocked_signals,
    playwright_launch_args,
    save_text_atomic,
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class MegapcScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("megapc", logger)
        self._pw = None
        self._browser = None
        self._ctx = None

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
        page = await self._ctx.new_page()
        status_code = None
        final_url = url
        html = None
        error = None
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status_code = resp.status if resp else None
            final_url = page.url
            # Wait for client-rendered content
            try:
                await page.wait_for_selector(
                    ".product-card, a[href^='/shop/product/'], a[href^='/shop/']",
                    timeout=15000,
                )
            except Exception:
                pass
            await asyncio.sleep(1)
            html = await page.content()
            if status_code and status_code >= 400:
                error = f"HTTP {status_code}"
            elif not html or len(html) < 500:
                error = "empty_response"
        except Exception as e:
            error = str(e) or e.__class__.__name__
            if raise_on_error:
                raise
        finally:
            await page.close()
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
    # Pagination — ?page=N
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    # ------------------------------------------------------------------
    # Categories — frontpage has /shop/{Parent}/{Child} links
    # ------------------------------------------------------------------

    def _absolute_url(self, href: str) -> str:
        if not href:
            return href
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return f"https://megapc.tn{href}"
        return f"https://megapc.tn/{href}"

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        seen = set()
        # Group by parent segment
        parents: dict = {}

        for a in tree.css("a[href^='/shop/']"):
            href = a.attributes.get("href", "")
            if not href or "/shop/product/" in href:
                continue
            # /shop/{Parent}/{Child} → 2 segments after /shop/
            path = href.split("?")[0]
            parts = [p for p in path.split("/") if p][1:]  # drop 'shop'
            if len(parts) < 2:
                continue
            if href in seen:
                continue
            seen.add(href)
            parent_name = unquote(parts[0]).replace("-", " ")
            child_name = a.text(strip=True) or unquote(parts[1]).replace("-", " ")
            url = self._absolute_url(href)

            if parent_name not in parents:
                parents[parent_name] = {
                    "name": parent_name,
                    "url": self._absolute_url(f"/shop/{parts[0]}"),
                    "level": "top",
                    "low_level_categories": [],
                }
            # Skip duplicate child URLs across parents
            existing = {c["url"] for c in parents[parent_name]["low_level_categories"]}
            if url not in existing:
                parents[parent_name]["low_level_categories"].append({
                    "name": child_name,
                    "url": url,
                    "level": "low",
                    "subcategories": [],
                })

        # Skip the synthetic "Nos Categories" parent — it duplicates real children
        categories = [v for k, v in parents.items() if k.lower() not in ("nos categories",)]
        if not categories:
            categories = list(parents.values())

        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top["low_level_categories"]:
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
        self.logger.info(f"Extracted {stats['top_level']} top, {stats['low_level']} low categories")
        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Products on category page
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        for card in tree.css(".product-card"):
            link = card.css_first("a[href*='/shop/product/']")
            if not link:
                continue
            url = self._absolute_url(link.attributes.get("href", ""))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            # Name from article title attribute or text inside link
            name = card.attributes.get("title") or link.text(strip=True)
            name = name.strip() if name else ""

            # Price — span with class containing 'inline-block' and 'font-semibold' and 'text-skin'
            price_el = card.css_first("span.inline-block.font-semibold[class*='text-skin'], span.font-semibold[class*='text-skin']")
            if not price_el:
                # any span containing DT
                for s in card.css("span"):
                    t = s.text(strip=True)
                    if t and ("DT" in t or "TND" in t):
                        price_el = s
                        break
            price = self._parse_price(price_el.text() if price_el else None)

            # Old price — line-through span
            old_el = card.css_first("span.line-through, [class*='line-through']")
            old_price = self._parse_price(old_el.text() if old_el else None)

            # Image
            img = card.css_first("img")
            image = None
            if img:
                src = img.attributes.get("src") or img.attributes.get("data-src")
                if src and not src.startswith("data:"):
                    # Next.js wraps in /_next/image?url=...
                    m = re.search(r"url=([^&]+)", src)
                    image = unquote(m.group(1)) if m else self._absolute_url(src)

            products.append({
                "id": None,
                "url": url,
                "name": name,
                "price": price,
                "old_price": old_price,
                "image": image,
            })
        return products

    # ------------------------------------------------------------------
    # Pagination — Next.js infinite-scroll; treat as single page
    # ------------------------------------------------------------------

    def extract_pagination_from_html(self, html: str) -> dict:
        # MegaPC uses lazy-loading; there's no traditional pagination link.
        # We rely on the initial load returning all products for now.
        return {"current_page": 1, "total_pages": 1, "has_next": False}

    # ------------------------------------------------------------------
    # Price parsing — handles narrow-no-break-space (U+202F) as thousand sep
    # ------------------------------------------------------------------

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        s = str(text)
        # Strip everything except digits, comma, dot
        cleaned = re.sub(r"[^\d.,]", "", s).strip()
        if not cleaned:
            return None
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            parts = cleaned.split(",")
            if len(parts[-1]) == 3:
                cleaned = cleaned.replace(",", "")
            else:
                cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Product detail
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Title
        title_el = tree.css_first("h1.text-skin-base, h1[class*='text-skin'], h1")
        data["title"] = title_el.text(strip=True) if title_el else None

        # Price
        price_el = tree.css_first("div.col-span-2 span.font-semibold, span.font-semibold[class*='text-skin']")
        if not price_el:
            for s in tree.css("span"):
                t = s.text(strip=True)
                if t and ("DT" in t or "TND" in t) and len(t) < 30:
                    price_el = s
                    break
        data["price"] = self._parse_price(price_el.text() if price_el else None)

        old_el = tree.css_first("span.line-through, [class*='line-through']")
        data["old_price"] = self._parse_price(old_el.text() if old_el else None)

        if data.get("old_price") and data.get("price") and data["old_price"] != data["price"]:
            data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)

        # SKU / reference — look for "Réf:" or similar
        data["sku"] = None
        body_text = tree.css_first("main")
        if body_text:
            txt = body_text.text(strip=True)
            m = re.search(r"R[ée]f[\s.:]+([A-Z0-9\-_/]+)", txt or "", re.I)
            if m:
                data["sku"] = m.group(1)

        # Availability — look for "En stock" / "Rupture" text
        full_text = (tree.css_first("body").text() or "").lower() if tree.css_first("body") else ""
        if "rupture" in full_text or "épuisé" in full_text or "indisponible" in full_text:
            data["availability"] = "Rupture de stock"
            data["available"] = False
        elif "en stock" in full_text or "disponible" in full_text:
            data["availability"] = "En stock"
            data["available"] = True
        else:
            data["availability"] = None
            data["available"] = None

        # Description — look for product description section
        desc_el = tree.css_first("[class*='description'], main p")
        if desc_el:
            data["description"] = re.sub(r"\s+", " ", desc_el.text(strip=True))[:2000]
        else:
            data["description"] = None

        # Brand — first try to extract from title
        data["brand"] = None
        if data.get("title"):
            first_word = data["title"].split()[0] if data["title"] else ""
            if first_word and first_word.isalpha() and first_word.isupper() or len(first_word) > 2:
                data["brand"] = first_word

        # Images — extract from /_next/image?url=... srcs
        images = []
        for img in tree.css("main img, [class*='gallery'] img, img"):
            src = img.attributes.get("src", "")
            if not src or src.startswith("data:"):
                continue
            m = re.search(r"url=([^&]+)", src)
            real = unquote(m.group(1)) if m else self._absolute_url(src)
            if real and "static.gi-ga.tech" in real and real not in images:
                images.append(real)
        data["images"] = images[:10]
        data["specifications"] = {}
        return data


def get_scraper(logger: logging.Logger) -> MegapcScraper:
    return MegapcScraper(logger)
