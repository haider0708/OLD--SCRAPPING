#!/usr/bin/env python3
"""
infotec.tn scraper — WooCommerce/WordPress, Cloudflare (needs Playwright).
Categories: /fr/c/{slug}/
Products:   /fr/p/{slug}/
Pagination: /fr/c/{slug}/page/{n}/
Price format: "21,000 DT" or "399,900 DT" (comma = thousands sep when 3 digits after)
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import (
    FastScraper,
    playwright_launch_args,
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
STEALTH_JS = 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
BASE = "https://infotec.tn"


class InfotecScraper(FastScraper):
    """Playwright scraper for infotec.tn (WooCommerce + Cloudflare)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("infotec", logger)
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
        base = base_url.rstrip("/")
        # Remove existing /page/N
        base = re.sub(r"/page/\d+$", "", base)
        return f"{base}/page/{page_num}/"

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

    def _parse_price(self, text: str) -> Optional[float]:
        if not text:
            return None
        # Format: "21,000 DT" = 21.000 TND, "399,900 DT" = 399.900 TND
        cleaned = re.sub(r"[^\d,.]", "", text).strip()
        if not cleaned:
            return None
        # "21,000" → 21.000 TND (thousands sep with 3 digits)
        m = re.match(r"^(\d+),(\d{3})$", cleaned)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        # "21,90" → 21.90 TND (decimal comma, 1-2 digits)
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

        for a in tree.css("a[href*='/fr/c/']"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            # Skip pagination URLs
            if "/page/" in href:
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

        # WooCommerce product list items
        for item in tree.css("li.product, .wd-product"):
            product = {}

            pid = item.attributes.get("data-id")
            if pid:
                product["id"] = str(pid)

            # URL from product image link or title link
            link = item.css_first("a.product-image-link, a.woocommerce-LoopProduct-link")
            if not link:
                link = item.css_first("h2 a, h3 a, .wd-entities-title a")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            # Name
            name_el = item.css_first(".wd-entities-title, .woocommerce-loop-product__title, h2, h3")
            if name_el:
                product["name"] = self._clean(name_el.text())

            # Image
            img = item.css_first("img")
            if img:
                src = img.attributes.get("src") or img.attributes.get("data-src") or ""
                if src and not src.startswith("data:"):
                    # Upgrade thumbnail to full size
                    src = re.sub(r"-\d+x\d+(\.\w+)$", r"\1", src)
                    product["image"] = self._abs(src)

            # Price — WooCommerce ins/del pattern
            price_block = item.css_first("span.price, .price")
            if price_block:
                ins_el = price_block.css_first("ins .woocommerce-Price-amount, ins")
                del_el = price_block.css_first("del .woocommerce-Price-amount, del")
                plain_el = price_block.css_first(".woocommerce-Price-amount")
                if ins_el:
                    product["price"] = self._parse_price(ins_el.text())
                elif plain_el:
                    product["price"] = self._parse_price(plain_el.text())
                if del_el:
                    product["old_price"] = self._parse_price(del_el.text())
                if product.get("price") and product.get("old_price") and product["old_price"] > 0:
                    product["discount_percent"] = round(
                        (1 - product["price"] / product["old_price"]) * 100
                    )

            product["shop"] = "infotec"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css("a.page-numbers, .woocommerce-pagination a, nav.pagination a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a.next.page-numbers, .woocommerce-pagination a.next") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        next_el = tree.css_first(
            "a.next.page-numbers, .woocommerce-pagination a.next, "
            f"a[href*='/page/{current_page + 1}/']"
        )
        return next_el is not None

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)

        name_el = tree.css_first("h1.product_title, h1.entry-title, h1")
        if name_el:
            details["name"] = self._clean(name_el.text())

        ins_el = tree.css_first("p.price ins .woocommerce-Price-amount")
        del_el = tree.css_first("p.price del .woocommerce-Price-amount")
        plain_el = tree.css_first("p.price .woocommerce-Price-amount")
        if ins_el:
            details["price"] = self._parse_price(ins_el.text())
        elif plain_el:
            details["price"] = self._parse_price(plain_el.text())
        if del_el:
            details["old_price"] = self._parse_price(del_el.text())
        if details.get("price") and details.get("old_price") and details["old_price"] > 0:
            details["discount_percent"] = round(
                (1 - details["price"] / details["old_price"]) * 100
            )

        # WooCommerce REF: span.sku_wrapper span.sku → e.g. "IPC-S7XP-6M1TED-EU"
        sku_el = tree.css_first("span.sku_wrapper span.sku, span.sku, [itemprop='sku'], .product_meta .sku")
        if sku_el:
            details["sku"] = self._clean(sku_el.text())

        desc_el = tree.css_first(".woocommerce-product-details__short-description, [itemprop='description']")
        if desc_el:
            details["description"] = self._clean(desc_el.text())

        main_img = tree.css_first("img.wp-post-image, .woocommerce-product-gallery img")
        if main_img:
            src = main_img.attributes.get("src") or main_img.attributes.get("data-large_image") or ""
            if src and not src.startswith("data:"):
                src = re.sub(r"-\d+x\d+(\.\w+)$", r"\1", src)
                details["image"] = self._abs(src)

        images = []
        for img in tree.css(".woocommerce-product-gallery img"):
            src = img.attributes.get("src") or img.attributes.get("data-large_image") or ""
            if src and not src.startswith("data:"):
                src = re.sub(r"-\d+x\d+(\.\w+)$", r"\1", src)
                abs_src = self._abs(src)
                if abs_src and abs_src not in images:
                    images.append(abs_src)
        if images:
            details["images"] = images

        avail_el = tree.css_first(".stock.in-stock, .stock.out-of-stock, p.stock")
        if avail_el:
            details["availability"] = self._clean(avail_el.text())

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
        self.logger.info("Starting infotec scrape")
        await self._ensure_browser()

        html = await self.fetch_html(self.base_url)
        if not html:
            self.logger.error("Failed to fetch homepage")
            return {"products": [], "categories": []}

        cat_data = self.extract_categories_from_html(html)
        categories = cat_data.get("categories", [])
        self.logger.info(f"Found {len(categories)} categories")

        # Deduplicate and build leaf list
        leaf_cats = []
        seen_urls = set()
        for cat in categories:
            url = cat.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                leaf_cats.append({
                    "url": url,
                    "top_category": cat["name"],
                    "low_category": "",
                    "subcategory": "",
                })

        all_products = []
        seen_ids = set()
        for cat in leaf_cats:
            prods = await self.scrape_category(cat)
            for p in prods:
                uid = p.get("id") or p.get("url")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    all_products.append(p)

        self.logger.info(f"Total unique products: {len(all_products)}")
        return {"products": all_products, "categories": categories}


def get_scraper(logger: logging.Logger) -> InfotecScraper:
    return InfotecScraper(logger)
