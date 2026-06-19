#!/usr/bin/env python3
"""
Dokani.tn scraper — Odoo TP Shop, no CF, httpx.
Category URLs: /shop/category/{name}-{id}
Product URLs:  /shop/{category-id}/{product-slug}
Pagination:    /shop/category/{name}-{id}/page/{n}?limit=50
"""
import asyncio
import logging
import re
import time
from typing import List, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

_MIN_INTERVAL = 0.5


class DokaniScraper(FastScraper):
    """httpx scraper for dokani.tn (Odoo TP Shop)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("dokani", logger)
        self._page_sem = asyncio.Semaphore(3)
        self._last_request = 0.0

    async def fetch_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        async with self._page_sem:
            now = time.monotonic()
            wait = _MIN_INTERVAL - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            return await super().fetch_html(url, raise_on_error=raise_on_error)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        # Odoo pagination: /shop/category/slug-id/page/N?limit=50
        base = re.sub(r"/page/\d+", "", base_url.rstrip("/"))
        parsed = urlparse(base)
        params = parse_qs(parsed.query)
        params["limit"] = ["50"]
        new_query = urlencode({k: v[0] for k, v in params.items()})
        new_path = parsed.path + f"/page/{page_num}"
        return urlunparse(parsed._replace(path=new_path, query=new_query))

    def _clean(self, text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _abs(self, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return f"{self.base_url}{url}"
        return f"{self.base_url}/{url}"

    def _parse_price(self, text: str) -> Optional[float]:
        if not text:
            return None
        # Price format: "149.0DT210.0DT(29% DÉSACTIVÉ)" — first number is current price
        m = re.search(r"([\d]+(?:[.,]\d+)?)\s*DT", text, re.IGNORECASE)
        if m:
            cleaned = m.group(1).replace(",", ".")
            try:
                return float(cleaned)
            except ValueError:
                pass
        cleaned = re.sub(r"[^\d.,]", "", text)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    def _parse_old_price(self, text: str) -> Optional[float]:
        """Extract the second price from 'currentDT originalDT' format."""
        matches = re.findall(r"([\d]+(?:[.,]\d+)?)\s*DT", text, re.IGNORECASE)
        if len(matches) >= 2:
            try:
                return float(matches[1].replace(",", "."))
            except ValueError:
                pass
        return None

    def _extract_category_links(self, html: str) -> dict:
        """Extract /shop/category/ links from HTML, deduplicated by numeric ID."""
        tree = HTMLParser(html)
        by_id: dict = {}
        for a in tree.css("a[href*='/shop/category/']"):
            href = a.attributes.get("href", "")
            abs_url = self._abs(href)
            if not abs_url:
                continue
            abs_url = re.sub(r"/page/\d+.*$", "", abs_url.rstrip("/")) + "/"
            name = self._clean(a.text(strip=True))
            if not name:
                continue
            m = re.search(r"/category/(?:[^/]+-)?(\d+)/?$", abs_url)
            if not m:
                continue
            cat_id = m.group(1)
            existing = by_id.get(cat_id)
            if not existing or len(abs_url) > len(existing["url"]):
                by_id[cat_id] = {"name": name, "url": abs_url}
        return by_id

    def extract_categories_from_html(self, html: str) -> dict:
        by_id = self._extract_category_links(html)
        categories = [
            {"name": v["name"], "url": v["url"], "level": "top", "low_level_categories": []}
            for v in by_id.values()
        ]
        self.logger.info(f"Found {len(categories)} top-level categories")
        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": len(categories)}
        return {"categories": categories, "stats": stats}

    async def _discover_subcategories(self, top_categories: list) -> list:
        """Fetch each top-level category page to find subcategory links."""
        enriched = []
        for top in top_categories:
            html = await self.fetch_html(top["url"])
            subcats = {}
            if html:
                subcats = self._extract_category_links(html)
                top_m = re.search(r"/category/(?:[^/]+-)?(\d+)/?$", top["url"])
                if top_m:
                    subcats.pop(top_m.group(1), None)

            if subcats:
                low_level = [
                    {"name": v["name"], "url": v["url"], "level": "low", "subcategories": []}
                    for v in subcats.values()
                ]
                self.logger.info(f"  {top['name']}: found {len(low_level)} subcategories")
                enriched.append({**top, "low_level_categories": low_level})
            else:
                enriched.append(top)
        return enriched

    async def scrape_categories_async(self) -> dict:
        """Two-pass category discovery: homepage then each category page for subcategories."""
        from scraper.base import save_json, get_date_folder
        from datetime import datetime

        html_path = self.html_dir / "frontpage.html"
        if not html_path.exists():
            raise FileNotFoundError(f"Frontpage not found: {html_path}")

        html = html_path.read_text(encoding="utf-8")
        base_data = self.extract_categories_from_html(html)
        top_categories = base_data["categories"]

        self.logger.info("Fetching category pages to discover subcategories...")
        enriched = await self._discover_subcategories(top_categories)

        total_low = sum(len(t.get("low_level_categories", [])) for t in enriched)
        stats = {
            "top_level": len(enriched),
            "low_level": total_low,
            "subcategory": 0,
            "total_urls": len(enriched) + total_low,
        }

        data = {
            "categories": enriched,
            "stats": stats,
            "site": self.site_name,
            "shop": self.site_name,
            "base_url": self.base_url,
            "extracted_at": datetime.now().isoformat(),
            "date": get_date_folder(),
        }

        output_path = self.data_dir / "categories.json"
        save_json(data, output_path, self.logger)
        self.logger.info(f"Categories saved: {len(enriched)} top, {total_low} subcategories")
        return data

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        for item in tree.css(".tp-product-item, div[class*='tp-product-item']"):
            link_el = item.css_first("a[href*='/shop/']")
            if not link_el:
                continue
            product_url = self._abs(link_el.attributes.get("href", ""))
            if not product_url or product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            # Product ID from hidden form input
            product_id = None
            pid_input = item.css_first("input[name='product_id'], input[name='product_template_id']")
            if pid_input:
                product_id = pid_input.attributes.get("value")

            name_el = item.css_first(".tp-product-title, h5, h4, h3, .product-title, .product-name")
            product_name = self._clean(name_el.text(strip=True)) if name_el else ""

            product_data = {"id": product_id, "url": product_url, "name": product_name}

            img_el = item.css_first("img")
            if img_el:
                src = img_el.attributes.get("src") or img_el.attributes.get("data-src")
                if src and not src.startswith("data:"):
                    product_data["image"] = self._abs(src)

            price_el = item.css_first(".product_price, [class*=price]")
            if price_el:
                price_text = price_el.text(strip=True)
                product_data["price"] = self._parse_price(price_text)
                old = self._parse_old_price(price_text)
                if old and old != product_data.get("price"):
                    product_data["old_price"] = old
                    if product_data.get("price") and old:
                        product_data["discount_percent"] = round((1 - product_data["price"] / old) * 100)

            products.append(product_data)

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        has_next = False
        max_page = 1

        # Odoo pagination: ul.pagination > li > a
        for a in tree.css("ul.pagination li a, .pagination a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
            href = a.attributes.get("href", "")
            m = re.search(r"/page/(\d+)", href)
            if m:
                try:
                    num = int(m.group(1))
                    if num > max_page:
                        max_page = num
                except ValueError:
                    pass

        # Check for next page link
        for a in tree.css("ul.pagination li a, .pagination a"):
            href = a.attributes.get("href", "")
            if "/page/" in href:
                has_next = True
                break

        # Also look at total count
        count_el = tree.css_first(".tp-product-count, [class*=product-count], .showing-results")
        if count_el:
            m = re.search(r"(\d+)\s+articles", count_el.text())
            if m:
                total = int(m.group(1))
                if total > 50:
                    has_next = True
                    max_page = max(max_page, (total + 49) // 50)

        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        tree = HTMLParser(html)
        data = {"url": url}

        title_el = tree.css_first("h1, .product-title, [itemprop='name']")
        data["title"] = self._clean(title_el.text(strip=True)) if title_el else None

        # Odoo product pages have JSON-LD or meta price
        price_el = tree.css_first("[itemprop='price'], .product_price, .css_price, .oe_price")
        if price_el:
            price_content = price_el.attributes.get("content")
            if price_content:
                try:
                    data["price"] = float(price_content)
                except (ValueError, TypeError):
                    data["price"] = self._parse_price(price_el.text())
            else:
                data["price"] = self._parse_price(price_el.text())
        else:
            data["price"] = None

        ref_el = tree.css_first(".product-reference span, [itemprop='sku']")
        data["sku"] = self._clean(ref_el.text(strip=True)) if ref_el else None

        avail_el = tree.css_first(".availability, [itemprop='availability'], .css_availability")
        if avail_el:
            avail_href = avail_el.attributes.get("href", "")
            avail_text = self._clean(avail_el.text(strip=True))
            data["availability"] = avail_text or avail_href
            data["available"] = "InStock" in avail_href or "stock" in avail_text.lower() or "disponible" in avail_text.lower()
        else:
            data["availability"] = None
            data["available"] = None

        desc_el = tree.css_first("#description, .product_description, .tab-pane#description div")
        data["description"] = self._clean(desc_el.text(strip=True)) if desc_el else None

        # Odoo product attributes table
        specs = {}
        for row in tree.css("table.table.table-sm tr, .product_attributes tr"):
            cells = row.css("td, th")
            if len(cells) >= 2:
                k = self._clean(cells[0].text(strip=True))
                v = self._clean(cells[1].text(strip=True))
                if k and v:
                    specs[k] = v
        data["specifications"] = specs

        images = []
        for img in tree.css("#o-carousel-product img, .o_product_page_gallery img, .product-image img"):
            src = img.attributes.get("src") or img.attributes.get("data-src")
            if src and not src.startswith("data:") and src not in images:
                images.append(self._abs(src))
        data["images"] = images[:10] if images else None

        return data


def get_scraper(logger: logging.Logger) -> DokaniScraper:
    return DokaniScraper(logger)
