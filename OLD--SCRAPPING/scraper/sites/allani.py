#!/usr/bin/env python3
"""
Allani.com.tn specific scraper implementation.
"""
import logging
import re
from typing import List, Dict, Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper


class AllaniScraper(FastScraper):
    """HTTPX/selectolax-based scraper for allani.com.tn (PrestaShop)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("allani", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract 3-level category hierarchy from allani.com.tn frontpage using depth-based PrestaShop menu."""
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories = []

        top_blocks = tree.css(fp.get("top_level_blocks", "ul#top-menu[data-depth='0'] > li.category"))
        self.logger.info(f"Found {len(top_blocks)} top-level category blocks")

        for top_block in top_blocks:
            top_link = top_block.css_first(fp.get("top_level_link", "a.dropdown-item[data-depth='0']"))
            if not top_link:
                continue

            top_name = top_link.text(strip=True)
            top_url = top_link.attributes.get("href", "")
            if top_url and not top_url.startswith("http"):
                top_url = urljoin(self.base_url, top_url)

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            low_blocks = top_block.css(fp.get("low_level_blocks", "ul[data-depth='1'] > li.category"))
            for low_block in low_blocks:
                low_link = low_block.css_first(fp.get("low_level_link", "a.dropdown-item[data-depth='1']"))
                if not low_link:
                    continue

                low_name = low_link.text(strip=True)
                low_url = low_link.attributes.get("href", "")
                if low_url and not low_url.startswith("http"):
                    low_url = urljoin(self.base_url, low_url)

                low_cat = {
                    "name": low_name,
                    "url": low_url,
                    "level": "low",
                    "subcategories": [],
                }

                sub_blocks = low_block.css(fp.get("subcategory_blocks", "ul[data-depth='2'] > li.category"))
                for sub_block in sub_blocks:
                    sub_link = sub_block.css_first(fp.get("subcategory_link", "a.dropdown-item[data-depth='2']"))
                    if not sub_link:
                        continue

                    sub_name = sub_link.text(strip=True)
                    sub_url = sub_link.attributes.get("href", "")
                    if sub_url and not sub_url.startswith("http"):
                        sub_url = urljoin(self.base_url, sub_url)

                    low_cat["subcategories"].append({
                        "name": sub_name,
                        "url": sub_url,
                        "level": "subcategory",
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

        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )

        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[Dict[str, Any]]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products = []

        items = tree.css(cp.get("item_selector", "article.product-miniature.js-product-miniature"))

        for item in items:
            product_id = item.attributes.get(cp.get("item_id_attr", "data-id-product"))

            link = item.css_first(cp.get("item_name", ".product-description h3.product-title a"))
            if not link:
                continue
            product_url = link.attributes.get("href", "")
            product_name = link.text(strip=True)

            if not product_id or not product_url:
                continue

            if not product_url.startswith("http"):
                product_url = urljoin(self.base_url, product_url)

            product_data: Dict[str, Any] = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }

            # Price
            price_el = item.css_first(cp.get("item_price", ".product-price-and-shipping span.price[itemprop='price']"))
            if price_el:
                price_content = price_el.attributes.get("content")
                if price_content:
                    try:
                        product_data["price"] = float(price_content)
                    except ValueError:
                        product_data["price"] = None
                else:
                    price_text = re.sub(r"[^\d.,]", "", price_el.text()).replace(",", ".")
                    try:
                        product_data["price"] = float(price_text) if price_text else None
                    except ValueError:
                        product_data["price"] = None
            else:
                product_data["price"] = None

            # Old price
            old_price_el = item.css_first(".regular-price")
            if old_price_el:
                old_text = re.sub(r"[^\d.,]", "", old_price_el.text()).replace(",", ".")
                try:
                    product_data["old_price"] = float(old_text) if old_text else None
                except ValueError:
                    product_data["old_price"] = None

            # Image
            img_el = item.css_first(cp.get("item_image", ".dd-product-image a.product-thumbnail img"))
            if img_el:
                image_url = None
                for attr in cp.get("item_image_attrs", ["data-src", "src"]):
                    image_url = img_el.attributes.get(attr)
                    if image_url:
                        break
                if image_url:
                    if image_url.startswith("//"):
                        image_url = "https:" + image_url
                    elif image_url.startswith("/"):
                        image_url = urljoin(self.base_url, image_url)
                    if not image_url.startswith("data:"):
                        product_data["image"] = image_url

            products.append(product_data)

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})

        current_page = 1
        total_pages = 1
        has_next = False

        # Current page from active/disabled link
        page_links = tree.css(cp.get("pagination_pages", "ul.page-list li a.js-search-link"))
        for el in page_links:
            classes = el.attributes.get("class", "")
            try:
                num = int(el.text(strip=True))
                if num > total_pages:
                    total_pages = num
                if "disabled" in classes or "current" in classes:
                    current_page = num
            except (ValueError, TypeError):
                continue

        next_link = tree.css_first(cp.get("pagination_next", "ul.pagination a.next.js-search-link[rel='next']"))
        if next_link and "disabled" not in next_link.attributes.get("class", ""):
            has_next = True

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = {"url": url}

        # Product ID from URL
        url_match = re.search(r"[\-/](\d+)[\-.]", url)
        data["product_id"] = url_match.group(1) if url_match else None

        # Title
        title_el = tree.css_first(pp.get("title", "h1.h1.product[itemprop='name']"))
        data["title"] = title_el.text(strip=True) if title_el else None

        # Price — prefer content attribute
        price_el = tree.css_first(pp.get("price", ".current-price span[itemprop='price']"))
        if price_el:
            price_content = price_el.attributes.get("content")
            if price_content:
                try:
                    data["price"] = float(price_content)
                except ValueError:
                    data["price"] = None
            else:
                price_text = re.sub(r"[^\d.,]", "", price_el.text()).replace(",", ".")
                try:
                    data["price"] = float(price_text) if price_text else None
                except ValueError:
                    data["price"] = None
        else:
            data["price"] = None

        # Old price
        old_price_el = tree.css_first(".regular-price")
        if old_price_el:
            old_text = re.sub(r"[^\d.,]", "", old_price_el.text()).replace(",", ".")
            try:
                data["old_price"] = float(old_text) if old_text else None
            except ValueError:
                data["old_price"] = None

        # Brand
        brand_el = tree.css_first(pp.get("brand", ".product-manufacturer a"))
        data["brand"] = brand_el.text(strip=True) if brand_el else None

        # SKU
        sku_el = tree.css_first(pp.get("sku", ".product-reference span[itemprop='sku']"))
        data["sku"] = sku_el.text(strip=True) if sku_el else None

        # Availability
        avail_el = tree.css_first(pp.get("availability", "span#product-availability"))
        if avail_el:
            avail_text = avail_el.text(strip=True)
            data["availability"] = avail_text
            avail_lower = avail_text.lower()
            data["available"] = (
                ("disponible" in avail_lower or "en stock" in avail_lower or "in stock" in avail_lower)
                and "rupture" not in avail_lower
                and "epuisé" not in avail_lower
                and "indisponible" not in avail_lower
            )
        else:
            data["availability"] = None
            data["available"] = None

        # Description
        desc_el = tree.css_first(pp.get("description", "[itemprop='description']"))
        data["description"] = desc_el.text(strip=True) if desc_el else None

        # Images
        images = []
        main_img = tree.css_first(pp.get("image_main", "img.js-qv-product-cover"))
        if main_img:
            src = main_img.attributes.get("data-image-large-src") or main_img.attributes.get("src")
            if src:
                images.append(src)

        thumb_attr = pp.get("image_thumb_attr", "data-image-large-src")
        for thumb in tree.css(pp.get("image_thumbnails", "img.thumb.js-thumb")):
            src = thumb.attributes.get(thumb_attr) or thumb.attributes.get("src")
            if src and src not in images:
                images.append(src)

        data["images"] = images if images else None

        return data


def get_scraper(logger: logging.Logger) -> AllaniScraper:
    return AllaniScraper(logger)
