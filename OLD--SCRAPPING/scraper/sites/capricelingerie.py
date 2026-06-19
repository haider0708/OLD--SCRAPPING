#!/usr/bin/env python3
"""
Capricelingerie.com.tn scraper — WooCommerce, Playwright (lazy-loaded images).
Categories: /categorie-produit/{slug}/
Products:   /article/{slug}/
Pagination: ?page=N (standard WooCommerce)
"""
import asyncio
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
STEALTH_JS = 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'


class CapriceLingerieScraper(FastScraper):
    """Playwright scraper for capricelingerie.com.tn (WooCommerce)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("capricelingerie", logger)
        self._pw = None
        self._browser = None
        self._ctx = None
        self._fetch_sem = asyncio.Semaphore(3)
        # Cap concurrent detail fetches — site rate-limits aggressively
        self._page_sem = asyncio.Semaphore(4)

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

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = base_url.rstrip("/")
        return f"{base}/page/{page_num}/"

    def _abs(self, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return f"https://capricelingerie.com.tn{url}"
        return url

    def _clean(self, text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _parse_price(self, text: str) -> Optional[float]:
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", text).strip()
        if not cleaned:
            return None
        cleaned = cleaned.replace(",", ".")
        # Remove duplicate dots
        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = parts[0] + "." + "".join(parts[1:])
        try:
            return float(cleaned)
        except ValueError:
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen_urls = set()

        # Capricelingerie nav: plain <ul><li><a> with /categorie/ URLs
        # Top-level li contain the first <a> (category link) + optional nested <ul>
        for top_li in tree.css("li"):
            # Find the first <a> that links to /categorie/
            top_a = None
            for child in top_li.iter():
                if child.tag == "a":
                    href = child.attributes.get("href", "")
                    if "/categorie/" in href:
                        top_a = child
                    break  # only check first <a>, not nested ones

            if not top_a:
                continue
            top_href = self._abs(top_a.attributes.get("href", ""))
            top_name = self._clean(top_a.text())
            if not top_name or not top_href or top_href in seen_urls:
                continue
            # Skip if it looks like a sub-url (has 3+ path segments → it's a subcategory)
            path_parts = [p for p in top_href.replace(self.base_url, "").split("/") if p]
            is_sub = len(path_parts) >= 3  # e.g. /categorie/soutien-gorge/bandeau/

            seen_urls.add(top_href)

            if is_sub:
                # Will be picked up as subcategory below
                continue

            top_cat = {
                "name": top_name,
                "url": top_href,
                "level": "top",
                "low_level_categories": []
            }

            # Sub-items: nested <li><a href="/categorie/parent/sub/"> inside this li
            for sub_a in top_li.css("ul li a"):
                sub_href = self._abs(sub_a.attributes.get("href", ""))
                sub_name = self._clean(sub_a.text())
                if not sub_name or not sub_href or sub_href in seen_urls:
                    continue
                if "/categorie/" not in sub_href:
                    continue
                seen_urls.add(sub_href)
                top_cat["low_level_categories"].append({
                    "name": sub_name,
                    "url": sub_href,
                    "level": "low",
                    "subcategories": []
                })

            categories.append(top_cat)

        return {"categories": categories}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        # WooCommerce product grid: li.product or article.product
        items = tree.css("li.product, article.product, .product-item")
        for item in items:
            product = {}

            # URL + ID
            link = item.css_first("a.woocommerce-LoopProduct-link, a[href*='/article/'], a[href*='/produit/'], h2 a, h3 a, .product-name a, a")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            # WooCommerce product ID from data attribute
            pid = item.attributes.get("data-product_id") or item.attributes.get("data-id")
            if pid:
                product["id"] = str(pid)

            # Name
            name_el = item.css_first(
                "h2.woocommerce-loop-product__title, h3.woocommerce-loop-product__title, "
                ".product-title, h2, h3, .product-name"
            )
            if name_el:
                product["name"] = self._clean(name_el.text())

            # Image
            img = item.css_first("img")
            if img:
                src = (img.attributes.get("src") or img.attributes.get("data-src")
                       or img.attributes.get("data-lazy-src") or "")
                if src and not src.startswith("data:"):
                    product["image"] = self._abs(src)

            # Price — WooCommerce: ins (sale) / del (old) or plain .price
            price_block = item.css_first("span.price")
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

            product["shop"] = "capricelingerie"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)

        # Name
        name_el = tree.css_first("h1.product_title, h1.entry-title, h1")
        if name_el:
            details["name"] = self._clean(name_el.text())

        # Product ID from WooCommerce form
        form = tree.css_first("form.variations_form, form[data-product_id]")
        if form and form.attributes.get("data-product_id"):
            details["id"] = form.attributes["data-product_id"]

        # Price
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

        # WooCommerce attributes table: td label "Ref :" → sibling td value e.g. "G21/LOUVRE"
        sku_el = tree.css_first("span.sku_wrapper span.sku, span.sku, [itemprop='sku']")
        if sku_el:
            details["sku"] = self._clean(sku_el.text())
        else:
            # Scan WooCommerce attribute table rows for Ref/Référence label
            for row in tree.css("table.woocommerce-product-attributes tr, .shop_attributes tr"):
                label_el = row.css_first("th, td.woocommerce-product-details__label")
                value_el = row.css_first("td:last-child, td.woocommerce-product-details__value")
                if label_el and value_el:
                    lbl = label_el.text(strip=True)
                    if re.search(r"Réf|Ref|SKU|UGS|Référence|Code", lbl, re.IGNORECASE):
                        details["sku"] = self._clean(value_el.text())
                        break
            # Fallback: scan any element for "Réf : VALUE" pattern
            if not details.get("sku"):
                for el in tree.css("p, span, li, td"):
                    txt = el.text(strip=True)
                    if re.match(r"^(Réf|Ref|SKU|UGS|Référence)\s*[:\-]", txt, re.IGNORECASE):
                        m = re.search(r"[:\-]\s*(\S+)", txt)
                        if m:
                            details["sku"] = m.group(1)
                            break

        # Main image — wp-post-image or first gallery thumb
        main_img = tree.css_first("img.wp-post-image, .woocommerce-product-gallery__image img, img[class*='attachment-woocommerce']")
        if main_img:
            src = (main_img.attributes.get("src") or
                   main_img.attributes.get("data-large_image") or
                   main_img.attributes.get("data-src") or "")
            if src and not src.startswith("data:"):
                details["image"] = self._abs(src)
            # Upgrade thumbnail URL to full size
            if details.get("image"):
                details["image"] = re.sub(r"-\d+x\d+(\.\w+)$", r"\1", details["image"])

        # Categories from breadcrumb
        breadcrumb = tree.css(".woocommerce-breadcrumb a")
        cats = [self._clean(a.text()) for a in breadcrumb if self._clean(a.text()) and self._clean(a.text()) != "Accueil"]
        if cats:
            details["top_category"] = cats[0]
        if len(cats) >= 2:
            details["subcategory"] = cats[-1]

        # Availability from variations JSON embedded in page
        avail_el = tree.css_first(".stock.in-stock, .stock.out-of-stock, p.stock")
        if avail_el:
            details["availability"] = self._clean(avail_el.text())

        return details

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css(".woocommerce-pagination a.page-numbers, a.page-numbers"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a.next.page-numbers, .woocommerce-pagination a.next") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        # Product pages don't need Playwright — use httpx directly.
        # _page_sem caps concurrency so we don't trigger the site's rate limit.
        async with self._page_sem:
            await asyncio.sleep(0.3)
            html = await FastScraper.fetch_html(self, url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        next_el = tree.css_first(
            "a.next.page-numbers, .woocommerce-pagination a.next, "
            f"a[href*='/page/{current_page + 1}/']"
        )
        return next_el is not None

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
            if detect_blocked_signals(html):
                self.logger.warning(f"Blocked on {page_url}")
                break
            products = self.extract_products_from_html(html, category)
            if not products:
                break
            all_products.extend(products)
            self.logger.info(f"  Page {page}: {len(products)} products ({url})")
            if not self.has_next_page(html, page):
                break
            page += 1
            await asyncio.sleep(1)
        return all_products

    async def scrape(self) -> dict:
        self.logger.info(f"Starting capricelingerie scrape")
        await self._ensure_browser()

        # Fetch homepage for categories
        html = await self.fetch_html(self.base_url)
        if not html:
            self.logger.error("Failed to fetch homepage")
            return {"products": [], "categories": []}

        cat_data = self.extract_categories_from_html(html)
        categories = cat_data.get("categories", [])
        self.logger.info(f"Found {len(categories)} top categories")

        # Flatten all leaf category URLs
        leaf_cats = []
        for top in categories:
            subs = top.get("low_level_categories", [])
            if subs:
                for low in subs:
                    for sub in low.get("subcategories", []):
                        leaf_cats.append({
                            "url": sub["url"],
                            "top_category": top["name"],
                            "low_category": low["name"],
                            "subcategory": sub["name"],
                        })
                    if not low.get("subcategories"):
                        leaf_cats.append({
                            "url": low["url"],
                            "top_category": top["name"],
                            "low_category": low["name"],
                            "subcategory": "",
                        })
            else:
                leaf_cats.append({
                    "url": top["url"],
                    "top_category": top["name"],
                    "low_category": "",
                    "subcategory": "",
                })

        # Scrape each category
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


def get_scraper(logger: logging.Logger) -> CapriceLingerieScraper:
    return CapriceLingerieScraper(logger)
