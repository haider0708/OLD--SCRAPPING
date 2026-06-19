#!/usr/bin/env python3
"""
ceresbookshop.com scraper — PrestaShop 1.7+, no bot protection (httpx).
Categories: /fr/{id}-{slug}
Pagination: ?page=N
Price format: "25,000 TND" (comma = thousands sep when 3 digits)
SKU: ISBN embedded in product URL and on detail page
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

BASE = "https://ceresbookshop.com"


class CeresbookshopScraper(FastScraper):
    """HTTP scraper for ceresbookshop.com (PrestaShop)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("ceresbookshop", logger)

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
        cleaned = re.sub(r"[^\d,.]", "", str(text)).strip()
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

    def _isbn_from_url(self, url: str) -> Optional[str]:
        """Extract ISBN-13 from product URL slug."""
        m = re.search(r"-(97[89]\d{10})\.html", url)
        return m.group(1) if m else None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        for a in tree.css("a[href*='/fr/']"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            path = href.replace(BASE, "").strip("/")
            # PrestaShop category pattern: fr/{id}-{slug}
            if not re.match(r"^fr/\d+-[a-z]", path):
                continue
            if "?" in path or path.endswith(".html"):
                continue
            seen.add(href)
            m = re.match(r"fr/(\d+)-", path)
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

        for item in tree.css("div.product-miniature.js-product-miniature, .product-miniature"):
            product = {}

            pid = item.attributes.get("data-id-product")
            if pid:
                product["id"] = str(pid)

            link = item.css_first("a.thumbnail.product-thumbnail, h2.product-title a, a[href*='.html']")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            isbn = self._isbn_from_url(href)
            if isbn:
                product["sku"] = isbn

            name_el = item.css_first("h2.product-title a, h2.product-title, .product-title")
            if name_el:
                product["name"] = self._clean(name_el.text())

            img = item.css_first("img.img-fluid, img[data-src], img")
            if img:
                src = img.attributes.get("data-src") or img.attributes.get("src") or ""
                if src and not src.startswith("data:"):
                    src = src.replace("home_default", "large_default")
                    product["image"] = self._abs(src)

            price_el = item.css_first("span.price")
            if price_el:
                product["price"] = self._parse_price(price_el.text())

            product["shop"] = "ceresbookshop"
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
        for a in tree.css("ul.pagination li a, .page-list li a"):
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

        # ISBN from URL or from page (strong tag with "ISBN:")
        if not details.get("sku"):
            isbn = self._isbn_from_url(details.get("url", ""))
            if isbn:
                details["sku"] = isbn
        if not details.get("sku"):
            for el in tree.css("strong, b, span, td, li, p"):
                txt = el.text(strip=True)
                if re.search(r"^ISBN\s*[:\-]?\s*\d", txt, re.IGNORECASE):
                    m = re.search(r"(97[89]\d{10})", txt)
                    if m:
                        details["sku"] = m.group(1)
                        break
                elif re.search(r"^(Réf|Ref|Référence|Code)\s*[:\-]\s*\S", txt, re.IGNORECASE):
                    m = re.search(r"[:\-]\s*(\S+)", txt)
                    if m:
                        details["sku"] = m.group(1)
                        break

        desc_el = tree.css_first("#product-description-short, [itemprop='description'], .product-description")
        if desc_el:
            details["description"] = self._clean(desc_el.text())

        images = []
        for img in tree.css(".product-images img, .slick-slide img, .product-cover img, img.js-lazy-image"):
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
        self.logger.info("Starting ceresbookshop scrape")
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


def get_scraper(logger: logging.Logger) -> CeresbookshopScraper:
    return CeresbookshopScraper(logger)
