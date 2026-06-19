#!/usr/bin/env python3
"""
Carthagoinformatique.tn scraper — WooCommerce + XTS Theme, Cloudflare JS challenge, full Playwright.
"""
import asyncio
import json
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


class CarthagoinformatiqueScraper(FastScraper):
    """Full-Playwright scraper for carthagoinformatique.tn (WooCommerce + XTS + Cloudflare)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("carthagoinformatique", logger)
        self._pw = None
        self._browser = None
        self._pw_context = None
        self._tor_slot = hash("carthagoinformatique") % max(TorPool.get().size, 1)
        self._page_sem = asyncio.Semaphore(3)  # max 3 concurrent Playwright pages

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True, args=playwright_launch_args()
        )
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
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"📥 Downloading (Playwright): {self.base_url}")

        await self._ensure_browser()
        page = await self._pw_context.new_page()
        try:
            await page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
            try:
                await page.wait_for_selector(
                    ".xts-nav-menu > ul > li > a, nav ul.xts-nav > li > a, "
                    "#menu-menu-1 > li > a, .main-nav > ul > li > a",
                    timeout=12000,
                )
            except Exception:
                self.logger.warning("Nav menu not found, continuing")
            html = await page.content()
        finally:
            await page.close()

        save_text_atomic(html, output_path, self.logger)
        self.logger.info(f"✓ Saved: {output_path} ({len(html):,} bytes)")
        return output_path

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
        async with self._page_sem:  # cap concurrent pages to avoid browser crash
            page = await self._pw_context.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                status_code = resp.status if resp else None
                final_url = page.url
                await page.wait_for_timeout(3000)
                try:
                    await page.wait_for_selector(
                        "li.product.type-product, .xts-product.type-product, "
                        "ul.products li.product",
                        timeout=8000,
                    )
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

    def build_page_url(self, base_url: str, page_num: int) -> str:
        """WooCommerce pagination: /page/N/"""
        base = base_url.rstrip("/")
        base = re.sub(r"/page/\d+$", "", base)
        return f"{base}/page/{page_num}/"

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def _make_absolute(self, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return f"{self.base_url}{url}"
        return f"{self.base_url}/{url}"

    def _parse_price(self, text: str) -> Optional[float]:
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", text).strip()
        cleaned = re.sub(r"\s+", "", cleaned)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    def _is_valid_image(self, url: str) -> bool:
        if not url or url.startswith("data:"):
            return False
        return url.startswith("http") or url.startswith("//")

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract categories from WooCommerce/XTS nav menu."""
        tree = HTMLParser(html)
        categories = []
        seen = set()

        top_items = tree.css(
            ".xts-nav-menu > ul > li.menu-item, "
            "nav ul.xts-nav > li.menu-item, "
            "#menu-menu-1 > li.menu-item, "
            ".main-nav > ul > li.menu-item"
        )

        for top_li in top_items:
            top_link = top_li.css_first("a")
            if not top_link:
                continue

            top_name = self._clean_text(top_link.text(strip=True))
            top_url = self._make_absolute(top_link.attributes.get("href", ""))

            if not top_name or top_name in seen:
                continue
            if not top_url or "javascript" in top_url:
                continue
            seen.add(top_name)

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            sub_items = top_li.css("ul.sub-menu li.menu-item a")
            if sub_items:
                low_cat = {
                    "name": top_name,
                    "url": top_url,
                    "level": "low",
                    "subcategories": [],
                }
                for sub_link in sub_items:
                    sub_name = self._clean_text(sub_link.text(strip=True))
                    sub_url = self._make_absolute(sub_link.attributes.get("href", ""))
                    if sub_name and sub_url:
                        low_cat["subcategories"].append(
                            {"name": sub_name, "url": sub_url, "level": "subcategory"}
                        )
                if low_cat["subcategories"]:
                    top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        # Fallback
        if not categories:
            for a in tree.css("a[href*='categorie-produit'], a[href*='product-category']"):
                href = self._make_absolute(a.attributes.get("href", ""))
                name = self._clean_text(a.text(strip=True))
                if name and href and name not in seen:
                    seen.add(name)
                    categories.append({
                        "name": name, "url": href, "level": "top",
                        "low_level_categories": [],
                    })

        self.logger.info(f"Found {len(categories)} top-level categories")
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["top_level"] += 1
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top.get("low_level_categories", []):
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
                for sub in low.get("subcategories", []):
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from WooCommerce/XTS category listing."""
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        # XTS theme uses li.product or div.xts-product
        items = tree.css(
            "li.product.type-product, .xts-product.type-product, "
            "ul.products li.product, .product-grid-item"
        )

        for item in items:
            link_el = item.css_first(
                "a.woocommerce-loop-product__link, a.xts-product-image-link, "
                "a.product-link, h2 a, h3 a"
            )
            if not link_el or link_el.tag != "a":
                link_el = item.css_first("a")

            product_url = self._make_absolute(link_el.attributes.get("href", "")) if link_el else None
            if not product_url or product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            product_id = item.attributes.get("data-id") or item.attributes.get("data-product_id")

            name_el = item.css_first(
                "h2.woocommerce-loop-product__title, h3.product-title, "
                ".woocommerce-loop-product__title, .product-title, h2, h3"
            )
            product_name = self._clean_text(name_el.text(strip=True)) if name_el else ""

            product_data = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }

            # Image — XTS often uses .xts-img or lazy-loading
            img_el = item.css_first(
                "img.xts-img, img.attachment-woocommerce_thumbnail, "
                "img.wp-post-image, img"
            )
            if img_el:
                src = (
                    img_el.attributes.get("data-src")
                    or img_el.attributes.get("src")
                    or img_el.attributes.get("data-lazy-src")
                )
                if src and self._is_valid_image(src):
                    product_data["image"] = self._make_absolute(src)

            # Price
            price_el = item.css_first(
                "span.price ins span.woocommerce-Price-amount bdi, "
                "span.price span.woocommerce-Price-amount bdi, "
                ".xts-product-price span.woocommerce-Price-amount bdi, "
                "span.woocommerce-Price-amount bdi"
            )
            if price_el:
                product_data["price"] = self._parse_price(price_el.text())

            old_price_el = item.css_first("del span.woocommerce-Price-amount bdi")
            if old_price_el:
                product_data["old_price"] = self._parse_price(old_price_el.text())
                if product_data.get("old_price") and product_data.get("price"):
                    product_data["discount_percent"] = round(
                        (1 - product_data["price"] / product_data["old_price"]) * 100
                    )

            oos = item.css_first(".stock.out-of-stock")
            if oos:
                product_data["availability"] = "Rupture de stock"
                product_data["available"] = False

            products.append(product_data)

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        current_page = 1

        next_link = tree.css_first("a.next.page-numbers")
        has_next = next_link is not None

        for a in tree.css("ul.page-numbers a.page-numbers, nav.woocommerce-pagination a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass

        current_el = tree.css_first("span.page-numbers.current")
        if current_el:
            try:
                current_page = int(current_el.text(strip=True))
            except ValueError:
                pass

        return {
            "current_page": current_page,
            "total_pages": max_page,
            "has_next": has_next,
        }

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Title
        title_el = tree.css_first("h1.product_title, h1.entry-title, h1[itemprop='name']")
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        # WooCommerce UGS: span.sku_wrapper span.sku → e.g. product reference code
        sku_el = tree.css_first("span.sku_wrapper span.sku, span.sku, [itemprop='sku']")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        # Price
        price_el = tree.css_first(
            "p.price ins span.woocommerce-Price-amount bdi, "
            "p.price span.woocommerce-Price-amount bdi, "
            ".xts-product-price span.woocommerce-Price-amount bdi, "
            "span.woocommerce-Price-amount bdi"
        )
        data["price"] = self._parse_price(price_el.text()) if price_el else None

        old_price_el = tree.css_first("p.price del span.woocommerce-Price-amount bdi")
        if old_price_el:
            data["old_price"] = self._parse_price(old_price_el.text())
            if data.get("old_price") and data.get("price"):
                data["discount_percent"] = round(
                    (1 - data["price"] / data["old_price"]) * 100
                )

        # Availability
        stock_el = tree.css_first("p.stock.in-stock, p.stock.out-of-stock")
        if stock_el:
            classes = stock_el.attributes.get("class", "")
            data["availability"] = self._clean_text(stock_el.text(strip=True))
            data["available"] = "in-stock" in classes
        else:
            cart_btn = tree.css_first("button.single_add_to_cart_button")
            data["availability"] = "En stock" if cart_btn else None
            data["available"] = bool(cart_btn) if cart_btn else None

        # Brand
        brand_el = tree.css_first(
            ".woocommerce-product-attributes td[data-title='Marque'], "
            ".woocommerce-product-attributes-item--attribute_pa_marque td, "
            ".product_meta .brand a"
        )
        data["brand"] = self._clean_text(brand_el.text(strip=True)) if brand_el else None

        # Description
        desc_el = tree.css_first(
            "div.woocommerce-product-details__short-description, "
            "div#tab-description"
        )
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        # Specifications
        specs = {}
        for row in tree.css("table.woocommerce-product-attributes tr"):
            key_el = row.css_first("th.woocommerce-product-attributes-item__label")
            val_el = row.css_first("td.woocommerce-product-attributes-item__value")
            if key_el and val_el:
                k = self._clean_text(key_el.text(strip=True))
                v = self._clean_text(val_el.text(strip=True))
                if k and v:
                    specs[k] = v
        data["specifications"] = specs

        # Images
        images = []
        for img in tree.css(
            "div.woocommerce-product-gallery__image img, "
            ".xts-product-gallery img, "
            "img.wp-post-image"
        ):
            src = (
                img.attributes.get("data-large_image")
                or img.attributes.get("data-src")
                or img.attributes.get("src")
            )
            if src and self._is_valid_image(src) and src not in images:
                images.append(self._make_absolute(src))

        data["images"] = images[:10] if images else None

        return data


def get_scraper(logger: logging.Logger) -> CarthagoinformatiqueScraper:
    return CarthagoinformatiqueScraper(logger)
