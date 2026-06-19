#!/usr/bin/env python3
"""
culturel.tn scraper — Magento 1.x, no bot protection (httpx).
Categories: /{slug}.html or /{parent}/{child}.html
Pagination: ?p=N
Price format: "62,790 TND" (comma = decimal sep / thousands sep when 3 digits)
SKU: ISBN in product URL slug and on detail page (strong ISBN :)
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

BASE = "https://www.culturel.tn"


class CulturelScraper(FastScraper):
    """HTTP scraper for culturel.tn (Magento 1)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("culturel", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]p=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}p={page_num}"

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
        # "62,790" → 62.790
        m = re.match(r"^(\d+),(\d{3})$", cleaned)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        # "38,500" multi: strip all but last comma
        m2 = re.match(r"^(\d+),(\d{1,2})$", cleaned)
        if m2:
            return float(f"{m2.group(1)}.{m2.group(2)}")
        cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _isbn_from_url(self, url: str) -> Optional[str]:
        m = re.search(r"-(97[89]\d{10})\.html", url)
        return m.group(1) if m else None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        for a in tree.css("a[href$='.html']"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            if "?" in href or "#" in href:
                continue
            path = href.replace(BASE, "").strip("/")
            if not path.endswith(".html"):
                continue
            parts = path.replace(".html", "").split("/")
            if len(parts) > 2:
                continue
            # Skip product pages (have ISBN in URL)
            if re.search(r"97[89]\d{10}", path):
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

        # culturel.tn: product cards are div.prod-selec (server-rendered Magento 1)
        for item in tree.css("div.prod-selec"):
            product = {}

            link = item.css_first("a.prodLink, a[href*='/livre/'][href$='.html']")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            isbn = self._isbn_from_url(href)
            if isbn:
                product["sku"] = isbn

            # Name from title attribute on prodLink, or .nv-titre, or link text
            name = link.attributes.get("title", "").strip()
            if not name:
                name_el = item.css_first(".nv-titre, h3, h2, .product-name")
                if name_el:
                    name_link = name_el.css_first("a")
                    name = self._clean((name_link or name_el).text())
            if not name:
                name = self._clean(link.text())
            if name:
                product["name"] = name

            # Image: lazy-loaded in data-src
            img = item.css_first("img[data-src], img[src]")
            if img:
                src = img.attributes.get("data-src") or img.attributes.get("src") or ""
                if src and not src.startswith("data:"):
                    product["image"] = self._abs(src)

            # Price: span.nv-prix inside div.info-prix
            price_el = item.css_first("span.nv-prix, div.price, span.price, .price")
            if price_el:
                product["price"] = self._parse_price(price_el.text())

            product["shop"] = "culturel"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        return tree.css_first(
            f"a[href*='?p={current_page + 1}'], a[href*='&p={current_page + 1}'], a[rel='next'], li.pages-item-next a"
        ) is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css("ul.pagination li a, .pages a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a[rel='next']") is not None
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

        price_el = tree.css_first("span.nv-prix, div.price, span.price[itemprop='price'], span.price, .price")
        if price_el:
            content = price_el.attributes.get("content")
            details["price"] = self._parse_price(content or price_el.text())

        # ISBN from URL first, then scan page for "ISBN :" label
        if not details.get("sku"):
            isbn = self._isbn_from_url(details.get("url", ""))
            if isbn:
                details["sku"] = isbn
        if not details.get("sku"):
            for el in tree.css("strong, b, th, td, span, li, p"):
                txt = el.text(strip=True)
                if re.search(r"^ISBN\s*[:\-]?\s*\d", txt, re.IGNORECASE):
                    m = re.search(r"(97[89]\d{10})", txt)
                    if m:
                        details["sku"] = m.group(1)
                        break
                elif re.search(r"^(Réf|Référence|Code)\s*[:\-]\s*\S", txt, re.IGNORECASE):
                    m = re.search(r"[:\-]\s*(\S+)", txt)
                    if m:
                        details["sku"] = m.group(1)
                        break

        desc_el = tree.css_first("div.product-description, .description, [itemprop='description']")
        if desc_el:
            details["description"] = self._clean(desc_el.text())

        images = []
        for img in tree.css(".product-media img, .MagicSlideshow img, img[itemprop='image']"):
            src = img.attributes.get("src") or img.attributes.get("data-src") or ""
            if src and not src.startswith("data:"):
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
        self.logger.info("Starting culturel scrape")
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
                uid = p.get("sku") or p.get("url")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    all_products.append(p)
        self.logger.info(f"Total unique products: {len(all_products)}")
        return {"products": all_products, "categories": categories}


def get_scraper(logger: logging.Logger) -> CulturelScraper:
    return CulturelScraper(logger)
