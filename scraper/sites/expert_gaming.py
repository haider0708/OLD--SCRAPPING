#!/usr/bin/env python3
"""
Expert-Gaming.tn specific scraper implementation.
Full Playwright: site is behind Cloudflare TLS fingerprinting on all pages.
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


class ExpertGamingScraper(FastScraper):
    """Full-Playwright scraper for expert-gaming.tn (Cloudflare-protected WooCommerce)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("expert_gaming", logger)
        self._pw = None
        self._browser = None
        self._pw_context = None
        self._tor_slot = hash("expert_gaming") % max(TorPool.get().size, 1)

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

    # ------------------------------------------------------------------
    # Playwright-based frontpage download
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        """Download frontpage using Playwright (Cloudflare + JS-rendered menu)."""
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"📥 Downloading (Playwright): {self.base_url}")

        await self._ensure_browser()
        page = await self._pw_context.new_page()
        try:
            await page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector("ul#menu-notre-boutique", state="attached", timeout=10000)
            except Exception:
                self.logger.warning("Menu selector not found, continuing with page content")
            html = await page.content()
        finally:
            await page.close()

        save_text_atomic(html, output_path, self.logger)
        self.logger.info(f"✓ Saved: {output_path} ({len(html):,} bytes)")
        return output_path

    # ------------------------------------------------------------------
    # Playwright-based fetch_html (replaces httpx for all pages)
    # ------------------------------------------------------------------

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
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status_code = resp.status if resp else None
            final_url = page.url
            html = await page.content()
            if status_code and status_code >= 400:
                error = f"HTTP {status_code}"
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
    # Close browser when scraping finishes
    # ------------------------------------------------------------------

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
        if url.startswith("/"):
            return f"{self.base_url}{url}"
        return f"{self.base_url}/{url}"

    def _parse_price(self, text: str) -> Optional[float]:
        """Extract numeric price from text like '1,234.500 TND'."""
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", text)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract category hierarchy from WooCommerce nav menu."""
        tree = HTMLParser(html)
        categories = []

        menu = tree.css_first("ul#menu-notre-boutique")
        if not menu:
            self.logger.warning("Could not find ul#menu-notre-boutique")
            return {"categories": [], "stats": {}}

        top_items = menu.css(
            "li.menu-item-type-taxonomy.menu-item-object-product_cat"
        )
        if not top_items:
            top_items = menu.css("li.menu-item")

        self.logger.info(f"Found {len(top_items)} top-level menu items")

        for top_li in top_items:
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

            sub_menu = top_li.css_first("ul.sub-menu")
            if sub_menu:
                low_items = sub_menu.css(
                    "li.menu-item-type-taxonomy.menu-item-object-product_cat"
                )
                if not low_items:
                    low_items = sub_menu.css("li.menu-item")

                for low_li in low_items:
                    low_link = low_li.css_first("a")
                    if not low_link:
                        continue

                    low_name = self._clean_text(low_link.text(strip=True))
                    low_url = self._make_absolute_url(
                        low_link.attributes.get("href", "")
                    )

                    if not low_name:
                        continue

                    low_cat = {
                        "name": low_name,
                        "url": low_url,
                        "level": "low",
                        "subcategories": [],
                    }

                    sub_sub_menu = low_li.css_first("ul.sub-menu")
                    if sub_sub_menu:
                        sub_items = sub_sub_menu.css(
                            "li.menu-item-type-taxonomy.menu-item-object-product_cat"
                        )
                        if not sub_items:
                            sub_items = sub_sub_menu.css("li.menu-item")

                        for sub_li in sub_items:
                            sub_link = sub_li.css_first("a")
                            if not sub_link:
                                continue
                            sub_name = self._clean_text(sub_link.text(strip=True))
                            sub_url = self._make_absolute_url(
                                sub_link.attributes.get("href", "")
                            )
                            if sub_name:
                                low_cat["subcategories"].append(
                                    {
                                        "name": sub_name,
                                        "url": sub_url,
                                        "level": "subcategory",
                                    }
                                )

                    top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        if categories and all(not c.get("low_level_categories") for c in categories):
            anchors = menu.css("a[href]")
            fallback_lows = []
            seen = set()
            for a in anchors:
                href = self._make_absolute_url(a.attributes.get("href", ""))
                name = self._clean_text(a.text(strip=True))
                if not href or not name:
                    continue
                href_l = href.lower()
                if any(
                    bad in href_l
                    for bad in (
                        "/my-account",
                        "/panier",
                        "/wishlist",
                        "/contact",
                        "/a-propos",
                    )
                ):
                    continue
                if not re.search(r"/[a-z0-9-]{3,}/?$", href_l):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                fallback_lows.append(
                    {"name": name, "url": href, "level": "low", "subcategories": []}
                )
            if fallback_lows:
                categories[0]["low_level_categories"] = fallback_lows

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

    # ------------------------------------------------------------------
    # Products (listing page)
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from a WooCommerce category listing page."""
        tree = HTMLParser(html)
        products = []
        seen_ids = set()

        for item in tree.css("li.product.type-product, section.product"):
            classes = item.attributes.get("class", "")
            product_id = None
            id_match = re.search(r"post-(\d+)", classes)
            if id_match:
                product_id = id_match.group(1)
            if not product_id:
                product_id = item.attributes.get("data-product_id")

            if product_id and product_id in seen_ids:
                continue
            if product_id:
                seen_ids.add(product_id)

            name_el = item.css_first(
                "h2.woocommerce-loop-product__title, "
                "a.woocommerce-LoopProduct-link h2, "
                "h3.heading-title.product-name a, "
                "h2.product-title a"
            )
            product_name = self._clean_text(name_el.text(strip=True)) if name_el else ""
            if not product_name:
                img_name = item.css_first("img[alt]")
                if img_name:
                    product_name = self._clean_text(img_name.attributes.get("alt", ""))

            link_el = item.css_first(
                "a.woocommerce-LoopProduct-link, "
                "h3.heading-title.product-name a, "
                "a[href]"
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

            img_el = item.css_first(
                "a.woocommerce-LoopProduct-link img, "
                "div.thumbnail-wrapper figure img.wp-post-image, "
                "img.attachment-woocommerce_thumbnail"
            )
            if img_el:
                image_url = (
                    img_el.attributes.get("src")
                    or img_el.attributes.get("data-src")
                    or img_el.attributes.get("data-lazy-src")
                )
                if image_url:
                    product_data["image"] = self._make_absolute_url(image_url)

            del_el = item.css_first("span.price del span.woocommerce-Price-amount.amount bdi")
            ins_el = item.css_first("span.price ins span.woocommerce-Price-amount.amount bdi")

            if del_el and ins_el:
                product_data["old_price"] = self._parse_price(del_el.text())
                product_data["price"] = self._parse_price(ins_el.text())
            else:
                price_el = item.css_first(
                    "span.woocommerce-Price-amount.amount bdi, "
                    "span.price span.woocommerce-Price-amount bdi"
                )
                product_data["price"] = self._parse_price(
                    price_el.text() if price_el else None
                )

            if product_data.get("old_price") and product_data.get("price"):
                product_data["discount_percent"] = round(
                    (1 - product_data["price"] / product_data["old_price"]) * 100
                )

            brand_el = item.css_first("span.loop-product-categories a")
            if brand_el:
                product_data["brand"] = self._clean_text(brand_el.text(strip=True))

            products.append(product_data)

        if products:
            return products

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
                if not isinstance(block, dict):
                    continue
                if block.get("@type") != "Product":
                    continue
                url = self._make_absolute_url(block.get("url", ""))
                if not url:
                    continue
                products.append(
                    {
                        "id": str(block.get("sku") or block.get("productID") or ""),
                        "url": url,
                        "name": self._clean_text(block.get("name", "")),
                        "price": self._parse_price(
                            str((block.get("offers") or {}).get("price", ""))
                        ),
                    }
                )
        return products

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        """WooCommerce pagination: /page/{n}/ suffix."""
        base = base_url.rstrip("/")
        base = re.sub(r"/page/\d+/?$", "", base)
        return f"{base}/page/{page_num}/"

    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from WooCommerce category page."""
        tree = HTMLParser(html)
        max_page = 1
        current_page = 1

        page_links = tree.css(
            "nav.woocommerce-pagination ul.page-numbers li a.page-numbers:not(.next):not(.prev), "
            "ul.page-numbers li a.page-numbers:not(.next):not(.prev)"
        )
        for link in page_links:
            try:
                num = int(link.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass

        current_el = tree.css_first(
            "nav.woocommerce-pagination ul.page-numbers li span.page-numbers.current, "
            "ul.page-numbers li span.page-numbers.current"
        )
        if current_el:
            try:
                current_page = int(current_el.text(strip=True))
                if current_page > max_page:
                    max_page = current_page
            except ValueError:
                pass

        has_next = tree.css_first(
            "nav.woocommerce-pagination ul.page-numbers li a.next, "
            "ul.page-numbers li a.next"
        ) is not None

        return {
            "current_page": current_page,
            "total_pages": max_page,
            "has_next": has_next,
        }

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape detailed product info from a WooCommerce product page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        body = tree.css_first("body")
        if body:
            body_cls = body.attributes.get("class", "")
            id_match = re.search(r"postid-(\d+)", body_cls)
            if id_match:
                data["product_id"] = id_match.group(1)
        if "product_id" not in data:
            url_match = re.search(r"/product/[^/]+-(\d+)/?", url)
            data["product_id"] = url_match.group(1) if url_match else None

        title_el = tree.css_first("h1.product_title.entry-title, h1.product_title")
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        sku_el = tree.css_first("span.sku, div.sku-wrapper span.sku")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        brand_el = tree.css_first(
            "div.product_meta span.posted_in a, "
            "div.product-brands a, "
            "div.product_meta .brand a"
        )
        data["brand"] = self._clean_text(brand_el.text(strip=True)) if brand_el else None

        del_el = tree.css_first("p.price del span.woocommerce-Price-amount.amount bdi")
        ins_el = tree.css_first("p.price ins span.woocommerce-Price-amount.amount bdi")

        if del_el and ins_el:
            data["old_price"] = self._parse_price(del_el.text())
            data["price"] = self._parse_price(ins_el.text())
        else:
            price_el = tree.css_first(
                "p.price span.woocommerce-Price-amount.amount bdi, "
                "span.woocommerce-Price-amount.amount bdi"
            )
            data["price"] = self._parse_price(price_el.text() if price_el else None)
            data["old_price"] = None

        if data.get("old_price") and data.get("price"):
            data["discount_percent"] = round(
                (1 - data["price"] / data["old_price"]) * 100
            )

        stock_el = tree.css_first("p.stock.in-stock")
        if stock_el:
            data["availability"] = self._clean_text(stock_el.text(strip=True))
            data["available"] = True
        else:
            oos_el = tree.css_first("p.stock.out-of-stock")
            if oos_el:
                data["availability"] = self._clean_text(oos_el.text(strip=True))
                data["available"] = False
            else:
                avail_el = tree.css_first(
                    "div.availability.stock span.availability-text, "
                    "p.stock"
                )
                if avail_el:
                    avail_text = avail_el.text(strip=True).lower()
                    data["availability"] = avail_el.text(strip=True)
                    data["available"] = (
                        "in stock" in avail_text
                        or "en stock" in avail_text
                        or "disponible" in avail_text
                    ) and "rupture" not in avail_text
                else:
                    data["availability"] = None
                    data["available"] = None

        desc_el = tree.css_first(
            "div.woocommerce-product-details__short-description, "
            "div#tab-description .panel-body, "
            "div#tab-description"
        )
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        specs = {}
        for row in tree.css(
            "table.woocommerce-product-attributes.shop_attributes tr, "
            "table.shop_attributes tr"
        ):
            key_el = row.css_first(
                "th.woocommerce-product-attributes-item__label, th"
            )
            val_el = row.css_first(
                "td.woocommerce-product-attributes-item__value, td"
            )
            if key_el and val_el:
                k = self._clean_text(key_el.text(strip=True))
                v = self._clean_text(val_el.text(strip=True))
                if k and v:
                    specs[k] = v
        data["specifications"] = specs

        images = []
        main_img = tree.css_first("div.woocommerce-product-gallery__image img")
        if main_img:
            src = (
                main_img.attributes.get("data-large_image")
                or main_img.attributes.get("data-src")
                or main_img.attributes.get("src")
            )
            if src:
                images.append(self._make_absolute_url(src))

        for img in tree.css(
            "ol.flex-control-thumbs li img, "
            "div.woocommerce-product-gallery__image:not(:first-child) img, "
            "div.woocommerce-product-gallery .woocommerce-product-gallery__image img"
        ):
            src = (
                img.attributes.get("data-large_image")
                or img.attributes.get("data-src")
                or img.attributes.get("src")
            )
            if src:
                abs_src = self._make_absolute_url(src)
                if abs_src not in images:
                    images.append(abs_src)

        data["images"] = images[:10] if images else None

        return data


def get_scraper(logger: logging.Logger) -> ExpertGamingScraper:
    """Factory function to get scraper."""
    return ExpertGamingScraper(logger)
