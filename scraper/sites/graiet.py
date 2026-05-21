#!/usr/bin/env python3
"""
Graiet.tn specific scraper implementation.
Hybrid: Playwright for all page fetches (site is behind Cloudflare TLS fingerprinting),
selectolax for HTML parsing.
"""
import asyncio
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


class GraietScraper(FastScraper):
    """Hybrid scraper for graiet.tn (behind Cloudflare, requires Playwright for all fetches)."""
    
    def __init__(self, logger: logging.Logger):
        super().__init__("graiet", logger)
        self._pw = None
        self._browser = None
        self._pw_context = None
        self._tor_slot = hash("graiet") % max(TorPool.get().size, 1)

    # ------------------------------------------------------------------
    # Shared Playwright browser (lazy init, reused across all fetches)
    # ------------------------------------------------------------------

    async def _ensure_browser(self):
        """Lazily start a shared Playwright browser with anti-detection."""
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
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"📥 Downloading (Playwright): {self.base_url}")

        await self._ensure_browser()
        page = await self._pw_context.new_page()
        try:
            await page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector("ul.horizontal-list", timeout=10000)
            except Exception:
                self.logger.warning("Menu selector not found, continuing")
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
    # Override: close browser when scraping finishes
    # ------------------------------------------------------------------

    async def run_full_scrape(self, category_limit=None, product_limit=None, detail_limit=None, on_result=None):
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
    
    def build_page_url(self, base_url: str, page_num: int) -> str:
        """Build paginated URL for Graiet."""
        cp = self.selectors.get("category_page", {})
        param = cp.get("pagination_param", "p")
        
        if "?" in base_url:
            return f"{base_url}&{param}={page_num}"
        return f"{base_url}?{param}={page_num}"
    
    def extract_categories_from_html(self, html: str) -> dict:
        """
        Extract category hierarchy from graiet.tn frontpage.
        
        Structure:
        - Top-level: ul.horizontal-list > li.ui-menu-item.level0
        - Subcategories: ul.subchildmenu > li.ui-menu-item.level1
        """
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        
        categories = []
        
        # Top-level categories
        top_sel = fp.get("top_level_blocks", "ul.horizontal-list > li.ui-menu-item.level0")
        top_blocks = tree.css(top_sel)
        
        self.logger.info(f"Found {len(top_blocks)} top-level categories")
        
        for top_block in top_blocks:
            # Get top-level name from a.level-top > span
            name_sel = fp.get("top_level_name", "a.level-top > span")
            name_node = top_block.css_first(name_sel)
            if not name_node:
                # Fallback to link text
                link_sel = fp.get("top_level_link", "a.level-top")
                link_node = top_block.css_first(link_sel)
                top_title = link_node.text(strip=True) if link_node else None
            else:
                top_title = name_node.text(strip=True)
            
            # Get top-level URL
            link_sel = fp.get("top_level_link", "a.level-top")
            link_node = top_block.css_first(link_sel)
            top_href = link_node.attributes.get("href", "") if link_node else ""
            
            if not top_title:
                continue
            
            # Skip javascript links
            if top_href.startswith("javascript:") or not top_href:
                top_href = None
            
            top_cat = {
                "name": top_title,
                "url": top_href,
                "level": "top",
                "low_level_categories": []
            }
            
            # Look for subcategories within this top-level block
            # Subcategories are in ul.subchildmenu > li.ui-menu-item.level1
            sub_sel = fp.get("subcategory_blocks", "ul.subchildmenu > li.ui-menu-item.level1")
            sub_blocks = top_block.css(sub_sel)
            
            if sub_blocks:
                # Create a low-level category wrapper for subcategories
                # Or we can put subcategories directly, depending on structure
                # Let's check if there's a grouping structure first
                # For now, create a low-level category with subcategories
                low_cat = {
                    "name": top_title,  # Use same name or extract from context
                    "url": top_href,  # Use same URL
                    "level": "low",
                    "subcategories": []
                }
                
                for sub_block in sub_blocks:
                    sub_link = sub_block.css_first("a")
                    if not sub_link:
                        continue
                    
                    sub_title = sub_link.text(strip=True)
                    sub_href = sub_link.attributes.get("href", "")
                    
                    if sub_href.startswith("javascript:"):
                        continue
                    
                    low_cat["subcategories"].append({
                        "name": sub_title,
                        "url": sub_href,
                        "level": "subcategory"
                    })
                
                if low_cat["subcategories"]:
                    top_cat["low_level_categories"].append(low_cat)
            
            categories.append(top_cat)
        
        # Calculate stats
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
        
        return {
            "categories": categories,
            "stats": stats
        }
    
    def _parse_price(self, text: str) -> Optional[float]:
        """Parse price text to float."""
        if not text:
            return None
        # Remove currency symbols, spaces, and convert comma to dot
        cleaned = re.sub(r'[^\d.,]', '', text).replace(',', '.')
        try:
            return float(cleaned)
        except ValueError:
            return None
    
    def _is_valid_image_url(self, url: str) -> bool:
        """Check if URL is a valid image URL (not placeholder/base64)."""
        if not url:
            return False
        # Filter out base64 data URLs
        if url.startswith("data:image"):
            return False
        # Filter out placeholder images
        if "placeholder" in url.lower():
            return False
        # Must be a proper URL
        if not (url.startswith("http://") or url.startswith("https://")):
            return False
        # Filter out 1x1 pixel placeholder images
        if "1x1" in url or "transparent" in url.lower():
            return False
        return True
    
    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from a Graiet category page HTML."""
        tree = HTMLParser(html)
        products = []
        
        cp = self.selectors.get("category_page", {})
        item_selector = cp.get("item_selector", "li.item.product.product-item")
        
        items = tree.css(item_selector)
        
        for item in items:
            try:
                # Product ID from div.product-item-info id attribute
                id_sel = cp.get("product_id_selector", "div.product-item-info")
                id_container = item.css_first(id_sel)
                product_id = None
                if id_container:
                    id_attr = cp.get("product_id_attr", "id")
                    product_id = id_container.attributes.get(id_attr)
                    # Clean ID if it has prefix like "product-item-info_"
                    if product_id and "_" in product_id:
                        product_id = product_id.split("_")[-1]
                
                # Product URL
                link_sel = cp.get("product_link", "a.product-item-link")
                link_node = item.css_first(link_sel)
                product_url = link_node.attributes.get("href", "") if link_node else None
                
                # Product name
                title_sel = cp.get("product_title", "a.product-item-link")
                title_node = item.css_first(title_sel)
                product_name = title_node.text(strip=True) if title_node else None
                
                # Price
                price_sel = cp.get("product_price", ".price-wrapper .price")
                price_node = item.css_first(price_sel)
                price = None
                if price_node:
                    price_text = price_node.text(strip=True)
                    price = self._parse_price(price_text)
                
                # Image - extract from product listing
                # Actual HTML: <img class="products-image-hover ls-is-cached lazyloaded" 
                #              data-src="..." src="..."> (src is populated after lazy load)
                image_url = None
                
                # Get selector from config (defaults to .products-image-hover)
                img_sel = cp.get("product_image", ".products-image-hover")
                
                # Try the configured selector first, then fallbacks
                image_selectors = [img_sel] if img_sel else []
                image_selectors.extend([
                    "img.products-image-hover",
                    ".products-image-hover",
                    ".product-image-photo",
                    "img.product-image-photo",
                    ".product-image-wrapper img",
                    ".product.photo img",
                    "a.product-item-link img",
                    "img"
                ])
                
                for selector in image_selectors:
                    img_node = item.css_first(selector)
                    if img_node:
                        # For lazyloaded images, src is populated after load, check src first
                        # then fallback to data-src
                        img_src = img_node.attributes.get("src")
                        if not img_src or not self._is_valid_image_url(img_src):
                            img_src = (img_node.attributes.get("data-src") or
                                      img_node.attributes.get("data-lazy-src") or
                                      img_node.attributes.get("data-original"))
                        
                        if img_src and self._is_valid_image_url(img_src):
                            image_url = img_src
                            break
                
                # If no image found, try looking for <picture> tag
                if not image_url:
                    picture = item.css_first("picture")
                    if picture:
                        # Try img inside picture
                        img_in_picture = picture.css_first("img")
                        if img_in_picture:
                            img_src = (img_in_picture.attributes.get("src") or 
                                      img_in_picture.attributes.get("data-src"))
                            if img_src and self._is_valid_image_url(img_src):
                                image_url = img_src
                        
                        # Try source tags if still no image
                        if not image_url:
                            for source in picture.css("source"):
                                srcset = source.attributes.get("srcset")
                                if srcset:
                                    # Take first URL from srcset
                                    first_url = srcset.split()[0] if " " in srcset else srcset
                                    if first_url and self._is_valid_image_url(first_url):
                                        image_url = first_url
                                        break
                
                if product_url:
                    products.append({
                        "id": product_id,
                        "url": product_url,
                        "name": product_name,
                        "price": price,
                        "image": image_url
                    })
            except Exception as e:
                self.logger.debug(f"Error extracting product from item: {e}")
                continue
        
        return products
    
    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination info from category page HTML."""
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        
        pagination = {
            "current_page": 1,
            "total_pages": 1
        }
        
        # Try to find current page
        current_sel = cp.get("pagination_current", "li.item.current")
        current_node = tree.css_first(current_sel)
        if current_node:
            current_text = current_node.text(strip=True)
            try:
                pagination["current_page"] = int(current_text)
            except ValueError:
                pass
        
        # Find all page links to determine max page
        links_sel = cp.get("pagination_links", "ul.pages-items li.item a.page")
        page_links = tree.css(links_sel)
        max_page = 1
        
        for link in page_links:
            page_text = link.text(strip=True)
            try:
                page_num = int(page_text)
                if page_num > max_page:
                    max_page = page_num
            except ValueError:
                pass
        
        pagination["total_pages"] = max_page if max_page > 1 else 1
        
        return pagination
    
    async def scrape_product_details(self, url: str) -> dict:
        """Scrape detailed product information from a Graiet product page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        
        tree = HTMLParser(html)
        data = {"url": url}
        
        pp = self.selectors.get("product_page", {})
        
        # Title
        title_sel = pp.get("title", "h1.page-title span.base")
        title_node = tree.css_first(title_sel)
        data["title"] = title_node.text(strip=True) if title_node else None
        
        # Product ID from URL or SKU form (if available)
        # Try to extract from URL first
        url_match = re.search(r'/(\d+)', url)
        if url_match:
            data["product_id"] = url_match.group(1)
        else:
            data["product_id"] = None
        
        # SKU
        sku_sel = pp.get("sku", "div.product.attribute.sku div.value")
        sku_node = tree.css_first(sku_sel)
        data["sku"] = sku_node.text(strip=True) if sku_node else None
        
        # Price
        price_sel = pp.get("price", "span[data-price-amount]")
        price_node = tree.css_first(price_sel)
        if price_node:
            price_attr = pp.get("price_attr", "data-price-amount")
            price_val = price_node.attributes.get(price_attr)
            if price_val:
                try:
                    data["price"] = float(price_val)
                except ValueError:
                    data["price"] = None
            else:
                # Fallback to text
                price_text = price_node.text(strip=True)
                data["price"] = self._parse_price(price_text)
        else:
            data["price"] = None
        
        # Old price (look for regular-price or special-price parent)
        # Magento usually has .old-price or .regular-price
        old_price_node = tree.css_first(".old-price, .regular-price")
        if old_price_node:
            old_price_text = old_price_node.text(strip=True)
            data["old_price"] = self._parse_price(old_price_text)
            if data["old_price"] and data.get("price"):
                try:
                    data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)
                except (TypeError, ZeroDivisionError):
                    pass
        else:
            data["old_price"] = None
        
        # Brand
        # Structure: div.product-page-brand-common-view > ul.product-brands > li.brand-item
        # There are two <a> tags: one wrapping <picture> (logo link) and one with text (brand name link)
        brand_container = tree.css_first("div.product-page-brand-common-view")
        if brand_container:
            brand_item = brand_container.css_first("li.brand-item")
            if brand_item:
                brand_name = None
                brand_url = None
                brand_logo = None
                
                # Find all <a> tags in the brand-item
                brand_links = brand_item.css("a")
                
                # The second <a> tag (with text) is the one we want for brand name
                # The first one wraps <picture> and has title attribute
                for link in brand_links:
                    # Check if this link wraps a picture
                    has_picture = link.css_first("picture") is not None
                    
                    if has_picture:
                        # This is the logo link - extract logo URL and use title for brand name
                        picture = link.css_first("picture")
                        if picture:
                            # Try img src first
                            img = picture.css_first("img")
                            if img:
                                brand_logo = img.attributes.get("src")
                            
                            # Fallback to source srcset (prefer PNG, then webp)
                            if not brand_logo:
                                png_source = picture.css_first("source[type='image/png']")
                                if png_source:
                                    brand_logo = png_source.attributes.get("srcset")
                            if not brand_logo:
                                webp_source = picture.css_first("source[type='image/webp']")
                                if webp_source:
                                    brand_logo = webp_source.attributes.get("srcset")
                        
                        # Get brand name from title attribute or alt of img
                        brand_name = link.attributes.get("title")
                        if not brand_name and img:
                            brand_name = img.attributes.get("alt")
                        
                        # Get URL from this link
                        brand_url = link.attributes.get("href")
                    else:
                        # This is the text link - get brand name from text content
                        link_text = link.text(strip=True)
                        if link_text and not brand_name:
                            brand_name = link_text
                        if not brand_url:
                            brand_url = link.attributes.get("href")
                
                # If we got brand_name from title but there's a text link, prefer text link
                # (text link is more reliable)
                for link in brand_links:
                    if link.css_first("picture") is None:
                        link_text = link.text(strip=True)
                        if link_text:
                            brand_name = link_text
                            break
                
                data["brand"] = brand_name
                data["brand_url"] = brand_url
                data["brand_logo"] = brand_logo
            else:
                data["brand"] = None
                data["brand_url"] = None
                data["brand_logo"] = None
        else:
            data["brand"] = None
            data["brand_url"] = None
            data["brand_logo"] = None
        
        # Overview/Description
        desc_sel = pp.get("description", "div.product.attribute.overview div.value")
        desc_node = tree.css_first(desc_sel)
        data["overview"] = desc_node.text(strip=True) if desc_node else None
        
        # Availability
        avail_sel = pp.get("availability", "div.stock.available, div.stock.unavailable")
        avail_node = tree.css_first(avail_sel)
        if avail_node:
            # Get text from span inside or directly
            span = avail_node.css_first("span")
            availability_text = span.text(strip=True) if span else avail_node.text(strip=True)
            data["availability"] = availability_text if availability_text else None
            
            # Check classes to determine available boolean
            classes = avail_node.attributes.get("class", "").lower()
            data["available"] = "available" in classes and "unavailable" not in classes
        else:
            data["availability"] = None
            data["available"] = None
        
        # Ensure consistency: if availability text exists but available is None, infer from text
        if data.get("availability") and data.get("available") is None:
            avail_text = str(data["availability"]).lower()
            if "en stock" in avail_text or "disponible" in avail_text or "in stock" in avail_text or "available" in avail_text:
                data["available"] = True
            elif "rupture" in avail_text or "epuisé" in avail_text or "indisponible" in avail_text or "unavailable" in avail_text or "hors stock" in avail_text:
                data["available"] = False
            elif "sur commande" in avail_text or "commande" in avail_text or "backorder" in avail_text:
                data["available"] = False  # On order = not immediately available
        
        # Store availability (Graiet might not have this, set to null for now)
        data["store_availability"] = None
        
        # Specifications
        specs = {}
        specs_table_sel = pp.get("specs_table", "table#product-attribute-specs-table tbody tr")
        specs_rows = tree.css(specs_table_sel)
        
        for row in specs_rows:
            name_sel = pp.get("specs_name", "th.col.label")
            value_sel = pp.get("specs_value", "td.col.data")
            
            name_node = row.css_first(name_sel)
            value_node = row.css_first(value_sel)
            
            if name_node and value_node:
                spec_name = name_node.text(strip=True)
                spec_value = value_node.text(strip=True)
                if spec_name and spec_value:
                    specs[spec_name] = spec_value
        
        data["specifications"] = specs if specs else None
        
        # Images - only save URLs (not downloading), filter out placeholders and base64 images
        # Actual HTML structure:
        # - Main: <meta itemprop="image" content="...">
        # - Gallery: .gallery-images .slick-slide .gallery-img > img.product-image-photo (with src and data-src)
        #   Also: .gallery-img div has data-src and data-bgset attributes
        # - Thumbnails: .p-thumb-nav .slick-slide .gallery-img > picture > source[srcset] or img[src]
        images = []
        
        # Main image from meta itemprop="image"
        main_img_sel = pp.get("main_image", "meta[itemprop='image']")
        main_img_node = tree.css_first(main_img_sel)
        if main_img_node:
            img_attr = pp.get("main_image_attr", "content")
            main_img_url = main_img_node.attributes.get(img_attr)
            if main_img_url and self._is_valid_image_url(main_img_url):
                images.append(main_img_url)
        
        # Gallery images from main gallery (.gallery-images)
        # Structure: .gallery-images .slick-slide .gallery-img > img.product-image-photo
        # The img has src (populated after lazy load) and data-src attributes
        # Also check parent .gallery-img div for data-src and data-bgset
        gallery_slides = tree.css(".gallery-images .slick-slide")
        for slide in gallery_slides:
            gallery_img_div = slide.css_first(".gallery-img")
            if gallery_img_div:
                # Check div's data-src or data-bgset first (most reliable)
                div_src = gallery_img_div.attributes.get("data-src") or gallery_img_div.attributes.get("data-bgset")
                if div_src and self._is_valid_image_url(div_src) and div_src not in images:
                    images.append(div_src)
                    continue
                
                # Fallback to img inside .gallery-img
                img = gallery_img_div.css_first("img.product-image-photo, img.product-image")
                if img:
                    # For lazyloaded images, src is populated, check src first
                    img_src = img.attributes.get("src")
                    if not img_src or not self._is_valid_image_url(img_src):
                        img_src = img.attributes.get("data-src")
                    
                    if img_src and self._is_valid_image_url(img_src) and img_src not in images:
                        images.append(img_src)
        
        # Thumbnail images from navigation (.p-thumb-nav)
        # Structure: .p-thumb-nav .slick-slide .gallery-img > picture > source[srcset] or img[src]
        thumb_slides = tree.css(".p-thumb-nav .slick-slide")
        for slide in thumb_slides:
            gallery_img_div = slide.css_first(".gallery-img")
            if gallery_img_div:
                # Check for <picture> tag first
                picture = gallery_img_div.css_first("picture")
                if picture:
                    # Try img inside picture first
                    img_in_picture = picture.css_first("img")
                    if img_in_picture:
                        pic_src = img_in_picture.attributes.get("src")
                        if pic_src and self._is_valid_image_url(pic_src) and pic_src not in images:
                            images.append(pic_src)
                            continue
                    
                    # Try source tags (prefer jpg/jpeg over webp for consistency)
                    sources = picture.css("source[type='image/jpg'], source[type='image/jpeg'], source[type='image/webp']")
                    for source in sources:
                        srcset = source.attributes.get("srcset")
                        if srcset:
                            # Take first URL from srcset
                            first_url = srcset.split()[0] if " " in srcset else srcset
                            if first_url and self._is_valid_image_url(first_url) and first_url not in images:
                                images.append(first_url)
                                break
                else:
                    # No picture tag, check for direct img
                    img = gallery_img_div.css_first("img")
                    if img:
                        img_src = img.attributes.get("src")
                        if img_src and self._is_valid_image_url(img_src) and img_src not in images:
                            images.append(img_src)
        
        # Fallback: try common gallery selectors if still no images
        if not images:
            for img in tree.css("img.product-image-photo, img.product-image, .gallery img, .product-media img"):
                src = (img.attributes.get("src") or 
                       img.attributes.get("data-src") or
                       img.attributes.get("data-lazy-src") or
                       img.attributes.get("data-original"))
                
                if src and self._is_valid_image_url(src) and src not in images:
                    images.append(src)
        
        data["images"] = images[:10] if images else None  # Limit to 10 images
        
        # Also set image (singular) from first image for consistency
        data["image"] = images[0] if images else None
        
        return data


# Factory function to get scraper
def get_scraper(logger: logging.Logger) -> GraietScraper:
    return GraietScraper(logger)
