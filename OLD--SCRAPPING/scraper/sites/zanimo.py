#!/usr/bin/env python3
"""
zanimo.tn scraper — Custom Next.js pet store, client-side rendered.
Uses Playwright to render pages since content is loaded via JS.
Category URL: discovered from nav after JS render
Pagination: ?page=N or scroll-based
Price: any element with DT/TND pattern
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, playwright_launch_args

BASE = "https://zanimo.tn"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

SEED_CATEGORIES = [
    {"name": "Chiens", "url": f"{BASE}/chiens"},
    {"name": "Chats", "url": f"{BASE}/chats"},
    {"name": "Oiseaux", "url": f"{BASE}/oiseaux"},
    {"name": "Poissons", "url": f"{BASE}/poissons"},
    {"name": "Rongeurs", "url": f"{BASE}/rongeurs"},
    {"name": "Accessoires", "url": f"{BASE}/accessoires"},
    {"name": "Alimentation", "url": f"{BASE}/alimentation"},
]


class ZanimoScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("zanimo", logger)
        self._pw = None
        self._browser = None
        self._ctx = None
        self._fetch_sem = asyncio.Semaphore(3)

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True, args=playwright_launch_args())
        self._ctx = await self._browser.new_context(user_agent=UA)

    async def close(self):
        await super().close()
        if self._ctx:
            await self._ctx.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _fetch_rendered(self, url: str) -> Optional[str]:
        await self._ensure_browser()
        async with self._fetch_sem:
            page = await self._ctx.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(2000)
                return await page.content()
            except Exception as e:
                self.logger.warning(f"Playwright error {url}: {e}")
                return None
            finally:
                await page.close()

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    def _abs(self, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        return urljoin(BASE, url)

    def _clean(self, text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        m = re.search(r"[\d\s]+[.,]\d+", str(text))
        if not m:
            m = re.search(r"\d+", str(text))
        if not m:
            return None
        cleaned = re.sub(r"[^\d.]", "", m.group().replace(",", ".").replace(" ", ""))
        try:
            return float(cleaned)
        except ValueError:
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()
        for a in tree.css("nav a, header a, .menu a, a[href]"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            path = href.replace(BASE, "").strip("/")
            if not path or "/" in path or "?" in path or "#" in path:
                continue
            if any(x in path.lower() for x in ["login", "cart", "account", "contact", "about"]):
                continue
            seen.add(href)
            categories.append({"name": name, "url": href, "level": "top", "low_level_categories": []})
        if not categories:
            categories = SEED_CATEGORIES
        return {"categories": categories}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        # Generic product card scan — Next.js sites vary in class names
        seen_urls = set()
        for item in tree.css("div, article, li"):
            link = item.css_first("a[href]")
            if not link:
                continue
            href_raw = link.attributes.get("href", "")
            href = self._abs(href_raw)
            if not href or href in seen_urls:
                continue
            # Skip nav/footer links
            path = href_raw.lstrip("/")
            if not path or any(x in path for x in ["login", "cart", "contact", "#"]):
                continue

            # Must have a price to be a product
            price = None
            for el in item.css("span, p, div"):
                txt = el.text(strip=True)
                if re.search(r"\d+[.,]\d+\s*(DT|TND)", txt, re.IGNORECASE) and len(txt) < 30:
                    price = self._parse_price(txt)
                    break
            if not price:
                continue

            name_el = item.css_first("h1, h2, h3, h4, .product-name, .title")
            if not name_el:
                continue
            name = self._clean(name_el.text())
            if not name or len(name) < 3:
                continue

            seen_urls.add(href)
            product = {
                "url": href,
                "name": name,
                "price": price,
                "shop": "zanimo",
                "top_category": top_cat,
                "low_category": low_cat,
                "subcategory": subcat,
            }

            img = item.css_first("img[src], img[data-src]")
            if img:
                src = img.attributes.get("data-src") or img.attributes.get("src") or ""
                if src and not src.startswith("data:"):
                    product["image"] = self._abs(src)

            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        return tree.css_first(
            f"a[href*='page={current_page + 1}'], .next, [aria-label='next']"
        ) is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        return {"current_page": 1, "total_pages": 1, "has_next": False}

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        await self._ensure_browser()
        html = await self._fetch_rendered(category_url)
        if not html:
            return []
        products = self.extract_products_from_html(html)
        self.logger.info(f"  zanimo: {len(products)} products from {category_url}")
        return products[:limit] if limit else products

    async def scrape_product_details(self, url: str) -> dict:
        html = await self._fetch_rendered(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)

        name_el = tree.css_first("h1")
        if name_el:
            details["name"] = self._clean(name_el.text())

        for el in tree.css("span, div, p"):
            txt = el.text(strip=True)
            if re.search(r"\d+[.,]\d+\s*(DT|TND)", txt, re.IGNORECASE) and len(txt) < 30:
                details["price"] = self._parse_price(txt)
                break

        desc_el = tree.css_first(".description, .product-description, article p")
        if desc_el:
            details["description"] = self._clean(desc_el.text())

        images = []
        for img in tree.css("img[src]"):
            src = img.attributes.get("src", "")
            if src and not src.startswith("data:") and not src.endswith(".svg"):
                abs_src = self._abs(src)
                if abs_src and abs_src not in images:
                    images.append(abs_src)
        if images:
            details["images"] = images
            details.setdefault("image", images[0])

        return details


def get_scraper(logger: logging.Logger) -> ZanimoScraper:
    return ZanimoScraper(logger)
