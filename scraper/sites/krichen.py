#!/usr/bin/env python3
"""
Krichen-distribution.tn scraper — WordPress + WooCommerce + Woodmart theme, httpx.
No Cloudflare. Infinite scroll on frontend but WooCommerce serves paginated HTML at /page/{N}/.
"""

import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser
from scraper.base import FastScraper


class KrichenScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("krichen", logger)

    # ------------------------------------------------------------------
    # Pagination — WooCommerce: /category/page/{N}/
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"/page/\d+/?$", "", base_url.rstrip("/"))
        return f"{base}/page/{page_num}/"

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen_urls = set()

        top_items = tree.css(
            "ul#menu-main-navigation.wd-nav-main > li.menu-item, "
            "ul#menu-main-navigation > li.menu-item, "
            "ul.wd-nav-main > li.menu-item"
        )
        if not top_items:
            top_items = tree.css("nav ul > li.menu-item")

        self.logger.info(f"Found {len(top_items)} top-level menu items")

        for li in top_items:
            a = li.css_first("a.woodmart-nav-link, > a")
            if not a:
                continue
            href = a.attributes.get("href", "")
            name_el = a.css_first("span.nav-link-text") or a
            name = name_el.text(strip=True)
            if not name or not href or href in ("#", "javascript:void(0)"):
                continue
            if any(x in href for x in ("cart", "panier", "account", "checkout", "contact", "blog")):
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            top_cat = {"name": name, "url": href, "level": "top", "low_level_categories": []}

            sub_menu = li.css_first("ul.wd-sub-menu, ul.sub-menu")
            if sub_menu:
                for sub_li in sub_menu.css("li.menu-item"):
                    sub_a = sub_li.css_first("a.woodmart-nav-link, > a")
                    if not sub_a:
                        continue
                    sub_href = sub_a.attributes.get("href", "")
                    sub_name_el = sub_a.css_first("span.nav-link-text") or sub_a
                    sub_name = sub_name_el.text(strip=True)
                    if not sub_name or not sub_href or sub_href in seen_urls:
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

        self.logger.info(f"Extracted {stats['top_level']} top, {stats['low_level']} low categories")
        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_ids = set()

        for card in tree.css("div.wd-product.wd-col, li.product.type-product, div.product-grid-item"):
            classes = card.attributes.get("class", "")
            pid_match = re.search(r"\bpost-(\d+)\b", classes)
            product_id = pid_match.group(1) if pid_match else None

            if product_id and product_id in seen_ids:
                continue
            if product_id:
                seen_ids.add(product_id)

            title_el = card.css_first("h2.wd-entities-title > a, h3.wd-entities-title > a, h2 a, h3 a")
            if not title_el:
                continue
            href = title_el.attributes.get("href", "")
            name = title_el.text(strip=True)

            ins_el = card.css_first("span.price ins span.woocommerce-Price-amount bdi")
            del_el = card.css_first("span.price del span.woocommerce-Price-amount bdi")
            price_el = card.css_first("span.price > span.woocommerce-Price-amount bdi, span.woocommerce-Price-amount bdi")

            if ins_el and del_el:
                price = self._parse_price(ins_el.text())
                old_price = self._parse_price(del_el.text())
            else:
                price = self._parse_price(price_el.text() if price_el else None)
                old_price = None

            img = card.css_first("img.attachment-woocommerce_thumbnail, img.wp-post-image, img")
            image = None
            if img:
                image = img.attributes.get("data-src") or img.attributes.get("src")
                if image and image.startswith("data:"):
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
    # Price parsing — WooCommerce TN: "1,200 TND" → 1200
    # ------------------------------------------------------------------

    def _parse_price(self, text: str) -> Optional[float]:
        if not text:
            return None
        # Krichen format: "95,00 TND" — comma is decimal (French locale)
        cleaned = re.sub(r"[^\d,.]", "", text.replace("\xa0", "").replace(" ", "")).strip()
        if not cleaned:
            return None
        if "," in cleaned and "." not in cleaned:
            # "95,00" → decimal  |  "1200,00" → decimal  |  "1.200,00" handled below
            cleaned = cleaned.replace(",", ".")
        elif "," in cleaned and "." in cleaned:
            # "1.200,00" → remove dot (thousands), comma=decimal
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
        # Primary: lazy-loaded product image has data-src set
        for img in tree.css("img[data-src*='wp-content/uploads']"):
            src = img.attributes.get("data-large_image") or img.attributes.get("data-src") or img.attributes.get("src")
            if src and "logo" not in src.lower() and src not in images:
                images.append(src)
        # Fallback: standard gallery
        if not images:
            for img in tree.css("div.woocommerce-product-gallery__image img, figure.woocommerce-product-gallery__wrapper img"):
                src = img.attributes.get("data-large_image") or img.attributes.get("src")
                if src and "logo" not in src.lower() and src not in images:
                    images.append(src)
        # Last fallback: attachment-large (the actual product image WordPress stores)
        if not images:
            for img in tree.css("img.attachment-large, img.wp-post-image"):
                src = img.attributes.get("src", "")
                if src and "logo" not in src.lower() and src not in images:
                    images.append(src)
        data["images"] = images[:10]

        return data


def get_scraper(logger: logging.Logger) -> KrichenScraper:
    return KrichenScraper(logger)
