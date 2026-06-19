#!/usr/bin/env python3
"""
Parahouse.tn scraper - PrestaShop + JMS MegaMenu, HTTP/selectolax.
"""

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


class ParahouseScraper(FastScraper):
    """HTTPX/selectolax scraper for parahouse.tn (PrestaShop)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("parahouse", logger)

    # ------------------------------------------------------------------
    # URL and text helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, href: Any) -> Optional[str]:
        return absolute_url(href, self.base_url)

    @staticmethod
    def _clean(value: Any) -> Optional[str]:
        return clean_text(value)

    @staticmethod
    def _text(node: Any, separator: str = " ") -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=separator, strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

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
    def _direct_children(node: Any, selector: str) -> List[Any]:
        return [child for child in node.css(selector) if child.parent == node]

    @staticmethod
    def _first_direct_link(node: Any) -> Optional[Any]:
        for link in node.css("a[href]"):
            if link.parent == node:
                return link
        return node.css_first("a[href]")

    @staticmethod
    def _first_srcset_url(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        first = value.split(",", 1)[0].strip()
        return first.split(" ", 1)[0] if first else None

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _product_id_from_class(class_name: str) -> Optional[str]:
        match = re.search(r"(?:^|\s)product-id-(\d+)(?:\s|$)", class_name or "")
        if match:
            return match.group(1)
        match = re.search(r"(?:^|\s)(?:post|postid)-(\d+)(?:\s|$)", class_name or "")
        return match.group(1) if match else None

    @classmethod
    def _body_product_id(cls, tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        return cls._product_id_from_class(body.attributes.get("class", "") if body else "")

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        return host == base_host

    def _is_category_url(self, url: Optional[str]) -> bool:
        if not url or not self._is_site_url(url):
            return False
        path = urlsplit(url).path
        if not re.match(r"^/fr/\d+-[^/]+/?$", path):
            return False
        low = url.lower()
        blocked = (
            "/fr/-",
            ".html",
            "accueil/",
            "product",
            "produit",
            "cart",
            "panier",
            "checkout",
            "order",
            "commande",
            "account",
            "mon-compte",
            "connexion",
            "search",
            "recherche",
            "contact",
            "blog",
            "cms",
            "content",
            "module/",
            "manufacturer",
            "brand/",
            "mailto:",
            "tel:",
            "javascript:",
            "#",
        )
        return not any(token in low for token in blocked)

    def _category_from_link(self, link: Any) -> Optional[Dict[str, str]]:
        if not link:
            return None
        url = self._absolute_url(link.attributes.get("href"))
        if url:
            url = self._strip_url(url)
        if not self._is_category_url(url):
            return None
        name = self._clean(link.text(strip=True)) or self._name_from_category_url(url)
        if not name or len(name) > 100:
            return None
        return {"name": name, "url": url}

    @staticmethod
    def _name_from_category_url(url: str) -> str:
        slug = urlsplit(url).path.strip("/").split("/")[-1]
        slug = re.sub(r"^\d+-", "", slug)
        return re.sub(r"[-_]+", " ", slug).strip().title() or slug

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "page"]
        if page_num > 1:
            query.append(("page", str(page_num)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1

        for link in tree.css(cp.get("pagination_pages", ".pagination .page-list a.js-search-link")):
            text = self._clean(link.text(strip=True))
            href = link.attributes.get("href", "")
            page_num = None
            if text and text.isdigit():
                page_num = int(text)
            else:
                match = re.search(r"[?&]page=(\d+)", href)
                if match:
                    page_num = int(match.group(1))
            if not page_num:
                continue
            total_pages = max(total_pages, page_num)
            classes = link.attributes.get("class", "")
            parent_classes = link.parent.attributes.get("class", "") if link.parent else ""
            if "disabled" in classes or "current" in classes or "current" in parent_classes:
                current_page = page_num

        next_link = tree.css_first(cp.get("pagination_next", ".pagination a.next.js-search-link"))
        has_next = bool(next_link and "disabled" not in next_link.attributes.get("class", ""))
        return {"current_page": current_page, "total_pages": total_pages, "has_next": has_next}

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories: List[Dict[str, Any]] = []
        seen_urls = set()

        top_items = tree.css(
            fp.get(
                "top_level_items",
                "#jms-megamenu-container .jms-megamenu > ul.nav.level0 > li.menu-item",
            )
        )
        self.logger.info(f"Found {len(top_items)} Parahouse top-level menu items")

        for top_li in top_items:
            top_meta = self._category_from_link(self._first_direct_link(top_li))
            if not top_meta or top_meta["url"] in seen_urls:
                continue
            seen_urls.add(top_meta["url"])

            top_cat = {
                "name": top_meta["name"],
                "url": top_meta["url"],
                "level": "top",
                "low_level_categories": [],
            }

            for low_li in self._menu_level_items(top_li, 1):
                low_meta = self._category_from_link(self._first_direct_link(low_li))
                if not low_meta or low_meta["url"] in seen_urls:
                    continue
                seen_urls.add(low_meta["url"])
                low_cat = {
                    "name": low_meta["name"],
                    "url": low_meta["url"],
                    "level": "low",
                    "subcategories": [],
                }

                for sub_li in self._descendant_menu_items(low_li, min_level=2):
                    sub_meta = self._category_from_link(self._first_direct_link(sub_li))
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
            categories = self._extract_categories_from_links(
                tree.css(fp.get("fallback_links", ".jms-megamenu li.menu-item a[href], nav a[href], footer a[href]")),
                seen_urls,
            )

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _menu_level_items(self, top_li: Any, level: int) -> List[Any]:
        items = []
        for li in top_li.css("li.menu-item"):
            data_level = self._safe_int(li.attributes.get("data-level"))
            if data_level == level:
                items.append(li)
        if items:
            return items

        for child in top_li.css(".nav-child li.menu-item"):
            link = self._first_direct_link(child)
            if self._category_from_link(link):
                items.append(child)
        return items

    def _descendant_menu_items(self, node: Any, min_level: int) -> List[Any]:
        items = []
        for li in node.css("li.menu-item"):
            data_level = self._safe_int(li.attributes.get("data-level"))
            if data_level is None or data_level >= min_level:
                items.append(li)
        return items

    def _extract_categories_from_links(self, links: List[Any], seen_urls: set) -> List[Dict[str, Any]]:
        categories: List[Dict[str, Any]] = []
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

    # ------------------------------------------------------------------
    # Product listings
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products: List[Dict[str, Any]] = []

        for card in tree.css(cp.get("item_selector", "#js-product-list .product-miniature.js-product-miniature")):
            url, name = self._link_and_name(card, cp)
            if not url or not self._is_product_url(url) or not name:
                continue

            product_id = self._extract_card_id(card, cp)
            price = self._extract_price(card, cp.get("item_price", ".content_price .price, .price.new"))
            old_price = self._extract_price(card, cp.get("item_old_price", ".regular-price, .old-price"))
            availability, available = self._availability_from_card(card, cp)

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "url": url,
                "name": name,
                "price": price,
            }

            discount = self._extract_discount(card, cp, price, old_price)
            if old_price is not None:
                product["old_price"] = old_price
            if discount is not None:
                product["discount_percent"] = discount
            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available

            image = self._extract_image(
                card,
                cp.get("item_image", "img.product-img1, img"),
                cp.get("item_image_attrs", ["data-full-size-image-url", "data-src", "src", "srcset"]),
            )
            if image:
                product["image"] = image

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "parahouse listing")

    def _is_product_url(self, url: Optional[str]) -> bool:
        return bool(url and self._is_site_url(url) and urlsplit(url).path.endswith(".html"))

    def _link_and_name(self, card: Any, cp: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        chosen = None
        for link in card.css(cp.get("item_url", "a.product-link[href], a.product-image[href]")):
            href = self._absolute_url(link.attributes.get("href"))
            if not self._is_product_url(href):
                continue
            chosen = link
            if self._clean(link.attributes.get("title")) or self._clean(link.text(strip=True)):
                break
        if not chosen:
            return None, None

        url = self._absolute_url(chosen.attributes.get("href"))
        name = self._clean(chosen.attributes.get("title")) or self._clean(chosen.text(strip=True))
        if not name or name.endswith("..."):
            img = card.css_first("img[title], img[alt]")
            name = (
                self._clean(img.attributes.get("title") if img else None)
                or self._clean(img.attributes.get("alt") if img else None)
                or name
            )
        return (self._strip_url(url) if url else None), name

    def _extract_card_id(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        product_id = self._clean(card.attributes.get("data-id-product"))
        if product_id:
            return product_id
        node = card.css_first(cp.get("item_id_button", "button.ajax-add-to-cart[data-id-product], [data-id-product]"))
        return self._clean(node.attributes.get("data-id-product") if node else None)

    def _extract_price(self, root: Any, selector: str) -> Optional[float]:
        node = root.css_first(selector)
        if not node:
            return None
        return parse_price(node.attributes.get("content") or node.text(strip=True))

    def _extract_discount(
        self,
        card: Any,
        cp: Dict[str, Any],
        price: Optional[float],
        old_price: Optional[float],
    ) -> Optional[int]:
        node = card.css_first(cp.get("item_discount", ".product-flag.discount, .discount"))
        text = self._clean(node.text(strip=True) if node else None)
        if text:
            match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", text)
            if match:
                return round(float(match.group(1).replace(",", ".")))
        if price and old_price and old_price != price:
            return round((1 - price / old_price) * 100)
        return None

    def _availability_from_card(self, card: Any, cp: Dict[str, Any]) -> Tuple[Optional[str], Optional[bool]]:
        class_name = (card.attributes.get("class") or "").lower()
        if any(token in class_name for token in ("outofstock", "out-of-stock", "unavailable")):
            return "Rupture de stock", False

        stock_node = card.css_first(".availability-list.out-of-stock, .out-of-stock, .product-availability")
        stock_text = self._text(stock_node)
        if stock_text and re.search(r"rupture|indisponible|sold\s*out", stock_text, re.I):
            return stock_text, False

        button = card.css_first(cp.get("item_id_button", "button.ajax-add-to-cart[data-id-product]"))
        if button:
            button_classes = button.attributes.get("class", "").lower()
            if "disabled" not in button.attributes and "disabled" not in button_classes:
                return "En stock", True

        text = self._text(card)
        return availability_from_text(text)

    def _extract_image(self, root: Any, selector: str, attrs: List[str]) -> Optional[str]:
        for img in root.css(selector):
            for attr in attrs:
                value = img.attributes.get(attr)
                if attr == "srcset":
                    value = self._first_srcset_url(value)
                image = self._absolute_url(value)
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
        data: Dict[str, Any] = {"url": self._strip_url(url)}

        metadata = html_product_metadata(html, url, self.base_url)
        data.update(metadata)
        data["url"] = self._strip_url(data.get("url") or url)

        product_json = self._extract_product_json(tree, pp)
        if product_json:
            self._apply_product_json(data, product_json)

        product_id = self._extract_detail_product_id(tree, pp, product_json)
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id
        self._clean_metadata_identifier(data, product_id)

        title_node = tree.css_first(pp.get("title", "h1, .h1[itemprop='name'], .product-name"))
        title = self._clean_title(self._text(title_node) or data.get("title"))
        if title:
            data["title"] = title

        reference_node = tree.css_first(pp.get("reference", ".product-reference span[itemprop='sku'], .product-reference span"))
        self._merge_identifier_fields(data, self._text(reference_node), product_id, replace_existing=False)

        brand = self._extract_brand(tree, pp)
        if brand:
            data["brand"] = brand

        price = self._extract_price(tree, pp.get("price", ".product-prices .current-price .price, .product-price"))
        if price is not None:
            data["price"] = price

        old_price = self._extract_price(tree, pp.get("old_price", ".regular-price, .product-discount .regular-price"))
        if old_price is not None:
            data["old_price"] = old_price

        discount = self._extract_discount(tree, {"item_discount": pp.get("discount", ".discount_text.discount, .product-flag.discount, .discount")}, data.get("price"), data.get("old_price"))
        if discount is not None:
            data["discount_percent"] = discount
        elif data.get("price") and data.get("old_price") and data["old_price"] != data["price"]:
            data["discount_percent"] = round((1 - data["price"] / data["old_price"]) * 100)

        availability, available = self._detail_availability(tree, pp, product_json)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        overview_node = tree.css_first(pp.get("overview", "#product-description-short"))
        overview = self._clean_description_text(self._text(overview_node, separator="\n"))
        if overview:
            data["overview"] = overview
            data.setdefault("description", overview)

        description_node = tree.css_first(pp.get("description", "#description .product-description, #description"))
        description = self._clean_description_text(self._text(description_node, separator="\n"))
        if description:
            data["description"] = description

        specs = self._extract_specifications(tree, pp, product_json, description_node)
        if specs:
            data["specifications"] = specs

        images = self._extract_detail_images(tree, pp, product_json)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = self._extract_detail_categories(tree, pp, data.get("title"), product_json)
        if categories:
            data["categories"] = categories

        return finalize_product_record(data)

    def _extract_product_json(self, tree: HTMLParser, pp: Dict[str, Any]) -> Dict[str, Any]:
        node = tree.css_first(pp.get("product_data", "#product-details[data-product]"))
        raw = node.attributes.get("data-product") if node else None
        if not raw:
            return {}
        try:
            parsed = json.loads(html_lib.unescape(raw))
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _apply_product_json(self, data: Dict[str, Any], product_json: Dict[str, Any]) -> None:
        product_id = self._clean(product_json.get("id_product") or product_json.get("id"))
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        title = self._clean_title(product_json.get("name") or product_json.get("meta_title"))
        if title:
            data["title"] = title

        self._merge_identifier_fields(data, product_json.get("reference"), product_id)

        price = parse_price(product_json.get("price_amount"))
        if price is None:
            price = parse_price(product_json.get("price"))
        if price is not None:
            data["price"] = price

        old_price = parse_price(product_json.get("price_without_reduction"))
        if old_price is not None and price is not None and old_price > price + 0.01:
            data["old_price"] = old_price

        discount = self._discount_from_json(product_json)
        if discount is not None:
            data["discount_percent"] = discount

        description = self._html_fragment_text(product_json.get("description"))
        if description:
            data["description"] = description
        overview = self._html_fragment_text(product_json.get("description_short"))
        if overview:
            data["overview"] = overview

        availability, available = self._availability_from_product_json(product_json)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        specs = self._specs_from_product_json(product_json)
        if specs:
            data["specifications"] = specs

    def _extract_detail_product_id(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        product_json: Dict[str, Any],
    ) -> Optional[str]:
        product_id = self._clean(product_json.get("id_product") or product_json.get("id"))
        if product_id:
            return product_id
        node = tree.css_first(pp.get("product_id", "input[name='id_product'][value]"))
        if node:
            for attr in ("value", "data-id-product", "data-product-id", "data-product_id"):
                product_id = self._clean(node.attributes.get(attr))
                if product_id:
                    return product_id
        return self._body_product_id(tree)

    def _extract_brand(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        brand_img = tree.css_first(pp.get("brand_image", ".product-manufacturer img[alt]"))
        brand = self._clean(brand_img.attributes.get("alt") if brand_img else None)
        if brand:
            return brand
        brand_link = tree.css_first(pp.get("brand_link", ".product-manufacturer a"))
        return self._clean(brand_link.text(strip=True) if brand_link else None)

    def _detail_availability(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        product_json: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[bool]]:
        node = tree.css_first(pp.get("availability", "#product-availability"))
        availability, available = availability_from_text(
            self._clean_availability_text(node.text(strip=True) if node else None)
        )
        if availability:
            return availability, available

        quantity_node = tree.css_first(pp.get("quantity", ".product-quantities"))
        availability, available = availability_from_text(
            self._clean_availability_text(quantity_node.text(strip=True) if quantity_node else None)
        )
        if availability:
            return availability, available

        availability, available = self._availability_from_product_json(product_json)
        if availability:
            return availability, available

        body = tree.css_first("body")
        class_name = (body.attributes.get("class", "") if body else "").lower()
        if "product-available-for-order" in class_name:
            return "En stock", True
        if "outofstock" in class_name or "unavailable" in class_name:
            return "Rupture de stock", False
        return None, None

    def _availability_from_product_json(self, product_json: Dict[str, Any]) -> Tuple[Optional[str], Optional[bool]]:
        text = self._clean(product_json.get("availability_message"))
        if text:
            availability, available = availability_from_text(text)
            return availability or text, available

        availability_value = self._clean(product_json.get("availability"))
        if availability_value:
            lower = availability_value.lower()
            if lower == "available":
                return "En stock", True
            if lower in {"unavailable", "out_of_stock", "outofstock"}:
                return "Rupture de stock", False
            availability, available = availability_from_text(availability_value)
            return availability, available

        quantity = self._safe_int(product_json.get("quantity"))
        if quantity is not None:
            return ("En stock", True) if quantity > 0 else ("Rupture de stock", False)
        return None, None

    def _extract_specifications(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        product_json: Dict[str, Any],
        description_node: Any,
    ) -> Dict[str, Any]:
        specs: Dict[str, Any] = {}

        keys = tree.css(pp.get("specs_key", ".product-features .data-sheet dt.name"))
        values = tree.css(pp.get("specs_value", ".product-features .data-sheet dd.value"))
        for key_node, value_node in zip(keys, values):
            key = self._text(key_node)
            value = self._text(value_node)
            if key and value:
                specs[key] = value

        for row in tree.css(pp.get("specs_rows", ".product-features tr, table.product-features tr")):
            key_node = row.css_first("th, td:first-child")
            value_node = row.css_first("td:last-child")
            if not key_node or not value_node or key_node == value_node:
                continue
            key = self._text(key_node)
            value = self._text(value_node)
            if key and value:
                specs.setdefault(key, value)

        specs.update({k: v for k, v in self._specs_from_product_json(product_json).items() if k not in specs})

        if description_node:
            for key, value in self._specs_from_text_node(description_node).items():
                specs.setdefault(key, value)
        return specs

    def _specs_from_product_json(self, product_json: Dict[str, Any]) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        features = product_json.get("features")
        if isinstance(features, list):
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                key = self._clean(feature.get("name"))
                value = self._clean(feature.get("value"))
                if key and value:
                    specs[key] = value
        return specs

    def _specs_from_text_node(self, node: Any) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        candidates = node.css("li, p")
        if not candidates:
            candidates = [node]
        for child in candidates:
            text = self._text(child)
            if not text or ":" not in text or len(text) > 200:
                continue
            key, value = text.split(":", 1)
            key = self._clean(key)
            value = self._clean(value)
            if key and value and len(key) <= 60:
                specs[key] = value
        return specs

    def _extract_detail_images(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        product_json: Dict[str, Any],
    ) -> List[str]:
        images: List[str] = []

        for image in self._images_from_product_json(product_json):
            if image not in images:
                images.append(image)

        selector = ", ".join(
            [
                pp.get("image_main", ".product-cover img"),
                pp.get("image_gallery", ".images-container img, .thumb-container img, .product-cover img"),
            ]
        )
        for img in tree.css(selector):
            for attr in ("data-image-large-src", "data-image-medium-src", "data-full-size-image-url", "data-src", "src", "srcset"):
                value = img.attributes.get(attr)
                if attr == "srcset":
                    value = self._first_srcset_url(value)
                image = self._absolute_url(value)
                if image and not image.startswith("data:") and image not in images:
                    images.append(image)
                    break
        return images[:20]

    def _images_from_product_json(self, product_json: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        raw_images = product_json.get("images")
        if not isinstance(raw_images, list):
            return images
        for item in raw_images:
            if not isinstance(item, dict):
                continue
            candidates = [
                ((item.get("large") or {}).get("url") if isinstance(item.get("large"), dict) else None),
                ((item.get("bySize") or {}).get("large_default") or {}).get("url")
                if isinstance(item.get("bySize"), dict)
                else None,
                ((item.get("medium") or {}).get("url") if isinstance(item.get("medium"), dict) else None),
            ]
            for candidate in candidates:
                image = self._absolute_url(candidate)
                if image and image not in images:
                    images.append(image)
                    break
        return images

    def _extract_detail_categories(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        title: Optional[str],
        product_json: Dict[str, Any],
    ) -> List[str]:
        categories: List[str] = []
        title_norm = (title or "").lower()
        for node in tree.css(pp.get("breadcrumbs", ".breadcrumb a")):
            name = self._clean(node.text(strip=True))
            if not name:
                continue
            low = name.lower()
            if low in {"accueil", "home"} or low == title_norm:
                continue
            if name not in categories:
                categories.append(name)

        json_category = self._clean(product_json.get("category_name"))
        if json_category and json_category.lower() not in {"accueil", "home"} and json_category not in categories:
            categories.append(json_category)
        return categories

    # ------------------------------------------------------------------
    # Detail cleanup helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_title(value: Any) -> Optional[str]:
        title = clean_text(value)
        if not title:
            return None
        title = re.sub(r"\s*\|\s*PARAHOUSE(?:\s+Tunisie)?\s*$", "", title, flags=re.I)
        return clean_text(title)

    @staticmethod
    def _clean_description_text(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"^(?:Description\s*){2,}", "Description ", text, flags=re.I)
        return clean_text(text)

    @staticmethod
    def _clean_availability_text(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"^-?\d+(?:[,.]\d+)?\s*%\s*", "", text)
        return clean_text(text)

    def _html_fragment_text(self, value: Any) -> Optional[str]:
        raw = self._clean(value)
        if not raw:
            return None
        tree = HTMLParser(raw)
        text = tree.body.text(separator=" ", strip=True) if tree.body else tree.text(separator=" ", strip=True)
        return self._clean_description_text(text)

    @staticmethod
    def _discount_from_json(product_json: Dict[str, Any]) -> Optional[int]:
        for key in ("discount_percentage_absolute", "discount_percentage", "discount_amount_to_display"):
            text = clean_text(product_json.get(key))
            if not text:
                continue
            match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", text)
            if match:
                return round(float(match.group(1).replace(",", ".")))
        return None

    def _clean_metadata_identifier(self, data: Dict[str, Any], product_id: Optional[str]) -> None:
        raw = data.get("reference") or data.get("sku")
        if raw:
            self._merge_identifier_fields(data, raw, product_id)

    def _merge_identifier_fields(
        self,
        data: Dict[str, Any],
        raw_identifier: Any,
        product_id: Optional[str],
        replace_existing: bool = True,
    ) -> None:
        fields = self._identifier_fields(raw_identifier, product_id)
        if replace_existing:
            data.pop("reference", None)
            data.pop("sku", None)

        data_quality = fields.pop("data_quality", None)
        if data_quality:
            data.setdefault("data_quality", {}).update(data_quality)
        data.update(fields)

    def _identifier_fields(self, raw_identifier: Any, product_id: Optional[str]) -> Dict[str, Any]:
        value = self._normalize_identifier_value(raw_identifier)
        if not value or (product_id and value == str(product_id)):
            return {}

        gtin = normalize_gtin(value)
        if gtin:
            return {"barcode": gtin}

        if re.fullmatch(r"\d{8}|\d{12,14}", value):
            return {"data_quality": {"invalid_barcode": value}}

        if value.isdigit():
            return {}

        return {"reference": value, "sku": value}

    @staticmethod
    def _normalize_identifier_value(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if re.fullmatch(r"\d+\.0", text):
            text = text[:-2]
        return text


def get_scraper(logger: logging.Logger) -> ParahouseScraper:
    return ParahouseScraper(logger)
