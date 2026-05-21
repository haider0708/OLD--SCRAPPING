#!/usr/bin/env python3
"""
Technopro-Online.com specific scraper implementation.
"""

import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser
from scraper.base import FastScraper



class TechnoproScraper(FastScraper):
    """HTTPX/selectolax-based scraper for technopro-online.com"""

    def __init__(self, logger: logging.Logger):
        super().__init__("technopro", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories = []
        top_sel = fp.get("top_level_blocks", "li[id*='cbp-hrmenu-tab']")
        top_blocks = tree.css(top_sel)
        self.logger.info(f"Found {len(top_blocks)} top-level categories")
        for top_block in top_blocks:
            top_link = top_block.css_first("a.nav-link")
            if not top_link:
                continue
            top_title = top_link.text(strip=True)
            top_href = top_link.attributes.get("href", "")
            if not top_title:
                continue
            top_cat = {
                "name": top_title,
                "url": top_href if top_href and top_href != "#" and not top_href.startswith("javascript:") else None,
                "level": "top",
                "low_level_categories": []
            }
            submenu = top_block.css_first(".cbp-hrsub")
            if not submenu:
                categories.append(top_cat)
                continue
            columns = submenu.css(".cbp-menu-column")
            for column in columns:
                header_span = column.css_first("strong span")
                header_link = column.css_first("strong a")
                if not header_span:
                    continue
                low_title = header_span.text(strip=True)
                low_href = header_link.attributes.get("href", "") if header_link else ""
                if not low_title:
                    continue
                low_cat = {
                    "name": low_title,
                    "url": low_href if low_href and low_href != "#" and not low_href.startswith("javascript:") else None,
                    "level": "low",
                    "subcategories": []
                }
                sub_links = column.css("ul li a")
                for sub_link in sub_links:
                    sub_title = sub_link.text(strip=True)
                    sub_href = sub_link.attributes.get("href", "")
                    if not sub_title or sub_href == "#" or sub_href.startswith("javascript:"):
                        continue
                    low_cat["subcategories"].append({
                        "name": sub_title,
                        "url": sub_href,
                        "level": "subcategory"
                    })
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
        if not items:
            items = tree.css("[data-id-product]")
        products = []
        for item in items:
            product_id = item.attributes.get("data-id-product")
            link = item.css_first("a.product-thumbnail") or item.css_first(".thumbnail-container a") or item.css_first(".product-title a") or item.css_first("a")
            product_url = link.attributes.get("href") if link else None
            title_el = item.css_first(".product-title a") or item.css_first(".product-title") or item.css_first("h2 a") or item.css_first("h3 a")
            product_name = title_el.text(strip=True) if title_el else ""
            
            # Price from listing
            price_el = item.css_first(".product-price-and-shipping .product-price")
            price = None
            if price_el:
                # Try content attribute first, then parse text
                content = price_el.attributes.get("content")
                if content:
                    try:
                        price = float(content)
                    except ValueError:
                        price = self._parse_price(price_el.text(strip=True))
                else:
                    price = self._parse_price(price_el.text(strip=True))
            
            # Old price from listing
            old_price_el = item.css_first(".product-price-and-shipping .regular-price")
            old_price = self._parse_price(old_price_el.text(strip=True)) if old_price_el else None
            
            # Availability from listing
            availability_span = item.css_first(".product-availability span")
            availability = None
            available = None
            if availability_span:
                availability = availability_span.text(strip=True)
                classes = availability_span.attributes.get("class", "").lower()
                # Check for product-available class (badge-success product-available)
                available = "product-available" in classes and "product-unavailable" not in classes
            
            # Image extraction
            image_url = None

            # Primary selector based on user's HTML structure - avoid SVG placeholders
            img_el = item.css_first("a.thumbnail.product-thumbnail img.product-thumbnail-first:not([src*='svg']), a.product-thumbnail img:first-child:not([src*='svg'])")
            if img_el:
                image_url = img_el.attributes.get('src') or img_el.attributes.get('data-src') or img_el.attributes.get('data-full-size-image-url')

            # Fallback to any image in the thumbnail link - avoid SVG
            if not image_url:
                thumbnail_link = item.css_first("a.thumbnail.product-thumbnail, a.product-thumbnail")
                if thumbnail_link:
                    img_el = thumbnail_link.css_first("img:not([src*='svg'])")
                    if img_el:
                        image_url = img_el.attributes.get('src') or img_el.attributes.get('data-src') or img_el.attributes.get('data-full-size-image-url')

            # Clean up image URL if found
            if image_url:
                # Skip data URLs (like SVG placeholders)
                if image_url.startswith('data:'):
                    image_url = None

                if image_url:
                    # Ensure it's a full URL
                    if image_url.startswith('//'):
                        image_url = 'https:' + image_url
                    elif image_url.startswith('/'):
                        image_url = 'https://www.technopro-online.com' + image_url

                    # Only include technopro-online.com images
                    if 'technopro-online.com' in image_url:
                        # Convert thickbox_default to home_default for smaller images if needed
                        # But keep the original size as provided
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
        tree = HTMLParser(html)
        pagination = tree.css_first(".pagination")
        has_next = False
        max_page = 1
        current_page = 1
        if pagination:
            # Next page
            next_link = pagination.css_first("a.next, a[rel='next']")
            if next_link and "disabled" not in next_link.attributes.get("class", ""):
                has_next = True
            # Page numbers
            page_links = pagination.css("a, li")
            for el in page_links:
                try:
                    num = int(el.text(strip=True))
                    if num > max_page:
                        max_page = num
                    if "current" in el.attributes.get("class", "") or "active" in el.attributes.get("class", ""):
                        current_page = num
                except Exception:
                    continue
            # Also check for .current or .active
            active = pagination.css_first(".current, .active")
            if active:
                try:
                    num = int(active.text(strip=True))
                    current_page = num
                except Exception:
                    pass
        return {
            "current_page": current_page,
            "total_pages": max_page,
            "has_next": has_next
        }

    def _parse_price(self, text: str) -> Optional[float]:
        """Parse price text like '729,000 TND' to float 729.0"""
        if not text:
            return None
        # Remove TND, spaces, non-breaking spaces
        cleaned = re.sub(r'[TND\s\u00a0]', '', text).replace(',', '.')
        try:
            return float(cleaned)
        except ValueError:
            return None

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        tree = HTMLParser(html)
        data = {"url": url}
        
        # Title
        h1 = tree.css_first('h1.h1') or tree.css_first('h1[itemprop="name"]') or tree.css_first('h1.product-name') or tree.css_first('h1')
        data["title"] = h1.text(strip=True) if h1 else None
        
        # Product ID from URL or page
        url_match = re.search(r"[\-/](\d+)[\-\.]", url)
        data["product_id"] = url_match.group(1) if url_match else None
        product_div = tree.css_first('[data-id-product]')
        if not data["product_id"] and product_div:
            data["product_id"] = product_div.attributes.get('data-id-product')
        
        # SKU/Reference
        ref_el = tree.css_first('.product-reference span') or tree.css_first('[itemprop="sku"]')
        data["sku"] = ref_el.text(strip=True) if ref_el else None
        
        # Brand
        brand_el = tree.css_first('[itemprop="brand"] span') or tree.css_first('.product-manufacturer span')
        data["brand"] = brand_el.text(strip=True) if brand_el else None
        if not data["brand"]:
            brand_img = tree.css_first('.product-manufacturer img')
            data["brand"] = brand_img.attributes.get('alt') if brand_img else None
        
        # Overview - try short description first, then full description
        overview_el = (tree.css_first('#product-description-short') or 
                       tree.css_first('.product-description-short') or
                       tree.css_first('#description .product-description') or
                       tree.css_first('.product-description'))
        data["overview"] = overview_el.text(strip=True) if overview_el else None
        
        # Price - extract from .product-price-and-shipping .product-price
        # Structure: <div class="product-price-and-shipping"> <span class="product-price" content="999">999,000&nbsp;TND</span> ...
        price_container = tree.css_first('.product-price-and-shipping')
        if price_container:
            price_el = price_container.css_first('.product-price')
            if price_el:
                # Try content attribute first, then parse text
                content = price_el.attributes.get('content')
                if content:
                    try:
                        data["price"] = float(content)
                    except ValueError:
                        data["price"] = self._parse_price(price_el.text(strip=True))
                else:
                    data["price"] = self._parse_price(price_el.text(strip=True))
            else:
                data["price"] = None
        else:
            # Fallback to old selector
            price_el = tree.css_first('.product-price[content]') or tree.css_first('[itemprop="price"]')
            if price_el:
                content = price_el.attributes.get('content')
                if content:
                    try:
                        data["price"] = float(content)
                    except ValueError:
                        data["price"] = self._parse_price(price_el.text(strip=True))
                else:
                    data["price"] = self._parse_price(price_el.text(strip=True))
            else:
                data["price"] = None
        
        # Old Price - extract from .product-price-and-shipping .regular-price
        # Structure: <div class="product-price-and-shipping"> ... <span class="regular-price">1 029,000&nbsp;TND</span></div>
        if price_container:
            old_price_el = price_container.css_first('.regular-price')
            if old_price_el:
                old_price = self._parse_price(old_price_el.text(strip=True))
                if old_price and data.get("price") and old_price != data["price"]:
                    data["old_price"] = old_price
                    data["discount_percent"] = round((1 - data["price"] / old_price) * 100)
                else:
                    data["old_price"] = None
                    data["discount_percent"] = None
            else:
                data["old_price"] = None
                data["discount_percent"] = None
        else:
            # Fallback
            old_price_el = tree.css_first('.regular-price')
            if old_price_el:
                old_price = self._parse_price(old_price_el.text(strip=True))
                if old_price and data.get("price") and old_price != data["price"]:
                    data["old_price"] = old_price
                    data["discount_percent"] = round((1 - data["price"] / old_price) * 100)
                else:
                    data["old_price"] = None
                    data["discount_percent"] = None
            else:
                data["old_price"] = None
                data["discount_percent"] = None
        
        # Availability - extract from .product-availability span
        # Structure: 
        #   <div class="product-availability"> <span class="badge badge-success product-available mt-2"> <i class="fa fa-check"></i> En Stock </span></div>
        #   <div class="product-availability"> <span class="badge badge-success product-available mt-2"> <i class="fa fa-check"></i> Sur commande </span></div>
        #   <div class="product-availability"> <span class="badge badge-danger product-unavailable mt-2"> <i class="fa fa-ban"></i> Rupture de stock </span></div>
        availability_div = tree.css_first('.product-availability')
        if availability_div:
            availability_span = availability_div.css_first('span')
            if availability_span:
                # Get text content (skip icon text)
                availability_text = availability_span.text(strip=True)
                data["availability"] = availability_text
                
                # Check classes to determine availability
                classes = availability_span.attributes.get("class", "").lower()
                # Available if it has "product-available" class and NOT "product-unavailable"
                # badge-success product-available = available (En Stock, Sur commande)
                # badge-danger product-unavailable = not available (Rupture de stock)
                data["available"] = (
                    "product-available" in classes and 
                    "product-unavailable" not in classes
                )
                # Ensure boolean (not None) if we have availability text
                if data.get("availability") and data.get("available") is None:
                    avail_text = str(data["availability"]).lower()
                    if "en stock" in avail_text or "disponible" in avail_text:
                        data["available"] = True
                    elif "rupture" in avail_text or "epuisé" in avail_text or "indisponible" in avail_text:
                        data["available"] = False
            else:
                data["availability"] = None
                data["available"] = None
        else:
            # Fallback to old selector
            stock_el = tree.css_first('#product-availability') or tree.css_first('.product-availability')
            if stock_el:
                data["availability"] = stock_el.text(strip=True)
                classes = stock_el.attributes.get("class", "").lower()
                data["available"] = "product-available" in classes and "product-unavailable" not in classes
                # Ensure boolean if we have text
                if data.get("availability") and data.get("available") is None:
                    avail_text = str(data["availability"]).lower()
                    if "en stock" in avail_text or "disponible" in avail_text:
                        data["available"] = True
                    elif "rupture" in avail_text or "epuisé" in avail_text or "indisponible" in avail_text:
                        data["available"] = False
            else:
                data["availability"] = None
                data["available"] = None
        
        # Final consistency check: if availability text exists but available is None, infer from text
        if data.get("availability") and data.get("available") is None:
            avail_text = str(data["availability"]).lower()
            if "en stock" in avail_text or "disponible" in avail_text:
                data["available"] = True
            elif "rupture" in avail_text or "epuisé" in avail_text or "indisponible" in avail_text or "hors stock" in avail_text:
                data["available"] = False
            elif "sur commande" in avail_text or "commande" in avail_text:
                data["available"] = False  # On order = not immediately available
        
        # Images from swiper carousel
        # Structure: .product-lmage-large img with src or data-image-large-src
        images = []
        for img in tree.css('.product-lmage-large img, .product-cover img'):
            # Try different source attributes
            src = img.attributes.get('src')
            # Skip placeholder SVGs
            if src and not src.startswith('data:'):
                if src not in images:
                    images.append(src)
            # Also check data-src for lazy loaded images
            data_src = img.attributes.get('data-src')
            if data_src and not data_src.startswith('data:') and data_src not in images:
                images.append(data_src)
        data["images"] = images
        
        # Specifications/Features
        specs_container = tree.css_first('.product-features') or tree.css_first('.data-sheet') or tree.css_first('.product-information')
        data["specifications"] = {}
        if specs_container:
            names = specs_container.css('dt.name')
            values = specs_container.css('dd.value')
            for i in range(min(len(names), len(values))):
                name = names[i].text(strip=True)
                value = values[i].text(strip=True)
                if name and value:
                    data["specifications"][name] = value
        
        return data


def get_scraper(logger: logging.Logger) -> TechnoproScraper:
    return TechnoproScraper(logger)
