#!/usr/bin/env python3
"""
Jumbo.tn specific scraper implementation.
Uses fast HTTP-based scraping (httpx + selectolax).
"""
import logging
import re
import json
from typing import List, Optional
from selectolax.parser import HTMLParser

from scraper.base import FastScraper


class JumboScraper(FastScraper):
    """Fast scraper for jumbo.tn e-commerce site."""
    
    def __init__(self, logger: logging.Logger):
        super().__init__("jumbo", logger)
    
    def build_page_url(self, base_url: str, page_num: int) -> str:
        """Build paginated URL for Jumbo."""
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"
    
    def extract_categories_from_html(self, html: str) -> dict:
        """
        Extract 3-level category hierarchy from jumbo.tn frontpage.
        
        Structure:
        - Top-level: li.mm_tabs_li with a.ets_mm_url inside .mm_tab_toggle_title
        - Low-level (headers): .ets_mm_block span.h4
        - Subcategory: ul.ets_mm_categories > li > a.ets_mm_url
        """
        tree = HTMLParser(html)
        
        categories = []
        
        # Top-level categories - use desktop menu only (#header_menu)
        top_blocks = tree.css("#header_menu li.mm_tabs_li.mm_tabs_has_content")
        
        self.logger.info(f"Found {len(top_blocks)} top-level category blocks")
        
        for top_block in top_blocks:
            # Get top-level category name and URL from .mm_tab_toggle_title > a
            top_link = top_block.css_first(".mm_tab_toggle_title a.ets_mm_url")
            
            if not top_link:
                continue
            
            top_title = top_link.text(strip=True)
            top_href = top_link.attributes.get("href", "")
            
            if not top_title or top_href == "#":
                continue
            
            top_cat = {
                "name": top_title,
                "url": top_href if top_href and not top_href.startswith("javascript:") else None,
                "level": "top",
                "low_level_categories": []
            }
            
            # Low-level categories (blocks with headers)
            low_blocks = top_block.css(".ets_mm_block.mm_block_type_category")
            
            for low_block in low_blocks:
                # Get header (low-level category name)
                header = low_block.css_first("span.h4")
                if not header:
                    continue
                
                # Header may contain a link or just text
                header_link = header.css_first("a")
                if header_link:
                    low_title = header_link.text(strip=True)
                    low_href = header_link.attributes.get("href", "")
                else:
                    low_title = header.text(strip=True)
                    low_href = ""
                
                if not low_title:
                    continue
                
                low_cat = {
                    "name": low_title,
                    "url": low_href if low_href and low_href != "#" and not low_href.startswith("javascript:") else None,
                    "level": "low",
                    "subcategories": []
                }
                
                # Subcategories - direct li > a inside ul.ets_mm_categories
                cat_list = low_block.css_first("ul.ets_mm_categories")
                if cat_list:
                    for li in cat_list.iter():
                        if li.tag == 'li' and li.parent == cat_list:
                            sub_link = li.css_first("a.ets_mm_url")
                            if sub_link:
                                sub_title = sub_link.text(strip=True)
                                sub_href = sub_link.attributes.get("href", "")
                                
                                if sub_title and sub_href and sub_href != "#":
                                    low_cat["subcategories"].append({
                                        "name": sub_title,
                                        "url": sub_href,
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
    
    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from a Jumbo category page HTML."""
        tree = HTMLParser(html)
        products = []
        
        # Use article[data-id-product] to avoid duplicates
        for item in tree.css("article[data-id-product]"):
            product_id = item.attributes.get("data-id-product")
            
            # Product URL from any link
            link = item.css_first('a[href*=".html"]')
            product_url = link.attributes.get("href") if link else None
            
            # Product name from h3
            title_el = item.css_first("h3 a") or item.css_first("h3")
            product_name = title_el.text(strip=True) if title_el else ""
            
            if product_id or product_url:
                product_data = {
                    "id": product_id,
                    "url": product_url,
                    "name": product_name
                }
                
                # Price - from .ce-product-price span
                price_el = item.css_first(".ce-product-price span")
                if price_el:
                    price_text = re.sub(r'[^\d.,]', '', price_el.text()).replace(',', '.')
                    try:
                        product_data["price"] = float(price_text) if price_text else None
                    except ValueError:
                        product_data["price"] = None
                
                # Old price - from .ce-product-price-regular
                old_price_el = item.css_first(".ce-product-price-regular")
                if old_price_el:
                    old_price_text = re.sub(r'[^\d.,]', '', old_price_el.text()).replace(',', '.')
                    try:
                        product_data["old_price"] = float(old_price_text) if old_price_text else None
                        # Calculate discount if both prices exist
                        if product_data.get("old_price") and product_data.get("price"):
                            product_data["discount_percent"] = round((1 - product_data["price"] / product_data["old_price"]) * 100)
                    except ValueError:
                        product_data["old_price"] = None
                
                # Availability - from .ce-product-stock__availability-label
                availability_el = item.css_first(".ce-product-stock__availability-label")
                if availability_el:
                    availability_text = availability_el.text(strip=True)
                    product_data["availability"] = availability_text
                    product_data["available"] = "disponible" in availability_text.lower() or "stock" in availability_text.lower()

                # Image - try multiple selectors for product images
                image_url = None

                # Try elementor widget image (as suggested by user)
                img_el = item.css_first(".elementor-widget-image img, .elementor-widget-product-miniature-image img")
                if img_el:
                    image_url = img_el.attributes.get('src') or img_el.attributes.get('data-src')

                # Fallback to other common image selectors
                if not image_url:
                    img_el = item.css_first("img")
                    if img_el:
                        image_url = img_el.attributes.get('src') or img_el.attributes.get('data-src') or img_el.attributes.get('data-lazy-src')

                # Clean up image URL if found
                if image_url:
                    # Ensure it's a full URL
                    if image_url.startswith('//'):
                        image_url = 'https:' + image_url
                    elif image_url.startswith('/'):
                        image_url = 'https://www.jumbo.tn' + image_url

                    # Only include jumbo.tn images
                    if 'jumbo.tn' in image_url:
                        product_data["image"] = image_url

                products.append(product_data)
        
        return products
    
    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from Jumbo category page HTML."""
        tree = HTMLParser(html)
        
        # Check for next page link
        next_link = tree.css_first('a.next, .pagination a[rel="next"]')
        has_next = next_link is not None and "disabled" not in (next_link.attributes.get("class", "") or "")
        
        # Get page numbers
        max_page = 1
        current_page = 1
        
        for page_link in tree.css(".pagination a, .page-list a"):
            text = page_link.text(strip=True)
            try:
                num = int(text)
                if num > max_page:
                    max_page = num
                classes = page_link.attributes.get("class", "") or ""
                parent_classes = page_link.parent.attributes.get("class", "") if page_link.parent else ""
                if "current" in classes or "active" in classes or "current" in parent_classes or "active" in parent_classes:
                    current_page = num
            except ValueError:
                pass
        
        return {
            "current_page": current_page,
            "total_pages": max_page,
            "has_next": has_next
        }
    
    async def scrape_product_details(self, url: str) -> dict:
        """Scrape detailed product information from a Jumbo product page."""
        html = await self.fetch_html(url)
        
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        
        tree = HTMLParser(html)
        data = {"url": url}
        
        # Try JSON-LD first (most reliable)
        ld_data = None
        for script in tree.css('script[type="application/ld+json"]'):
            try:
                parsed = json.loads(script.text())
                if isinstance(parsed, dict) and parsed.get("@type") == "Product":
                    ld_data = parsed
                    break
            except (json.JSONDecodeError, TypeError):
                pass
        
        # Product ID from body class (product-id-XXXX)
        body = tree.css_first("body")
        body_class = body.attributes.get("class", "") if body else ""
        id_match = re.search(r'product-id-(\d+)', body_class)
        data["product_id"] = id_match.group(1) if id_match else None
        
        # Title
        if ld_data and ld_data.get("name"):
            data["title"] = ld_data["name"]
        else:
            h1 = tree.css_first(".ce-product-name") or tree.css_first("h1")
            data["title"] = h1.text(strip=True) if h1 else None
        
        # SKU/Reference
        ref_el = tree.css_first(".ce-product-meta__reference .ce-product-meta__value")
        data["sku"] = ref_el.text(strip=True) if ref_el else (ld_data.get("sku") if ld_data else None)
        
        # Overview
        overview_el = tree.css_first(".ce-product-description-short")
        data["overview"] = overview_el.text(strip=True) if overview_el else None
        
        # Brand
        if ld_data and ld_data.get("brand", {}).get("name"):
            data["brand"] = ld_data["brand"]["name"]
        else:
            mfr_img = tree.css_first(".elementor-widget-manufacturer-image img")
            data["brand"] = mfr_img.attributes.get("alt") if mfr_img else None
        
        # Price - from .ce-product-price span (parse as float)
        price_el = tree.css_first(".ce-product-price span")
        if price_el:
            price_text = re.sub(r'[^\d.,]', '', price_el.text()).replace(',', '.')
            try:
                data["price"] = float(price_text) if price_text else None
            except ValueError:
                data["price"] = None
        elif ld_data and ld_data.get("offers", {}).get("price"):
            try:
                data["price"] = float(ld_data["offers"]["price"])
            except (ValueError, TypeError):
                data["price"] = None
        else:
            data["price"] = None
        
        # Old price - from .ce-product-regular-price (if exists)
        old_price_el = tree.css_first(".ce-product-regular-price span")
        if old_price_el:
            old_text = re.sub(r'[^\d.,]', '', old_price_el.text()).replace(',', '.')
            try:
                data["old_price"] = float(old_text) if old_text else None
                # Calculate discount if both prices exist
                if data.get("old_price") and data.get("price"):
                    data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)
            except ValueError:
                data["old_price"] = None
        else:
            data["old_price"] = None
        
        # Availability - from .ce-product-stock__availability-label
        stock_el = tree.css_first(".ce-product-stock__availability-label")
        if stock_el:
            availability_text = stock_el.text(strip=True)
            data["availability"] = availability_text
            avail_lower = availability_text.lower()
            # More precise check
            data["available"] = (
                ("disponible" in avail_lower or "en stock" in avail_lower or "in stock" in avail_lower) and
                "rupture" not in avail_lower and
                "epuisé" not in avail_lower and
                "indisponible" not in avail_lower
            )
        elif ld_data and ld_data.get("offers", {}).get("availability"):
            avail = ld_data["offers"]["availability"]
            data["availability"] = "Disponible" if "InStock" in avail else "Rupture de stock"
            data["available"] = "InStock" in avail
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
        
        # Images
        images = []
        for img in tree.css(".swiper-zoom-container img"):
            src = img.attributes.get("src") or img.attributes.get("data-src")
            if src and "jumbo.tn" in src:
                src = re.sub(r'home_default|medium_default', 'large_default', src)
                if src not in images:
                    images.append(src)
        
        # Fallback to JSON-LD image
        if not images and ld_data and ld_data.get("image"):
            main_img = re.sub(r'home_default|medium_default', 'large_default', ld_data["image"])
            images.append(main_img)
        
        data["images"] = images
        
        # Specifications
        specs = {}
        for row in tree.css(".ce-product-features__row"):
            label = row.css_first(".ce-product-features__label")
            value = row.css_first(".ce-product-features__value")
            if label and value:
                specs[label.text(strip=True)] = value.text(strip=True)
        data["specifications"] = specs
        
        return data


def get_scraper(logger: logging.Logger) -> JumboScraper:
    """Factory function to create a JumboScraper instance."""
    return JumboScraper(logger)
