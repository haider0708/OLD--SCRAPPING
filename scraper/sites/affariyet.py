#!/usr/bin/env python3
"""
Affariyet.com scraper — PrestaShop (ZOneTheme). Categories /{id}-{slug}, pagination ?page=N.
Product URLs use /{cat-slug}/{slug}.html pattern.
"""

import logging
import re
import json as _json
from typing import List, Optional
from selectolax.parser import HTMLParser
from scraper.base import FastScraper


class AffariyetScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("affariyet", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    def _absolute_url(self, href: str) -> str:
        if not href:
            return href
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return f"https://www.affariyet.com{href}"
        return f"https://www.affariyet.com/{href}"

    @staticmethod
    def _is_category_url(href: str) -> bool:
        if not href:
            return False
        path = href.split("?")[0].split("#")[0]
        path = re.sub(r"^https?://[^/]+", "", path)
        # PrestaShop: /{id}-{slug} OR /{parent-slug}/{id}-{slug}
        # Exclude product pages (.html)
        if path.endswith(".html"):
            return False
        return bool(re.search(r"/\d+[-_][a-z]", path))

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen_urls = set()

        # Top menu items
        top_items = tree.css("ul#top-menu > li, nav#top-menu > ul > li, ul.top-menu > li.category, ul.top-menu > li")

        for top_li in top_items:
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

            # Sub-categories
            for sub_link in top_li.css("ul a, .dropdown-menu a, .sub-menu a"):
                sub_href = self._absolute_url(sub_link.attributes.get("href", ""))
                if not self._is_category_url(sub_href) or sub_href in seen_urls:
                    continue
                sub_name = sub_link.text(strip=True)
                if not sub_name:
                    continue
                seen_urls.add(sub_href)
                top_cat["low_level_categories"].append({
                    "name": sub_name, "url": sub_href, "level": "low", "subcategories": [],
                })
            categories.append(top_cat)

        # Fallback: scan all category-shaped links
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
        self.logger.info(f"Extracted {stats['top_level']} top, {stats['low_level']} low")
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        for card in tree.css("article.js-product-miniature, article.product-miniature, div.js-product-miniature"):
            link = card.css_first("h2.product-title a, h3.product-title a, a.thumbnail.product-thumbnail")
            if not link:
                link = card.css_first("a[href]")
            url = self._absolute_url(link.attributes.get("href", "")) if link else None
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            # affariyet uses <span class="h3 product-title h4">
            name_el = card.css_first("span.product-title, h2.product-title, h3.product-title, .product-title")
            if name_el:
                name = name_el.text(strip=True)
            else:
                name = link.text(strip=True) if link else ""
            # Truncated names ("...") aren't useful — fall back to img alt
            if not name or name.endswith("...") or len(name) < 5:
                img = card.css_first("img[alt]")
                if img:
                    alt = (img.attributes.get("alt") or "").strip()
                    if alt:
                        name = alt

            price_el = card.css_first(".product-price-and-shipping span.price, span.price")
            old_el = card.css_first("span.regular-price")
            price = self._parse_price(price_el.text() if price_el else None)
            old_price = self._parse_price(old_el.text() if old_el else None)

            img = card.css_first(".thumbnail-container img, img.product_image, img.product-thumbnail, img")
            image = None
            if img:
                image = img.attributes.get("data-src") or img.attributes.get("src")
                if image and image.startswith("data:"):
                    image = None

            pid = card.attributes.get("data-id-product")
            products.append({
                "id": pid, "url": url, "name": name,
                "price": price, "old_price": old_price, "image": image,
            })
        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css(".page-list a, ul.pagination a"):
            try:
                n = int(a.text(strip=True))
                if n > max_page:
                    max_page = n
            except ValueError:
                pass
        has_next = tree.css_first("a.next, a[rel='next']") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", str(text)).strip()
        if not cleaned:
            return None
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        elif "." in cleaned:
            parts = cleaned.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
                cleaned = cleaned.replace(".", "")
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

        title_el = tree.css_first("h1[itemprop='name'], h1.h1, h1.product-name, h1")
        data["title"] = title_el.text(strip=True) if title_el else None

        sku_el = tree.css_first(".product-reference span, span[itemprop='sku']")
        data["sku"] = sku_el.text(strip=True) if sku_el else None

        # Try multiple price locations
        price = None
        # 1. itemprop=price
        for el in tree.css("span[itemprop='price'], meta[itemprop='price']"):
            content = el.attributes.get("content")
            v = self._parse_price(content) if content else self._parse_price(el.text())
            if v:
                price = v
                break
        # 2. meta og:price / product:price:amount
        if price is None:
            for el in tree.css('meta[property="product:price:amount"], meta[property="og:price:amount"]'):
                v = self._parse_price(el.attributes.get("content"))
                if v:
                    price = v
                    break
        # 3. .current-price text
        if price is None:
            el = tree.css_first(".current-price, span.current-price")
            if el:
                price = self._parse_price(el.text())
        data["price"] = price

        old_el = tree.css_first("span.regular-price, .regular-price")
        data["old_price"] = self._parse_price(old_el.text() if old_el else None)

        if data.get("old_price") and data.get("price") and data["old_price"] != data["price"]:
            data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)

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
        if not brand:
            brand_el = tree.css_first(".product-manufacturer img, .product-manufacturer a")
            if brand_el:
                brand = brand_el.attributes.get("alt") or brand_el.text(strip=True)
        data["brand"] = brand

        avail_el = tree.css_first("#product-availability, .product-availability span, .product-quantities span")
        if avail_el:
            txt = avail_el.text(strip=True)
            data["availability"] = txt
            data["available"] = bool(re.search(r"stock|disponible", txt or "", re.I))
        else:
            data["availability"] = None
            data["available"] = None

        desc_el = tree.css_first("#description div.product-description, div.product-description, #description")
        if desc_el:
            data["description"] = re.sub(r"\s+", " ", desc_el.text(strip=True))[:2000]
        else:
            data["description"] = None

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

        images = []
        for img in tree.css(".product-cover img, .product-images li img, .js-thumbnails img"):
            src = img.attributes.get("data-image-large-src") or img.attributes.get("src")
            if src and not src.startswith("data:") and src not in images:
                images.append(self._absolute_url(src))
        data["images"] = images[:10]
        return data


def get_scraper(logger: logging.Logger) -> AffariyetScraper:
    return AffariyetScraper(logger)
