#!/usr/bin/env python3
"""
Parashop.tn specific scraper implementation.
"""
import json
import logging
import re
from typing import List, Dict, Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper
from scraper.product_utils import (
    absolute_url,
    dedupe_products,
    finalize_product_record,
    html_product_metadata,
    parse_price,
)


class ParashopScraper(FastScraper):
    """HTTPX/selectolax-based scraper for parashop.tn (OpenCart + Journal 3)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("parashop", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract categories from parashop.tn Journal 3 j-menu."""
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories = []

        top_blocks = tree.css(
            fp.get("top_level_blocks", "ul.j-menu > li.menu-item.main-menu-item.dropdown.mega-menu")
        )
        self.logger.info(f"Found {len(top_blocks)} top-level menu items")

        for top_block in top_blocks:
            top_link = top_block.css_first(
                fp.get("top_level_link", "a.dropdown-toggle")
            )
            if not top_link:
                continue

            name_el = top_block.css_first(fp.get("top_level_name", "a.dropdown-toggle span.links-text"))
            top_name = name_el.text(strip=True) if name_el else top_link.text(strip=True)
            top_url = top_link.attributes.get("href", "")

            if not top_name:
                continue
            if top_url and not top_url.startswith("http"):
                top_url = urljoin(self.base_url, top_url)

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            seen_urls = set()

            # Low-level: Journal 3 mega-menu catalog titles
            low_links = top_block.css(
                fp.get(
                    "low_level_links",
                    "div.dropdown-menu.j-dropdown div.module-item > div.item-content > a.catalog-title",
                )
            )
            for link in low_links:
                name = link.text(strip=True)
                url = link.attributes.get("href", "")
                if not name or not url:
                    continue
                if not url.startswith("http"):
                    url = urljoin(self.base_url, url)
                if url in seen_urls:
                    continue

                low_cat = {
                    "name": name,
                    "url": url,
                    "level": "low",
                    "subcategories": [],
                }

                # Subcategories: subitems under item-assets
                parent_item = link.parent  # div.item-content
                if parent_item:
                    module_item = parent_item.parent  # div.module-item
                    if module_item:
                        sub_links = module_item.css(
                            fp.get(
                                "subcategory_items",
                                "div.item-assets > div.subitems > div.subitem > a",
                            )
                        )
                        for sub_link in sub_links:
                            sub_name = sub_link.text(strip=True)
                            sub_url = sub_link.attributes.get("href", "")
                            if not sub_name or not sub_url:
                                continue
                            if not sub_url.startswith("http"):
                                sub_url = urljoin(self.base_url, sub_url)
                            if sub_url not in seen_urls:
                                low_cat["subcategories"].append({
                                    "name": sub_name,
                                    "url": sub_url,
                                    "level": "subcategory",
                                })
                                seen_urls.add(sub_url)

                top_cat["low_level_categories"].append(low_cat)
                seen_urls.add(url)

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

        items = tree.css(cp.get("item_selector", "div.product-layout.has-extra-button"))

        for item in items:
            # Product ID from hidden input
            id_el = item.css_first(
                cp.get("item_id_selector", "input[type='hidden'][name='product_id']")
            )
            product_id = id_el.attributes.get("value") if id_el else None

            # Name and URL
            name_el = item.css_first(cp.get("item_name", "div.caption > div.name > a"))
            if not name_el:
                continue
            product_name = name_el.text(strip=True)
            product_url = name_el.attributes.get("href", "")
            if not product_url:
                continue
            if not product_url.startswith("http"):
                product_url = urljoin(self.base_url, product_url)

            product_data: Dict[str, Any] = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }

            # Price
            price_el = item.css_first(cp.get("item_price", "span.price-new"))
            if price_el:
                product_data["price"] = parse_price(price_el.text())
            else:
                product_data["price"] = None

            # Old price
            old_price_el = item.css_first(cp.get("item_old_price", "span.price-old"))
            if old_price_el:
                product_data["old_price"] = parse_price(old_price_el.text())

            # Brand
            brand_el = item.css_first(
                cp.get("item_brand", "div.caption > div.stats span.stat-1 span a")
            )
            if brand_el:
                product_data["brand"] = brand_el.text(strip=True)

            # Image
            img_el = item.css_first(cp.get("item_image", "a.product-img img.img-first"))
            if img_el:
                image_url = None
                for attr in cp.get("item_image_attrs", ["src", "data-src", "data-largeimg"]):
                    image_url = img_el.attributes.get(attr)
                    if image_url and not image_url.startswith("data:"):
                        break
                if image_url:
                    image_url = absolute_url(image_url, self.base_url)
                    if image_url:
                        product_data["image"] = image_url

            products.append(finalize_product_record(product_data))

        return dedupe_products(products, self.logger, "parashop listing")

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})

        current_page = 1
        total_pages = 1
        has_next = False

        # Parse page numbers from pagination links
        page_links = tree.css("ul.pagination li a")
        for el in page_links:
            href = el.attributes.get("href", "")
            page_match = re.search(r"[?&]page=(\d+)", href)
            if page_match:
                num = int(page_match.group(1))
                if num > total_pages:
                    total_pages = num

        # Current page from active element
        active_el = tree.css_first("ul.pagination li.active span, ul.pagination li.active a")
        if active_el:
            try:
                current_page = int(active_el.text(strip=True))
            except (ValueError, TypeError):
                pass

        # Next page link
        next_link = tree.css_first(cp.get("pagination_next", "li > a.next"))
        if next_link:
            has_next = True

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape product details from parashop.tn OpenCart product page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = {"url": url}

        # Title
        title_el = tree.css_first(pp.get("title", "div.product-details > div.title.page-title"))
        if title_el:
            data["title"] = title_el.text(strip=True)

        # SKU / model
        sku_el = tree.css_first(pp.get("sku", "li.product-model > span"))
        if sku_el:
            data["reference"] = sku_el.text(strip=True)

        mpn_el = tree.css_first("li.product-mpn > span, li.product-upc > span, li.product-ean > span")
        if mpn_el:
            data["mpn"] = mpn_el.text(strip=True)

        # Price
        price_el = tree.css_first(
            pp.get("price", "div.product-price-group div.product-price-new")
        ) or tree.css_first("div.product-price-group div.product-price, div.product-price-group .product-price-new")
        if price_el:
            data["price"] = parse_price(price_el.text())
        else:
            data["price"] = None

        # Old price
        old_price_el = tree.css_first(
            pp.get("old_price", "div.product-price-group div.product-price-old")
        )
        if old_price_el:
            data["old_price"] = parse_price(old_price_el.text())

        # Availability
        in_stock_el = tree.css_first(
            pp.get("availability_in", pp.get("availability_in_stock", "li.product-stock.in-stock > span"))
        )
        out_stock_el = tree.css_first(
            pp.get("availability_out", pp.get("availability_out_of_stock", "li.product-stock.out-of-stock > span"))
        )
        if in_stock_el:
            data["availability"] = in_stock_el.text(strip=True)
            data["available"] = True
        elif out_stock_el:
            data["availability"] = out_stock_el.text(strip=True)
            data["available"] = False

        # Brand
        brand_el = tree.css_first(
            pp.get("brand", "div.brand-image.product-manufacturer a span")
        )
        if brand_el:
            data["brand"] = brand_el.text(strip=True)

        # Description
        desc_el = tree.css_first(
            pp.get("description", "div.tabs-container.product_tabs div.tab-pane.active")
        )
        if desc_el:
            data["description"] = desc_el.text(strip=True)

        # Product ID from hidden input or URL fallback
        product_id_el = tree.css_first("#product-id[value], input[name='product_id'][value]")
        if product_id_el:
            data["product_id"] = product_id_el.attributes.get("value")
        if not data.get("product_id"):
            url_match = re.search(r"product_id=(\d+)", url)
            if not url_match:
                url_match = re.search(r"[\-/](\d+)(?:[\-.]|$)", url)
            if url_match:
                data["product_id"] = url_match.group(1)

        # === Images ===
        images = []

        # Try lightgallery first (Journal 3 pattern)
        gallery_el = tree.css_first(
            pp.get("lightgallery", "div.lightgallery.lightgallery-product-images")
        )
        if gallery_el:
            raw_images = gallery_el.attributes.get("data-images")
            if raw_images:
                try:
                    images_data = json.loads(raw_images)
                    if isinstance(images_data, list):
                        for img_obj in images_data:
                            if isinstance(img_obj, dict):
                                img_url = img_obj.get("image") or img_obj.get("src") or img_obj.get("thumb")
                                if img_url and img_url not in images:
                                    img_url = absolute_url(img_url, self.base_url)
                                    if img_url:
                                        images.append(img_url)
                except (json.JSONDecodeError, TypeError):
                    self.logger.debug(f"Failed to parse lightgallery JSON for {url}")

        # Fallback: swiper main-image slides
        if not images:
            for slide_img in tree.css(
                pp.get("image_swiper", "div.swiper.main-image div.swiper-slide img")
            ):
                img_url = slide_img.attributes.get("data-largeimg") or slide_img.attributes.get("src")
                if img_url and not img_url.startswith("data:"):
                    img_url = absolute_url(img_url, self.base_url)
                    if img_url and img_url not in images:
                        images.append(img_url)

        data["images"] = images if images else None

        metadata = html_product_metadata(html, url, self.base_url)
        for key, value in metadata.items():
            if key in {"barcode", "reference", "sku", "currency"}:
                data[key] = value
            elif data.get(key) in (None, "", [], {}):
                data[key] = value

        return finalize_product_record(data)


def get_scraper(logger: logging.Logger) -> ParashopScraper:
    return ParashopScraper(logger)
