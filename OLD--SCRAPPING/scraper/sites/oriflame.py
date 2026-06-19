#!/usr/bin/env python3
"""
tn.oriflame.com scraper — Next.js + F5 Volterra WAF (Playwright).
Base URL: https://tn.oriflame.com  (no locale prefix)
Categories: hardcoded slug list (not in server-rendered HTML)
Products: a[data-testid^="Presentation-product-box-"]
Pagination: "Afficher plus" load-more button (JS AJAX, must click)
Price format: "29.90 DT" (dot = decimal separator)
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, playwright_launch_args

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
STEALTH_JS = 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
BASE = "https://tn.oriflame.com"

# Static category list — the main category tree is JS-only, these are all
# the server-rendered category slugs available on this subdomain
CATEGORIES = [
    {"name": "Nouveaux arrivages", "url": f"{BASE}/new"},
    {"name": "Nouveautés Soins de la peau", "url": f"{BASE}/new/skincare"},
    {"name": "Nouveautés Maquillage", "url": f"{BASE}/new/makeup"},
    {"name": "Nouveautés Parfums", "url": f"{BASE}/new/fragrance"},
    {"name": "Nouveautés Bain et Corps", "url": f"{BASE}/new/bath-body"},
    {"name": "Nouveautés Cheveux", "url": f"{BASE}/new/hair"},
    {"name": "Nouveautés Accessoires", "url": f"{BASE}/new/accessories"},
    {"name": "Nouveautés Hommes", "url": f"{BASE}/new/men"},
    {"name": "Bestsellers", "url": f"{BASE}/bestsellers"},
    {"name": "Bestsellers Soins de la peau", "url": f"{BASE}/bestsellers/skincare"},
    {"name": "Bestsellers Maquillage", "url": f"{BASE}/bestsellers/makeup"},
    {"name": "Bestsellers Parfums", "url": f"{BASE}/bestsellers/fragrance"},
    {"name": "Bestsellers Bain et Corps", "url": f"{BASE}/bestsellers/bath-body"},
    {"name": "Bestsellers Cheveux", "url": f"{BASE}/bestsellers/hair"},
    {"name": "Bestsellers Accessoires", "url": f"{BASE}/bestsellers/accessories"},
    {"name": "Bestsellers Hommes", "url": f"{BASE}/bestsellers/men"},
    {"name": "Offres", "url": f"{BASE}/focus"},
]


class OriflameScaper(FastScraper):
    """Playwright scraper for tn.oriflame.com (Next.js + F5 WAF)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("oriflame", logger)
        self._pw = None
        self._browser = None
        self._ctx = None
        self._fetch_sem = asyncio.Semaphore(2)

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True, args=playwright_launch_args()
        )
        self._ctx = await self._browser.new_context(user_agent=UA)
        await self._ctx.add_init_script(STEALTH_JS)

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
        await self._ensure_browser()
        async with self._fetch_sem:
            page = await self._ctx.new_page()
            html = None
            try:
                resp = await page.goto(url, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(3000)
                html = await page.content()
                if resp and resp.status >= 400:
                    self.logger.warning(f"HTTP {resp.status}: {url}")
                    if raise_on_error:
                        return None
            except Exception as e:
                self.logger.warning(f"Playwright fetch error {url}: {e}")
                if raise_on_error:
                    raise
            finally:
                await page.close()
            return html

    async def _fetch_all_products_on_page(self, url: str) -> Optional[str]:
        """Load a category page and click 'Afficher plus' until all products are shown."""
        await self._ensure_browser()
        page = await self._ctx.new_page()
        html = None
        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(3000)

            # Click "Afficher plus" (Load More) until it disappears
            for _ in range(50):
                btn = page.locator("button:has-text('Afficher plus')")
                if await btn.count() == 0:
                    break
                try:
                    await btn.first.scroll_into_view_if_needed()
                    await btn.first.click()
                    await page.wait_for_timeout(2000)
                except Exception:
                    break

            html = await page.content()
        except Exception as e:
            self.logger.warning(f"Playwright fetch error {url}: {e}")
        finally:
            await page.close()
        return html

    def build_page_url(self, base_url: str, page_num: int) -> str:
        # Not used — pagination is click-based, handled in _fetch_all_products_on_page
        return base_url

    def _abs(self, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return "https:" + url
        return urljoin(BASE, url)

    def _clean(self, text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        # "29.90 DT" — dot is decimal separator on this site
        cleaned = re.sub(r"[^\d.]", "", str(text)).strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        # Categories are hardcoded — not in server-rendered HTML
        return {"categories": [dict(c) for c in CATEGORIES]}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        for item in tree.css("a[data-testid^='Presentation-product-box-']"):
            # Skip non-product testids (images, labels, etc.)
            testid = item.attributes.get("data-testid", "")
            if not re.match(r"^Presentation-product-box-\d+$", testid):
                continue

            product = {}

            m = re.search(r"-(\d+)$", testid)
            if m:
                product["id"] = m.group(1)

            href = self._abs(item.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            name_el = item.css_first("p[data-testid^='Presentation-product-box-name-']")
            if name_el:
                product["name"] = self._clean(name_el.text())

            brand_el = item.css_first("span[data-testid^='Presentation-product-box-brand-']")
            if brand_el:
                product["brand"] = self._clean(brand_el.text())

            img = item.css_first("img[data-testid^='Presentation-product-box-img-']")
            if not img:
                img = item.css_first("img[src]")
            if img:
                src = img.attributes.get("src") or ""
                if src and not src.startswith("data:"):
                    product["image"] = src

            price_el = item.css_first("p[data-testid^='Presentation-product-box-current-price-']")
            if price_el:
                product["price"] = self._parse_price(price_el.text())

            old_price_el = item.css_first("p[data-testid^='Presentation-product-box-old-price-']")
            if old_price_el:
                product["old_price"] = self._parse_price(old_price_el.text())

            if product.get("price") and product.get("old_price") and product["old_price"] > product["price"]:
                product["discount_percent"] = round(
                    (1 - product["price"] / product["old_price"]) * 100
                )

            product["shop"] = "oriflame"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        # Not used — all pages loaded via button clicking in _fetch_all_products_on_page
        return False

    def extract_pagination_from_html(self, html: str) -> dict:
        return {"current_page": 1, "total_pages": 1, "has_next": False}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)
        name_el = tree.css_first("h1[data-testid='Presentation-product-detail-name'], h1")
        if name_el:
            details["name"] = self._clean(name_el.text())
        price_el = tree.css_first("[data-testid='Presentation-product-detail-current-price']")
        if price_el:
            details["price"] = self._parse_price(price_el.text())
        sku_el = tree.css_first("[data-testid='Presentation-product-detail-code'], [data-testid*='product-code']")
        if sku_el:
            details["sku"] = self._clean(sku_el.text())
        desc_el = tree.css_first("[data-testid='Presentation-product-detail-description'], .product-description")
        if desc_el:
            details["description"] = self._clean(desc_el.text())
        return details

    async def scrape_category(self, category: dict) -> List[dict]:
        url = category.get("url")
        if not url:
            return []
        self.logger.info(f"  Loading all products for: {url}")
        html = await self._fetch_all_products_on_page(url)
        if not html:
            return []
        products = self.extract_products_from_html(html, category)
        self.logger.info(f"  Found {len(products)} products ({url})")
        return products

    async def scrape(self) -> dict:
        self.logger.info("Starting oriflame scrape")
        await self._ensure_browser()
        categories = [dict(c) for c in CATEGORIES]
        self.logger.info(f"Using {len(categories)} hardcoded categories")
        all_products = []
        seen_ids = set()
        for cat in categories:
            cat_info = {"url": cat["url"], "top_category": cat["name"], "low_category": "", "subcategory": ""}
            prods = await self.scrape_category(cat_info)
            for p in prods:
                uid = p.get("id") or p.get("url")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    all_products.append(p)
            await asyncio.sleep(1.0)
        self.logger.info(f"Total unique products: {len(all_products)}")
        return {"products": all_products, "categories": categories}


def get_scraper(logger: logging.Logger) -> OriflameScaper:
    return OriflameScaper(logger)
