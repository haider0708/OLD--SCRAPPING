#!/usr/bin/env python3
"""
Acspace.tn scraper — WordPress + WooCommerce + Bricks Builder, httpx.
No Cloudflare. Products fully server-rendered.
"""

import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser
from scraper.base import FastScraper


class AcspaceScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("acspace", logger)

    # ------------------------------------------------------------------
    # Pagination — Bricks WooCommerce: ?paged=N
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]paged=\d+", "", base_url).rstrip("&").rstrip("?")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}paged={page_num}"

    # ------------------------------------------------------------------
    # Categories — static links in Splide slider
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen_urls = set()

        # Category links in the Splide slider
        for a in tree.css(".splide__list a.brxe-text-link[href], .splide__slide a[href]"):
            href = a.attributes.get("href", "")
            if not href or href in seen_urls:
                continue
            # Only category-style paths (not homepage, not cart, etc.)
            if href in ("/", "#") or any(x in href for x in ("cart", "panier", "account", "checkout")):
                continue
            seen_urls.add(href)
            name = a.text(strip=True)
            if not name:
                img = a.css_first("img")
                name = img.attributes.get("alt", "") if img else ""
            if not name:
                continue
            url = href if href.startswith("http") else f"https://acspace.tn{href}"
            categories.append({
                "name": name,
                "url": url,
                "level": "top",
                "low_level_categories": [],
            })

        # Fallback: any nav links to category paths
        if not categories:
            for a in tree.css("nav a[href], header a[href]"):
                href = a.attributes.get("href", "")
                if not href or href in seen_urls:
                    continue
                if not re.search(r"/[a-z][a-z0-9-]{2,}/?$", href):
                    continue
                if any(x in href for x in ("cart", "panier", "account", "checkout", "wp-")):
                    continue
                seen_urls.add(href)
                name = a.text(strip=True)
                if not name:
                    continue
                url = href if href.startswith("http") else f"https://acspace.tn{href}"
                categories.append({
                    "name": name,
                    "url": url,
                    "level": "top",
                    "low_level_categories": [],
                })

        self.logger.info(f"Found {len(categories)} categories")
        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": len(categories)}
        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        # Bricks query loop cards
        cards = tree.css("div.bt-product-21__product-card, div.brxe-block[class*='product']")
        if not cards:
            # Fallback: standard WooCommerce cards
            cards = tree.css("li.product.type-product, div.product")

        for card in cards:
            # URL from image link
            a = card.css_first("figure a[href], a[href*='/produit/'], a[href*='/product/']")
            if not a:
                a = card.css_first("a[href]")
            href = a.attributes.get("href", "") if a else ""
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)
            url = href if href.startswith("http") else f"https://acspace.tn{href}"

            # Name
            name_el = card.css_first("h2 a, h3 a, h2, h3, .product-title")
            name = name_el.text(strip=True) if name_el else ""

            # Price
            ins_el = card.css_first("span.price ins span.woocommerce-Price-amount bdi")
            del_el = card.css_first("span.price del span.woocommerce-Price-amount bdi")
            price_el = card.css_first("div.bt-product-21__price span.woocommerce-Price-amount bdi, span.woocommerce-Price-amount bdi")

            if ins_el and del_el:
                price = self._parse_price(ins_el.text())
                old_price = self._parse_price(del_el.text())
            else:
                price = self._parse_price(price_el.text() if price_el else None)
                old_price = None

            # SKU
            sku_el = card.css_first("span.sku")
            sku = sku_el.text(strip=True) if sku_el else None

            # Image
            img = card.css_first("figure img, img.wp-post-image, img")
            image = None
            if img:
                image = img.attributes.get("data-src") or img.attributes.get("src")
                if image and image.startswith("data:"):
                    image = None

            products.append({
                "id": sku,
                "url": url,
                "name": name,
                "price": price,
                "old_price": old_price,
                "sku": sku,
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

        # Bricks pagination
        pagination = tree.css_first("div.brxe-woocommerce-products-pagination, nav.woocommerce-pagination")
        if pagination:
            for a in pagination.css("a.page-numbers"):
                try:
                    num = int(a.text(strip=True))
                    if num > max_page:
                        max_page = num
                except ValueError:
                    pass
            current_el = pagination.css_first("span.current, .current")
            if current_el:
                try:
                    current_page = int(current_el.text(strip=True))
                    if current_page > max_page:
                        max_page = current_page
                except ValueError:
                    pass

        # Also check data-max-pages attribute
        trail = tree.css_first("div.brx-query-trail[data-max-pages]")
        if trail:
            try:
                mp = int(trail.attributes.get("data-max-pages", "1"))
                if mp > max_page:
                    max_page = mp
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
        cleaned = re.sub(r"[^\d,.]", "", text).strip()
        if not cleaned:
            return None
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
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

        title_el = tree.css_first("h1.product_title, h1")
        data["title"] = title_el.text(strip=True) if title_el else None

        sku_el = tree.css_first(".sku_wrapper span.sku, span.sku")
        data["sku"] = sku_el.text(strip=True) if sku_el else None

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
        for img in tree.css("div.woocommerce-product-gallery__image img"):
            src = img.attributes.get("data-large_image") or img.attributes.get("src")
            if src and src not in images:
                images.append(src)
        data["images"] = images[:10]

        return data


def get_scraper(logger: logging.Logger) -> AcspaceScraper:
    return AcspaceScraper(logger)
