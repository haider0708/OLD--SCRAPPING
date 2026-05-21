#!/usr/bin/env python3
"""
Skymill Informatique (skymil-informatique.com) specific scraper implementation.
Hybrid: Playwright for all page fetches (site is behind Cloudflare),
selectolax for HTML parsing.
"""

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


class SkymillScraper(FastScraper):
    """Hybrid scraper for skymil-informatique.com (behind Cloudflare, requires Playwright for all fetches)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("skymill", logger)
        self._pw = None
        self._browser = None
        self._pw_context = None
        self._tor_slot = hash("skymill") % max(TorPool.get().size, 1)

    # ------------------------------------------------------------------
    # Shared Playwright browser (lazy init, reused across all fetches)
    # ------------------------------------------------------------------

    async def _ensure_browser(self):
        """Lazily start a shared Playwright browser with anti-detection.

        Uses --headless=new (Chrome's new headless mode) which is less
        detectable by Cloudflare than the old headless implementation.
        """
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=playwright_launch_args(),
        )
        pool = TorPool.get()
        self._pw_context = await self._browser.new_context(
            user_agent=STEALTH_UA,
            proxy=pool.pw_proxy(self._tor_slot)
            or proxy_url_to_playwright(self.proxy_url),
        )
        await self._pw_context.add_init_script(STEALTH_JS)

    async def _close_browser(self):
        """Close the shared browser."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    # ------------------------------------------------------------------
    # Override: Playwright-based frontpage download
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        """Download frontpage using shared Playwright browser."""
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"📥 Downloading (Playwright): {self.base_url}")

        fp = self.selectors.get("frontpage", {})
        wait_sel = fp.get(
            "wait_selector", "div#spverticalmenu_1 ul.level-1 > li.item-1 > a"
        )

        await self._ensure_browser()
        page = await self._pw_context.new_page()
        try:
            await page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_selector(wait_sel, timeout=15000)
            except Exception:
                self.logger.warning(f"Wait selector '{wait_sel}' not found, continuing")
            html = await page.content()
        finally:
            await page.close()

        save_text_atomic(html, output_path, self.logger)
        self.logger.info(f"✓ Saved: {output_path} ({len(html):,} bytes)")
        return output_path

    # ------------------------------------------------------------------
    # Override: Playwright-based fetch_html (replaces httpx)
    # ------------------------------------------------------------------

    async def fetch_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        meta = await self.fetch_html_with_meta(url, raise_on_error=raise_on_error)
        return meta.get("html")

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> dict:
        """Fetch HTML via shared Playwright browser with probe metadata."""
        started = time.monotonic()
        await self._ensure_browser()
        page = await self._pw_context.new_page()
        status_code = None
        final_url = url
        html = None
        error = None
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status_code = resp.status if resp else None
            final_url = page.url
            html = await page.content()
            if status_code and status_code >= 400:
                error = f"HTTP {status_code}"
                self.logger.debug(f"  {error} for {url}")
            elif not html or not html.strip():
                error = "empty_response"
            elif is_blocked_response(html, status_code):
                error = "blocked_response"
        except Exception as e:
            error = str(e) or e.__class__.__name__
            self.logger.debug(f"  Error fetching {url}: {e}")
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def _make_absolute_url(self, url: str) -> Optional[str]:
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
        """Extract numeric price from text like '1 299,000 DT'."""
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", text).strip()
        # Handle thousand separators: "1 299,000" → "1299.000"
        cleaned = cleaned.replace(" ", "")
        if "," in cleaned and "." in cleaned:
            # e.g. "1.299,00" → "1299.00"
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Category extraction (from Playwright-rendered HTML)
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract 3-level category hierarchy from SP Mega Menu."""
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories = []

        top_blocks = tree.css(fp.get("top_level_blocks", "ul.level-1 > li.item-1"))
        self.logger.info(f"Found {len(top_blocks)} top-level category blocks")

        for top_block in top_blocks:
            top_link = top_block.css_first(fp.get("top_level_link", "a"))
            if not top_link:
                continue

            top_name = self._clean_text(top_link.text(strip=True))
            top_url = top_link.attributes.get("href", "")
            top_url = self._make_absolute_url(top_url)

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            # Low-level: ul.level-2 > li.item-2 > a
            low_links = top_block.css(
                fp.get("low_level_items", "ul.level-2 > li.item-2 > a")
            )
            for low_link in low_links:
                low_name = self._clean_text(low_link.text(strip=True))
                low_url = low_link.attributes.get("href", "")
                low_url = self._make_absolute_url(low_url)

                low_cat = {
                    "name": low_name,
                    "url": low_url,
                    "level": "low",
                    "subcategories": [],
                }

                # Subcategories: from the parent li of this low_link
                low_li = low_link.parent
                if low_li:
                    sub_links = low_li.css("ul.level-3 > li.item-3 > a")
                    for sub_link in sub_links:
                        sub_name = self._clean_text(sub_link.text(strip=True))
                        sub_url = sub_link.attributes.get("href", "")
                        sub_url = self._make_absolute_url(sub_url)

                        low_cat["subcategories"].append(
                            {
                                "name": sub_name,
                                "url": sub_url,
                                "level": "subcategory",
                            }
                        )

                top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

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

        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )

        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Product listing extraction (httpx HTML)
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from PrestaShop category page."""
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products = []

        items = tree.css(
            cp.get("item_selector", "article.product-miniature.js-product-miniature")
        )

        for item in items:
            product_id = item.attributes.get(cp.get("item_id_attr", "data-id-product"))

            # Name & URL
            link = item.css_first(cp.get("item_name", "h2.h3.product-title a"))
            if not link:
                link = item.css_first("h3.product-title a")
            if not link:
                continue

            product_url = self._make_absolute_url(link.attributes.get("href", ""))
            product_name = self._clean_text(link.text(strip=True))

            if not product_id or not product_url:
                continue

            product_data = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }

            # Price — prefer aria-label='Prix' span, then meta itemprop
            price_el = item.css_first(
                cp.get("item_price", "span.price[aria-label='Prix']")
            )
            if price_el:
                content = price_el.attributes.get("content")
                if content:
                    try:
                        product_data["price"] = float(content)
                    except ValueError:
                        product_data["price"] = self._parse_price(price_el.text())
                else:
                    product_data["price"] = self._parse_price(price_el.text())
            else:
                meta_price = item.css_first(
                    cp.get("item_price_meta", "meta[itemprop='price']")
                )
                if meta_price:
                    try:
                        product_data["price"] = float(
                            meta_price.attributes.get("content", "")
                        )
                    except ValueError:
                        product_data["price"] = None
                else:
                    product_data["price"] = None

            # Old price
            old_el = item.css_first(cp.get("item_old_price", "span.regular-price"))
            if old_el:
                product_data["old_price"] = self._parse_price(old_el.text())

            # Brand
            brand_el = item.css_first("span.product-manufacturer a.brand")
            if brand_el:
                product_data["brand"] = self._clean_text(brand_el.text(strip=True))

            # Image
            img_el = item.css_first(cp.get("item_image", "img.product-thumbnail-first"))
            if not img_el:
                img_el = item.css_first("a.thumbnail.product-thumbnail img")
            if img_el:
                for attr in cp.get("item_image_attrs", ["src", "data-src"]):
                    src = img_el.attributes.get(attr)
                    if src and not src.startswith("data:"):
                        product_data["image"] = self._make_absolute_url(src)
                        break

            # Out of stock flag
            oos_el = item.css_first("ul.product-flags > li.product-flag.out_of_stock")
            if oos_el:
                product_data["in_stock"] = False

            products.append(product_data)

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from PrestaShop category page."""
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})

        current_page = 1
        total_pages = 1
        has_next = False

        page_links = tree.css("ul.page-list li a.js-search-link")
        for el in page_links:
            classes = el.attributes.get("class", "")
            try:
                num = int(el.text(strip=True))
                if num > total_pages:
                    total_pages = num
                if "disabled" in classes or "current" in classes:
                    current_page = num
            except (ValueError, TypeError):
                continue

        next_link = tree.css_first(
            cp.get("pagination_next", "a.next.js-search-link[rel='next']")
        )
        if next_link and "disabled" not in next_link.attributes.get("class", ""):
            has_next = True

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    # ------------------------------------------------------------------
    # Product detail scraping (httpx)
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape product details — JSON-primary from data-product, fallback HTML."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data = {"url": url}

        # --- JSON-primary: div.tab-pane#product-details[data-product] ---
        json_el = tree.css_first("div.tab-pane#product-details[data-product]")
        if json_el:
            raw = json_el.attributes.get("data-product", "")
            try:
                pj = json.loads(raw)
                data["product_id"] = str(pj.get("id", "") or pj.get("id_product", ""))
                data["title"] = pj.get("name")
                data["reference"] = pj.get("reference")
                data["sku"] = pj.get("sku") or pj.get("reference")
                # Price from JSON
                if "price_amount" in pj:
                    data["price"] = pj["price_amount"]
                elif "price" in pj:
                    data["price"] = self._parse_price(str(pj["price"]))
                # Old price
                if pj.get("regular_price_amount"):
                    data["old_price"] = pj["regular_price_amount"]
                elif pj.get("regular_price"):
                    data["old_price"] = self._parse_price(str(pj["regular_price"]))
                # Description
                data["description_short"] = pj.get("description_short")
                data["description"] = pj.get("description")
                # Availability
                data["availability"] = pj.get("availability_message")
                data["quantity"] = pj.get("quantity")
                # Category
                data["category_name"] = pj.get("category_name")
                # Images from JSON
                if pj.get("images"):
                    data["images"] = [
                        img.get("large", {}).get("url")
                        or img.get("bySize", {}).get("large_default", {}).get("url")
                        for img in pj["images"]
                        if img.get("large", {}).get("url")
                        or img.get("bySize", {}).get("large_default", {}).get("url")
                    ]
                # Features / specs
                if pj.get("features"):
                    data["specs"] = {
                        f.get("name", ""): f.get("value", "")
                        for f in pj["features"]
                        if f.get("name")
                    }
                # Brand from JSON
                if pj.get("manufacturer_name"):
                    data["brand"] = pj["manufacturer_name"]

                return data
            except (json.JSONDecodeError, TypeError):
                self.logger.debug(
                    f"JSON parse failed for data-product on {url}, falling back to HTML"
                )

        # --- Fallback: HTML parsing ---
        # Product ID from URL
        url_match = re.search(r"[\-/](\d+)[\-.]", url)
        data["product_id"] = url_match.group(1) if url_match else None

        # Title
        title_el = tree.css_first(pp.get("title", "h1.h1[itemprop='name']"))
        if not title_el:
            title_el = tree.css_first("h1.product-name[itemprop='name']")
        data["title"] = (
            self._clean_text(title_el.text(strip=True)) if title_el else None
        )

        # Price — prefer content attr
        price_el = tree.css_first(
            pp.get("price", "div.current-price span[itemprop='price']")
        )
        if not price_el:
            price_el = tree.css_first(".product-price span[itemprop='price']")
        if price_el:
            content = price_el.attributes.get("content")
            if content:
                try:
                    data["price"] = float(content)
                except ValueError:
                    data["price"] = self._parse_price(price_el.text())
            else:
                data["price"] = self._parse_price(price_el.text())
        else:
            data["price"] = None

        # Old price
        old_el = tree.css_first(pp.get("old_price", "span.regular-price"))
        if old_el:
            data["old_price"] = self._parse_price(old_el.text())

        # Brand — from manufacturer img alt
        brand_el = tree.css_first(pp.get("brand", "div.product-manufacturer img"))
        if brand_el:
            data["brand"] = brand_el.attributes.get("alt", "").strip() or None
        else:
            brand_link = tree.css_first("div.product-manufacturer a")
            if brand_link:
                data["brand"] = self._clean_text(brand_link.text(strip=True))

        # SKU
        sku_el = tree.css_first(
            pp.get("sku", ".product-reference span[itemprop='sku']")
        )
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        # Availability
        avail_el = tree.css_first(pp.get("availability", "span#product-availability"))
        if avail_el:
            data["availability"] = self._clean_text(avail_el.text(strip=True))
        else:
            avail_schema = tree.css_first(
                pp.get("availability_schema", "link[itemprop='availability'][href]")
            )
            if avail_schema:
                data["availability"] = avail_schema.attributes.get("href", "")

        # Description
        desc_el = tree.css_first(
            pp.get("description", "div.product-description[itemprop='description']")
        )
        if not desc_el:
            desc_el = tree.css_first(".product-short-description")
        data["description"] = (
            self._clean_text(desc_el.text(strip=True)) if desc_el else None
        )

        # Specs — dl.data-sheet or feature table
        specs = {}
        specs_container = tree.css_first(
            pp.get("specs_container", "section.product-features dl.data-sheet")
        )
        if specs_container:
            keys = specs_container.css(pp.get("specs_key", "dt.name"))
            vals = specs_container.css(pp.get("specs_value", "dd.value"))
            for k, v in zip(keys, vals):
                k_text = self._clean_text(k.text(strip=True))
                v_text = self._clean_text(v.text(strip=True))
                if k_text:
                    specs[k_text] = v_text
        if specs:
            data["specs"] = specs

        # Images
        images = []
        main_img = tree.css_first(
            pp.get("image_main", "div.product-cover img.js-qv-product-cover")
        )
        if main_img:
            src = main_img.attributes.get(
                "data-image-large-src"
            ) or main_img.attributes.get("src")
            if src:
                images.append(self._make_absolute_url(src))

        for thumb in tree.css(
            pp.get(
                "image_thumbnails", "ul.product-images.js-qv-product-images img.thumb"
            )
        ):
            src = thumb.attributes.get(
                pp.get("image_thumb_attr", "data-image-large-src")
            ) or thumb.attributes.get("src")
            if src:
                abs_src = self._make_absolute_url(src)
                if abs_src not in images:
                    images.append(abs_src)

        data["images"] = images if images else None

        return data


def get_scraper(logger: logging.Logger) -> SkymillScraper:
    return SkymillScraper(logger)
