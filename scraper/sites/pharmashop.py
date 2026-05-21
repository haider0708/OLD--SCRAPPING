#!/usr/bin/env python3
"""
Pharma-shop.tn specific scraper implementation.
NOTE: This site is rate-limited — use a single worker.
"""
import json
import logging
import re
from typing import List, Dict, Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper


class PharmashopScraper(FastScraper):
    """HTTPX/selectolax-based scraper for pharma-shop.tn (PrestaShop + Leo Theme)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("pharmashop", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract categories from pharma-shop.tn ApMegamenu horizontal menu."""
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories = []

        top_blocks = tree.css(
            fp.get("top_level_blocks", "ul.megamenu.horizontal > li.nav-item.parent.dropdown")
        )
        self.logger.info(f"Found {len(top_blocks)} top-level menu items")

        for top_block in top_blocks:
            top_link = top_block.css_first(
                fp.get("top_level_link", "a.nav-link.dropdown-toggle")
            )
            if not top_link:
                continue

            name_el = top_block.css_first(fp.get("top_level_name", "a.nav-link.dropdown-toggle span.menu-title"))
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

            # Low-level links from dropdown inner columns
            low_links = top_block.css(
                fp.get(
                    "low_level_links",
                    "div.dropdown-menu-inner ul.row > li.col-md-4 > a",
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

                # Subcategories: nested ul under the same column li
                parent_li = link.parent
                if parent_li:
                    sub_links = parent_li.css(
                        fp.get("subcategory_items", "ul > li > a")
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

        items = tree.css(
            cp.get("item_selector", "article.product-miniature.js-product-miniature")
        )

        for item in items:
            product_id = item.attributes.get(
                cp.get("item_id_attr", "data-id-product")
            )

            # Name and URL
            name_el = item.css_first(
                cp.get("item_name", "h2.h3.product-title[itemprop='name'] a")
            )
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

            # Price — try aria-label span first, then meta itemprop
            price_el = item.css_first(
                cp.get("item_price", "span.price[aria-label='Prix']")
            )
            if price_el:
                price_text = re.sub(r"[^\d.,]", "", price_el.text()).replace(",", ".")
                try:
                    product_data["price"] = float(price_text) if price_text else None
                except ValueError:
                    product_data["price"] = None
            else:
                meta_price = item.css_first("meta[itemprop='price'][content]")
                if meta_price:
                    try:
                        product_data["price"] = float(meta_price.attributes.get("content", ""))
                    except (ValueError, TypeError):
                        product_data["price"] = None
                else:
                    product_data["price"] = None

            # Old price
            old_price_el = item.css_first(
                cp.get("item_old_price", "span.regular-price[aria-label='Prix de base']")
            )
            if old_price_el:
                old_text = re.sub(r"[^\d.,]", "", old_price_el.text()).replace(",", ".")
                try:
                    product_data["old_price"] = float(old_text) if old_text else None
                except ValueError:
                    product_data["old_price"] = None

            # Brand
            brand_el = item.css_first(
                cp.get("item_brand", "div.text-center.txt-marque a")
            )
            if brand_el:
                product_data["brand"] = brand_el.text(strip=True)

            # Out of stock flag
            oos_el = item.css_first("ul.product-flags > li.product-flag.out_of_stock")
            if oos_el:
                product_data["available"] = False
                product_data["availability"] = oos_el.text(strip=True) or "Rupture de stock"
            else:
                product_data["available"] = True

            # Image
            img_el = item.css_first(
                cp.get("item_image", "a.thumbnail.product-thumbnail img")
            )
            if img_el:
                image_url = None
                for attr in cp.get(
                    "item_image_attrs", ["src", "data-src", "data-full-size-image-url"]
                ):
                    image_url = img_el.attributes.get(attr)
                    if image_url and not image_url.startswith("data:"):
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

        page_links = tree.css("ul.page-list li a.js-search-link")
        for el in page_links:
            try:
                num = int(el.text(strip=True))
                if num > total_pages:
                    total_pages = num
            except (ValueError, TypeError):
                continue

        # Current page from active/disabled element
        active_el = tree.css_first(
            "ul.page-list li.current a, ul.page-list li.active a"
        )
        if active_el:
            try:
                current_page = int(active_el.text(strip=True))
            except (ValueError, TypeError):
                pass

        next_link = tree.css_first(
            cp.get("pagination_next", "a.next.js-search-link[rel='next']")
        )
        if next_link and "disabled" not in next_link.attributes.get("class", ""):
            has_next = True

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape product details — JSON-primary from data-product attr, with HTML fallback."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = {"url": url}

        # === Try JSON data first ===
        json_el = tree.css_first(
            pp.get("json_data", "div.tab-pane#product-details[data-product]")
        )
        json_parsed = False
        if json_el:
            raw_json = json_el.attributes.get("data-product")
            if raw_json:
                try:
                    pdata = json.loads(raw_json)
                    json_parsed = True

                    data["product_id"] = str(pdata.get("id_product") or pdata.get("id", ""))
                    data["title"] = pdata.get("name")
                    data["reference"] = pdata.get("reference")

                    # Price
                    price_amount = pdata.get("price_amount")
                    if price_amount is not None:
                        try:
                            data["price"] = float(price_amount)
                        except (ValueError, TypeError):
                            data["price"] = None
                    else:
                        data["price"] = None

                    # Old price / discount
                    price_without_reduction = pdata.get("price_without_reduction")
                    if price_without_reduction is not None:
                        try:
                            old_val = float(price_without_reduction)
                            if data.get("price") and old_val > data["price"]:
                                data["old_price"] = old_val
                        except (ValueError, TypeError):
                            pass

                    discount_amount = pdata.get("discount_amount")
                    if discount_amount is not None:
                        try:
                            data["discount"] = float(discount_amount)
                        except (ValueError, TypeError):
                            pass

                    # Quantity / availability
                    quantity = pdata.get("quantity", 0)
                    available_for_order = pdata.get("available_for_order", True)
                    data["quantity"] = quantity
                    if quantity > 0:
                        data["availability"] = "En stock"
                        data["available"] = True
                    else:
                        data["availability"] = "En rupture"
                        data["available"] = available_for_order

                    # Images from JSON
                    images_data = pdata.get("images", {})
                    images = []
                    img_items = images_data.values() if isinstance(images_data, dict) else images_data if isinstance(images_data, list) else []
                    for img_info in img_items:
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
            title_el = tree.css_first(pp.get("title", "h1.h1[itemprop='name']"))
            if title_el:
                data["title"] = title_el.text(strip=True)

        if not json_parsed or data.get("price") is None:
            price_el = tree.css_first(
                pp.get("price", "div.current-price span[itemprop='price']")
            )
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

        if not json_parsed or not data.get("product_id"):
            url_match = re.search(r"[\-/](\d+)[\-.]", url)
            if url_match:
                data["product_id"] = url_match.group(1)

        # Old price fallback
        if not data.get("old_price"):
            old_price_el = tree.css_first(
                pp.get("old_price", "div.product-discount span.regular-price")
            )
            if old_price_el:
                old_text = re.sub(r"[^\d.,]", "", old_price_el.text()).replace(",", ".")
                try:
                    data["old_price"] = float(old_text) if old_text else None
                except ValueError:
                    data["old_price"] = None

        # Brand from manufacturer logo alt
        if not data.get("brand"):
            brand_el = tree.css_first(
                pp.get("brand", "div.product-manufacturer img.manufacturer-logo")
            )
            if brand_el:
                data["brand"] = brand_el.attributes.get("alt")

        # Description fallback
        if not data.get("description"):
            desc_el = tree.css_first(
                pp.get("description", "div.product-description[itemprop='description']")
            )
            if desc_el:
                data["description"] = desc_el.text(strip=True)

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
            main_img = tree.css_first(
                pp.get("image_main", "div.product-cover img.js-qv-product-cover")
            )
            if main_img:
                src = main_img.attributes.get("src")
                if src:
                    images.append(src)

            for thumb in tree.css(
                pp.get("image_thumbnails", "ul.product-images.js-qv-product-images img.thumb")
            ):
                src = thumb.attributes.get("data-image-large-src") or thumb.attributes.get("src")
                if src and src not in images:
                    images.append(src)

            data["images"] = images if images else None

        return data


def get_scraper(logger: logging.Logger) -> PharmashopScraper:
    return PharmashopScraper(logger)
