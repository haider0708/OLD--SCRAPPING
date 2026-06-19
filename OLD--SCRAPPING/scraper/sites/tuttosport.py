#!/usr/bin/env python3
"""
Tuttosport.com.tn scraper — PrestaShop, httpx (no Cloudflare).
Categories: /{id}-{slug}
Products:   /{category}/{id}-{combination}-{slug}.html
Pagination: ?page=N
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


class TuttoSportScraper(FastScraper):
    """httpx scraper for tuttosport.com.tn (PrestaShop)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("tuttosport", logger)
        self._page_sem = asyncio.Semaphore(4)
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

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

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
        # PrestaShop TN format: "399,90 TND" — comma is decimal separator
        cleaned = re.sub(r"[^\d,.]", "", text).strip()
        if not cleaned:
            return None
        # If comma present with exactly 2 digits after → decimal comma
        m = re.match(r"^(\d+),(\d{1,2})$", cleaned)
        if m:
            cleaned = f"{m.group(1)}.{m.group(2)}"
        else:
            # Tunisian thousands: "1.399,90" → remove dots, replace comma
            if "," in cleaned and "." in cleaned:
                cleaned = cleaned.replace(".", "").replace(",", ".")
            elif "," in cleaned:
                cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        import re as _re

        # Tuttosport nav: ul.sf-menu inside #block_top_menu
        # Top-level li are direct children; subcategories are nested ul li
        top_lis = tree.css("#block_top_menu ul.sf-menu > li")
        for top_li in top_lis:
            # First <a> inside this li (not in sub-ul)
            top_a = None
            for child in top_li.iter():
                if child.tag == "a":
                    top_a = child
                    break
            if not top_a:
                continue
            top_href = self._abs(top_a.attributes.get("href", ""))
            top_name = self._clean(top_a.text())
            if not top_name or not top_href or top_href in seen:
                continue
            if top_href.rstrip("/") == self.base_url.rstrip("/"):
                continue
            # Only keep numeric-id category URLs like /14-homme
            if not _re.search(r"/\d+-", top_href) and "/nouveaux" not in top_href and "/fabricants" not in top_href and "/promo" not in top_href:
                continue
            seen.add(top_href)

            top_cat = {
                "name": top_name,
                "url": top_href,
                "level": "top",
                "low_level_categories": []
            }

            for sub_li in top_li.css("ul li"):
                sub_a = sub_li.css_first("a")
                if not sub_a:
                    continue
                sub_href = self._abs(sub_a.attributes.get("href", ""))
                sub_name = self._clean(sub_a.text())
                if not sub_name or not sub_href or sub_href in seen:
                    continue
                if not _re.search(r"/\d+-", sub_href):
                    continue
                seen.add(sub_href)
                top_cat["low_level_categories"].append({
                    "name": sub_name,
                    "url": sub_href,
                    "level": "low",
                    "subcategories": []
                })

            categories.append(top_cat)

        return {"categories": categories}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        # PrestaShop product card: div.product-miniature or article.product-miniature
        for item in tree.css("div.product-miniature, article.product-miniature, .js-product-miniature"):
            product = {}

            # ID from data attribute
            pid = item.attributes.get("data-id-product") or item.attributes.get("data-id")
            if pid:
                product["id"] = str(pid)

            # URL
            link = item.css_first("a.thumbnail, a.product-thumbnail, h3.product-title a, .product-title a, a")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            # Extract clean URL without combination hash
            product["url"] = re.sub(r"#.*$", "", product["url"])

            # Name
            name_el = item.css_first("h3.product-title, h2.product-title, .product-title, h3, h2")
            if name_el:
                product["name"] = self._clean(name_el.text())

            # Image
            img = item.css_first("img.js-image-product, img.img-responsive, img")
            if img:
                src = (img.attributes.get("src") or img.attributes.get("data-src") or "")
                if src and not src.startswith("data:"):
                    # PrestaShop: swap small_default → large_default for better quality
                    src = src.replace("small_default", "large_default").replace("home_default", "large_default")
                    product["image"] = self._abs(src)

            # Price
            price_el = item.css_first("span.price, .product-price-and-shipping .price")
            old_price_el = item.css_first("span.regular-price, .product-price-and-shipping .regular-price")
            if price_el:
                product["price"] = self._parse_price(price_el.text())
            if old_price_el:
                product["old_price"] = self._parse_price(old_price_el.text())
            if product.get("price") and product.get("old_price") and product["old_price"] > 0:
                product["discount_percent"] = round(
                    (1 - product["price"] / product["old_price"]) * 100
                )

            product["shop"] = "tuttosport"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)

        # Name
        name_el = tree.css_first("h1.page-title, h1[itemprop='name'], h1")
        if name_el:
            details["name"] = self._clean(name_el.text())

        # Price
        price_el = tree.css_first(".current-price span.price, .product-price .price, [itemprop='price']")
        old_price_el = tree.css_first(".product-price .regular-price, .has-discount .regular-price")
        if price_el:
            details["price"] = self._parse_price(
                price_el.attributes.get("content") or price_el.text()
            )
        if old_price_el:
            details["old_price"] = self._parse_price(old_price_el.text())
        if details.get("price") and details.get("old_price") and details["old_price"] > 0:
            details["discount_percent"] = round(
                (1 - details["price"] / details["old_price"]) * 100
            )

        # SKU / Reference
        ref_el = tree.css_first(".product-reference span, [itemprop='sku'], #product-reference span")
        if ref_el:
            details["sku"] = self._clean(ref_el.text())

        # Description
        desc_el = tree.css_first(
            "#product-description-short, .product-description-short, "
            "[itemprop='description'], #tab-description"
        )
        if desc_el:
            details["description"] = self._clean(desc_el.text())

        # Images
        images = []
        for img in tree.css(".product-images img, #product-images-large img, .slick-slide img, .product-cover img"):
            src = img.attributes.get("src") or img.attributes.get("data-image-large-src") or ""
            if src and not src.startswith("data:"):
                abs_src = self._abs(src.replace("small_default", "large_default").replace("home_default", "large_default"))
                if abs_src and abs_src not in images:
                    images.append(abs_src)
        if images:
            details["images"] = images
            if not details.get("image"):
                details["image"] = images[0]

        # Availability
        avail_el = tree.css_first(".product-available, .in-stock, .out-of-stock, [class*='availability']")
        if avail_el:
            details["availability"] = self._clean(avail_el.text())

        # Brand
        brand_el = tree.css_first(".product-manufacturer a, [itemprop='brand'], .brand")
        if brand_el:
            details["brand"] = self._clean(brand_el.text())

        return details

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css(".pagination a, ul.pagination li a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first(".pagination li.next a, a[rel='next']") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        next_el = tree.css_first(
            f"a[href*='page={current_page + 1}'], "
            ".pagination li.next a, a[rel='next']"
        )
        return next_el is not None

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
            await asyncio.sleep(0.5)
        return all_products

    async def scrape(self) -> dict:
        self.logger.info("Starting tuttosport scrape")

        html = await self.fetch_html(self.base_url)
        if not html:
            self.logger.error("Failed to fetch homepage")
            return {"products": [], "categories": []}

        cat_data = self.extract_categories_from_html(html)
        categories = cat_data.get("categories", [])
        self.logger.info(f"Found {len(categories)} top categories")

        leaf_cats = []
        for top in categories:
            subs = top.get("low_level_categories", [])
            if subs:
                for low in subs:
                    leaf_cats.append({
                        "url": low["url"],
                        "top_category": top["name"],
                        "low_category": low["name"],
                        "subcategory": "",
                    })
            else:
                leaf_cats.append({
                    "url": top["url"],
                    "top_category": top["name"],
                    "low_category": "",
                    "subcategory": "",
                })

        all_products = []
        seen_ids = set()
        for cat in leaf_cats:
            prods = await self.scrape_category(cat)
            for p in prods:
                uid = p.get("id") or p.get("url")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    all_products.append(p)

        self.logger.info(f"Total unique products: {len(all_products)}")
        return {"products": all_products, "categories": categories}


def get_scraper(logger: logging.Logger) -> TuttoSportScraper:
    return TuttoSportScraper(logger)
