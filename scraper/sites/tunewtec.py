#!/usr/bin/env python3
"""
Tunewtec.com scraper — WordPress + WooCommerce + Woodmart theme.
Custom URL rewrite: categories under /c/{slug}/ instead of /product-category/{slug}/.
"""

import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser
from scraper.base import FastScraper


class TunewtecScraper(FastScraper):

    def __init__(self, logger: logging.Logger):
        super().__init__("tunewtec", logger)

    # ------------------------------------------------------------------
    # Pagination — /c/{slug}/page/{N}/
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"/page/\d+/?$", "", base_url.rstrip("/"))
        return f"{base}/page/{page_num}/"

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def _absolute_url(self, href: str) -> str:
        if not href:
            return href
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return f"https://tunewtec.com{href}"
        return href

    def _is_category_url(self, href: str) -> bool:
        return "/c/" in href and "/page/" not in href

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        # Scan all /c/ links; group by path depth.
        # /c/parent/                  → top (depth 1)
        # /c/parent/child/            → low (depth 2)
        # /c/parent/child/grandchild/ → sub (depth 3)
        url_to_name: dict = {}
        for a in tree.css("a[href*='/c/']"):
            href = self._absolute_url(a.attributes.get("href", "")).rstrip("/") + "/"
            if not self._is_category_url(href):
                continue
            name = a.text(strip=True)
            if not name:
                continue
            # Keep the first name we see for each URL
            if href not in url_to_name:
                url_to_name[href] = name

        # Build hierarchy
        tops: dict = {}  # parent-slug → top category dict
        lows: dict = {}  # f'{p}/{c}' → low category dict

        for href, name in url_to_name.items():
            # Strip protocol+host and split
            path = re.sub(r"^https?://[^/]+", "", href).strip("/")
            parts = path.split("/")
            if len(parts) < 2 or parts[0] != "c":
                continue
            segs = parts[1:]
            if len(segs) == 1:
                slug = segs[0]
                if slug not in tops:
                    tops[slug] = {"name": name, "url": href.rstrip("/"), "level": "top", "low_level_categories": []}
                else:
                    tops[slug]["name"] = name
            elif len(segs) == 2:
                key = f"{segs[0]}/{segs[1]}"
                if key not in lows:
                    lows[key] = {"name": name, "url": href.rstrip("/"), "level": "low", "subcategories": []}
                # Ensure parent exists
                if segs[0] not in tops:
                    tops[segs[0]] = {
                        "name": segs[0].replace("-", " ").title(),
                        "url": f"https://tunewtec.com/c/{segs[0]}",
                        "level": "top",
                        "low_level_categories": [],
                    }
            elif len(segs) >= 3:
                key = f"{segs[0]}/{segs[1]}"
                if key not in lows:
                    lows[key] = {
                        "name": segs[1].replace("-", " ").title(),
                        "url": f"https://tunewtec.com/c/{segs[0]}/{segs[1]}",
                        "level": "low",
                        "subcategories": [],
                    }
                if segs[0] not in tops:
                    tops[segs[0]] = {
                        "name": segs[0].replace("-", " ").title(),
                        "url": f"https://tunewtec.com/c/{segs[0]}",
                        "level": "top",
                        "low_level_categories": [],
                    }
                lows[key]["subcategories"].append({
                    "name": name,
                    "url": href.rstrip("/"),
                    "level": "subcategory",
                })

        # Attach lows to tops
        for key, low_cat in lows.items():
            parent_slug = key.split("/")[0]
            if parent_slug in tops:
                # Avoid duplicate URLs in the same parent
                existing = {c["url"] for c in tops[parent_slug]["low_level_categories"]}
                if low_cat["url"] not in existing:
                    tops[parent_slug]["low_level_categories"].append(low_cat)

        categories = list(tops.values())
        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top["low_level_categories"]:
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
                for sub in low.get("subcategories", []):
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1
        self.logger.info(f"Extracted {stats['top_level']} top, {stats['low_level']} low, {stats['subcategory']} sub")
        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Products on category page
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        for card in tree.css("section.product, div.product, li.product.type-product"):
            link = card.css_first("h3.wd-entities-title a, h3 a, .product-title a, a[href*='/s/'], a.product-image-link")
            if not link:
                link = card.css_first("a[href]")
            if not link:
                continue
            url = self._absolute_url(link.attributes.get("href", ""))
            if not url or url in seen_urls:
                continue
            # Skip category-like links (must have /s/ for product detail)
            if "/c/" in url and "/s/" not in url:
                continue
            seen_urls.add(url)

            name_el = card.css_first("h3.wd-entities-title a, h3 a, h2 a, .product-title")
            name = name_el.text(strip=True) if name_el else link.text(strip=True)

            # Price — sale = ins, regular = single span
            ins_el = card.css_first("span.price ins span.woocommerce-Price-amount bdi, ins .woocommerce-Price-amount bdi")
            del_el = card.css_first("span.price del span.woocommerce-Price-amount bdi, del .woocommerce-Price-amount bdi")
            price_el = card.css_first("span.price span.woocommerce-Price-amount bdi, .price .woocommerce-Price-amount bdi")

            if ins_el and del_el:
                price = self._parse_price(ins_el.text())
                old_price = self._parse_price(del_el.text())
            else:
                price = self._parse_price(price_el.text() if price_el else None)
                old_price = None

            # SKU from add-to-cart button
            sku = None
            sku_btn = card.css_first("a.add_to_cart_button[data-product_sku], button[data-product_sku]")
            if sku_btn:
                sku = sku_btn.attributes.get("data-product_sku")

            # Image
            img = card.css_first("img.wp-post-image, figure img, img.attachment-woocommerce_thumbnail, img")
            image = None
            if img:
                image = img.attributes.get("src") or img.attributes.get("data-src")
                if image and image.startswith("data:"):
                    image = None

            pid = card.attributes.get("data-product_id") or sku
            products.append({
                "id": pid,
                "url": url,
                "name": name,
                "price": price,
                "old_price": old_price,
                "sku": sku,
                "image": image,
            })
        return products

    # ------------------------------------------------------------------
    # Pagination info
    # ------------------------------------------------------------------

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css("nav.woocommerce-pagination a.page-numbers, ul.page-numbers a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a.next.page-numbers") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    # ------------------------------------------------------------------
    # Price parsing — TN format "1,200 TND" → 1200
    # ------------------------------------------------------------------

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", str(text)).strip()
        if not cleaned:
            return None
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        try:
            return float(cleaned)
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

        # Body class postid-{N}
        body = tree.css_first("body")
        if body:
            m = re.search(r"\bpostid-(\d+)\b", body.attributes.get("class", ""))
            data["product_id"] = m.group(1) if m else None

        title_el = tree.css_first("h1.product_title, h1.entry-title")
        data["title"] = title_el.text(strip=True) if title_el else None

        sku_el = tree.css_first(".product_meta .sku, span.sku")
        data["sku"] = sku_el.text(strip=True) if sku_el else None

        ins_el = tree.css_first(".summary p.price ins .woocommerce-Price-amount bdi, p.price ins .woocommerce-Price-amount bdi")
        del_el = tree.css_first(".summary p.price del .woocommerce-Price-amount bdi, p.price del .woocommerce-Price-amount bdi")
        price_el = tree.css_first(".summary p.price .woocommerce-Price-amount bdi, p.price .woocommerce-Price-amount bdi")

        if ins_el and del_el:
            data["price"] = self._parse_price(ins_el.text())
            data["old_price"] = self._parse_price(del_el.text())
        else:
            data["price"] = self._parse_price(price_el.text() if price_el else None)
            data["old_price"] = None

        if data.get("old_price") and data.get("price") and data["old_price"] != data["price"]:
            data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)

        brand_el = tree.css_first(".wd-brand-name a, .product_meta .posted_in a")
        data["brand"] = brand_el.text(strip=True) if brand_el else None

        # Stock
        in_stock = tree.css_first("p.stock.in-stock")
        out_stock = tree.css_first("p.stock.out-of-stock")
        if in_stock:
            data["availability"] = "En stock"
            data["available"] = True
        elif out_stock:
            data["availability"] = "Rupture de stock"
            data["available"] = False
        else:
            data["availability"] = None
            data["available"] = None

        desc_el = tree.css_first("#tab-description, div.woocommerce-product-details__short-description")
        if desc_el:
            data["description"] = re.sub(r"\s+", " ", desc_el.text(strip=True))[:2000]
        else:
            data["description"] = None

        # Specs from product attributes table
        specs = {}
        for row in tree.css("table.shop_attributes tr, table.woocommerce-product-attributes tr"):
            k = row.css_first("th")
            v = row.css_first("td")
            if k and v:
                key = k.text(strip=True)
                val = v.text(strip=True)
                if key and val:
                    specs[key] = val
        data["specifications"] = specs

        # Images from gallery
        images = []
        for a in tree.css(".woocommerce-product-gallery__image a[href]"):
            src = a.attributes.get("href", "")
            if src and not src.startswith("data:") and src not in images:
                images.append(src)
        if not images:
            for img in tree.css(".woocommerce-product-gallery img"):
                src = img.attributes.get("data-large_image") or img.attributes.get("src")
                if src and not src.startswith("data:") and src not in images:
                    images.append(src)
        data["images"] = images[:10]
        return data


def get_scraper(logger: logging.Logger) -> TunewtecScraper:
    return TunewtecScraper(logger)
