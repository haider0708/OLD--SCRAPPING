#!/usr/bin/env python3
"""
Electrochaabani.com scraper — custom PHP platform, no CF, httpx.
Category URLs: /categorie-produit-route/{slug}
Product URLs: /produit/{slug}
Pagination: ?page=N
"""
import asyncio
import logging
import re
import time
from typing import List, Optional
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from selectolax.parser import HTMLParser

from scraper.base import FastScraper

_MIN_INTERVAL = 0.5


class ElectrochaabaniScraper(FastScraper):
    """httpx scraper for electrochaabani.com (custom platform)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("electrochaabani", logger)
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
        parsed = urlparse(base_url)
        params = parse_qs(parsed.query)
        params["page"] = [str(page_num)]
        new_query = urlencode({k: v[0] for k, v in params.items()})
        return urlunparse(parsed._replace(query=new_query))

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

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        # electrochaabani has a nav with /categorie-produit-route/ links
        for a in tree.css("a[href*='categorie-produit-route'], a[href*='categorie-produit']"):
            href = a.attributes.get("href", "")
            abs_url = self._abs(href).rstrip("/") + "/"
            name = self._clean(a.text(strip=True))
            if not name or not abs_url or abs_url in seen:
                continue
            seen.add(abs_url)

            path = abs_url.split("categorie-produit-route/")[-1].split("categorie-produit/")[-1]
            segments = [s for s in path.strip("/").split("/") if s]
            depth = len(segments)

            if depth <= 1:
                categories.append({"name": name, "url": abs_url, "level": "top", "low_level_categories": []})
            else:
                # attach as subcategory of the last top
                if categories:
                    last = categories[-1]
                    if not last["low_level_categories"]:
                        last["low_level_categories"].append({"name": last["name"], "url": last["url"], "level": "low", "subcategories": []})
                    last["low_level_categories"][-1]["subcategories"].append({"name": name, "url": abs_url, "level": "subcategory"})

        self.logger.info(f"Found {len(categories)} top-level categories")
        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": len(categories)}
        for top in categories:
            for low in top.get("low_level_categories", []):
                stats["low_level"] += 1
                for sub in low.get("subcategories", []):
                    stats["subcategory"] += 1
                    stats["total_urls"] += 1
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        items = tree.css("div.item-product, div.product-card, div.product-item")

        for item in items:
            # Find first real product link — skip cart/add URLs
            link_el = None
            for a in item.css("a[href*='/produit/']"):
                href = a.attributes.get("href", "")
                if "/cart/add/" not in href:
                    link_el = a
                    break
            if not link_el:
                continue

            product_url = self._abs(link_el.attributes.get("href", ""))
            if not product_url or product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            name_el = item.css_first("h4, h3, h2, .product-name, .product-title")
            product_name = self._clean(name_el.text(strip=True)) if name_el else ""

            product_data = {"id": None, "url": product_url, "name": product_name}

            img_el = item.css_first("img")
            if img_el:
                src = img_el.attributes.get("src") or img_el.attributes.get("data-src")
                if src and not src.startswith("data:"):
                    product_data["image"] = self._abs(src)

            price_el = item.css_first(".price, .product-price, [class*=price]")
            if price_el:
                price_text = price_el.text(strip=True)
                m = re.search(r"([\d\s]+(?:[.,]\d+)?)\s*(?:TND|DT|dt|tnd)", price_text, re.IGNORECASE)
                if m:
                    product_data["price"] = self._parse_price(m.group(1))
                else:
                    product_data["price"] = self._parse_price(price_text)

            avail_text = price_el.text(strip=True) if price_el else ""
            if "disponible" in avail_text.lower():
                product_data["availability"] = "Disponible"
                product_data["available"] = True
            elif "rupture" in avail_text.lower() or "indisponible" in avail_text.lower():
                product_data["availability"] = "Rupture de stock"
                product_data["available"] = False

            products.append(product_data)

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        current_page = 1

        next_link = tree.css_first(".pagination a[href*='page=']:last-child, a.next[href*='page=']")
        has_next = next_link is not None

        for a in tree.css(".pagination a[href*='page=']"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
            href = a.attributes.get("href", "")
            m = re.search(r"page=(\d+)", href)
            if m:
                try:
                    num = int(m.group(1))
                    if num > max_page:
                        max_page = num
                except ValueError:
                    pass

        return {"current_page": current_page, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        tree = HTMLParser(html)
        data = {"url": url}

        title_el = tree.css_first("h2.title-detail, h1, .product-title, .product-name, [itemprop='name']")
        if title_el:
            data["title"] = self._clean(title_el.text(strip=True))
        else:
            # Fallback: og:title or <title>
            og = tree.css_first('meta[property="og:title"]')
            if og:
                data["title"] = self._clean((og.attributes.get("content") or "").strip())
            else:
                t = tree.css_first("title")
                if t:
                    raw = t.text(strip=True)
                    # Strip suffix like " - Electro Chaabani"
                    data["title"] = self._clean(re.split(r"\s*[-|]\s*", raw)[0])
                else:
                    data["title"] = None

        # Custom platform: <div><strong>Référence:</strong> CL0358</div>
        data["sku"] = None
        for el in tree.css("strong, b, th, td, span, p, div"):
            txt = el.text(strip=True)
            if re.match(r"^(Référence|Ref|SKU|UGS|Code|Barcode|EAN)\s*[:\-]?\s*$", txt, re.IGNORECASE):
                # Label element — value is in sibling or parent's next text
                parent = el.parent
                if parent:
                    full = parent.text(strip=True)
                    m = re.search(r"[:\-]\s*(\S+)", full)
                    if m:
                        data["sku"] = m.group(1)
                        break
            elif re.match(r"^(Référence|Ref|SKU|UGS|Code|EAN)\s*[:\-]\s*\S", txt, re.IGNORECASE):
                m = re.search(r"[:\-]\s*(\S+)", txt)
                if m:
                    data["sku"] = m.group(1)
                    break

        price_el = tree.css_first(".price, [class*=price], [itemprop='price']")
        if price_el:
            price_text = price_el.text(strip=True)
            m = re.search(r"([\d\s]+(?:[.,]\d+)?)\s*(?:TND|DT|dt|tnd)", price_text, re.IGNORECASE)
            data["price"] = self._parse_price(m.group(1)) if m else self._parse_price(price_text)
        else:
            data["price"] = None

        avail_el = tree.css_first(".stock, .availability, [class*=stock], [class*=disponib]")
        if avail_el:
            avail_text = self._clean(avail_el.text(strip=True))
            data["availability"] = avail_text
            data["available"] = "disponible" in avail_text.lower() or "stock" in avail_text.lower()
        else:
            data["availability"] = None
            data["available"] = None

        desc_el = tree.css_first(".product-description, .description, #description, [itemprop='description']")
        if desc_el:
            data["description"] = self._clean(desc_el.text(strip=True))
        else:
            # Custom platform fallback: og:description / meta description
            og = tree.css_first('meta[property="og:description"]') or tree.css_first('meta[name="description"]')
            data["description"] = self._clean((og.attributes.get("content") or "").strip()) if og else None

        images = []
        for img in tree.css(".product-image img, .main-image img, img[itemprop='image'], .product-gallery img"):
            src = img.attributes.get("src") or img.attributes.get("data-src")
            if src and not src.startswith("data:") and src not in images:
                images.append(self._abs(src))
        data["images"] = images[:10] if images else None
        data["specifications"] = {}

        return data


def get_scraper(logger: logging.Logger) -> ElectrochaabaniScraper:
    return ElectrochaabaniScraper(logger)
