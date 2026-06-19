#!/usr/bin/env python3
"""
sqes.tn scraper — Shopify, Cloudflare CDN (httpx).
Category URL: /collections/{slug}
Pagination: ?page=N
Price: span.price__regular or span.price__sale
SKU: from product URL or detail page
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

BASE = "https://www.sqes.tn"


class SqesScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("sqes", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

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
        m = re.search(r"[\d\s]+[.,]\d+", str(text))
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
        for a in tree.css("a[href*='/collections/']"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            if "/products/" in href or "?" in href:
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

        for item in tree.css("li.grid__item, .product-item, .card-wrapper"):
            product = {}

            link = item.css_first("a[href*='/products/']")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            name_el = item.css_first("h3 a, h2 a, .card__heading a, .card__title a")
            if name_el:
                product["name"] = self._clean(name_el.text())
            elif link:
                product["name"] = self._clean(link.text())

            img = item.css_first("img[src], img[data-src]")
            if img:
                src = img.attributes.get("data-src") or img.attributes.get("src") or ""
                if src and not src.startswith("data:"):
                    product["image"] = self._abs(src)

            # Shopify price structure: span.price__sale > span.price-item--sale
            sale = item.css_first(".price__sale .price-item--sale, .price-item--sale")
            if sale:
                product["price"] = self._parse_price(sale.text())
                reg = item.css_first(".price__regular .price-item--regular, .price-item--regular")
                if reg:
                    product["old_price"] = self._parse_price(reg.text())
            else:
                price_el = item.css_first(".price__regular, .price, span.price")
                if price_el:
                    product["price"] = self._parse_price(price_el.text())

            product["shop"] = "sqes"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        return tree.css_first(
            f"a[href*='page={current_page + 1}'], a[aria-label='Next'], .pagination__next"
        ) is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css(".pagination a, [data-page]"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        return {"current_page": 1, "total_pages": max_page, "has_next": max_page > 1}

    async def scrape_product_details(self, url: str) -> dict:
        await asyncio.sleep(0.5)
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)

        name_el = tree.css_first("h1.product__title, h1")
        if name_el:
            details["name"] = self._clean(name_el.text())

        sale = tree.css_first(".price__sale .price-item--sale")
        if sale:
            details["price"] = self._parse_price(sale.text())
            reg = tree.css_first(".price__regular .price-item--regular")
            if reg:
                details["old_price"] = self._parse_price(reg.text())
        else:
            price_el = tree.css_first(".price__regular, .price")
            if price_el:
                details["price"] = self._parse_price(price_el.text())

        sku_el = tree.css_first(".product__sku, [data-product-sku]")
        if sku_el:
            details["sku"] = self._clean(sku_el.text())

        desc_el = tree.css_first(".product__description, .product-description, #product-description")
        if desc_el:
            details["description"] = self._clean(desc_el.text())

        images = []
        for img in tree.css(".product__media img[src], .product-gallery img[src]"):
            src = img.attributes.get("src") or ""
            if src and not src.startswith("data:"):
                abs_src = self._abs(src)
                if abs_src and abs_src not in images:
                    images.append(abs_src)
        if images:
            details["images"] = images
            details.setdefault("image", images[0])

        return details


def get_scraper(logger: logging.Logger) -> SqesScraper:
    return SqesScraper(logger)
