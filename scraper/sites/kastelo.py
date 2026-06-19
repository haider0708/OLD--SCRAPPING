#!/usr/bin/env python3
"""
Kastelo.com.tn scraper — custom NetAdvisor platform, httpx (no CF).
Categories: /filter/{CategoryName}/{id}
Products:   /produit/{slug}/{id}
Pagination: load-more (all products returned on first page, 52+ per page)
Images:     fileskastelo.com/uploads/product/initial/{id}/...
"""
import asyncio
import logging
import re
import time
from typing import List, Optional
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
_MIN_INTERVAL = 0.4

# All known categories from the nav (slug → id)
KASTELO_CATEGORIES = [
    ("Pulls", 30),
    ("Accessoires", 31),
    ("Pantalons", 32),
    ("Chemises", 33),
    ("Manteaux", 34),
    ("Vestes", 35),
    ("Jeans", 36),
    ("Robes", 37),
    ("Blazer", 38),
    ("Jupes", 39),
    ("T-shirts", 40),
    ("Shorts", 41),
    ("Tops-&-Débardeurs", 42),
    ("Combinaisons", 43),
]


class KasteloCcraper(FastScraper):
    """httpx scraper for kastelo.com.tn (NetAdvisor platform)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("kastelo", logger)
        self._page_sem = asyncio.Semaphore(3)
        self._rate_lock = asyncio.Lock()
        self._last_req = 0.0

    async def fetch_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        async with self._page_sem:
            async with self._rate_lock:
                now = time.monotonic()
                wait = _MIN_INTERVAL - (now - self._last_req)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_req = time.monotonic()
            return await super().fetch_html(url, raise_on_error=raise_on_error)

    def _abs(self, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return "https:" + url
        return urljoin(self.base_url, url)

    def _clean(self, text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _parse_price(self, text: str) -> Optional[float]:
        if not text:
            return None
        # Format: "44.90 TND" or "44,90 TND"
        cleaned = re.sub(r"[^\d,.]", "", text).strip()
        if not cleaned:
            return None
        cleaned = cleaned.replace(",", ".")
        parts = cleaned.split(".")
        if len(parts) > 2:
            # e.g. "44.90.00" → keep first two
            cleaned = parts[0] + "." + parts[1]
        try:
            return float(cleaned)
        except ValueError:
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        # Kastelo nav: links matching /filter/
        for a in tree.css("a[href*='/filter/']"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            seen.add(href)
            # Extract category id from URL: /filter/T-shirts/40
            m = re.search(r"/filter/([^/]+)/(\d+)", href)
            cat_id = m.group(2) if m else None
            categories.append({
                "name": name,
                "url": href,
                "id": cat_id,
                "level": "top",
                "low_level_categories": []
            })

        # If nav parse failed, fall back to hardcoded list
        if not categories:
            for slug, cat_id in KASTELO_CATEGORIES:
                url = f"{self.base_url}/filter/{slug}/{cat_id}"
                categories.append({
                    "name": slug.replace("-", " ").replace("&", "&"),
                    "url": url,
                    "id": str(cat_id),
                    "level": "top",
                    "low_level_categories": []
                })

        return {"categories": categories}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        seen_ids = set()
        # Kastelo: product cards are div.product-item, each has one image link to /produit/
        for card in tree.css("div.product-item"):
            # Find the product link (image anchor)
            link = card.css_first("a[href*='/produit/']")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            m = re.search(r"/produit/[^/]+/(\d+)", href)
            if not m:
                continue
            pid = m.group(1)
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            product = {
                "id": pid,
                "url": href,
                "shop": "kastelo",
                "top_category": top_cat,
                "low_category": low_cat,
                "subcategory": subcat,
            }

            # Name from img alt (most reliable on listing page)
            img = card.css_first("img")
            if img:
                src = img.attributes.get("src") or img.attributes.get("data-src") or ""
                alt = img.attributes.get("alt", "")
                if src and not src.startswith("data:"):
                    product["image"] = self._abs(src)
                if alt:
                    product["name"] = self._clean(alt)

            # Price not shown on listing page — will be filled by details scrape
            products.append(product)

        return products

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)

        # Product info block: .single-product-info
        info = tree.css_first(".single-product-info")

        # Name: h4.title inside single-product-info (first one is the product)
        name_el = info.css_first("h4.title") if info else None
        if not name_el:
            name_el = tree.css_first(".single-product-info h4, h4.title")
        if name_el:
            details["name"] = self._clean(name_el.text())

        # Price: span.price inside .prices (first one = current, second if exists = old)
        prices_block = info.css_first(".prices") if info else tree.css_first(".prices")
        if prices_block:
            price_els = prices_block.css("span.price")
            if price_els:
                details["price"] = self._parse_price(price_els[0].text())
            old_el = prices_block.css_first(".old-price, del, s")
            if old_el:
                details["old_price"] = self._parse_price(old_el.text())
        else:
            # fallback
            price_el = tree.css_first("span.price")
            if price_el:
                details["price"] = self._parse_price(price_el.text())

        if details.get("price") and details.get("old_price") and details["old_price"] > 0:
            details["discount_percent"] = round(
                (1 - details["price"] / details["old_price"]) * 100
            )

        # SKU: [class*=sku] contains "Réference:XXXXX Code article:YYYYYYY"
        sku_el = tree.css_first("[class*='sku']")
        if sku_el:
            raw = self._clean(sku_el.text())
            # Extract reference number
            m = re.search(r"[Rr]é?f[eé]rence\s*:?\s*(\S+)", raw)
            if m:
                details["sku"] = m.group(1)
            # Extract barcode
            m2 = re.search(r"[Cc]ode\s+article\s*:?\s*(\d+)", raw)
            if m2:
                details["barcode"] = m2.group(1)

        # Description: look in single-product-info for paragraph text
        desc_el = (info.css_first("p") if info else None) or tree.css_first(".product-desc p, .product-description p")
        if desc_el:
            txt = self._clean(desc_el.text())
            if txt and len(txt) > 10:
                details["description"] = txt

        # Composition
        comp_h = tree.css_first("h3.title")
        if comp_h and "Composition" in comp_h.text():
            # Sibling paragraph after the h3
            node = comp_h
            for _ in range(5):
                node = node.next  # type: ignore
                if node is None:
                    break
                if node.tag in ("p", "div", "span"):
                    txt = self._clean(node.text())
                    if txt:
                        details["composition"] = txt
                        break

        # Images — single-product-thumb-slider contains all product images
        images = []
        for img in tree.css(".single-product-thumb-slider img, img[src*='fileskastelo.com/uploads/product']"):
            src = img.attributes.get("src") or img.attributes.get("data-src") or ""
            if src and not src.startswith("data:") and "fileskastelo.com/uploads/product" in src:
                abs_src = self._abs(src)
                if abs_src and abs_src not in images:
                    images.append(abs_src)
        if images:
            details["images"] = images
            if not details.get("image"):
                details["image"] = images[0]

        # Sizes: list items in hover-anis div (on listing) or size selector on detail
        sizes = []
        for a in tree.css(".single-product-info ul li a, [class*='size'] ul li a"):
            sz = self._clean(a.text())
            if sz and len(sz) <= 5 and sz.lower() not in ("xs", "s", "m", "l", "xl", "xxl", "xxxl", "xxs") or sz.upper() in ("XS","XXS","S","M","L","XL","XXL","XXXL"):
                sizes.append(sz.upper())
        # Deduplicate while preserving order
        seen_sz = set()
        sizes = [s for s in sizes if not (s in seen_sz or seen_sz.add(s))]
        if sizes:
            details["sizes"] = sizes

        return details

    def build_page_url(self, base_url: str, page_num: int) -> str:
        # Kastelo loads all products on one page — no pagination
        return base_url

    def extract_pagination_from_html(self, html: str) -> dict:
        # Kastelo uses load-more, all products served on page 1
        return {"current_page": 1, "total_pages": 1, "has_next": False}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def has_next_page(self, html: str, current_page: int) -> bool:
        return False

    async def scrape_category(self, category: dict) -> List[dict]:
        url = category.get("url")
        if not url:
            return []
        html = await self.fetch_html(url)
        if not html:
            return []
        products = self.extract_products_from_html(html, category)
        self.logger.info(f"  {len(products)} products ({url})")
        return products

    async def scrape(self) -> dict:
        self.logger.info("Starting kastelo scrape")

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


def get_scraper(logger: logging.Logger) -> KasteloCcraper:
    return KasteloCcraper(logger)
