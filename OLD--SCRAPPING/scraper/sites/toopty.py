#!/usr/bin/env python3
"""
toopty.net scraper — PrestaShop 1.7, Cloudflare (needs Playwright).
Categories: /{id}-{slug}
Pagination: ?p=N
Price format: "5,000 TND" (comma = thousands sep when 3 digits)
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
BASE = "https://www.toopty.net"


class TooptyScraper(FastScraper):
    """Playwright scraper for toopty.net (PrestaShop + Cloudflare)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("toopty", logger)
        self._pw = None
        self._browser = None
        self._ctx = None
        self._fetch_sem = asyncio.Semaphore(3)

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
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2500)
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

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]p=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}p={page_num}"

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
        cleaned = re.sub(r"[^\d,.]", "", str(text)).strip()
        if not cleaned:
            return None
        m = re.match(r"^(\d+),(\d{3})$", cleaned)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        m2 = re.match(r"^(\d+)\.(\d{3})$", cleaned)
        if m2:
            return float(f"{m2.group(1)}.{m2.group(2)}")
        m3 = re.match(r"^(\d+),(\d{1,2})$", cleaned)
        if m3:
            return float(f"{m3.group(1)}.{m3.group(2)}")
        cleaned = cleaned.replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        for a in tree.css("a[href*='toopty.net/']"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            path = href.replace(BASE, "").strip("/")
            # PrestaShop friendly URLs: /{id}-{slug}
            if not re.match(r"^\d+-[a-z]", path):
                continue
            if path.count("/") > 0 or path.endswith(".html"):
                continue
            if "?" in path:
                continue
            seen.add(href)
            m = re.match(r"^(\d+)-", path)
            cat_id = m.group(1) if m else None
            categories.append({
                "name": name,
                "url": href,
                "id": cat_id,
                "level": "top",
                "low_level_categories": [],
            })

        return {"categories": categories}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        for item in tree.css("article.product-miniature.js-product-miniature, .product-miniature, .js-product-miniature"):
            product = {}

            pid = item.attributes.get("data-id-product")
            if pid:
                product["id"] = str(pid)

            link = item.css_first("a.thumbnail.product-thumbnail")
            if not link:
                link = item.css_first("a[href*='.html'], h2 a, h3 a")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            name = link.attributes.get("title", "")
            if not name:
                name_el = item.css_first(".product-title, h2, h3")
                if name_el:
                    name = name_el.text()
            if name:
                product["name"] = self._clean(name)

            img = item.css_first("img[data-src], img")
            if img:
                src = img.attributes.get("data-src") or img.attributes.get("src") or ""
                if src and not src.startswith("data:"):
                    src = src.replace("home_default", "large_default")
                    product["image"] = self._abs(src)

            price_el = item.css_first("span.price:not(.regular-price), .product-price-and-shipping span.price")
            old_price_el = item.css_first(".regular-price, del span.price, s span.price")
            if price_el:
                content = price_el.attributes.get("content")
                product["price"] = self._parse_price(content or price_el.text())
            if old_price_el:
                product["old_price"] = self._parse_price(old_price_el.text())
            if product.get("price") and product.get("old_price") and product["old_price"] > product["price"]:
                product["discount_percent"] = round(
                    (1 - product["price"] / product["old_price"]) * 100
                )

            product["shop"] = "toopty"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        return tree.css_first(
            f"a.next.js-search-link, .pagination li.next a, a[rel='next'], a[href*='p={current_page + 1}']"
        ) is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css("ul.page-list li a, .pagination a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a.next.js-search-link, .pagination li.next a") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)
        name_el = tree.css_first("h1[itemprop='name'], h1.page-title, h1")
        if name_el:
            details["name"] = self._clean(name_el.text())
        price_el = tree.css_first(".current-price span.price, [itemprop='price']")
        if price_el:
            details["price"] = self._parse_price(price_el.attributes.get("content") or price_el.text())
        old_price_el = tree.css_first(".product-price .regular-price, .has-discount .regular-price")
        if old_price_el:
            details["old_price"] = self._parse_price(old_price_el.text())
        ref_el = tree.css_first(".product-reference span, [itemprop='sku']")
        if ref_el:
            details["sku"] = self._clean(ref_el.text())
        desc_el = tree.css_first("#product-description-short, [itemprop='description']")
        if desc_el:
            details["description"] = self._clean(desc_el.text())
        return details

    async def scrape_category(self, category: dict) -> List[dict]:
        url = category.get("url")
        if not url:
            return []
        all_products = []
        page = 1
        while True:
            page_url = url if page == 1 else self.build_page_url(url, page)
            html = await self.fetch_html(page_url)
            if not html:
                break
            products = self.extract_products_from_html(html, category)
            if not products:
                break
            all_products.extend(products)
            self.logger.info(f"  Page {page}: {len(products)} products ({url})")
            if not self.has_next_page(html, page):
                break
            page += 1
            await asyncio.sleep(0.5)
        return all_products

    async def scrape(self) -> dict:
        self.logger.info("Starting toopty scrape")
        await self._ensure_browser()
        html = await self.fetch_html(self.base_url)
        if not html:
            self.logger.error("Failed to fetch homepage")
            return {"products": [], "categories": []}
        cat_data = self.extract_categories_from_html(html)
        categories = cat_data.get("categories", [])
        self.logger.info(f"Found {len(categories)} categories")
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
        self.logger.info(f"Total unique products: {len(all_products)}")
        return {"products": all_products, "categories": categories}


def get_scraper(logger: logging.Logger) -> TooptyScraper:
    return TooptyScraper(logger)
