#!/usr/bin/env python3
"""
Mytek.tn specific scraper implementation.
"""

import asyncio
import logging
from typing import List

from playwright.async_api import BrowserContext, Page
from selectolax.parser import HTMLParser

from scraper.base import BaseScraper, CategoryInfo, ScrapeStats


class MytekScraper(BaseScraper):
    """Scraper for mytek.tn e-commerce site."""

    def __init__(self, logger: logging.Logger):
        super().__init__("mytek", logger)

    def get_wait_selector(self) -> str:
        """CSS selector to wait for — mytek's SEO data layer is in static HTML."""
        return self.selectors.get("category_page", {}).get(
            "wait_selector", "#seo-product-data > div[data-id]"
        )

    def build_page_url(self, base_url: str, page_num: int) -> str:
        """Build paginated URL for mytek."""
        if "?" in base_url:
            return f"{base_url}&p={page_num}"
        return f"{base_url}?p={page_num}"

    async def scrape_single_page(self, page: Page, url: str) -> dict:
        """Override: use domcontentloaded — product data is in static SEO HTML.

        mytek embeds all product data in a hidden #seo-product-data div that
        is present in the raw server-rendered HTML before any JavaScript runs.
        Using 'domcontentloaded' instead of 'networkidle' avoids waiting for
        mytek's continuous background XHR requests that keep the network busy
        and cause the default 'networkidle' wait to time out every time.
        """
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=self.page_timeout
            )
            await page.wait_for_selector(
                "#seo-product-data > div[data-id]", timeout=15000
            )
        except Exception as e:
            return {"url": url, "products": [], "error": str(e)}

        products = await self.extract_products_from_page(page)
        pagination = await self.extract_pagination_info(page)

        return {
            "url": url,
            "products": products,
            "product_count": len(products),
            "pagination": pagination,
        }

    def extract_categories_from_html(self, html: str) -> dict:
        """
        Extract 3-level category hierarchy from mytek.tn frontpage.

        Structure:
        - Top-level (14): li.rootverticalnav.category-item
        - Low-level: .grid-item-6.clearfix
        - Subcategory: .level3-popup li.category-item
        """
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})

        categories = []

        # Top-level categories
        top_sel = fp.get("top_level_blocks", "li.rootverticalnav.category-item")
        top_blocks = tree.css(top_sel)

        self.logger.info(f"Found {len(top_blocks)} top-level categories")

        for top_block in top_blocks:
            # Get top-level title
            top_title = None
            for child in top_block.iter():
                if child.tag == "a":
                    text = child.text(strip=True)
                    if text and not text.startswith("javascript"):
                        top_title = text
                        break

            if not top_title:
                continue

            top_cat = {"name": top_title, "level": "top", "low_level_categories": []}

            # Low-level categories
            low_sel = fp.get("low_level_blocks", ".grid-item-6.clearfix")
            low_blocks = top_block.css(low_sel)

            for low_block in low_blocks:
                low_link_sel = fp.get("low_level_link", ".title_normal > a")
                low_link_node = low_block.css_first(low_link_sel)

                if not low_link_node:
                    continue

                low_title = low_link_node.text(strip=True)
                low_href = low_link_node.attributes.get("href") or ""

                if low_href.startswith("javascript:"):
                    continue

                low_cat = {
                    "name": low_title,
                    "url": low_href,
                    "level": "low",
                    "subcategories": [],
                }

                # Subcategories
                sub_sel = fp.get("subcategory_blocks", ".level3-popup li.category-item")
                sub_blocks = low_block.css(sub_sel)

                for sub_block in sub_blocks:
                    sub_link_node = sub_block.css_first(
                        "a.clearfix"
                    ) or sub_block.css_first("a")
                    if not sub_link_node:
                        continue

                    sub_title_sel = fp.get("subcategory_title", ".level3-name")
                    sub_title_node = sub_block.css_first(sub_title_sel)
                    sub_title = (
                        sub_title_node.text(strip=True)
                        if sub_title_node
                        else sub_link_node.text(strip=True)
                    )
                    sub_href = sub_link_node.attributes.get("href", "")

                    if sub_href and not sub_href.startswith("javascript:"):
                        low_cat["subcategories"].append(
                            {"name": sub_title, "url": sub_href, "level": "subcategory"}
                        )

                top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        # Calculate stats
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["top_level"] += 1
            for low in top.get("low_level_categories", []):
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
                for sub in low.get("subcategories", []):
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1

        return {"categories": categories, "stats": stats}

    async def extract_products_from_page(self, page: Page) -> List[dict]:
        """Extract products from mytek's SEO data layer.

        Mytek injects a hidden <div id="seo-product-data"> into every category
        page containing one child <div> per product with all fields stored as
        data-* attributes.  This is present in the raw server-rendered HTML
        before any JavaScript runs, making it fast and reliable.

        Attributes:
            data-id           → internal Magento product ID
            data-sku          → model / SKU string
            data-name         → product title
            data-url          → product page URL (relative or absolute)
            data-image        → main image path (relative or absolute)
            data-final-price  → current selling price (float string)
            data-price        → original / list price (float string)
            data-erpstock     → stock status text ("En stock", "Epuisé", …)
            data-manufacturer → brand name
        """
        products = await page.evaluate("""
            () => {
                const items = document.querySelectorAll('#seo-product-data > div[data-id]');
                const BASE  = 'https://www.mytek.tn';
                const out   = [];

                items.forEach(item => {
                    const id       = item.getAttribute('data-id');
                    const sku      = item.getAttribute('data-sku');
                    const name     = item.getAttribute('data-name');
                    const rawUrl   = item.getAttribute('data-url')   || '';
                    const rawImg   = item.getAttribute('data-image') || '';
                    const erpstock = item.getAttribute('data-erpstock')     || null;
                    const brand    = item.getAttribute('data-manufacturer') || null;

                    // data-final-price = current price, data-price = list/original price
                    const finalPrice    = parseFloat(item.getAttribute('data-final-price'));
                    const originalPrice = parseFloat(item.getAttribute('data-price'));

                    const price    = isNaN(finalPrice) ? null : finalPrice;
                    const oldPrice = (!isNaN(originalPrice) && !isNaN(finalPrice) && originalPrice > finalPrice)
                                     ? originalPrice : null;

                    // Build absolute URLs
                    const productUrl = rawUrl   ? (rawUrl.startsWith('http')   ? rawUrl   : BASE + rawUrl)   : null;
                    const imageUrl   = rawImg   ? (rawImg.startsWith('http')   ? rawImg   : BASE + rawImg)   : null;

                    // Map stock text to boolean
                    const sl = (erpstock || '').toLowerCase();
                    let available = null;
                    if (sl.includes('stock')) {
                        available = true;
                    } else if (sl.includes('puis') || sl.includes('indisponible') || sl.includes('rupture')) {
                        available = false;
                    }

                    if (id || productUrl) {
                        out.push({
                            id:           id,
                            sku:          sku,
                            url:          productUrl,
                            name:         name,
                            price:        price,
                            old_price:    oldPrice,
                            image:        imageUrl,
                            availability: erpstock,
                            available:    available,
                            brand:        brand,
                        });
                    }
                });

                return out;
            }
        """)

        return products

    async def extract_pagination_info(self, page: Page) -> dict:
        """Extract pagination from mytek category page.

        Detects the highest visible page number AND whether a next-page
        button is still active (not disabled).  The 'has_next' flag is
        used by scrape_category_all_pages to follow mytek's sliding
        pagination window correctly.

        HTML structure (Bootstrap pagination):
            <ul class="pagination justify-content-center">
              <li class="page-item disabled"><span class="page-link">‹</span></li>
              <li class="page-item active"><span class="page-link">1</span></li>
              <li class="page-item"><a class="page-link" href="...?p=2">2</a></li>
              ...
              <li class="page-item"><a class="page-link" href="...?p=2">›</a></li>
            </ul>
        The last <li> is the "next" arrow; it is .disabled on the last page.
        """
        pagination = await page.evaluate("""
            () => {
                // Current page from active item
                const activeEl = document.querySelector('.page-item.active .page-link');
                const currentPage = parseInt(activeEl?.innerText?.trim()) || 1;

                // Max visible page number — scan all page-link text values
                let maxPage = currentPage;
                const allLinks = document.querySelectorAll('.pagination .page-item .page-link');
                allLinks.forEach(el => {
                    const num = parseInt(el.innerText?.trim());
                    if (!isNaN(num) && num > maxPage) {
                        maxPage = num;
                    }
                });

                // has_next: the last .page-item must NOT be disabled
                // (when disabled it contains a <span>, not an <a>)
                const lastItem = document.querySelector('.pagination .page-item:last-child');
                const hasNext = lastItem
                    ? (!lastItem.classList.contains('disabled') && !!lastItem.querySelector('a.page-link'))
                    : false;

                return {
                    current_page: currentPage,
                    total_pages: maxPage,
                    has_next: hasNext
                };
            }
        """)

        return pagination

    async def scrape_category_all_pages(
        self,
        context: BrowserContext,
        category: CategoryInfo,
        stats: ScrapeStats,
    ) -> List[dict]:
        """Override to handle mytek's sliding pagination window.

        The base implementation reads total_pages once from the first page,
        then iterates up to that number.  Mytek's pagination widget shows
        only a window of ~4 page numbers at a time, so total_pages from
        page 1 would be at most 4 — even when 20 pages exist.

        This override keeps scraping the next page as long as has_next is
        True, stopping only when the next-arrow is disabled or the page
        returns no products.
        """
        all_products = []
        page = await context.new_page()
        consecutive_failures = 0
        page_num = 1

        try:
            while True:
                if consecutive_failures >= self.max_consecutive_failures:
                    self.logger.warning(
                        f"    Stopping {category.name}: "
                        f"{consecutive_failures} consecutive failures"
                    )
                    break

                url = (
                    category.url
                    if page_num == 1
                    else self.build_page_url(category.url, page_num)
                )

                # Fetch this page with retries
                result = None
                for attempt in range(1, self.retry_config.max_retries + 1):
                    try:
                        result = await self.scrape_single_page(page, url)
                        if not result.get("error"):
                            consecutive_failures = 0
                            break
                        raise Exception(result["error"])
                    except Exception as e:
                        consecutive_failures += 1
                        stats.retries_total += 1
                        if attempt < self.retry_config.max_retries:
                            delay = self.retry_config.get_delay(attempt)
                            self.logger.debug(
                                f"    Retry {attempt}: {e}. Waiting {delay:.1f}s"
                            )
                            await asyncio.sleep(delay)
                        else:
                            self.logger.warning(
                                f"    Page {page_num} of {category.name} failed "
                                f"after {self.retry_config.max_retries} retries"
                            )
                            return all_products

                if not result or result.get("error"):
                    break

                products = result.get("products", [])
                if not products:
                    # Empty page — we've gone past the last real page
                    break

                all_products.extend(products)
                stats.total_pages += 1

                # Decide whether to continue
                pagination = result.get("pagination", {})
                has_next = pagination.get("has_next", False)

                if not has_next:
                    break  # Next arrow is disabled — no more pages

                page_num += 1

        finally:
            await page.close()

        return all_products

    async def scrape_product_details(self, page: Page, product_url: str) -> dict:
        """
        Scrape detailed product information from a mytek.tn product page.

        Extracts:
        - title, sku, brand, overview/description
        - price, old_price, discount
        - specifications table
        - images, stock status, store availability
        """
        max_retries = 2
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                await page.goto(
                    product_url, wait_until="domcontentloaded", timeout=15000
                )

                # Single short wait for critical content to be present in DOM
                try:
                    await page.wait_for_selector(
                        '.page-title-wrapper h1, meta[itemprop="price"]', timeout=3000
                    )
                except:
                    pass

                break

            except Exception as e:
                last_error = str(e)
                if attempt < max_retries:
                    await page.wait_for_timeout(500)
                    continue
                else:
                    return {
                        "url": product_url,
                        "error": f"Page load failed after {max_retries + 1} attempts: {last_error}",
                        "title": None,
                        "price": None,
                        "availability": None,
                        "available": None,
                        "sku": None,
                        "brand": None,
                        "overview": None,
                        "specifications": {},
                        "images": [],
                        "store_availability": None,
                    }

        details = await page.evaluate("""() => {
            const data = {url: window.location.href};

            // 1. Title - page-title-wrapper
            const titleEl = document.querySelector('.page-title-wrapper h1 span.base');
            data.title = titleEl ? titleEl.textContent.trim() : null;

            // 2. Product ID from data attribute
            const priceBox = document.querySelector('[data-product-id]');
            data.product_id = priceBox ? priceBox.getAttribute('data-product-id') : null;

            // 3. SKU - product attribute sku
            const skuEl = document.querySelector('.product.attribute.sku .value');
            data.sku = skuEl ? skuEl.textContent.trim() : null;

            // 4. Overview/Description - try multiple selectors
            let overviewEl = document.querySelector('.product.attribute.overview .value');
            if (!overviewEl || !overviewEl.textContent.trim()) {
                // Try description selector
                overviewEl = document.querySelector('#description .product-description') ||
                            document.querySelector('.product-description');
            }
            if (!overviewEl || !overviewEl.textContent.trim()) {
                // Try general description areas
                overviewEl = document.querySelector('.product-details .description') ||
                            document.querySelector('[data-role="description"]') ||
                            document.querySelector('.tab-content .description');
            }
            data.overview = overviewEl ? overviewEl.textContent.trim() : null;

            // 5. Brand from logo (removed in_stock/stock_status - use store_availability instead)
            const brandImg = document.querySelector('.product-info-stock-sku a img');
            if (brandImg) {
                data.brand_logo = brandImg.getAttribute('src');
                data.brand = brandImg.getAttribute('alt') ||
                             brandImg.getAttribute('src').split('/').pop().replace('.jpg', '').replace('.png', '');
            }

            // 7. PRICES - IMPORTANT: Use meta[itemprop="price"] which is unique to main product
            // The page has multiple price-boxes (for similar products) but only ONE meta itemprop="price"
            // Structure: <span itemprop="offers"><meta itemprop="price" content="789">...</span>

            // PRIMARY: Use meta itemprop="price" (only 1 on page, always correct)
            const metaPrice = document.querySelector('meta[itemprop="price"]');
            if (metaPrice) {
                data.price = parseFloat(metaPrice.getAttribute('content')) || null;
            }

            // OLD PRICE: Find the container with itemprop="offers" and look for old-price sibling
            // The old-price is a sibling of special-price (which contains itemprop="offers")
            const offersContainer = document.querySelector('[itemprop="offers"]');
            if (offersContainer) {
                // Navigate up to find the price-box that contains both special-price and old-price
                let priceBox = offersContainer.closest('.price-box');
                if (priceBox) {
                    const oldPriceEl = priceBox.querySelector('.old-price [data-price-type="oldPrice"]');
                    if (oldPriceEl) {
                        const oldPrice = parseFloat(oldPriceEl.getAttribute('data-price-amount'));
                        if (oldPrice && oldPrice !== data.price) {
                            data.old_price = oldPrice;
                            data.discount_percent = Math.round((1 - data.price / oldPrice) * 100);
                        }
                    }
                }
            }

            // 8. Specifications - product info detailed (excluding DISPONIBILITÉ - use store_availability instead)
            const specsTable = document.querySelector('#product-attribute-specs-table');
            if (specsTable) {
                const specs = {};
                const rows = specsTable.querySelectorAll('tbody tr');
                rows.forEach(row => {
                    const label = row.querySelector('th.label');
                    const value = row.querySelector('td.data');
                    if (label && value) {
                        const labelText = label.textContent.trim();
                        // Skip DISPONIBILITÉ - we get this from store_availability
                        if (labelText.toUpperCase() !== 'DISPONIBILITÉ') {
                            specs[labelText] = value.textContent.trim();
                        }
                    }
                });
                data.specifications = specs;
            }

            // 9. Images - try multiple selectors to handle single and multi-image products
            const images = [];

            // First try: carousel images (for multi-image products)
            document.querySelectorAll('#gallery-container .carousel-item img').forEach(img => {
                const src = img.getAttribute('src');
                if (src && !src.includes('placeholder') && !images.includes(src)) {
                    images.push(src);
                }
            });

            // Second try: direct img with itemprop="image" (for single-image products)
            if (images.length === 0) {
                document.querySelectorAll('img[itemprop="image"]').forEach(img => {
                    const src = img.getAttribute('src');
                    if (src && !src.includes('placeholder') && !images.includes(src)) {
                        images.push(src);
                    }
                });
            }

            // Third try: any image in gallery-container
            if (images.length === 0) {
                document.querySelectorAll('#gallery-container img, .product-media-gallery img').forEach(img => {
                    const src = img.getAttribute('src');
                    if (src && !src.includes('placeholder') && !images.includes(src)) {
                        images.push(src);
                    }
                });
            }

            // Fourth try: main product image
            if (images.length === 0) {
                const mainImg = document.querySelector('.product.media img, .fotorama__stage img, .product-image-container img');
                if (mainImg) {
                    const src = mainImg.getAttribute('src');
                    if (src && !src.includes('placeholder')) {
                        images.push(src);
                    }
                }
            }

            data.images = images;

            // 10. Main availability - extract from stock div
            // Structure:
            //   <div class="stock available" itemprop="availability" href="https://schema.org/InStock" title="En stock"><span>En stock</span></div>
            //   <div class="stock unavailable" itemprop="availability" href="https://schema.org/OutOfStock" title="Epuisé"><span>Epuisé</span></div>
            //   <div class="stock unavailable_backorder" itemprop="availability" href="https://schema.org/BackOrder" title="En arrivage"><span>En arrivage</span></div>

            // Try multiple selectors to find the stock div
            let stockDiv = document.querySelector('[data-role="stockStatus"]');
            if (!stockDiv) {
                stockDiv = document.querySelector('.stock.available');
            }
            if (!stockDiv) {
                stockDiv = document.querySelector('.stock.unavailable');
            }
            if (!stockDiv) {
                stockDiv = document.querySelector('.stock.unavailable_backorder');
            }
            if (!stockDiv) {
                stockDiv = document.querySelector('[itemprop="availability"]');
            }

            if (stockDiv) {
                // Get text from span inside or directly from div - clean whitespace
                const spanEl = stockDiv.querySelector('span');
                let availabilityText = spanEl ? spanEl.textContent : stockDiv.textContent;
                // Clean up whitespace, newlines, and normalize
                data.availability = availabilityText ? availabilityText.replace(/\\s+/g, ' ').trim() : null;

                // Determine availability based on TEXT (most reliable)
                const textLower = (data.availability || '').toLowerCase();

                // Check if in stock based on text content
                const inStockKeywords = ['en stock', 'disponible', 'in stock'];
                const outOfStockKeywords = ['epuisé', 'épuisé', 'rupture', 'indisponible', 'out of stock', 'non disponible'];
                const backorderKeywords = ['arrivage', 'commande', 'backorder', 'sur commande'];

                const textIndicatesInStock = inStockKeywords.some(kw => textLower.includes(kw));
                const textIndicatesOutOfStock = outOfStockKeywords.some(kw => textLower.includes(kw));
                const textIndicatesBackorder = backorderKeywords.some(kw => textLower.includes(kw));

                // Also check classes as backup
                const hasAvailableClass = stockDiv.classList && stockDiv.classList.contains('available');
                const hasUnavailableClass = stockDiv.classList && stockDiv.classList.contains('unavailable');

                // Also check href for schema.org values as backup
                const href = stockDiv.getAttribute('href') || '';
                const hrefInStock = href.includes('InStock');
                const hrefOutOfStock = href.includes('OutOfStock');

                // Simplified availability determination - prioritize classes, then text
                if (hasAvailableClass && !hasUnavailableClass) {
                    data.available = true;
                } else if (hasUnavailableClass) {
                    data.available = false;
                } else {
                    // Fallback to text analysis
                    const hasInStockText = ['en stock', 'disponible', 'in stock', 'available'].some(kw => textLower.includes(kw));
                    const hasOutOfStockText = ['epuisé', 'épuisé', 'rupture', 'indisponible', 'out of stock'].some(kw => textLower.includes(kw));

                    if (hasInStockText && !hasOutOfStockText) {
                        data.available = true;
                    } else if (hasOutOfStockText) {
                        data.available = false;
                    } else if (['arrivage', 'commande', 'sur commande'].some(kw => textLower.includes(kw))) {
                        data.available = false;  // Backorder = not immediately available
                    } else if (data.availability && data.availability.trim()) {
                        // Default to available if we have some text
                        data.available = true;
                    } else {
                        data.available = null;
                    }
                }
            } else {
                data.availability = null;
                data.available = null;
            }

            // 11. Store availability - simplified extraction from .tab_retrait_mag table
            const storeAvailability = [];

            // Find the store availability table
            const table = document.querySelector('.tab_retrait_mag') ||
                         document.querySelector('table.tab_retrait_mag');

            if (table) {
                const tbody = table.querySelector('tbody');
                if (tbody) {
                    const rows = tbody.querySelectorAll('tr');

                    rows.forEach(row => {
                        const cells = row.querySelectorAll('td');
                        if (cells.length < 2) return;

                        let storeName = null;
                        let statusText = null;
                        let isAvailable = false;

                        const firstCell = cells[0];
                        const secondCell = cells[1];

                        // Extract store name
                        if (firstCell.hasAttribute('colspan')) {
                            // "Achat En Ligne" row
                            storeName = firstCell.textContent.trim() || 'Achat En Ligne';
                        } else if (firstCell.classList && firstCell.classList.contains('mag_name')) {
                            // Regular store row
                            const link = firstCell.querySelector('a');
                            storeName = link ? link.textContent.trim() : firstCell.textContent.trim();
                        } else {
                            // Fallback
                            storeName = firstCell.textContent.trim();
                        }

                        // Clean store name
                        storeName = storeName.replace(/[:;]+/g, '').trim();

                        // Extract status
                        const statusSpan = secondCell.querySelector('span');
                        if (statusSpan) {
                            statusText = statusSpan.textContent.trim();
                            const statusClass = statusSpan.className || '';
                            isAvailable = statusClass.includes('enStock') ||
                                        statusText.toLowerCase().includes('en stock') ||
                                        statusText.toLowerCase().includes('disponible');
                        } else {
                            statusText = secondCell.textContent.trim();
                            const textLower = statusText.toLowerCase();
                            isAvailable = textLower.includes('stock') || textLower.includes('disponible');
                        }

                        // Only add valid entries
                        if (storeName && statusText) {
                            storeAvailability.push({
                                store: storeName,
                                status: statusText,
                                available: isAvailable
                            });
                        }
                    });
                }
            }

            // Always set store_availability array (even if empty) to distinguish from null
            // But only if we found at least one store, otherwise null means "not found"
            if (storeAvailability.length > 0) {
                data.store_availability = storeAvailability;
            } else {
                data.store_availability = null;
            }

            return data;
        }""")

        return details


# Factory function to get scraper
def get_scraper(logger: logging.Logger) -> MytekScraper:
    return MytekScraper(logger)
