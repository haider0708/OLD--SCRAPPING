#!/usr/bin/env python3
"""
Promouv (promouv.com) scraper.
Fast: PrestaShop 1.6 (ecolife theme + posmegamenu), static HTML.
"""
import logging
import re
from typing import List, Optional

from selectolax.parser import HTMLParser

from scraper.base import FastScraper


class PromouvScraper(FastScraper):
    """Fast scraper for promouv.com (PrestaShop 1.6)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("promouv", logger)

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

        # Promouv uses a mega-menu with column layout inside #_desktop_megamenu
        menu_root = tree.css_first("#_desktop_megamenu, .pos-menu-horizontal")
        if menu_root:
            top_blocks = menu_root.css(".menu-content > li.menu-item.dropdown-mega.hasChild, .menu-content > li.menu-item")
        else:
            top_blocks = tree.css("li.menu-item.dropdown-mega.hasChild")

        self.logger.info(f"Found {len(top_blocks)} top-level blocks")

        for top_li in top_blocks:
            top_link = top_li.css_first("a")
            if not top_link:
                continue
            top_name = self._clean_text(top_link.text(strip=True))
            top_href = top_link.attributes.get("href", "")
            top_url = self._make_absolute_url(top_href)
            if not top_name or not top_url or top_url in seen:
                continue
            # Include if URL looks like a category (numeric ID slug or known pattern)
            if not re.search(r"/\d+[-_]", top_url) and "controller=category" not in top_url:
                continue
            seen.add(top_url)

            top_cat = {"name": top_name, "url": top_url, "level": "top", "low_level_categories": []}

            # Column headers are low-level categories
            for col_link in top_li.css("a.column_title"):
                low_name = self._clean_text(col_link.text(strip=True))
                low_url = self._make_absolute_url(col_link.attributes.get("href", ""))
                if not low_name or low_url in seen:
                    continue
                seen.add(low_url)
                low_cat = {"name": low_name, "url": low_url, "level": "low", "subcategories": []}

                # Sub-items in the column below this header
                col_container = col_link.parent
                if col_container:
                    next_ul = col_container.css_first("ul.ul-column")
                    if not next_ul:
                        # Try sibling approach: find the ul-column after this column_title
                        pass
                    if next_ul:
                        for sub_link in next_ul.css("li.submenu-item > a"):
                            sub_name = self._clean_text(sub_link.text(strip=True))
                            sub_url = self._make_absolute_url(sub_link.attributes.get("href", ""))
                            if sub_name and sub_url not in seen:
                                seen.add(sub_url)
                                low_cat["subcategories"].append({"name": sub_name, "url": sub_url, "level": "subcategory"})

                top_cat["low_level_categories"].append(low_cat)

            # Fallback: if no column_title found, grab all submenu-item links as low-level
            if not top_cat["low_level_categories"]:
                for sub_link in top_li.css("li.submenu-item > a"):
                    low_name = self._clean_text(sub_link.text(strip=True))
                    low_url = self._make_absolute_url(sub_link.attributes.get("href", ""))
                    if low_name and low_url not in seen:
                        seen.add(low_url)
                        top_cat["low_level_categories"].append({"name": low_name, "url": low_url, "level": "low", "subcategories": []})

            categories.append(top_cat)

        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["low_level"] += len(top["low_level_categories"])
            stats["total_urls"] += 1 + len(top["low_level_categories"])
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        for item in tree.css("article.product-miniature, .product-miniature, ul.product_list li.ajax_block_product"):
            link = item.css_first("a.product_name, a.thumbnail.product-thumbnail, h5.product-title a, a.product_img_link")
            url = self._make_absolute_url(link.attributes.get("href", "")) if link else None
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            name_el = item.css_first(".product_name, h2.product-title, h5.product-title, .h3.product-title")
            name = self._clean_text(name_el.text(strip=True)) if name_el else None
            if not name and link:
                name = self._clean_text(link.text(strip=True))

            img_el = item.css_first("img.first-image, img.replace-2x, img.product_list_image, img")
            image = None
            if img_el:
                src = img_el.attributes.get("data-src") or img_el.attributes.get("src")
                if src and not src.startswith("data:"):
                    image = self._make_absolute_url(src)

            price_el = item.css_first("span.price, span.price.product-price, div.content_price span.price")
            price = self._parse_price(price_el.text() if price_el else None)

            old_price_el = item.css_first("span.regular-price, span.old-price, span.price_old")
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
        # PrestaShop pagination links
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

        title_el = tree.css_first("h1[itemprop='name'], h1.product-name")
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        sku_el = tree.css_first("span[itemprop='sku'], span.editable")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        brand_el = tree.css_first("div.manufacturer-info a, span[itemprop='brand'] span")
        data["brand"] = self._clean_text(brand_el.text(strip=True)) if brand_el else None

        # Price: try content attr first (machine-readable), then text
        price_el = tree.css_first("span[itemprop='price'], span.our_price_display span.price")
        if price_el:
            content = price_el.attributes.get("content")
            data["price"] = self._parse_price(content or price_el.text())
        else:
            data["price"] = None

        old_price_el = tree.css_first("span.old-price")
        data["old_price"] = self._parse_price(old_price_el.text() if old_price_el else None)

        avail_el = tree.css_first("span.availability span, span[itemprop='availability']")
        data["availability"] = self._clean_text(avail_el.text(strip=True)) if avail_el else None
        if avail_el:
            avail_text = (avail_el.text(strip=True) or "").lower()
            data["available"] = "disponible" in avail_text or "en stock" in avail_text or "stock" in avail_text

        desc_el = tree.css_first("div#short_description_content, div[itemprop='description']")
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        images = []
        main_img = tree.css_first("img#bigpic, img[itemprop='image']")
        if main_img:
            src = main_img.attributes.get("src")
            if src:
                images.append(self._make_absolute_url(src))
        for thumb in tree.css("ul#thumbs_list_frame li a img"):
            src = thumb.attributes.get("src")
            if src and src not in images:
                images.append(self._make_absolute_url(src))
        data["images"] = images

        return data


def get_scraper(logger: logging.Logger) -> PromouvScraper:
    return PromouvScraper(logger)
