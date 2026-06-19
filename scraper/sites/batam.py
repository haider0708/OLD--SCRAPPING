from typing import List, Dict, Any
import re
import logging
from scraper.base import FastScraper, save_text_atomic
from selectolax.parser import HTMLParser


class BatamScraper(FastScraper):
    """Scraper for Batam.com.tn"""

    def __init__(self, logger: logging.Logger):
        super().__init__("batam", logger)

    async def download_frontpage(self):
        """Use Playwright for dynamic menu rendering."""
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
                    await page.wait_for_selector("a[href*='batam.com.tn/']", timeout=8000)
                except Exception:
                    pass
                html = await page.content()
            finally:
                await page.close()
                await browser.close()
        save_text_atomic(html, output_path, self.logger)
        return output_path

    def build_page_url(self, base_url: str, page_num: int) -> str:
        """Build paginated URL for Batam."""
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract categories from Batam.com.tn frontpage HTML using the JavaScript logic."""
        tree = HTMLParser(html)
        categories = []

        # Get all top-level containers
        top_containers = tree.css("li.level-0.parent-ul-list")
        self.logger.info(f"Found {len(top_containers)} top-level category containers")
        if not top_containers:
            return self._extract_categories_fallback(tree)

        for container in top_containers:
            # Extract top category name
            top_name_el = container.css_first("button.level-0 span.text-left")
            top_name = top_name_el.text(strip=True) if top_name_el else None

            if not top_name:
                continue

            # Skip promotional categories
            skip_keywords = ["promo", "spécial", "special", "offre", "remise"]
            if any(keyword.lower() in top_name.lower() for keyword in skip_keywords):
                self.logger.info(f"⏭️ Skipping promotional category: {top_name}")
                continue

            # Extract top-level URL (optional)
            top_link_el = container.css_first("ul.level-1 > li > a.font-bold")
            top_url = top_link_el.attributes.get('href') if top_link_el else None

            # Initialize top category
            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": []
            }

            # Extract level-1 children - process each li element once
            level1_ul = container.css_first("ul.level-1")
            if level1_ul:
                # Get all li elements except the back button (first item)
                items = level1_ul.css("li:not(:first-child)")

                # Use a set to track processed URLs to avoid duplicates
                processed_urls = set()

                for li in items:
                    # Priority 1: Main category with level-2 submenu (button that expands)
                    expand_button = li.css_first("button[title]")
                    if expand_button:
                        cat_name = expand_button.attributes.get("title", "").strip() or expand_button.text(strip=True)
                        cat_url = None  # Buttons don't have direct URLs

                        # Check for level-2 submenu
                        level2_ul = li.css_first("ul.level-2")
                        subcategories = []

                        if level2_ul:
                            # Extract level-2 links (skip back button)
                            level2_items = level2_ul.css("li:not(:first-child)")
                            overview_link_processed = False

                            for l2_item in level2_items:
                                l2_link = l2_item.css_first("a[href]")
                                if not l2_link:
                                    continue

                                l2_name = l2_link.text(strip=True)
                                l2_url = l2_link.attributes.get('href')

                                if not l2_name or not l2_url:
                                    continue

                                # Use first link as parent category URL if not set
                                if not cat_url and not overview_link_processed:
                                    cat_url = l2_url
                                    overview_link_processed = True
                                    continue

                                # Add remaining links as subcategories
                                if l2_url not in processed_urls:
                                    subcategories.append({
                                        "name": l2_name,
                                        "url": l2_url,
                                        "level": "subcategory"
                                    })
                                    processed_urls.add(l2_url)

                        if cat_name and cat_name != top_name:
                            top_cat["low_level_categories"].append({
                                "name": cat_name,
                                "url": cat_url,
                                "level": "low",
                                "subcategories": subcategories
                            })
                        continue

                    # Priority 2: Direct category links (a tags)
                    direct_link = li.css_first("a[href]")
                    if direct_link:
                        cat_name = direct_link.text(strip=True)
                        cat_url = direct_link.attributes.get('href')

                        # Skip if already processed or same as top-level
                        if cat_url in processed_urls or cat_name == top_name:
                            continue

                        # Skip main category overview links (font-bold)
                        if "font-bold" in direct_link.attributes.get("class", ""):
                            # Use URL for top-level if not set
                            if cat_url and not top_cat.get("url"):
                                top_cat["url"] = cat_url
                            continue

                        # This is a regular category link
                        top_cat["low_level_categories"].append({
                            "name": cat_name,
                            "url": cat_url,
                            "level": "low",
                            "subcategories": []
                        })
                        processed_urls.add(cat_url)

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

        self.logger.info(f"Extracted {stats['top_level']} top, {stats['low_level']} low, {stats['subcategory']} sub categories ({stats['total_urls']} URLs)")

        return {
            "categories": categories,
            "stats": stats
        }

    def _extract_categories_fallback(self, tree: HTMLParser) -> dict:
        """Fallback category extraction for homepage layout changes."""
        seen = set()
        low_level_categories = []
        for link in tree.css("a[href]"):
            href = (link.attributes.get("href") or "").strip()
            name = (link.text(strip=True) or "").strip()
            if not href or not name:
                continue
            if "batam.com.tn/" not in href:
                continue
            # Product pages on Batam mostly end with .html and should be ignored.
            if href.endswith(".html"):
                continue
            if any(
                bad in href.lower()
                for bad in ("/checkout", "/customer", "/account", "/cart", "/wishlist")
            ):
                continue
            if href in seen:
                continue
            seen.add(href)
            low_level_categories.append(
                {"name": name, "url": href, "level": "low", "subcategories": []}
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
        self.logger.info(
            f"Fallback extracted {stats['low_level']} low categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[Dict[str, Any]]:
        """Extract products from a Batam category page HTML."""
        tree = HTMLParser(html)
        products = []

        # Get product containers
        containers = tree.css("form.product_addtocart_form")

        for container in containers:
            # Product URL
            link_el = container.css_first("a.product-item-link")
            if not link_el:
                continue

            product_url = link_el.attributes.get('href')
            if not product_url:
                continue

            # Product name
            product_name = link_el.text(strip=True)

            # Price
            price_el = container.css_first("[data-price-type='finalPrice'] .price")
            price = None
            if price_el:
                price_text = re.sub(r'[^\d.,]', '', price_el.text()).replace(',', '.')
                try:
                    price = float(price_text) if price_text else None
                except ValueError:
                    price = None

            # Old price
            old_price_el = container.css_first("[data-price-type='oldPrice'] .price")
            old_price = None
            if old_price_el:
                old_price_text = re.sub(r'[^\d.,]', '', old_price_el.text()).replace(',', '.')
                try:
                    old_price = float(old_price_text) if old_price_text else None
                except ValueError:
                    old_price = None

            # Availability logic - check for specific span classes and text content
            availability = "out_of_stock"  # default

            # Check all spans for availability text
            all_spans = container.css("span")
            for span in all_spans:
                span_text = span.text(strip=True)
                if span_text == "En stock" or "text-green-500" in span.attributes.get("class", ""):
                    availability = "in_stock"
                    break
                elif span_text == "En Arrivage" or "text-blue" in span.attributes.get("class", ""):
                    availability = "arriving"
                    break
                elif span_text == "Epuisé" or "text-red" in span.attributes.get("class", ""):
                    availability = "out_of_stock"
                    break

            # Fallback: check for add to cart button
            if availability == "out_of_stock" and container.css_first(".btn-add-to-cart"):
                availability = "in_stock"

            # Image
            image_url = None
            img_el = container.css_first("img.product-image-photo")
            if img_el:
                image_url = img_el.attributes.get('src')
                if image_url:
                    # Clean up image URL
                    if image_url.startswith('//'):
                        image_url = 'https:' + image_url
                    elif image_url.startswith('/'):
                        image_url = 'https://www.batam.com.tn' + image_url
                    # Only include batam.com.tn images and avoid data URLs
                    if not image_url.startswith('data:') and 'batam.com.tn' in image_url:
                        pass  # Keep as is
                    else:
                        image_url = None

            # Product ID from hidden input field
            product_id = None
            id_input = container.css_first('input[type="hidden"][name="product"]')
            if id_input:
                product_id = id_input.attributes.get('value')

            # Fallback: extract from URL if input not found
            if not product_id and product_url:
                id_match = re.search(r'/(\d+)/?$', product_url) or re.search(r'id=(\d+)', product_url)
                if id_match:
                    product_id = id_match.group(1)

            if product_name and product_url:
                product_data = {
                    "id": product_id,
                    "url": product_url,
                    "name": product_name,
                    "price": price,
                    "old_price": old_price,
                    "availability": availability,
                    "available": availability == "in_stock"
                }

                if image_url:
                    product_data["image"] = image_url

                products.append(product_data)

        return products

    def extract_pagination_from_html(self, html: str) -> Dict[str, Any]:
        """Extract pagination information from Batam category page."""
        tree = HTMLParser(html)

        # Check for active page
        active_page_el = tree.css_first("a[aria-current='page']")
        if active_page_el:
            active_page_text = active_page_el.text(strip=True)
            try:
                current_page = int(active_page_text)
            except ValueError:
                current_page = 1
        else:
            current_page = 1

        # Check if there's a "next" link or more pages
        # For Batam, if we're on page 1 and there are products, assume there might be more pages
        # The JavaScript logic checks if the active page resets to 1, indicating no more pages

        has_next = True  # Assume there might be more pages unless proven otherwise

        # Look for pagination links
        pagination_links = tree.css("a[href*='?p=']")
        page_numbers = set()

        for link in pagination_links:
            href = link.attributes.get('href', '')
            page_match = re.search(r'\?p=(\d+)', href)
            if page_match:
                try:
                    page_num = int(page_match.group(1))
                    page_numbers.add(page_num)
                except ValueError:
                    pass

        if page_numbers:
            max_page = max(page_numbers)
            has_next = current_page < max_page
        else:
            # No explicit pagination found, assume single page
            has_next = False

        return {
            "has_next": has_next,
            "total_pages": max(page_numbers) if page_numbers else 1,
            "current_page": current_page
        }

    async def scrape_product_details(self, url: str) -> Dict[str, Any]:
        """Scrape detailed product information from a Batam product page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Product ID from hidden input
        product_id_input = tree.css_first("input[name='product']")
        data["product_id"] = product_id_input.attributes.get("value") if product_id_input else None

        # Title/Name
        title_el = tree.css_first("h1 span.base")
        data["title"] = title_el.text(strip=True) if title_el else None

        # Availability (text only from spans)
        availability_el = tree.css_first("span.text-green-500, span.text-blue, span.text-red")
        data["availability"] = availability_el.text(strip=True) if availability_el else None
        data["available"] = data["availability"] is not None and "stock" in (data["availability"].lower())

        # Price - final price with fallbacks
        price = None

        # 1. Try visible final price
        price_el = (tree.css_first(".final-price .price") or
                   tree.css_first("[data-price-type='finalPrice'] .price"))
        if price_el:
            price_text = price_el.text(strip=True)
            if price_text:
                # Clean price text and convert to float
                price_clean = re.sub(r'[^\d.,]', '', price_text).replace(',', '.')
                try:
                    price = float(price_clean)
                except ValueError:
                    pass

        # 2. Fallback: schema.org meta
        if price is None:
            meta_price = tree.css_first("meta[itemprop='price']")
            if meta_price and meta_price.attributes.get("content"):
                try:
                    price = float(meta_price.attributes["content"])
                except (ValueError, TypeError):
                    pass

        data["price"] = price

        # Old price
        old_price_el = (tree.css_first(".old-price .price") or
                       tree.css_first("[data-price-type='oldPrice'] .price"))
        if old_price_el:
            old_price_text = re.sub(r'[^\d.,]', '', old_price_el.text(strip=True)).replace(',', '.')
            try:
                data["old_price"] = float(old_price_text) if old_price_text else None
            except ValueError:
                data["old_price"] = None

        # Images (find product images in catalog)
        images = []
        seen_urls = set()

        # Find all images that are product images (contain media/catalog/product in src)
        all_img_els = tree.css("img")
        for img_el in all_img_els:
            img_url = img_el.attributes.get('src')
            if img_url and 'media/catalog/product' in img_url and img_url not in seen_urls:
                # Clean up URL
                if img_url.startswith('//'):
                    img_url = 'https:' + img_url
                elif img_url.startswith('/'):
                    img_url = 'https://www.batam.com.tn' + img_url

                if img_url and not img_url.startswith('data:'):
                    images.append(img_url)
                    seen_urls.add(img_url)

        data["images"] = images[:10] if images else None  # Limit to 10 images

        # Fiche Technique (technical specifications)
        fiche_technique = {}

        spec_rows = tree.css("table.additional-attributes tbody tr")
        for row in spec_rows:
            key_el = row.css_first("th")
            value_el = row.css_first("td")
            if key_el and value_el:
                key = key_el.text(strip=True)
                value = value_el.text(strip=True)
                if key and value:
                    fiche_technique[key] = value

        data["fiche_technique"] = fiche_technique if fiche_technique else None

        # Map to standard fields for consistency with other scrapers
        # Keep fiche_technique as-is since it's Batam-specific
        # Other fields already match the standard format

        return data


def get_scraper(logger: logging.Logger) -> BatamScraper:
    """Factory function to create Batam scraper instance."""
    return BatamScraper(logger)
