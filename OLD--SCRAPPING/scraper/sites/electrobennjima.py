#!/usr/bin/env python3
"""
ElectroBennjima (electrobennjima.tn) scraper.
Fast: WooCommerce on WordPress, standard /categorie-produit/ URLs.
"""
import logging
import re
from typing import List, Optional

from selectolax.parser import HTMLParser

from scraper.base import FastScraper


class ElectrobennjimaScraper(FastScraper):
    """Fast scraper for electrobennjima.tn (WooCommerce)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("electrobennjima", logger)

    def _parse_price(self, text: str) -> Optional[float]:
        if not text:
            return None
        # Strip non-ASCII first (drops Arabic "د.ت" and similar currency tokens
        # whose embedded dot otherwise corrupts the decimal/thousand split).
        ascii_text = re.sub(r"[^\x00-\x7f]", "", text)
        cleaned = re.sub(r"[^\d.,]", "", ascii_text).strip()
        cleaned = re.sub(r"\s+", "", cleaned).strip(".")
        # Tunisian DT uses comma as decimal separator with up to 3 millimes
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

        # Find all menu items with categorie-produit URLs that have children
        top_blocks = tree.css("li.menu-item.menu-item-has-children")
        # Filter to product category items
        product_top = []
        for li in top_blocks:
            link = li.css_first("a")
            if link and "/categorie-produit/" in (link.attributes.get("href") or ""):
                product_top.append(li)

        self.logger.info(f"Found {len(product_top)} top-level product menu blocks")

        for top_li in product_top:
            top_link = top_li.css_first("a")
            if not top_link:
                continue
            top_name = self._clean_text(top_link.text(strip=True))
            top_url = self._make_absolute_url(top_link.attributes.get("href", ""))
            if not top_name or not top_url or top_url in seen:
                continue
            if "/categorie-produit/" not in top_url:
                continue
            seen.add(top_url)

            top_cat = {"name": top_name, "url": top_url, "level": "top", "low_level_categories": []}

            # Sub-categories: standard WordPress nested ul.sub-menu
            for sub_li in top_li.css("ul.sub-menu > li"):
                low_link = sub_li.css_first("a")
                if not low_link:
                    continue
                low_name = self._clean_text(low_link.text(strip=True))
                low_url = self._make_absolute_url(low_link.attributes.get("href", ""))
                if not low_name or low_url in seen:
                    continue
                seen.add(low_url)
                low_cat = {"name": low_name, "url": low_url, "level": "low", "subcategories": []}

                for sub2_li in sub_li.css("ul.sub-menu > li"):
                    sub2_link = sub2_li.css_first("a")
                    if not sub2_link:
                        continue
                    sub2_name = self._clean_text(sub2_link.text(strip=True))
                    sub2_url = self._make_absolute_url(sub2_link.attributes.get("href", ""))
                    if sub2_name and sub2_url not in seen:
                        seen.add(sub2_url)
                        low_cat["subcategories"].append({"name": sub2_name, "url": sub2_url, "level": "subcategory"})

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

        for item in tree.css("li.product.type-product, ul.products li.product, div.product.type-product"):
            link = item.css_first("a.woocommerce-LoopProduct-link, a.woocommerce-loop-product__link")
            if not link:
                link = item.css_first("h3 a, h2.woocommerce-loop-product__title a")
            url = self._make_absolute_url(link.attributes.get("href", "")) if link else None
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            name_el = item.css_first("h2.woocommerce-loop-product__title, h3.product-title, h3.woocommerce-loop-product__title")
            name = self._clean_text(name_el.text(strip=True)) if name_el else None

            img_el = item.css_first("img.attachment-woocommerce_thumbnail, img.wp-post-image, img")
            image = None
            if img_el:
                src = img_el.attributes.get("src") or img_el.attributes.get("data-src")
                if src and not src.startswith("data:"):
                    image = self._make_absolute_url(src)

            ins_el = item.css_first("span.price ins span.woocommerce-Price-amount")
            plain_el = item.css_first("span.price span.woocommerce-Price-amount")
            price_el = ins_el or plain_el
            price = self._parse_price(price_el.text() if price_el else None)

            del_el = item.css_first("span.price del span.woocommerce-Price-amount")
            old_price = self._parse_price(del_el.text() if del_el else None)

            product = {"url": url, "name": name, "price": price, "image": image}
            if old_price:
                product["old_price"] = old_price
            products.append(product)

        return products

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"/page/\d+/?$", "", base_url.rstrip("/"))
        return f"{base}/page/{page_num}/"

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css("ul.page-numbers li a.page-numbers:not(.next):not(.prev)"):
            try:
                n = int(a.text(strip=True))
                if n > max_page:
                    max_page = n
            except ValueError:
                pass
        has_next = tree.css_first("a.next.page-numbers") is not None
        return {"max_page": max_page, "current_page": 1, "has_next_page": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "fetch_failed"}
        tree = HTMLParser(html)
        data = {"url": url}

        title_el = tree.css_first("h1.product_title.entry-title, h1.product_title")
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        sku_el = tree.css_first("span.sku")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        brand_el = tree.css_first("div.product_meta span.posted_in a")
        data["brand"] = self._clean_text(brand_el.text(strip=True)) if brand_el else None

        ins_el = tree.css_first("p.price ins span.woocommerce-Price-amount bdi")
        plain_el = tree.css_first("p.price span.woocommerce-Price-amount bdi")
        data["price"] = self._parse_price((ins_el or plain_el).text() if (ins_el or plain_el) else None)

        del_el = tree.css_first("p.price del span.woocommerce-Price-amount bdi")
        data["old_price"] = self._parse_price(del_el.text() if del_el else None)

        stock_el = tree.css_first("p.stock")
        data["availability"] = self._clean_text(stock_el.text(strip=True)) if stock_el else None
        data["available"] = stock_el is not None and "in-stock" in (stock_el.attributes.get("class") or "")

        desc_el = tree.css_first("div.woocommerce-product-details__short-description")
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        images = []
        main_img = tree.css_first("div.woocommerce-product-gallery__image img")
        if main_img:
            src = main_img.attributes.get("data-large_image") or main_img.attributes.get("src")
            if src:
                images.append(self._make_absolute_url(src))
        for thumb in tree.css("ol.flex-control-thumbs li img"):
            src = thumb.attributes.get("src")
            if src and src not in images:
                images.append(self._make_absolute_url(src))
        data["images"] = images

        return data


def get_scraper(logger: logging.Logger) -> ElectrobennjimaScraper:
    return ElectrobennjimaScraper(logger)
