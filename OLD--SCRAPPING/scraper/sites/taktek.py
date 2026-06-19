#!/usr/bin/env python3
"""
Taktek.com.tn scraper — PrestaShop, no Cloudflare, httpx for all pages.
"""
import json
import logging
import re
from typing import List, Optional

from selectolax.parser import HTMLParser

from scraper.base import FastScraper


class TaktekScraper(FastScraper):
    """httpx-based scraper for taktek.com.tn (PrestaShop, no CF)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("taktek", logger)

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"[?&]page=\d+", "", base_url)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def _make_absolute(self, url: str) -> Optional[str]:
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
        cleaned = re.sub(r"[^\d.,]", "", text).strip()
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
        """Extract categories from PrestaShop nav menu."""
        tree = HTMLParser(html)
        categories = []
        seen = set()

        # Try standard PrestaShop nav containers
        containers = tree.css(
            "#top-menu li.category, "
            "ul.menu-content > li.level-1, "
            "nav ul > li.top-level-elem"
        )

        for top_li in containers:
            top_link = top_li.css_first("a")
            if not top_link:
                continue
            top_name = self._clean_text(top_link.text(strip=True))
            top_url = self._make_absolute(top_link.attributes.get("href", ""))
            if not top_name or top_name in seen:
                continue
            seen.add(top_name)

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            # Sub-categories
            sub_items = top_li.css("ul li a")
            if sub_items:
                low_cat = {
                    "name": top_name,
                    "url": top_url,
                    "level": "low",
                    "subcategories": [],
                }
                for sub_link in sub_items:
                    sub_name = self._clean_text(sub_link.text(strip=True))
                    sub_url = self._make_absolute(sub_link.attributes.get("href", ""))
                    if sub_name and sub_url:
                        low_cat["subcategories"].append(
                            {"name": sub_name, "url": sub_url, "level": "subcategory"}
                        )
                if low_cat["subcategories"]:
                    top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        # Fallback: collect all category links from anywhere in the page
        if not categories:
            for a in tree.css("a[href*='/informatique'], a[href*='/telephonie'], a[href*='/tv-son'], a[href*='/maison']"):
                href = self._make_absolute(a.attributes.get("href", ""))
                name = self._clean_text(a.text(strip=True))
                if name and href and name not in seen:
                    seen.add(name)
                    categories.append({
                        "name": name, "url": href, "level": "top",
                        "low_level_categories": [],
                    })

        self.logger.info(f"Found {len(categories)} top-level categories")
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["top_level"] += 1
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top.get("low_level_categories", []):
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
                for sub in low.get("subcategories", []):
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1
        return {"categories": categories, "stats": stats}

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from a PrestaShop category listing page."""
        tree = HTMLParser(html)
        products = []
        seen_ids = set()

        items = tree.css("article.product-miniature[data-id-product]")
        if not items:
            items = tree.css("article.product-miniature")

        for item in items:
            product_id = item.attributes.get("data-id-product")
            if product_id and product_id in seen_ids:
                continue
            if product_id:
                seen_ids.add(product_id)

            name_el = item.css_first(
                "h6[itemprop='name'], h2.product-title a, h3.product-title a, "
                ".product-title a, h2 a, h3 a"
            )
            product_name = self._clean_text(name_el.text(strip=True)) if name_el else ""

            link_el = item.css_first(
                "a.thumbnail.product-thumbnail, h2.product-title a, "
                "h3.product-title a, .product-title a"
            )
            product_url = (
                self._make_absolute(link_el.attributes.get("href", ""))
                if link_el else None
            )

            if not product_id and not product_url:
                continue

            product_data = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }

            # Image
            img_el = item.css_first(
                "img.img-fluid, img.img-responsive, a.thumbnail img, "
                "div.product-thumbnail img"
            )
            if img_el:
                src = (
                    img_el.attributes.get("src")
                    or img_el.attributes.get("data-src")
                    or img_el.attributes.get("data-lazy-src")
                )
                if src and not src.startswith("data:"):
                    product_data["image"] = self._make_absolute(src)

            # Price
            price_el = item.css_first(
                "span[itemprop='price'], span.price, "
                ".product-price-and-shipping span.price"
            )
            if price_el:
                price_content = price_el.attributes.get("content")
                if price_content:
                    try:
                        product_data["price"] = float(price_content)
                    except (ValueError, TypeError):
                        product_data["price"] = self._parse_price(price_el.text())
                else:
                    product_data["price"] = self._parse_price(price_el.text())
            else:
                product_data["price"] = None

            # Old price
            old_price_el = item.css_first(
                "span.regular-price, .product-price-and-shipping span.regular-price"
            )
            if old_price_el:
                product_data["old_price"] = self._parse_price(old_price_el.text())
                if product_data.get("old_price") and product_data.get("price"):
                    product_data["discount_percent"] = round(
                        (1 - product_data["price"] / product_data["old_price"]) * 100
                    )

            # Out of stock flag
            oos_flag = item.css_first("li.out_of_stock, .product-flag.out_of_stock")
            if oos_flag:
                product_data["availability"] = "Rupture de stock"
                product_data["available"] = False

            products.append(product_data)

        # JSON-LD fallback
        if not products:
            for script in tree.css('script[type="application/ld+json"]'):
                raw = (script.text() or "").strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                blocks = data if isinstance(data, list) else [data]
                for block in blocks:
                    if not isinstance(block, dict) or block.get("@type") != "Product":
                        continue
                    url = self._make_absolute(block.get("url", ""))
                    if url:
                        products.append({
                            "id": str(block.get("sku") or block.get("productID") or ""),
                            "url": url,
                            "name": self._clean_text(block.get("name", "")),
                            "price": self._parse_price(
                                str((block.get("offers") or {}).get("price", ""))
                            ),
                        })

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from PrestaShop category page."""
        tree = HTMLParser(html)
        max_page = 1
        current_page = 1

        next_link = tree.css_first("a.next.js-search-link, a[rel='next']")
        has_next = next_link is not None

        for page_link in tree.css("nav.pagination a, ul.page-list a"):
            try:
                num = int(page_link.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
            href = page_link.attributes.get("href", "")
            page_match = re.search(r"[?&]page=(\d+)", href)
            if page_match:
                try:
                    num = int(page_match.group(1))
                    if num > max_page:
                        max_page = num
                except ValueError:
                    pass

        current_el = tree.css_first("nav.pagination li.current a, ul.page-list li.current a")
        if current_el:
            try:
                current_page = int(current_el.text(strip=True))
            except ValueError:
                pass

        return {
            "current_page": current_page,
            "total_pages": max_page,
            "has_next": has_next,
        }

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape detailed product info from a PrestaShop product page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Product ID from URL (PrestaShop: /123-slug.html)
        url_match = re.search(r"/(\d+)-", url)
        data["product_id"] = url_match.group(1) if url_match else None

        # Title
        title_el = tree.css_first("h1[itemprop='name'], h1.h1, h1")
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        # SKU
        sku_el = tree.css_first("span[itemprop='sku'], div.product-reference span")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        # Price
        price_el = tree.css_first("span[itemprop='price'], div.current-price span.price")
        if price_el:
            price_content = price_el.attributes.get("content")
            if price_content:
                try:
                    data["price"] = float(price_content)
                except (ValueError, TypeError):
                    data["price"] = self._parse_price(price_el.text())
            else:
                data["price"] = self._parse_price(price_el.text())
        else:
            data["price"] = None

        # Old price
        old_price_el = tree.css_first("span.regular-price, div.product-discount span.regular-price")
        if old_price_el:
            data["old_price"] = self._parse_price(old_price_el.text())
            if data.get("old_price") and data.get("price"):
                data["discount_percent"] = round(
                    (1 - data["price"] / data["old_price"]) * 100
                )
        else:
            data["old_price"] = None

        # Brand
        brand_img = tree.css_first("div.product-manufacturer img, .manufacturer img")
        if brand_img:
            data["brand"] = brand_img.attributes.get("alt")
            data["brand_logo"] = self._make_absolute(brand_img.attributes.get("src"))
        else:
            brand_el = tree.css_first("div.product-manufacturer a, .manufacturer a")
            data["brand"] = self._clean_text(brand_el.text(strip=True)) if brand_el else None

        # Availability
        avail_el = tree.css_first("span#product-availability, div.product-availability")
        if avail_el:
            avail_text = avail_el.text(strip=True)
            data["availability"] = avail_text
            lower = avail_text.lower()
            data["available"] = (
                ("en stock" in lower or "disponible" in lower or "in stock" in lower)
                and "rupture" not in lower
                and "indisponible" not in lower
            )
        else:
            avail_link = tree.css_first("link[itemprop='availability'][href]")
            if avail_link:
                href = avail_link.attributes.get("href", "")
                data["availability"] = "En stock" if "InStock" in href else "Rupture de stock"
                data["available"] = "InStock" in href
            else:
                data["availability"] = None
                data["available"] = None

        # Description
        desc_el = tree.css_first(
            "div.product-description, div[id^='product-description-short-'], "
            "div.tab-pane#description"
        )
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        # Specifications
        specs = {}
        features = tree.css_first("section.product-features, .product-features")
        if features:
            for dt in features.css("dt"):
                dd = dt.next
                while dd and dd.tag != "dd":
                    dd = dd.next
                if dd:
                    k = self._clean_text(dt.text(strip=True))
                    v = self._clean_text(dd.text(strip=True))
                    if k and v:
                        specs[k] = v
        data["specifications"] = specs

        # Images
        images = []
        main_img = tree.css_first("div.product-cover img, img[itemprop='image']")
        if main_img:
            src = (
                main_img.attributes.get("data-image-large-src")
                or main_img.attributes.get("data-src")
                or main_img.attributes.get("src")
            )
            if src:
                images.append(self._make_absolute(src))

        for img in tree.css("ul.product-images img.thumb, img.thumb.js-thumb"):
            src = (
                img.attributes.get("data-image-large-src")
                or img.attributes.get("data-src")
                or img.attributes.get("src")
            )
            if src:
                abs_src = self._make_absolute(src)
                if abs_src not in images:
                    images.append(abs_src)

        data["images"] = images[:10] if images else None

        return data


def get_scraper(logger: logging.Logger) -> TaktekScraper:
    return TaktekScraper(logger)
