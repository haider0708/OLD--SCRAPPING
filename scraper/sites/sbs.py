#!/usr/bin/env python3
"""
SBS Informatique (sbsinformatique.com) specific scraper implementation.

Full CSR: Playwright for all phases — frontpage, listing pages, and product details.
Platform: PrestaShop + TvCMS MegaMenu + heavy JS product loading.
"""
import asyncio
import json
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser
from playwright.async_api import Page

from scraper.base import BaseScraper, CategoryInfo, save_text_atomic


class SbsScraper(BaseScraper):
    """Full-CSR Playwright scraper for sbsinformatique.com (PrestaShop + TvCMS)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("sbs", logger)

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
        """Extract numeric price from text like '1 234,500 DT'."""
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
    # Abstract implementations
    # ------------------------------------------------------------------

    def get_wait_selector(self) -> str:
        return self.selectors.get("category_page", {}).get(
            "wait_selector",
            "article.product-miniature.js-product-miniature",
        )

    def build_page_url(self, base_url: str, page_num: int) -> str:
        """PrestaShop pagination: ?page={n} query parameter."""
        param = self.selectors.get("category_page", {}).get("pagination_param", "page")
        base = re.sub(rf"[?&]{param}=\d+", "", base_url)
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{param}={page_num}"

    # ------------------------------------------------------------------
    # Frontpage — Playwright override (CSR category tree)
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        """Download frontpage using Playwright (block-categories requires JS)."""
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"📥 Downloading (Playwright): {self.base_url}")

        fp = self.selectors.get("frontpage", {})
        wait_sel = fp.get("wait_selector", "div.block-categories ul.category-top-menu")

        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            from scraper.base import playwright_launch_args, get_playwright_proxy
            browser = await pw.chromium.launch(headless=True, args=playwright_launch_args())
            page = await browser.new_page(
                proxy=get_playwright_proxy(self.site_name, self.config.get("settings", {}))
            )
            try:
                await page.goto(self.base_url, wait_until="networkidle", timeout=30000)
                try:
                    await page.wait_for_selector(wait_sel, timeout=10000)
                except Exception:
                    self.logger.warning(f"Wait selector '{wait_sel}' not found, continuing")
                await asyncio.sleep(self.wait_after_load)
                html = await page.content()
            finally:
                await page.close()
                await browser.close()

        save_text_atomic(html, output_path, self.logger)
        self.logger.info(f"✓ Saved: {output_path} ({len(html):,} bytes)")
        return output_path

    # ------------------------------------------------------------------
    # Categories — block-categories data-depth hierarchy
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        """
        Extract categories from block-categories sidebar with data-depth attributes.

        Structure:
        - li[data-depth='0'] > a  → top-level
        - li[data-depth='1'] > a.category-sub-link  → low-level
        - li[data-depth='2']/li[data-depth='3'] > a.category-sub-link  → subcategory
        """
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories = []
        seen_urls = set()

        # Find the category tree container
        container = tree.css_first(
            "div.block-categories ul.category-top-menu, "
            "ul.category-top-menu"
        )
        if not container:
            self.logger.warning("Category container not found")
            return {"categories": [], "stats": {}}

        top_sel = fp.get("top_level_items", "li[data-depth='0'] > a")
        top_items = container.css(top_sel)
        self.logger.info(f"Found {len(top_items)} top-level categories")

        # We need parent li elements for hierarchy traversal
        top_lis = container.css("li[data-depth='0']")

        for top_li in top_lis:
            top_link = top_li.css_first("a")
            if not top_link:
                continue

            top_name = self._clean_text(top_link.text(strip=True))
            top_url = self._make_absolute_url(top_link.attributes.get("href"))

            if not top_name:
                continue

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            # Low-level: data-depth="1"
            low_sel = fp.get("low_level_items", "li[data-depth='1'] > a.category-sub-link")
            low_lis = top_li.css("li[data-depth='1']")

            for low_li in low_lis:
                low_link = low_li.css_first("a.category-sub-link, a")
                if not low_link:
                    continue

                low_name = self._clean_text(low_link.text(strip=True))
                low_url = self._make_absolute_url(low_link.attributes.get("href"))

                if not low_name or low_url in seen_urls:
                    continue
                if low_url:
                    seen_urls.add(low_url)

                low_cat = {
                    "name": low_name,
                    "url": low_url,
                    "level": "low",
                    "subcategories": [],
                }

                # Subcategories: data-depth="2" and data-depth="3"
                sub_sel = fp.get(
                    "subcategory_items",
                    "li[data-depth='2'] > a.category-sub-link, li[data-depth='3'] > a.category-sub-link",
                )
                sub_links = low_li.css(sub_sel)

                for sub_link in sub_links:
                    sub_name = self._clean_text(sub_link.text(strip=True))
                    sub_url = self._make_absolute_url(sub_link.attributes.get("href"))
                    if sub_name and sub_url and sub_url not in seen_urls:
                        seen_urls.add(sub_url)
                        low_cat["subcategories"].append({
                            "name": sub_name,
                            "url": sub_url,
                            "level": "subcategory",
                        })

                top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        # Stats
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
    # Products — PrestaShop listing page (Playwright page.evaluate)
    # ------------------------------------------------------------------

    async def extract_products_from_page(self, page: Page) -> List[dict]:
        """Extract products from a loaded PrestaShop category page."""
        cp = self.selectors.get("category_page", {})
        item_selector = cp.get(
            "item_selector",
            "article.product-miniature.js-product-miniature",
        )
        grid_scope = cp.get("grid_scope", ".tvproduct-wrapper.grid")

        products = await page.evaluate(
            """(args) => {
                const [selector, gridScope] = args;
                // Scope to main product grid to avoid sidebar/related items
                const scope = gridScope
                    ? (document.querySelector(gridScope) || document)
                    : document;
                const items = scope.querySelectorAll(selector);
                const products = [];
                const seenIds = new Set();

                const parsePrice = (text) => {
                    if (!text) return null;
                    const cleaned = text.replace(/[^\\d.,]/g, '');
                    let n = cleaned;
                    if (n.includes(',') && n.includes('.')) n = n.replace(',', '');
                    else if (n.includes(',')) n = n.replace(',', '.');
                    const v = parseFloat(n);
                    return isNaN(v) ? null : v;
                };

                items.forEach(item => {
                    const productId = item.getAttribute('data-id-product');
                    if (productId && seenIds.has(productId)) return;
                    if (productId) seenIds.add(productId);

                    // Name
                    const nameEl = item.querySelector(
                        'h6[itemprop="name"], '
                        + 'div.tvproduct-name.product-title a h6, '
                        + 'h2.product-title a, '
                        + 'div.tvproduct-name a'
                    );
                    const name = nameEl ? nameEl.innerText.trim() : '';

                    // URL
                    const linkEl = item.querySelector(
                        'div.tvproduct-name.product-title a, '
                        + 'h2.product-title a, '
                        + 'a.thumbnail.product-thumbnail'
                    );
                    const url = linkEl ? linkEl.getAttribute('href') : null;

                    if (!productId && !url) return;

                    const p = {
                        id: productId || null,
                        url: url,
                        name: name,
                    };

                    // Image
                    const imgEl = item.querySelector(
                        'img.tvproduct-defult-img.tv-img-responsive, '
                        + 'img.product-thumbnail-first, '
                        + 'a.thumbnail.product-thumbnail img'
                    );
                    if (imgEl) {
                        const src = imgEl.getAttribute('src')
                            || imgEl.getAttribute('data-src')
                            || imgEl.getAttribute('data-lazy-src');
                        if (src) p.image = src;
                    }

                    // Price
                    const priceEl = item.querySelector(
                        '.product-price-and-shipping span.price, span.price'
                    );
                    p.price = parsePrice(priceEl ? priceEl.innerText : null);

                    // Old price
                    const oldPriceEl = item.querySelector(
                        '.product-price-and-shipping span.regular-price, span.regular-price'
                    );
                    p.old_price = parsePrice(oldPriceEl ? oldPriceEl.innerText : null);
                    if (p.old_price && p.price) {
                        p.discount_percent = Math.round((1 - p.price / p.old_price) * 100);
                    }

                    // Brand
                    const brandEl = item.querySelector(
                        'span.product-manufacturer a.brand, '
                        + 'span.manufacturer a, '
                        + 'div.product-manufacturer a'
                    );
                    if (brandEl) p.brand = brandEl.innerText.trim();

                    // Out of stock flag
                    const oosFlag = item.querySelector(
                        'ul.product-flags > li.product-flag.out_of_stock, li.out_of_stock'
                    );
                    if (oosFlag) {
                        p.availability = 'Rupture de stock';
                        p.available = false;
                    }

                    products.push(p);
                });

                return products;
            }""",
            [item_selector, grid_scope],
        )

        return products

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def extract_pagination_info(self, page: Page) -> dict:
        """Extract PrestaShop pagination info from the loaded page."""
        cp = self.selectors.get("category_page", {})
        next_sel = cp.get("pagination_next", "a.next.js-search-link[rel='next']")

        pagination = await page.evaluate(
            """(nextSel) => {
                let maxPage = 1;
                let currentPage = 1;

                // Current page from active/current element
                const currentEl = document.querySelector(
                    'nav.pagination li.current a, '
                    + 'nav.pagination li.active a, '
                    + 'div.pagination li.current a'
                );
                if (currentEl) {
                    const n = parseInt(currentEl.innerText);
                    if (!isNaN(n)) currentPage = n;
                }

                // Max page from all pagination links
                document.querySelectorAll(
                    'nav.pagination a.js-search-link, div.pagination a.js-search-link'
                ).forEach(el => {
                    const href = el.getAttribute('href') || '';
                    const m = href.match(/[?&]page=(\\d+)/);
                    if (m) {
                        const n = parseInt(m[1]);
                        if (n > maxPage) maxPage = n;
                    }
                    const n = parseInt(el.innerText);
                    if (!isNaN(n) && n > maxPage) maxPage = n;
                });

                // Detect next link
                const nextLink = document.querySelector(nextSel);
                const hasNext = !!nextLink;

                return {
                    current_page: currentPage,
                    total_pages: Math.max(maxPage, currentPage),
                    has_next: hasNext,
                };
            }""",
            next_sel,
        )

        return pagination

    # ------------------------------------------------------------------
    # Product details (Playwright) — JSON-primary + HTML fallback
    # ------------------------------------------------------------------

    async def scrape_product_details(self, page: Page, product_url: str) -> dict:
        """
        Scrape detailed product info from a PrestaShop product page.
        Uses JSON-primary approach via data-product attribute, with HTML fallback.
        """
        pp = self.selectors.get("product_page", {})
        wait_sel = pp.get("wait_selector", "div.current-price span.price")

        last_err = None
        for attempt in range(2):
            try:
                await page.goto(product_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    # Wait for the always-present title rather than a JS-injected price
                    await page.wait_for_selector("h1[itemprop='name'], h1.h1", timeout=12000)
                except Exception:
                    try:
                        await page.wait_for_selector(wait_sel, timeout=5000)
                    except Exception:
                        pass
                last_err = None
                break
            except Exception as e:
                last_err = str(e)
                continue
        if last_err is not None:
            return {"url": product_url, "error": last_err}

        data = await page.evaluate(
            """(selectors) => {
                const data = {url: window.location.href};

                const cleanText = (el) => el ? el.innerText.replace(/\\s+/g, ' ').trim() : null;

                const parsePrice = (text) => {
                    if (!text) return null;
                    const cleaned = text.replace(/[^\\d.,]/g, '');
                    let n = cleaned;
                    if (n.includes(',') && n.includes('.')) n = n.replace(',', '');
                    else if (n.includes(',')) n = n.replace(',', '.');
                    const v = parseFloat(n);
                    return isNaN(v) ? null : v;
                };

                // JSON-primary: data-product attribute (PrestaShop standard)
                const jsonEl = document.querySelector('div.tab-pane#product-details[data-product]');
                let productJson = null;
                if (jsonEl) {
                    try {
                        productJson = JSON.parse(jsonEl.getAttribute('data-product'));
                    } catch (e) {}
                }

                if (productJson) {
                    data.product_id = String(productJson.id_product || '') || null;
                    data.title = productJson.name || null;
                    data.sku = productJson.reference || null;

                    // Price from JSON
                    if (productJson.price_amount !== undefined && productJson.price_amount !== null) {
                        data.price = parseFloat(productJson.price_amount) || null;
                    } else {
                        data.price = parsePrice(productJson.price);
                    }

                    // Availability from JSON
                    data.availability = productJson.availability_message || null;
                    if (productJson.availability === 'available') {
                        data.available = true;
                    } else if (productJson.availability === 'unavailable') {
                        data.available = false;
                    } else if (productJson.availability === 'last_remaining_items') {
                        data.available = true;
                    } else {
                        const qty = productJson.quantity;
                        data.available = (typeof qty === 'number') ? qty > 0 : null;
                    }
                } else {
                    // HTML fallback — product ID from URL (PrestaShop: /123-slug.html)
                    const urlMatch = window.location.href.match(/\\/(\\d+)-/);
                    data.product_id = urlMatch ? urlMatch[1] : null;

                    // Title
                    const titleEl = document.querySelector(
                        selectors.title || "h1.h1[itemprop='name']"
                    ) || document.querySelector('h1.h1, h1[itemprop="name"]');
                    data.title = cleanText(titleEl);

                    // SKU
                    const skuEl = document.querySelector(
                        selectors.sku || "div.product-reference span[itemprop='sku']"
                    );
                    data.sku = cleanText(skuEl);

                    // Price from content attribute
                    const priceEl = document.querySelector(
                        selectors.price || "div.current-price span.price[itemprop='price']"
                    );
                    if (priceEl) {
                        const contentAttr = selectors.price_content_attr || 'content';
                        const contentVal = priceEl.getAttribute(contentAttr);
                        if (contentVal) {
                            data.price = parseFloat(contentVal) || null;
                        }
                        if (!data.price) {
                            data.price = parsePrice(priceEl.innerText);
                        }
                    }

                    // Availability
                    const availEl = document.querySelector(
                        selectors.availability || 'span#product-availability'
                    );
                    if (availEl) {
                        const txt = availEl.innerText.trim();
                        data.availability = txt;
                        const lower = txt.toLowerCase();
                        data.available = (
                            lower.includes('en stock') || lower.includes('disponible') || lower.includes('in stock')
                        ) && !lower.includes('rupture') && !lower.includes('indisponible');
                    } else {
                        // Schema.org fallback
                        const availLink = document.querySelector(
                            selectors.availability_schema || "link[itemprop='availability'][href]"
                        );
                        if (availLink) {
                            const href = availLink.getAttribute('href') || '';
                            if (href.includes('InStock')) {
                                data.availability = 'En stock';
                                data.available = true;
                            } else if (href.includes('OutOfStock')) {
                                data.availability = 'Rupture de stock';
                                data.available = false;
                            }
                        }
                    }
                }

                // Old price (always from HTML)
                const oldPriceEl = document.querySelector(
                    selectors.old_price || 'div.product-discount span.regular-price'
                ) || document.querySelector('span.regular-price');
                data.old_price = parsePrice(oldPriceEl ? oldPriceEl.innerText : null);
                if (data.old_price && data.price) {
                    data.discount_percent = Math.round((1 - data.price / data.old_price) * 100);
                }

                // Brand
                const brandImg = document.querySelector(
                    selectors.brand || 'a.tvproduct-brand img'
                ) || document.querySelector('div.product-manufacturer img');
                if (brandImg) {
                    data.brand = brandImg.getAttribute('alt') || null;
                    const brandSrc = brandImg.getAttribute('src');
                    if (brandSrc) data.brand_logo = brandSrc;
                } else {
                    const brandEl = document.querySelector(
                        'div.product-manufacturer a, a.tvproduct-brand'
                    );
                    data.brand = brandEl ? brandEl.innerText.trim() : null;
                }

                // Short description
                const descEl = document.querySelector(
                    selectors.description || "div[id^='product-description-short-']"
                );
                data.description = cleanText(descEl);

                // Full description
                const fullDescEl = document.querySelector(
                    selectors.full_description || 'div.tab-pane#description div.product-description'
                );
                if (fullDescEl) {
                    data.full_description = cleanText(fullDescEl);
                }

                // Specifications (dt/dd in data-sheet)
                const specs = {};
                const specsContainer = document.querySelector(
                    selectors.specs_container || 'div.product-features dl.data-sheet'
                );
                if (specsContainer) {
                    const dts = specsContainer.querySelectorAll(selectors.specs_key || 'dt.name');
                    const dds = specsContainer.querySelectorAll(selectors.specs_value || 'dd.value');
                    for (let i = 0; i < dts.length && i < dds.length; i++) {
                        const k = dts[i].innerText.trim();
                        const v = dds[i].innerText.trim();
                        if (k && v) specs[k] = v;
                    }
                }
                data.specifications = specs;

                // Images
                const images = [];

                // Main product cover image
                const mainImg = document.querySelector(
                    'div.product-cover img.js-qv-product-cover, div.product-cover img'
                );
                if (mainImg) {
                    const src = mainImg.getAttribute('data-image-large-src')
                        || mainImg.getAttribute('data-src')
                        || mainImg.getAttribute('src');
                    if (src && !images.includes(src)) images.push(src);
                }

                // Thumbnail images
                document.querySelectorAll('ul.product-images img.thumb, ul.product-images img').forEach(img => {
                    const src = img.getAttribute('data-image-large-src')
                        || img.getAttribute('src');
                    if (src && !images.includes(src)) images.push(src);
                });

                data.images = images;

                return data;
            }""",
            pp,
        )

        return data


# Factory function — required
def get_scraper(logger: logging.Logger) -> SbsScraper:
    """Factory function to create scraper instance."""
    return SbsScraper(logger)
