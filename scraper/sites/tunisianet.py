#!/usr/bin/env python3
"""
TunisiaNet.com.tn specific scraper implementation.
Uses fast HTTP-based scraping (httpx + selectolax).
"""
import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser

from scraper.base import FastScraper


class TunisiaNetScraper(FastScraper):
    """Fast scraper for tunisianet.com.tn e-commerce site."""
    
    def __init__(self, logger: logging.Logger):
        super().__init__("tunisianet", logger)
    
    def build_page_url(self, base_url: str, page_num: int) -> str:
        """Build paginated URL for TunisiaNet."""
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"
    
    def extract_categories_from_html(self, html: str) -> dict:
        """
        Extract 3-level category hierarchy from tunisianet.com.tn frontpage.
        
        Structure:
        - Top-level (9): li.level-1.parent
        - Low-level (headers): .menu-item.item-header
        - Subcategory (lines): .menu-item.item-line (follow headers)
        """
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        
        categories = []
        
        # Top-level categories
        top_sel = fp.get("top_level_blocks", "li.level-1.parent")
        top_blocks = tree.css(top_sel)
        
        self.logger.info(f"Found {len(top_blocks)} top-level categories")
        
        for top_block in top_blocks:
            # Get top-level title from icon-drop-mobile > span (correct location)
            icon_div = top_block.css_first("div.icon-drop-mobile")
            if icon_div:
                span = icon_div.css_first("span")
                top_title = span.text(strip=True) if span else None
            else:
                # Fallback to link text
                top_link = top_block.css_first("a")
                top_title = top_link.text(strip=True) if top_link else None
            
            # Get URL from first link
            top_link = top_block.css_first("a")
            top_href = top_link.attributes.get("href", "") if top_link else ""
            
            if not top_title:
                continue
            
            top_cat = {
                "name": top_title,
                "url": top_href if top_href and not top_href.startswith("#") and not top_href.startswith("javascript:") else None,
                "level": "top",
                "low_level_categories": []
            }
            
            # Get all menu items within this top-level block (headers and lines)
            all_items = top_block.css(".menu-item")
            
            if not all_items:
                categories.append(top_cat)
                continue
            
            current_low_cat = None
            
            for item in all_items:
                classes = item.attributes.get("class", "")
                link = item.css_first("a")
                
                if not link:
                    continue
                
                item_title = link.text(strip=True)
                item_href = link.attributes.get("href", "")
                
                if not item_title or item_href.startswith("javascript:"):
                    continue
                
                if "item-header" in classes:
                    # This is a low-level category (header)
                    current_low_cat = {
                        "name": item_title,
                        "url": item_href,
                        "level": "low",
                        "subcategories": []
                    }
                    top_cat["low_level_categories"].append(current_low_cat)
                    
                elif "item-line" in classes and current_low_cat is not None:
                    # This is a subcategory (follows the current header)
                    current_low_cat["subcategories"].append({
                        "name": item_title,
                        "url": item_href,
                        "level": "subcategory"
                    })
            
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
        """Parse price text like '2 025,000 DT' to float 2025.0"""
        if not text:
            return None
        # Remove "DT", spaces, non-breaking spaces, and convert comma to dot
        cleaned = re.sub(r'[DT\s\u00a0]', '', text).replace(',', '.')
        try:
            return float(cleaned)
        except ValueError:
            return None
    
    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from a TunisiaNet category page HTML."""
        tree = HTMLParser(html)
        products = []
        
        cp = self.selectors.get("category_page", {})
        item_selector = cp.get("item_selector", ".item-product")
        
        for item in tree.css(item_selector):
            # Product ID from article element
            article = item.css_first("article[data-id-product]")
            product_id = article.attributes.get("data-id-product") if article else None
            
            # Product URL
            link = item.css_first("a.product-thumbnail")
            product_url = link.attributes.get("href") if link else None
            
            # Product title
            title_el = item.css_first(".product-title a")
            product_name = title_el.text(strip=True) if title_el else ""
            
            # Price from listing page
            # Structure: <span itemprop="price" class="price">2 025,000 DT</span>
            price_el = item.css_first(".product-price-and-shipping .price")
            price = self._parse_price(price_el.text(strip=True)) if price_el else None
            
            # Old price (if discounted)
            # Structure: <span class="regular-price">2 235,000 DT</span>
            old_price_el = item.css_first(".product-price-and-shipping .regular-price")
            old_price = self._parse_price(old_price_el.text(strip=True)) if old_price_el else None
            
            # Availability status
            # Structure: <div id="stock_availability"><span class="in-stock">En stock</span></div>
            # Note: in listing, it might be a different selector
            availability_el = item.css_first(".product-availability span") or item.css_first("[id*='stock'] span")
            availability = availability_el.text(strip=True) if availability_el else None
            available = availability_el is not None and "stock" in (availability_el.attributes.get("class", ""))

            # Image extraction
            image_url = None

            # Primary selector based on user's HTML structure
            img_el = item.css_first("a.thumbnail.product-thumbnail.first-img img.center-block.img-responsive, a.product-thumbnail img.center-block.img-responsive")
            if img_el:
                image_url = img_el.attributes.get('src') or img_el.attributes.get('data-src') or img_el.attributes.get('data-full-size-image-url')

            # Fallback to any image in the wb-image-block
            if not image_url:
                wb_block = item.css_first(".wb-image-block")
                if wb_block:
                    img_el = wb_block.css_first("img")
                    if img_el:
                        image_url = img_el.attributes.get('src') or img_el.attributes.get('data-src')

            # Clean up image URL if found
            if image_url:
                # Ensure it's a full URL
                if image_url.startswith('//'):
                    image_url = 'https:' + image_url
                elif image_url.startswith('/'):
                    image_url = 'https://www.tunisianet.com.tn' + image_url

                # Only include tunisianet.com.tn images
                if 'tunisianet.com.tn' in image_url:
                    # Optionally convert sizes (home, large, etc.) but keep as-is for now
                    pass

            if product_id or product_url:
                product_data = {
                    "id": product_id,
                    "url": product_url,
                    "name": product_name,
                    "price": price,
                    "old_price": old_price,
                    "availability": availability,
                    "available": available
                }

                # Add image if found
                if image_url:
                    product_data["image"] = image_url

                products.append(product_data)
        
        return products
    
    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from TunisiaNet category page HTML."""
        tree = HTMLParser(html)
        
        # Check for next page link
        next_link = tree.css_first("a.next")
        has_next = next_link is not None and "disabled" not in (next_link.attributes.get("class", ""))
        
        # Get page numbers
        max_page = 1
        current_page = 1
        
        for page_link in tree.css(".pagination a"):
            text = page_link.text(strip=True)
            try:
                num = int(text)
                if num > max_page:
                    max_page = num
                classes = page_link.attributes.get("class", "")
                if "current" in classes:
                    current_page = num
            except ValueError:
                pass
        
        # Check for active/current page
        active_page = tree.css_first(".pagination .current")
        if active_page:
            try:
                current_page = int(active_page.text(strip=True))
            except ValueError:
                pass
        
        return {
            "current_page": current_page,
            "total_pages": max_page,
            "has_next": has_next
        }
    
    async def scrape_product_details(self, url: str) -> dict:
        """Scrape detailed product information from a TunisiaNet product page."""
        html = await self.fetch_html(url)
        
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        
        tree = HTMLParser(html)
        data = {"url": url}
        
        # Title
        h1 = tree.css_first("h1.h1") or tree.css_first('h1[itemprop="name"]')
        data["title"] = h1.text(strip=True) if h1 else None
        
        # Product ID from URL
        url_match = re.search(r'/(\d+)-', url)
        data["product_id"] = url_match.group(1) if url_match else None
        
        # SKU/Reference
        ref_el = tree.css_first(".product-reference span")
        data["sku"] = ref_el.text(strip=True) if ref_el else None
        
        # Brand
        brand_img = tree.css_first(".product-manufacturer img")
        if brand_img:
            data["brand"] = brand_img.attributes.get("alt") or brand_img.attributes.get("title")
            data["brand_logo"] = brand_img.attributes.get("src")
        else:
            data["brand"] = None
            data["brand_logo"] = None
        
        # Price
        price_el = tree.css_first('[itemprop="price"]')
        data["price"] = float(price_el.attributes.get("content", 0)) if price_el else None
        
        # Old price (if discounted)
        old_price_el = tree.css_first(".regular-price")
        if old_price_el:
            old_price_text = re.sub(r'[^\d.,]', '', old_price_el.text()).replace(',', '.')
            try:
                data["old_price"] = float(old_price_text) if old_price_text else None
                if data["old_price"] and data["price"]:
                    data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)
            except ValueError:
                data["old_price"] = None
        else:
            data["old_price"] = None
        
        # Overview/Short description - selector: #product-description-short-{product_id}
        if data.get("product_id"):
            overview_el = tree.css_first(f"#product-description-short-{data['product_id']}")
            data["overview"] = overview_el.text(strip=True) if overview_el else None
        else:
            data["overview"] = None
        
        # Description (full)
        desc_el = tree.css_first("#description .product-description")
        data["description"] = desc_el.text(strip=True) if desc_el else None
        
        # Main availability - extract from #stock_availability
        # Structure: 
        #   <div id="stock_availability">Disponibilté : <span class="in-stock">En stock</span></div>
        #   <div id="stock_availability">Disponibilté : <span class="later-stock">Sur commande</span></div>
        #   <div id="stock_availability">Disponibilté : <span class="out-of-stock">Hors stock</span></div>
        stock_availability_div = tree.css_first("#stock_availability")
        if stock_availability_div:
            # Find any span inside (could be in-stock, later-stock, out-of-stock, etc.)
            availability_span = stock_availability_div.css_first("span")
            if availability_span:
                data["availability"] = availability_span.text(strip=True)
                # Get classes to determine availability status
                classes = availability_span.attributes.get("class", "").lower()
                # Only mark as available if it's actually in stock
                # "in-stock" = available, "later-stock" = on order, "out-of-stock"/"hors-stock" = not available
                data["available"] = "in-stock" in classes and "out" not in classes and "hors" not in classes
            else:
                # Fallback: get text from div
                text = stock_availability_div.text(strip=True)
                if text:
                    # Extract status from text like "Disponibilté : En stock"
                    if ":" in text:
                        availability_text = text.split(":", 1)[1].strip()
                    else:
                        availability_text = text
                    data["availability"] = availability_text
                    # Check availability based on text content
                    text_lower = availability_text.lower()
                    # Only mark as available if explicitly "en stock" and not "sur commande" or "hors stock"
                    data["available"] = (
                        ("en stock" in text_lower or "in stock" in text_lower) and
                        "sur commande" not in text_lower and
                        "hors stock" not in text_lower and
                        "out of stock" not in text_lower
                    )
                else:
                    data["availability"] = None
                    data["available"] = None
        else:
            data["availability"] = None
            data["available"] = None
        
        # Ensure consistency: if availability text exists but available is None, infer from text
        if data.get("availability") and data.get("available") is None:
            avail_text = str(data["availability"]).lower()
            if "en stock" in avail_text or "in stock" in avail_text or "disponible" in avail_text:
                data["available"] = True
            elif "epuisé" in avail_text or "rupture" in avail_text or "hors stock" in avail_text or "out of stock" in avail_text or "indisponible" in avail_text:
                data["available"] = False
            elif "sur commande" in avail_text or "commande" in avail_text:
                data["available"] = False  # On order = not immediately available
        
        # Store availability - extract from #product-availability-store-mobile
        # Structure: first .stores div has store names, second has availability status
        store_availability = []
        store_container = tree.css_first("#product-availability-store-mobile")
        if store_container:
            stores_divs = store_container.css(".stores")
            if len(stores_divs) >= 2:
                # First div has store names (skip "Magasin" label)
                store_names = [el.text(strip=True) for el in stores_divs[0].css(".store-availability")]
                # Second div has availability status (skip "Disponibilité" label)
                store_statuses = stores_divs[1].css(".store-availability")
                
                for i, name in enumerate(store_names):
                    if i < len(store_statuses):
                        status_el = store_statuses[i]
                        status_text = status_el.text(strip=True)
                        # Check if "stock" class is present (indicates available)
                        is_available = "stock" in status_el.attributes.get("class", "")
                        store_availability.append({
                            "store": name,
                            "status": status_text,
                            "available": is_available
                        })
        
        data["store_availability"] = store_availability if store_availability else None
        
        # Specifications from data-sheet
        specs = {}
        data_sheet = tree.css_first(".product-features") or tree.css_first(".data-sheet")
        if data_sheet:
            for dt in data_sheet.css("dt.name"):
                dd = dt.next
                # Find the next dd.value sibling
                while dd and dd.tag != "dd":
                    dd = dd.next
                if dd and "value" in (dd.attributes.get("class", "")):
                    specs[dt.text(strip=True)] = dd.text(strip=True)
        data["specifications"] = specs
        
        # Images
        images = []
        main_img = tree.css_first(".product-cover img")
        if main_img:
            src = main_img.attributes.get("src") or main_img.attributes.get("data-src")
            if src:
                images.append(src)
        
        for img in tree.css(".product-images img, .images-container img, .thumb-container img"):
            src = img.attributes.get("src") or img.attributes.get("data-src")
            if src and src not in images:
                images.append(src)
        
        data["images"] = images[:10]
        
        return data


# Factory function to get scraper
def get_scraper(logger: logging.Logger) -> TunisiaNetScraper:
    return TunisiaNetScraper(logger)
