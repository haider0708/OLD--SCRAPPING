#!/usr/bin/env python3
"""
celio.tn scraper — Magento 2 (httpx).
Category URL: /collection/{cat}/{sub}.html
Pagination: ?p=N
Price: [data-price-amount] attribute (numeric)
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

BASE = "https://celio.tn"


class CelioScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("celio", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]p=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}p={page_num}"

    def _abs(self, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http"):
            return url
        return urljoin(BASE, url)

    def _clean(self, text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", str(text)).strip()
        cleaned = cleaned.replace(",", ".")
        if not cleaned:
            return None
        # If multiple dots, keep last as decimal
        if cleaned.count(".") > 1:
            parts = cleaned.split(".")
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _price_from_attr(self, el) -> Optional[float]:
        if not el:
            return None
        amt = el.attributes.get("data-price-amount")
        if amt:
            try:
                return float(amt)
            except ValueError:
                pass
        return self._parse_price(el.text())

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()
        for a in tree.css("a[href*='/collection/']"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            if not href.endswith(".html"):
                continue
            seen.add(href)
            categories.append({"name": name, "url": href, "level": "top", "low_level_categories": []})
        return {"categories": categories}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        for item in tree.css("li.product-item, div.product-item"):
            product = {}

            link = item.css_first("a.product-item-link")
            if not link:
                link = item.css_first("a[href]")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href
            product["name"] = self._clean(link.text())

            img = item.css_first("img[src], img[data-src]")
            if img:
                src = img.attributes.get("data-src") or img.attributes.get("src") or ""
                if src and not src.startswith("data:"):
                    product["image"] = self._abs(src)

            price_el = item.css_first("[data-price-amount]")
            if price_el:
                product["price"] = self._price_from_attr(price_el)
            else:
                fallback = item.css_first(".price")
                if fallback:
                    product["price"] = self._parse_price(fallback.text())

            old_price_el = item.css_first(".old-price [data-price-amount]")
            if old_price_el:
                product["old_price"] = self._price_from_attr(old_price_el)

            product["shop"] = "celio"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        return tree.css_first(
            f"a[href*='?p={current_page + 1}'], .pages-item-next a"
        ) is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css(".pages a, .pages-items a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        return {"current_page": 1, "total_pages": max_page, "has_next": max_page > 1}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)

        name_el = tree.css_first("h1.page-title, h1")
        if name_el:
            details["name"] = self._clean(name_el.text())

        price_el = tree.css_first(".product-info-main .price [data-price-amount]")
        if price_el:
            details["price"] = self._price_from_attr(price_el)
        else:
            price_fb = tree.css_first(".product-info-main .price, .price")
            if price_fb:
                details["price"] = self._parse_price(price_fb.text())

        old_price_el = tree.css_first(".old-price [data-price-amount]")
        if old_price_el:
            details["old_price"] = self._price_from_attr(old_price_el)

        sku_el = tree.css_first(".product.attribute.sku .value")
        if sku_el:
            details["sku"] = self._clean(sku_el.text())

        desc_el = tree.css_first(".product.attribute.description")
        if desc_el:
            details["description"] = self._clean(desc_el.text())

        images = []
        for img in tree.css(".gallery img[src], .fotorama__img"):
            src = img.attributes.get("src") or ""
            if src and not src.startswith("data:"):
                abs_src = self._abs(src)
                if abs_src and abs_src not in images:
                    images.append(abs_src)
        if images:
            details["images"] = images
            details.setdefault("image", images[0])

        return details


def get_scraper(logger: logging.Logger) -> CelioScraper:
    return CelioScraper(logger)
