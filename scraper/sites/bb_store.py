#!/usr/bin/env python3
"""
xn--bb-store-b1ab.com (بب ستور) scraper — WooCommerce (Minimog theme), no bot protection (httpx).
Categories: /product-category/{slug}/
Pagination: /page/{n}/
Price format: "185,000 DT" (comma = thousands sep when 3 digits)
Product container: div.product (or div.grid-item.product) — NOT article or li
Product link: a.woocommerce-LoopProduct-link
Image: img[data-src] inside m-image custom element
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

BASE = "https://xn--bb-store-b1ab.com"


class BbStoreScraper(FastScraper):
    """HTTP scraper for xn--bb-store-b1ab.com (WooCommerce)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("bb_store", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = base_url.rstrip("/")
        base = re.sub(r"/page/\d+$", "", base)
        return f"{base}/page/{page_num}/"

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
        cleaned = re.sub(r"[^\d,.]", "", str(text)).strip()
        if not cleaned:
            return None
        # "185,000" → 185.000 (comma = thousands sep)
        m = re.match(r"^(\d+),(\d{3})$", cleaned)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        # "185.000" → 185.000 (dot = thousands sep)
        m2 = re.match(r"^(\d+)\.(\d{3})$", cleaned)
        if m2:
            return float(f"{m2.group(1)}.{m2.group(2)}")
        # "49,90" → 49.90 (decimal)
        m3 = re.match(r"^(\d+),(\d{1,2})$", cleaned)
        if m3:
            return float(f"{m3.group(1)}.{m3.group(2)}")
        cleaned = cleaned.replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        for a in tree.css("a[href*='/product-category/']"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            if "/page/" in href or "?" in href:
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

        # Container is div.product (grid-item div), NOT article or li
        for item in tree.css("div.product.type-product, div.grid-item.product, div.product[class*='post-']"):
            product = {}

            cls = item.attributes.get("class", "")
            m = re.search(r"post-(\d+)", cls)
            if m:
                product["id"] = m.group(1)

            link = item.css_first("a.woocommerce-LoopProduct-link, a.woocommerce-loop-product__link")
            if not link:
                link = item.css_first("h3 a, h2 a, .product-title a")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            name_el = item.css_first("h3.woocommerce-loop-product__title, h2.woocommerce-loop-product__title, .woocommerce-loop-product__title")
            if not name_el:
                name_el = item.css_first("h3, h2")
            if name_el:
                product["name"] = self._clean(name_el.text())

            # Image is inside m-image custom element; img[data-src] works
            img = item.css_first("img[data-src], img[src]")
            if img:
                src = img.attributes.get("data-src") or img.attributes.get("src") or ""
                if src and not src.startswith("data:"):
                    src = re.sub(r"-\d+x\d+(\.\w+)$", r"\1", src)
                    product["image"] = self._abs(src)

            # Price block is div.price (NOT span.price) on this theme
            price_block = item.css_first("div.price, span.price, .price")
            if price_block:
                ins_el = price_block.css_first("ins .woocommerce-Price-amount, ins")
                del_el = price_block.css_first("del .woocommerce-Price-amount, del")
                plain_el = price_block.css_first(".woocommerce-Price-amount")
                if ins_el:
                    product["price"] = self._parse_price(ins_el.text())
                elif plain_el:
                    product["price"] = self._parse_price(plain_el.text())
                if del_el:
                    product["old_price"] = self._parse_price(del_el.text())
                if product.get("price") and product.get("old_price") and product["old_price"] > product["price"]:
                    product["discount_percent"] = round(
                        (1 - product["price"] / product["old_price"]) * 100
                    )

            product["shop"] = "bb_store"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        return tree.css_first(
            f"a.next.page-numbers, a[href*='/page/{current_page + 1}/'], a[rel='next']"
        ) is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css("a.page-numbers, .woocommerce-pagination a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a.next.page-numbers") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)
        name_el = tree.css_first("h1.product_title, h1.entry-title, h1")
        if name_el:
            details["name"] = self._clean(name_el.text())
        ins_el = tree.css_first("p.price ins .woocommerce-Price-amount")
        del_el = tree.css_first("p.price del .woocommerce-Price-amount")
        plain_el = tree.css_first("p.price .woocommerce-Price-amount")
        if ins_el:
            details["price"] = self._parse_price(ins_el.text())
        elif plain_el:
            details["price"] = self._parse_price(plain_el.text())
        if del_el:
            details["old_price"] = self._parse_price(del_el.text())
        sku_el = tree.css_first(".sku")
        if sku_el:
            details["sku"] = self._clean(sku_el.text())
        desc_el = tree.css_first(".woocommerce-product-details__short-description")
        if desc_el:
            details["description"] = self._clean(desc_el.text())
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
        self.logger.info("Starting bb_store scrape")
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
            cat_info = {"url": cat["url"], "top_category": cat["name"], "low_category": "", "subcategory": ""}
            prods = await self.scrape_category(cat_info)
            for p in prods:
                uid = p.get("id") or p.get("url")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    all_products.append(p)
        self.logger.info(f"Total unique products: {len(all_products)}")
        return {"products": all_products, "categories": categories}


def get_scraper(logger: logging.Logger) -> BbStoreScraper:
    return BbStoreScraper(logger)
