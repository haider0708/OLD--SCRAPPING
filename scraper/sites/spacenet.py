#!/usr/bin/env python3
"""
SpaceNet.tn specific scraper implementation.
Uses fast HTTP-based scraping (httpx + selectolax).
"""
import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser

from scraper.base import FastScraper, save_text_atomic


class SpaceNetScraper(FastScraper):
    """Fast scraper for spacenet.tn e-commerce site."""
    
    def __init__(self, logger: logging.Logger):
        super().__init__("spacenet", logger)

    async def download_frontpage(self):
        """Use Playwright for JS-rendered mega menu."""
        output_path = self.html_dir / "frontpage.html"
        from playwright.async_api import async_playwright
        from scraper.base import get_playwright_proxy, playwright_launch_args

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=playwright_launch_args())
            page = await browser.new_page(
                proxy=get_playwright_proxy(self.site_name, self.config.get("settings", {}))
            )
            try:
                await page.goto(self.base_url, wait_until="networkidle", timeout=45000)
                try:
                    await page.wait_for_selector("a[href*='spacenet.tn/']", timeout=8000)
                except Exception:
                    pass
                html = await page.content()
            finally:
                await page.close()
                await browser.close()
        save_text_atomic(html, output_path, self.logger)
        return output_path
    
    def build_page_url(self, base_url: str, page_num: int) -> str:
        """Build paginated URL for SpaceNet."""
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"
    
    def _clean_text(self, text: str) -> str:
        """Clean text by removing extra whitespace and newlines."""
        if not text:
            return ""
        return re.sub(r'\s+', ' ', text).strip()

    def _parse_price(self, text: str) -> Optional[float]:
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
    
    def _make_absolute_url(self, url: str) -> str:
        """Convert relative URL to absolute."""
        if not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("/"):
            return f"{self.base_url}{url}"
        return f"{self.base_url}/{url}"
    
    def extract_categories_from_html(self, html: str) -> dict:
        """
        Extract 3-level category hierarchy from spacenet.tn frontpage.
        """
        tree = HTMLParser(html)
        categories = []
        
        # Get menu container
        menu_container = tree.css_first("#desktop-header #sp-vermegamenu > ul")
        if not menu_container:
            menu_container = tree.css_first("#sp-vermegamenu > ul")
        
        if not menu_container:
            self.logger.warning("Could not find menu container, using fallback link extraction")
            return self._extract_categories_fallback(tree)
        
        # Get direct li children
        top_blocks = [n for n in menu_container.iter() if n.tag == 'li' and n.parent == menu_container]
        
        self.logger.info(f"Found {len(top_blocks)} top-level categories")
        
        for top_block in top_blocks:
            top_link = top_block.css_first("a")
            if not top_link:
                continue
            
            span = top_link.css_first("span")
            top_title = self._clean_text(span.text(strip=True) if span else top_link.text(strip=True))
            top_href = top_link.attributes.get("href", "")
            
            if not top_title:
                continue
            
            if top_href.startswith("javascript:") or top_href == "#":
                top_href = None
            else:
                top_href = self._make_absolute_url(top_href)
            
            top_cat = {
                "name": top_title,
                "url": top_href,
                "level": "top",
                "low_level_categories": []
            }
            
            # Get low-level categories
            low_container = top_block.css_first("div > ul")
            if low_container:
                low_blocks = [n for n in low_container.iter() if n.tag == 'li' and n.parent == low_container]
                
                for low_block in low_blocks:
                    low_link = low_block.css_first("a")
                    if not low_link:
                        continue
                    
                    low_span = low_link.css_first("span")
                    low_title = self._clean_text(low_span.text(strip=True) if low_span else low_link.text(strip=True))
                    low_href = low_link.attributes.get("href", "")
                    
                    if not low_title or low_href.startswith("javascript:"):
                        continue
                    
                    low_cat = {
                        "name": low_title,
                        "url": self._make_absolute_url(low_href) if low_href and low_href != "#" else None,
                        "level": "low",
                        "subcategories": []
                    }
                    
                    # Get subcategories
                    sub_container = low_block.css_first("div > ul")
                    if sub_container:
                        sub_blocks = [n for n in sub_container.iter() if n.tag == 'li' and n.parent == sub_container]
                        
                        for sub_block in sub_blocks:
                            sub_link = sub_block.css_first("a")
                            if not sub_link:
                                continue
                            
                            sub_span = sub_link.css_first("span")
                            sub_title = self._clean_text(sub_span.text(strip=True) if sub_span else sub_link.text(strip=True))
                            sub_href = sub_link.attributes.get("href", "")
                            
                            if not sub_title or sub_href.startswith("javascript:"):
                                continue
                            
                            low_cat["subcategories"].append({
                                "name": sub_title,
                                "url": self._make_absolute_url(sub_href) if sub_href and sub_href != "#" else None,
                                "level": "subcategory"
                            })
                    
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

    def _extract_categories_fallback(self, tree: HTMLParser) -> dict:
        """Fallback category extraction using stable category-like links."""
        seen = set()
        low_level_categories = []
        for link in tree.css("a[href]"):
            href = (link.attributes.get("href") or "").strip()
            name = self._clean_text(link.text(strip=True))
            if not href or not name:
                continue
            if "spacenet.tn" not in href:
                continue
            if re.search(r"/\d+-[a-z0-9-]+", href, re.I) is None and re.search(
                r"/[a-z0-9-]{4,}", href, re.I
            ) is None:
                continue
            if any(
                bad in href.lower()
                for bad in ("/product/", "/panier", "/cart", "/account", "/login")
            ):
                continue
            if href in seen:
                continue
            seen.add(href)
            low_level_categories.append(
                {
                    "name": name,
                    "url": href,
                    "level": "low",
                    "subcategories": [],
                }
            )

        categories = []
        if low_level_categories:
            categories.append(
                {
                    "name": "Catalog",
                    "url": None,
                    "level": "top",
                    "low_level_categories": low_level_categories,
                }
            )

        stats = {
            "top_level": len(categories),
            "low_level": len(low_level_categories),
            "subcategory": 0,
            "total_urls": len(low_level_categories),
        }
        return {"categories": categories, "stats": stats}
    
    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from a SpaceNet category page HTML."""
        tree = HTMLParser(html)
        products = []
        seen_ids = set()
        
        # SpaceNet has both desktop (#box-product-grid) and mobile (#box-product-list) views
        # Only extract from desktop grid to avoid duplicates
        grid_container = tree.css_first("#box-product-grid")
        if not grid_container:
            # Fallback to any product container
            grid_container = tree
        
        for item in grid_container.css(".js-product-miniature, .product-miniature"):
            product_id = item.attributes.get("data-id-product")
            
            # Skip duplicates
            if product_id and product_id in seen_ids:
                continue
            if product_id:
                seen_ids.add(product_id)
            
            link = item.css_first("a.thumbnail.product-thumbnail, a.product-thumbnail")
            product_url = self._make_absolute_url(link.attributes.get("href")) if link else None
            
            title_el = item.css_first("h2.product_name a, .product_name a")
            product_name = title_el.text(strip=True) if title_el else ""
            
            if product_id or product_url:
                product_data = {
                    "id": product_id,
                    "url": product_url,
                    "name": product_name
                }
                
                # Price - from .product-price-and-shipping .price
                price_el = item.css_first(".product-price-and-shipping .price")
                product_data["price"] = self._parse_price(price_el.text() if price_el else None)
                
                # Old price - from .product-price-and-shipping .regular-price
                old_price_el = item.css_first(".product-price-and-shipping .regular-price")
                product_data["old_price"] = self._parse_price(
                    old_price_el.text() if old_price_el else None
                )
                if product_data.get("old_price") and product_data.get("price"):
                    product_data["discount_percent"] = round(
                        (1 - product_data["price"] / product_data["old_price"]) * 100
                    )
                
                # Availability - from .product-quantities .label
                availability_el = item.css_first(".product-quantities .label")
                if availability_el:
                    availability_text = availability_el.text(strip=True)
                    product_data["availability"] = availability_text
                    product_data["available"] = "stock" in availability_text.lower() and "rupture" not in availability_text.lower()

                # Image - from .left-product .thumbnail .cover_image img
                image_url = None

                # Primary selector based on user's HTML structure
                img_el = item.css_first(".left-product .thumbnail.product-thumbnail .cover_image img, .thumbnail.product-thumbnail .cover_image img")
                if img_el:
                    image_url = img_el.attributes.get('src') or img_el.attributes.get('data-src') or img_el.attributes.get('data-lazy-src')

                # Fallback to other image selectors
                if not image_url:
                    img_el = item.css_first("img.img-responsive, .product_image")
                    if img_el:
                        image_url = img_el.attributes.get('src') or img_el.attributes.get('data-src') or img_el.attributes.get('data-lazy-src')

                # Clean up image URL if found
                if image_url:
                    # Ensure it's a full URL
                    if image_url.startswith('//'):
                        image_url = 'https:' + image_url
                    elif image_url.startswith('/'):
                        image_url = 'https://www.spacenet.tn' + image_url

                    # Only include spacenet.tn images
                    if 'spacenet.tn' in image_url:
                        product_data["image"] = image_url

                products.append(product_data)
        
        return products
    
    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from SpaceNet category page HTML."""
        tree = HTMLParser(html)
        
        # Check for next page link
        next_link = tree.css_first("a.next.js-search-link")
        has_next = next_link is not None and "disabled" not in (next_link.attributes.get("class", "") or "")
        
        max_page = 1
        current_page = 1
        
        for page_link in tree.css(".page-list li a.js-search-link"):
            try:
                num = int(page_link.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        
        current_el = tree.css_first(".page-list li.current a")
        if current_el:
            try:
                current_page = int(current_el.text(strip=True))
            except ValueError:
                pass
        
        return {
            "current_page": current_page,
            "total_pages": max_page,
            "has_next": has_next
        }
    
    async def scrape_product_details(self, url: str) -> dict:
        """Scrape detailed product information from a SpaceNet product page."""
        html = await self.fetch_html(url)
        
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        
        tree = HTMLParser(html)
        data = {"url": url}
        
        # Title
        right_section = tree.css_first('#main .product_right, .product_right')
        h1 = right_section.css_first('h1') if right_section else None
        if not h1:
            h1 = tree.css_first('h1.h1, h1[itemprop="name"]')
        data["title"] = h1.text(strip=True) if h1 else None
        
        # Product ID from URL
        url_match = re.search(r'/(\d+)-', url)
        data["product_id"] = url_match.group(1) if url_match else None
        
        # SKU/Reference
        ref_el = tree.css_first(".product-reference span")
        data["sku"] = ref_el.text(strip=True) if ref_el else None
        
        # Brand
        brand_img = tree.css_first(".product-manufacturer img")
        brand_text = tree.css_first(".product-manufacturer")
        data["brand"] = brand_img.attributes.get("alt") if brand_img else (brand_text.text(strip=True) if brand_text else None)
        data["brand_logo"] = brand_img.attributes.get("src") if brand_img else None
        
        # Price
        # Get selector from config or fallback
        price_selector = self.selectors.get("product_page", {}).get("price", '.current-price span[content], .current-price .price, [itemprop="price"], .product-price')
        
        price_el = right_section.css_first(price_selector) if right_section else tree.css_first(price_selector)
        if price_el:
            price_content = price_el.attributes.get("content")
            if price_content:
                try:
                    data["price"] = float(price_content)
                except ValueError:
                    data["price"] = None
            else:
                price_text = re.sub(r'[^\d.,]', '', price_el.text()).replace(',', '.')
                try:
                    data["price"] = float(price_text) if price_text else None
                except ValueError:
                    data["price"] = None
        else:
            data["price"] = None
        
        # Old price
        # Old price
        old_price_selector = self.selectors.get("product_page", {}).get("old_price", ".regular-price, .old-price")
        old_price_el = right_section.css_first(old_price_selector) if right_section else tree.css_first(old_price_selector)
        
        if old_price_el:
            old_text = re.sub(r'[^\d.,]', '', old_price_el.text()).replace(',', '.')
            try:
                data["old_price"] = float(old_text) if old_text else None
                if data["old_price"] and data["price"]:
                    data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)
            except ValueError:
                data["old_price"] = None
        else:
            data["old_price"] = None
        
        # Store availability - extract from .social-sharing-magasin
        # Structure: .table-bloc with .left-side (store name) and .right-side (status)
        store_availability = []
        store_container = tree.css_first(".social-sharing-magasin .magasin-table")
        if store_container:
            for bloc in store_container.css(".table-bloc"):
                store_name_el = bloc.css_first(".left-side span")
                status_el = bloc.css_first(".right-side span")
                
                if store_name_el:
                    store_name = store_name_el.text(strip=True)
                    
                    # Get status text (remove icon text)
                    if status_el:
                        status_text = status_el.text(strip=True)
                        # Check for fa-check (available) or fa-times (not available)
                        has_check = status_el.css_first(".fa-check") is not None
                        is_available = has_check or "disponible" in status_text.lower()
                    else:
                        status_text = None
                        is_available = False
                    
                    store_availability.append({
                        "store": store_name,
                        "status": status_text,
                        "available": is_available
                    })
        
        data["store_availability"] = store_availability if store_availability else None
        
        # Main availability - based on stores (available if ANY store has "Disponible")
        if store_availability:
            any_available = any(s.get("available") for s in store_availability)
            if any_available:
                data["availability"] = "Disponible"
                data["available"] = True
            else:
                # Check if all are "Sur commande" or "Rupture"
                statuses = [s.get("status", "").lower() for s in store_availability]
                if all("commande" in s for s in statuses if s):
                    data["availability"] = "Sur commande"
                    data["available"] = False
                else:
                    data["availability"] = "Rupture de stock"
                    data["available"] = False
        else:
            # Fallback to product-availability element
            availability_el = tree.css_first("#product-availability span, #product-availability")
            if availability_el:
                data["availability"] = availability_el.text(strip=True)
                avail_text = availability_el.text().lower()
                data["available"] = "rupture" not in avail_text and "indisponible" not in avail_text
            else:
                data["availability"] = None
                data["available"] = None
        
        # Ensure consistency: if availability text exists but available is None, infer from text
        if data.get("availability") and data.get("available") is None:
            avail_text = str(data["availability"]).lower()
            if "disponible" in avail_text or "en stock" in avail_text or "in stock" in avail_text:
                data["available"] = True
            elif "rupture" in avail_text or "epuisé" in avail_text or "indisponible" in avail_text or "hors stock" in avail_text:
                data["available"] = False
            elif "sur commande" in avail_text or "commande" in avail_text:
                data["available"] = False  # On order = not immediately available
        
        # Overview - try description elements
        overview_el = (tree.css_first(".product-description-short") or 
                       tree.css_first("#product-description-short") or
                       tree.css_first("#description .product-description") or
                       tree.css_first(".product-description"))
        data["overview"] = overview_el.text(strip=True) if overview_el else None
        
        # Specifications
        specs = {}
        features_section = tree.css_first("#product-details > section, .product-features, .data-sheet")
        if features_section:
            # Try dl > dt/dd pattern
            for dt in features_section.css("dt"):
                dd = dt.next
                while dd and dd.tag != "dd":
                    dd = dd.next
                if dd:
                    key = dt.text(strip=True)
                    value = dd.text(strip=True)
                    if key and value:
                        specs[key] = value
            
            # Try table rows pattern
            for row in features_section.css("tr"):
                cells = row.css("td, th")
                if len(cells) >= 2:
                    key = cells[0].text(strip=True)
                    value = cells[1].text(strip=True)
                    if key and value:
                        specs[key] = value
        
        data["specifications"] = specs
        
        # Images
        images = []
        left_section = tree.css_first("#main .product_left, .product_left")
        
        main_img = left_section.css_first("img") if left_section else tree.css_first(".product-cover img")
        if main_img:
            src = main_img.attributes.get("data-image-large-src") or main_img.attributes.get("data-src") or main_img.attributes.get("src")
            if src:
                images.append(src)
        
        for img in tree.css(".product-images img, .js-thumb img, .thumb-container img"):
            src = img.attributes.get("data-image-large-src") or img.attributes.get("data-src") or img.attributes.get("src")
            if src and src not in images:
                images.append(src)
        
        data["images"] = images[:10]
        
        return data


def get_scraper(logger: logging.Logger) -> SpaceNetScraper:
    """Factory function to get scraper."""
    return SpaceNetScraper(logger)
