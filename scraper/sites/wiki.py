#!/usr/bin/env python3
"""
Wiki.tn specific scraper implementation.
Hybrid: Playwright for all page fetches (site is behind Cloudflare TLS fingerprinting),
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


class WikiScraper(FastScraper):
    """Hybrid scraper for wiki.tn (behind Cloudflare, requires Playwright for all fetches)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("wiki", logger)
        self._pw = None
        self._browser = None
        self._tor_slot = hash("wiki") % max(TorPool.get().size, 1)

    # ------------------------------------------------------------------
    # Shared Playwright browser (lazy init, reused across all fetches)
    # Each fetch gets its own fresh context to avoid CF session detection.
    # ------------------------------------------------------------------

    async def _ensure_browser(self):
        """Lazily start the shared Playwright browser."""
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=playwright_launch_args(),
        )

    async def _new_context(self):
        """Create a fresh browser context for a single request."""
        await self._ensure_browser()
        pool = TorPool.get()
        ctx = await self._browser.new_context(
            user_agent=STEALTH_UA,
            proxy=pool.pw_proxy(self._tor_slot)
            or proxy_url_to_playwright(self.proxy_url),
        )
        await ctx.add_init_script(STEALTH_JS)
        return ctx

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
            "wait_selector",
            "nav.desktop-nav, nav.brxe-nav-nested, nav[class*='nav']",
        )

        ctx = await self._new_context()
        page = await ctx.new_page()
        try:
            await page.goto(self.base_url, wait_until="networkidle", timeout=60000)
            try:
                await page.wait_for_selector(wait_sel, timeout=15000)
            except Exception:
                self.logger.warning(f"Wait selector '{wait_sel}' not found, continuing")
            html = await page.content()
        finally:
            await page.close()
            await ctx.close()

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
        """Fetch HTML using a fresh browser context per request.

        wiki.tn uses Cloudflare Bot Management that detects session reuse,
        so each request gets its own isolated context.
        """
        started = time.monotonic()
        ctx = await self._new_context()
        page = await ctx.new_page()
        status_code = None
        final_url = url
        html = None
        error = None
        try:
            resp = await page.goto(url, wait_until="networkidle", timeout=45000)
            status_code = resp.status if resp else None
            final_url = page.url
            try:
                await page.wait_for_selector(
                    "div.product-card--grid, div.product-card, li.product",
                    timeout=5000,
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
            await ctx.close()
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
        """Run full scrape, then close the shared browser."""
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
        """Extract numeric price from text like '1 299,000 DT' or '299.000'."""
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,\s]", "", text).strip()
        # Remove spaces used as thousand separators
        cleaned = re.sub(r"\s+", "", cleaned)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    def _parse_woo_price(self, el) -> Optional[float]:
        """Parse WooCommerce price from a bdi element."""
        if not el:
            return None
        # Remove currency symbol nodes and get text
        text = el.text(strip=True)
        return self._parse_price(text)

    # ------------------------------------------------------------------
    # Category extraction (from Playwright-rendered HTML)
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract category hierarchy from Bricks Builder mega menu.

        Top-level items are label-only (no product URLs).
        Actual category URLs come from low-level headings and subcategory links.
        """
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories = []

        top_blocks = tree.css(
            fp.get("top_level_blocks", "nav.brx-nav-nested > ul > li.brx-has-megamenu")
        )
        if not top_blocks:
            top_blocks = tree.css(
                "nav.brxe-nav-nested.desktop-nav ul.brx-nav-nested-items > li.brxe-dropdown"
            )
        self.logger.info(f"Found {len(top_blocks)} top-level category blocks")

        for top_block in top_blocks:
            # Top-level name: label-only span or anchor text
            top_name_el = top_block.css_first(
                fp.get("top_level_name", "div.brx-submenu-toggle > span")
            )
            if not top_name_el:
                top_link = top_block.css_first("a")
                top_name = (
                    self._clean_text(top_link.text(strip=True)) if top_link else ""
                )
            else:
                top_name = self._clean_text(top_name_el.text(strip=True))

            # Top-level URL (usually # or empty for mega menu labels)
            top_link = top_block.css_first("a")
            top_url_raw = top_link.attributes.get("href", "") if top_link else ""
            top_url = (
                self._make_absolute_url(top_url_raw)
                if top_url_raw and top_url_raw != "#"
                else None
            )

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            # Low-level: subcategory headings with actual URLs
            low_headings = top_block.css(fp.get("low_level_link", "h6.subcategory-heading a"))
            if not low_headings:
                low_headings = top_block.css(
                    "div.brx-submenu-content a[href], ul.brx-dropdown-content a[href]"
                )

            for heading_el in low_headings:
                # If it's an <a> tag directly
                if heading_el.tag == "a":
                    low_name = self._clean_text(heading_el.text(strip=True))
                    low_url = self._make_absolute_url(
                        heading_el.attributes.get("href", "")
                    )
                    # Find subcategories in the sibling div
                    parent_li = heading_el.parent
                    if parent_li and parent_li.tag == "h6":
                        parent_li = parent_li.parent
                else:
                    # It's an li or container element
                    link = heading_el.css_first("a")
                    if not link:
                        continue
                    low_name = self._clean_text(link.text(strip=True))
                    low_url = self._make_absolute_url(link.attributes.get("href", ""))
                    parent_li = heading_el

                low_cat = {
                    "name": low_name,
                    "url": low_url,
                    "level": "low",
                    "subcategories": [],
                }

                # Subcategories: links in the sibling subcategories container
                if parent_li:
                    sub_links = parent_li.css(
                        fp.get("subcategory_items", "div.subcategories-div a")
                    )
                    for sub_link in sub_links:
                        sub_name = self._clean_text(sub_link.text(strip=True))
                        sub_url = self._make_absolute_url(
                            sub_link.attributes.get("href", "")
                        )
                        if sub_name and sub_url:
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
        # WP Grid Builder uses /page/{n}/ path-based pagination
        # Remove existing /page/N/ if present
        base = re.sub(r"/page/\d+/?", "", base_url).rstrip("/")
        return f"{base}/page/{page_num}/"

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from WooCommerce / WP Grid Builder category page."""
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products = []

        items = tree.css(cp.get("item_selector", "div.product-card--grid"))
        if not items:
            items = tree.css("div.product-card")
        if not items:
            items = tree.css("div.wp-grid-builder article.product")
        if not items:
            items = tree.css("li.product.type-product")

        for item in items:
            # Product ID — from add-to-cart button data attr or post-{id} class
            product_id = None
            id_el = item.css_first(cp.get("item_id_selector", "a.add_to_cart_button"))
            if id_el:
                product_id = id_el.attributes.get(
                    cp.get("item_id_attr", "data-product_id")
                )
            if not product_id:
                classes = item.attributes.get("class", "")
                id_match = re.search(r"post-(\d+)", classes)
                if id_match:
                    product_id = id_match.group(1)

            # Name & URL
            link = item.css_first(cp.get("item_name", "h3.product-card__title a"))
            if not link:
                link = item.css_first("figure.product-card__image a")
            if not link:
                link = item.css_first("h2.woocommerce-loop-product__title a")
            if not link:
                link = item.css_first("h2.woocommerce-loop-product__title")
                if link:
                    # Title without link — find link elsewhere
                    parent_link = item.css_first("a.woocommerce-LoopProduct-link")
                    product_url = (
                        self._make_absolute_url(parent_link.attributes.get("href", ""))
                        if parent_link
                        else None
                    )
                    product_name = self._clean_text(link.text(strip=True))
                else:
                    # Home cards may only provide linked image + alt text.
                    figure_link = item.css_first("figure.product-card__image a")
                    figure_img = item.css_first("figure.product-card__image img")
                    product_url = (
                        self._make_absolute_url(figure_link.attributes.get("href", ""))
                        if figure_link
                        else None
                    )
                    product_name = self._clean_text(
                        (figure_img.attributes.get("alt", "") if figure_img else "")
                    )
                    if not product_url:
                        continue
            else:
                product_url = self._make_absolute_url(link.attributes.get("href", ""))
                product_name = self._clean_text(link.text(strip=True))
                if not product_name:
                    img_fallback = item.css_first("figure.product-card__image img[alt]")
                    if img_fallback:
                        product_name = self._clean_text(
                            img_fallback.attributes.get("alt", "")
                        )

            if not product_url:
                continue

            product_data = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }

            # Price — handle del/ins for sale pricing
            # Check for sale price first (ins element)
            sale_price_el = item.css_first(
                cp.get(
                    "item_old_price",
                    ".product-card__price del .woocommerce-Price-amount bdi",
                )
            )
            ins_price_el = item.css_first(
                ".product-card__price ins .woocommerce-Price-amount bdi"
            )

            if sale_price_el and ins_price_el:
                # On sale: del = old, ins = current
                product_data["price"] = self._parse_woo_price(ins_price_el)
                product_data["old_price"] = self._parse_woo_price(sale_price_el)
            else:
                # Regular price
                price_el = item.css_first(
                    cp.get(
                        "item_price",
                        ".product-card__price .woocommerce-Price-amount bdi",
                    )
                )
                if not price_el:
                    price_el = item.css_first(
                        "span.woocommerce-Price-amount.amount bdi"
                    )
                product_data["price"] = self._parse_woo_price(price_el)

            # Brand
            brand_el = item.css_first(
                cp.get("item_brand", ".product-card__brand-logo img")
            )
            if brand_el:
                brand_text = brand_el.attributes.get(
                    cp.get("item_brand_attr", "alt"), ""
                ) or ""
                product_data["brand"] = (
                    brand_text.strip()
                    or None
                )
            else:
                brand_div = item.css_first("div.product-brand")
                if brand_div:
                    product_data["brand"] = self._clean_text(brand_div.text(strip=True))

            # SKU
            sku_el = item.css_first(cp.get("item_sku", ".product-card__sku .sku"))
            if sku_el:
                product_data["sku"] = self._clean_text(sku_el.text(strip=True))

            # Image
            img_el = item.css_first(
                cp.get("item_image", "figure.product-card__image img")
            )
            if not img_el:
                img_el = item.css_first("img.attachment-woocommerce_thumbnail")
            if img_el:
                for attr in cp.get("item_image_attrs", ["src", "data-src"]):
                    src = img_el.attributes.get(attr)
                    if src and not src.startswith("data:"):
                        product_data["image"] = self._make_absolute_url(src)
                        break

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

    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from WP Grid Builder facets."""
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})

        current_page = 1
        total_pages = 1
        has_next = False

        # WP Grid Builder pagination
        page_links = tree.css(
            "div.wpgb-pagination a.page-numbers, ul.page-numbers li a"
        )
        for el in page_links:
            text = el.text(strip=True)
            try:
                num = int(text)
                if num > total_pages:
                    total_pages = num
            except (ValueError, TypeError):
                continue

        # Current page from span.current
        current_el = tree.css_first(
            "div.wpgb-pagination span.page-numbers.current, ul.page-numbers li span.current"
        )
        if current_el:
            try:
                current_page = int(current_el.text(strip=True))
            except (ValueError, TypeError):
                pass

        # Next link
        next_link = tree.css_first(cp.get("pagination_next", "li.wpgb-page-next a"))
        if not next_link:
            next_link = tree.css_first("a.next.page-numbers")
        if next_link:
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
        """Scrape WooCommerce product detail page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data = {"url": url}

        # Product ID from body class
        body = tree.css_first("body")
        if body:
            body_classes = body.attributes.get("class", "")
            id_match = re.search(r"postid-(\d+)", body_classes)
            if id_match:
                data["product_id"] = id_match.group(1)

        # Title
        title_el = tree.css_first(pp.get("title", "h1.brxe-product-title"))
        if not title_el:
            title_el = tree.css_first("h1.product_title.entry-title")
        data["title"] = (
            self._clean_text(title_el.text(strip=True)) if title_el else None
        )

        # SKU
        sku_el = tree.css_first(pp.get("sku", ".product_meta .sku"))
        if not sku_el:
            sku_el = tree.css_first("span.sku")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        # Price — handle sale (del/ins) and regular
        price_sale_el = tree.css_first(
            pp.get(
                "price_sale",
                ".product-card__price-new .price ins .woocommerce-Price-amount bdi",
            )
        )
        price_orig_el = tree.css_first(
            pp.get(
                "price_original",
                ".product-card__price-new .price del .woocommerce-Price-amount bdi",
            )
        )

        if price_sale_el and price_orig_el:
            data["price"] = self._parse_woo_price(price_sale_el)
            data["old_price"] = self._parse_woo_price(price_orig_el)
        else:
            price_el = tree.css_first(
                pp.get(
                    "price",
                    ".product-card__price-new .price > .woocommerce-Price-amount bdi, p.price .woocommerce-Price-amount bdi, .price .woocommerce-Price-amount bdi",
                )
            )
            if not price_el:
                price_el = tree.css_first("p.price bdi")
            if not price_el:
                price_el = tree.css_first("p.price .woocommerce-Price-amount bdi")
            data["price"] = self._parse_woo_price(price_el)

        # Brand
        brand_el = tree.css_first(
            pp.get(
                "brand",
                ".product-card__logo-wrapper--big .product-card__brand-logo img",
            )
        )
        if brand_el:
            data["brand"] = (
                brand_el.attributes.get(pp.get("brand_attr", "alt"), "").strip() or None
            )
        else:
            meta_brand = tree.css_first("div.product_meta")
            if meta_brand:
                brand_text = self._clean_text(meta_brand.text(strip=True))
                brand_match = re.search(
                    r"(?:brand|marque)\s*:\s*(.+?)(?:\s*\||$)",
                    brand_text,
                    re.IGNORECASE,
                )
                if brand_match:
                    data["brand"] = brand_match.group(1).strip()

        # Availability
        avail_el = tree.css_first(
            pp.get("availability_badge", ".stock-status-badge[data-stock-status]")
        )
        if avail_el:
            data["availability"] = avail_el.attributes.get("data-stock-status", "")
            data["available"] = data["availability"] in ("instock", "onbackorder")
        else:
            stock_el = tree.css_first(
                pp.get(
                    "availability_woo", ".stock.in-stock, .stock.available-on-backorder"
                )
            )
            if stock_el:
                data["availability"] = self._clean_text(stock_el.text(strip=True))
                data["available"] = True
            else:
                oos_el = tree.css_first("p.stock.out-of-stock")
                if oos_el:
                    data["availability"] = self._clean_text(oos_el.text(strip=True))
                    data["available"] = False

        # Description
        desc_el = tree.css_first(
            pp.get("description", ".woocommerce-product-details__short-description")
        )
        data["description"] = (
            self._clean_text(desc_el.text(strip=True)) if desc_el else None
        )

        full_desc_el = tree.css_first(pp.get("full_description", ".woocommerce-tabs .panel, #tab-description"))
        if full_desc_el:
            data["full_description"] = self._clean_text(full_desc_el.text(strip=True))

        # Specs — WooCommerce product attributes table
        specs = {}
        specs_table = tree.css_first(pp.get("specs_container", "table.shop_attributes"))
        if not specs_table:
            specs_table = tree.css_first("table.woocommerce-product-attributes")
        if specs_table:
            rows = specs_table.css("tr")
            for row in rows:
                key_el = row.css_first(
                    pp.get("specs_key", "th.woocommerce-product-attributes-item__label")
                )
                if not key_el:
                    key_el = row.css_first("th")
                val_el = row.css_first(
                    pp.get(
                        "specs_value", "td.woocommerce-product-attributes-item__value"
                    )
                )
                if not val_el:
                    val_el = row.css_first("td")
                if key_el and val_el:
                    # Value may be in a <p> tag
                    val_p = val_el.css_first("p")
                    val_text = self._clean_text((val_p or val_el).text(strip=True))
                    key_text = self._clean_text(key_el.text(strip=True))
                    if key_text:
                        specs[key_text] = val_text
        if specs:
            data["specs"] = specs

        # Images — gallery
        images = []
        gallery_imgs = tree.css(
            pp.get("image_gallery", ".woocommerce-product-gallery__image a")
        )
        for a_el in gallery_imgs:
            # Prefer data-large_image from the <a> tag
            src = a_el.attributes.get("href")
            if not src:
                img = a_el.css_first("img")
                if img:
                    src = img.attributes.get("data-large_image") or img.attributes.get(
                        "src"
                    )
            if src and not src.startswith("data:"):
                abs_src = self._make_absolute_url(src)
                if abs_src not in images:
                    images.append(abs_src)

        if not images:
            # Fallback: direct img tags in gallery
            main_img = tree.css_first(
                pp.get(
                    "image_main",
                    ".woocommerce-product-gallery__image img.wp-post-image",
                )
            )
            if main_img:
                src = main_img.attributes.get(
                    "data-large_image"
                ) or main_img.attributes.get("src")
                if src and not src.startswith("data:"):
                    images.append(self._make_absolute_url(src))

        # Thumbnails
        for thumb in tree.css("ol.flex-control-thumbs li img"):
            src = thumb.attributes.get("src")
            if src and not src.startswith("data:"):
                abs_src = self._make_absolute_url(src)
                if abs_src not in images:
                    images.append(abs_src)

        data["images"] = images if images else None

        return data


def get_scraper(logger: logging.Logger) -> WikiScraper:
    return WikiScraper(logger)
