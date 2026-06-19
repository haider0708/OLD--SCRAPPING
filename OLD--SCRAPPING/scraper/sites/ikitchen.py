#!/usr/bin/env python3
"""
Ikitchen.com.tn scraper — Shopify, no CF, uses /products.json API for fast listing.
Collections API: /collections/{handle}/products.json?limit=250&page=N
"""
import asyncio
import json
import logging
import re
import time
from typing import List, Optional

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

_IKITCHEN_MIN_INTERVAL = 1.0  # seconds between requests


class IkitchenScraper(FastScraper):
    """httpx scraper for ikitchen.com.tn (Shopify via products.json API)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("ikitchen", logger)
        self._page_sem = asyncio.Semaphore(2)
        self._last_request = 0.0

    def build_page_url(self, base_url: str, page_num: int) -> str:
        # For products.json API URLs
        if "products.json" in base_url:
            base = re.sub(r"[?&]page=\d+", "", base_url)
            sep = "&" if "?" in base else "?"
            return f"{base}{sep}page={page_num}"
        # For HTML collection pages
        base = re.sub(r"[?&]page=\d+", "", base_url)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

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
        if isinstance(text, (int, float)):
            return float(text)
        cleaned = re.sub(r"[^\d.,]", "", str(text))
        cleaned = re.sub(r"\s+", "", cleaned)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    def _shopify_price(self, cents_str) -> Optional[float]:
        """Shopify stores TND prices as plain integers e.g. '219' = 219 TND."""
        try:
            return round(float(cents_str), 3)
        except (ValueError, TypeError):
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract Shopify collections from homepage HTML."""
        tree = HTMLParser(html)
        categories = []
        seen = set()

        # First try collections.json API (fetched separately in a custom override)
        # Fall back to scraping nav links
        for a in tree.css("a[href*='/collections/']"):
            href = a.attributes.get("href", "")
            abs_url = self._abs(href)
            if not abs_url or abs_url in seen:
                continue
            # Normalize
            abs_url = abs_url.rstrip("/") + "/"
            if abs_url in seen:
                continue
            # Skip /collections/all and root
            if abs_url.rstrip("/").endswith("/collections") or "/collections/all" in abs_url:
                continue
            seen.add(abs_url)
            name = self._clean(a.text(strip=True))
            if not name:
                # derive name from slug
                slug = abs_url.rstrip("/").split("/")[-1]
                name = slug.replace("-", " ").title()
            categories.append({"name": name, "url": abs_url, "level": "top", "low_level_categories": []})

        self.logger.info(f"Found {len(categories)} collections")
        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": len(categories)}
        return {"categories": categories, "stats": stats}

    def _collection_handle(self, url: str) -> Optional[str]:
        """Extract collection handle from URL like /collections/robots-multifonction/"""
        m = re.search(r"/collections/([^/?#]+)", url)
        return m.group(1) if m else None

    async def fetch_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        """Override to use products.json API and throttle to avoid 429s."""
        handle = self._collection_handle(url)
        if handle and "products.json" not in url:
            api_url = f"{self.base_url}/collections/{handle}/products.json?limit=250"
            m = re.search(r"[?&]page=(\d+)", url)
            if m:
                api_url += f"&page={m.group(1)}"
        else:
            api_url = url

        async with self._page_sem:
            now = time.monotonic()
            wait = _IKITCHEN_MIN_INTERVAL - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            return await super().fetch_html(api_url, raise_on_error=raise_on_error)

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Parse Shopify products.json response."""
        if not html:
            return []

        # Try JSON first (products.json API)
        try:
            data = json.loads(html)
            shopify_products = data.get("products", [])
            if shopify_products is not None:
                return self._parse_shopify_json(shopify_products)
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: parse HTML product cards
        return self._parse_html_products(html)

    def _parse_shopify_json(self, products: list) -> List[dict]:
        result = []
        for p in products:
            handle = p.get("handle", "")
            product_url = f"{self.base_url}/products/{handle}"
            variants = p.get("variants", [{}])
            first_var = variants[0] if variants else {}

            price_cents = first_var.get("price", "0")
            compare_cents = first_var.get("compare_at_price")

            price = self._shopify_price(price_cents)
            old_price = self._shopify_price(compare_cents) if compare_cents and compare_cents != "0" else None

            images = p.get("images", [])
            image_url = images[0].get("src") if images else None

            product_data = {
                "id": str(p.get("id", "")),
                "url": product_url,
                "name": self._clean(p.get("title", "")),
                "price": price,
                "image": image_url,
                "sku": first_var.get("sku") or None,
                "availability": "En stock" if first_var.get("available") else "Rupture de stock",
                "available": bool(first_var.get("available")),
            }
            if old_price and price and old_price > price:
                product_data["old_price"] = old_price
                product_data["discount_percent"] = round((1 - price / old_price) * 100)

            result.append(product_data)
        return result

    def _parse_html_products(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        for item in tree.css(".product-card__wrapper, [class*=product-card]"):
            link_el = item.css_first("a[href*='/products/']")
            if not link_el:
                continue
            product_url = self._abs(link_el.attributes.get("href", ""))
            if not product_url or product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            name_el = item.css_first(".product-card__title, h2, h3")
            price_el = item.css_first(".product-card__price, [class*=price]")
            img_el = item.css_first("img")

            product_data = {
                "id": None,
                "url": product_url,
                "name": self._clean(name_el.text(strip=True)) if name_el else "",
                "price": self._parse_price(price_el.text()) if price_el else None,
            }
            if img_el:
                src = img_el.attributes.get("src") or img_el.attributes.get("data-src")
                if src and not src.startswith("data:"):
                    product_data["image"] = src
            products.append(product_data)

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        """For Shopify products.json: has_next if products list is full (250 items)."""
        try:
            data = json.loads(html)
            prods = data.get("products", [])
            has_next = len(prods) == 250
            return {"current_page": 1, "total_pages": 999 if has_next else 1, "has_next": has_next}
        except (json.JSONDecodeError, ValueError):
            pass

        tree = HTMLParser(html)
        next_link = tree.css_first("a[rel='next'], a[href*='?page=']")
        return {"current_page": 1, "total_pages": 1, "has_next": next_link is not None}

    async def scrape_product_details(self, url: str) -> dict:
        """Fetch Shopify product JSON via /products/{handle}.js"""
        handle = url.rstrip("/").split("/products/")[-1].split("?")[0] if "/products/" in url else None
        if handle:
            api_url = f"{self.base_url}/products/{handle}.js"
            html = await self.fetch_html(api_url)
            if html:
                try:
                    p = json.loads(html)
                    variants = p.get("variants", [{}])
                    first_var = variants[0] if variants else {}
                    price = self._shopify_price(first_var.get("price", "0"))
                    compare = self._shopify_price(first_var.get("compare_at_price")) if first_var.get("compare_at_price") else None
                    images = p.get("images", [])
                    data = {
                        "url": url,
                        "title": self._clean(p.get("title", "")),
                        "sku": first_var.get("sku") or None,
                        "price": price,
                        "availability": "En stock" if first_var.get("available") else "Rupture de stock",
                        "available": bool(first_var.get("available")),
                        "description": re.sub(r"<[^>]+>", " ", p.get("description") or "").strip() or None,
                        "images": [img if isinstance(img, str) else img.get("src") for img in images][:10] if images else None,
                        "specifications": {},
                    }
                    if compare and price and compare > price:
                        data["old_price"] = compare
                        data["discount_percent"] = round((1 - price / compare) * 100)
                    return data
                except (json.JSONDecodeError, ValueError):
                    pass

        # HTML fallback
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        tree = HTMLParser(html)
        data = {"url": url}
        title_el = tree.css_first("h1, .product-title, [itemprop='name']")
        data["title"] = self._clean(title_el.text(strip=True)) if title_el else None
        price_el = tree.css_first("[itemprop='price'], .product__price .price")
        data["price"] = self._parse_price(price_el.attributes.get("content") or price_el.text()) if price_el else None
        data["specifications"] = {}
        return data


def get_scraper(logger: logging.Logger) -> IkitchenScraper:
    return IkitchenScraper(logger)
