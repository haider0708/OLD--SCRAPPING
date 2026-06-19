#!/usr/bin/env python3
"""
Techland.tn scraper — Custom Next.js (RSC) storefront with Cloudflare.
Categories: /categories/{slug}, pagination: ?page=N, products: /produit/{slug}.
httpx works with a standard Chrome UA — no Playwright needed.
"""

import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser
from scraper.base import FastScraper


class TechlandScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("techland", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    def _absolute_url(self, href: str) -> str:
        if not href:
            return href
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return f"https://techland.tn{href}"
        return href

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen_urls = set()

        for a in tree.css("a[href^='/categories/']"):
            href = a.attributes.get("href", "")
            # Skip query-string variants
            if "?" in href:
                continue
            url = self._absolute_url(href)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            name = a.text(strip=True)
            if not name:
                # Try image alt
                img = a.css_first("img")
                if img:
                    name = img.attributes.get("alt", "").strip()
            if not name:
                # Derive from slug
                slug = href.rsplit("/", 1)[-1]
                name = slug.replace("-", " ").title()
            categories.append({
                "name": name, "url": url, "level": "top", "low_level_categories": [],
            })

        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": len(categories)}
        self.logger.info(f"Extracted {stats['top_level']} categories")
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        for a in tree.css("a[href^='/produit/']"):
            href = a.attributes.get("href", "")
            url = self._absolute_url(href)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            # Name from img[alt]
            img = a.css_first("img[alt]")
            name = (img.attributes.get("alt", "") if img else "").strip()
            if not name:
                name = a.text(strip=True)[:200]

            # Image
            image = None
            if img:
                image = img.attributes.get("src") or img.attributes.get("data-src")
                if image and image.startswith("data:"):
                    image = None

            # Price — find the smallest card container holding this anchor
            # that has a price span (usually 1-3 levels up, but never more).
            price = None
            old_price = None
            node = a
            card = None
            for _ in range(4):
                if node.parent is None:
                    break
                node = node.parent
                price_spans = node.css("span.font-bold[class*='text-base'], span[class*='font-bold']")
                dt_spans = [sp for sp in price_spans if "DT" in sp.text(strip=True) or "TND" in sp.text(strip=True)]
                # Card container = first ancestor where there's exactly 1-2 price spans (just this product's)
                if dt_spans and len(dt_spans) <= 4:
                    card = node
                    break
            if card:
                values = []
                for sp in card.css("span[class*='font-bold']"):
                    t = sp.text(strip=True)
                    if t and ("DT" in t or "TND" in t):
                        v = self._parse_price(t)
                        if v and v > 0:
                            values.append((v, "line-through" in sp.attributes.get("class", "")))
                currents = [v for v, struck in values if not struck]
                olds = [v for v, struck in values if struck]
                if currents:
                    price = currents[0]
                if olds:
                    old_price = olds[0]

            products.append({
                "id": None, "url": url, "name": name,
                "price": price, "old_price": old_price, "image": image,
            })
        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        # Pagination nav: "Previous 1 2 3 4 5 Next"
        nav = tree.css_first("nav.mx-auto.flex.w-full.justify-center")
        if not nav:
            # Fallback: any nav containing digit-only buttons
            for n in tree.css("nav"):
                if any(c.isdigit() for c in n.text(strip=True)):
                    nav = n
                    break
        if nav:
            for el in nav.css("a, button, span"):
                t = el.text(strip=True)
                if t.isdigit():
                    num = int(t)
                    if num > max_page:
                        max_page = num
        # Also check ?page=N links
        for a in tree.css("a[href*='page=']"):
            href = a.attributes.get("href", "")
            m = re.search(r"page=(\d+)", href)
            if m:
                num = int(m.group(1))
                if num > max_page:
                    max_page = num
        has_next = max_page > 1
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        # Handle U+00A0 (nbsp) and other whitespace
        s = str(text).replace("\xa0", " ").replace(" ", " ")
        cleaned = re.sub(r"[^\d.,]", "", s).strip()
        if not cleaned:
            return None
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            parts = cleaned.split(",")
            if len(parts[-1]) == 3:
                cleaned = cleaned.replace(",", "")
            else:
                cleaned = cleaned.replace(",", ".")
        elif "." in cleaned:
            parts = cleaned.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
                cleaned = cleaned.replace(".", "")
        try:
            return float(cleaned)
        except ValueError:
            return None

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Title — first h1 with class text-2xl or text-4xl
        title_el = tree.css_first("h1.text-2xl, h1.text-4xl, h1[class*='font-bold']")
        if not title_el:
            h1s = tree.css("h1")
            title_el = h1s[0] if h1s else None
        data["title"] = title_el.text(strip=True) if title_el else None

        # Price — try multiple selectors, prefer the one closest to the title.
        # techland's main product price uses "text-3xl font-bold" while "Produits
        # similaires" cards use "text-base font-bold".
        # Walk forward from h1 to find the first price span.
        data["price"] = None
        data["old_price"] = None
        if title_el:
            # Find the closest container holding the price (usually next sibling of h1's grandparent)
            node = title_el
            for _ in range(6):
                if node.parent is None:
                    break
                node = node.parent
                # Check this container for a text-3xl price
                price_el = node.css_first("span.text-3xl.font-bold, span[class*='text-3xl'][class*='font-bold']")
                if price_el:
                    data["price"] = self._parse_price(price_el.text())
                    # Old price (line-through) in same container
                    for old_el in node.css("span.line-through, span[class*='line-through']"):
                        v = self._parse_price(old_el.text())
                        if v:
                            data["old_price"] = v
                            break
                    break

        # Fallback if not found in container
        if data["price"] is None:
            price_el = tree.css_first("span.text-3xl.font-bold, span[class*='text-3xl'][class*='font-bold']")
            data["price"] = self._parse_price(price_el.text()) if price_el else None
            old_el = tree.css_first("span.line-through, span[class*='line-through']")
            data["old_price"] = self._parse_price(old_el.text()) if old_el else None

        if data.get("old_price") and data.get("price") and data["old_price"] != data["price"]:
            data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)

        # SKU — "Référence : XXXX" in text (after title)
        data["sku"] = None
        body = tree.css_first("body")
        if body:
            body_text = body.text()
            m = re.search(r"R[ée]f[ée]rence\s*[:.]\s*([A-Z0-9][\w/\-+]*)", body_text or "", re.I)
            if m:
                data["sku"] = m.group(1).strip()

        # Brand — try breadcrumb second segment (e.g. "smartphone")
        data["brand"] = None

        # Availability — look for "En stock" with green color or "Rupture"
        body_text_lower = (body.text() if body else "").lower()
        # Check the in-page indicator (green dot/text near price)
        avail_el = tree.css_first("p.text-sm.font-medium[style*='23, 182, 58'], p[style*='23, 182, 58']")
        if avail_el:
            data["availability"] = avail_el.text(strip=True)
            data["available"] = "en stock" in data["availability"].lower() or "stock" in data["availability"].lower()
        elif "rupture" in body_text_lower or "indisponible" in body_text_lower:
            data["availability"] = "Rupture de stock"
            data["available"] = False
        elif "en stock" in body_text_lower:
            data["availability"] = "En stock"
            data["available"] = True
        else:
            data["availability"] = None
            data["available"] = None

        # Description — first text block under "Description" tab
        data["description"] = None
        # Find div with class text-base text-[#707070] (the description container)
        desc_el = tree.css_first("div.text-base.text-\\[\\#707070\\]")
        if not desc_el:
            # Try by content: any div containing rich description with strong tags
            for div in tree.css("div"):
                cls = div.attributes.get("class", "")
                if "text-base" in cls and "text-" in cls:
                    txt = div.text(strip=True)
                    if len(txt) > 100:
                        desc_el = div
                        break
        if desc_el:
            txt = re.sub(r"\s+", " ", desc_el.text(strip=True))
            data["description"] = txt[:2000] or None

        # Images — techland or contabostorage CDN
        images = []
        for img in tree.css("img[src]"):
            src = img.attributes.get("src", "")
            if not src or src.startswith("data:"):
                continue
            if "techland" in src or "contabostorage.com" in src or "api-storage" in src:
                if src not in images:
                    images.append(src)
        data["images"] = images[:10]
        data["specifications"] = {}
        return data


def get_scraper(logger: logging.Logger) -> TechlandScraper:
    return TechlandScraper(logger)
