#!/usr/bin/env python3
"""
informatica.tn scraper — WooCommerce + Woodmart, no bot protection (httpx).
Categories: /categorie-produit/{slug}/
Pagination: ?paged=N
Price format: "1652 DT" (plain integer or decimal)
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

BASE = "https://informatica.tn"


class InformaticaScraper(FastScraper):
    """HTTP scraper for informatica.tn (WooCommerce + Woodmart)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("informatica", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]paged=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}paged={page_num}"

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
        # "1 652 DT" space = thousands sep, "1652 DT" plain, "27.900 DT" dot = thousands
        cleaned = re.sub(r"[^\d\s,.]", "", str(text)).strip()
        cleaned = cleaned.replace(" ", "")
        if not cleaned:
            return None
        m = re.match(r"^(\d+)\.(\d{3})$", cleaned)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        m2 = re.match(r"^(\d+),(\d{3})$", cleaned)
        if m2:
            return float(f"{m2.group(1)}.{m2.group(2)}")
        m3 = re.match(r"^(\d+),(\d{1,2})$", cleaned)
        if m3:
            return float(f"{m3.group(1)}.{m3.group(2)}")
        cleaned = cleaned.replace(",", "").replace(".", "")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        for a in tree.css("a[href*='/categorie-produit/']"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            if "/page/" in href or "paged=" in href:
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

        for item in tree.css(".wd-product.product-grid-item, li.product, .type-product"):
            product = {}

            cls = item.attributes.get("class", "")
            m = re.search(r"post-(\d+)", cls)
            if m:
                product["id"] = m.group(1)

            link = item.css_first("a.product-image-link, a.woocommerce-LoopProduct-link")
            if not link:
                link = item.css_first("a[href*='/product/'], h3 a, h2 a")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            name_el = item.css_first("h3.product-title, h2.woocommerce-loop-product__title, h3, h2")
            if name_el:
                product["name"] = self._clean(name_el.text())

            img = item.css_first("img[data-wood-src], img[data-src], img")
            if img:
                src = img.attributes.get("data-wood-src") or img.attributes.get("data-src") or img.attributes.get("src") or ""
                if src and not src.startswith("data:"):
                    src = re.sub(r"-\d+x\d+(\.\w+)$", r"\1", src)
                    product["image"] = self._abs(src)

            price_block = item.css_first("span.price, .price")
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
                if product.get("price") and product.get("old_price") and product["old_price"] > 0:
                    product["discount_percent"] = round(
                        (1 - product["price"] / product["old_price"]) * 100
                    )

            product["shop"] = "informatica"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        return tree.css_first(
            f"a.next.page-numbers, a[href*='paged={current_page + 1}'], a[href*='/page/{current_page + 1}/']"
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
        # WooCommerce UGS: span.sku_wrapper span.sku → e.g. "TAC-09CHSA-TPG11I"
        sku_el = tree.css_first("span.sku_wrapper span.sku, span.sku, [itemprop='sku']")
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
        self.logger.info("Starting informatica scrape")
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


def get_scraper(logger: logging.Logger) -> InformaticaScraper:
    return InformaticaScraper(logger)
