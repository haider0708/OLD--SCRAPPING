#!/usr/bin/env python3
"""
Koktahome.com scraper — WooCommerce, Cloudflare JS challenge, full Playwright.
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


class KoktahomeScraper(FastScraper):
    """Full-Playwright scraper for koktahome.com (WooCommerce + Cloudflare)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("koktahome", logger)
        self._pw = None
        self._browser = None
        self._pw_context = None
        self._tor_slot = hash("koktahome") % max(TorPool.get().size, 1)
        self._page_sem = asyncio.Semaphore(5)  # max 5 concurrent Playwright pages

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
                    "#menu-menu-principal li > a, ul.navbar-nav li > a, nav ul li > a",
                    timeout=10000,
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
                await page.wait_for_timeout(2000)
                try:
                    await page.wait_for_selector(
                        "div.wd-product, div.product-grid-item",
                        timeout=10000,
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
        """WooCommerce pagination: /page/N/ appended to category URL."""
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
            # Tunisian format: 1.299,000 → thousands sep is ".", decimal is ","
            # After stripping non-digits/punct we get e.g. "1.299,000" or "659,000"
            # Remove "." thousand separators, replace "," decimal with "."
            cleaned = cleaned.replace(".", "").replace(",", ".")
            value = float(cleaned) if cleaned else None
            # If result looks like it was divided by 1000 (e.g. 0.659), multiply back
            if value is not None and value < 10:
                value = round(value * 1000, 3)
            return value
        elif "," in cleaned:
            # Could be "659,000" (Tunisian thousands) or "6,99" (decimal)
            parts = cleaned.split(",")
            if len(parts) == 2 and len(parts[1]) == 3:
                # Thousands separator: "659,000" → 659.0
                cleaned = parts[0] + parts[1]
                cleaned = cleaned.lstrip("0") or "0"
            else:
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
        """Extract categories from WooCommerce megamenu using produit-categorie links."""
        tree = HTMLParser(html)
        categories = []
        seen_urls = set()

        # koktahome uses a JS megamenu — categories are in href*=produit-categorie links.
        # Collect all such links, deduplicate by URL, and build a 2-level hierarchy:
        # top-level = depth-1 paths (/produit-categorie/slug/)
        # subcategory = depth-2+ paths (/produit-categorie/slug/sub/)
        top_by_slug = {}  # slug -> top_cat dict

        for a in tree.css("a[href*='produit-categorie'], a[href*='product-category']"):
            href = a.attributes.get("href", "")
            if not href:
                continue
            abs_url = self._make_absolute(href).rstrip("/") + "/"  # normalize trailing slash
            if abs_url in seen_urls:
                continue
            seen_urls.add(abs_url)

            name = self._clean_text(a.text(strip=True))
            if not name:
                continue

            # Determine depth by counting path segments after the keyword
            path = abs_url.split("produit-categorie/")[-1].split("product-category/")[-1]
            segments = [s for s in path.strip("/").split("/") if s]
            depth = len(segments)

            if depth == 1:
                slug = segments[0]
                top_cat = {
                    "name": name,
                    "url": abs_url,
                    "level": "top",
                    "low_level_categories": [],
                }
                top_by_slug[slug] = top_cat
                categories.append(top_cat)
            elif depth >= 2:
                parent_slug = segments[0]
                if parent_slug not in top_by_slug:
                    # Create a placeholder top if we haven't seen it yet
                    parent_url = self._make_absolute(f"/produit-categorie/{parent_slug}/")
                    top_by_slug[parent_slug] = {
                        "name": parent_slug.replace("-", " ").title(),
                        "url": parent_url,
                        "level": "top",
                        "low_level_categories": [],
                    }
                    categories.append(top_by_slug[parent_slug])

                top_cat = top_by_slug[parent_slug]
                # Find or create low_cat for depth-2
                mid_slug = segments[1] if depth > 2 else segments[-1]
                mid_url = self._make_absolute("/produit-categorie/" + "/".join(segments[:2]) + "/")
                low_cat = next(
                    (lc for lc in top_cat["low_level_categories"] if lc["url"] == mid_url),
                    None,
                )
                if low_cat is None:
                    low_cat = {
                        "name": name if depth == 2 else mid_slug.replace("-", " ").title(),
                        "url": abs_url if depth == 2 else mid_url,
                        "level": "low",
                        "subcategories": [],
                    }
                    top_cat["low_level_categories"].append(low_cat)

                if depth > 2:
                    low_cat["subcategories"].append(
                        {"name": name, "url": abs_url, "level": "subcategory"}
                    )

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
        """Extract products from WooCommerce category listing."""
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        # koktahome uses div.wd-product (WoodMart theme), not li.product
        items = tree.css("div.wd-product, div.product-grid-item.type-product")

        for item in items:
            # Product URL — WoodMart puts it on .wd-entities-title-link or the image anchor
            link_el = item.css_first(
                "a.wd-entities-title-link, a.product-link, "
                "a[href*='/produits/'], a[href*='/produit/'], a"
            )
            if not link_el or link_el.tag != "a":
                continue

            product_url = self._make_absolute(link_el.attributes.get("href", ""))
            if not product_url or product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            # Product ID from post-NNNNN class on the div
            product_id = None
            classes = item.attributes.get("class", "")
            id_match = re.search(r"post-(\d+)", classes)
            if id_match:
                product_id = id_match.group(1)

            # Name — WoodMart uses .wd-entities-title
            name_el = item.css_first(
                ".wd-entities-title, h3.wd-entities-title, "
                "h2.woocommerce-loop-product__title, h2, h3"
            )
            product_name = self._clean_text(name_el.text(strip=True)) if name_el else ""

            product_data = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }

            # Image
            img_el = item.css_first(
                "img.attachment-woocommerce_thumbnail, img.wp-post-image, "
                "div.product-image img, img"
            )
            if img_el:
                src = (
                    img_el.attributes.get("src")
                    or img_el.attributes.get("data-src")
                    or img_el.attributes.get("data-lazy-src")
                )
                if src and self._is_valid_image(src):
                    product_data["image"] = self._make_absolute(src)

            # Price — WooCommerce: <span class="woocommerce-Price-amount"><bdi>
            price_el = item.css_first(
                "span.price ins span.woocommerce-Price-amount bdi, "
                "span.price span.woocommerce-Price-amount bdi, "
                "span.woocommerce-Price-amount bdi"
            )
            if price_el:
                product_data["price"] = self._parse_price(price_el.text())

            old_price_el = item.css_first(
                "del span.woocommerce-Price-amount bdi"
            )
            if old_price_el:
                product_data["old_price"] = self._parse_price(old_price_el.text())
                if product_data.get("old_price") and product_data.get("price"):
                    product_data["discount_percent"] = round(
                        (1 - product_data["price"] / product_data["old_price"]) * 100
                    )

            # Stock
            oos = item.css_first(".stock.out-of-stock, button.disabled[disabled]")
            if oos:
                product_data["availability"] = "Rupture de stock"
                product_data["available"] = False

            products.append(product_data)

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract WooCommerce pagination."""
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
        """Scrape detailed product info from a WooCommerce product page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Product ID
        id_match = re.search(r"[?&]p=(\d+)", url)
        if not id_match:
            for el in tree.css("button.single_add_to_cart_button, form.cart"):
                val = el.attributes.get("data-product_id") or el.attributes.get("value")
                if val and val.isdigit():
                    data["product_id"] = val
                    break
        else:
            data["product_id"] = id_match.group(1)

        # Title
        title_el = tree.css_first("h1.product_title, h1.entry-title, h1[itemprop='name']")
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        # SKU
        sku_el = tree.css_first("span.sku")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        # Price
        price_el = tree.css_first(
            "p.price ins span.woocommerce-Price-amount bdi, "
            "p.price span.woocommerce-Price-amount bdi, "
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
        stock_el = tree.css_first("p.stock.in-stock, p.stock.out-of-stock, p.availability span")
        if stock_el:
            classes = stock_el.attributes.get("class", "")
            data["availability"] = self._clean_text(stock_el.text(strip=True))
            data["available"] = "in-stock" in classes and "out-of-stock" not in classes
        else:
            cart_btn = tree.css_first("button.single_add_to_cart_button")
            if cart_btn:
                data["availability"] = "En stock"
                data["available"] = True
            else:
                data["availability"] = None
                data["available"] = None

        # Brand
        brand_el = tree.css_first(
            ".woocommerce-product-attributes td[data-title='Marque'], "
            ".product_meta .brand a, "
            ".woocommerce-product-attributes-item--attribute_pa_marque td"
        )
        data["brand"] = self._clean_text(brand_el.text(strip=True)) if brand_el else None

        # Description
        desc_el = tree.css_first(
            "div.woocommerce-product-details__short-description, "
            "div#tab-description .entry-content"
        )
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        # Specifications from attributes table
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
            "div.flex-viewport img, "
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


def get_scraper(logger: logging.Logger) -> KoktahomeScraper:
    return KoktahomeScraper(logger)
