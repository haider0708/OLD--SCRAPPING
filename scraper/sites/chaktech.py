#!/usr/bin/env python3
"""
Chaktech.tn scraper — WordPress + WooCommerce + Porto theme + Perfect WooCommerce Brands plugin.
Categories: /product-category/{slug}/, pagination: /page/N/, product URLs: /shop/{slug}/.
"""

import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser
from scraper.base import FastScraper


class ChaktechScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("chaktech", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"/page/\d+/?$", "", base_url.rstrip("/"))
        return f"{base}/page/{page_num}/"

    @staticmethod
    def _first_child_link(node):
        for child in node.iter():
            if child.tag == "a":
                return child
        return None

    def _absolute_url(self, href: str) -> str:
        if not href:
            return href
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return f"https://chaktech.tn{href}"
        return href

    @staticmethod
    def _is_category_url(href: str) -> bool:
        return "/product-category/" in href and "/page/" not in href

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen_urls = set()

        # Try main nav menu
        top_items = tree.css(
            "ul#menu-main-menu > li.menu-item, ul#menu-categories > li.menu-item, "
            "ul.main-menu > li.menu-item, ul.menu > li.menu-item-has-children"
        )

        for top_li in top_items:
            a = self._first_child_link(top_li)
            if not a:
                continue
            href = self._absolute_url(a.attributes.get("href", ""))
            if not href or not self._is_category_url(href) or href in seen_urls:
                continue
            name = a.text(strip=True)
            if not name:
                continue
            seen_urls.add(href)
            top_cat = {"name": name, "url": href, "level": "top", "low_level_categories": []}

            # Sub-categories
            for sub_li in top_li.css("ul.sub-menu li, ul.dropdown-menu li"):
                sub_a = self._first_child_link(sub_li)
                if not sub_a:
                    continue
                sub_href = self._absolute_url(sub_a.attributes.get("href", ""))
                if not sub_href or not self._is_category_url(sub_href) or sub_href in seen_urls:
                    continue
                sub_name = sub_a.text(strip=True)
                if not sub_name:
                    continue
                seen_urls.add(sub_href)
                top_cat["low_level_categories"].append({
                    "name": sub_name, "url": sub_href, "level": "low", "subcategories": [],
                })
            categories.append(top_cat)

        # Fallback: scan all /product-category/ links
        if len(seen_urls) < 5:
            for a in tree.css("a[href*='/product-category/']"):
                href = self._absolute_url(a.attributes.get("href", ""))
                if not href or not self._is_category_url(href) or href in seen_urls:
                    continue
                name = a.text(strip=True)
                if not name or len(name) > 80:
                    continue
                seen_urls.add(href)
                categories.append({"name": name, "url": href, "level": "top", "low_level_categories": []})

        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top["low_level_categories"]:
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
        self.logger.info(f"Extracted {stats['top_level']} top, {stats['low_level']} low")
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        cards = tree.css("div.porto-tb-item.product, li.product.type-product, section.product.type-product")

        for card in cards:
            cls = card.attributes.get("class", "")

            # URL — find first /shop/ link inside (avoid img alt links)
            url = None
            for a in card.css("a[href*='/shop/']"):
                href = a.attributes.get("href", "")
                if href and "?add-to-cart=" not in href:
                    url = self._absolute_url(href)
                    break
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            # Name — Porto puts it on [data-title] on the image wrapper
            name = ""
            title_holder = card.css_first("[data-title]")
            if title_holder:
                name = (title_holder.attributes.get("data-title") or "").strip()
            if not name:
                # Fallback: any title heading
                name_el = card.css_first("h2.woocommerce-loop-product__title, h3 a, .product-title a, .porto-tb-title a")
                if name_el:
                    name = name_el.text(strip=True)
            # The aria-label "post featured image" is a false positive — never use it as name.

            # Price — read from span.price block; ignore .onsale (discount badge)
            price = None
            old_price = None
            price_block = card.css_first("span.price")
            if price_block:
                ins_bdi = price_block.css_first("ins .woocommerce-Price-amount bdi, ins bdi")
                del_bdi = price_block.css_first("del .woocommerce-Price-amount bdi, del bdi")
                if ins_bdi and del_bdi:
                    price = self._parse_price(ins_bdi.text())
                    old_price = self._parse_price(del_bdi.text())
                else:
                    plain = price_block.css_first(".woocommerce-Price-amount bdi, bdi")
                    if plain:
                        price = self._parse_price(plain.text())

            # SKU & ID from add-to-cart button (data attrs)
            sku = None
            pid = None
            sku_btn = card.css_first("a.add_to_cart_button[data-product_sku], a[data-product_sku], button[data-product_sku], [data-product_sku]")
            if sku_btn:
                _sku = sku_btn.attributes.get("data-product_sku") or ""
                sku = _sku.strip() or None  # treat empty string as missing
                pid = sku_btn.attributes.get("data-product_id")
            if not pid:
                m = re.search(r"\bpost-(\d+)\b", cls)
                if m:
                    pid = m.group(1)

            # Brand from class list "pwb-brand-{name}"
            brand = None
            m = re.search(r"pwb-brand-([a-z0-9-]+)", cls, re.I)
            if m:
                brand = m.group(1).replace("-", " ").title()

            # Image — Porto lazy-loads via data-oi
            img = card.css_first("img.porto-lazyload, img[data-oi], img.wp-post-image, img")
            image = None
            if img:
                image = (img.attributes.get("data-oi") or img.attributes.get("data-src")
                         or img.attributes.get("src"))
                if image and (image.startswith("data:") or "lazy.png" in image or "lazy.svg" in image):
                    image = img.attributes.get("data-oi") or None

            # Stock from class
            available = None
            if "instock" in cls:
                available = True
            elif "outofstock" in cls:
                available = False

            products.append({
                "id": pid, "url": url, "name": name, "sku": sku, "brand": brand,
                "price": price, "old_price": old_price, "image": image,
                "available": available,
            })
        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css("nav.woocommerce-pagination a.page-numbers, ul.page-numbers a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a.next.page-numbers") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        # Tunisian WooCommerce format: "د.ت 650,000" or "1 499,000 DT"
        # Strip currency + non-numeric, then comma = decimal (French), dot = decimal too.
        s = str(text).replace("\xa0", "").replace(" ", "")
        cleaned = re.sub(r"[^\d.,]", "", s).strip()
        if not cleaned:
            return None
        if "," in cleaned and "." in cleaned:
            # Both: dot = thousand sep, comma = decimal
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            # French decimal: "650,000" → 650.0
            cleaned = cleaned.replace(",", ".")
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

        body = tree.css_first("body")
        if body:
            m = re.search(r"\bpostid-(\d+)\b", body.attributes.get("class", ""))
            data["product_id"] = m.group(1) if m else None

        # Chaktech uses h2.product_title (Porto theme variant)
        title_el = tree.css_first(
            "h1.product_title.entry-title, h1.product_title, "
            "h2.product_title.entry-title, h2.product_title, "
            "h1.entry-title, [class*='product_title']"
        )
        if not title_el:
            title_el = tree.css_first("h1") or tree.css_first("h2.entry-title")
        data["title"] = title_el.text(strip=True) if title_el else None

        sku_el = tree.css_first(".product_meta .sku, span.sku")
        data["sku"] = sku_el.text(strip=True) if sku_el else None

        ins_el = tree.css_first("p.price ins .woocommerce-Price-amount bdi")
        del_el = tree.css_first("p.price del .woocommerce-Price-amount bdi")
        price_el = tree.css_first("p.price .woocommerce-Price-amount bdi")
        if ins_el and del_el:
            data["price"] = self._parse_price(ins_el.text())
            data["old_price"] = self._parse_price(del_el.text())
        else:
            data["price"] = self._parse_price(price_el.text() if price_el else None)
            data["old_price"] = None

        if data.get("old_price") and data.get("price") and data["old_price"] != data["price"]:
            data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)

        # Brand — pwb-single-product-brands has <a title="Lexical"><img ...></a>
        brand = None
        brand_el = tree.css_first(".pwb-single-product-brands a, .pwb-brand-link")
        if brand_el:
            # Try title attribute first, then img alt, then anchor text
            brand = brand_el.attributes.get("title") or brand_el.text(strip=True)
            if not brand:
                img = brand_el.css_first("img")
                if img:
                    brand = (img.attributes.get("alt") or img.attributes.get("title") or "").strip()
        if not brand:
            # Parse from body classes "pwb-brand-{name}"
            body = tree.css_first("body")
            cls = body.attributes.get("class", "") if body else ""
            m = re.search(r"pwb-brand-([a-z0-9-]+)", cls, re.I)
            if m:
                brand = m.group(1).replace("-", " ").title()
        # Also check the product-{id} container class for brand
        if not brand:
            cont = tree.css_first("[id^='product-']")
            if cont:
                cls = cont.attributes.get("class", "")
                m = re.search(r"pwb-brand-([a-z0-9-]+)", cls, re.I)
                if m:
                    brand = m.group(1).replace("-", " ").title()
        data["brand"] = brand

        # Availability from product container class (instock/outofstock)
        cont = tree.css_first("[id^='product-'].product")
        if not cont:
            cont = tree.css_first("body")
        cont_cls = cont.attributes.get("class", "") if cont else ""
        if "outofstock" in cont_cls:
            data["availability"] = "Rupture de stock"
            data["available"] = False
        elif "instock" in cont_cls:
            data["availability"] = "En stock"
            data["available"] = True
        else:
            # Fallback to <p class="stock">
            in_stock = tree.css_first("p.stock.in-stock")
            out_stock = tree.css_first("p.stock.out-of-stock")
            if in_stock:
                data["availability"] = "En stock"
                data["available"] = True
            elif out_stock:
                data["availability"] = "Rupture de stock"
                data["available"] = False
            else:
                data["availability"] = None
                data["available"] = None

        desc_el = tree.css_first("#tab-description, div.woocommerce-product-details__short-description")
        if desc_el:
            data["description"] = re.sub(r"\s+", " ", desc_el.text(strip=True))[:2000]
        else:
            data["description"] = None

        specs = {}
        for row in tree.css("table.shop_attributes tr, table.woocommerce-product-attributes tr"):
            k = row.css_first("th")
            v = row.css_first("td")
            if k and v:
                key = k.text(strip=True)
                val = v.text(strip=True)
                if key and val:
                    specs[key] = val
        data["specifications"] = specs

        images = []
        for a in tree.css(".woocommerce-product-gallery__image a[href]"):
            src = a.attributes.get("href", "")
            if src and not src.startswith("data:") and src not in images:
                images.append(src)
        if not images:
            for img in tree.css(".woocommerce-product-gallery img"):
                src = img.attributes.get("data-large_image") or img.attributes.get("src")
                if src and not src.startswith("data:") and src not in images:
                    images.append(src)
        data["images"] = images[:10]
        return data


def get_scraper(logger: logging.Logger) -> ChaktechScraper:
    return ChaktechScraper(logger)
