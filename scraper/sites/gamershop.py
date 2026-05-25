#!/usr/bin/env python3
"""
GamerShop.tn scraper — PrestaShop. Categories at /{id}-{slug}, pagination ?page=N.
"""

import logging
import re
import json as _json
from typing import List, Optional
from selectolax.parser import HTMLParser
from scraper.base import FastScraper


class GamershopScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("gamershop", logger)

    # ------------------------------------------------------------------
    # Pagination — PrestaShop: ?page=N
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, href: str) -> str:
        if not href:
            return href
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return f"https://gamershop.tn{href}"
        return f"https://gamershop.tn/{href}"

    @staticmethod
    def _is_category_url(href: str) -> bool:
        # PrestaShop category: /{numeric_id}-{slug}
        if not href:
            return False
        path = href.split("?")[0].split("#")[0]
        # Strip protocol+host
        path = re.sub(r"^https?://[^/]+", "", path)
        return bool(re.match(r"^/\d+[-_]", path))

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen_urls = set()

        # Walk top-level menu items
        top_items = tree.css("ul.menu.sp_lesp.level-1 > li, ul.menu > li.category, ul.top-menu > li.category")
        if not top_items:
            top_items = tree.css("ul.menu > li")

        for top_li in top_items:
            # Direct child link
            top_link = None
            for child in top_li.iter():
                if child.tag == "a":
                    top_link = child
                    break
            if not top_link:
                continue
            top_href = self._absolute_url(top_link.attributes.get("href", ""))
            if not self._is_category_url(top_href) or top_href in seen_urls:
                continue
            top_name = top_link.text(strip=True)
            if not top_name:
                continue
            seen_urls.add(top_href)
            top_cat = {"name": top_name, "url": top_href, "level": "top", "low_level_categories": []}

            # Sub-level: ul.level-2 > li > a
            low_lis = top_li.css("ul.level-2 > li, .dropdown-menu ul > li")
            for low_li in low_lis:
                low_link = None
                for child in low_li.iter():
                    if child.tag == "a":
                        low_link = child
                        break
                if not low_link:
                    continue
                low_href = self._absolute_url(low_link.attributes.get("href", ""))
                if not self._is_category_url(low_href) or low_href in seen_urls:
                    continue
                low_name = low_link.text(strip=True)
                if not low_name:
                    continue
                seen_urls.add(low_href)
                low_cat = {"name": low_name, "url": low_href, "level": "low", "subcategories": []}

                # Sub-sub: ul.level-3 > li > a
                for sub_li in low_li.css("ul.level-3 > li, ul.level-3 li"):
                    sub_link = None
                    for child in sub_li.iter():
                        if child.tag == "a":
                            sub_link = child
                            break
                    if not sub_link:
                        continue
                    sub_href = self._absolute_url(sub_link.attributes.get("href", ""))
                    if not self._is_category_url(sub_href) or sub_href in seen_urls:
                        continue
                    sub_name = sub_link.text(strip=True)
                    if not sub_name:
                        continue
                    seen_urls.add(sub_href)
                    low_cat["subcategories"].append({"name": sub_name, "url": sub_href, "level": "subcategory"})
                top_cat["low_level_categories"].append(low_cat)
            categories.append(top_cat)

        # Fallback: scan all category-shaped links if menu walking was empty
        if not categories:
            for a in tree.css("a[href]"):
                href = self._absolute_url(a.attributes.get("href", ""))
                if not self._is_category_url(href) or href in seen_urls:
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
                for sub in low["subcategories"]:
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1
        self.logger.info(f"Extracted {stats['top_level']} top, {stats['low_level']} low, {stats['subcategory']} sub")
        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Products on category page
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        for card in tree.css("article.js-product-miniature, div.js-product-miniature, article.product-miniature"):
            link = card.css_first("a.thumbnail.product-thumbnail, h2.product_name a, h2 a, a.product_img_link")
            if not link:
                link = card.css_first("a[href]")
            url = self._absolute_url(link.attributes.get("href", "")) if link else None
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            name_el = card.css_first("h2.product_name, h2.product-title a, .product-title")
            name = name_el.text(strip=True) if name_el else (link.text(strip=True) if link else "")

            # Price
            price_el = card.css_first(".product-price-and-shipping span.price, .product-price span.price, span.price")
            old_el = card.css_first("span.regular-price, .product-price-and-shipping .regular-price")
            price = self._parse_price(price_el.text() if price_el else None)
            old_price = self._parse_price(old_el.text() if old_el else None)

            # Image
            img = card.css_first("img.product_image, img.product-thumbnail, img.replace-2x, img")
            image = None
            if img:
                image = img.attributes.get("data-src") or img.attributes.get("src")
                if image and image.startswith("data:"):
                    image = None

            pid = card.attributes.get("data-id-product")
            products.append({
                "id": pid,
                "url": url,
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
        for a in tree.css(".page-list a, ul.pagination a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a.next, a[rel='next']") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    # ------------------------------------------------------------------
    # Price — TN: "1 200,000 DT" or "1,200 DT"
    # ------------------------------------------------------------------

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", str(text)).strip()
        if not cleaned:
            return None
        # PrestaShop TN format: "1 459,000 DT" → 1459 (millimes, drop them)
        # Strategy: comma = decimal (French), dot = thousand sep (rare here)
        if "," in cleaned and "." in cleaned:
            # Both present: dots are thousand sep, comma is decimal
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            # Only comma: French decimal
            cleaned = cleaned.replace(",", ".")
        elif "." in cleaned:
            # Only dot: could be decimal OR thousand sep
            # "1.459.000" → thousand sep (drop dots)
            # "1459.500" → decimal (keep one)
            # Heuristic: if more than one dot OR last group is exactly 3 digits → thousand sep
            parts = cleaned.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
                cleaned = cleaned.replace(".", "")
        try:
            val = float(cleaned)
            # PrestaShop TN stores prices in TND with 3 decimal places (millimes).
            # Tax-inclusive prices like "1459.000" are real TND, not millimes.
            return val
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

        title_el = tree.css_first("h1.h1, h1[itemprop='name'], h1.product-name, h1.page-title, h1")
        data["title"] = title_el.text(strip=True) if title_el else None

        sku_el = tree.css_first(".product-reference span, span[itemprop='sku'], div.product-reference span")
        data["sku"] = sku_el.text(strip=True) if sku_el else None

        # Price — current
        price_el = tree.css_first(
            ".product-prices .current-price span[itemprop='price'], "
            ".current-price span, span[itemprop='price'], "
            ".product-price"
        )
        if price_el:
            content = price_el.attributes.get("content")
            data["price"] = self._parse_price(content or price_el.text())
        else:
            data["price"] = None

        old_el = tree.css_first(".regular-price, .product-discount .regular-price")
        data["old_price"] = self._parse_price(old_el.text() if old_el else None)

        if data.get("old_price") and data.get("price") and data["old_price"] != data["price"]:
            data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)

        # Brand from JSON-LD
        brand = None
        for script in tree.css('script[type="application/ld+json"]'):
            raw = (script.text() or "").strip()
            if not raw:
                continue
            try:
                d = _json.loads(raw)
            except Exception:
                continue
            blocks = d if isinstance(d, list) else [d]
            for b in blocks:
                if isinstance(b, dict) and b.get("@type") == "Product":
                    br = b.get("brand")
                    if isinstance(br, dict):
                        brand = br.get("name")
                    elif isinstance(br, str):
                        brand = br
                    if brand:
                        break
            if brand:
                break
        data["brand"] = brand

        # Availability
        avail_el = tree.css_first("#stock_availability, .product-availability span, span[itemprop='availability']")
        if avail_el:
            txt = avail_el.text(strip=True)
            data["availability"] = txt
            data["available"] = bool(re.search(r"stock|disponible|in stock", txt or "", re.I))
        else:
            data["availability"] = None
            data["available"] = None

        # Description
        desc_el = tree.css_first("#description, .product-description, div[itemprop='description'], #product-description")
        if desc_el:
            data["description"] = re.sub(r"\s+", " ", desc_el.text(strip=True))[:2000]
        else:
            data["description"] = None

        # Specs
        specs = {}
        for row in tree.css("section.product-features dl.data-sheet div, table.data-sheet tr, dl.data-sheet > div"):
            dt = row.css_first("dt")
            dd = row.css_first("dd")
            if dt and dd:
                k = dt.text(strip=True)
                v = dd.text(strip=True)
                if k and v:
                    specs[k] = v
        data["specifications"] = specs

        # Images
        images = []
        for img in tree.css(".product-cover img, .js-thumbnails img, .product-images img"):
            src = img.attributes.get("data-image-large-src") or img.attributes.get("src")
            if src and not src.startswith("data:") and src not in images:
                images.append(self._absolute_url(src))
        data["images"] = images[:10]
        return data


def get_scraper(logger: logging.Logger) -> GamershopScraper:
    return GamershopScraper(logger)
