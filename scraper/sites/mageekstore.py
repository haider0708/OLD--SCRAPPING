#!/usr/bin/env python3
"""
Mageekstore.tn scraper — Wix platform, fully JS-rendered, no Cloudflare, full Playwright.
Uses networkidle wait and data-hook attributes common in Wix Stores.
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

# Wix Stores API endpoint pattern (used if HTML parsing fails)
WIX_STORE_API = "/_api/wix-ecommerce-storefront-web/api"


class MageekstoreScraper(FastScraper):
    """Full-Playwright scraper for mageekstore.tn (Wix Stores)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("mageekstore", logger)
        self._pw = None
        self._browser = None
        self._pw_context = None
        self._tor_slot = hash("mageekstore") % max(TorPool.get().size, 1)
        self._page_sem = asyncio.Semaphore(5)  # cap concurrent Playwright pages

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
        html = ""
        try:
            await page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(8000)
            html = await page.content()
        except Exception as e:
            self.logger.warning(f"Frontpage fetch failed: {e}")
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
                await page.wait_for_timeout(8000)
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
        """Wix pagination: ?page=N or appended query."""
        base = re.sub(r"[?&]page=\d+", "", base_url)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

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

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract categories from Wix navigation."""
        tree = HTMLParser(html)
        categories = []
        seen = set()

        # Wix renders nav items with data-testid or aria attributes
        nav_links = tree.css(
            "nav a[href], "
            "[data-testid='siteHeader'] a[href], "
            "header a[href*='/acheter'], "
            "header a[href*='/categorie'], "
            "a[href*='/acheter-'], "
            "a[data-hook='menu-item-link']"
        )

        for link in nav_links:
            href = link.attributes.get("href", "")
            if not href or href == "#" or "javascript" in href:
                continue
            abs_url = self._make_absolute(href)
            if not abs_url:
                continue

            name = self._clean_text(link.text(strip=True))
            if not name or name in seen:
                continue
            # Skip non-category links (external, tel:, etc.)
            if not (abs_url.startswith(self.base_url) or abs_url.startswith("/")):
                continue

            seen.add(name)
            categories.append({
                "name": name,
                "url": abs_url,
                "level": "top",
                "low_level_categories": [],
            })

        # Fallback: look for product-page links as category hints
        if not categories:
            for a in tree.css("a[href*='/product-page/'], a[href*='/produit/']"):
                href = self._make_absolute(a.attributes.get("href", ""))
                name = self._clean_text(a.text(strip=True))
                if name and href and name not in seen:
                    seen.add(name)
                    # Derive category from URL pattern
                    cat_url = "/".join(href.split("/")[:-1]) + "/"
                    categories.append({
                        "name": name,
                        "url": cat_url,
                        "level": "top",
                        "low_level_categories": [],
                    })

        self.logger.info(f"Found {len(categories)} categories")
        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0,
                 "total_urls": len(categories)}
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from Wix Stores listing page."""
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        # Wix Stores uses data-hook attributes for product items
        items = tree.css(
            "[data-hook='product-item'], "
            "[data-hook='product-list-grid-item'], "
            "li[data-hook*='product'], "
            "[data-testid='product-item']"
        )

        for item in items:
            link_el = item.css_first(
                "a[data-hook='product-item-container'], "
                "a[href*='/product-page/'], "
                "a[href*='/produit/'], "
                "a[href]"
            )
            product_url = self._make_absolute(link_el.attributes.get("href", "")) if link_el else None
            if not product_url or product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            product_id = item.attributes.get("data-product-id", "")

            name_el = item.css_first(
                "[data-hook='product-item-name'], "
                "[data-testid='product-title'], "
                "h3, h2"
            )
            product_name = self._clean_text(name_el.text(strip=True)) if name_el else ""

            product_data = {
                "id": product_id or None,
                "url": product_url,
                "name": product_name,
            }

            # Image
            img_el = item.css_first(
                "[data-hook='product-item-image'] img, "
                "img[src*='wixstatic'], img"
            )
            if img_el:
                src = img_el.attributes.get("src") or img_el.attributes.get("data-src")
                if src and not src.startswith("data:"):
                    product_data["image"] = src

            # Price
            price_el = item.css_first(
                "[data-hook='product-item-price-to-pay'], "
                "[data-hook='price-range-from'], "
                "[data-testid='price']"
            )
            if price_el:
                product_data["price"] = self._parse_price(price_el.text())

            old_price_el = item.css_first("[data-hook='product-item-price-before-discount']")
            if old_price_el:
                product_data["old_price"] = self._parse_price(old_price_el.text())
                if product_data.get("old_price") and product_data.get("price"):
                    product_data["discount_percent"] = round(
                        (1 - product_data["price"] / product_data["old_price"]) * 100
                    )

            products.append(product_data)

        # Fallback: try JSON-LD structured data
        if not products:
            for script in tree.css('script[type="application/ld+json"]'):
                raw = (script.text() or "").strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                blocks = data if isinstance(data, list) else [data]
                for block in blocks:
                    if not isinstance(block, dict) or block.get("@type") != "Product":
                        continue
                    url = self._make_absolute(block.get("url", ""))
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    offers = block.get("offers") or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    products.append({
                        "id": str(block.get("sku") or block.get("productID") or ""),
                        "url": url,
                        "name": self._clean_text(block.get("name", "")),
                        "price": self._parse_price(str(offers.get("price", ""))),
                        "image": (block.get("image") or [None])[0] if isinstance(block.get("image"), list) else block.get("image"),
                    })

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from Wix page."""
        tree = HTMLParser(html)

        next_link = tree.css_first(
            "[data-hook='load-more-button'], "
            "button[aria-label='Next'], "
            "a[aria-label='Next page']"
        )
        has_next = next_link is not None

        return {
            "current_page": 1,
            "total_pages": 999 if has_next else 1,
            "has_next": has_next,
        }

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape detailed product info from a Wix product page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Try JSON-LD first (most reliable on Wix)
        for script in tree.css('script[type="application/ld+json"]'):
            raw = (script.text() or "").strip()
            if not raw:
                continue
            try:
                ld = json.loads(raw)
            except Exception:
                continue
            blocks = ld if isinstance(ld, list) else [ld]
            for block in blocks:
                if not isinstance(block, dict) or block.get("@type") != "Product":
                    continue
                offers = block.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}

                data["title"] = block.get("name")
                data["sku"] = block.get("sku") or block.get("productID")
                data["description"] = re.sub(r"<[^>]+>", " ", block.get("description") or "").strip() or None
                data["brand"] = (block.get("brand") or {}).get("name") if isinstance(block.get("brand"), dict) else block.get("brand")
                data["price"] = self._parse_price(str(offers.get("price", "")))

                avail_url = offers.get("availability", "")
                if "InStock" in avail_url:
                    data["availability"] = "En stock"
                    data["available"] = True
                elif "OutOfStock" in avail_url:
                    data["availability"] = "Rupture de stock"
                    data["available"] = False
                else:
                    data["availability"] = None
                    data["available"] = None

                imgs = block.get("image") or []
                if isinstance(imgs, str):
                    imgs = [imgs]
                data["images"] = imgs[:10] if imgs else None
                data["specifications"] = {}
                return data

        # Fallback: HTML parsing
        title_el = tree.css_first(
            "[data-hook='product-title'], "
            "[data-testid='product-title'], "
            "h1"
        )
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        sku_el = tree.css_first("[data-hook='sku'], [data-testid='sku']")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        price_el = tree.css_first(
            "[data-hook='formatted-primary-price'], "
            "[data-hook='product-price'], "
            "[data-testid='price']"
        )
        data["price"] = self._parse_price(price_el.text()) if price_el else None

        old_price_el = tree.css_first("[data-hook='product-price-before-discount']")
        if old_price_el:
            data["old_price"] = self._parse_price(old_price_el.text())
            if data.get("old_price") and data.get("price"):
                data["discount_percent"] = round(
                    (1 - data["price"] / data["old_price"]) * 100
                )

        avail_el = tree.css_first("[data-hook='inventory-wrapper'], [data-testid='inventory']")
        if avail_el:
            avail_text = self._clean_text(avail_el.text(strip=True))
            data["availability"] = avail_text
            lower = avail_text.lower()
            data["available"] = (
                "en stock" in lower or "disponible" in lower or "in stock" in lower
            ) and "rupture" not in lower

        desc_el = tree.css_first("[data-hook='description'], [data-testid='product-description']")
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        images = []
        for img in tree.css("[data-hook='product-images-layout'] img, img[src*='wixstatic']"):
            src = img.attributes.get("src")
            if src and not src.startswith("data:") and src not in images:
                images.append(src)

        data["images"] = images[:10] if images else None
        data["specifications"] = {}

        return data


def get_scraper(logger: logging.Logger) -> MageekstoreScraper:
    return MageekstoreScraper(logger)
