#!/usr/bin/env python3
"""
KamounHome scraper.

The site is WooCommerce on WordPress with a Woodmart theme. Category pages and
product detail pages are fully available over HTTP, so this scraper intentionally
uses FastScraper instead of Playwright.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit, urlunsplit

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


class KamounhomeScraper(FastScraper):
    """Fast HTTP scraper for kamounhome.tn."""

    CATEGORY_API_PATH = "/wp-json/wp/v2/product_cat"
    MENU_CATEGORY_SELECTOR = (
        "header a[href*='/boutique/'], "
        ".menu a[href*='/boutique/'], "
        ".wd-nav a[href*='/boutique/'], "
        "footer a[href*='/boutique/']"
    )
    PRODUCT_CARD_SELECTOR = (
        "div.product-grid-item.product.type-product, "
        "div.product.type-product, "
        "li.product.type-product, "
        "ul.products li.product, "
        ".wd-product"
    )
    IMAGE_ATTRS = (
        "data-large_image",
        "data-src",
        "data-lazy-src",
        "data-o_src",
        "data-srcset",
        "srcset",
        "src",
    )
    BAD_IMAGE_MARKERS = (
        "logo-kamoun-home",
        "/logo-",
        "woocommerce-placeholder",
        "placeholder",
        "blank.gif",
    )
    SKIP_CATEGORY_PARTS = (
        "/cart",
        "/panier",
        "/checkout",
        "/mon-compte",
        "/my-account",
        "/wishlist",
        "/compare",
        "/contact",
        "/blog",
        "add-to-cart",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("kamounhome", logger)

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    def _abs_url(self, value: Any) -> Optional[str]:
        return absolute_url(value, self.base_url)

    def _canonical_url(self, value: Any, keep_query: bool = False) -> Optional[str]:
        url = self._abs_url(value)
        if not url:
            return None
        parts = urlsplit(url)
        path = re.sub(r"/page/\d+/?$", "/", parts.path)
        if path != "/" and not path.endswith("/"):
            path += "/"
        query = parts.query if keep_query else ""
        return urlunsplit((parts.scheme, parts.netloc, path, query, ""))

    def _is_category_url(self, value: Any) -> bool:
        url = self._canonical_url(value)
        if not url:
            return False
        parts = urlsplit(url)
        if "kamounhome.tn" not in parts.netloc:
            return False
        if "/boutique/" not in parts.path:
            return False
        lower = url.lower()
        if any(part in lower for part in self.SKIP_CATEGORY_PARTS):
            return False
        return True

    def _node_text(self, node: Any) -> Optional[str]:
        if not node:
            return None
        return clean_text(node.text(separator=" ", strip=True))

    def _first_text(self, root: Any, selectors: Iterable[str]) -> Optional[str]:
        for selector in selectors:
            node = root.css_first(selector)
            text = self._node_text(node)
            if text:
                return text
        return None

    def _first_attr(
        self, root: Any, selectors: Iterable[str], attrs: Iterable[str]
    ) -> Optional[str]:
        for selector in selectors:
            node = root.css_first(selector)
            if not node:
                continue
            for attr in attrs:
                value = clean_text(node.attributes.get(attr))
                if value:
                    return value
        return None

    def _srcset_first_url(self, value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        for candidate in text.split(","):
            url = clean_text(candidate.split()[0] if candidate.split() else "")
            if url:
                return url
        return None

    def _is_bad_image(self, value: Any) -> bool:
        url = clean_text(value)
        if not url:
            return True
        lower = url.lower()
        if lower.startswith("data:"):
            return True
        return any(marker in lower for marker in self.BAD_IMAGE_MARKERS)

    def _image_from_node(self, node: Any) -> Optional[str]:
        if not node:
            return None
        for attr in self.IMAGE_ATTRS:
            raw = node.attributes.get(attr)
            if attr.endswith("srcset"):
                raw = self._srcset_first_url(raw)
            url = self._abs_url(raw)
            if url and not self._is_bad_image(url):
                return url
        return None

    def _collect_images(self, nodes: Iterable[Any]) -> List[str]:
        images: List[str] = []
        for node in nodes:
            image = self._image_from_node(node)
            if image and image not in images:
                images.append(image)
        return images

    def _first_price(self, root: Any, selectors: Iterable[str]) -> Optional[float]:
        for selector in selectors:
            for node in root.css(selector):
                value = node.attributes.get("content") or node.text(separator=" ", strip=True)
                price = parse_price(value)
                if price is not None:
                    return price
        return None

    def _listing_prices(self, item: Any) -> Tuple[Optional[float], Optional[float]]:
        old_price = self._first_price(
            item,
            (
                "span.price del .woocommerce-Price-amount bdi",
                "span.price del .woocommerce-Price-amount",
            ),
        )
        current_price = self._first_price(
            item,
            (
                "span.price ins .woocommerce-Price-amount bdi",
                "span.price ins .woocommerce-Price-amount",
            ),
        )
        if current_price is None:
            amounts = []
            for node in item.css("span.price .woocommerce-Price-amount bdi"):
                price = parse_price(node.text(separator=" ", strip=True))
                if price is not None:
                    amounts.append(price)
            if not amounts:
                for node in item.css("span.price .woocommerce-Price-amount"):
                    price = parse_price(node.text(separator=" ", strip=True))
                    if price is not None:
                        amounts.append(price)
            if amounts:
                current_price = amounts[-1] if old_price and len(amounts) > 1 else amounts[0]
        return current_price, old_price

    def _discount_percent(
        self, price: Optional[float], old_price: Optional[float]
    ) -> Optional[int]:
        if price is None or old_price is None or old_price <= price or old_price <= 0:
            return None
        return round(((old_price - price) / old_price) * 100)

    def _availability_from_class_or_text(self, *values: Any) -> Tuple[Optional[str], Optional[bool]]:
        for value in values:
            availability, available = availability_from_text(value)
            if availability or available is not None:
                return availability, available
        return None, None

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def _fetch_category_api_items(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page = 1
        headers = dict(self.headers)
        headers["Accept"] = "application/json"
        client_kwargs: Dict[str, Any] = {
            "headers": headers,
            "follow_redirects": True,
            "timeout": httpx.Timeout(self.request_timeout, connect=10.0),
        }
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url

        with httpx.Client(**client_kwargs) as client:
            while page <= 20:
                query = urlencode({"per_page": 100, "page": page})
                url = f"{self.base_url.rstrip('/')}{self.CATEGORY_API_PATH}?{query}"
                response = client.get(url)
                if response.status_code == 400 and page > 1:
                    break
                response.raise_for_status()

                payload = response.json()
                if not isinstance(payload, list) or not payload:
                    break
                items.extend(item for item in payload if isinstance(item, dict))

                total_pages = int(response.headers.get("x-wp-totalpages") or "0")
                if total_pages and page >= total_pages:
                    break
                if not total_pages and len(payload) < 100:
                    break
                page += 1
        return items

    def _category_entry(self, item: Dict[str, Any], level: str) -> Optional[Dict[str, Any]]:
        name = clean_text(item.get("name"))
        url = self._canonical_url(item.get("link") or item.get("url"))
        if not name or not url or not self._is_category_url(url):
            return None
        entry: Dict[str, Any] = {
            "name": name,
            "url": url,
            "level": level,
        }
        if item.get("id") is not None:
            entry["category_id"] = str(item.get("id"))
        if item.get("count") is not None:
            entry["source_count"] = item.get("count")
        if level == "top":
            entry["low_level_categories"] = []
        elif level == "low":
            entry["subcategories"] = []
        return entry

    def _build_api_category_hierarchy(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        by_id = {int(item["id"]): item for item in items if item.get("id") is not None}
        children_by_parent: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for item in items:
            try:
                parent_id = int(item.get("parent") or 0)
            except (TypeError, ValueError):
                parent_id = 0
            children_by_parent[parent_id].append(item)

        useful_cache: Dict[int, bool] = {}

        def is_useful(item: Dict[str, Any]) -> bool:
            try:
                item_id = int(item.get("id"))
            except (TypeError, ValueError):
                return False
            if item_id in useful_cache:
                return useful_cache[item_id]
            count = int(item.get("count") or 0)
            useful = count > 0 or any(is_useful(child) for child in children_by_parent.get(item_id, []))
            useful_cache[item_id] = useful
            return useful

        def descendant_categories(parent_id: int) -> List[Dict[str, Any]]:
            descendants: List[Dict[str, Any]] = []
            for child in children_by_parent.get(parent_id, []):
                if not is_useful(child):
                    continue
                try:
                    child_id = int(child.get("id"))
                except (TypeError, ValueError):
                    continue
                child_descendants = descendant_categories(child_id)
                if child_descendants:
                    descendants.extend(child_descendants)
                    continue
                entry = self._category_entry(child, "subcategory")
                if entry:
                    descendants.append(entry)
            return descendants

        categories: List[Dict[str, Any]] = []
        for root in children_by_parent.get(0, []):
            if not is_useful(root):
                continue
            top = self._category_entry(root, "top")
            if not top:
                continue
            try:
                root_id = int(root.get("id"))
            except (TypeError, ValueError):
                root_id = 0

            for low_item in children_by_parent.get(root_id, []):
                if not is_useful(low_item):
                    continue
                low = self._category_entry(low_item, "low")
                if not low:
                    continue
                try:
                    low_id = int(low_item.get("id"))
                except (TypeError, ValueError):
                    low_id = 0
                low["subcategories"] = descendant_categories(low_id)
                top["low_level_categories"].append(low)

            categories.append(top)

        missing_parent_roots = [
            item for item in items
            if item.get("id") in by_id
            and int(item.get("parent") or 0) not in by_id
            and int(item.get("parent") or 0) != 0
            and is_useful(item)
        ]
        for item in missing_parent_roots:
            top = self._category_entry(item, "top")
            if top:
                categories.append(top)

        return categories

    def _fallback_categories_from_html(self, html: str) -> List[Dict[str, Any]]:
        tree = HTMLParser(html)
        nodes_by_key: Dict[Tuple[str, ...], Dict[str, Any]] = {}

        def pretty_slug(value: str) -> str:
            return clean_text(value.replace("-", " ").replace("_", " ").title()) or value

        for link in tree.css(self.MENU_CATEGORY_SELECTOR):
            url = self._canonical_url(link.attributes.get("href"))
            if not url or not self._is_category_url(url):
                continue
            path = urlsplit(url).path.strip("/")
            try:
                boutique_index = path.split("/").index("boutique")
            except ValueError:
                continue
            pieces = [part for part in path.split("/")[boutique_index + 1:] if part]
            if not pieces:
                continue
            key = tuple(pieces[:3])
            name = self._node_text(link) or pretty_slug(key[-1])
            nodes_by_key[key] = {"name": name, "url": url}

        categories: Dict[str, Dict[str, Any]] = {}
        lows: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for key, node in sorted(nodes_by_key.items(), key=lambda item: (len(item[0]), item[0])):
            top_slug = key[0]
            top = categories.setdefault(
                top_slug,
                {
                    "name": pretty_slug(top_slug),
                    "url": node["url"] if len(key) == 1 else "",
                    "level": "top",
                    "low_level_categories": [],
                },
            )
            if len(key) == 1:
                top["name"] = node["name"]
                top["url"] = node["url"]
                continue

            low_key = (top_slug, key[1])
            low = lows.get(low_key)
            if not low:
                low = {
                    "name": pretty_slug(key[1]),
                    "url": node["url"] if len(key) == 2 else "",
                    "level": "low",
                    "subcategories": [],
                }
                lows[low_key] = low
                top["low_level_categories"].append(low)
            if len(key) == 2:
                low["name"] = node["name"]
                low["url"] = node["url"]
                continue

            sub = {"name": node["name"], "url": node["url"], "level": "subcategory"}
            if all(existing.get("url") != sub["url"] for existing in low["subcategories"]):
                low["subcategories"].append(sub)

        return [category for category in categories.values() if category.get("url") or category.get("low_level_categories")]

    def _category_stats(self, categories: List[Dict[str, Any]]) -> Dict[str, int]:
        stats = {"top_level": len(categories), "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            if top.get("url"):
                stats["total_urls"] += 1
            lows = top.get("low_level_categories") or []
            stats["low_level"] += len(lows)
            for low in lows:
                if low.get("url"):
                    stats["total_urls"] += 1
                subs = low.get("subcategories") or []
                stats["subcategory"] += len(subs)
                stats["total_urls"] += sum(1 for sub in subs if sub.get("url"))
        return stats

    def extract_categories_from_html(self, html: str) -> dict:
        categories: List[Dict[str, Any]] = []
        try:
            api_items = self._fetch_category_api_items()
            categories = self._build_api_category_hierarchy(api_items)
            self.logger.info(f"KamounHome category API: {len(api_items)} terms, {len(categories)} top categories")
        except Exception as exc:
            self.logger.warning(f"KamounHome category API failed, falling back to menu links: {exc}")

        if not categories:
            categories = self._fallback_categories_from_html(html)

        return {"categories": categories, "stats": self._category_stats(categories)}

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------

    def _extract_product_id(self, item: Any) -> Optional[str]:
        for value in (
            item.attributes.get("data-id"),
            self._first_attr(item, ("[data-product_id]", "[data-product-id]"), ("data-product_id", "data-product-id")),
        ):
            text = clean_text(value)
            if text:
                return text
        classes = item.attributes.get("class") or ""
        match = re.search(r"\bpost-(\d+)\b", classes)
        return match.group(1) if match else None

    def _extract_listing_availability(self, item: Any) -> Tuple[Optional[str], Optional[bool]]:
        classes = item.attributes.get("class") or ""
        text = self._first_text(item, (".stock", ".availability", ".wd-stock-status"))
        return self._availability_from_class_or_text(classes, text)

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products: List[Dict[str, Any]] = []

        for item in tree.css(self.PRODUCT_CARD_SELECTOR):
            link = item.css_first(
                "a.product-image-link[href], "
                "h3.wd-entities-title a[href], "
                "a.woocommerce-LoopProduct-link[href], "
                "a.woocommerce-loop-product__link[href]"
            )
            url = self._canonical_url(link.attributes.get("href")) if link else None
            if not url:
                continue

            name = self._first_text(
                item,
                (
                    "h3.wd-entities-title a",
                    "h3.wd-entities-title",
                    "h2.woocommerce-loop-product__title",
                    "h3.product-title",
                ),
            )
            if not name and link:
                name = self._node_text(link)

            price, old_price = self._listing_prices(item)
            product_id = self._extract_product_id(item)
            sku = self._first_attr(
                item,
                ("a.add_to_cart_button[data-product_sku]", "a.button[data-product_sku]"),
                ("data-product_sku",),
            )
            brand = self._first_text(item, (".wd-product-brands a", ".product-brand a"))
            listing_categories = [
                text for text in (self._node_text(cat) for cat in item.css(".wd-product-cats a"))
                if text
            ]
            availability, available = self._extract_listing_availability(item)
            image = self._collect_images(item.css("img"))[:1]

            product: Dict[str, Any] = {
                "url": url,
                "name": name,
                "price": price,
                "shop": self.site_name,
            }
            if product_id:
                product["id"] = product_id
                product["product_id"] = product_id
            if sku:
                product["sku"] = sku
                product["reference"] = sku
            if old_price is not None:
                product["old_price"] = old_price
            discount_percent = self._discount_percent(price, old_price)
            if discount_percent is not None:
                product["discount_percent"] = discount_percent
            if image:
                product["image"] = image[0]
            if brand:
                product["brand"] = brand
            if listing_categories:
                product["listing_categories"] = listing_categories
            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "kamounhome listing")

    def build_page_url(self, base_url: str, page_num: int) -> str:
        if page_num <= 1:
            return base_url
        parts = urlsplit(base_url)
        path = re.sub(r"/page/\d+/?$", "/", parts.path)
        path = path.rstrip("/") + f"/page/{page_num}/"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        current_page = 1
        total_pages = 1

        current = tree.css_first("nav.woocommerce-pagination .page-numbers.current, .woocommerce-pagination .current")
        current_text = self._node_text(current)
        if current_text and current_text.isdigit():
            current_page = int(current_text)

        for node in tree.css("nav.woocommerce-pagination .page-numbers, .woocommerce-pagination .page-numbers"):
            text = self._node_text(node)
            if text and text.isdigit():
                total_pages = max(total_pages, int(text))

        has_next = tree.css_first("nav.woocommerce-pagination a.next.page-numbers, .woocommerce-pagination a.next.page-numbers") is not None
        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
            "max_page": total_pages,
            "has_next_page": has_next,
        }

    # ------------------------------------------------------------------
    # Details
    # ------------------------------------------------------------------

    def _detail_product_id(self, tree: HTMLParser) -> Optional[str]:
        product_id = self._first_attr(
            tree,
            (
                "input[name='product_id'][value]",
                "button[name='add-to-cart'][value]",
                "[data-product_id]",
                "[data-product-id]",
            ),
            ("value", "data-product_id", "data-product-id"),
        )
        if product_id:
            return product_id
        for selector in ("body", "div.product.type-product", ".product"):
            node = tree.css_first(selector)
            classes = node.attributes.get("class") if node else ""
            match = re.search(r"\bpost(?:id)?-(\d+)\b", classes or "")
            if match:
                return match.group(1)
        return None

    def _detail_prices(self, tree: HTMLParser) -> Tuple[Optional[float], Optional[float]]:
        old_price = self._first_price(
            tree,
            (
                "p.price del .woocommerce-Price-amount bdi",
                "p.price del .woocommerce-Price-amount",
            ),
        )
        price = self._first_price(
            tree,
            (
                "p.price ins .woocommerce-Price-amount bdi",
                "p.price ins .woocommerce-Price-amount",
            ),
        )
        if price is None:
            amount_nodes = tree.css("p.price .woocommerce-Price-amount bdi")
            if not amount_nodes:
                amount_nodes = tree.css("p.price .woocommerce-Price-amount")
            amounts = [
                parsed for parsed in (
                    parse_price(node.text(separator=" ", strip=True)) for node in amount_nodes
                )
                if parsed is not None
            ]
            if amounts:
                price = amounts[-1] if old_price and len(amounts) > 1 else amounts[0]
        return price, old_price

    def _extract_specs(self, tree: HTMLParser) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for row in tree.css(".woocommerce-product-attributes tr, table.woocommerce-product-attributes tr"):
            key_node = row.css_first("th, td:first-child")
            value_node = row.css_first("td:last-child")
            key = self._node_text(key_node)
            value = self._node_text(value_node)
            if key and value and key != value:
                specs[key.rstrip(":")] = value
        return specs

    def _brand_from_specs(self, specs: Dict[str, str]) -> Optional[str]:
        for key, value in specs.items():
            if re.search(r"\b(marque|brand)\b", key, re.I):
                return value
        return None

    def _identifier_from_specs(self, specs: Dict[str, str], pattern: str) -> Optional[str]:
        for key, value in specs.items():
            if re.search(pattern, key, re.I):
                return value
        return None

    def _detail_availability(self, tree: HTMLParser) -> Tuple[Optional[str], Optional[bool]]:
        stock_text = self._first_text(tree, ("p.stock", ".stock", ".availability", ".wd-stock-status"))
        product_node = tree.css_first("div.product.type-product, .product")
        product_classes = product_node.attributes.get("class") if product_node else ""
        body = tree.css_first("body")
        body_classes = body.attributes.get("class") if body else ""
        return self._availability_from_class_or_text(stock_text, product_classes, body_classes)

    def _detail_breadcrumbs(self, tree: HTMLParser) -> List[str]:
        breadcrumbs: List[str] = []
        for node in tree.css(".woocommerce-breadcrumb a, nav.woocommerce-breadcrumb a, .breadcrumbs a"):
            text = self._node_text(node)
            if text and text not in breadcrumbs:
                breadcrumbs.append(text)
        return breadcrumbs

    def _best_description(
        self,
        short_description: Optional[str],
        full_description: Optional[str],
        title: Optional[str],
    ) -> Optional[str]:
        if full_description:
            title_text = (title or "").strip().lower()
            full_text = full_description.strip().lower()
            if full_text != title_text and len(full_description) >= 40:
                return full_description
        return short_description or full_description

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "fetch_failed"}

        tree = HTMLParser(html)
        data: Dict[str, Any] = html_product_metadata(html, product_url=url, base_url=self.base_url)
        data["url"] = self._canonical_url(data.get("url") or url) or url

        title = self._first_text(tree, ("h1.product_title.entry-title", "h1.product_title"))
        if title:
            data["title"] = title
            data.setdefault("name", title)

        product_id = self._detail_product_id(tree)
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        sku = self._first_text(tree, ("span.sku", ".sku_wrapper .sku"))
        if sku and sku.upper() not in {"N/A", "NA"}:
            data["sku"] = sku
            data.setdefault("reference", sku)

        specs = self._extract_specs(tree)
        brand = self._first_text(tree, (".wd-product-brands a", ".product_meta .brand a"))
        if not brand:
            brand = self._brand_from_specs(specs)
        if brand:
            data["brand"] = brand

        price, old_price = self._detail_prices(tree)
        if price is not None:
            data["price"] = price
        if old_price is not None:
            data["old_price"] = old_price
        discount_percent = self._discount_percent(data.get("price"), data.get("old_price"))
        if discount_percent is not None:
            data["discount_percent"] = discount_percent

        availability, available = self._detail_availability(tree)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        short_description = self._first_text(
            tree,
            (
                ".woocommerce-product-details__short-description",
                ".summary .woocommerce-product-details__short-description",
            ),
        )
        full_description = self._first_text(
            tree,
            (
                "#tab-description",
                ".woocommerce-Tabs-panel--description",
                ".woocommerce-tabs #tab-description",
            ),
        )
        if short_description:
            data["short_description"] = short_description
            data["overview"] = short_description
        if full_description:
            data["full_description"] = full_description
        best_description = self._best_description(short_description, full_description, data.get("title"))
        if best_description:
            data["description"] = best_description

        if specs:
            data["specifications"] = specs
            data.setdefault("brand", self._brand_from_specs(specs))
            data.setdefault("reference", self._identifier_from_specs(specs, r"r[e\u00e9]f|reference|sku|model|mod[e\u00e8]le"))
            data.setdefault("barcode", self._identifier_from_specs(specs, r"ean|gtin|code\s*bar|barcode"))

        images = self._collect_images(
            tree.css(
                ".woocommerce-product-gallery img, "
                ".woocommerce-product-gallery__image img, "
                "ol.flex-control-thumbs img"
            )
        )
        if images:
            data["images"] = images
            data["image"] = images[0]
        elif data.get("images"):
            images = []
            for image in data.get("images") or []:
                absolute = self._abs_url(image)
                if absolute and not self._is_bad_image(absolute) and absolute not in images:
                    images.append(absolute)
            if images:
                data["images"] = images
                data["image"] = images[0]

        categories = [
            text for text in (self._node_text(node) for node in tree.css(".posted_in a"))
            if text
        ]
        if categories:
            data["categories"] = categories
        breadcrumbs = self._detail_breadcrumbs(tree)
        if breadcrumbs:
            data["breadcrumbs"] = breadcrumbs

        return finalize_product_record(data)


def get_scraper(logger: logging.Logger) -> KamounhomeScraper:
    return KamounhomeScraper(logger)
