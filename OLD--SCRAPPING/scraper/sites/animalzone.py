#!/usr/bin/env python3
"""
animalzone.tn scraper — PrestaShop (httpx).
Categories: /{id}-{slug}
Pagination: ?page=N
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

BASE = "https://animalzone.tn"


class AnimalzoneScraper(FastScraper):
    """HTTP scraper for animalzone.tn (PrestaShop)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("animalzone", logger)
        self._fetch_sem = asyncio.Semaphore(4)

    async def fetch_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        async with self._fetch_sem:
            await asyncio.sleep(0.3)
            return await super().fetch_html(url, raise_on_error=raise_on_error)

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
        cleaned = re.sub(r"[^\d\s,.]", "", str(text)).strip()
        cleaned = cleaned.replace(" ", "")
        if not cleaned:
            return None
        m = re.match(r"^(\d+),(\d{3})$", cleaned)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        m2 = re.match(r"^(\d+),(\d{1,2})$", cleaned)
        if m2:
            return float(f"{m2.group(1)}.{m2.group(2)}")
        cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _ean_from_url(self, url: str) -> Optional[str]:
        m = re.search(r"-(\d{13})\.html", url)
        return m.group(1) if m else None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        for a in tree.css("a[href*='animalzone.tn/']"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            path = href.replace(BASE, "").strip("/")
            if not re.match(r"^\d+-[a-z]", path):
                continue
            if "/" in path or "?" in path or path.endswith(".html"):
                continue
            seen.add(href)
            m = re.match(r"^(\d+)-", path)
            cat_id = m.group(1) if m else None
            categories.append({
                "name": name,
                "url": href,
                "id": cat_id,
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

        for item in tree.css("article.elementor-product-miniature, div.product-miniature.js-product-miniature, .product-miniature"):
            product = {}

            pid = item.attributes.get("data-id-product")
            if pid:
                product["id"] = str(pid)

            link = item.css_first("a.elementor-product-link, a.thumbnail.product-thumbnail, a[href*='.html']")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            ean = self._ean_from_url(href)
            if ean:
                product["sku"] = ean

            name_el = item.css_first(".elementor-title, h2.product-title, h3.product-title, .product-title")
            if name_el:
                product["name"] = self._clean(name_el.text())

            img = item.css_first("img.img-fluid[data-src], img[data-src], img")
            if img:
                src = img.attributes.get("data-src") or img.attributes.get("src") or ""
                if src and not src.startswith("data:"):
                    src = src.replace("home_default", "large_default")
                    product["image"] = self._abs(src)

            price_el = item.css_first(".elementor-price, span.price, .price")
            old_price_el = item.css_first(".elementor-old-price, .regular-price, del span.price, s span.price")
            if price_el:
                product["price"] = self._parse_price(price_el.text())
            if old_price_el:
                product["old_price"] = self._parse_price(old_price_el.text())
            if product.get("price") and product.get("old_price") and product["old_price"] > product["price"]:
                product["discount_percent"] = round(
                    (1 - product["price"] / product["old_price"]) * 100
                )

            product["shop"] = "animalzone"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        return tree.css_first(
            f"a.next.js-search-link, .pagination li.next a, a[rel='next'], a[href*='page={current_page + 1}']"
        ) is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css("ul.page-list li a, .pagination a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a.next.js-search-link, .pagination li.next a, a[rel='next']") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)

        name_el = tree.css_first("h1[itemprop='name'], h1")
        if name_el:
            details["name"] = self._clean(name_el.text())

        price_el = tree.css_first(".current-price span.price, span.price[itemprop='price'], span.price")
        if price_el:
            content = price_el.attributes.get("content")
            details["price"] = self._parse_price(content or price_el.text())

        old_price_el = tree.css_first(".regular-price, .product-price .regular-price")
        if old_price_el:
            details["old_price"] = self._parse_price(old_price_el.text())

        if not details.get("sku"):
            ean = self._ean_from_url(details.get("url", ""))
            if ean:
                details["sku"] = ean
        if not details.get("sku"):
            ref_el = tree.css_first(
                ".product-reference .js-product-reference, "
                ".product-reference span, "
                "p#product_reference span.editable, "
                "span[itemprop='sku'], [itemprop='sku']"
            )
            if ref_el:
                details["sku"] = self._clean(ref_el.text())
        if not details.get("sku"):
            for el in tree.css("span, p, div, li"):
                txt = el.text(strip=True)
                if re.match(r"^Référence\s+\S", txt):
                    m = re.search(r"Référence\s+(\S+)", txt)
                    if m:
                        details["sku"] = m.group(1)
                        break
                elif re.match(r"^(Réf|Ref|SKU|EAN|UGS)\s*[:\-]\s*\S", txt, re.IGNORECASE):
                    m = re.search(r"[:\-]\s*(\S+)", txt)
                    if m:
                        details["sku"] = m.group(1)
                        break

        desc_el = tree.css_first("div.product-description, #product-description-short, [itemprop='description']")
        if desc_el:
            details["description"] = self._clean(desc_el.text())

        images = []
        for img in tree.css(".product-images img, .slick-slide img, .product-cover img"):
            src = img.attributes.get("src") or img.attributes.get("data-src") or ""
            if src and not src.startswith("data:"):
                abs_src = self._abs(src.replace("home_default", "large_default"))
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
        self.logger.info("Starting animalzone scrape")
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


def get_scraper(logger: logging.Logger) -> AnimalzoneScraper:
    return AnimalzoneScraper(logger)
