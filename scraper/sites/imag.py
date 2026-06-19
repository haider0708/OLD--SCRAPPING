#!/usr/bin/env python3
"""
Imag.tn scraper — WooCommerce WoodMart theme, no CF, httpx.
Same WoodMart structure as electrohadjkacem.
"""
import asyncio
import logging
import re
import time
from typing import List, Optional

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

_MIN_INTERVAL = 0.5


class ImagScraper(FastScraper):
    """httpx scraper for imag.tn (WooCommerce WoodMart)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("imag", logger)
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
        base = base_url.rstrip("/")
        base = re.sub(r"/page/\d+$", "", base)
        return f"{base}/page/{page_num}/"

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
        cleaned = re.sub(r"[^\d.,]", "", text)
        cleaned = re.sub(r"\s+", "", cleaned)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    def _valid_img(self, url: str) -> bool:
        return bool(url) and not url.startswith("data:") and (url.startswith("http") or url.startswith("//"))

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        top_items = tree.css("ul.primary-nav > li.menu-item, nav ul > li.menu-item, ul.nav-menu > li.menu-item, ul.main-menu > li.menu-item")
        for top_li in top_items:
            top_link = top_li.css_first("a")
            if not top_link:
                continue
            top_name = self._clean(top_link.text(strip=True))
            top_url = self._abs(top_link.attributes.get("href", ""))
            if not top_name or top_name in seen:
                continue
            if not top_url or "javascript" in top_url:
                continue
            seen.add(top_name)

            top_cat = {"name": top_name, "url": top_url, "level": "top", "low_level_categories": []}
            sub_links = top_li.css("ul.sub-menu li.menu-item a")
            if sub_links:
                low_cat = {"name": top_name, "url": top_url, "level": "low", "subcategories": []}
                for sl in sub_links:
                    sn = self._clean(sl.text(strip=True))
                    su = self._abs(sl.attributes.get("href", ""))
                    if sn and su:
                        low_cat["subcategories"].append({"name": sn, "url": su, "level": "subcategory"})
                if low_cat["subcategories"]:
                    top_cat["low_level_categories"].append(low_cat)
            categories.append(top_cat)

        # Fallback: produit-categorie links (with accent variants)
        if not categories:
            for a in tree.css("a[href*='produit-cat'], a[href*='product-category']"):
                href = self._abs(a.attributes.get("href", ""))
                name = self._clean(a.text(strip=True))
                if name and href and name not in seen:
                    seen.add(name)
                    categories.append({"name": name, "url": href, "level": "top", "low_level_categories": []})

        self.logger.info(f"Found {len(categories)} top-level categories")
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["top_level"] += 1
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top.get("low_level_categories", []):
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
                for sub in low.get("subcategories", []):
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        for item in tree.css("div.wd-product, div.product-grid-item.type-product"):
            link_el = item.css_first("a.wd-entities-title-link, a.product-link, a")
            if not link_el:
                continue
            product_url = self._abs(link_el.attributes.get("href", ""))
            if not product_url or product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            product_id = None
            m = re.search(r"post-(\d+)", item.attributes.get("class", ""))
            if m:
                product_id = m.group(1)

            name_el = item.css_first(".wd-entities-title, h2.woocommerce-loop-product__title, h3")
            product_data = {
                "id": product_id,
                "url": product_url,
                "name": self._clean(name_el.text(strip=True)) if name_el else "",
            }

            img_el = item.css_first("img")
            if img_el:
                src = img_el.attributes.get("data-src") or img_el.attributes.get("src") or img_el.attributes.get("data-lazy-src")
                if src and self._valid_img(src):
                    product_data["image"] = self._abs(src)

            price_el = item.css_first("span.price ins span.woocommerce-Price-amount bdi, span.price span.woocommerce-Price-amount bdi, span.woocommerce-Price-amount bdi")
            if price_el:
                product_data["price"] = self._parse_price(price_el.text())

            old_price_el = item.css_first("del span.woocommerce-Price-amount bdi")
            if old_price_el:
                product_data["old_price"] = self._parse_price(old_price_el.text())
                if product_data.get("old_price") and product_data.get("price"):
                    product_data["discount_percent"] = round((1 - product_data["price"] / product_data["old_price"]) * 100)

            if item.css_first(".stock.out-of-stock"):
                product_data["availability"] = "Rupture de stock"
                product_data["available"] = False

            products.append(product_data)

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        next_link = tree.css_first("a.next.page-numbers")
        has_next = next_link is not None
        max_page = 1
        for a in tree.css("ul.page-numbers a.page-numbers, nav.woocommerce-pagination a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        current_el = tree.css_first("span.page-numbers.current")
        current_page = 1
        if current_el:
            try:
                current_page = int(current_el.text(strip=True))
            except ValueError:
                pass
        return {"current_page": current_page, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        tree = HTMLParser(html)
        data = {"url": url}

        title_el = tree.css_first("h1.product_title, h1.entry-title")
        data["title"] = self._clean(title_el.text(strip=True)) if title_el else None

        # WooCommerce UGS: span.sku_wrapper span.sku; fallback: "Référence:" in description
        sku_el = tree.css_first("span.sku_wrapper span.sku, span.sku, [itemprop='sku']")
        if sku_el:
            data["sku"] = self._clean(sku_el.text(strip=True))
        else:
            data["sku"] = None
            for el in tree.css(".woocommerce-product-details__short-description h3, .entry-content h3, p"):
                txt = el.text(strip=True)
                if re.search(r"Référence\s*:", txt, re.IGNORECASE):
                    m = re.search(r":\s*(\S+)", txt)
                    if m:
                        data["sku"] = m.group(1)
                        break

        price_el = tree.css_first("p.price ins span.woocommerce-Price-amount bdi, p.price span.woocommerce-Price-amount bdi")
        data["price"] = self._parse_price(price_el.text()) if price_el else None

        old_price_el = tree.css_first("p.price del span.woocommerce-Price-amount bdi")
        if old_price_el:
            data["old_price"] = self._parse_price(old_price_el.text())
            if data.get("old_price") and data.get("price"):
                data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)

        stock_el = tree.css_first("p.stock.in-stock, p.stock.out-of-stock")
        if stock_el:
            data["availability"] = self._clean(stock_el.text(strip=True))
            data["available"] = "in-stock" in (stock_el.attributes.get("class") or "")
        else:
            data["availability"] = None
            data["available"] = None

        desc_el = tree.css_first("div.woocommerce-product-details__short-description")
        data["description"] = self._clean(desc_el.text(strip=True)) if desc_el else None

        specs = {}
        for row in tree.css("table.woocommerce-product-attributes tr"):
            k_el = row.css_first("th.woocommerce-product-attributes-item__label")
            v_el = row.css_first("td.woocommerce-product-attributes-item__value")
            if k_el and v_el:
                k, v = self._clean(k_el.text(strip=True)), self._clean(v_el.text(strip=True))
                if k and v:
                    specs[k] = v
        data["specifications"] = specs

        images = []
        for img in tree.css("div.woocommerce-product-gallery__image img"):
            src = img.attributes.get("data-large_image") or img.attributes.get("data-src") or img.attributes.get("src")
            if src and self._valid_img(src) and src not in images:
                images.append(self._abs(src))
        data["images"] = images[:10] if images else None

        return data


def get_scraper(logger: logging.Logger) -> ImagScraper:
    return ImagScraper(logger)
