#!/usr/bin/env python3
"""
Darty.tn specific scraper implementation.
"""
import logging
from typing import List
from selectolax.parser import HTMLParser
import re

from scraper.base import FastScraper


class DartyScraper(FastScraper):
    """HTTPX/selectolax-based scraper for darty.tn e-commerce site."""
    
    def __init__(self, logger: logging.Logger):
        super().__init__("darty", logger)
    

    
    def build_page_url(self, base_url: str, page_num: int) -> str:
        """Build paginated URL for Darty."""
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"
    
    def extract_categories_from_html(self, html: str) -> dict:
        """
        Extract 3-level category hierarchy from darty.tn frontpage.
        
        Structure varies by tab:
        - Tabs 0-2: Headers with nested subcategories
          - li.globomenu-item-header contains li.globomenu-item-normal items
          - Special case: In "Beauté" tab, items after "PÈSE PERSONNE" are standalone low-level cats
        - Tabs 3-7: Flat structure (empty header, items directly as normals)
          - li.globomenu-item-normal items at tab level
        """
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        
        categories = []
        
        # Top category IDs and their tab indices
        top_cats_info = [
            (134, 0), (135, 1), (160, 2), (161, 3), 
            (162, 4), (163, 5), (165, 6), (167, 7)
        ]
        
        # Headers that have fake subcategories (items should be treated as separate low-level categories)
        # In Beauté tab: PÈSE PERSONNE and its "children" are actually all low-level categories
        standalone_low_headers = {'globomenu-item-303'}  # PÈSE PERSONNE
        
        # Get tab-contents
        tab_contents = tree.css('li.globomenu-tab-content')
        
        self.logger.info(f"Found {len(tab_contents)} tab-content sections")
        
        for tid, tab_idx in top_cats_info:
            top_item = tree.css_first(f'#globomenu-item-{tid}')
            if not top_item:
                continue
            
            link = top_item.css_first('a')
            top_name = link.text(strip=True) if link else 'NO NAME'
            top_url = link.attributes.get('href', '') if link else ''
            
            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": []
            }
            
            # Get the tab content by index
            if tab_idx < len(tab_contents):
                tab_content = tab_contents[tab_idx]
                # Parse tab_content HTML separately
                tab_tree = HTMLParser(tab_content.html)
                
                # Find headers (low-level categories) in this subtree
                low_items = tab_tree.css('li.globomenu-item-header')
                
                # Check if this is a "flat" tab (tabs 3-7 where header is empty)
                has_nested_structure = False
                for low_item in low_items:
                    low_link = low_item.css_first('a')
                    if low_link and low_link.text(strip=True):
                        has_nested_structure = True
                        break
                
                if has_nested_structure:
                    # Tabs 0-2: Headers with nested subcategories
                    for low_item in low_items:
                        low_link = low_item.css_first('a')
                        if not low_link:
                            continue
                        low_name = low_link.text(strip=True)
                        low_url = low_link.attributes.get('href', '')
                        item_id = low_item.attributes.get('id', '')
                        
                        # Skip empty names
                        if not low_name:
                            continue
                        
                        # Check if this header's "children" are actually standalone low-level cats
                        if item_id in standalone_low_headers:
                            # Add this header as a low-level category (no subcategories)
                            top_cat["low_level_categories"].append({
                                "name": low_name,
                                "url": low_url,
                                "level": "low",
                                "subcategories": []
                            })
                            
                            # Add each "child" as a separate low-level category
                            sub_items = low_item.css('li.globomenu-item-normal')
                            for sub_item in sub_items:
                                sub_link = sub_item.css_first('a')
                                if not sub_link:
                                    continue
                                sub_name = sub_link.text(strip=True)
                                sub_url = sub_link.attributes.get('href', '')
                                
                                if sub_name and sub_url and not sub_url.endswith('//darty.tn/'):
                                    top_cat["low_level_categories"].append({
                                        "name": sub_name,
                                        "url": sub_url,
                                        "level": "low",
                                        "subcategories": []
                                    })
                        else:
                            # Normal case: header with real subcategories
                            low_cat = {
                                "name": low_name,
                                "url": low_url,
                                "level": "low",
                                "subcategories": []
                            }
                            
                            # Subcategories (normal items inside low-level)
                            sub_items = low_item.css('li.globomenu-item-normal')
                            for sub_item in sub_items:
                                sub_link = sub_item.css_first('a')
                                if not sub_link:
                                    continue
                                sub_name = sub_link.text(strip=True)
                                sub_url = sub_link.attributes.get('href', '')
                                
                                if sub_name and sub_url:
                                    low_cat["subcategories"].append({
                                        "name": sub_name,
                                        "url": sub_url,
                                        "level": "subcategory"
                                    })
                            
                            top_cat["low_level_categories"].append(low_cat)
                else:
                    # Tabs 3-7: Flat structure - all items are low-level categories (no subcategories)
                    # Each normal item becomes its own low-level category
                    normal_items = tab_tree.css('li.globomenu-item-normal')
                    
                    for item in normal_items:
                        item_link = item.css_first('a')
                        if not item_link:
                            continue
                        item_name = item_link.text(strip=True)
                        item_url = item_link.attributes.get('href', '')
                        
                        if item_name and item_url and not item_url.endswith('//darty.tn/'):
                            top_cat["low_level_categories"].append({
                                "name": item_name,
                                "url": item_url,
                                "level": "low",
                                "subcategories": []
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
    
    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        item_selector = cp.get("item_selector", "article.product-miniature")
        items = tree.css(item_selector)
        if not items:
            items = tree.css("article.product-miniature")
        products = []
        for item in items:
            product_id = item.attributes.get("data-id-product")
            link = item.css_first('div.product-description h3 a')
            product_url = link.attributes.get('href') if link else None
            product_name = link.text(strip=True) if link else ''
            
            if product_id and product_url:
                product_data = {
                    "id": product_id,
                    "url": product_url,
                    "name": product_name
                }
                
                # Price - from [itemprop="price"] content or .price span.money
                price_el = item.css_first('[itemprop="price"]')
                if price_el:
                    price_content = price_el.attributes.get('content')
                    if price_content:
                        try:
                            product_data["price"] = float(price_content)
                        except ValueError:
                            product_data["price"] = None
                    else:
                        price_text = re.sub(r'[^\d.,]', '', price_el.text()).replace(',', '.')
                        try:
                            product_data["price"] = float(price_text) if price_text else None
                        except ValueError:
                            product_data["price"] = None
                
                # Old price - from .regular-price
                old_price_el = item.css_first('.regular-price')
                if old_price_el:
                    old_price_text = re.sub(r'[^\d.,]', '', old_price_el.text()).replace(',', '.')
                    try:
                        product_data["old_price"] = float(old_price_text) if old_price_text else None
                        # Calculate discount if both prices exist
                        if product_data.get("old_price") and product_data.get("price"):
                            product_data["discount_percent"] = round((1 - product_data["price"] / product_data["old_price"]) * 100)
                    except ValueError:
                        product_data["old_price"] = None
                
                # Availability - from span.texte-panier element
                panier_span = item.css_first('span.texte-panier')
                if panier_span:
                    panier_text = panier_span.text(strip=True).lower()
                    if 'en rupture' in panier_text or 'rupture' in panier_text:
                        product_data["availability"] = "En rupture"
                        product_data["available"] = False
                    elif 'ajouter au panier' in panier_text or 'panier' in panier_text:
                        product_data["availability"] = "En stock"
                        product_data["available"] = True
                    else:
                        product_data["availability"] = panier_span.text(strip=True)
                        product_data["available"] = None
                else:
                    # Fallback to structured data if span not found
                    availability_link = item.css_first('link[itemprop="availability"]')
                    if availability_link:
                        avail_href = availability_link.attributes.get('href', '')
                        if 'InStock' in avail_href:
                            product_data["availability"] = "En stock"
                            product_data["available"] = True
                        elif 'OutOfStock' in avail_href:
                            product_data["availability"] = "En rupture"
                            product_data["available"] = False
                        else:
                            product_data["availability"] = None
                            product_data["available"] = None
                    else:
                        product_data["availability"] = None
                        product_data["available"] = None

                # Image extraction - from product image in listing
                image_url = None

                # Try to find product image in listing
                img_el = item.css_first('img.center-block.img-responsive, .product-image img, img[data-src]')
                if img_el:
                    image_url = img_el.attributes.get('src') or img_el.attributes.get('data-src')

                # Clean up image URL if found
                if image_url:
                    # Ensure it's a full URL
                    if image_url.startswith('//'):
                        image_url = 'https:' + image_url
                    elif image_url.startswith('/'):
                        image_url = 'https://www.darty.tn' + image_url

                    # Only include darty.tn images and avoid SVG placeholders
                    if 'darty.tn' in image_url and not image_url.startswith('data:'):
                        product_data["image"] = image_url

                products.append(product_data)
        return products
    
    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        pagination = tree.css_first('#pagination-main')
        has_next = False
        max_page = 1
        current_page = 1
        if pagination:
            next_link = pagination.css_first('a.next.js-search-link')
            if next_link and 'disabled' not in next_link.attributes.get('class', ''):
                has_next = True
            page_links = pagination.css('li a')
            for el in page_links:
                try:
                    num = int(el.text(strip=True))
                    if num > max_page:
                        max_page = num
                    if 'disabled' in el.attributes.get('class', ''):
                        current_page = num
                except Exception:
                    continue
        return {
            "current_page": current_page,
            "total_pages": max_page,
            "has_next": has_next
        }
    
    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        tree = HTMLParser(html)
        data = {"url": url}
        
        # Product ID from URL
        url_match = re.search(r"[\-/](\d+)[\-]", url)
        data["product_id"] = url_match.group(1) if url_match else None
        
        # Title - from h1 in product description, not productblock-image (which has category)
        h1 = tree.css_first('.product-description h1') or tree.css_first('h1[itemprop="name"]')
        if not h1:
            # Fallback: get from productblock-image but clean it
            h1 = tree.css_first('div.productblock-image h1')
        data["title"] = h1.text(strip=True) if h1 else None
        
        # Brand - from img.manufacturer-logo alt attribute
        brand_img = tree.css_first('img.manufacturer-logo') or tree.css_first('.product-manufacturer img')
        data["brand"] = brand_img.attributes.get('alt') if brand_img else None
        
        # Price - parse as float from div.product-price span
        price_el = tree.css_first('div.product-price span') or tree.css_first('[itemprop="price"]')
        if price_el:
            price_content = price_el.attributes.get('content')
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
        
        # Old price - parse as float
        old_price_el = tree.css_first('div.product-discount .regular-price')
        if old_price_el:
            old_text = re.sub(r'[^\d.,]', '', old_price_el.text()).replace(',', '.')
            try:
                data["old_price"] = float(old_text) if old_text else None
                # Calculate discount percent
                if data.get("old_price") and data.get("price"):
                    data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)
            except ValueError:
                data["old_price"] = None
        else:
            data["old_price"] = None
        
        # Specifications - extract from both sections
        specs = {}
        specs_rows = tree.css('#product-details dl.data_sheet')
        for dl in specs_rows:
            name = dl.css_first('dt.name')
            value = dl.css_first('dd.value')
            if name and value:
                n = name.text(strip=True)
                v = value.text(strip=True)
                if n and v:
                    specs[n] = v
        
        features_rows = tree.css('div.productblock-features dl.data_sheet')
        for dl in features_rows:
            name = dl.css_first('dt.name')
            value = dl.css_first('dd.value')
            if name and value:
                n = name.text(strip=True)
                v = value.text(strip=True)
                if n and v and n not in specs:
                    specs[n] = v
        
        data["specifications"] = specs
        
        # SKU - from "Référence" in specifications
        data["sku"] = specs.get("Référence")
        
        # Overview - from "Caractéristiques complémentaires" in specifications
        data["overview"] = specs.get("Caractéristiques complémentaires")
        
        # Availability - try span.texte-panier first (same as listing page)
        panier_span = tree.css_first('span.texte-panier')
        if panier_span:
            panier_text = panier_span.text(strip=True).lower()
            if 'en rupture' in panier_text or 'rupture' in panier_text:
                data["availability"] = "En rupture"
                data["available"] = False
            elif 'ajouter au panier' in panier_text or 'panier' in panier_text:
                data["availability"] = "En stock"
                data["available"] = True
            else:
                data["availability"] = panier_span.text(strip=True)
                data["available"] = None
        else:
            # Fallback to structured data and other elements
            availability_link = tree.css_first('link[itemprop="availability"]')
            if availability_link:
                avail_href = availability_link.attributes.get('href', '')
                if 'InStock' in avail_href:
                    data["availability"] = "En stock"
                    data["available"] = True
                elif 'OutOfStock' in avail_href:
                    data["availability"] = "En rupture"
                    data["available"] = False
                else:
                    data["availability"] = None
                    data["available"] = None
            else:
                # Try to find stock status element
                stock_el = tree.css_first('#product-availability, .product-availability, .stock-availability')
                if stock_el:
                    availability_text = stock_el.text(strip=True)
                    data["availability"] = availability_text
                    avail_lower = availability_text.lower()
                    data["available"] = (
                        ("disponible" in avail_lower or "en stock" in avail_lower or "in stock" in avail_lower) and
                        "rupture" not in avail_lower and
                        "epuisé" not in avail_lower and
                        "indisponible" not in avail_lower
                    )
                else:
                    data["availability"] = None
                    data["available"] = None
        
        # Ensure consistency: if availability text exists but available is None, infer from text
        if data.get("availability") and data.get("available") is None:
            avail_text = str(data["availability"]).lower()
            if "disponible" in avail_text or "en stock" in avail_text or "in stock" in avail_text:
                data["available"] = True
            elif "rupture" in avail_text or "epuisé" in avail_text or "indisponible" in avail_text or "hors stock" in avail_text or "out of stock" in avail_text:
                data["available"] = False
        
        # Store availability (Darty may not have this)
        data["store_availability"] = None
        
        # Images - from product-cover and gallery
        images = []
        main_img_el = tree.css_first('.product-cover img')
        if main_img_el:
            src = main_img_el.attributes.get('data-image-large-src') or main_img_el.attributes.get('src')
            if src:
                images.append(src)
        
        for img in tree.css('.product-images img, .js-thumb img, .thumb-container img'):
            src = img.attributes.get('data-image-large-src') or img.attributes.get('src')
            if src and src not in images:
                images.append(src)
        
        data["images"] = images if images else None
        
        return data
    


# Factory function to get scraper
def get_scraper(logger: logging.Logger) -> DartyScraper:
    return DartyScraper(logger)
