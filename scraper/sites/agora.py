#!/usr/bin/env python3
"""
Agora (agora.tn/fr/) scraper.
Fast: PrestaShop with vertical mega-menu (menu-vertical, ul.menu-content, /fr/ prefix).
"""
import logging
import re
from typing import List, Optional

from selectolax.parser import HTMLParser

from scraper.base import FastScraper


class AgoraScraper(FastScraper):
    """Fast scraper for agora.tn (PrestaShop)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("agora", logger)

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

        # Vertical mega-menu: .menu-vertical > ul.menu-content > li.level-1.parent
        menu_root = tree.css_first(".menu-vertical")
        if menu_root:
            top_blocks = menu_root.css("ul.menu-content > li.level-1.parent, ul.menu-content > li.level-1")
        else:
            top_blocks = tree.css("ul.menu-content > li.level-1")

        # Skip the "Tous nos rayons" placeholder item
        filtered = [li for li in top_blocks if "menumobileitem" not in (li.attributes.get("class") or "")]

        self.logger.info(f"Found {len(filtered)} top-level blocks")

        for top_li in filtered:
            top_link = top_li.css_first("a")
            if not top_link:
                continue
            top_name = self._clean_text(top_link.text(strip=True))
            top_url = self._make_absolute_url(top_link.attributes.get("href", ""))
            if not top_name or not top_url or top_url in seen:
                continue
            # Only PrestaShop category links: /fr/{id}-{slug}
            if not re.search(r"/\d+[-_]", top_url):
                continue
            seen.add(top_url)

            top_cat = {"name": top_name, "url": top_url, "level": "top", "low_level_categories": []}

            # Inside dropdown: each .ul-column has its own column with item-header (low) + item-line (sub)
            for col in top_li.css(".it-sub-menu .ul-column, .menu-dropdown .ul-column"):
                # Column header = low-level category
                header_link = col.css_first("li.item-header > a")
                if not header_link:
                    # Fallback: first link is the header
                    header_link = col.css_first("li > a")
                if header_link:
                    low_name = self._clean_text(header_link.text(strip=True))
                    low_url = self._make_absolute_url(header_link.attributes.get("href", ""))
                    if low_name and low_url not in seen and re.search(r"/\d+[-_]", low_url):
                        seen.add(low_url)
                        low_cat = {"name": low_name, "url": low_url, "level": "low", "subcategories": []}

                        # Sub-items in same column: li.item-line > a
                        for sub_link in col.css("li.item-line > a"):
                            sub_name = self._clean_text(sub_link.text(strip=True))
                            sub_url = self._make_absolute_url(sub_link.attributes.get("href", ""))
                            if sub_name and sub_url not in seen and re.search(r"/\d+[-_]", sub_url):
                                seen.add(sub_url)
                                low_cat["subcategories"].append({"name": sub_name, "url": sub_url, "level": "subcategory"})

                        top_cat["low_level_categories"].append(low_cat)

            # Fallback: if no .ul-column found, treat any sub-menu anchor as low-level
            if not top_cat["low_level_categories"]:
                for sub_link in top_li.css(".it-sub-menu a, .menu-dropdown a"):
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
            link = item.css_first("a.product-thumbnail, a.product_name, a.thumbnail, h2.product-title a, h3.product-title a, h5.product-title a")
            url = self._make_absolute_url(link.attributes.get("href", "")) if link else None
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            name_el = item.css_first(".product-title a, h2.product-title, h3.product-title")
            name = self._clean_text(name_el.text(strip=True)) if name_el else None
            if not name and link:
                name = self._clean_text(link.text(strip=True))

            img_el = item.css_first("img.first-image, img.replace-2x, img.product_list_image, img")
            image = None
            if img_el:
                src = img_el.attributes.get("data-src") or img_el.attributes.get("src")
                if src and not src.startswith("data:"):
                    image = self._make_absolute_url(src)

            price_el = item.css_first("span.price, .product-price-and-shipping span.price, span.product-price")
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


def get_scraper(logger: logging.Logger) -> AgoraScraper:
    return AgoraScraper(logger)
