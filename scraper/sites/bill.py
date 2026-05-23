#!/usr/bin/env python3
"""
Bill.tn scraper — Shopify (Electro Theme), Cloudflare proxy only, httpx.
Uses Shopify's /products.json API for listings (more reliable than HTML).
"""

import json
import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser
from scraper.base import FastScraper


class BillScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("bill", logger)

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        # Shopify: /collections/{slug}/products.json?page=N&limit=250
        # base_url is already the products.json URL for the collection
        base = re.sub(r"[?&]page=\d+", "", base_url)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    # ------------------------------------------------------------------
    # Frontpage override — fetch collections via Shopify JSON API
    # bill.tn blocks httpx on the HTML frontpage (403) but the JSON
    # API endpoints are accessible.
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        from scraper.base import save_text_atomic
        output_path = self.html_dir / "frontpage.html"
        # Shopify collections API — returns all collections as JSON
        api_url = "https://bill.tn/collections.json?limit=250"
        self.logger.info(f"Downloading bill.tn collections via API: {api_url}")
        raw = await self.fetch_html(api_url)
        if not raw:
            # Fall back to cached frontpage from a previous run if available
            if output_path.exists():
                self.logger.warning("API fetch failed — using cached frontpage.html")
                return output_path
            raise RuntimeError("Failed to fetch bill.tn collections and no cached frontpage found")
        save_text_atomic(raw, output_path, self.logger)
        return output_path

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        SKIP_SLUGS = {"promotions", "nouveautes", "all", "frontpage", ""}
        categories = []
        seen_slugs: set = set()

        # --- Path A: Shopify collections.json API response ---
        try:
            data = json.loads(html)
            collections = data.get("collections", [])
            if collections:
                for c in collections:
                    handle = c.get("handle", "")
                    if not handle or handle in SKIP_SLUGS:
                        continue
                    if handle in seen_slugs:
                        continue
                    seen_slugs.add(handle)
                    title = c.get("title") or handle.replace("-", " ").title()
                    categories.append({
                        "name": title,
                        "url": f"https://bill.tn/collections/{handle}",
                        "level": "top",
                        "low_level_categories": [],
                    })
                self.logger.info(f"Extracted {len(categories)} categories from Shopify API")
                stats = {"top_level": len(categories), "low_level": 0,
                         "subcategory": 0, "total_urls": len(categories)}
                return {"categories": categories, "stats": stats}
        except (json.JSONDecodeError, AttributeError):
            pass

        # --- Path B: HTML fallback (cached frontpage) ---
        # Use regex to capture hrefs inside <template> tags too,
        # since selectolax skips content inside <template> elements.
        raw_pairs = re.findall(
            r'<a[^>]+href="(/collections/([^"/]+))"[^>]*>(.*?)</a>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        for href, slug, inner_html in raw_pairs:
            if slug in SKIP_SLUGS or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            name = re.sub(r"<[^>]+>", " ", inner_html).strip()
            name = re.sub(r"\s+", " ", name).strip()
            if not name:
                continue
            categories.append({
                "name": name,
                "url": f"https://bill.tn{href}",
                "level": "top",
                "low_level_categories": [],
            })

        abs_pairs = re.findall(
            r'<a[^>]+href="(https://bill\.tn/collections/([^"/\s?]+))[^"]*"[^>]*>(.*?)</a>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        for full_url, slug, inner_html in abs_pairs:
            if slug in SKIP_SLUGS or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            name = re.sub(r"<[^>]+>", " ", inner_html).strip()
            name = re.sub(r"\s+", " ", name).strip()
            if not name:
                continue
            categories.append({
                "name": name,
                "url": full_url,
                "level": "top",
                "low_level_categories": [],
            })

        stats = {"top_level": len(categories), "low_level": 0,
                 "subcategory": 0, "total_urls": len(categories)}
        self.logger.info(f"Extracted {len(categories)} categories (HTML fallback)")
        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Products — parse Shopify JSON API response
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        # Shopify /products.json returns JSON, not HTML
        try:
            data = json.loads(html)
            raw_products = data.get("products", [])
        except (json.JSONDecodeError, AttributeError):
            # Fallback: parse HTML product cards
            return self._extract_products_from_html_fallback(html)

        products = []
        for p in raw_products:
            variant = (p.get("variants") or [{}])[0]
            price = self._parse_price(variant.get("price", ""))
            compare_at = self._parse_price(variant.get("compare_at_price") or "")
            image = None
            if p.get("images"):
                image = p["images"][0].get("src")
            products.append({
                "id": str(p.get("id", "")),
                "url": f"https://bill.tn/products/{p.get('handle', '')}",
                "name": p.get("title", ""),
                "price": price,
                "old_price": compare_at if compare_at and compare_at != price else None,
                "image": image,
                "sku": variant.get("sku") or str(p.get("id", "")),
            })
        return products

    def _extract_products_from_html_fallback(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        for card in tree.css("product-card, section.product-card, li.product-card"):
            a = card.css_first("a[href*='/products/']")
            if not a:
                continue
            href = a.attributes.get("href", "")
            url = href if href.startswith("http") else f"https://bill.tn{href}"
            name_el = card.css_first("h3.product-card_title, h2, h3")
            name = name_el.text(strip=True) if name_el else ""
            price_el = card.css_first("div.price-sale, div.product-price, span.price")
            price = self._parse_price(price_el.text(strip=True)) if price_el else None
            img = card.css_first("img")
            image = img.attributes.get("src") or img.attributes.get("data-src") if img else None
            products.append({"id": None, "url": url, "name": name, "price": price, "image": image})
        return products

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def extract_pagination_from_html(self, html: str) -> dict:
        # For JSON API responses, check if we got a full page of results
        try:
            data = json.loads(html)
            products = data.get("products", [])
            # Shopify default limit is 250; if we got fewer, it's the last page
            has_next = len(products) >= 250
            return {"current_page": 1, "total_pages": 999 if has_next else 1, "has_next": has_next}
        except (json.JSONDecodeError, AttributeError):
            pass

        tree = HTMLParser(html)
        next_link = tree.css_first("a[rel='next'], a.next")
        has_next = next_link is not None
        max_page = 1
        for a in tree.css("ul.pagination a, ul.page-numbers a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    # ------------------------------------------------------------------
    # Override: category URL → Shopify JSON API URL
    # ------------------------------------------------------------------

    def _category_url_to_api_url(self, url: str) -> str:
        """Convert /collections/{slug} to /collections/{slug}/products.json?limit=250"""
        url = url.rstrip("/")
        if "/products.json" not in url:
            url = f"{url}/products.json?limit=250"
        return url

    # ------------------------------------------------------------------
    # Price parsing — Shopify: "1060.00" or "1,060 DT"
    # ------------------------------------------------------------------

    def _parse_price(self, text: str) -> Optional[float]:
        if not text:
            return None
        text = str(text)
        cleaned = re.sub(r"[^\d.,]", "", text).strip()
        if not cleaned:
            return None
        # Shopify API gives "1060.00" (dot = decimal)
        if "." in cleaned and "," not in cleaned:
            try:
                return float(cleaned)
            except ValueError:
                return None
        # HTML text: "1,060 DT" — comma = thousands separator
        if "," in cleaned:
            cleaned = cleaned.replace(",", "")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Product detail — Shopify JSON API (avoids 403 on HTML product pages)
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        # Derive the handle from the product URL and call /products/{handle}.json
        # which is the Shopify storefront API — accessible without browser headers.
        handle = url.rstrip("/").rsplit("/products/", 1)[-1].split("?")[0]
        api_url = f"https://bill.tn/products/{handle}.json"

        raw = await self.fetch_html(api_url)
        if raw:
            try:
                data = json.loads(raw)
                p = data.get("product", data)  # some Shopify stores wrap, some don't
                if p and "variants" in p:
                    return self._parse_shopify_product_json(url, p)
            except (json.JSONDecodeError, AttributeError):
                pass

        # If JSON API also fails, return a minimal stub so the pipeline
        # doesn't crash — detail fields will be null but url/product_id survive.
        return {"url": url, "product_id": None, "title": None, "price": None,
                "old_price": None, "sku": None, "availability": None,
                "available": None, "description": None, "images": [],
                "specifications": {}, "error": "Failed to fetch"}

    def _parse_shopify_product_json(self, url: str, p: dict) -> dict:
        """Convert a Shopify product JSON object into our standard detail dict."""
        variant = (p.get("variants") or [{}])[0]

        raw_price = variant.get("price", 0)
        raw_compare = variant.get("compare_at_price") or 0

        # Shopify API returns prices as strings ("1060.00") from storefront JSON
        # and as integers (106000 = cents) from private Admin API.
        # Detect cents by checking if int value > plausible TND price (10000 TND max).
        def _to_price(v):
            if isinstance(v, int):
                return float(v) / 100 if v > 100000 else float(v)
            return self._parse_price(str(v)) if v else None

        price = _to_price(raw_price)
        compare_at = _to_price(raw_compare)

        images = [img.get("src") for img in (p.get("images") or []) if img.get("src")]
        body_html = p.get("body_html") or p.get("description") or ""
        description = re.sub(r"<[^>]+>", " ", body_html)
        description = re.sub(r"\s+", " ", description).strip() or None

        # Build specifications from metafields if present
        specs = {}
        for mf in p.get("metafields", []):
            k = mf.get("key") or mf.get("namespace")
            v = mf.get("value")
            if k and v:
                specs[str(k)] = str(v)

        return {
            "url": url,
            "product_id": str(p.get("id", "")),
            "title": p.get("title"),
            "sku": variant.get("sku") or str(p.get("id", "")),
            "price": price,
            "old_price": compare_at if compare_at and compare_at != price else None,
            "availability": "En stock" if variant.get("available") else "Rupture de stock",
            "available": bool(variant.get("available", False)),
            "description": description,
            "images": images[:10],
            "specifications": specs,
        }


def get_scraper(logger: logging.Logger) -> BillScraper:
    return BillScraper(logger)
