#!/usr/bin/env python3
"""
animalia.tn scraper — Custom Next.js classifieds platform (animals for sale).
Listings are server-rendered anchor tags.
Category URL: /{animal-type}  e.g. /chiens /chats /oiseaux
Pagination: ?page=N
Price: last <span> in the listing anchor (contains "X DT" or "X.00 DT")
Name: <h4> inside anchor
Image: <img src="...cloudinary...">
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

BASE = "https://www.animalia.tn"

SEED_CATEGORIES = [
    {"name": "Chiens", "url": f"{BASE}/chiens"},
    {"name": "Chats", "url": f"{BASE}/chats"},
    {"name": "Oiseaux", "url": f"{BASE}/oiseaux"},
    {"name": "Poissons", "url": f"{BASE}/poissons"},
    {"name": "Rongeurs", "url": f"{BASE}/rongeurs"},
    {"name": "Reptiles", "url": f"{BASE}/reptiles"},
    {"name": "Lapins", "url": f"{BASE}/lapins"},
]


class AnimaliaScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("animalia", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    def _abs(self, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        return urljoin(BASE, url)

    def _clean(self, text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        m = re.search(r"([\d\s]+[.,]\d+)", str(text))
        if not m:
            m = re.search(r"\d+", str(text))
        if not m:
            return None
        cleaned = re.sub(r"[^\d.]", "", m.group().replace(",", ".").replace(" ", ""))
        try:
            return float(cleaned)
        except ValueError:
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()
        for a in tree.css("nav a, header a, .menu a"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            path = href.replace(BASE, "").strip("/")
            # Only top-level single-segment paths (animal categories)
            if not path or "/" in path or "?" in path or "#" in path:
                continue
            seen.add(href)
            categories.append({"name": name, "url": href, "level": "top", "low_level_categories": []})
        if not categories:
            categories = SEED_CATEGORIES
        return {"categories": categories}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        # Listings are <a href="/{type}/{breed}/{title}-{id}"> wrapping the whole card
        for item in tree.css("a[href]"):
            href_raw = item.attributes.get("href", "")
            # Must be a listing URL: 3 path segments minimum, with a numeric ID suffix
            if not re.search(r"/[^/]+/[^/]+-\d+$", href_raw):
                continue
            href = self._abs(href_raw)
            if not href:
                continue

            product = {"url": href}

            name_el = item.css_first("h4, h3, h2")
            if name_el:
                product["name"] = self._clean(name_el.text())
            else:
                continue

            # Price: last span that contains a number followed by DT
            price = None
            for span in reversed(item.css("span")):
                txt = span.text(strip=True)
                if re.search(r"\d", txt) and ("DT" in txt or re.search(r"\d+[.,]\d+", txt)):
                    price = self._parse_price(txt)
                    if price:
                        break
            if price:
                product["price"] = price

            img = item.css_first("img[src]")
            if img:
                product["image"] = img.attributes.get("src", "")

            # Location: <p> tag
            loc_el = item.css_first("p")
            if loc_el:
                product["location"] = self._clean(loc_el.text())

            product["shop"] = "animalia"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        return tree.css_first(
            f"a[href*='page={current_page + 1}'], a[aria-label='next'], .next"
        ) is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css("a[href*='page=']"):
            m = re.search(r"page=(\d+)", a.attributes.get("href", ""))
            if m:
                num = int(m.group(1))
                if num > max_page:
                    max_page = num
        return {"current_page": 1, "total_pages": max_page, "has_next": max_page > 1}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)

        name_el = tree.css_first("h1")
        if name_el:
            details["name"] = self._clean(name_el.text())

        for el in tree.css("span, div, p"):
            txt = el.text(strip=True)
            if re.search(r"\d+[.,]\d+\s*(DT|TND)", txt, re.IGNORECASE) and len(txt) < 30:
                details["price"] = self._parse_price(txt)
                break

        desc_el = tree.css_first(".description, .ad-description, article p")
        if desc_el:
            details["description"] = self._clean(desc_el.text())

        images = []
        for img in tree.css("img[src*='cloudinary'], .gallery img[src]"):
            src = img.attributes.get("src", "")
            if src and src not in images:
                images.append(src)
        if images:
            details["images"] = images
            details.setdefault("image", images[0])

        return details


def get_scraper(logger: logging.Logger) -> AnimaliaScraper:
    return AnimaliaScraper(logger)
