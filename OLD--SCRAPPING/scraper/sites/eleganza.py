#!/usr/bin/env python3
"""
eleganza.tn scraper — Custom Tijarati platform, Cloudflare (needs Playwright).
Categories: /categories/{id}/{slug}/
Pagination: ?page=N
Price format: "259,000 DT" (comma = thousands sep), "del" for old price
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
BASE = "https://eleganza.tn"


class EleganzaScraper(FastScraper):
    """Playwright scraper for eleganza.tn (Tijarati + Cloudflare)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("eleganza", logger)
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
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

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
        m2 = re.match(r"^(\d+),(\d{1,2})$", cleaned)
        if m2:
            return float(f"{m2.group(1)}.{m2.group(2)}")
        cleaned = cleaned.replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        for a in tree.css("a[href*='/categories/']"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            if "/page/" in href or "?" in href:
                continue
            seen.add(href)
            categories.append({
                "name": name,
                "url": href,
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

        for item in tree.css(".product-card.animate-underline, .product-card"):
            product = {}

            link = item.css_first("a[href*='/products/']")
            if not link:
                link = item.css_first("a[href]")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            name_el = item.css_first("h3 span.animate-target, h3 a span, h3 a, h3, .product-title")
            if name_el:
                product["name"] = self._clean(name_el.text())

            img = item.css_first("img")
            if img:
                src = img.attributes.get("src") or img.attributes.get("data-src") or ""
                if src and not src.startswith("data:"):
                    product["image"] = self._abs(src)

            # Price: .h5 element, with optional del inside for old price
            price_block = item.css_first(".h5, .price, [class*='price']")
            if price_block:
                del_el = price_block.css_first("del")
                if del_el:
                    product["old_price"] = self._parse_price(del_el.text())
                    del_text = del_el.text()
                    full_text = price_block.text()
                    current_text = full_text.replace(del_text, "").strip()
                    product["price"] = self._parse_price(current_text)
                else:
                    product["price"] = self._parse_price(price_block.text())

            if product.get("price") and product.get("old_price") and product["old_price"] > product["price"]:
                product["discount_percent"] = round(
                    (1 - product["price"] / product["old_price"]) * 100
                )

            product["shop"] = "eleganza"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        next_el = tree.css_first(
            "a[rel='next'], .pagination a.next, "
            f"a[href*='page={current_page + 1}']"
        )
        return next_el is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css(".pagination a, nav.pagination a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a[rel='next'], .pagination a.next") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)

        name_el = tree.css_first("h1")
        if name_el:
            details["name"] = self._clean(name_el.text())

        # SKU / reference — Tijarati platform uses .product-reference or span.sku
        sku_el = tree.css_first(".product-reference span, span.sku, [itemprop='sku'], .reference span")
        if sku_el:
            details["sku"] = self._clean(sku_el.text())
        else:
            for el in tree.css("p, span, li, div"):
                txt = el.text(strip=True)
                if re.match(r"^(Réf|Ref|SKU|UGS|Référence)\s*[:\-]", txt, re.IGNORECASE):
                    m = re.search(r"[:\-]\s*(\S+)", txt)
                    if m:
                        details["sku"] = m.group(1)
                        break

        price_block = tree.css_first(".product-price, .price, [class*='price']")
        if price_block:
            del_el = price_block.css_first("del")
            if del_el:
                details["old_price"] = self._parse_price(del_el.text())
                full_text = price_block.text().replace(del_el.text(), "").strip()
                details["price"] = self._parse_price(full_text)
            else:
                details["price"] = self._parse_price(price_block.text())

        if details.get("price") and details.get("old_price") and details["old_price"] > details["price"]:
            details["discount_percent"] = round(
                (1 - details["price"] / details["old_price"]) * 100
            )

        images = []
        for img in tree.css(".product-gallery img, .product-images img, img[itemprop='image']"):
            src = img.attributes.get("src") or img.attributes.get("data-src") or ""
            if src and not src.startswith("data:"):
                abs_src = self._abs(src)
                if abs_src and abs_src not in images:
                    images.append(abs_src)
        if images:
            details["images"] = images
            if not details.get("image"):
                details["image"] = images[0]

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
        self.logger.info("Starting eleganza scrape")
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
            cat_info = {
                "url": cat["url"],
                "top_category": cat["name"],
                "low_category": "",
                "subcategory": "",
            }
            prods = await self.scrape_category(cat_info)
            for p in prods:
                uid = p.get("url")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    all_products.append(p)

        self.logger.info(f"Total unique products: {len(all_products)}")
        return {"products": all_products, "categories": categories}


def get_scraper(logger: logging.Logger) -> EleganzaScraper:
    return EleganzaScraper(logger)
