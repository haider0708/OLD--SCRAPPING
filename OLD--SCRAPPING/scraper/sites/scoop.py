#!/usr/bin/env python3
"""
Scoop Gaming (scoopgaming.com.tn) specific scraper implementation.
Full Playwright: site uses TvCMS mega-menu (JS-rendered) and Cloudflare protection.
Platform: PrestaShop + TvCMS MegaMenu.
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


class ScoopScraper(FastScraper):
    """Full-Playwright scraper for scoopgaming.com.tn (PrestaShop + TvCMS MegaMenu)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("scoop", logger)
        self._pw = None
        self._browser = None
        self._pw_context = None
        self._tor_slot = hash("scoop") % max(TorPool.get().size, 1)

    # ------------------------------------------------------------------
    # Shared Playwright browser (lazy init, reused across all fetches)
    # ------------------------------------------------------------------

    async def _ensure_browser(self):
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
        """Download frontpage using Playwright (TvCMS menu requires JS)."""
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"📥 Downloading (Playwright): {self.base_url}")

        fp = self.selectors.get("frontpage", {})
        wait_sel = fp.get("wait_selector", "div#tvdesktop-megamenu ul.menu-content > li.level-1 > a")

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
    # Override: Playwright-based fetch_html (replaces httpx for all pages)
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
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status_code = resp.status if resp else None
            final_url = page.url
            # Wait for product cards to appear on category pages
            try:
                await page.wait_for_selector(
                    "article.product-miniature, article[data-id-product]",
                    timeout=8000,
                )
            except Exception:
                pass
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
    # Override: close browser when scraping finishes
    # ------------------------------------------------------------------

    async def run_full_scrape(
        self, category_limit=None, product_limit=None, detail_limit=None, on_result=None
    ):
        try:
            return await super().run_full_scrape(
                category_limit=category_limit,
                product_limit=product_limit,
                detail_limit=detail_limit,
                on_result=on_result,
            )
        finally:
            await self._close_browser()

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
        """Extract numeric price from text like '299,000 TND' or '1 234,500 DT'."""
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,\s]", "", text).strip()
        cleaned = re.sub(r"\s+", "", cleaned)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Category extraction — TvCMS dual-menu
    # ------------------------------------------------------------------

    def _extract_menu_items(self, container) -> List[dict]:
        """Extract categories from a TvCMS menu-content container."""
        categories = []

        top_items = container.css("li.level-1")
        for top_li in top_items:
            # Skip title-only items
            if "tvmega-menu-title" in (top_li.attributes.get("class") or ""):
                continue

            top_link = top_li.css_first("a")
            if not top_link:
                continue

            top_name = self._clean_text(top_link.text(strip=True))
            top_url = self._make_absolute_url(top_link.attributes.get("href", ""))

            if not top_name:
                continue

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            # Low-level: li.level-2 inside the dropdown
            low_items = top_li.css("ul.menu-dropdown > li.level-2")
            for low_li in low_items:
                low_link = low_li.css_first("a")
                if not low_link:
                    continue

                low_name = self._clean_text(low_link.text(strip=True))
                low_url = self._make_absolute_url(low_link.attributes.get("href", ""))

                if not low_name:
                    continue

                low_cat = {
                    "name": low_name,
                    "url": low_url,
                    "level": "low",
                    "subcategories": [],
                }

                # Subcategories: li.level-3
                sub_items = low_li.css("ul.menu-dropdown > li.level-3, ul > li.level-3")
                for sub_li in sub_items:
                    sub_link = sub_li.css_first("a")
                    if not sub_link:
                        continue
                    sub_name = self._clean_text(sub_link.text(strip=True))
                    sub_url = self._make_absolute_url(sub_link.attributes.get("href", ""))
                    if sub_name:
                        low_cat["subcategories"].append(
                            {"name": sub_name, "url": sub_url, "level": "subcategory"}
                        )

                top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        return categories

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract category hierarchy from TvCMS menu-content."""
        tree = HTMLParser(html)
        all_categories = []
        seen_names = set()

        # Primary: div#tvdesktop-megamenu ul.menu-content
        container = tree.css_first("div#tvdesktop-megamenu ul.menu-content")
        if container:
            cats = self._extract_menu_items(container)
            for cat in cats:
                if cat["name"] not in seen_names:
                    seen_names.add(cat["name"])
                    all_categories.append(cat)

        # Fallback: any ul.menu-content
        if not all_categories:
            for ul in tree.css("ul.menu-content"):
                cats = self._extract_menu_items(ul)
                for cat in cats:
                    if cat["name"] not in seen_names:
                        seen_names.add(cat["name"])
                        all_categories.append(cat)

        self.logger.info(f"Found {len(all_categories)} top-level categories")

        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in all_categories:
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

        return {"categories": all_categories, "stats": stats}

    # ------------------------------------------------------------------
    # Product listing extraction
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        """PrestaShop pagination: ?page={n}."""
        base = re.sub(r"[?&]page=\d+", "", base_url)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from a PrestaShop/TvCMS category listing page."""
        tree = HTMLParser(html)
        products = []
        seen_ids = set()

        items = tree.css("article.product-miniature[data-id-product]")
        if not items:
            items = tree.css("article.product-miniature")

        for item in items:
            product_id = item.attributes.get("data-id-product")

            if product_id and product_id in seen_ids:
                continue
            if product_id:
                seen_ids.add(product_id)

            # Name — h6 with itemprop or inside .tvproduct-name
            name_el = item.css_first(
                "h6[itemprop='name'], "
                "div.tvproduct-name.product-title a h6, "
                "div.tvproduct-name a, "
                "h2.product-title a, "
                "h3.product-title a"
            )
            product_name = self._clean_text(name_el.text(strip=True)) if name_el else ""

            # URL
            link_el = item.css_first(
                "div.tvproduct-name.product-title a, "
                "a.thumbnail.product-thumbnail, "
                "h2.product-title a, "
                "h3.product-title a"
            )
            product_url = (
                self._make_absolute_url(link_el.attributes.get("href", ""))
                if link_el
                else None
            )

            if not product_id and not product_url:
                continue

            product_data = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }

            # Image
            img_el = item.css_first(
                "img.tvproduct-defult-img, "
                "img.tv-img-responsive, "
                "a.thumbnail.product-thumbnail img, "
                "img.img-responsive"
            )
            if img_el:
                src = (
                    img_el.attributes.get("src")
                    or img_el.attributes.get("data-src")
                    or img_el.attributes.get("data-lazy-src")
                )
                if src and not src.startswith("data:"):
                    product_data["image"] = self._make_absolute_url(src)

            # Price
            price_el = item.css_first(
                ".product-price-and-shipping span.price, "
                "div.tv-product-price span.price, "
                "span.price"
            )
            product_data["price"] = self._parse_price(
                price_el.text() if price_el else None
            )

            # Old price
            old_price_el = item.css_first(
                ".product-price-and-shipping span.regular-price, "
                "span.regular-price"
            )
            if old_price_el:
                product_data["old_price"] = self._parse_price(old_price_el.text())
                if product_data.get("old_price") and product_data.get("price"):
                    product_data["discount_percent"] = round(
                        (1 - product_data["price"] / product_data["old_price"]) * 100
                    )

            # Out of stock flag
            oos_flag = item.css_first(
                "ul.product-flags > li.product-flag.out_of_stock, "
                "li.out_of_stock"
            )
            if oos_flag:
                product_data["availability"] = "Rupture de stock"
                product_data["available"] = False

            products.append(product_data)

        if products:
            return products

        # JSON-LD fallback
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
                url = self._make_absolute_url(block.get("url", ""))
                if not url:
                    continue
                products.append({
                    "id": str(block.get("sku") or block.get("productID") or ""),
                    "url": url,
                    "name": self._clean_text(block.get("name", "")),
                    "price": self._parse_price(
                        str((block.get("offers") or {}).get("price", ""))
                    ),
                })
        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from PrestaShop category page."""
        tree = HTMLParser(html)
        max_page = 1
        current_page = 1

        next_link = tree.css_first("a.next.js-search-link[rel='next'], a[rel='next'].js-search-link")
        has_next = next_link is not None

        for page_link in tree.css("nav.pagination a.js-search-link, ul.page-list a.js-search-link"):
            href = page_link.attributes.get("href", "")
            page_match = re.search(r"[?&]page=(\d+)", href)
            if page_match:
                try:
                    num = int(page_match.group(1))
                    if num > max_page:
                        max_page = num
                except ValueError:
                    pass
            try:
                num = int(page_link.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass

        current_el = tree.css_first(
            "nav.pagination li.current a, "
            "nav.pagination li.active a, "
            "ul.page-list li.current a"
        )
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

    # ------------------------------------------------------------------
    # Product detail scraping
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape detailed product info from a PrestaShop product page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Try JSON-primary approach: data-product attribute
        json_el = tree.css_first("div.tab-pane#product-details[data-product]")
        product_json = None
        if json_el:
            raw = json_el.attributes.get("data-product", "")
            if raw:
                try:
                    product_json = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    pass

        if product_json:
            data["product_id"] = str(product_json.get("id_product", "")) or None
            data["title"] = product_json.get("name")
            data["sku"] = product_json.get("reference")

            price_val = product_json.get("price_amount")
            if price_val is not None:
                try:
                    data["price"] = float(price_val)
                except (ValueError, TypeError):
                    data["price"] = None
            else:
                data["price"] = self._parse_price(product_json.get("price"))

            data["availability"] = product_json.get("availability_message")
            avail = product_json.get("availability")
            if avail == "available":
                data["available"] = True
            elif avail in ("unavailable", "last_remaining_items"):
                data["available"] = avail == "last_remaining_items"
            else:
                data["available"] = (
                    product_json.get("quantity", 0) > 0
                    if isinstance(product_json.get("quantity"), (int, float))
                    else None
                )
        else:
            # Product ID from URL (PrestaShop: /123-slug.html)
            url_match = re.search(r"/(\d+)-", url)
            data["product_id"] = url_match.group(1) if url_match else None

            # Title
            title_el = tree.css_first("h1.h1[itemprop='name'], h1.h1, h1[itemprop='name']")
            data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

            # SKU
            sku_el = tree.css_first("div.product-reference span[itemprop='sku'], span[itemprop='sku']")
            data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

            # Price
            price_el = tree.css_first("div.current-price span[itemprop='price'], span[itemprop='price']")
            if price_el:
                price_content = price_el.attributes.get("content")
                if price_content:
                    try:
                        data["price"] = float(price_content)
                    except ValueError:
                        data["price"] = self._parse_price(price_el.text())
                else:
                    data["price"] = self._parse_price(price_el.text())
            else:
                price_el = tree.css_first("span.price, div.current-price .price")
                data["price"] = self._parse_price(price_el.text() if price_el else None)

            # Availability
            avail_el = tree.css_first("span#product-availability, #product-availability")
            if avail_el:
                avail_text = avail_el.text(strip=True)
                data["availability"] = avail_text
                lower = avail_text.lower()
                data["available"] = (
                    ("en stock" in lower or "disponible" in lower or "in stock" in lower)
                    and "rupture" not in lower
                    and "indisponible" not in lower
                )
            else:
                avail_link = tree.css_first("link[itemprop='availability'][href]")
                if avail_link:
                    href = avail_link.attributes.get("href", "")
                    if "InStock" in href:
                        data["availability"] = "En stock"
                        data["available"] = True
                    elif "OutOfStock" in href:
                        data["availability"] = "Rupture de stock"
                        data["available"] = False
                    else:
                        data["availability"] = None
                        data["available"] = None
                else:
                    data["availability"] = None
                    data["available"] = None

        # Old price
        old_price_el = tree.css_first("span.regular-price, div.product-discount span.regular-price")
        if old_price_el:
            data["old_price"] = self._parse_price(old_price_el.text())
            if data.get("old_price") and data.get("price"):
                data["discount_percent"] = round(
                    (1 - data["price"] / data["old_price"]) * 100
                )
        else:
            data["old_price"] = None

        # Brand
        brand_img = tree.css_first("div.product-manufacturer img, a.tvproduct-brand img")
        if brand_img:
            data["brand"] = brand_img.attributes.get("alt")
            data["brand_logo"] = self._make_absolute_url(brand_img.attributes.get("src"))
        else:
            brand_el = tree.css_first("div.product-manufacturer a, a.tvproduct-brand")
            data["brand"] = self._clean_text(brand_el.text(strip=True)) if brand_el else None

        # Description
        desc_el = tree.css_first(
            "div.product-description, "
            "div[id^='product-description-short-'], "
            "div.tab-pane#description div.product-description"
        )
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        # Specifications
        specs = {}
        features = tree.css_first(".product-features, #product-details section")
        if features:
            for dt in features.css("dt"):
                dd = dt.next
                while dd and dd.tag != "dd":
                    dd = dd.next
                if dd:
                    k = self._clean_text(dt.text(strip=True))
                    v = self._clean_text(dd.text(strip=True))
                    if k and v:
                        specs[k] = v
        data["specifications"] = specs

        # Images
        images = []
        main_img = tree.css_first(
            "div.product-cover img.js-qv-product-cover, "
            "div.product-cover img, "
            "div.tvproduct-image-slider img[itemprop='image']"
        )
        if main_img:
            src = (
                main_img.attributes.get("data-image-large-src")
                or main_img.attributes.get("data-src")
                or main_img.attributes.get("src")
            )
            if src:
                images.append(self._make_absolute_url(src))

        for img in tree.css(
            "ul.product-images img.thumb, "
            "img.thumb.js-thumb, "
            ".thumb-container img"
        ):
            src = (
                img.attributes.get("data-image-large-src")
                or img.attributes.get("data-src")
                or img.attributes.get("src")
            )
            if src:
                abs_src = self._make_absolute_url(src)
                if abs_src not in images:
                    images.append(abs_src)

        data["images"] = images[:10] if images else None

        return data


def get_scraper(logger: logging.Logger) -> ScoopScraper:
    return ScoopScraper(logger)
