#!/usr/bin/env python3
"""
MaalejAudio (maalejaudio.tn) scraper.
Fast: PrestaShop with sidevertical menu (ul#top-menu, data-depth).
"""
import logging
import re
from typing import List, Optional

from selectolax.parser import HTMLParser

from scraper.base import FastScraper


class MaalejaudioScraper(FastScraper):
    """Fast scraper for maalejaudio.tn (PrestaShop)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("maalejaudio", logger)

    def _parse_price(self, text: str) -> Optional[float]:
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", text).strip()
        cleaned = re.sub(r"\s+", "", cleaned)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _clean_text(self, text: str) -> Optional[str]:
        if not text:
            return None
        return " ".join(text.split()).strip() or None

    def _make_absolute_url(self, url: str) -> str:
        if not url:
            return url
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return f"{self.base_url}{url}"
        return f"{self.base_url}/{url}"

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        # Top-level: ul#top-menu > li.category > a.dropdown-item (data-depth=0)
        # Note: the same #top-menu is used recursively for nested levels via .popover.sub-menu containers.
        top_menu = tree.css_first("#top-menu, ul.top-menu#top-menu")
        if not top_menu:
            top_menu = tree.css_first("ul.top-menu")
        if not top_menu:
            self.logger.warning("Top-menu container not found")
            return {"categories": [], "stats": {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}}

        # Direct child <li.category> are top-level
        top_blocks = top_menu.css("li.category")
        # Filter to only TOP-level (those whose parent is the top_menu, depth=0)
        depth0 = []
        for li in top_blocks:
            a = li.css_first("a.dropdown-item")
            if a and a.attributes.get("data-depth", "") == "0":
                depth0.append(li)
        if not depth0:
            depth0 = top_blocks

        self.logger.info(f"Found {len(depth0)} top-level blocks")

        for top_li in depth0:
            top_link = top_li.css_first("a.dropdown-item")
            if not top_link:
                continue
            top_name = self._clean_text(top_link.text(strip=True))
            top_url = self._make_absolute_url(top_link.attributes.get("href", ""))
            if not top_name or not top_url or top_url in seen:
                continue
            if not re.search(r"/\d+[-_]", top_url):
                continue
            seen.add(top_url)

            top_cat = {"name": top_name, "url": top_url, "level": "top", "low_level_categories": []}

            # Low-level: inside the same <li>, find sub-list with depth=1
            for low_link in top_li.css("a.dropdown-item"):
                if low_link.attributes.get("data-depth", "") != "1":
                    continue
                low_name = self._clean_text(low_link.text(strip=True))
                low_url = self._make_absolute_url(low_link.attributes.get("href", ""))
                if not low_name or low_url in seen:
                    continue
                seen.add(low_url)
                low_cat = {"name": low_name, "url": low_url, "level": "low", "subcategories": []}

                # Sub-sub: depth=2 anchors within the same <li>
                low_li = low_link.parent
                if low_li:
                    for sub_link in low_li.css("a.dropdown-item"):
                        if sub_link.attributes.get("data-depth", "") != "2":
                            continue
                        sub_name = self._clean_text(sub_link.text(strip=True))
                        sub_url = self._make_absolute_url(sub_link.attributes.get("href", ""))
                        if sub_name and sub_url not in seen:
                            seen.add(sub_url)
                            low_cat["subcategories"].append({"name": sub_name, "url": sub_url, "level": "subcategory"})

                top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["low_level"] += len(top["low_level_categories"])
            for low in top["low_level_categories"]:
                stats["subcategory"] += len(low["subcategories"])
            stats["total_urls"] += 1 + len(top["low_level_categories"])
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        for item in tree.css("article.product-miniature, .product-miniature, div.product-container, ul.product_list li.ajax_block_product"):
            link = item.css_first("a.product-thumbnail, a.product_name, a.thumbnail, h2.product-title a, h3.product-title a, h5.product-title a")
            url = self._make_absolute_url(link.attributes.get("href", "")) if link else None
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            name_el = item.css_first(".product-title a, h2.product-title, h3.product-title, .product_name")
            name = self._clean_text(name_el.text(strip=True)) if name_el else None
            if not name and link:
                name = self._clean_text(link.text(strip=True))

            img_el = item.css_first("img.first-image, img.replace-2x, img.product_list_image, img")
            image = None
            if img_el:
                src = img_el.attributes.get("data-src") or img_el.attributes.get("src")
                if src and not src.startswith("data:"):
                    image = self._make_absolute_url(src)

            price_el = item.css_first("span.price, .product-price-and-shipping span.price")
            price = self._parse_price(price_el.text() if price_el else None)

            old_price_el = item.css_first("span.regular-price, span.old-price")
            old_price = self._parse_price(old_price_el.text() if old_price_el else None)

            product = {"url": url, "name": name, "price": price, "image": image}
            if old_price:
                product["old_price"] = old_price
            products.append(product)

        return products

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css(".pagination a, ul.pagination li a"):
            try:
                n = int(a.text(strip=True))
                if n > max_page:
                    max_page = n
            except ValueError:
                pass
        has_next = tree.css_first("a[rel='next'], .pagination a.next") is not None
        return {"max_page": max_page, "current_page": 1, "has_next_page": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "fetch_failed"}
        tree = HTMLParser(html)
        data = {"url": url}

        title_el = tree.css_first("h1[itemprop='name'], h1.product-name, h1.h1.product-title, h1.page-title")
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        sku_el = tree.css_first("span[itemprop='sku'], div.product-reference span")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        brand_el = tree.css_first("div.manufacturer-info a, span[itemprop='brand'] span, .product-manufacturer a")
        data["brand"] = self._clean_text(brand_el.text(strip=True)) if brand_el else None

        price_el = tree.css_first("span[itemprop='price'], span.current-price span, .current-price span")
        if price_el:
            content = price_el.attributes.get("content")
            data["price"] = self._parse_price(content or price_el.text())
        else:
            data["price"] = None

        old_price_el = tree.css_first("span.regular-price, span.old-price")
        data["old_price"] = self._parse_price(old_price_el.text() if old_price_el else None)

        avail_el = tree.css_first("span#product-availability, span.availability span")
        data["availability"] = self._clean_text(avail_el.text(strip=True)) if avail_el else None
        if avail_el:
            avail_text = (avail_el.text(strip=True) or "").lower()
            data["available"] = "disponible" in avail_text or "en stock" in avail_text

        desc_el = tree.css_first("div#short_description_content, div[itemprop='description'], div.product-description-short")
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        images = []
        for img in tree.css("img.js-qv-product-cover, .product-cover img, div.product-images img"):
            src = img.attributes.get("src") or img.attributes.get("data-image-large-src")
            if src and src not in images:
                images.append(self._make_absolute_url(src))
        data["images"] = images

        return data


def get_scraper(logger: logging.Logger) -> MaalejaudioScraper:
    return MaalejaudioScraper(logger)
