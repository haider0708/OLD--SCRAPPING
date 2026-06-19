#!/usr/bin/env python3
"""
Parafendri.tn specific scraper implementation.
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


class ParafendriScraper(FastScraper):
    """HTTPX/selectolax-based scraper for parafendri.tn (PrestaShop + PosThemes MegaMenu)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("parafendri", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract categories from parafendri.tn PosThemes MegaMenu with mega/simple/sub levels."""
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories = []

        top_blocks = tree.css(fp.get("top_level_blocks", "div#_desktop_megamenu ul.menu-content > li.menu-item, div#_desktop_verticalmenu ul.menu-content > li.menu-item"))
        self.logger.info(f"Found {len(top_blocks)} top-level menu items")

        for top_block in top_blocks:
            top_link = top_block.css_first(fp.get("top_level_link", "a"))
            if not top_link:
                continue

            # Name from span inside link, or link text
            name_el = top_block.css_first(fp.get("top_level_name", "a > span"))
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

            # Mega menu low-level links
            mega_links = top_block.css(fp.get("mega_low", "div.avaorg-sub-menu.menu-dropdown div.avaorg-menu-col > ul.ul-column > li.submenu-item > a"))
            for link in mega_links:
                name = link.text(strip=True)
                url = link.attributes.get("href", "")
                if not name or not url:
                    continue
                if url and not url.startswith("http"):
                    url = urljoin(self.base_url, url)
                if url in seen_urls:
                    continue

                low_cat = {
                    "name": name,
                    "url": url,
                    "level": "low",
                    "subcategories": [],
                }

                # Subcategories under this mega item
                parent_li = link.parent
                if parent_li:
                    sub_links = parent_li.css(fp.get("subcategory_items", "ul.category-sub-menu > li > a"))
                    for sub_link in sub_links:
                        sub_name = sub_link.text(strip=True)
                        sub_url = sub_link.attributes.get("href", "")
                        if not sub_name or not sub_url:
                            continue
                        if sub_url and not sub_url.startswith("http"):
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

            # Simple dropdown low-level links
            simple_links = top_block.css(fp.get("simple_low", "div.avaorg-sub-menu.menu-dropdown li.submenu-item > a"))
            for link in simple_links:
                name = link.text(strip=True)
                url = link.attributes.get("href", "")
                if not name or not url:
                    continue
                if url and not url.startswith("http"):
                    url = urljoin(self.base_url, url)
                if url not in seen_urls:
                    top_cat["low_level_categories"].append({
                        "name": name,
                        "url": url,
                        "level": "low",
                        "subcategories": [],
                    })
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

        items = tree.css(cp.get("item_selector", "article.product-miniature.js-product-miniature"))

        for item in items:
            product_id = item.attributes.get(cp.get("item_id_attr", "data-id-product"))

            name_el = item.css_first(
                cp.get("item_name", "h2.product-title a")
            ) or item.css_first("h3.product-title a, h3.h3.product-title a, .product-title a")
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
            price_el = item.css_first(cp.get("item_price", "div.product-price-and-shipping span.price"))
            if price_el:
                price_content = price_el.attributes.get("content")
                if price_content:
                    try:
                        product_data["price"] = float(price_content)
                    except ValueError:
                        product_data["price"] = None
                else:
                    product_data["price"] = parse_price(price_el.text())
            else:
                product_data["price"] = None

            # Old price
            old_price_el = item.css_first(cp.get("item_old_price", "span.regular-price"))
            if old_price_el:
                product_data["old_price"] = parse_price(old_price_el.text())

            # Availability from listing
            in_stock_el = item.css_first("div.availability-list.in-stock > span")
            out_stock_el = item.css_first("div.availability-list.out-of-stock > span")
            if in_stock_el:
                product_data["availability"] = in_stock_el.text(strip=True)
                product_data["available"] = True
            elif out_stock_el:
                product_data["availability"] = out_stock_el.text(strip=True)
                product_data["available"] = False

            # Image
            img_el = item.css_first(cp.get("item_image", "a.thumbnail.product-thumbnail > img"))
            if img_el:
                image_url = None
                for attr in cp.get("item_image_attrs", ["data-src", "data-full-size-image-url", "src"]):
                    image_url = img_el.attributes.get(attr)
                    if image_url and not image_url.startswith("data:"):
                        break
                if image_url:
                    image_url = absolute_url(image_url, self.base_url)
                    if image_url:
                        product_data["image"] = image_url

            products.append(finalize_product_record(product_data))

        if products:
            return dedupe_products(products, self.logger, "parafendri listing")

        # Fallback for theme/layout drifts
        for item in tree.css("article.product-miniature, div.js-product-miniature"):
            link = item.css_first("h2.product-title a, a.product-thumbnail, a[href]")
            if not link:
                continue
            url = link.attributes.get("href", "")
            if not url:
                continue
            products.append(
                finalize_product_record({
                    "id": item.attributes.get("data-id-product"),
                    "url": urljoin(self.base_url, url) if not url.startswith("http") else url,
                    "name": (link.text(strip=True) or "").strip(),
                    "price": None,
                })
            )
        return dedupe_products(products, self.logger, "parafendri listing fallback")

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})

        current_page = 1
        total_pages = 1
        has_next = False

        page_links = tree.css("ul.page-list li a.js-search-link")
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

        next_link = tree.css_first(cp.get("pagination_next", "a.next.js-search-link[rel='next']"))
        if next_link and "disabled" not in next_link.attributes.get("class", ""):
            has_next = True

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape product details — JSON-primary from #product-details data-product, with HTML fallback."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = {"url": url}

        # === Try JSON data first ===
        json_el = tree.css_first(pp.get("json_data", "#product-details[data-product]"))
        json_parsed = False
        if json_el:
            json_attr = pp.get("json_data_attr", "data-product")
            raw_json = json_el.attributes.get(json_attr)
            if raw_json:
                try:
                    pdata = json.loads(raw_json)
                    json_parsed = True

                    data["product_id"] = str(pdata.get("id_product") or pdata.get("id", ""))
                    data["title"] = pdata.get("name")
                    data["reference"] = pdata.get("reference")

                    # Price from JSON
                    price_amount = pdata.get("price_amount")
                    if price_amount is not None:
                        try:
                            data["price"] = float(price_amount)
                        except (ValueError, TypeError):
                            data["price"] = None
                    else:
                        data["price"] = None

                    # Quantity / availability
                    quantity = pdata.get("quantity", 0)
                    data["availability"] = "En stock" if quantity > 0 else "En rupture"
                    data["available"] = quantity > 0

                    # Images from JSON
                    images_data = pdata.get("images", {})
                    images = []
                    if isinstance(images_data, dict):
                        for img_info in images_data.values():
                            if isinstance(img_info, dict):
                                large = img_info.get("large", {})
                                img_url = large.get("url") if isinstance(large, dict) else None
                                if img_url and img_url not in images:
                                    images.append(img_url)
                    elif isinstance(images_data, list):
                        for img_info in images_data:
                            if isinstance(img_info, dict):
                                large = img_info.get("large", {})
                                img_url = large.get("url") if isinstance(large, dict) else None
                                if img_url and img_url not in images:
                                    images.append(img_url)
                    data["images"] = images if images else None

                    # Features from JSON
                    features_list = pdata.get("features", [])
                    if features_list and isinstance(features_list, list):
                        specs = {}
                        for feat in features_list:
                            if isinstance(feat, dict):
                                fname = feat.get("name", "")
                                fval = feat.get("value", "")
                                if fname and fval:
                                    specs[fname] = fval
                        data["specifications"] = specs if specs else None

                except (json.JSONDecodeError, TypeError):
                    self.logger.debug(f"Failed to parse JSON data for {url}")

        # === HTML fallback for missing fields ===
        if not json_parsed or not data.get("title"):
            title_el = tree.css_first(pp.get("title", "h1.h1.namne_details[itemprop='name']"))
            if title_el:
                data["title"] = title_el.text(strip=True)

        if not json_parsed or data.get("price") is None:
            price_el = tree.css_first(pp.get("price", "div.current-price > span[itemprop='price']"))
            if price_el:
                price_content = price_el.attributes.get(pp.get("price_content_attr", "content"))
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

        if not json_parsed or not data.get("product_id"):
            url_match = re.search(r"[\-/](\d+)[\-.]", url)
            if url_match:
                data["product_id"] = url_match.group(1)

        # Old price (HTML only)
        old_price_el = tree.css_first(pp.get("old_price", "div.product-discount > span.regular-price"))
        if old_price_el:
            old_text = re.sub(r"[^\d.,]", "", old_price_el.text()).replace(",", ".")
            try:
                data["old_price"] = float(old_text) if old_text else None
            except ValueError:
                data["old_price"] = None

        # Brand from manufacturer logo alt
        brand_el = tree.css_first(pp.get("brand", "div.product-manufacturer img.manufacturer-logo"))
        if brand_el:
            data["brand"] = brand_el.attributes.get(pp.get("brand_attr", "alt"))

        # SKU fallback
        if not data.get("reference"):
            sku_el = tree.css_first(pp.get("sku", "p.reference > span"))
            if sku_el:
                data["reference"] = sku_el.text(strip=True)

        # Availability fallback
        if not json_parsed or data.get("availability") is None:
            avail_el = tree.css_first("span#product-availability")
            if avail_el:
                avail_text = avail_el.text(strip=True)
                data["availability"] = avail_text
                avail_lower = avail_text.lower()
                data["available"] = (
                    ("disponible" in avail_lower or "en stock" in avail_lower)
                    and "rupture" not in avail_lower
                    and "epuisé" not in avail_lower
                )

        # Images fallback from HTML
        if not data.get("images"):
            images = []
            main_img = tree.css_first(pp.get("image_main", "div.product-cover.slider-for div.easyzoom img[itemprop='image']"))
            if main_img:
                src = main_img.attributes.get("src")
                if src:
                    images.append(absolute_url(src, self.base_url) or src)

            for thumb in tree.css(pp.get("image_thumbnails", "ul.product-images.slider-nav img.thumb.js-thumb")):
                src = thumb.attributes.get("data-image-large-src") or thumb.attributes.get("src")
                if src and src not in images:
                    images.append(absolute_url(src, self.base_url) or src)

            data["images"] = images if images else None

        metadata = html_product_metadata(html, url, self.base_url)
        for key, value in metadata.items():
            if key in {"barcode", "reference", "sku", "currency"}:
                data[key] = value
            elif data.get(key) in (None, "", [], {}):
                data[key] = value

        return finalize_product_record(data)


def get_scraper(logger: logging.Logger) -> ParafendriScraper:
    return ParafendriScraper(logger)
