#!/usr/bin/env python3
"""
BestBuy Tunisie (bestbuytunisie.tn) scraper.
Full Playwright: WordPress + WooCommerce + Elementor (JS-rendered).
"""
import logging
import re
import time
from typing import List, Optional

from selectolax.parser import HTMLParser

from scraper.base import (
    FastScraper,
    TorPool,
    detect_blocked_signals,
    is_blocked_response,
    playwright_launch_args,
    proxy_url_to_playwright,
    save_text_atomic,
)

STEALTH_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
STEALTH_JS = 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'


class BestBuyTunisieScraper(FastScraper):
    """Full-Playwright scraper for bestbuytunisie.tn (WooCommerce + Elementor)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("bestbuytunisie", logger)
        self._pw = None
        self._browser = None
        self._pw_context = None
        self._tor_slot = hash("bestbuytunisie") % max(TorPool.get().size, 1)

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True, args=playwright_launch_args())
        pool = TorPool.get()
        self._pw_context = await self._browser.new_context(
            user_agent=STEALTH_UA,
            proxy=pool.pw_proxy(self._tor_slot) or proxy_url_to_playwright(self.proxy_url),
        )
        await self._pw_context.add_init_script(STEALTH_JS)

    async def _close_browser(self):
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def download_frontpage(self):
        """Fresh browser context + JS-challenge wait to bypass Cloudflare."""
        import asyncio
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"📥 Downloading (Playwright fresh ctx): {self.base_url}")
        await self._ensure_browser()
        html = ""
        resp = None
        for attempt in range(1, 4):
            ctx = await self._browser.new_context(user_agent=STEALTH_UA, locale="fr-FR")
            await ctx.add_init_script(STEALTH_JS)
            page = await ctx.new_page()
            try:
                resp = await page.goto(self.base_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(8000)
                try:
                    await page.wait_for_selector(
                        ".xts-sub-menu > li > a, ul#menu-main-menu > li > a",
                        timeout=10000,
                    )
                except Exception:
                    pass
                html = await page.content()
            finally:
                await page.close()
                await ctx.close()
            is_cf = "Just a moment" in html or "cf-chl" in html
            if resp and resp.status == 200 and not is_cf and len(html) > 50000:
                save_text_atomic(html, output_path, self.logger)
                self.logger.info(f"✓ Saved: {output_path} ({len(html):,} bytes)")
                return output_path
            self.logger.warning(f"Attempt {attempt}: status={resp.status if resp else None}, len={len(html)}, cf={is_cf}")
            await asyncio.sleep(2 + attempt * 2)
        save_text_atomic(html, output_path, self.logger)
        return output_path

    async def fetch_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        meta = await self.fetch_html_with_meta(url, raise_on_error=raise_on_error)
        return meta.get("html")

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> dict:
        started = time.monotonic()
        await self._ensure_browser()
        page = await self._pw_context.new_page()
        status_code = None
        final_url = url
        html = None
        error = None
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status_code = resp.status if resp else None
            final_url = page.url
            try:
                await page.wait_for_selector("div.xts-product.type-product, li.product.type-product", timeout=8000)
            except Exception:
                pass
            html = await page.content()
            if status_code and status_code >= 400:
                error = f"HTTP {status_code}"
            elif not html or not html.strip():
                error = "empty_response"
            elif is_blocked_response(html, status_code):
                error = "blocked_response"
        except Exception as e:
            error = str(e) or e.__class__.__name__
            if raise_on_error:
                raise
        finally:
            await page.close()
        blocked_signals = detect_blocked_signals(html, status_code)
        if raise_on_error and error:
            raise RuntimeError(error)
        return {
            "html": None if error else html,
            "status_code": status_code,
            "final_url": final_url,
            "content_type": None,
            "content_encoding": None,
            "attempts": 1,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "blocked_signals": blocked_signals,
            "error": error,
        }

    async def run_full_scrape(self, category_limit=None, product_limit=None, detail_limit=None, on_result=None):
        try:
            return await super().run_full_scrape(
                category_limit=category_limit,
                product_limit=product_limit,
                detail_limit=detail_limit,
                on_result=on_result,
            )
        finally:
            await self._close_browser()

    def _parse_price(self, text: str) -> Optional[float]:
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", text).strip()
        cleaned = re.sub(r"\s+", "", cleaned)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _clean_text(self, text: str) -> Optional[str]:
        if not text:
            return None
        return " ".join(text.split()).strip() or None

    def _make_absolute_url(self, url: str) -> str:
        if not url:
            return url
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return f"{self.base_url}{url}"
        return f"{self.base_url}/{url}"

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        # Try XTS theme menu first
        top_blocks = tree.css(".xts-sub-menu > li, ul#menu-main-menu > li")
        if not top_blocks:
            top_blocks = tree.css("ul.menu > li.menu-item-has-children, ul.menu > li.menu-item")

        self.logger.info(f"Found {len(top_blocks)} top-level blocks")

        for top_li in top_blocks:
            top_link = top_li.css_first("a")
            if not top_link:
                continue
            top_name = self._clean_text(top_link.text(strip=True))
            top_url = self._make_absolute_url(top_link.attributes.get("href", ""))
            if not top_name or top_url in seen:
                continue
            seen.add(top_url)

            top_cat = {"name": top_name, "url": top_url, "level": "top", "low_level_categories": []}

            for low_link in top_li.css("ul.sub-sub-menu > li > a, ul.sub-menu > li > a"):
                low_name = self._clean_text(low_link.text(strip=True))
                low_url = self._make_absolute_url(low_link.attributes.get("href", ""))
                if low_name and low_url not in seen:
                    seen.add(low_url)
                    top_cat["low_level_categories"].append({
                        "name": low_name, "url": low_url, "level": "low", "subcategories": [],
                    })

            categories.append(top_cat)

        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["low_level"] += len(top["low_level_categories"])
            stats["total_urls"] += 1 + len(top["low_level_categories"])
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        items = tree.css("div.xts-product.type-product, li.product.type-product, div.product-grid-item")
        for item in items:
            link = item.css_first("a.xts-product-link, a.woocommerce-LoopProduct-link")
            url = self._make_absolute_url(link.attributes.get("href", "")) if link else None
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            name_el = item.css_first("h3.product-title, h3.woocommerce-loop-product__title, h2.woocommerce-loop-product__title, .xts-product-title")
            name = self._clean_text(name_el.text(strip=True)) if name_el else None
            if not name and link:
                name = self._clean_text(link.attributes.get("aria-label", ""))

            img_el = item.css_first("img.attachment-woocommerce_thumbnail, img.wp-post-image, img")
            image = None
            if img_el:
                image = self._make_absolute_url(
                    img_el.attributes.get("data-src") or img_el.attributes.get("src")
                )

            ins_el = item.css_first("span.price ins span.woocommerce-Price-amount")
            plain_el = item.css_first("span.price span.woocommerce-Price-amount")
            price_el = ins_el or plain_el
            price = self._parse_price(price_el.text() if price_el else None)

            del_el = item.css_first("span.price del span.woocommerce-Price-amount")
            old_price = self._parse_price(del_el.text() if del_el else None)

            product = {"url": url, "name": name, "price": price, "image": image}
            if old_price:
                product["old_price"] = old_price
            products.append(product)

        return products

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"/page/\d+/?$", "", base_url.rstrip("/"))
        return f"{base}/page/{page_num}/"

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css("ul.page-numbers li a.page-numbers:not(.next):not(.prev)"):
            try:
                n = int(a.text(strip=True))
                if n > max_page:
                    max_page = n
            except ValueError:
                pass
        has_next = tree.css_first("a.next.page-numbers") is not None
        return {"max_page": max_page, "current_page": 1, "has_next_page": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "fetch_failed"}
        tree = HTMLParser(html)
        data = {"url": url}

        title_el = tree.css_first("h1.product_title.entry-title, h1.product_title")
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        sku_el = tree.css_first("span.sku")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        brand_el = tree.css_first("div.product_meta span.posted_in a")
        data["brand"] = self._clean_text(brand_el.text(strip=True)) if brand_el else None

        ins_el = tree.css_first("p.price ins span.woocommerce-Price-amount bdi")
        plain_el = tree.css_first("p.price span.woocommerce-Price-amount bdi")
        price_el = ins_el or plain_el
        data["price"] = self._parse_price(price_el.text() if price_el else None)

        del_el = tree.css_first("p.price del span.woocommerce-Price-amount bdi")
        data["old_price"] = self._parse_price(del_el.text() if del_el else None)

        stock_el = tree.css_first("p.stock")
        data["availability"] = self._clean_text(stock_el.text(strip=True)) if stock_el else None
        data["available"] = stock_el is not None and "in-stock" in (stock_el.attributes.get("class") or "")

        desc_el = tree.css_first("div.woocommerce-product-details__short-description")
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        images = []
        main_img = tree.css_first("div.woocommerce-product-gallery__image img")
        if main_img:
            src = main_img.attributes.get("data-large_image") or main_img.attributes.get("src")
            if src:
                images.append(self._make_absolute_url(src))
        for thumb in tree.css("ol.flex-control-thumbs li img"):
            src = thumb.attributes.get("src")
            if src and src not in images:
                images.append(self._make_absolute_url(src))
        data["images"] = images

        return data


def get_scraper(logger: logging.Logger) -> BestBuyTunisieScraper:
    return BestBuyTunisieScraper(logger)
