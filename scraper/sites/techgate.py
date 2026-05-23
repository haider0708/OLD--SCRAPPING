#!/usr/bin/env python3
"""
Techgate.tn scraper — WordPress + WooCommerce + Woodmart theme, httpx.
"""

import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser
from scraper.base import FastScraper


class TechgateScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("techgate", logger)

    # ------------------------------------------------------------------
    # Pagination — WooCommerce: /product-category/{slug}/page/{N}/
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"/page/\d+/?$", "", base_url.rstrip("/"))
        return f"{base}/page/{page_num}/"

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    @staticmethod
    def _first_child_link(node):
        """Return the first direct-child <a> of a node (or None)."""
        for child in node.iter():
            if child.tag == "a":
                return child
        return None

    def _absolute_url(self, href: str) -> str:
        """Convert relative paths to absolute URLs using the site's base."""
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return f"https://techgate.tn{href}"
        return href

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen_urls = set()

        # Vertical sidebar category menu (primary)
        top_items = tree.css("ul#menu-categories.wd-nav-vertical > li.menu-item, ul#menu-categories > li.menu-item")
        if not top_items:
            # Fallback: horizontal main nav
            top_items = tree.css("nav.wd-header-main-nav ul.wd-nav-main > li.menu-item, ul.wd-nav-main > li.menu-item")

        self.logger.info(f"Found {len(top_items)} top-level menu items")

        for li in top_items:
            a = self._first_child_link(li)
            if not a:
                continue
            href = a.attributes.get("href", "")
            name_el = a.css_first("span.nav-link-text") or a
            name = name_el.text(strip=True)
            if not name or not href or href == "#":
                continue
            href = self._absolute_url(href)
            if href in seen_urls:
                continue
            seen_urls.add(href)

            top_cat = {"name": name, "url": href, "level": "top", "low_level_categories": []}

            # Sub-menu items
            sub_menu = li.css_first("ul.wd-sub-menu, ul.sub-menu")
            if sub_menu:
                for sub_li in sub_menu.css("li"):
                    sub_a = self._first_child_link(sub_li)
                    if not sub_a:
                        continue
                    sub_href = sub_a.attributes.get("href", "")
                    sub_name_el = sub_a.css_first("span.nav-link-text") or sub_a
                    sub_name = sub_name_el.text(strip=True)
                    if not sub_name or not sub_href:
                        continue
                    sub_href = self._absolute_url(sub_href)
                    if sub_href in seen_urls:
                        continue
                    seen_urls.add(sub_href)
                    top_cat["low_level_categories"].append({
                        "name": sub_name,
                        "url": sub_href,
                        "level": "low",
                        "subcategories": [],
                    })

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

        self.logger.info(f"Extracted {stats['top_level']} top, {stats['low_level']} low categories ({stats['total_urls']} URLs)")
        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_ids = set()

        for card in tree.css("div.wd-product.wd-col, li.product.type-product"):
            # Product ID from class like "post-1234"
            classes = card.attributes.get("class", "")
            pid_match = re.search(r"\bpost-(\d+)\b", classes)
            product_id = pid_match.group(1) if pid_match else None

            if product_id and product_id in seen_ids:
                continue
            if product_id:
                seen_ids.add(product_id)

            # URL and name
            title_el = card.css_first("h2.wd-entities-title > a, h3.wd-entities-title > a, h2 a, h3 a")
            if not title_el:
                continue
            href = title_el.attributes.get("href", "")
            name = title_el.text(strip=True)

            # Price
            ins_el = card.css_first("span.price ins span.woocommerce-Price-amount bdi")
            del_el = card.css_first("span.price del span.woocommerce-Price-amount bdi")
            price_el = card.css_first("span.price > span.woocommerce-Price-amount bdi, span.woocommerce-Price-amount bdi")

            if ins_el and del_el:
                price = self._parse_price(ins_el.text())
                old_price = self._parse_price(del_el.text())
            else:
                price = self._parse_price(price_el.text() if price_el else None)
                old_price = None

            # Image
            img = card.css_first("img.attachment-woocommerce_thumbnail, img.wp-post-image, img")
            image = None
            if img:
                image = img.attributes.get("data-src") or img.attributes.get("src")
                if image and (image.startswith("data:") or "logo" in image):
                    image = None

            products.append({
                "id": product_id,
                "url": href,
                "name": name,
                "price": price,
                "old_price": old_price,
                "image": image,
            })

        return products

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        current_page = 1

        for a in tree.css("nav.woocommerce-pagination ul.page-numbers a.page-numbers, ul.page-numbers a.page-numbers"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass

        current_el = tree.css_first("ul.page-numbers li span.current, ul.page-numbers .current")
        if current_el:
            try:
                current_page = int(current_el.text(strip=True))
                if current_page > max_page:
                    max_page = current_page
            except ValueError:
                pass

        has_next = tree.css_first("a.next.page-numbers") is not None
        return {"current_page": current_page, "total_pages": max_page, "has_next": has_next}

    # ------------------------------------------------------------------
    # Price parsing — Woodmart TN: "59,900 DT" → 59900
    # ------------------------------------------------------------------

    def _parse_price(self, text: str) -> Optional[float]:
        if not text:
            return None
        # Techgate format: "1 499,000 DT" — space=thousands, comma=decimal
        # Strip currency symbols, spaces, non-breaking spaces
        cleaned = re.sub(r"[^\d,.]", "", text.replace("\xa0", "").replace(" ", "")).strip()
        if not cleaned:
            return None
        # "1499,000" — comma is decimal separator (French locale)
        if "," in cleaned and "." not in cleaned:
            # Check if it looks like a decimal: digits,3digits at end → decimal
            # e.g. "1499,000" → 1499.0  vs "1,200" (ambiguous — treat as decimal too)
            cleaned = cleaned.replace(",", ".")
        elif "," in cleaned and "." in cleaned:
            # both separators: "1.499,00" → remove dots, comma=decimal
            cleaned = cleaned.replace(".", "").replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Product detail
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Product ID from body class postid-{N}
        body = tree.css_first("body")
        if body:
            pid_match = re.search(r"\bpostid-(\d+)\b", body.attributes.get("class", ""))
            data["product_id"] = pid_match.group(1) if pid_match else None

        title_el = tree.css_first("h1.product_title.entry-title, h1.product_title, h1")
        data["title"] = title_el.text(strip=True) if title_el else None

        sku_el = tree.css_first(".sku_wrapper span.sku, span.sku")
        data["sku"] = sku_el.text(strip=True) if sku_el else None

        brand_el = tree.css_first("div.product-brands a, .brand a")
        data["brand"] = brand_el.text(strip=True) if brand_el else None
        if not data["brand"]:
            brand_img = tree.css_first("div.product-brands img, .brand img")
            data["brand"] = brand_img.attributes.get("alt") if brand_img else None

        ins_el = tree.css_first("p.price ins span.woocommerce-Price-amount bdi")
        del_el = tree.css_first("p.price del span.woocommerce-Price-amount bdi")
        price_el = tree.css_first("p.price span.woocommerce-Price-amount bdi")

        if ins_el and del_el:
            data["price"] = self._parse_price(ins_el.text())
            data["old_price"] = self._parse_price(del_el.text())
        else:
            data["price"] = self._parse_price(price_el.text() if price_el else None)
            data["old_price"] = None

        if data.get("old_price") and data.get("price") and data["old_price"] != data["price"]:
            data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)

        stock_el = tree.css_first("p.stock.in-stock")
        if stock_el:
            data["availability"] = stock_el.text(strip=True)
            data["available"] = True
        else:
            oos_el = tree.css_first("p.stock.out-of-stock")
            if oos_el:
                data["availability"] = oos_el.text(strip=True)
                data["available"] = False
            else:
                data["availability"] = None
                data["available"] = None

        desc_el = tree.css_first("div.woocommerce-product-details__short-description")
        data["description"] = desc_el.text(strip=True) if desc_el else None

        specs = {}
        for row in tree.css("table.shop_attributes tr, table.woocommerce-product-attributes tr"):
            key_el = row.css_first("th")
            val_el = row.css_first("td")
            if key_el and val_el:
                k = key_el.text(strip=True)
                v = val_el.text(strip=True)
                if k and v:
                    specs[k] = v
        data["specifications"] = specs

        images = []
        for img in tree.css("div.woocommerce-product-gallery__image img, figure.woocommerce-product-gallery__wrapper img"):
            src = img.attributes.get("data-large_image") or img.attributes.get("data-src") or img.attributes.get("src")
            if src and "logo" not in src and src not in images:
                images.append(src)
        # Fallback: any product upload image
        if not images:
            for img in tree.css("img[src*='wp-content/uploads']"):
                src = img.attributes.get("src", "")
                if src and "logo" not in src and src not in images:
                    images.append(src)
        data["images"] = images[:10]

        return data


def get_scraper(logger: logging.Logger) -> TechgateScraper:
    return TechgateScraper(logger)
