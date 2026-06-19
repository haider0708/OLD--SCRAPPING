#!/usr/bin/env python3
"""
kiabi.tn scraper — Shopify, no bot protection (httpx).
Categories: /collections/{name}
Pagination: /collections/{name}?page=N
Price format: "29 DT" (plain integer TND)
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

BASE = "https://kiabi.tn"


class KiabiScraper(FastScraper):
    """HTTP scraper for kiabi.tn (Shopify)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("kiabi", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    def _abs(self, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return "https:" + url
        return urljoin(BASE, url)

    def _clean(self, text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        # "29 DT", "29,000 DT", "1 290 DT" (space as thousands sep)
        cleaned = re.sub(r"[^\d,.\s]", "", str(text)).strip()
        # Remove spaces used as thousands separators
        if re.match(r"^\d[\d\s]+$", cleaned):
            cleaned = cleaned.replace(" ", "")
        cleaned = cleaned.strip()
        if not cleaned:
            return None
        m = re.match(r"^(\d+),(\d{3})$", cleaned)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        m2 = re.match(r"^(\d+),(\d{1,2})$", cleaned)
        if m2:
            return float(f"{m2.group(1)}.{m2.group(2)}")
        cleaned = cleaned.replace(",", "")
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
            # Skip 'all' collection and pagination
            path = href.replace(BASE, "").strip("/")
            if path in ("collections/all", "collections") or "?" in path:
                continue
            seen.add(href)
            categories.append({
                "name": name,
                "url": href,
                "level": "top",
                "low_level_categories": [],
            })

        return {"categories": categories}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        for item in tree.css("div.product-item, .grid__item, li.grid__item, .product-card"):
            product = {}

            link = item.css_first("a[href*='/products/']")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            # Shopify product handle as ID
            m = re.search(r"/products/([^/?#]+)", href)
            if m:
                product["id"] = m.group(1)

            name_el = item.css_first("h3 a, h2 a, .product-item__title, .card__heading, h3, h2")
            if name_el:
                product["name"] = self._clean(name_el.text())

            img = item.css_first("img")
            if img:
                src = img.attributes.get("src") or img.attributes.get("data-src") or ""
                if src and not src.startswith("data:"):
                    # Shopify CDN: upgrade to large size
                    src = re.sub(r"_\d+x\d*\.", "_1024x.", src)
                    if src.startswith("//"):
                        src = "https:" + src
                    product["image"] = self._abs(src)

            # Price
            price_el = item.css_first(".price__current, .price-item--sale, .price-item--regular, .product-item__price, s.price-item--regular, del")
            regular_el = item.css_first(".price-item--regular, s.price-item--regular, del")
            sale_el = item.css_first(".price-item--sale, .price__current:not(s):not(del)")

            if sale_el and regular_el and sale_el != regular_el:
                product["price"] = self._parse_price(sale_el.text())
                product["old_price"] = self._parse_price(regular_el.text())
            elif price_el:
                product["price"] = self._parse_price(price_el.text())

            if product.get("price") and product.get("old_price") and product["old_price"] > product["price"]:
                product["discount_percent"] = round(
                    (1 - product["price"] / product["old_price"]) * 100
                )

            product["shop"] = "kiabi"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        next_el = tree.css_first(
            "a[rel='next'], .pagination__item--next a, "
            f"a[href*='page={current_page + 1}']"
        )
        return next_el is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css(".pagination a, nav.pagination a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a[rel='next'], .pagination__item--next a") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
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

        price_el = tree.css_first(".price__current, .price-item--sale, .price-item--regular")
        if price_el:
            details["price"] = self._parse_price(price_el.text())

        desc_el = tree.css_first(".product__description, .product-single__description, [itemprop='description']")
        if desc_el:
            details["description"] = self._clean(desc_el.text())

        # Kiabi embeds reference inside description text: "Nom de la référence : FPF62"
        # Also try Shopify variant SKU field
        sku_el = tree.css_first("[itemprop='sku'], .product__sku, .variant-sku")
        if sku_el:
            details["sku"] = self._clean(sku_el.text())
        else:
            # Scan description paragraphs for "référence :" pattern
            for el in tree.css(".product__description p, .product-single__description p, p"):
                txt = el.text(strip=True)
                if re.search(r"référence|nom de la ref", txt, re.IGNORECASE):
                    m = re.search(r"[:\-]\s*([A-Z0-9\-/]+)", txt, re.IGNORECASE)
                    if m:
                        details["sku"] = m.group(1).strip()
                        break

        images = []
        for img in tree.css(".product__media img, .product-single__media img"):
            src = img.attributes.get("src") or img.attributes.get("data-src") or ""
            if src and not src.startswith("data:"):
                if src.startswith("//"):
                    src = "https:" + src
                abs_src = self._abs(src)
                if abs_src and abs_src not in images:
                    images.append(abs_src)
        if images:
            details["images"] = images
            if not details.get("image"):
                details["image"] = images[0]

        return details

    async def scrape_category(self, category: dict) -> List[dict]:
        url = category.get("url")
        if not url:
            return []
        all_products = []
        page = 1
        while True:
            page_url = url if page == 1 else self.build_page_url(url, page)
            html = await self.fetch_html(page_url)
            if not html:
                break
            products = self.extract_products_from_html(html, category)
            if not products:
                break
            all_products.extend(products)
            self.logger.info(f"  Page {page}: {len(products)} products ({url})")
            if not self.has_next_page(html, page):
                break
            page += 1
            await asyncio.sleep(0.3)
        return all_products

    async def scrape(self) -> dict:
        self.logger.info("Starting kiabi scrape")

        html = await self.fetch_html(self.base_url)
        if not html:
            self.logger.error("Failed to fetch homepage")
            return {"products": [], "categories": []}

        cat_data = self.extract_categories_from_html(html)
        categories = cat_data.get("categories", [])
        self.logger.info(f"Found {len(categories)} categories")

        all_products = []
        seen_ids = set()
        for cat in categories:
            cat_info = {
                "url": cat["url"],
                "top_category": cat["name"],
                "low_category": "",
                "subcategory": "",
            }
            prods = await self.scrape_category(cat_info)
            for p in prods:
                uid = p.get("id") or p.get("url")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    all_products.append(p)

        self.logger.info(f"Total unique products: {len(all_products)}")
        return {"products": all_products, "categories": categories}


def get_scraper(logger: logging.Logger) -> KiabiScraper:
    return KiabiScraper(logger)
