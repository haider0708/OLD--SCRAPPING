#!/usr/bin/env python3
"""
Zoom.com.tn Scraper
===================

Fast HTTP scraper for Zoom.com.tn e-commerce site.
"""

import re
from typing import Dict, Any, List
from selectolax.parser import HTMLParser

from scraper.base import FastScraper


class ZoomScraper(FastScraper):
    """Zoom.com.tn scraper using FastScraper base."""

    def __init__(self, logger):
        super().__init__("zoom", logger)

    @property
    def base_url(self) -> str:
        """Get base URL for Zoom."""
        return "https://zoom.com.tn"

    def build_page_url(self, category_url: str, page: int = 1) -> str:
        """Build pagination URL for category pages."""
        if page == 1:
            return category_url
        # Add pagination parameter
        if "?" in category_url:
            return f"{category_url}&page={page}"
        else:
            return f"{category_url}?page={page}"

    def extract_categories_from_html(self, html: str) -> Dict[str, Any]:
        """Extract categories from Zoom.com.tn homepage HTML using the exact JavaScript logic."""
        tree = HTMLParser(html)
        categories = []

        # Track processed URLs to avoid duplicates (as per JavaScript logic)
        processed_urls = set()

        # Get main category containers - exactly like JavaScript
        main_containers = tree.css("li.mm_tabs_li.mm_tabs_has_content")

        for main_li in main_containers:
            # Extract main category name - exactly like JavaScript
            main_link = main_li.css_first(".mm_tab_toggle_title a")
            if not main_link:
                continue

            main_category_name = main_link.text(strip=True)
            if not main_category_name:
                continue

            # Process category blocks within this main category
            blocks = main_li.css(".ets_mm_block.mm_block_type_category")

            for block in blocks:
                # Extract block category header (level 1)
                block_link = block.css_first("span.h4 a")
                if not block_link:
                    continue

                category_name = block_link.text(strip=True)
                if not category_name:
                    continue

                # Extract subcategories (level 2 and level 3) - exactly like JavaScript
                subcategories = []

                # Level 2 categories
                level2_items = block.css(".ets_mm_categories li")


                for li in level2_items:
                    # Level 2 link - find any link within this li
                    level2_link = li.css_first("a")
                    if level2_link and level2_link.attributes.get('href'):
                        level2_url = level2_link.attributes['href']
                        level2_name = level2_link.text(strip=True)

                        if level2_url and level2_name and level2_url not in processed_urls:
                            subcategories.append({
                                "name": level2_name,
                                "url": level2_url,
                                "level": "subcategory"
                            })
                            processed_urls.add(level2_url)

                    # Level 3 links (sub-subcategories) - nested ul li a
                    level3_links = li.css("ul li a")
                    for sub_link in level3_links:
                        level3_url = sub_link.attributes.get('href')
                        level3_name = sub_link.text(strip=True)

                        if level3_url and level3_name and level3_url not in processed_urls:
                            subcategories.append({
                                "name": level3_name,
                                "url": level3_url,
                                "level": "subcategory"
                            })
                            processed_urls.add(level3_url)

                # Add ALL categories, even if they don't have subcategories (unlike my previous logic)
                # Use the first subcategory URL as the category URL, or the block header URL
                category_url = None
                if subcategories:
                    category_url = subcategories[0]["url"]
                elif block_link.attributes.get('href'):
                    category_url = block_link.attributes['href']

                if category_url and category_url.startswith('/'):
                    category_url = f"https://zoom.com.tn{category_url}"

                categories.append({
                    "main_category": main_category_name,
                    "name": category_name,
                    "url": category_url,
                    "level": "low",
                    "subcategories": subcategories
                })

        # Restructure to match expected pipeline format
        top_level_categories = []

        # Group by main category
        main_category_groups = {}
        for cat in categories:
            main_cat = cat.get("main_category", "General")
            if main_cat not in main_category_groups:
                main_category_groups[main_cat] = []
            main_category_groups[main_cat].append(cat)

        # Create top-level structure
        for main_cat_name, low_cats in main_category_groups.items():
            # Get main category URL from the first container that has it
            main_url = None
            for container in main_containers:
                link = container.css_first(".mm_tab_toggle_title a")
                if link and link.text(strip=True) == main_cat_name:
                    href = link.attributes.get('href')
                    if href:
                        main_url = href if href.startswith('http') else f"https://zoom.com.tn{href}"
                    break

            top_cat = {
                "name": main_cat_name,
                "url": main_url,
                "level": "top",
                "low_level_categories": []
            }

            for low_cat in low_cats:
                low_level_cat = {
                    "name": low_cat["name"],
                    "url": low_cat["url"],
                    "level": "low",
                    "subcategories": low_cat.get("subcategories", [])
                }
                top_cat["low_level_categories"].append(low_level_cat)

            top_level_categories.append(top_cat)

        # Calculate statistics
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in top_level_categories:
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

        self.logger.info(f"Extracted {stats['top_level']} top, {stats['low_level']} low, {stats['subcategory']} sub categories ({stats['total_urls']} URLs)")

        return {
            "categories": top_level_categories,
            "stats": stats
        }

    def extract_products_from_html(self, html: str) -> List[Dict[str, Any]]:
        """Extract products from a Zoom category page using the correct selectors."""
        tree = HTMLParser(html)
        products = []

        # Use the correct container selector from the working JavaScript
        product_containers = tree.css(".product-miniature.js-product-miniature")

        for container in product_containers:
            # Product ID from data attribute (like JavaScript)
            product_id = container.attributes.get("data-id-product")

            # Product name from h5.product-name a (like JavaScript)
            name_el = container.css_first("h5.product-name a")
            if not name_el:
                continue

            product_name = name_el.text(strip=True)
            product_url = name_el.attributes.get('href')

            if not product_name or not product_url:
                continue

            # Make URL absolute
            if product_url.startswith('/'):
                product_url = f"{self.base_url}{product_url}"

            # Availability from .product-availability span (like JavaScript)
            availability_text = None
            avail_el = container.css_first(".product-availability span")
            if avail_el:
                availability_text = avail_el.text(strip=True)

            # Map availability text to standard format (like the JavaScript)
            availability = "out_of_stock"  # default
            if availability_text:
                # Check in order of specificity (most specific first)
                if "hors stock" in availability_text.lower():
                    availability = "out_of_stock"
                elif "en stock" in availability_text.lower():
                    availability = "in_stock"
                elif "en arrivage" in availability_text.lower() or "arrivage" in availability_text.lower():
                    availability = "arriving"
                elif "en commande" in availability_text.lower() or "commande" in availability_text.lower():
                    availability = "arriving"

            # Price from .price.product-price (like JavaScript)
            price = None
            price_el = container.css_first(".price.product-price")
            if price_el:
                price_text = re.sub(r'[^\d.,]', '', price_el.text()).replace(',', '.')
                try:
                    price = float(price_text) if price_text else None
                except ValueError:
                    pass

            # Old price from .regular-price (like JavaScript)
            old_price = None
            old_price_el = container.css_first(".regular-price")
            if old_price_el:
                old_price_text = re.sub(r'[^\d.,]', '', old_price_el.text()).replace(',', '.')
                try:
                    old_price = float(old_price_text) if old_price_text else None
                except ValueError:
                    pass

            # Image - try to find product image (prioritize data-original over src)
            image_url = None
            img_el = container.css_first("img")
            if img_el:
                # Try data-original first (real image), then data-src, then src
                image_url = (img_el.attributes.get('data-original') or
                           img_el.attributes.get('data-src') or
                           img_el.attributes.get('src'))

                if image_url:
                    # Skip SVG placeholders and other data URLs
                    if image_url.startswith('data:') or 'svg' in image_url:
                        image_url = None
                    else:
                        # Clean up image URL
                        if image_url.startswith('//'):
                            image_url = 'https:' + image_url
                        elif image_url.startswith('/'):
                            image_url = f"{self.base_url}{image_url}"

            if product_name and product_url:
                product_data = {
                    "id": product_id,
                    "url": product_url,
                    "name": product_name,
                    "price": price,
                    "old_price": old_price,
                    "availability": availability,
                    "available": availability == "in_stock",
                    "image": image_url
                }

                products.append(product_data)

        return products

    def extract_pagination_from_html(self, html: str) -> Dict[str, Any]:
        """Extract pagination info from category page with loop detection."""
        tree = HTMLParser(html)

        current_page = 1
        total_pages = 1
        has_next = False

        # Try to find pagination info
        pagination = tree.css_first(".pagination, .pager")

        if pagination:
            # Current page
            current_el = pagination.css_first(".current, .active, [aria-current='page']")
            if current_el:
                try:
                    current_page = int(current_el.text(strip=True))
                except (ValueError, TypeError):
                    pass

            # Total pages - look for last page link
            page_links = pagination.css("a[href]")
            for link in page_links:
                try:
                    page_num = int(link.text(strip=True))
                    total_pages = max(total_pages, page_num)
                except (ValueError, TypeError):
                    continue

            # Check if there's a next button
            next_link = pagination.css_first(".next, .pagination-next, [rel='next']")
            has_next = next_link is not None

        # For now, assume we can paginate until we find no products or loop
        # The main pagination logic will be handled by the scraping framework
        # with stop conditions for no products and loop detection

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next
        }

    async def scrape_product_details(self, url: str) -> Dict[str, Any]:
        """Scrape detailed product information from a Zoom product page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Title
        title_el = tree.css_first("h1, .product-name, .page-title")
        data["title"] = title_el.text(strip=True) if title_el else None

        # Product ID from URL
        url_match = re.search(r'/(\d+)', url)
        data["product_id"] = url_match.group(1) if url_match else None

        # Price
        price_el = tree.css_first(".current-price, .price, [itemprop='price']")
        if price_el:
            price_text = re.sub(r'[^\d.,]', '', price_el.text(strip=True)).replace(',', '.')
            try:
                data["price"] = float(price_text) if price_text else None
            except ValueError:
                data["price"] = None

        # Old price
        old_price_el = tree.css_first(".old-price, .regular-price")
        if old_price_el:
            old_price_text = re.sub(r'[^\d.,]', '', old_price_el.text(strip=True)).replace(',', '.')
            try:
                data["old_price"] = float(old_price_text) if old_price_text else None
            except ValueError:
                data["old_price"] = None

        # Availability (product details page - can have different values)
        availability_text = None
        avail_el = tree.css_first(".product-availability span, .product-availability.product-available")
        if avail_el:
            availability_text = avail_el.text(strip=True)
        else:
            # Try alternative selectors
            avail_el = tree.css_first(".product-availability")
            if avail_el:
                availability_text = avail_el.text(strip=True)

        # Map availability text to standard format
        availability = "out_of_stock"  # default
        if availability_text:
            avail_lower = availability_text.lower()
            # Check in order of specificity
            if "hors stock" in avail_lower or "indisponible" in avail_lower:
                availability = "out_of_stock"
            elif "en stock" in avail_lower or "disponible" in avail_lower:
                availability = "in_stock"
            elif "sur commande" in avail_lower or "commande" in avail_lower:
                availability = "arriving"
            elif "en arrivage" in avail_lower or "arrivage" in avail_lower:
                availability = "arriving"

        data["availability"] = availability
        data["available"] = availability == "in_stock"

        # Brand extraction - try multiple selectors
        brand = None
        brand_el = tree.css_first(".attribute-item.product-manufacturer a.li-a span")
        if brand_el:
            brand = brand_el.text(strip=True)

        data["brand"] = brand

        # Reference/SKU extraction
        sku = None
        sku_el = tree.css_first(".product-reference span")
        if sku_el:
            sku = sku_el.text(strip=True)
        data["sku"] = sku

        # Overview/Short description - use product ID in selector
        overview = None
        if data.get("product_id"):
            overview_selector = f"#product-description-short-{data['product_id']}"
            overview_el = tree.css_first(overview_selector)
            if overview_el:
                overview = overview_el.text(strip=True)
        data["overview"] = overview

        # Full description (fallback if overview not found)
        if not overview:
            desc_el = tree.css_first(".product-description, .description, #description")
            data["description"] = desc_el.text(strip=True) if desc_el else None

        # Images
        images = []
        img_els = tree.css(".product-images img, .gallery img, .product-gallery img")
        for img_el in img_els:
            img_url = img_el.attributes.get('src') or img_el.attributes.get('data-src')
            if img_url:
                if img_url.startswith('//'):
                    img_url = 'https:' + img_url
                elif img_url.startswith('/'):
                    img_url = f"{self.base_url}{img_url}"
                if img_url not in images:
                    images.append(img_url)

        data["images"] = images[:10] if images else None

        # Brand
        brand_el = tree.css_first(".product-brand, .brand")
        data["brand"] = brand_el.text(strip=True) if brand_el else None

        # Specifications (DL structure)
        specs = {}
        dl = tree.css_first("dl")
        if dl:
            dts = dl.css("dt")
            dds = dl.css("dd")
            for i, dt in enumerate(dts):
                if i < len(dds):
                    key = dt.text(strip=True)
                    value = dds[i].text(strip=True)
                    if key and value:
                        specs[key] = value

        data["specifications"] = specs if specs else None

        return data


# Factory function to get scraper
def get_scraper(logger) -> ZoomScraper:
    return ZoomScraper(logger)
