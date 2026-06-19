#!/usr/bin/env python3
"""
EMH (emh.tn) scraper.
Fast: PrestaShop with pos-megamenu / ul.menu-content theme.
"""
import logging
import re
from typing import List, Optional

from selectolax.parser import HTMLParser

from scraper.base import FastScraper


class EmhScraper(FastScraper):
    """Fast scraper for emh.tn (PrestaShop)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("emh", logger)

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

        # Top-level categories from the vertical mega-menu (Nos Rayons)
        menu_root = tree.css_first(".main-menu, #_desktop_vegamenu, #_desktop_megamenu")
        if menu_root:
            top_blocks = menu_root.css(".menu-content > li.menu-item.hasChild, .menu-content > li.menu-item")
        else:
            top_blocks = tree.css("ul.menu-content > li.menu-item.hasChild, ul.menu-content > li.menu-item")

        self.logger.info(f"Found {len(top_blocks)} top-level blocks")

        for top_li in top_blocks:
            top_link = top_li.css_first("a")
            if not top_link:
                continue
            top_name = self._clean_text(top_link.text(strip=True))
            top_url = self._make_absolute_url(top_link.attributes.get("href", ""))
            if not top_name or not top_url or top_url in seen:
                continue
            # Only include category links (PrestaShop URL format: /{id}-{slug})
            if not re.search(r"/\d+[-_]", top_url):
                continue
            seen.add(top_url)

            top_cat = {"name": top_name, "url": top_url, "level": "top", "low_level_categories": []}

            # Low-level categories: .ul-column > li.submenu-item > a (column-titles in mega-menu)
            for sub_link in top_li.css(".ul-column > li.submenu-item > a"):
                low_name = self._clean_text(sub_link.text(strip=True))
                low_url = self._make_absolute_url(sub_link.attributes.get("href", ""))
                if not low_name or low_url in seen:
                    continue
                seen.add(low_url)
                low_cat = {"name": low_name, "url": low_url, "level": "low", "subcategories": []}

                # Sub-sub: ul.category-sub-menu > li > a (under each column title)
                sub_li = sub_link.parent
                if sub_li:
                    for sub2 in sub_li.css("ul.category-sub-menu > li > a"):
                        sub2_name = self._clean_text(sub2.text(strip=True))
                        sub2_url = self._make_absolute_url(sub2.attributes.get("href", ""))
                        if sub2_name and sub2_url not in seen:
                            seen.add(sub2_url)
                            low_cat["subcategories"].append({"name": sub2_name, "url": sub2_url, "level": "subcategory"})

                top_cat["low_level_categories"].append(low_cat)

            # Fallback: collect any sub-menu anchors if no .ul-column found
            if not top_cat["low_level_categories"]:
                for sub_link in top_li.css(".pos-sub-menu a, .menu-dropdown a"):
                    low_name = self._clean_text(sub_link.text(strip=True))
                    low_url = self._make_absolute_url(sub_link.attributes.get("href", ""))
                    if low_name and low_url not in seen and re.search(r"/\d+[-_]", low_url):
                        seen.add(low_url)
                        top_cat["low_level_categories"].append({"name": low_name, "url": low_url, "level": "low", "subcategories": []})

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
            link = item.css_first("a.product-thumbnail, a.product_name, a.thumbnail, h5.product-title a, h2.product-title a, h3.product-title a")
            url = self._make_absolute_url(link.attributes.get("href", "")) if link else None
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            name_el = item.css_first(".product-title, h2.product-title, h3.product-title, h5.product-title, .product_name")
            name = self._clean_text(name_el.text(strip=True)) if name_el else None
            if not name and link:
                name = self._clean_text(link.text(strip=True))

            img_el = item.css_first("img.first-image, img.replace-2x, img.product_list_image, img")
            image = None
            if img_el:
                src = img_el.attributes.get("data-src") or img_el.attributes.get("src")
                if src and not src.startswith("data:"):
                    image = self._make_absolute_url(src)

            price_el = item.css_first("span.price, span.product-price, div.product-price-and-shipping span.price")
            price = self._parse_price(price_el.text() if price_el else None)

            old_price_el = item.css_first("span.regular-price, span.old-price, .price-discount span.regular-price")
            old_price = self._parse_price(old_price_el.text() if old_price_el else None)

            product = {"url": url, "name": name, "price": price, "image": image}
            if old_price:
                product["old_price"] = old_price
            products.append(product)

        return products

    def build_page_url(self, base_url: str, page_num: int) -> str:
        # PrestaShop 1.7 pagination uses ?page=N
        base = re.sub(r"[?&]page=\d+", "", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css(".pagination a, ul.pagination li a, div.pagination a"):
            try:
                n = int(a.text(strip=True))
                if n > max_page:
                    max_page = n
            except ValueError:
                pass
        has_next = tree.css_first("a[rel='next'], li.pagination_next a, .pagination a.next") is not None
        return {"max_page": max_page, "current_page": 1, "has_next_page": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "fetch_failed"}
        tree = HTMLParser(html)
        data = {"url": url}

        title_el = tree.css_first("h1[itemprop='name'], h1.product-name, h1.h1.product-title")
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        sku_el = tree.css_first("span[itemprop='sku'], div.product-reference span")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        brand_el = tree.css_first("div.manufacturer-info a, span[itemprop='brand'] span, .product-manufacturer a")
        data["brand"] = self._clean_text(brand_el.text(strip=True)) if brand_el else None

        price_el = tree.css_first("span[itemprop='price'], span.current-price span, span.our_price_display span.price, .current-price span")
        if price_el:
            content = price_el.attributes.get("content")
            data["price"] = self._parse_price(content or price_el.text())
        else:
            data["price"] = None

        old_price_el = tree.css_first("span.regular-price, span.old-price")
        data["old_price"] = self._parse_price(old_price_el.text() if old_price_el else None)

        avail_el = tree.css_first("span#product-availability, span.availability span, span[itemprop='availability']")
        data["availability"] = self._clean_text(avail_el.text(strip=True)) if avail_el else None
        if avail_el:
            avail_text = (avail_el.text(strip=True) or "").lower()
            data["available"] = "disponible" in avail_text or "en stock" in avail_text or "stock" in avail_text

        desc_el = tree.css_first("div#short_description_content, div[itemprop='description'], div.product-description-short")
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        images = []
        for img in tree.css("img.js-qv-product-cover, ul#thumbs_list_frame li a img, .product-cover img, div.product-images img"):
            src = img.attributes.get("src") or img.attributes.get("data-image-large-src")
            if src and src not in images:
                images.append(self._make_absolute_url(src))
        data["images"] = images

        return data


def get_scraper(logger: logging.Logger) -> EmhScraper:
    return EmhScraper(logger)
