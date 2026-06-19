#!/usr/bin/env python3
"""
BSTech.tn scraper — Custom React/Vite SPA backed by Supabase.
Categories from sitemap.xml (186 cats), products at /site/product/{slug}.
Requires Playwright for both category page rendering and product details.
"""

import asyncio
import logging
import re
import time
from typing import List, Optional
from urllib.parse import unquote

from selectolax.parser import HTMLParser

from scraper.base import (
    FastScraper,
    detect_blocked_signals,
    playwright_launch_args,
    save_text_atomic,
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class BstechScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("bstech", logger)
        self._pw = None
        self._browser = None
        self._ctx = None

    # ------------------------------------------------------------------
    # Shared Playwright browser
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
            # Wait for product anchors or detail page content to render
            try:
                await page.wait_for_selector(
                    "a[href^='/site/product/'], h1.font-black, h1, span.text-price",
                    timeout=15000,
                )
            except Exception:
                pass
            await asyncio.sleep(2)
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
    # Frontpage override — fetch sitemap.xml directly (httpx) and use it
    # as the category source (the live React menu only exposes 1 category).
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        import httpx
        output_path = self.html_dir / "frontpage.html"
        sitemap_url = "https://www.bstech.tn/sitemap.xml"
        self.logger.info(f"Downloading bstech sitemap: {sitemap_url}")
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": UA}, follow_redirects=True, timeout=30
            ) as c:
                r = await c.get(sitemap_url)
                r.raise_for_status()
                save_text_atomic(r.text, output_path, self.logger)
                return output_path
        except Exception as e:
            self.logger.warning(f"Sitemap fetch failed: {e} — falling back to Playwright frontpage")
            raw = await self.fetch_html("https://www.bstech.tn/")
            if not raw:
                raise RuntimeError("Failed to fetch bstech frontpage and sitemap")
            save_text_atomic(raw, output_path, self.logger)
            return output_path

    # ------------------------------------------------------------------
    # Pagination — ?page=N
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    def _absolute_url(self, href: str) -> str:
        if not href:
            return href
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return f"https://www.bstech.tn{href}"
        return f"https://www.bstech.tn/{href}"

    # ------------------------------------------------------------------
    # Categories — parse from sitemap.xml or HTML fallback
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        categories = []
        seen_urls = set()

        # If it's the sitemap (XML), parse the <loc> tags
        if html.lstrip().startswith("<?xml"):
            for url in re.findall(r"<loc>([^<]+)</loc>", html):
                if "/category/" not in url or "?" in url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                slug = url.rsplit("/", 1)[-1].rstrip("/")
                name = slug.replace("-", " ").replace("_", " ").strip().title()
                if not name:
                    continue
                categories.append({
                    "name": name, "url": url, "level": "top", "low_level_categories": [],
                })
        else:
            # HTML fallback
            tree = HTMLParser(html)
            for a in tree.css("a[href^='/category/']"):
                href = a.attributes.get("href", "")
                url = self._absolute_url(href)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                name = a.text(strip=True)
                if not name:
                    slug = href.rsplit("/", 1)[-1]
                    name = slug.replace("-", " ").title()
                categories.append({
                    "name": name, "url": url, "level": "top", "low_level_categories": [],
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

        for a in tree.css("a[href^='/site/product/']"):
            href = a.attributes.get("href", "")
            url = self._absolute_url(href)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            # Walk up to find the card container (each product is 2 anchors
            # sharing the same href: image + title)
            node = a
            for _ in range(4):
                if node.parent is None:
                    break
                node = node.parent

            # Name from h3 (font-semibold)
            name = ""
            if node:
                name_el = node.css_first("h3.font-semibold, h3, .font-semibold")
                if name_el:
                    name = name_el.text(strip=True)
            if not name:
                # Try img alt
                img = a.css_first("img")
                if img:
                    name = img.attributes.get("alt", "").strip()
            if not name:
                name = a.text(strip=True)[:200]

            # Price from span.text-price (multiple in a card if discounted)
            price = None
            old_price = None
            if node:
                price_els = node.css("span.text-price, .text-price")
                values = []
                for el in price_els:
                    t = el.text(strip=True)
                    v = self._parse_price(t)
                    if v and v > 0:
                        values.append(v)
                if values:
                    if len(values) >= 2:
                        price = min(values)
                        old_price = max(values) if max(values) != price else None
                    else:
                        price = values[0]
                # Fallback: regex on card text
                if price is None:
                    txt = node.text(strip=True)
                    matches = re.findall(r"(\d[\d\s.,]*?)\s*(?:DT|TND)", txt)
                    vals = [self._parse_price(m) for m in matches]
                    vals = [v for v in vals if v and v > 0]
                    if vals:
                        price = min(vals)
                        if len(vals) >= 2 and max(vals) != price:
                            old_price = max(vals)

            # Image from supabase
            image = None
            img = a.css_first("img[src*='supabase']") or a.css_first("img")
            if img:
                src = img.attributes.get("src") or img.attributes.get("data-src")
                if src and not src.startswith("data:"):
                    image = src

            products.append({
                "id": None, "url": url, "name": name,
                "price": price, "old_price": old_price, "image": image,
            })
        return products

    # ------------------------------------------------------------------
    # Pagination — bstech category page lists everything on one page (no real pagination)
    # ------------------------------------------------------------------

    def extract_pagination_from_html(self, html: str) -> dict:
        return {"current_page": 1, "total_pages": 1, "has_next": False}

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        s = str(text).replace("\xa0", " ").replace(" ", " ")
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
        elif "." in cleaned:
            parts = cleaned.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
                cleaned = cleaned.replace(".", "")
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

        # Title — h1.font-black (text-2xl or text-3xl variant)
        title_el = tree.css_first("h1.font-black, h1[class*='font-black'], h1")
        data["title"] = title_el.text(strip=True) if title_el else None

        # Réf and SKU are in separate spans: "<span>Réf: 110110143</span><span>SKU: 82QY00PEFE</span>"
        data["sku"] = None
        data["reference"] = None
        for span in tree.css("span"):
            t = span.text(strip=True)
            if not t:
                continue
            m = re.match(r"R[ée]f\s*[:.]\s*(\S+)", t, re.I)
            if m and not data["reference"]:
                data["reference"] = m.group(1).strip()
            m = re.match(r"SKU\s*[:.]\s*(\S+)", t, re.I)
            if m and not data["sku"]:
                data["sku"] = m.group(1).strip()
        # Use SKU if available else reference
        if not data["sku"]:
            data["sku"] = data.get("reference")

        # Price — span.text-4xl or text-5xl font-black with red color (text-[#c1121f])
        price_el = tree.css_first(
            "span.text-4xl.font-black, span.text-5xl.font-black, "
            "span[class*='text-4xl'][class*='font-black'], "
            "span[class*='text-5xl'][class*='font-black']"
        )
        data["price"] = self._parse_price(price_el.text()) if price_el else None

        # Old price — line-through span
        old_el = tree.css_first("span.line-through, span[class*='line-through']")
        data["old_price"] = self._parse_price(old_el.text()) if old_el else None

        # Fallback to text-price spans if specific classes not found
        if data["price"] is None:
            for el in tree.css("span.text-price, .text-price"):
                v = self._parse_price(el.text(strip=True))
                if v and v > 0:
                    data["price"] = v
                    break

        if data.get("old_price") and data.get("price") and data["old_price"] != data["price"]:
            data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)

        # Brand — orange button linking to /marques/{brand}
        brand_el = tree.css_first("a[href*='/marques/']")
        if brand_el:
            data["brand"] = brand_el.text(strip=True)
        else:
            brand_el = tree.css_first("img[alt$='logo'], img[alt*=' logo']")
            data["brand"] = brand_el.attributes.get("alt", "").replace(" logo", "").strip() if brand_el else None

        # Availability — green status badge
        avail_el = tree.css_first("span.text-success, span[class*='text-success']")
        if avail_el:
            data["availability"] = avail_el.text(strip=True)
            data["available"] = True
        else:
            body = tree.css_first("body")
            body_lower = (body.text() if body else "").lower()
            if "rupture" in body_lower or "indisponible" in body_lower:
                data["availability"] = "Rupture de stock"
                data["available"] = False
            elif "sur commande" in body_lower:
                data["availability"] = "Sur commande"
                data["available"] = True
            elif "en stock" in body_lower:
                data["availability"] = "En stock"
                data["available"] = True
            else:
                data["availability"] = None
                data["available"] = None

        # Description — div.product-description (rich HTML)
        desc_el = tree.css_first("div.product-description, [class*='product-description']")
        if desc_el:
            txt = re.sub(r"\s+", " ", desc_el.text(strip=True))
            data["description"] = txt[:2000] or None
        else:
            data["description"] = None

        # Specs — table with th/td rows under "Caractéristiques techniques"
        specs = {}
        for row in tree.css("table tr"):
            ths = row.css("th")
            tds = row.css("td")
            # Skip rowgroup headers (rowspan attribute) — only use single-column th + td
            if not ths or not tds:
                continue
            # Find the th that is NOT rowgroup (label th, not section header)
            label_th = None
            for th in ths:
                if not th.attributes.get("rowspan") and th.attributes.get("scope") != "rowgroup":
                    label_th = th
                    break
            if not label_th:
                continue
            k = label_th.text(strip=True)
            v = tds[0].text(strip=True)
            if k and v and len(k) < 80:
                specs[k] = v
        data["specifications"] = specs

        # Images — supabase storage CDN (filter out logo and small thumbs)
        images = []
        for img in tree.css("img[src*='supabase']"):
            src = img.attributes.get("src", "")
            if not src or src in images:
                continue
            # Skip very small thumbnails (width=96 or =72)
            if "width=96" in src or "width=72" in src:
                continue
            # Skip logo-style imgs (alt contains "logo")
            alt = (img.attributes.get("alt", "") or "").lower()
            if "logo" in alt:
                continue
            images.append(src)
        data["images"] = images[:10]
        return data


def get_scraper(logger: logging.Logger) -> BstechScraper:
    return BstechScraper(logger)
