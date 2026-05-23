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
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        # Use regex on raw HTML to capture hrefs inside <template> tags too,
        # since selectolax skips content inside <template> elements.
        SKIP_SLUGS = {"promotions", "nouveautes", "all", "frontpage"}
        seen_slugs = set()
        categories = []

        # Extract name+href pairs from ALL <a> tags in raw HTML (including inside <template>)
        # Pattern: captures href and then looks for visible text nearby
        # We parse with regex to get href, then use selectolax for the visible-DOM name fallback.
        raw_pairs = re.findall(
            r'<a[^>]+href="(/collections/([^"/]+))"[^>]*>(.*?)</a>',
            html,
            re.DOTALL | re.IGNORECASE,
        )

        for href, slug, inner_html in raw_pairs:
            if slug in SKIP_SLUGS or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            # Strip tags from inner HTML to get text
            name = re.sub(r"<[^>]+>", " ", inner_html).strip()
            name = re.sub(r"\s+", " ", name).strip()
            if not name:
                continue
            url = f"https://bill.tn{href}"
            categories.append({
                "name": name,
                "url": url,
                "level": "top",
                "low_level_categories": [],
            })

        # Also catch absolute URLs like href="https://bill.tn/collections/slug"
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

        stats = {
            "top_level": len(categories),
            "low_level": 0,
            "subcategory": 0,
            "total_urls": len(categories),
        }
        self.logger.info(f"Extracted {len(categories)} categories (including sub-nav)")
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
    # Product detail
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Shopify embeds full product JSON in a <script id="ProductJson-*"> or
        # <script type="application/json" data-product-json> tag
        product_json = None
        for script in tree.css('script[id^="ProductJson"], script[data-product-json], script[type="application/json"]'):
            raw = (script.text() or "").strip()
            if not raw or '"variants"' not in raw:
                continue
            try:
                product_json = json.loads(raw)
                break
            except json.JSONDecodeError:
                continue

        if product_json:
            variant = (product_json.get("variants") or [{}])[0]
            # Shopify stores price in cents (integer)
            raw_price = variant.get("price", 0)
            raw_compare = variant.get("compare_at_price") or 0
            price = float(raw_price) / 100 if isinstance(raw_price, int) and raw_price > 1000 else self._parse_price(str(raw_price))
            compare_at = float(raw_compare) / 100 if isinstance(raw_compare, int) and raw_compare > 1000 else self._parse_price(str(raw_compare))
            images = [img.get("src") for img in (product_json.get("images") or []) if img.get("src")]
            body_html = product_json.get("body_html") or product_json.get("description") or ""
            data.update({
                "product_id": str(product_json.get("id", "")),
                "title": product_json.get("title"),
                "sku": variant.get("sku"),
                "price": price,
                "old_price": compare_at if compare_at and compare_at != price else None,
                "availability": "En stock" if variant.get("available") else "Rupture de stock",
                "available": variant.get("available", False),
                "description": re.sub(r"<[^>]+>", " ", body_html).strip(),
                "images": images[:10],
                "specifications": {},
            })
            return data

        # Fallback: parse HTML selectors
        title_el = tree.css_first("h1.product-single__title, h1.product__title, h1[itemprop='name'], h1")
        data["title"] = title_el.text(strip=True) if title_el else None

        price_el = tree.css_first(
            "span.product-price__price, "
            "span[itemprop='price'], "
            "div.product-price span.price-item--regular, "
            "span.price-item"
        )
        data["price"] = self._parse_price(price_el.text(strip=True)) if price_el else None

        compare_el = tree.css_first("span.price-item--regular s, s.price-item--regular")
        data["old_price"] = self._parse_price(compare_el.text(strip=True)) if compare_el else None

        sku_el = tree.css_first("span.product-single__sku-number, span.variant-sku, span.sku")
        data["sku"] = sku_el.text(strip=True) if sku_el else None

        desc_el = tree.css_first(
            "div.product-single__description, "
            "div.product__description, "
            "div[class*='product-description']"
        )
        data["description"] = desc_el.text(strip=True) if desc_el else None

        avail_el = tree.css_first("span.product__availability, span[class*='availability']")
        data["availability"] = avail_el.text(strip=True) if avail_el else None
        data["available"] = "stock" in (data["availability"] or "").lower() if data["availability"] else None

        images = []
        for img in tree.css("div.product-single__media img, div.product__media img, div[class*='product-gallery'] img"):
            src = img.attributes.get("src") or img.attributes.get("data-src")
            if src and not src.startswith("data:") and src not in images:
                images.append(src)
        data["images"] = images[:10]
        data["specifications"] = {}
        return data


def get_scraper(logger: logging.Logger) -> BillScraper:
    return BillScraper(logger)
