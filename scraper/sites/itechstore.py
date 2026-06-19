#!/usr/bin/env python3
"""
iTechStore.tn scraper - PrestaShop + IqitMegaMenu, HTTP/selectolax.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from scraper.base import FastScraper
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    dedupe_products,
    finalize_product_record,
    html_product_metadata,
    parse_price,
)


class ItechstoreScraper(FastScraper):
    """HTTPX/selectolax scraper for itechstore.tn (PrestaShop)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("itechstore", logger)

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, href: Any) -> Optional[str]:
        return absolute_url(href, self.base_url)

    @staticmethod
    def _strip_tracking(url: str) -> str:
        parts = urlsplit(url)
        path = parts.path.rstrip("/") if parts.path != "/" else parts.path
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    @staticmethod
    def _is_category_url(url: Optional[str]) -> bool:
        if not url:
            return False
        path = urlsplit(url).path
        if not re.match(r"^/\d+[-_][^/]+/?$", path):
            return False
        low = url.lower()
        blocked = (
            "mon-compte",
            "connexion",
            "panier",
            "cart",
            "recherche",
            "search",
            "module/",
            "content/",
            ".html",
        )
        return not any(token in low for token in blocked)

    @staticmethod
    def _direct_children(node, selector: str) -> List[Any]:
        return [child for child in node.css(selector) if child.parent == node]

    @staticmethod
    def _first_link(node):
        for link in node.css("a[href]"):
            if link.parent == node:
                return link
        return node.css_first("a[href]")

    def _category_from_link(self, link) -> Optional[Dict[str, str]]:
        href = self._absolute_url(link.attributes.get("href")) if link else None
        if href:
            href = self._strip_tracking(href)
        if not self._is_category_url(href):
            return None
        name = re.sub(r"\s+", " ", link.text(strip=True)).strip()
        if not name or len(name) > 90:
            return None
        return {"name": name, "url": href}

    @staticmethod
    def _availability_from_text(text: Optional[str]) -> tuple[Optional[str], Optional[bool]]:
        availability, available = availability_from_text(text)
        normalized = (availability or text or "").lower()
        if available is None and "derni" in normalized:
            available = True
        return availability, available

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = re.sub(r"([?&])page=\d+&?", r"\1", base_url).rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories: List[Dict[str, Any]] = []
        seen_urls = set()

        root = tree.css_first(fp.get("nav_container", "#iqitmegamenu-mobile"))
        top_items = (
            root.css(fp.get("top_level_items", "ul.mobile-menu__scroller > li.mobile-menu__tab"))
            if root
            else []
        )

        for top_li in top_items:
            top_link = self._first_link(top_li)
            top_meta = self._category_from_link(top_link)
            if not top_meta or top_meta["url"] in seen_urls:
                continue
            seen_urls.add(top_meta["url"])

            top_cat = {
                "name": top_meta["name"],
                "url": top_meta["url"],
                "level": "top",
                "low_level_categories": [],
            }

            for top_submenu in self._direct_children(
                top_li, fp.get("submenu", "ul.mobile-menu__submenu")
            ):
                low_items = self._direct_children(top_submenu, "li.mobile-menu__tab")
                for low_li in low_items:
                    low_link = self._first_link(low_li)
                    low_meta = self._category_from_link(low_link)
                    if not low_meta or low_meta["url"] in seen_urls:
                        continue
                    seen_urls.add(low_meta["url"])

                    low_cat = {
                        "name": low_meta["name"],
                        "url": low_meta["url"],
                        "level": "low",
                        "subcategories": [],
                    }

                    for low_submenu in self._direct_children(
                        low_li, fp.get("submenu", "ul.mobile-menu__submenu")
                    ):
                        sub_items = self._direct_children(low_submenu, "li.mobile-menu__tab")
                        for sub_li in sub_items:
                            sub_link = self._first_link(sub_li)
                            sub_meta = self._category_from_link(sub_link)
                            if not sub_meta or sub_meta["url"] in seen_urls:
                                continue
                            seen_urls.add(sub_meta["url"])
                            low_cat["subcategories"].append(
                                {
                                    "name": sub_meta["name"],
                                    "url": sub_meta["url"],
                                    "level": "subcategory",
                                }
                            )

                    top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        if not categories:
            categories = self._extract_categories_fallback(tree, seen_urls)

        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
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

        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _extract_categories_fallback(self, tree: HTMLParser, seen_urls: set) -> List[Dict[str, Any]]:
        categories = []
        for link in tree.css("a[href]"):
            meta = self._category_from_link(link)
            if not meta or meta["url"] in seen_urls:
                continue
            seen_urls.add(meta["url"])
            categories.append(
                {
                    "name": meta["name"],
                    "url": meta["url"],
                    "level": "top",
                    "low_level_categories": [],
                }
            )
        return categories

    # ------------------------------------------------------------------
    # Product listings
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products: List[Dict[str, Any]] = []

        item_selector = cp.get(
            "item_selector",
            "#js-product-list article.product-miniature, article.product-miniature.js-product-miniature",
        )
        for card in tree.css(item_selector):
            product_id = card.attributes.get("data-id-product")
            link = card.css_first(cp.get("item_url", "a.thumbnail.product-thumbnail, h2.product-title a"))
            title_link = card.css_first(cp.get("item_name", "h2.product-title a, .product-title a"))
            product_url = self._absolute_url(link.attributes.get("href") if link else None)
            name = title_link.text(strip=True) if title_link else None
            if not product_url or not name:
                continue

            price_node = card.css_first(cp.get("item_price", "span.product-price"))
            old_price_node = card.css_first(cp.get("item_old_price", ".regular-price"))
            brand_node = card.css_first(cp.get("item_brand", ".product-brand"))
            reference_node = card.css_first(cp.get("item_reference", ".product-reference"))
            availability_node = card.css_first(
                cp.get("item_availability", ".product-availability span, .product-available")
            )

            price = parse_price(
                price_node.attributes.get("content") if price_node else None
            )
            if price is None and price_node:
                price = parse_price(price_node.text())

            availability, available = self._availability_from_text(
                availability_node.text(strip=True) if availability_node else None
            )

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "url": product_url,
                "name": re.sub(r"\s+", " ", name).strip(),
                "price": price,
            }

            old_price = parse_price(old_price_node.text() if old_price_node else None)
            if old_price is not None:
                product["old_price"] = old_price
                if price and old_price != price:
                    product["discount_percent"] = round((1 - price / old_price) * 100)

            if brand_node:
                product["brand"] = brand_node.text(strip=True)

            if reference_node:
                reference = reference_node.text(strip=True)
                if reference:
                    product["reference"] = reference

            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available

            image = self._extract_image(card, cp)
            if image:
                product["image"] = image

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "itechstore listing")

    def _extract_image(self, card, cp: Dict[str, Any]) -> Optional[str]:
        image_selector = cp.get("item_image", "a.thumbnail.product-thumbnail img, img")
        attrs = cp.get("item_image_attrs", ["data-full-size-image-url", "data-src", "src"])
        for img in card.css(image_selector):
            for attr in attrs:
                value = img.attributes.get(attr)
                image = self._absolute_url(value)
                if image and not image.startswith("data:"):
                    return image
        return None

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1

        for link in tree.css(cp.get("pagination_pages", ".pagination .page-list a.js-search-link")):
            text = link.text(strip=True)
            try:
                page_num = int(text)
            except (TypeError, ValueError):
                continue
            total_pages = max(total_pages, page_num)
            classes = link.attributes.get("class", "")
            parent_classes = link.parent.attributes.get("class", "") if link.parent else ""
            if "disabled" in classes or "current" in parent_classes:
                current_page = page_num

        next_link = tree.css_first(cp.get("pagination_next", ".pagination a.next.js-search-link"))
        has_next = bool(next_link and "disabled" not in next_link.attributes.get("class", ""))
        return {"current_page": current_page, "total_pages": total_pages, "has_next": has_next}

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = {"url": url}

        metadata = html_product_metadata(html, url, self.base_url)
        data.update(metadata)

        product_id_node = tree.css_first(pp.get("product_id", "input[name='id_product'][value]"))
        if product_id_node:
            data["product_id"] = product_id_node.attributes.get("value")

        title_node = tree.css_first(pp.get("title", "h1.h1.page-title, h1.h1"))
        if title_node:
            data["title"] = title_node.text(strip=True)

        reference_node = tree.css_first(pp.get("reference", ".product-reference span"))
        if reference_node:
            reference = reference_node.text(strip=True)
            if reference:
                data["reference"] = reference
                data["sku"] = reference

        brand = self._extract_brand(tree, pp)
        if brand:
            data["brand"] = brand

        price_node = tree.css_first(
            pp.get("price", ".product-prices .current-price span.product-price")
        )
        price = parse_price(price_node.attributes.get("content") if price_node else None)
        if price is None and price_node:
            price = parse_price(price_node.text())
        if price is not None:
            data["price"] = price

        old_price_node = tree.css_first(pp.get("old_price", ".regular-price"))
        old_price = parse_price(old_price_node.text() if old_price_node else None)
        if old_price is not None:
            data["old_price"] = old_price
            if data.get("price") and old_price != data["price"]:
                data["discount_percent"] = round((1 - data["price"] / old_price) * 100)

        availability_node = tree.css_first(pp.get("availability", "#product-availability"))
        availability, available = self._availability_from_text(
            availability_node.text(strip=True) if availability_node else None
        )
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        description_node = tree.css_first(pp.get("description", "#description .product-description"))
        if description_node:
            data["description"] = re.sub(r"\s+", " ", description_node.text(strip=True)).strip()

        specs = self._extract_specifications(tree, pp)
        if specs:
            data["specifications"] = specs

        images = self._extract_detail_images(tree, pp)
        if images:
            data["images"] = images
            data["image"] = images[0]

        return finalize_product_record(data)

    def _extract_brand(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        brand_img = tree.css_first(pp.get("brand_image", ".product-manufacturer img[alt]"))
        if brand_img:
            brand = brand_img.attributes.get("alt")
            if brand:
                return brand.strip()
        brand_link = tree.css_first(pp.get("brand_link", ".product-manufacturer a"))
        if brand_link:
            brand = brand_link.text(strip=True)
            if brand:
                return brand
        return None

    def _extract_specifications(self, tree: HTMLParser, pp: Dict[str, Any]) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        keys = tree.css(pp.get("specs_key", ".product-features .data-sheet dt.name"))
        values = tree.css(pp.get("specs_value", ".product-features .data-sheet dd.value"))
        for key_node, value_node in zip(keys, values):
            key = re.sub(r"\s+", " ", key_node.text(strip=True)).strip()
            value = re.sub(r"\s+", " ", value_node.text(strip=True)).strip()
            if key and value:
                specs[key] = value
        return specs

    def _extract_detail_images(self, tree: HTMLParser, pp: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        selector = ", ".join(
            [
                pp.get("image_main", ".product-cover img"),
                pp.get("image_gallery", ".images-container img, .thumb-container img"),
            ]
        )
        for img in tree.css(selector):
            for attr in ("data-image-large-src", "data-src", "content", "src"):
                image = self._absolute_url(img.attributes.get(attr))
                if image and not image.startswith("data:") and image not in images:
                    images.append(image)
                    break
        return images[:10]


def get_scraper(logger: logging.Logger) -> ItechstoreScraper:
    return ItechstoreScraper(logger)
