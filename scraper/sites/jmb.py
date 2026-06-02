#!/usr/bin/env python3
"""
JMB Tunisie scraper - WordPress/WooCommerce + Machic theme, HTTP/selectolax.
"""

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx
from selectolax.parser import HTMLParser

from scraper.base import FastScraper
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    clean_text,
    dedupe_products,
    finalize_product_record,
    html_product_metadata,
    parse_price,
)


class JmbScraper(FastScraper):
    """HTTPX/selectolax scraper for jmb.com.tn."""

    def __init__(self, logger: logging.Logger):
        super().__init__("jmb", logger)

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
        return "jmb.com.tn"

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        host = urlsplit(url).netloc.lower()
        return host == self._site_host() or host.endswith("." + self._site_host())

    def _is_category_url(self, url: Optional[str]) -> bool:
        if not url or not self._is_site_url(url):
            return False

        parts = urlsplit(url)
        path = parts.path or "/"
        low = url.lower()
        blocked = (
            "/cart",
            "/cart-",
            "/checkout",
            "/my-account",
            "/wishlist",
            "/compare",
            "/blog",
            "/sav",
            "/contact",
            "/a-propos",
            "/boutique",
            "/cdn-cgi/",
            "/wp-",
            "add-to-cart",
            "add-to-compare",
            "filter_",
            "query_type_",
            "shop_view=",
            "mailto:",
            "tel:",
            "javascript:",
            "#",
        )
        if parts.query:
            return False
        if any(token in low for token in blocked):
            return False
        return path not in {"", "/"}

    def _category_from_link(self, link: Any) -> Optional[Dict[str, str]]:
        if not link:
            return None
        href = self._absolute_url(link.attributes.get("href"))
        if href:
            href = self._strip_url(href)
        if not self._is_category_url(href):
            return None
        name = self._node_text(link)
        if not name or len(name) > 120:
            return None
        return {"name": name, "url": href}

    @staticmethod
    def _direct_children(node: Any, selector: str) -> List[Any]:
        selector = selector.strip()
        if selector.startswith(">"):
            selector = selector[1:].strip()
        return [child for child in node.css(selector) if child.parent == node]

    def _first_direct_link(self, node: Any, selector: str = "> a[href]") -> Optional[Any]:
        selector = selector.strip()
        if selector.startswith(">"):
            selector = selector[1:].strip()
        for link in node.css(selector):
            if link.parent == node:
                return link
        return node.css_first("a[href]")

    @staticmethod
    def _post_id_from_class(class_name: str) -> Optional[str]:
        match = re.search(r"(?:^|\s)(?:post|postid)-(\d+)(?:\s|$)", class_name or "")
        return match.group(1) if match else None

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
    def _availability_from_text(text: Optional[str]) -> Tuple[Optional[str], Optional[bool]]:
        availability, available = availability_from_text(text)
        normalized = (availability or text or "").lower()
        if available is None and ("in stock" in normalized or "only" in normalized and "stock" in normalized):
            available = True
        if available is None and ("sur commande" in normalized or "backorder" in normalized):
            available = True
        if available is None and ("out of stock" in normalized or "rupture" in normalized or "indisponible" in normalized):
            available = False
        return availability, available

    @staticmethod
    def _first_srcset_url(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        first = value.split(",", 1)[0].strip()
        return first.split(" ", 1)[0] if first else None

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
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        path = re.sub(r"/page/\d+/?$", "", parts.path or "/").rstrip("/")
        if page_num > 1:
            path = f"{path}/page/{page_num}"
        path = path or "/"
        if path != "/" and not path.endswith("/"):
            path += "/"
        query = "&".join(
            item
            for item in (parts.query or "").split("&")
            if item and not item.startswith("paged=")
        )
        return urlunsplit((parts.scheme, parts.netloc, path, query, ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1

        current_node = tree.css_first(
            cp.get(
                "pagination_current",
                "nav.woocommerce-pagination span.page-numbers.current, ul.page-numbers span.current, span.page-numbers.current",
            )
        )
        current_text = self._node_text(current_node)
        if current_text and current_text.isdigit():
            current_page = int(current_text)
            total_pages = max(total_pages, current_page)

        for link in tree.css(cp.get("pagination_pages", "nav.woocommerce-pagination a.page-numbers, ul.page-numbers a, a.page-numbers")):
            page_num = self._page_number_from_link(link)
            if page_num is not None:
                total_pages = max(total_pages, page_num)

        next_link = tree.css_first(cp.get("pagination_next", "nav.woocommerce-pagination a.next.page-numbers, ul.page-numbers a.next, a.next.page-numbers"))
        has_next = bool(next_link and "disabled" not in next_link.attributes.get("class", ""))
        return {"current_page": current_page, "total_pages": total_pages, "has_next": has_next}

    def _page_number_from_link(self, link: Any) -> Optional[int]:
        text = self._node_text(link) or ""
        if text.isdigit():
            return int(text)
        href = link.attributes.get("href") or ""
        match = re.search(r"/page/(\d+)/?", href) or re.search(r"[?&]paged=(\d+)", href)
        return int(match.group(1)) if match else None

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        root = tree.css_first(fp.get("nav_container", "ul#menu-sidebar-menu.departments-menu, ul.departments-menu.show"))
        categories: List[Dict[str, Any]] = []
        seen_urls = set()

        top_items = self._direct_children(root, fp.get("top_level_items", "li.menu-item-object-product_cat")) if root else []
        for top_li in top_items:
            top_meta = self._category_from_link(
                self._first_direct_link(top_li, fp.get("top_level_link", "> a[href]"))
            )
            if not top_meta or top_meta["url"] in seen_urls:
                continue

            top_seen = {top_meta["url"]}
            low_metas = self._fetch_child_category_links(top_meta["url"], fp)
            if not low_metas:
                low_metas = self._nested_child_links(top_li, fp, top_seen)

            top_cat = {
                "name": top_meta["name"],
                "url": top_meta["url"],
                "level": "top",
                "low_level_categories": [],
            }
            seen_urls.add(top_meta["url"])

            for low_meta in low_metas:
                if low_meta["url"] in seen_urls:
                    continue
                seen_urls.add(low_meta["url"])

                sub_metas = self._fetch_child_category_links(low_meta["url"], fp)
                if not sub_metas:
                    low_node = self._find_menu_node_by_url(top_li, low_meta["url"])
                    sub_metas = self._nested_child_links(low_node, fp, {low_meta["url"]}) if low_node else []

                low_cat = {
                    "name": low_meta["name"],
                    "url": low_meta["url"],
                    "level": "low",
                    "subcategories": [],
                }
                for sub_meta in sub_metas:
                    if sub_meta["url"] in seen_urls:
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
            categories = self._extract_categories_fallback(
                tree.css(fp.get("fallback_links", "header li.menu-item-object-product_cat > a[href], .widget_product_categories a[href]")),
                seen_urls,
            )

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _fetch_child_category_links(self, url: str, fp: Dict[str, Any]) -> List[Dict[str, str]]:
        try:
            with httpx.Client(
                headers=self.headers,
                follow_redirects=True,
                timeout=httpx.Timeout(self.request_timeout, connect=10.0),
            ) as client:
                response = client.get(url)
                if response.status_code >= 400 or not response.text:
                    return []
        except Exception as exc:
            self.logger.debug(f"Failed to fetch category children {url}: {exc}")
            return []

        tree = HTMLParser(response.text)
        children: List[Dict[str, str]] = []
        seen = {url}
        selector = fp.get("child_category_links", ".widget_product_categories .product-categories > li > a[href]")
        for link in tree.css(selector):
            meta = self._category_from_link(link)
            if not meta or meta["url"] in seen:
                continue
            seen.add(meta["url"])
            children.append(meta)
        return children

    def _nested_child_links(self, node: Any, fp: Dict[str, Any], seen_urls: set) -> List[Dict[str, str]]:
        if not node:
            return []
        children: List[Dict[str, str]] = []
        for submenu in self._direct_children(node, fp.get("submenu", "> ul.sub-menu")):
            for child_li in self._direct_children(submenu, "li.menu-item-object-product_cat"):
                meta = self._category_from_link(self._first_direct_link(child_li, "> a[href]"))
                if not meta or meta["url"] in seen_urls:
                    continue
                seen_urls.add(meta["url"])
                children.append(meta)
        return children

    def _find_menu_node_by_url(self, root: Any, url: str) -> Optional[Any]:
        target = self._strip_url(url)
        for link in root.css("a[href]") if root else []:
            href = self._absolute_url(link.attributes.get("href"))
            if href and self._strip_url(href) == target:
                return link.parent
        return None

    def _extract_categories_fallback(self, links: Iterable[Any], seen_urls: set) -> List[Dict[str, Any]]:
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

        item_selector = cp.get("item_selector", ".products .product.type-product, div.product.type-product")
        for card in tree.css(item_selector):
            product_id = self._post_id_from_class(card.attributes.get("class", ""))
            link = card.css_first(cp.get("item_url", ".thumbnail-wrapper > a[href], .product-title a"))
            title_link = card.css_first(cp.get("item_name", ".product-title a, .product-title"))
            product_url = self._absolute_url(link.attributes.get("href") if link else None)
            if product_url:
                product_url = self._strip_url(product_url)
            name = self._node_text(title_link)
            if not name:
                image_node = card.css_first("img[alt]")
                name = clean_text(image_node.attributes.get("alt") if image_node else None)
            if not product_url or not name:
                continue

            price_node = card.css_first("ins .woocommerce-Price-amount.amount")
            if not price_node:
                price_node = card.css_first(cp.get("item_price", "ins .woocommerce-Price-amount.amount, .product-price-cart .price, span.price"))
            old_price_node = card.css_first(cp.get("item_old_price", "del .woocommerce-Price-amount.amount"))
            discount_node = card.css_first(cp.get("item_discount", ".product-badges .badge.onsale, .onsale"))
            availability_node = card.css_first(cp.get("item_availability", ".product-message, .stock"))

            price = self._node_price(price_node)
            old_price = self._node_price(old_price_node)
            discount_percent = self._discount_percent(price, old_price, self._node_text(discount_node))
            availability, available = self._availability_from_card(card, availability_node)

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "shop": self.site_name,
                "url": product_url,
                "name": name,
                "price": price,
            }
            if old_price is not None:
                product["old_price"] = old_price
            if discount_percent is not None:
                product["discount_percent"] = discount_percent
            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available

            image = self._extract_listing_image(card, cp)
            if image:
                product["image"] = image

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "jmb listing")

    def _availability_from_card(self, card: Any, availability_node: Any) -> Tuple[Optional[str], Optional[bool]]:
        availability, available = self._availability_from_text(self._node_text(availability_node))
        classes = (card.attributes.get("class", "") or "").lower()
        if available is None:
            if "outofstock" in classes:
                availability, available = availability or "Rupture de stock", False
            elif "onbackorder" in classes:
                availability, available = availability or "Sur commande", True
            elif "instock" in classes:
                availability, available = availability or "En stock", True
        return availability, available

    def _extract_listing_image(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        selector = cp.get("item_image", "img[data-src], img[src]")
        attrs = cp.get("item_image_attrs", ["data-src", "src"])
        for img in card.css(selector):
            for attr in attrs:
                image = self._absolute_url(img.attributes.get(attr))
                if image and not image.startswith("data:"):
                    return image
            image = self._absolute_url(self._first_srcset_url(img.attributes.get("srcset")))
            if image:
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
        jsonld_product = self._jsonld_product(html, url)

        data: Dict[str, Any] = {"url": url, "shop": self.site_name}
        data.update(html_product_metadata(html, url, self.base_url))
        data["url"] = data.get("url") or url

        product_id = self._extract_detail_product_id(tree)
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        title = self._node_text(tree.css_first(pp.get("title", "h1.product_title.entry-title, h1.entry-title, h1")))
        if title:
            data["title"] = title
            data.setdefault("name", title)

        reference = self._node_text(tree.css_first(pp.get("reference", ".sku_wrapper .sku")))
        if reference and reference.upper() != "N/A":
            data["reference"] = reference
            data["sku"] = reference

        specs = self._extract_specifications(tree, pp)
        brand = specs.get("Marque") if specs else None
        if not brand:
            brand = self._brand_from_jsonld(jsonld_product)
        if brand:
            data["brand"] = brand

        price = self._price_from_jsonld(jsonld_product)
        if price is None:
            price = self._extract_detail_price(tree, pp)
        if price is not None:
            data["price"] = price

        old_price = self._extract_detail_old_price(tree, data.get("price"))
        if old_price is not None:
            data["old_price"] = old_price
            discount_percent = self._discount_percent(data.get("price"), old_price)
            if discount_percent is not None:
                data["discount_percent"] = discount_percent

        availability, available = self._extract_detail_availability(tree, pp, jsonld_product)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        short_description = self._extract_first_text(tree, pp.get("short_description", ".woocommerce-product-details__short-description"))
        if short_description:
            data["short_description"] = short_description
            data.setdefault("overview", short_description)

        description = self._extract_first_text(tree, pp.get("description", "#tab-description, .woocommerce-Tabs-panel--description"))
        if description:
            data["description"] = description

        if specs:
            data["specifications"] = specs

        breadcrumbs = self._extract_breadcrumbs(tree, pp, jsonld_product)
        if breadcrumbs:
            data["breadcrumbs"] = breadcrumbs

        images = self._extract_detail_images(tree, pp)
        if not images:
            images = self._images_from_jsonld(jsonld_product)
        if images:
            data["images"] = images
            data["image"] = images[0]

        return finalize_product_record(data)

    def _extract_detail_product_id(self, tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        product_id = self._post_id_from_class(body.attributes.get("class", "") if body else "")
        if product_id:
            return product_id
        for node in tree.css("div.product[class]"):
            product_id = self._post_id_from_class(node.attributes.get("class", ""))
            if product_id:
                return product_id
        return None

    def _extract_detail_price(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[float]:
        price_node = tree.css_first(".single-product-wrapper .price ins .woocommerce-Price-amount.amount")
        if price_node:
            return self._node_price(price_node)
        price_node = tree.css_first(pp.get("price", ".single-product-wrapper .price, .price"))
        return self._node_price(price_node)

    def _extract_detail_old_price(self, tree: HTMLParser, price: Optional[float]) -> Optional[float]:
        main_price_node = tree.css_first(".single-product-wrapper .price")
        old_price = self._node_price(
            main_price_node.css_first("del .woocommerce-Price-amount.amount") if main_price_node else None
        )
        if old_price is not None and price is not None and old_price <= price:
            return None
        return old_price

    def _extract_detail_availability(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        jsonld_product: Optional[dict],
    ) -> Tuple[Optional[str], Optional[bool]]:
        availability, available = self._availability_from_text(self._availability_from_jsonld(jsonld_product))
        if availability or available is not None:
            return availability, available

        node = tree.css_first(pp.get("availability", ".stock, .product-message"))
        availability, available = self._availability_from_text(self._node_text(node))
        if availability or available is not None:
            return availability, available

        classes = []
        body = tree.css_first("body")
        if body:
            classes.append(body.attributes.get("class", ""))
        product = tree.css_first("div.product[class]")
        if product:
            classes.append(product.attributes.get("class", ""))
        joined = " ".join(classes).lower()
        if "outofstock" in joined:
            return "Rupture de stock", False
        if "onbackorder" in joined:
            return "Sur commande", True
        if "instock" in joined:
            return "En stock", True
        return None, None

    def _extract_first_text(self, tree: HTMLParser, selector: str) -> Optional[str]:
        for node in tree.css(selector):
            text = self._node_text(node)
            if text:
                return text
        return None

    def _extract_specifications(self, tree: HTMLParser, pp: Dict[str, Any]) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for row in tree.css(pp.get("specs_rows", "table.shop_attributes tr, .woocommerce-product-attributes tr")):
            key_node = row.css_first("th, td:first-child")
            value_node = row.css_first("td:last-child")
            if key_node and value_node and key_node != value_node:
                key = self._node_text(key_node)
                value = self._node_text(value_node)
                if key and value:
                    specs.setdefault(key, value)
        return specs

    def _extract_breadcrumbs(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        jsonld_product: Optional[dict],
    ) -> List[str]:
        breadcrumbs: List[str] = []
        for node in tree.css(pp.get("breadcrumbs", "nav.woocommerce-breadcrumb a")):
            text = self._node_text(node)
            if text and text.lower() not in {"accueil", "home"} and text not in breadcrumbs:
                breadcrumbs.append(text)

        if breadcrumbs:
            return breadcrumbs

        for item in self._jsonld_breadcrumb_items(jsonld_product):
            name = clean_text(item)
            if name and name.lower() not in {"accueil", "home"} and name not in breadcrumbs:
                breadcrumbs.append(name)
        return breadcrumbs

    def _extract_detail_images(self, tree: HTMLParser, pp: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        selector = pp.get("image_gallery", ".woocommerce-product-gallery__image img, .woocommerce-product-gallery img")
        for img in tree.css(selector):
            for attr in ("data-large_image", "data-src", "src"):
                image = self._absolute_url(img.attributes.get(attr))
                if image and not image.startswith("data:") and image not in images:
                    images.append(image)
                    break
            else:
                image = self._absolute_url(self._first_srcset_url(img.attributes.get("srcset")))
                if image and image not in images:
                    images.append(image)
        return images[:12]

    # ------------------------------------------------------------------
    # JSON-LD helpers
    # ------------------------------------------------------------------

    def _jsonld_product(self, html: str, url: str) -> Optional[dict]:
        target = self._strip_url(url)
        products = []
        tree = HTMLParser(html)
        for script in tree.css("script[type='application/ld+json']"):
            raw = script.text()
            if not raw:
                continue
            try:
                parsed = json.loads(html_lib.unescape(raw.strip()))
            except (TypeError, json.JSONDecodeError):
                continue
            products.extend([obj for obj in self._walk_json(parsed) if self._is_jsonld_product(obj)])

        for product in products:
            product_url = self._strip_url(str(product.get("url") or product.get("@id") or ""))
            if product_url == target:
                return product
        return products[0] if products else None

    def _jsonld_breadcrumb_items(self, jsonld_product: Optional[dict]) -> List[str]:
        return []

    @staticmethod
    def _walk_json(value: Any) -> Iterable[Any]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from JmbScraper._walk_json(child)
        elif isinstance(value, list):
            for item in value:
                yield from JmbScraper._walk_json(item)

    @staticmethod
    def _is_jsonld_product(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        raw = value.get("@type")
        types = raw if isinstance(raw, list) else [raw]
        return any(str(item).lower() == "product" for item in types if item)

    @staticmethod
    def _brand_from_jsonld(product: Optional[dict]) -> Optional[str]:
        if not isinstance(product, dict):
            return None
        brand = product.get("brand")
        if isinstance(brand, dict):
            return clean_text(brand.get("name") or brand.get("@id"))
        return clean_text(brand)

    def _price_from_jsonld(self, product: Optional[dict]) -> Optional[float]:
        offer = self._first_offer(product)
        if not offer:
            return None
        price = parse_price(offer.get("price"))
        if price is not None:
            return price
        specs = offer.get("priceSpecification")
        spec_items = specs if isinstance(specs, list) else [specs]
        for spec in spec_items:
            if isinstance(spec, dict):
                price = parse_price(spec.get("price"))
                if price is not None:
                    return price
        return None

    def _availability_from_jsonld(self, product: Optional[dict]) -> Optional[str]:
        offer = self._first_offer(product)
        if offer:
            return clean_text(offer.get("availability"))
        return None

    @staticmethod
    def _first_offer(product: Optional[dict]) -> Optional[dict]:
        if not isinstance(product, dict):
            return None
        offers = product.get("offers")
        if isinstance(offers, list):
            return offers[0] if offers and isinstance(offers[0], dict) else None
        return offers if isinstance(offers, dict) else None

    @staticmethod
    def _images_from_jsonld(product: Optional[dict]) -> List[str]:
        if not isinstance(product, dict):
            return []
        raw = product.get("image")
        items = raw if isinstance(raw, list) else [raw]
        images = []
        for item in items:
            image = clean_text(item.get("url") if isinstance(item, dict) else item)
            if image and image not in images:
                images.append(image)
        return images


def get_scraper(logger: logging.Logger) -> JmbScraper:
    return JmbScraper(logger)
