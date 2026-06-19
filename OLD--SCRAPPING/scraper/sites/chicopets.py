#!/usr/bin/env python3
"""
chicopets.tn scraper — Custom Next.js with embedded streaming JSON (httpx).
Categories: /category/{slug}-{id}
Products: /discover/{slug}
Pagination: ?skip={N}&first=20 on the category URL.
Products embedded in HTML via self.__next_f.push streaming JSON.
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

BASE = "https://chicopets.tn"
PAGE_SIZE = 20


class ChicopetsScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("chicopets", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        # page_num starts at 1; skip = (page_num - 1) * PAGE_SIZE
        skip = (page_num - 1) * PAGE_SIZE
        base = re.sub(r"[?&](skip|first)=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}skip={skip}&first={PAGE_SIZE}"

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

    def _parse_price(self, text) -> Optional[float]:
        if text is None:
            return None
        cleaned = re.sub(r"[^\d.,]", "", str(text)).strip()
        cleaned = cleaned.replace(",", ".")
        if not cleaned:
            return None
        if cleaned.count(".") > 1:
            parts = cleaned.split(".")
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _extract_nb_products(self, html: str) -> Optional[int]:
        m = re.search(r'\\"nbProducts\\":(\d+)', html)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        m2 = re.search(r'"nbProducts":(\d+)', html)
        if m2:
            try:
                return int(m2.group(1))
            except ValueError:
                pass
        return None

    def _extract_products_regex(self, html: str, category_info: dict = None) -> List[dict]:
        """Extract products from streaming Next.js JSON via regex.
        Heavily escaped JSON in self.__next_f.push payload."""
        products = []
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        # Pattern for escaped JSON product objects
        pattern_escaped = re.compile(
            r'\{\\"__typename\\":\\"ProductType\\",\\"id\\":\\"(\d+)\\",\\"name\\":\\"([^\\"]+)\\".*?\\"sellPriceTaxInclude\\":\\"([^\\"]+)\\".*?\\"slug\\":\\"([^\\"]+)\\"(?:.*?\\"barCode\\":\\"([^\\"]*)\\")?',
            re.DOTALL,
        )
        seen = set()
        for m in pattern_escaped.finditer(html):
            pid, name, price, slug, barcode = m.groups()
            if pid in seen:
                continue
            seen.add(pid)
            url = f"{BASE}/discover/{slug}"
            product = {
                "id": pid,
                "url": url,
                "name": self._clean(name.encode().decode("unicode_escape", errors="ignore") if "\\u" in name else name),
                "price": self._parse_price(price),
                "shop": "chicopets",
                "top_category": top_cat,
                "low_category": low_cat,
                "subcategory": subcat,
            }
            if barcode:
                product["sku"] = barcode

            # Try to grab gallery / image (best-effort)
            img_m = re.search(
                r'\\"id\\":\\"' + re.escape(pid) + r'\\".*?\\"gallery\\":\[\\"([^\\"]+)\\"',
                html, re.DOTALL,
            )
            if img_m:
                product["image"] = self._abs(img_m.group(1))

            products.append(product)

        # Fallback unescaped JSON
        if not products:
            pattern_plain = re.compile(
                r'\{"__typename":"ProductType","id":"(\d+)","name":"([^"]+)".*?"sellPriceTaxInclude":"([^"]+)".*?"slug":"([^"]+)"(?:.*?"barCode":"([^"]*)")?',
                re.DOTALL,
            )
            for m in pattern_plain.finditer(html):
                pid, name, price, slug, barcode = m.groups()
                if pid in seen:
                    continue
                seen.add(pid)
                url = f"{BASE}/discover/{slug}"
                product = {
                    "id": pid,
                    "url": url,
                    "name": self._clean(name),
                    "price": self._parse_price(price),
                    "shop": "chicopets",
                    "top_category": top_cat,
                    "low_category": low_cat,
                    "subcategory": subcat,
                }
                if barcode:
                    product["sku"] = barcode
                products.append(product)

        return products

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()
        for a in tree.css("a[href*='/category/']"):
            href = self._abs(a.attributes.get("href", ""))
            if not href or href in seen:
                continue
            m = re.search(r"/category/(.+)-(\d+)$", href.replace(BASE, ""))
            if not m:
                continue
            slug, cat_id = m.group(1), m.group(2)
            name = self._clean(a.text())
            if not name:
                img = a.css_first("img")
                if img:
                    name = (img.attributes.get("alt") or img.attributes.get("title") or "").strip()
            if not name:
                name = slug.replace("-", " ").title()
            seen.add(href)
            categories.append({
                "name": name,
                "url": href,
                "id": cat_id,
                "level": "top",
                "low_level_categories": [],
            })
        return {"categories": categories}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        return self._extract_products_regex(html, category_info)

    def has_next_page(self, html: str, current_page: int) -> bool:
        return False  # overridden via scrape_all_pages

    def extract_pagination_from_html(self, html: str) -> dict:
        return {"current_page": 1, "total_pages": 1, "has_next": False}

    async def scrape_all_pages(self, category_url: str, limit: Optional[int] = None,
                                category_info: dict = None) -> List[dict]:
        all_products = []
        # First page
        html = await self.fetch_html(category_url)
        if not html:
            return []
        nb_products = self._extract_nb_products(html)
        first_batch = self._extract_products_regex(html, category_info)
        all_products.extend(first_batch)
        self.logger.info(f"  Page 1: {len(first_batch)} products (total expected: {nb_products})")

        if nb_products is None:
            return all_products
        skip = PAGE_SIZE
        page = 2
        while skip < nb_products:
            if limit and len(all_products) >= limit:
                break
            page_url = self.build_page_url(category_url, page)
            page_html = await self.fetch_html(page_url)
            if not page_html:
                break
            batch = self._extract_products_regex(page_html, category_info)
            if not batch:
                break
            all_products.extend(batch)
            self.logger.info(f"  Page {page}: {len(batch)} products")
            skip += PAGE_SIZE
            page += 1
            await asyncio.sleep(0.3)
        return all_products

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        details = dict(product)
        tree = HTMLParser(html)

        # Name
        name_el = tree.css_first("h1")
        if name_el:
            name_text = self._clean(name_el.text())
            if name_text:
                details["name"] = name_text

        # Try JSON regex
        m = re.search(r'\\"name\\":\\"([^\\"]+)\\".*?\\"sellPriceTaxInclude\\":\\"([^\\"]+)\\"', html, re.DOTALL)
        if m:
            if not details.get("name"):
                details["name"] = self._clean(m.group(1))
            details["price"] = self._parse_price(m.group(2))
        else:
            m2 = re.search(r'"sellPriceTaxInclude":"([^"]+)"', html)
            if m2:
                details["price"] = self._parse_price(m2.group(1))

        # SKU / barcode
        if not details.get("sku"):
            m3 = re.search(r'\\"barCode\\":\\"([^\\"]+)\\"', html) or re.search(r'"barCode":"([^"]+)"', html)
            if m3:
                details["sku"] = m3.group(1)

        # Description (best-effort)
        m4 = re.search(r'\\"description\\":\\"([^\\"]+)\\"', html)
        if m4:
            details["description"] = self._clean(m4.group(1))

        # Gallery
        images = []
        for gm in re.finditer(r'\\"gallery\\":\[((?:\\"[^\\"]+\\",?)+)\]', html):
            inner = gm.group(1)
            for im in re.finditer(r'\\"([^\\"]+)\\"', inner):
                src = self._abs(im.group(1))
                if src and src not in images:
                    images.append(src)
            if images:
                break
        if images:
            details["images"] = images
            details.setdefault("image", images[0])

        return details


def get_scraper(logger: logging.Logger) -> ChicopetsScraper:
    return ChicopetsScraper(logger)
