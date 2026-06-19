#!/usr/bin/env python3
"""
Template for new site scrapers.

To add support for a new site:
1. Copy this file to scraper/sites/{site_name}.py
2. Copy configs/sites/_template.yaml to configs/sites/{site_name}.yaml
3. Fill in the YAML selectors for your site
4. Implement the abstract methods below
5. Add the site to AVAILABLE_SCRAPERS in scraper/sites/__init__.py

Most methods can use the default implementations from BaseScraper
if your site follows standard patterns. Only override what's different.
"""
import logging
from typing import List
from selectolax.parser import HTMLParser
from playwright.async_api import Page

from scraper.base import BaseScraper


class TemplateScraper(BaseScraper):
    """
    Scraper for example.tn
    
    Replace this docstring with site-specific notes:
    - Platform/technology (Magento, WooCommerce, custom, etc.)
    - Any quirks or special handling needed
    - Rate limiting observations
    """
    
    def __init__(self, logger: logging.Logger):
        super().__init__("template", logger)  # Change "template" to site name
    
    def get_wait_selector(self) -> str:
        """
        CSS selector to wait for after page load.
        Should be an element that appears when products are loaded.
        """
        return self.selectors.get("category_page", {}).get(
            "wait_selector", 
            ".product-item"  # Change to your site's selector
        )
    
    def build_page_url(self, base_url: str, page_num: int) -> str:
        """
        Build URL for paginated results.
        
        Common patterns:
        - Query param: ?page=2, ?p=2
        - Path-based: /page/2, /p-2
        """
        param = self.selectors.get("category_page", {}).get("pagination_param", "page")
        
        if "?" in base_url:
            return f"{base_url}&{param}={page_num}"
        return f"{base_url}?{param}={page_num}"
    
    def extract_categories_from_html(self, html: str) -> dict:
        """
        Extract category hierarchy from frontpage HTML.
        
        Customize this based on how your site structures its navigation.
        The returned structure should match:
        {
            "categories": [
                {
                    "name": "Top Category",
                    "level": "top",
                    "low_level_categories": [
                        {
                            "name": "Sub Category",
                            "url": "https://...",
                            "level": "low",
                            "subcategories": [
                                {
                                    "name": "Deep Category",
                                    "url": "https://...",
                                    "level": "subcategory"
                                }
                            ]
                        }
                    ]
                }
            ],
            "stats": {
                "top_level": N,
                "low_level": N,
                "subcategory": N,
                "total_urls": N
            }
        }
        """
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        
        categories = []
        
        # Get selectors from config
        top_sel = fp.get("top_level_blocks", ".nav-item.level-1")
        low_sel = fp.get("low_level_blocks", ".submenu-item")
        sub_sel = fp.get("subcategory_blocks", ".sub-submenu-item")
        
        # Parse top-level categories
        for top_block in tree.css(top_sel):
            top_title_node = top_block.css_first(fp.get("top_level_title", "a"))
            if not top_title_node:
                continue
            
            top_cat = {
                "name": top_title_node.text(strip=True),
                "level": "top",
                "low_level_categories": []
            }
            
            # Parse low-level categories
            for low_block in top_block.css(low_sel):
                low_link = low_block.css_first(fp.get("low_level_link", "a"))
                if not low_link:
                    continue
                
                low_cat = {
                    "name": low_link.text(strip=True),
                    "url": low_link.attributes.get("href", ""),
                    "level": "low",
                    "subcategories": []
                }
                
                # Parse subcategories
                for sub_block in low_block.css(sub_sel):
                    sub_link = sub_block.css_first(fp.get("subcategory_link", "a"))
                    if not sub_link:
                        continue
                    
                    sub_href = sub_link.attributes.get("href", "")
                    if sub_href and not sub_href.startswith("javascript:"):
                        low_cat["subcategories"].append({
                            "name": sub_link.text(strip=True),
                            "url": sub_href,
                            "level": "subcategory"
                        })
                
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
        """
        Extract products from a loaded category page.
        
        This runs JavaScript in the browser context.
        Customize the selectors for your site's product cards.
        """
        cp = self.selectors.get("category_page", {})
        item_selector = cp.get("item_selector", ".product-item")
        
        # Adjust this JavaScript based on your site's HTML structure
        products = await page.evaluate(f"""
            (selector) => {{
                const items = document.querySelectorAll(selector);
                const products = [];
                
                items.forEach(item => {{
                    // Get product ID
                    const productId = item.getAttribute('data-id') || 
                                      item.querySelector('[data-product-id]')?.getAttribute('data-product-id');
                    
                    // Get product URL
                    const linkEl = item.querySelector('a[href*=".html"], a.product-link');
                    const productUrl = linkEl?.getAttribute('href');
                    
                    // Get product title
                    const titleEl = item.querySelector('.product-name, .product-title, h2, h3');
                    const productTitle = titleEl?.innerText?.trim() || '';
                    
                    if (productId || productUrl) {{
                        products.push({{
                            id: productId,
                            url: productUrl,
                            title: productTitle
                        }});
                    }}
                }});
                
                return products;
            }}
        """, item_selector)
        
        return products
    
    async def extract_pagination_info(self, page: Page) -> dict:
        """
        Extract pagination information from the page.
        
        Returns: {"current_page": int, "total_pages": int}
        """
        cp = self.selectors.get("category_page", {})
        
        # Customize this based on your site's pagination structure
        pagination = await page.evaluate("""
            () => {
                // Try to find current page
                const currentEl = document.querySelector('.pagination .active, .current-page');
                const currentPage = parseInt(currentEl?.innerText) || 1;
                
                // Try to find total pages by looking at all page links
                const pageLinks = document.querySelectorAll('.pagination a, .pages a');
                let maxPage = 1;
                
                pageLinks.forEach(el => {
                    const num = parseInt(el.innerText);
                    if (!isNaN(num) && num > maxPage) maxPage = num;
                });
                
                return {
                    current_page: currentPage,
                    total_pages: maxPage
                };
            }
        """)
        
        return pagination


# Factory function - required
def get_scraper(logger: logging.Logger) -> TemplateScraper:
    """Factory function to create scraper instance."""
    return TemplateScraper(logger)
