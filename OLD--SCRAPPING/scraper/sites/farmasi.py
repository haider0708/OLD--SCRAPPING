#!/usr/bin/env python3
"""
farmasi.tn scraper — nopCommerce, broken SSL cert (httpx, verify=False).
Categories: /{slug}  (root-relative, no /category/ prefix)
  - top-level: a.with-subcategories
  - sub: .picture-title-wrap .title a
  - leaf: a.lastLevelCategory
Pagination: ?pagenumber=N
Price format: "54,000 TND" (comma = thousands sep when 3 digits)
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from scraper.base import FastScraper

BASE = "https://www.farmasi.tn"

# Known non-category slugs to skip
_SKIP_PATHS = {
    "", "login", "logout", "cart", "wishlist", "search", "contact",
    "about", "sitemap", "register", "account", "checkout", "404",
    "panier", "connexion", "inscription",
}


class FarmasiScraper(FastScraper):
    """HTTP scraper for farmasi.tn (nopCommerce)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("farmasi", logger)

    async def get_client(self, slot: int = 0) -> httpx.AsyncClient:
        """Override to disable SSL verification — farmasi.tn has a broken cert chain."""
        key = -1
        if key not in self._clients or self._clients[key].is_closed:
            self._clients[key] = httpx.AsyncClient(
                headers=self.headers,
                follow_redirects=True,
                timeout=httpx.Timeout(self.request_timeout, connect=10.0),
                limits=httpx.Limits(
                    max_connections=200,
                    max_keepalive_connections=40,
                    keepalive_expiry=30.0,
                ),
                verify=False,
            )
        return self._clients[key]

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]pagenumber=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}pagenumber={page_num}"

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
        # "54,000" → 54.000 (thousands sep)
        m = re.match(r"^(\d+),(\d{3})$", cleaned)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        # "54,90" → 54.90 (decimal)
        m2 = re.match(r"^(\d+),(\d{1,2})$", cleaned)
        if m2:
            return float(f"{m2.group(1)}.{m2.group(2)}")
        cleaned = cleaned.replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _is_category_href(self, href: str) -> bool:
        """Return True if this href looks like a nopCommerce category slug."""
        path = href.replace(BASE, "").lstrip("/").split("?")[0].split("#")[0]
        if not path or "/" in path:
            return False
        if path in _SKIP_PATHS:
            return False
        # nopCommerce slugs are kebab-case with letters/digits/hyphens
        if not re.match(r"^[a-z0-9][a-z0-9\-]+$", path):
            return False
        return True

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        # Collect from the mega-menu: top-level, sub, and leaf links
        selectors = [
            "a.with-subcategories",
            ".picture-title-wrap .title a",
            "a.lastLevelCategory",
            "ul.subcategories li a",
        ]
        for sel in selectors:
            for a in tree.css(sel):
                href = a.attributes.get("href", "")
                href = self._abs(href)
                name = self._clean(a.text())
                if not href or not name or href in seen:
                    continue
                if not self._is_category_href(href):
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

        for item in tree.css("div.product-item[data-productid]"):
            product = {}

            pid = item.attributes.get("data-productid")
            if pid:
                product["id"] = str(pid)

            link = item.css_first(".product-title a")
            if not link:
                link = item.css_first("h2 a, a[href]")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            name_el = item.css_first("h2.product-title, .product-title")
            if name_el:
                product["name"] = self._clean(name_el.text())
            elif link:
                t = self._clean(link.text())
                if t:
                    product["name"] = t

            img = item.css_first("img.product-image")
            if img:
                src = img.attributes.get("data-lazyloadsrc") or img.attributes.get("src") or ""
                if src and not src.startswith("data:"):
                    product["image"] = self._abs(src)

            price_el = item.css_first("span.actual-price")
            old_price_el = item.css_first("span.old-price")
            if price_el:
                product["price"] = self._parse_price(price_el.text())
            if old_price_el:
                product["old_price"] = self._parse_price(old_price_el.text())
            if product.get("price") and product.get("old_price") and product["old_price"] > product["price"]:
                product["discount_percent"] = round(
                    (1 - product["price"] / product["old_price"]) * 100
                )

            product["shop"] = "farmasi"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        return tree.css_first("div.pager li.next-page a") is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css("div.pager li.individual-page a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("div.pager li.next-page a") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)
        name_el = tree.css_first("h1.product-name, h1[itemprop='name'], h1")
        if name_el:
            details["name"] = self._clean(name_el.text())
        price_el = tree.css_first("span.actual-price")
        if price_el:
            details["price"] = self._parse_price(price_el.text())
        old_price_el = tree.css_first("span.old-price")
        if old_price_el:
            details["old_price"] = self._parse_price(old_price_el.text())
        # nopCommerce: <meta itemprop="sku" content="1001486"> or <div class="sku"><span class="value">
        sku_meta = tree.css_first("meta[itemprop='sku']")
        if sku_meta:
            details["sku"] = self._clean(sku_meta.attributes.get("content", ""))
        if not details.get("sku"):
            sku_el = tree.css_first("div.sku span.value, span.value[id^='sku-']")
            if sku_el:
                details["sku"] = self._clean(sku_el.text())
        desc_el = tree.css_first(".short-description, [itemprop='description'], .full-description")
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
        self.logger.info("Starting farmasi scrape")
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


def get_scraper(logger: logging.Logger) -> FarmasiScraper:
    return FarmasiScraper(logger)
