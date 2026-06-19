#!/usr/bin/env python3
"""
BeautyStore.tn scraper - PrestaShop + IQIT/Lab mega menu, HTTP/selectolax.
"""

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from scraper.base import FastScraper
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    clean_text,
    dedupe_products,
    finalize_product_record,
    html_product_metadata,
    normalize_gtin,
    parse_price,
)


class BeautystoreScraper(FastScraper):
    """HTTPX/selectolax scraper for beautystore.tn."""

    def __init__(self, logger: logging.Logger):
        super().__init__("beautystore", logger)

    # ------------------------------------------------------------------
    # URL and text helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, href: Any) -> Optional[str]:
        return absolute_url(href, self.base_url)

    @staticmethod
    def _clean(value: Any) -> Optional[str]:
        return clean_text(value)

    @staticmethod
    def _node_text(node: Any, separator: str = " ") -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=separator, strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

    @staticmethod
    def _clean_category_name(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = text.lstrip(" \t\r\n>+-:|/\u27a4\u203a\u00bb")
        text = re.sub(r"\s+", " ", text).strip()
        return text or None

    @staticmethod
    def _strip_url(url: str, keep_query: bool = False) -> str:
        parts = urlsplit(url)
        path = parts.path.rstrip("/") if parts.path != "/" else parts.path
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                path or "/",
                parts.query if keep_query else "",
                "",
            )
        )

    @staticmethod
    def _site_host() -> str:
        return "beautystore.tn"

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        host = urlsplit(url).netloc.lower()
        return host == self._site_host() or host.endswith("." + self._site_host())

    def _is_category_url(self, url: Optional[str]) -> bool:
        if not url or not self._is_site_url(url):
            return False

        parts = urlsplit(url)
        path = (parts.path or "/").rstrip("/") or "/"
        low = url.lower()
        blocked = (
            ".html",
            "/brand/",
            "/brands",
            "/cart",
            "/panier",
            "/checkout",
            "/commande",
            "/login",
            "/connexion",
            "/account",
            "/mon-compte",
            "/wishlist",
            "/search",
            "/recherche",
            "/contact",
            "/blog",
            "/module/",
            "/content/",
            "add-to-cart",
            "facebook.",
            "instagram.",
            "youtube.",
            "mailto:",
            "tel:",
            "javascript:",
        )
        if any(token in low for token in blocked):
            return False

        return bool(re.match(r"^/\d+[-_][^/]+$", path))

    def _category_from_link(self, link: Any) -> Optional[Dict[str, str]]:
        if not link:
            return None
        href = self._absolute_url(link.attributes.get("href"))
        if href:
            href = self._strip_url(href)
        if not self._is_category_url(href):
            return None
        name = self._clean_category_name(link.text(strip=True))
        if not name or len(name) > 100:
            return None
        return {"name": name, "url": href}

    @staticmethod
    def _direct_children(node: Any, selector: str) -> List[Any]:
        return [child for child in node.css(selector) if child.parent == node]

    def _first_direct_link(self, node: Any, selector: str = "a[href]") -> Optional[Any]:
        for link in node.css(selector):
            if link.parent == node:
                return link
        return node.css_first(selector)

    @staticmethod
    def _category_stats(categories: List[Dict[str, Any]]) -> Dict[str, int]:
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
        return stats

    @staticmethod
    def _availability_from_text(text: Optional[str]) -> Tuple[Optional[str], Optional[bool]]:
        availability, available = availability_from_text(text)
        normalized = (availability or text or "").lower()
        if available is None and ("instock" in normalized or "stock" in normalized or "disponible" in normalized):
            available = True
        if available is None and ("outofstock" in normalized or "rupture" in normalized or "indisponible" in normalized):
            available = False
        return availability, available

    @staticmethod
    def _product_id_from_url(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        match = re.search(r"/(\d+)-[^/]+\.html(?:$|\?)", url)
        return match.group(1) if match else None

    @staticmethod
    def _barcode_from_url(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        for match in re.finditer(r"(\d{8}|\d{12,14})(?=\.html|[-_/])", url):
            barcode = normalize_gtin(match.group(1))
            if barcode:
                return barcode
        return None

    @staticmethod
    def _node_price(node: Any) -> Optional[float]:
        if not node:
            return None
        price = parse_price(node.attributes.get("content"))
        if price is None:
            price = parse_price(node.text())
        return price

    @staticmethod
    def _discount_percent(price: Optional[float], old_price: Optional[float], text: Optional[str] = None) -> Optional[int]:
        if text and "%" in text:
            match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", text)
            if match:
                return int(round(float(match.group(1).replace(",", "."))))
        if price is not None and old_price and old_price > price:
            return int(round((1 - price / old_price) * 100))
        return None

    @staticmethod
    def _clean_reference(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"^(?:UGS|SKU|Reference|R.f.rence)\s*:?\s*", "", text, flags=re.I)
        return clean_text(text)

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        query_parts = [
            item
            for item in (parts.query or "").split("&")
            if item and not item.startswith("page=")
        ]
        if page_num > 1:
            query_parts.append(f"page={page_num}")
        query = "&".join(query_parts)
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1

        for link in tree.css(cp.get("pagination_pages", ".pagination a, ul.page-list a, nav.pagination a")):
            page_num = self._page_number_from_link(link)
            if page_num is None:
                continue
            total_pages = max(total_pages, page_num)
            classes = link.attributes.get("class", "")
            parent_classes = link.parent.attributes.get("class", "") if link.parent else ""
            if "disabled" in classes or "current" in classes or "current" in parent_classes or "active" in parent_classes:
                current_page = page_num

        next_link = tree.css_first(cp.get("pagination_next", ".pagination a.next.js-search-link, nav.pagination a.next"))
        has_next = False
        if next_link:
            classes = next_link.attributes.get("class", "")
            parent_classes = next_link.parent.attributes.get("class", "") if next_link.parent else ""
            has_next = "disabled" not in classes and "disabled" not in parent_classes

        return {"current_page": current_page, "total_pages": total_pages, "has_next": has_next}

    def _page_number_from_link(self, link: Any) -> Optional[int]:
        text = self._node_text(link) or ""
        if re.fullmatch(r"\d+", text):
            return int(text)
        href = link.attributes.get("href") or ""
        match = re.search(r"[?&]page=(\d+)", href)
        return int(match.group(1)) if match else None

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories: List[Dict[str, Any]] = []
        seen_urls = set()

        root = tree.css_first(fp.get("nav_container", ".lab-menu-horizontal > ul.menu-content, ul.menu-content"))
        top_items = (
            self._direct_children(root, fp.get("top_level_items", "li.level-1"))
            if root
            else []
        )

        for top_li in top_items:
            top_meta = self._category_from_link(
                self._first_direct_link(top_li, fp.get("top_level_link", "a[href]"))
            )
            if not top_meta or top_meta["url"] in seen_urls:
                continue

            seen_urls.add(top_meta["url"])
            top_cat = {
                "name": top_meta["name"],
                "url": top_meta["url"],
                "level": "top",
                "low_level_categories": [],
            }

            top_seen = {top_meta["url"]}
            for column in top_li.css(fp.get("column", ".lab-sub-menu .lab-menu-col.CAT, .lab-sub-menu .lab-menu-col")):
                column_metas = self._column_category_links(
                    column,
                    fp.get("category_links", "a[href]"),
                    top_seen,
                )
                if not column_metas:
                    continue

                low_meta = column_metas[0]
                if low_meta["url"] in seen_urls:
                    continue
                seen_urls.add(low_meta["url"])
                top_seen.add(low_meta["url"])
                low_cat = {
                    "name": low_meta["name"],
                    "url": low_meta["url"],
                    "level": "low",
                    "subcategories": [],
                }

                for sub_meta in column_metas[1:]:
                    if sub_meta["url"] in seen_urls:
                        continue
                    seen_urls.add(sub_meta["url"])
                    top_seen.add(sub_meta["url"])
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
            categories = self._extract_categories_fallback(
                tree.css(fp.get("fallback_links", "header a[href], .lab-menu-horizontal a[href], a[href]")),
                seen_urls,
            )

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _column_category_links(self, column: Any, selector: str, seen_urls: set) -> List[Dict[str, str]]:
        metas: List[Dict[str, str]] = []
        for link in column.css(selector):
            meta = self._category_from_link(link)
            if not meta or meta["url"] in seen_urls:
                continue
            seen_urls.add(meta["url"])
            metas.append(meta)
        return metas

    def _extract_categories_fallback(self, links: List[Any], seen_urls: set) -> List[Dict[str, Any]]:
        categories = []
        for link in links:
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
            link = card.css_first(cp.get("item_url", "a.thumbnail.product-thumbnail, .product-title a"))
            title_link = card.css_first(cp.get("item_name", ".product-title a"))
            product_url = self._absolute_url(link.attributes.get("href") if link else None)
            if product_url:
                product_url = self._strip_url(product_url)

            name = self._node_text(title_link)
            if not name:
                image_node = card.css_first("img[alt], img[title]")
                name = clean_text(
                    image_node.attributes.get("alt") or image_node.attributes.get("title")
                    if image_node
                    else None
                )
            if not product_url or not name:
                continue

            product_id = card.attributes.get("data-id-product") or self._product_id_from_url(product_url)
            price_node = card.css_first(cp.get("item_price", ".product-price-and-shipping .price, span.price"))
            old_price_node = card.css_first(cp.get("item_old_price", ".regular-price"))
            discount_node = card.css_first(cp.get("item_discount", ".product-flag.discount, .discount-percentage, .discount"))
            brand_node = card.css_first(cp.get("item_brand", ".product-brand"))
            reference_node = card.css_first(cp.get("item_reference", ".product-reference"))
            availability_node = card.css_first(cp.get("item_availability", ".product-availability, #product-availability"))

            price = self._node_price(price_node)
            old_price = parse_price(old_price_node.text() if old_price_node else None)
            discount_percent = self._discount_percent(price, old_price, self._node_text(discount_node))
            availability, available = self._availability_from_text(
                self._node_text(availability_node) if availability_node else None
            )

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "url": product_url,
                "name": name,
                "price": price,
            }

            barcode = self._barcode_from_url(product_url)
            if barcode:
                product["barcode"] = barcode
            if old_price is not None:
                product["old_price"] = old_price
            if discount_percent is not None:
                product["discount_percent"] = discount_percent
            if brand_node:
                brand = self._node_text(brand_node)
                if brand:
                    product["brand"] = brand
            if reference_node:
                reference = self._clean_reference(self._node_text(reference_node))
                if reference:
                    product["reference"] = reference
                    product["sku"] = reference
            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available

            image = self._extract_listing_image(card, cp)
            if image:
                product["image"] = image

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "beautystore listing")

    def _extract_listing_image(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        selector = cp.get("item_image", "img[data-full-size-image-url], img[data-src], img[src]")
        attrs = cp.get("item_image_attrs", ["data-full-size-image-url", "data-src", "src"])
        for img in card.css(selector):
            for attr in attrs:
                image = self._absolute_url(img.attributes.get(attr))
                if image and not image.startswith("data:"):
                    return image
        return None

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        product_data = self._extract_product_data(tree, pp)

        data: Dict[str, Any] = {"url": url}
        data.update(html_product_metadata(html, url, self.base_url))
        data["url"] = data.get("url") or url

        product_id = self._extract_detail_product_id(tree, pp)
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        title = self._node_text(tree.css_first(pp.get("title", "h1.h1")))
        if title:
            data["title"] = title
            data.setdefault("name", title)

        reference = self._extract_reference(tree, pp, product_data)
        if reference:
            data["reference"] = reference
            data["sku"] = reference

        barcode = self._extract_barcode(tree, pp, url, product_id)
        if barcode:
            data["barcode"] = barcode

        brand = self._extract_brand(tree, pp)
        if brand:
            data["brand"] = brand

        price = self._extract_detail_price(tree, pp, product_data)
        if price is not None:
            data["price"] = price

        old_price = self._node_price(tree.css_first(pp.get("old_price", ".bloc-cart-price .cart-price-discount")))
        if old_price is not None:
            data["old_price"] = old_price
            discount_percent = self._discount_percent(data.get("price"), old_price)
            if discount_percent is not None:
                data["discount_percent"] = discount_percent

        availability, available = self._extract_availability(tree, pp, product_data)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        short_description = self._extract_short_description(tree, pp)
        if short_description:
            data["short_description"] = short_description
            data.setdefault("overview", short_description)

        description = self._extract_description(tree, pp)
        if description:
            data["description"] = description

        specs = self._extract_specifications(tree, pp)
        if specs:
            data["specifications"] = specs

        breadcrumbs = self._extract_breadcrumbs(tree, pp)
        if breadcrumbs:
            data["breadcrumbs"] = breadcrumbs

        images = self._extract_detail_images(tree, pp)
        if images:
            data["images"] = images
            data["image"] = images[0]

        return finalize_product_record(data)

    def _extract_product_data(self, tree: HTMLParser, pp: Dict[str, Any]) -> Dict[str, Any]:
        for node in tree.css(pp.get("product_data", "#product-details[data-product], #product-details2[data-product]")):
            raw = node.attributes.get("data-product")
            if not raw:
                continue
            try:
                parsed = json.loads(html_lib.unescape(raw))
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _extract_detail_product_id(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        selectors = [
            pp.get("product_id", "input#product_page_product_id[name='id_product'][value]"),
            pp.get("product_id_fallback", "meta[property='product:retailer_item_id'][content]"),
        ]
        for selector in selectors:
            for node in tree.css(selector):
                value = clean_text(node.attributes.get("value") or node.attributes.get("content"))
                if value:
                    return value
        return None

    def _extract_reference(self, tree: HTMLParser, pp: Dict[str, Any], product_data: Dict[str, Any]) -> Optional[str]:
        reference = self._clean_reference(product_data.get("reference"))
        if reference:
            return reference
        node = tree.css_first(pp.get("reference", "meta[itemprop='sku'][content]"))
        return self._clean_reference(node.attributes.get("content") if node else None)

    def _extract_barcode(self, tree: HTMLParser, pp: Dict[str, Any], url: str, product_id: Optional[str]) -> Optional[str]:
        barcode = self._barcode_from_url(url)
        if barcode:
            return barcode

        for node in tree.css(pp.get("barcode", "meta[itemprop='gtin13'][content], meta[itemprop='gtin'][content]")):
            product_scope = self._nearest_product_scope(node)
            if product_scope and product_id:
                product_id_node = product_scope.css_first("meta[itemprop='productID'][content]")
                scoped_id = clean_text(product_id_node.attributes.get("content") if product_id_node else None)
                if scoped_id and scoped_id != str(product_id):
                    continue
            candidate = normalize_gtin(node.attributes.get("content"))
            if candidate:
                return candidate
        return None

    @staticmethod
    def _nearest_product_scope(node: Any) -> Optional[Any]:
        parent = node.parent
        while parent is not None:
            itemtype = parent.attributes.get("itemtype", "")
            if "Product" in itemtype:
                return parent
            parent = parent.parent
        return None

    def _extract_brand(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        brand_img = tree.css_first(pp.get("brand_image", ".product-manufacturer img[alt]"))
        if brand_img:
            brand = clean_text(brand_img.attributes.get("alt"))
            if brand:
                return brand
        brand_link = tree.css_first(pp.get("brand_link", ".product-manufacturer a"))
        brand = self._node_text(brand_link)
        if brand:
            return brand
        brand_meta = tree.css_first(pp.get("brand_meta", "meta[property='product:brand'][content]"))
        return clean_text(brand_meta.attributes.get("content") if brand_meta else None)

    def _extract_detail_price(self, tree: HTMLParser, pp: Dict[str, Any], product_data: Dict[str, Any]) -> Optional[float]:
        price = self._node_price(tree.css_first(".bloc-cart-price .cart-price-value"))
        if price is not None:
            return price
        price = parse_price(product_data.get("price"))
        if price is not None:
            return price
        for selector in ("meta[property='product:price:amount'][content]", "meta[itemprop='price'][content]"):
            node = tree.css_first(selector)
            price = parse_price(node.attributes.get("content") if node else None)
            if price is not None:
                return price
        return None

    def _extract_availability(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        product_data: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[bool]]:
        availability_node = tree.css_first(pp.get("availability", "#product-availability"))
        availability, available = self._availability_from_text(self._node_text(availability_node))
        if availability or available is not None:
            return availability, available

        meta = tree.css_first(pp.get("availability_meta", "meta[property='product:availability'][content], link[itemprop='availability'][href]"))
        availability, available = self._availability_from_text(
            meta.attributes.get("content") or meta.attributes.get("href") if meta else None
        )
        if availability or available is not None:
            return availability, available

        raw_available = clean_text(product_data.get("available_for_order"))
        if raw_available == "1":
            return "Disponible a la commande", True
        if raw_available == "0":
            return "Indisponible", False

        body = tree.css_first("body")
        classes = (body.attributes.get("class", "") if body else "").lower()
        if "out-of-stock" in classes or "unavailable" in classes:
            return "Rupture de stock", False
        if "product-available-for-order" in classes or "product-in-stock" in classes:
            return "Disponible a la commande", True
        return None, None

    def _extract_short_description(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        for node in tree.css(pp.get("short_description", "#product-description-short")):
            text = self._node_text(node)
            if text:
                return text
        return None

    def _extract_description(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        for node in tree.css(pp.get("description", "#description")):
            text = self._node_text(node)
            if text:
                return text
        return None

    def _extract_specifications(self, tree: HTMLParser, pp: Dict[str, Any]) -> Dict[str, str]:
        specs: Dict[str, str] = {}

        keys = tree.css(".product-features dt")
        values = tree.css(".product-features dd")
        for key_node, value_node in zip(keys, values):
            key = self._node_text(key_node)
            value = self._node_text(value_node)
            if key and value:
                specs.setdefault(key, value)

        for row in tree.css(".product-features tr, table tr"):
            cells = row.css("th, td")
            if len(cells) < 2:
                continue
            key = self._node_text(cells[0])
            value = self._node_text(cells[-1])
            if key and value and key != value:
                specs.setdefault(key, value)

        return specs

    def _extract_breadcrumbs(self, tree: HTMLParser, pp: Dict[str, Any]) -> List[str]:
        selector = pp.get("breadcrumbs", "[data-depth] a, .breadcrumb a")
        breadcrumbs: List[str] = []
        for node in tree.css(selector):
            text = self._node_text(node)
            if not text:
                continue
            if text.lower() in {"accueil", "home"} or text in breadcrumbs:
                continue
            breadcrumbs.append(text)
        return breadcrumbs

    def _extract_detail_images(self, tree: HTMLParser, pp: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        selector = ", ".join(
            [
                pp.get("image_main", ".product-cover img"),
                pp.get("image_gallery", ".images-container img, .thumb-container img"),
            ]
        )
        for img in tree.css(selector):
            for attr in ("data-image-large-src", "data-full-size-image-url", "data-src", "content", "src"):
                image = self._absolute_url(img.attributes.get(attr))
                if image and not image.startswith("data:") and image not in images:
                    images.append(image)
                    break
        return images[:12]


def get_scraper(logger: logging.Logger) -> BeautystoreScraper:
    return BeautystoreScraper(logger)
