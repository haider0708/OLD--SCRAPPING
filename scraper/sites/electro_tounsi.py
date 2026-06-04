#!/usr/bin/env python3
"""
Electro Tounsi scraper - WordPress/WooCommerce + Woodmart, HTTP/selectolax.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, save_text_atomic
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    clean_text,
    dedupe_products,
    finalize_product_record,
    html_product_metadata,
    normalize_gtin,
    normalize_url,
    parse_price,
)


class ElectroTounsiScraper(FastScraper):
    """HTTP scraper for et.com.tn."""

    CATEGORY_PATH_PREFIX = "/categorie-produit/"
    BLOCKED_CATEGORY_PARTS = (
        "/mon-compte",
        "/my-account",
        "/panier",
        "/cart",
        "/checkout",
        "/wishlist",
        "/compare",
        "/contact",
        "/blog",
        "/actualite",
        "/wp-content/",
        "/wp-json/",
        "/feed",
        "/author/",
        "/tag/",
        "/product-tag/",
        "/marque/",
        "/brand/",
        "/page/",
    )
    LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)

    def __init__(self, logger: logging.Logger):
        super().__init__("electro_tounsi", logger)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, href: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(href, base_url or self.base_url)

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
    def _first_srcset_url(value: Optional[str], prefer_largest: bool = False) -> Optional[str]:
        if not value:
            return None
        entries = [chunk.strip() for chunk in value.split(",") if chunk.strip()]
        if not entries:
            return None
        chosen = entries[-1] if prefer_largest else entries[0]
        return chosen.split(" ", 1)[0] if chosen else None

    @staticmethod
    def _post_id_from_class(class_name: str) -> Optional[str]:
        match = re.search(r"(?:^|\s)(?:post|postid)-(\d+)(?:\s|$)", class_name or "")
        return match.group(1) if match else None

    @classmethod
    def _body_post_id(cls, tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        return cls._post_id_from_class(body.attributes.get("class", "") if body else "")

    @staticmethod
    def _direct_link(node: Any) -> Optional[Any]:
        for link in node.css("a[href]"):
            if link.parent == node:
                return link
        return node.css_first("a[href]")

    @staticmethod
    def _menu_level(node: Any) -> Optional[int]:
        class_name = node.attributes.get("class", "") if node else ""
        match = re.search(r"(?:^|\s)item-level-(\d+)(?:\s|$)", class_name)
        return int(match.group(1)) if match else None

    @staticmethod
    def _name_from_url(url: str) -> str:
        slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        return re.sub(r"[-_]+", " ", slug).strip().title() or slug

    @staticmethod
    def _discount_percent(price: Optional[float], old_price: Optional[float]) -> Optional[float]:
        if price is None or old_price is None or old_price <= 0 or price >= old_price:
            return None
        return round(((old_price - price) / old_price) * 100, 2)

    def _strip_url(self, url: Optional[str], keep_query: bool = False) -> Optional[str]:
        if not url:
            return None
        abs_url = self._absolute_url(url)
        if not abs_url:
            return None
        parts = urlsplit(abs_url)
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

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        return host == base_host

    def _is_category_url(self, url: Optional[str]) -> bool:
        url = self._strip_url(url)
        if not url or not self._is_site_url(url):
            return False
        parts = urlsplit(url)
        path = parts.path.rstrip("/") or "/"
        low = url.lower()
        if not path.startswith(self.CATEGORY_PATH_PREFIX):
            return False
        if path == self.CATEGORY_PATH_PREFIX.rstrip("/"):
            return False
        if "." in path.rsplit("/", 1)[-1]:
            return False
        if any(token in low for token in self.BLOCKED_CATEGORY_PARTS):
            return False
        if any(token in low for token in ("?add-to-cart", "mailto:", "tel:", "javascript:", "#")):
            return False
        if "non-classe" in low or "uncategorized" in low:
            return False
        return True

    def _node_href_name(self, node: Any) -> Tuple[Optional[str], Optional[str]]:
        link = self._direct_link(node)
        if not link:
            return None, None
        url = self._strip_url(link.attributes.get("href"))
        if not self._is_category_url(url):
            return None, None
        name_node = link.css_first("span.nav-link-text") or link
        name = self._text(name_node) or self._name_from_url(url)
        return url, name

    def _image_from_node(
        self,
        root: Any,
        selector: str,
        attrs: Iterable[str],
        *,
        prefer_largest_srcset: bool = False,
    ) -> Optional[str]:
        image = root.css_first(selector)
        if not image:
            return None
        for attr in attrs:
            candidate = image.attributes.get(attr)
            if attr == "srcset":
                candidate = self._first_srcset_url(candidate, prefer_largest=prefer_largest_srcset)
            url = self._absolute_url(candidate)
            if not url:
                continue
            low = url.lower()
            if "logo" in low or "placeholder" in low or low.startswith("data:"):
                continue
            return url
        return None

    # ------------------------------------------------------------------
    # Frontpage/category discovery
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"Downloading Electro Tounsi frontpage: {self.base_url}")
        html = await self.fetch_html(self.base_url, raise_on_error=True)
        save_text_atomic(html, output_path, self.logger)

        sitemap_urls = list(self.selectors.get("frontpage", {}).get("sitemaps", []))
        discovered_sitemaps: List[str] = []
        for sitemap_url in sitemap_urls:
            meta = await self.fetch_html_with_meta(sitemap_url)
            sitemap = meta.get("html")
            if not sitemap:
                self.logger.debug(f"Failed Electro Tounsi sitemap fetch: {sitemap_url} ({meta.get('error')})")
                continue

            final_url = meta.get("final_url") or sitemap_url
            filename = urlsplit(final_url).path.rstrip("/").rsplit("/", 1)[-1] or "sitemap.xml"
            if not filename.endswith(".xml"):
                filename = "sitemap.xml"
            save_text_atomic(sitemap, self.html_dir / filename, self.logger)

            for loc in self._locs_from_sitemap_text(sitemap):
                if "product_cat-sitemap" in loc and loc not in sitemap_urls and loc not in discovered_sitemaps:
                    discovered_sitemaps.append(loc)

        for sitemap_url in discovered_sitemaps:
            meta = await self.fetch_html_with_meta(sitemap_url)
            sitemap = meta.get("html")
            if not sitemap:
                continue
            filename = urlsplit(meta.get("final_url") or sitemap_url).path.rstrip("/").rsplit("/", 1)[-1]
            save_text_atomic(sitemap, self.html_dir / (filename or "product_cat-sitemap.xml"), self.logger)

        return output_path

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = self._extract_categories_from_menu(tree)
        if not categories:
            categories = self._extract_categories_from_fallback_links(tree)
        if not categories:
            categories = self._extract_categories_from_sitemap()

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _extract_categories_from_menu(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        selector = self.selectors.get("frontpage", {}).get(
            "top_level_items", "ul.wd-nav-vertical > li.menu-item"
        )
        categories: List[Dict[str, Any]] = []
        seen_urls = set()

        for top_li in tree.css(selector):
            top_url, top_name = self._node_href_name(top_li)
            if not top_url or not top_name or top_url in seen_urls:
                continue
            seen_urls.add(top_url)

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            low_nodes = [node for node in top_li.css("li.menu-item") if self._menu_level(node) == 1]
            if not low_nodes:
                low_nodes = top_li.css("ul.sub-menu > li.menu-item, ul.wd-sub-menu > li.menu-item")

            low_seen = set()
            for low_li in low_nodes:
                low_url, low_name = self._node_href_name(low_li)
                if not low_url or not low_name or low_url in seen_urls or low_url in low_seen:
                    continue
                low_seen.add(low_url)
                seen_urls.add(low_url)

                low_cat = {
                    "name": low_name,
                    "url": low_url,
                    "level": "low",
                    "subcategories": [],
                }

                sub_seen = set()
                sub_nodes = [
                    node
                    for node in low_li.css("li.menu-item")
                    if (self._menu_level(node) or 0) >= 2
                ]
                if not sub_nodes:
                    sub_nodes = low_li.css("ul.sub-menu li.menu-item, ul.wd-sub-menu li.menu-item")

                for sub_li in sub_nodes:
                    sub_url, sub_name = self._node_href_name(sub_li)
                    if not sub_url or not sub_name:
                        continue
                    if sub_url in seen_urls or sub_url in low_seen or sub_url in sub_seen:
                        continue
                    sub_seen.add(sub_url)
                    seen_urls.add(sub_url)
                    low_cat["subcategories"].append(
                        {
                            "name": sub_name,
                            "url": sub_url,
                            "level": "subcategory",
                        }
                    )

                top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        return categories

    def _extract_categories_from_fallback_links(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        selector = self.selectors.get("frontpage", {}).get(
            "fallback_links",
            "header a[href], nav a[href], footer a[href]",
        )
        links: List[Tuple[str, str]] = []
        seen = set()
        for link in tree.css(selector):
            url = self._strip_url(link.attributes.get("href"))
            if not self._is_category_url(url) or url in seen:
                continue
            seen.add(url)
            name = self._text(link.css_first("span.nav-link-text") or link) or self._name_from_url(url)
            links.append((url, name))
        return self._flat_categories(links)

    def _extract_categories_from_sitemap(self) -> List[Dict[str, Any]]:
        links: List[Tuple[str, str]] = []
        seen = set()
        for path in self.html_dir.glob("*sitemap*.xml"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for loc in self._locs_from_sitemap_text(text):
                url = self._strip_url(loc)
                if not self._is_category_url(url) or url in seen:
                    continue
                seen.add(url)
                links.append((url, self._name_from_url(url)))
        return self._flat_categories(links)

    def _locs_from_sitemap_text(self, text: str) -> List[str]:
        return [html_lib.unescape(match.strip()) for match in self.LOC_RE.findall(text or "")]

    @staticmethod
    def _flat_categories(links: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
        return [
            {
                "name": name,
                "url": url,
                "level": "top",
                "low_level_categories": [],
            }
            for url, name in links
        ]

    @staticmethod
    def _category_stats(categories: List[Dict[str, Any]]) -> Dict[str, int]:
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["top_level"] += 1
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top.get("low_level_categories", []) or []:
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1
                for sub in low.get("subcategories", []) or []:
                    stats["subcategory"] += 1
                    if sub.get("url"):
                        stats["total_urls"] += 1
        return stats

    # ------------------------------------------------------------------
    # Product listings
    # ------------------------------------------------------------------

    async def scrape_category_page(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {
                "products": [],
                "pagination": {"current_page": 1, "total_pages": 1, "has_next": False},
                "error": "Failed to fetch",
            }

        sample_path = self.html_dir / "listing_sample_1.html"
        if not sample_path.exists() and self._is_category_url(url):
            save_text_atomic(html, sample_path, self.logger)

        try:
            products = self.extract_products_from_html(html)
            pagination = self.extract_pagination_from_html(html)
        except Exception as exc:
            self.logger.debug(f"Error parsing {url}: {exc}")
            return {
                "products": [],
                "pagination": {"current_page": 1, "total_pages": 1, "has_next": False},
                "error": str(exc),
            }
        return {"products": products, "pagination": pagination}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        item_selector = cp.get(
            "item_selector",
            ".products div.wd-product.product-grid-item, .products div.wd-product.product",
        )
        cards = tree.css(item_selector)
        if not cards:
            cards = tree.css(".products .product")

        products: List[Dict[str, Any]] = []
        for card in cards:
            product = self._product_from_card(card, cp)
            if product:
                products.append(product)

        return dedupe_products(products, self.logger, context=self.site_name)

    def _product_from_card(self, card: Any, selectors: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        class_name = card.attributes.get("class", "")
        product_id = self._post_id_from_class(class_name)

        id_node = card.css_first(selectors.get("item_id", "[data-product_id], [data-id]"))
        if id_node:
            product_id = (
                id_node.attributes.get("data-product_id")
                or id_node.attributes.get("data-id")
                or id_node.attributes.get("value")
                or product_id
            )
        product_id = self._clean(product_id)

        link = card.css_first(selectors.get("item_url", ".wd-entities-title a[href], h3 a[href], h2 a[href]"))
        if not link:
            return None
        url = self._strip_url(link.attributes.get("href"))
        name = self._text(link)
        if not url or not name:
            return None

        current_el = card.css_first(selectors.get("item_current_price", "span.price ins bdi"))
        old_el = card.css_first(selectors.get("item_old_price", "span.price del bdi"))
        price_el = card.css_first(selectors.get("item_price", "span.price bdi, span.price"))
        price = parse_price(self._text(current_el) if current_el else self._text(price_el))
        old_price = parse_price(self._text(old_el)) if old_el else None

        discount_text = self._text(card.css_first(selectors.get("item_discount", ".onsale")))
        discount_percent = self._discount_percent(price, old_price)
        if discount_percent is None and discount_text:
            match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", discount_text)
            if match:
                discount_percent = parse_price(match.group(1))

        image = self._image_from_node(
            card,
            selectors.get("item_image", "a.product-image-link img, img.wp-post-image, img"),
            selectors.get("item_image_attrs", ["data-large_image", "data-src", "src", "srcset"]),
        )

        sku = None
        sku_node = card.css_first(selectors.get("item_sku", "[data-product_sku]"))
        if sku_node:
            sku = self._clean(sku_node.attributes.get("data-product_sku"))

        availability_node = card.css_first(selectors.get("item_availability", ".wd-product-stock, .stock"))
        availability_text = self._text(availability_node)
        availability, available = availability_from_text(availability_text or class_name)

        product: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": url,
            "name": name,
            "title": name,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": image,
            "sku": sku,
            "reference": sku,
            "availability": availability,
            "available": available,
            "shop": self.site_name,
        }

        return finalize_product_record({k: v for k, v in product.items() if v is not None})

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        base = self._strip_url(base_url) or base_url
        base = re.sub(r"/page/\d+/?$", "", base.rstrip("/"))
        if page_num <= 1:
            return base
        return f"{base}/page/{page_num}/"

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1

        current_el = tree.css_first(cp.get("pagination_current", "ul.page-numbers span.current"))
        if current_el:
            try:
                current_page = int(self._text(current_el) or "1")
                total_pages = max(total_pages, current_page)
            except ValueError:
                current_page = 1

        for link in tree.css(cp.get("pagination_pages", "a.page-numbers[href]")):
            text = self._text(link) or ""
            href = link.attributes.get("href") or ""
            candidates = [text] + re.findall(r"/page/(\d+)/?", href)
            for candidate in candidates:
                try:
                    total_pages = max(total_pages, int(candidate))
                except (TypeError, ValueError):
                    continue

        next_node = tree.css_first(cp.get("pagination_next", "a.next.page-numbers[href], link[rel='next']"))
        has_next = next_node is not None
        max_pages = int(self.config.get("settings", {}).get("max_pagination_pages", 80))
        total_pages = min(max(total_pages, current_page), max_pages)
        if current_page >= total_pages:
            has_next = False

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    # ------------------------------------------------------------------
    # Product detail
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        sample_path = self.html_dir / "detail_sample_1.html"
        if not sample_path.exists():
            save_text_atomic(html, sample_path, self.logger)

        tree = HTMLParser(html)
        data: Dict[str, Any] = html_product_metadata(html, url, self.base_url)
        data["url"] = self._strip_url(data.get("url") or url) or url
        data["shop"] = self.site_name

        self._apply_detail_identifiers(tree, data)
        self._apply_detail_title_price(tree, data)
        self._apply_detail_availability(tree, data)
        self._apply_detail_descriptions(tree, data)
        self._apply_detail_specs(tree, data)
        self._apply_detail_images(tree, data)
        self._apply_detail_categories(tree, data)

        return finalize_product_record({k: v for k, v in data.items() if v is not None})

    def _apply_detail_identifiers(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        product_id = self._body_post_id(tree)
        product_id_node = tree.css_first(pp.get("product_id", "button[name='add-to-cart'][value]"))
        if product_id_node:
            product_id = product_id_node.attributes.get("value") or product_id
        if product_id:
            data["product_id"] = self._clean(product_id)
            data.setdefault("id", self._clean(product_id))

        sku_node = tree.css_first(pp.get("sku", ".sku_wrapper .sku, span.sku"))
        sku = self._text(sku_node)
        if sku and sku.upper() != "N/A":
            gtin = normalize_gtin(sku)
            if gtin:
                data.setdefault("barcode", gtin)
            else:
                data["reference"] = sku
                data["sku"] = sku

        brand = self._text(tree.css_first(pp.get("brand", ".product-brands a, .brand a")))
        if not brand:
            brand_img = tree.css_first(pp.get("brand_image", ".product-brands img, .brand img"))
            brand = self._clean(brand_img.attributes.get("alt")) if brand_img else None
        if brand:
            data["brand"] = brand

    def _apply_detail_title_price(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        title = self._text(tree.css_first(pp.get("title", "h1.product_title, h1")))
        if title:
            data["title"] = title
            data.setdefault("name", title)

        current_el = tree.css_first(pp.get("current_price", "p.price ins bdi"))
        old_el = tree.css_first(pp.get("old_price", "p.price del bdi"))
        price_el = tree.css_first(pp.get("price", "p.price bdi, p.price"))
        price = parse_price(self._text(current_el) if current_el else self._text(price_el))
        old_price = parse_price(self._text(old_el)) if old_el else None
        if price is not None:
            data["price"] = price
        if old_price is not None:
            data["old_price"] = old_price
        discount = self._discount_percent(data.get("price"), data.get("old_price"))
        if discount is not None:
            data["discount_percent"] = discount

    def _apply_detail_availability(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        availability_node = tree.css_first(pp.get("availability", ".summary .stock, p.stock"))
        availability_text = self._text(availability_node)
        availability, available = availability_from_text(availability_text)
        if availability_text and availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available
            return

        body = tree.css_first("body")
        body_classes = body.attributes.get("class", "") if body else ""
        body_availability, body_available = availability_from_text(body_classes)
        if body_available is not None:
            data["availability"] = body_availability or ("En stock" if body_available else "Rupture de stock")
            data["available"] = body_available

    def _apply_detail_descriptions(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        short_description = self._text(tree.css_first(pp.get("short_description", ".woocommerce-product-details__short-description")))
        description = self._text(tree.css_first(pp.get("description", "#tab-description, .woocommerce-Tabs-panel--description")))
        if short_description:
            data["short_description"] = short_description
        if description:
            data["description"] = description
        elif short_description:
            data.setdefault("description", short_description)

    def _apply_detail_specs(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        specs: Dict[str, str] = {}
        for row in tree.css(pp.get("specs_rows", "table.shop_attributes tr")):
            key = self._text(row.css_first("th, td:first-child"))
            value = self._text(row.css_first("td:last-child"))
            if not key or not value or key == value:
                continue
            specs[key] = value
            if re.search(r"brand|marque", key, re.I):
                data.setdefault("brand", value)
            elif re.search(r"ean|gtin|code\s*bar|barcode", key, re.I):
                gtin = normalize_gtin(value)
                if gtin:
                    data.setdefault("barcode", gtin)
            elif re.search(r"sku|mpn|model|reference|ref", key, re.I):
                if normalize_gtin(value):
                    data.setdefault("barcode", value)
                else:
                    data.setdefault("reference", value)

        if specs:
            data["specifications"] = specs

    def _apply_detail_images(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        images: List[str] = []
        seen = set()
        for img in tree.css(pp.get("image_gallery", "img.wp-post-image, .woocommerce-product-gallery img")):
            for attr in ("data-large_image", "data-src", "src", "srcset"):
                candidate = img.attributes.get(attr)
                if attr == "srcset":
                    candidate = self._first_srcset_url(candidate, prefer_largest=True)
                url = self._absolute_url(candidate)
                if not url:
                    continue
                low = url.lower()
                if "logo" in low or "placeholder" in low or low.startswith("data:"):
                    continue
                norm = normalize_url(url) or url
                if norm in seen:
                    continue
                seen.add(norm)
                images.append(url)
                break

        if images:
            data["images"] = images
            data["image"] = images[0]

    def _apply_detail_categories(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})
        categories: List[str] = []
        category_urls: List[str] = []

        for link in tree.css(pp.get("breadcrumbs", ".woocommerce-breadcrumb a, .breadcrumb a")):
            name = self._text(link)
            href = self._strip_url(link.attributes.get("href"))
            if name and name.lower() not in {"accueil", "home"} and name not in categories:
                categories.append(name)
            if href and self._is_category_url(href) and href not in category_urls:
                category_urls.append(href)

        for link in tree.css(".posted_in a[href], .product_meta a[href*='/categorie-produit/']"):
            name = self._text(link)
            href = self._strip_url(link.attributes.get("href"))
            if name and name not in categories:
                categories.append(name)
            if href and self._is_category_url(href) and href not in category_urls:
                category_urls.append(href)

        if categories:
            data["categories"] = categories
            data["breadcrumbs"] = categories
        if category_urls:
            data["category_urls"] = category_urls


def get_scraper(logger: logging.Logger) -> ElectroTounsiScraper:
    return ElectroTounsiScraper(logger)
