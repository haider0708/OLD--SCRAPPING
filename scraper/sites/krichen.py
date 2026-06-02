#!/usr/bin/env python3
"""
Krichen Distribution scraper - WordPress/WooCommerce + Woodmart, HTTP/selectolax.
"""

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

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
    normalize_url,
    parse_price,
)


class KrichenScraper(FastScraper):
    """Fresh HTTPX/selectolax scraper for krichen-distribution.tn."""

    BLOCKED_PATH_PARTS = (
        "/boutique/",
        "/mon-compte",
        "/my-account",
        "/panier",
        "/cart",
        "/checkout",
        "/wishlist",
        "/compare",
        "/contact",
        "/blog",
        "/wp-content/",
        "/wp-json/",
        "/feed",
        "/author/",
        "/tag/",
        "/product-tag/",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("krichen", logger)

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
    def _first_srcset_url(value: Optional[str], prefer_largest: bool = False) -> Optional[str]:
        if not value:
            return None
        entries = [chunk.strip() for chunk in value.split(",") if chunk.strip()]
        if not entries:
            return None
        chosen = entries[-1] if prefer_largest else entries[0]
        return chosen.split(" ", 1)[0] if chosen else None

    @staticmethod
    def _direct_link(node: Any) -> Optional[Any]:
        for link in node.css("a[href]"):
            if link.parent == node:
                return link
        return node.css_first("a[href]")

    @staticmethod
    def _post_id_from_class(class_name: str) -> Optional[str]:
        match = re.search(r"(?:^|\s)(?:post|postid)-(\d+)(?:\s|$)", class_name or "")
        return match.group(1) if match else None

    @classmethod
    def _body_post_id(cls, tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        return cls._post_id_from_class(body.attributes.get("class", "") if body else "")

    @staticmethod
    def _type_names(value: Dict[str, Any]) -> List[str]:
        raw = value.get("@type")
        if isinstance(raw, list):
            return [str(item).lower() for item in raw]
        return [str(raw).lower()] if raw is not None else []

    @classmethod
    def _walk_json(cls, value: Any) -> Iterable[Any]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from cls._walk_json(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._walk_json(child)

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        return host == base_host

    def _is_category_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        url = self._absolute_url(url)
        if not url or not self._is_site_url(url):
            return False

        parts = urlsplit(url)
        path = parts.path.rstrip("/") or "/"
        low = url.lower()
        if path == "/" or path == "/boutique":
            return False
        if "." in path.rsplit("/", 1)[-1]:
            return False
        if any(token in low for token in self.BLOCKED_PATH_PARTS):
            return False
        if any(token in low for token in ("?add-to-cart", "mailto:", "tel:", "javascript:", "#")):
            return False
        return True

    def _is_product_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        url = self._absolute_url(url)
        if not url or not self._is_site_url(url):
            return False
        path = urlsplit(url).path.rstrip("/")
        return path.startswith("/boutique/") and path != "/boutique"

    def _html_text(self, value: Any) -> Optional[str]:
        raw = self._clean(value)
        if not raw:
            return None
        tree = HTMLParser(raw)
        try:
            return clean_text(tree.body.text(separator=" ", strip=True) if tree.body else tree.text(separator=" ", strip=True))
        except TypeError:
            return clean_text(tree.text(strip=True))

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories: List[Dict[str, Any]] = []
        seen_urls = set()

        top_links = self._menu_category_links(tree, fp)
        self.logger.info(f"Found {len(top_links)} Krichen top-level category links")

        with httpx.Client(
            headers=self._sync_headers(),
            follow_redirects=True,
            timeout=self.request_timeout,
        ) as client:
            for top_meta in top_links:
                if top_meta["url"] in seen_urls:
                    continue
                seen_urls.add(top_meta["url"])

                top_cat = {
                    "name": top_meta["name"],
                    "url": top_meta["url"],
                    "level": "top",
                    "low_level_categories": [],
                }

                top_html = self._fetch_sync(client, top_meta["url"])
                low_tiles = self._extract_category_tiles(top_html, top_meta["url"], seen_urls) if top_html else []

                for low_meta in low_tiles:
                    low_cat = {
                        "name": low_meta["name"],
                        "url": low_meta["url"],
                        "level": "low",
                        "subcategories": [],
                    }

                    low_html = self._fetch_sync(client, low_meta["url"])
                    sub_seen = set(seen_urls)
                    sub_tiles = self._extract_category_tiles(low_html, low_meta["url"], sub_seen) if low_html else []
                    for sub_meta in sub_tiles:
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

        if not self._has_product_queue(categories):
            categories = self._extract_categories_from_api(top_links) or categories
        if not categories:
            categories = self._extract_categories_from_sitemap()

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _sync_headers(self) -> Dict[str, str]:
        headers = dict(self.headers)
        headers["Accept-Encoding"] = "gzip, deflate"
        return headers

    def _fetch_sync(self, client: httpx.Client, url: str) -> Optional[str]:
        try:
            response = client.get(url)
            if response.status_code >= 400:
                self.logger.debug(f"Category fetch HTTP {response.status_code}: {url}")
                return None
            return response.text if response.text and response.text.strip() else None
        except Exception as exc:
            self.logger.debug(f"Category fetch failed {url}: {exc}")
            return None

    def _menu_category_links(self, tree: HTMLParser, fp: Dict[str, Any]) -> List[Dict[str, str]]:
        candidates: List[Dict[str, str]] = []
        seen = set()

        selectors = [
            fp.get("top_level_items", "ul#menu-main-navigation > li.menu-item"),
            fp.get("mobile_items", ".wd-nav-mobile li.menu-item"),
        ]
        for selector in selectors:
            for li in tree.css(selector):
                link = self._direct_link(li)
                meta = self._category_from_link(link)
                if meta and meta["url"] not in seen:
                    seen.add(meta["url"])
                    candidates.append(meta)

        if candidates:
            return candidates

        for link in tree.css(fp.get("fallback_links", "header a[href], nav a[href]")):
            meta = self._category_from_link(link)
            if meta and meta["url"] not in seen:
                seen.add(meta["url"])
                candidates.append(meta)
        return candidates

    def _category_from_link(self, link: Any, name_override: Optional[str] = None) -> Optional[Dict[str, str]]:
        if not link:
            return None
        url = self._absolute_url(link.attributes.get("href"))
        if url:
            url = self._strip_url(url)
        if not self._is_category_url(url):
            return None

        name = (
            self._clean(name_override)
            or self._clean(link.attributes.get("title"))
            or self._clean(link.css_first(".nav-link-text").text(strip=True) if link.css_first(".nav-link-text") else None)
            or self._clean(link.text(strip=True))
            or self._name_from_url(url)
        )
        if not name or len(name) > 120 or name.lower() in {"accueil", "home", "menu"}:
            return None
        return {"name": name, "url": url}

    def _extract_category_tiles(self, html: str, page_url: str, seen_urls: set) -> List[Dict[str, str]]:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        out: List[Dict[str, str]] = []
        local_seen = set()

        for card in tree.css(fp.get("category_tiles", ".products .product-category, .product-category")):
            title_link = card.css_first(".wd-entities-title a[href], h3.wd-entities-title a[href]")
            fill_link = card.css_first("a.wd-fill[href], a[href]")
            link = title_link or fill_link
            name = self._text(title_link) if title_link else None
            if not name and fill_link:
                aria = fill_link.attributes.get("aria-label")
                match = re.search(r"Product category\s+(.+)", aria or "", flags=re.I)
                name = self._name_from_url(fill_link.attributes.get("href", "")) if not match else match.group(1)
            meta = self._category_from_link(link, name_override=name)
            if not meta:
                continue
            if meta["url"] == self._strip_url(page_url) or meta["url"] in seen_urls or meta["url"] in local_seen:
                continue
            local_seen.add(meta["url"])
            out.append(meta)
        return out

    def _extract_categories_from_api(self, top_links: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        fp = self.selectors.get("frontpage", {})
        endpoint = fp.get("category_api", f"{self.base_url.rstrip('/')}/wp-json/wp/v2/product_cat")
        try:
            response = httpx.get(
                endpoint,
                params={"per_page": 100, "page": 1},
                headers=self._sync_headers(),
                follow_redirects=True,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            items = response.json()
        except Exception as exc:
            self.logger.warning(f"Failed Krichen product_cat API fallback: {exc}")
            return []
        if not isinstance(items, list):
            return []

        by_id: Dict[int, Dict[str, Any]] = {}
        by_url: Dict[str, Dict[str, Any]] = {}
        children: Dict[int, List[Dict[str, Any]]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            url = self._strip_url(item.get("link") or "")
            if not self._is_category_url(url):
                continue
            item_id = self._safe_int(item.get("id"))
            parent_id = self._safe_int(item.get("parent")) or 0
            if item_id is None:
                continue
            normalized = {
                "id": item_id,
                "parent": parent_id,
                "name": self._clean(item.get("name")) or self._name_from_url(url),
                "url": url,
                "count": self._safe_int(item.get("count")) or 0,
            }
            by_id[item_id] = normalized
            by_url[url] = normalized
            children.setdefault(parent_id, []).append(normalized)

        for child_list in children.values():
            child_list.sort(key=lambda row: (row.get("name") or "").lower())

        categories: List[Dict[str, Any]] = []
        seen_roots = set()
        for top in top_links:
            root = by_url.get(top["url"])
            if not root or root["id"] in seen_roots:
                continue
            seen_roots.add(root["id"])
            top_cat = {
                "name": top["name"],
                "url": root["url"],
                "level": "top",
                "category_id": str(root["id"]),
                "product_count_hint": root["count"],
                "low_level_categories": [],
            }
            for low in children.get(root["id"], []):
                if not self._has_products_or_children(low, children):
                    continue
                low_cat = {
                    "name": low["name"],
                    "url": low["url"],
                    "level": "low",
                    "category_id": str(low["id"]),
                    "product_count_hint": low["count"],
                    "subcategories": [],
                }
                for sub in children.get(low["id"], []):
                    if not self._has_products_or_children(sub, children):
                        continue
                    low_cat["subcategories"].append(
                        {
                            "name": sub["name"],
                            "url": sub["url"],
                            "level": "subcategory",
                            "category_id": str(sub["id"]),
                            "product_count_hint": sub["count"],
                        }
                    )
                top_cat["low_level_categories"].append(low_cat)
            categories.append(top_cat)
        return categories

    def _extract_categories_from_sitemap(self) -> List[Dict[str, Any]]:
        sitemap_url = self.selectors.get("frontpage", {}).get(
            "category_sitemap", f"{self.base_url.rstrip('/')}/product_cat-sitemap.xml"
        )
        try:
            response = httpx.get(
                sitemap_url,
                headers=self._sync_headers(),
                follow_redirects=True,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
        except Exception as exc:
            self.logger.warning(f"Failed Krichen category sitemap fallback: {exc}")
            return []

        categories: List[Dict[str, Any]] = []
        seen = set()
        for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", response.text, flags=re.I | re.S):
            url = self._strip_url(html_lib.unescape(loc.strip()))
            if not self._is_category_url(url) or url in seen:
                continue
            seen.add(url)
            categories.append(
                {
                    "name": self._name_from_url(url),
                    "url": url,
                    "level": "top",
                    "low_level_categories": [],
                }
            )
        return categories

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _has_products_or_children(self, item: Dict[str, Any], children: Dict[int, List[Dict[str, Any]]]) -> bool:
        if item.get("count", 0) > 0:
            return True
        return any(self._has_products_or_children(child, children) for child in children.get(item["id"], []))

    @staticmethod
    def _has_product_queue(categories: List[Dict[str, Any]]) -> bool:
        for top in categories:
            lows = top.get("low_level_categories") or []
            if not lows and top.get("url"):
                return True
            for low in lows:
                subs = low.get("subcategories") or []
                if subs or low.get("url"):
                    return True
        return False

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
    def _name_from_url(url: str) -> str:
        slug = urlsplit(url).path.strip("/").split("/")[-1]
        return re.sub(r"[-_]+", " ", slug).strip().title() or slug

    # ------------------------------------------------------------------
    # Listings and pagination
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products: List[Dict[str, Any]] = []

        for card in tree.css(cp.get("item_selector", "div.product.type-product, li.product.type-product")):
            class_name = card.attributes.get("class", "")
            if "product-category" in class_name:
                continue

            url, name = self._listing_link_and_name(card, cp)
            if not url or not name:
                continue

            product_id = self._listing_product_id(card, cp)
            sku = self._listing_sku(card, cp, product_id)
            price = self._extract_price(
                card,
                cp.get("item_current_price", "ins .woocommerce-Price-amount bdi"),
                cp.get("item_price", ".price .woocommerce-Price-amount bdi"),
            )
            old_price_node = card.css_first(cp.get("item_old_price", "del .woocommerce-Price-amount bdi"))
            old_price = parse_price(old_price_node.text(strip=True) if old_price_node else None)
            availability, available = self._availability_from_listing(card)

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "url": url,
                "name": name,
                "price": price,
            }
            if sku:
                product["reference"] = sku
                product["sku"] = sku
            if old_price is not None:
                product["old_price"] = old_price
            discount = self._extract_discount(card, cp, price, old_price)
            if discount is not None:
                product["discount_percent"] = discount
            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available

            brand = self._extract_listing_brand(card, cp)
            if brand:
                product["brand"] = brand

            image = self._extract_listing_image(card, cp)
            if image:
                product["image"] = image

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "krichen listing")

    def _listing_link_and_name(self, card: Any, cp: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        chosen = None
        for link in card.css(cp.get("item_url", ".wd-entities-title a[href*='/boutique/']")):
            href = self._absolute_url(link.attributes.get("href"))
            if not self._is_product_url(href):
                continue
            chosen = link
            if self._clean(link.text(strip=True)):
                break
        if not chosen:
            return None, None
        url = self._strip_url(self._absolute_url(chosen.attributes.get("href")) or "")
        name = self._clean(chosen.text(strip=True))
        if not name:
            img = card.css_first("img[alt], img[title]")
            name = self._clean(img.attributes.get("alt") if img else None) or self._clean(
                img.attributes.get("title") if img else None
            )
        return url, name

    def _listing_product_id(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        product_id = self._post_id_from_class(card.attributes.get("class", ""))
        if product_id:
            return product_id
        node = card.css_first(cp.get("item_id", "[data-product_id]"))
        return self._clean(node.attributes.get("data-product_id") if node else None)

    def _listing_sku(self, card: Any, cp: Dict[str, Any], product_id: Optional[str]) -> Optional[str]:
        node = card.css_first(cp.get("item_sku", "[data-product_sku]"))
        sku = self._clean(node.attributes.get("data-product_sku") if node else None)
        if not sku or sku == product_id:
            return None
        return sku

    def _extract_price(self, root: Any, current_selector: str, fallback_selector: str) -> Optional[float]:
        node = root.css_first(current_selector)
        if node is None:
            node = root.css_first(fallback_selector)
        return parse_price(node.text(strip=True) if node else None)

    def _extract_discount(
        self,
        root: Any,
        cp: Dict[str, Any],
        price: Optional[float],
        old_price: Optional[float],
    ) -> Optional[int]:
        node = root.css_first(cp.get("item_discount", ".onsale.product-label"))
        text = self._clean(node.text(strip=True) if node else None)
        if text:
            match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", text)
            if match:
                return round(float(match.group(1).replace(",", ".")))
        if price and old_price and old_price != price:
            return round((1 - price / old_price) * 100)
        return None

    def _availability_from_listing(self, card: Any) -> Tuple[Optional[str], Optional[bool]]:
        class_name = (card.attributes.get("class") or "").lower()
        if "outofstock" in class_name or "out-of-stock" in class_name:
            return "Rupture de stock", False
        stock_node = card.css_first(".out-of-stock.product-label, .out-of-stock, .stock")
        stock_text = self._text(stock_node)
        if stock_text:
            availability, available = availability_from_text(stock_text)
            if available is None and "sold out" in stock_text.lower():
                return "Sold out", False
            return availability, available
        if "instock" in class_name:
            return "En stock", True
        return None, None

    def _extract_listing_brand(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        node = card.css_first(cp.get("item_brand", ".product-labels [class*='label-attribute-pa_brand']"))
        if not node:
            return None
        brand = self._clean(node.attributes.get("title")) or self._text(node)
        if not brand:
            img = node.css_first("img[title], img[alt]")
            brand = self._clean(img.attributes.get("title") if img else None) or self._clean(
                img.attributes.get("alt") if img else None
            )
        if brand and brand.lower() not in {"sold out"} and not re.search(r"^\-?\d+%", brand):
            return brand
        return None

    def _extract_listing_image(self, card: Any, cp: Dict[str, Any]) -> Optional[str]:
        selector = cp.get(
            "item_image",
            ".product-image-link img.attachment-woocommerce_thumbnail, .product-image-link img.wp-post-image, .product-image-link img",
        )
        attrs = cp.get("item_image_attrs", ["data-large_image", "data-src", "src", "srcset"])
        fallback = None
        for img in card.css(selector):
            if self._is_inside_classes(img, ("product-labels", "attribute-label", "label-attribute-pa_brand")):
                continue
            image = self._image_from_node(img, attrs)
            if not image:
                continue
            if self._looks_like_logo_image(image, img):
                fallback = fallback or image
                continue
            return image
        return fallback

    def _image_from_node(self, img: Any, attrs: List[str], prefer_largest_srcset: bool = False) -> Optional[str]:
        for attr in attrs:
            value = img.attributes.get(attr)
            if attr == "srcset":
                value = self._first_srcset_url(value, prefer_largest=prefer_largest_srcset)
            image = self._absolute_url(value)
            if image and not image.startswith("data:"):
                return image
        return None

    @staticmethod
    def _is_inside_classes(node: Any, tokens: Tuple[str, ...]) -> bool:
        parent = node.parent
        while parent is not None:
            class_name = parent.attributes.get("class", "") if hasattr(parent, "attributes") else ""
            if any(token in class_name for token in tokens):
                return True
            parent = parent.parent
        return False

    @staticmethod
    def _looks_like_logo_image(url: str, img: Any) -> bool:
        low = url.lower()
        if "logo" in low:
            return True
        width = KrichenScraper._safe_int(img.attributes.get("width")) or 0
        height = KrichenScraper._safe_int(img.attributes.get("height")) or 0
        if width and height and width > height * 1.8:
            return True
        return False

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        path = re.sub(r"/page/\d+/?$", "", parts.path or "/").rstrip("/")
        if page_num > 1:
            path = f"{path}/page/{page_num}"
        path = path or "/"
        if path != "/" and not path.endswith("/"):
            path += "/"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1

        current = tree.css_first(".page-numbers.current, span.current")
        if current:
            current_page = self._safe_int(current.text(strip=True)) or current_page
            total_pages = max(total_pages, current_page)

        next_url = None
        for link in tree.css(cp.get("pagination_pages", "a.page-numbers[href], .wd-load-more[href*='/page/']")):
            href = self._absolute_url(link.attributes.get("href"))
            if not href:
                continue
            match = re.search(r"/page/(\d+)/?", urlsplit(href).path)
            if match:
                page_num = int(match.group(1))
                total_pages = max(total_pages, page_num)

        next_link = tree.css_first(cp.get("pagination_next", ".wd-load-more[href*='/page/'], a.next.page-numbers[href]"))
        if next_link:
            next_url = self._absolute_url(next_link.attributes.get("href"))
        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": bool(next_url),
            "next_url": next_url,
        }

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        products: List[dict] = []
        seen_pages = set()
        next_url = category_url
        max_pages = self.config.get("settings", {}).get("max_pagination_pages", 80)

        for _ in range(max_pages):
            page_url = normalize_url(next_url) or next_url
            if page_url in seen_pages:
                break
            seen_pages.add(page_url)

            result = await self.scrape_category_page(next_url)
            if result.get("error"):
                break
            products.extend(result.get("products") or [])
            if limit and len(products) >= limit:
                break

            pagination = result.get("pagination") or {}
            next_candidate = pagination.get("next_url")
            if not next_candidate:
                break
            next_url = next_candidate

        products = dedupe_products(products, self.logger, "krichen all pages")
        return products[:limit] if limit else products

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

        product_json = self._jsonld_product(tree, data["url"])
        if product_json:
            self._apply_jsonld_product(data, product_json)

        product_id = self._detail_product_id(tree, pp)
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        title_node = tree.css_first(pp.get("title", "h1.product_title, h1.entry-title"))
        title = self._clean_title(self._text(title_node))
        if title:
            data["title"] = title

        sku_node = tree.css_first(pp.get("sku", ".sku_wrapper .sku, span.sku"))
        sku = self._clean(sku_node.text(strip=True) if sku_node else None)
        if self._meaningful_sku(sku, product_id):
            data["reference"] = sku
            data["sku"] = sku

        price = self._detail_price(tree, pp)
        if price is not None:
            data["price"] = price

        old_price = self._detail_old_price(tree, pp)
        if old_price is not None:
            data["old_price"] = old_price
            if data.get("price") and old_price != data["price"]:
                data["discount_percent"] = round((1 - data["price"] / old_price) * 100)

        availability, available = self._detail_availability(tree, pp, data)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        description = self._detail_description(tree, pp, data)
        if description:
            data["description"] = description

        specs = self._extract_detail_specs(tree, pp)
        if specs:
            data["specifications"] = specs
            brand = self._brand_from_specs(specs)
            if brand:
                data["brand"] = brand

        brand = self._detail_brand(tree, pp, data)
        if brand:
            data["brand"] = brand

        images = self._detail_images(tree, pp, data)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = self._detail_categories(tree, pp, product_json)
        if categories:
            data["categories"] = categories

        return finalize_product_record(data)

    def _detail_product_id(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("product_id", "button[name='add-to-cart'][value]"))
        if node:
            for attr in ("value", "data-product_id", "data-product-id"):
                product_id = self._clean(node.attributes.get(attr))
                if product_id:
                    return product_id
        return self._body_post_id(tree)

    @staticmethod
    def _meaningful_sku(sku: Optional[str], product_id: Optional[str]) -> bool:
        if not sku:
            return False
        if sku.upper() in {"N/A", "NA", "SKU", "ND"}:
            return False
        if product_id and sku == product_id:
            return False
        return True

    @staticmethod
    def _clean_title(value: Any) -> Optional[str]:
        title = clean_text(value)
        if not title:
            return None
        title = re.sub(r"\s*[–-]\s*Prix Tunisie\s*\|\s*Krichen Distribution\s*$", "", title, flags=re.I)
        title = re.sub(r"\s*\|\s*Krichen Distribution\s*$", "", title, flags=re.I)
        return clean_text(title)

    def _detail_price(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[float]:
        return self._extract_price(
            tree,
            pp.get("current_price", "p.price ins .woocommerce-Price-amount bdi"),
            pp.get("price", "p.price .woocommerce-Price-amount bdi"),
        )

    def _detail_old_price(self, tree: HTMLParser, pp: Dict[str, Any]) -> Optional[float]:
        node = tree.css_first(pp.get("old_price", "p.price del .woocommerce-Price-amount bdi"))
        return parse_price(node.text(strip=True) if node else None)

    def _detail_availability(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        data: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[bool]]:
        node = tree.css_first(
            pp.get(
                "availability",
                ".summary .stock, .entry-summary .stock, .product-image-summary .stock, form.cart .stock",
            )
        )
        if node:
            text = self._text(node)
            availability, available = availability_from_text(text)
            if available is None and text and "sold out" in text.lower():
                return text, False
            return availability, available

        if data.get("availability"):
            return availability_from_text(data.get("availability"))

        add_to_cart = tree.css_first("button[name='add-to-cart'], .single_add_to_cart_button")
        if add_to_cart:
            classes = add_to_cart.attributes.get("class", "").lower()
            if "disabled" not in classes and "disabled" not in add_to_cart.attributes:
                return "En stock", True

        body = tree.css_first("body")
        class_name = (body.attributes.get("class", "") if body else "").lower()
        if "outofstock" in class_name:
            return "Rupture de stock", False
        if "instock" in class_name:
            return "En stock", True
        return None, None

    def _detail_description(self, tree: HTMLParser, pp: Dict[str, Any], data: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("description", ".woocommerce-product-details__short-description, #tab-description"))
        description = self._text(node, separator="\n") if node else None
        return clean_text(description or data.get("description"))

    def _extract_detail_specs(self, tree: HTMLParser, pp: Dict[str, Any]) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for row in tree.css(pp.get("specs_rows", "table.shop_attributes tr, table.woocommerce-product-attributes tr")):
            key_node = row.css_first("th, .woocommerce-product-attributes-item__label")
            value_node = row.css_first("td, .woocommerce-product-attributes-item__value")
            key = self._text(key_node)
            value = self._text(value_node)
            if key and value:
                specs[key] = value
        return specs

    @staticmethod
    def _brand_from_specs(specs: Dict[str, str]) -> Optional[str]:
        for key, value in specs.items():
            if key and key.lower() in {"brand", "marque"} and value:
                return clean_text(value)
        return None

    def _detail_brand(self, tree: HTMLParser, pp: Dict[str, Any], data: Dict[str, Any]) -> Optional[str]:
        node = tree.css_first(pp.get("brand", ".wd-product-brands a, .product-brands a, .brand a"))
        brand = self._clean(node.text(strip=True) if node else None)
        if brand:
            return brand
        href = node.attributes.get("href") if node else None
        if href:
            query = parse_qs(urlsplit(href).query)
            raw = (query.get("filter_brand") or [None])[0]
            if raw:
                return self._name_from_url(raw)
        return self._clean(data.get("brand"))

    def _detail_images(self, tree: HTMLParser, pp: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        selector = pp.get("image_gallery", ".woocommerce-product-gallery__image img, .woocommerce-product-gallery img, img.wp-post-image")
        for img in tree.css(selector):
            image = self._image_from_node(
                img,
                ["data-large_image", "data-src", "srcset", "src"],
                prefer_largest_srcset=True,
            )
            if not image or image in images:
                continue
            if self._looks_like_logo_image(image, img) and "wp-post-image" not in img.attributes.get("class", ""):
                continue
            images.append(image)

        for image in data.get("images") or []:
            image_url = image.get("url") if isinstance(image, dict) else image
            image_url = self._absolute_url(image_url)
            if image_url and image_url not in images:
                images.append(image_url)
        return images[:20]

    def _detail_categories(self, tree: HTMLParser, pp: Dict[str, Any], product_json: Dict[str, Any]) -> List[str]:
        categories: List[str] = []

        raw_category = product_json.get("category") if product_json else None
        if isinstance(raw_category, str):
            for part in re.split(r"\s*>\s*", raw_category):
                name = self._clean(part)
                if name and name not in categories:
                    categories.append(name)

        for link in tree.css(pp.get("breadcrumbs", ".woocommerce-breadcrumb a, .breadcrumb a")):
            name = self._clean(link.text(strip=True))
            href = self._absolute_url(link.attributes.get("href"))
            if not name or name.lower() in {"accueil", "home"}:
                continue
            if href and self._is_category_url(href) and name not in categories:
                categories.append(name)
        return categories

    def _jsonld_product(self, tree: HTMLParser, product_url: Optional[str]) -> Dict[str, Any]:
        target = normalize_url(product_url)
        products: List[Dict[str, Any]] = []
        for script in tree.css("script[type='application/ld+json']"):
            raw = script.text()
            if not raw:
                continue
            try:
                parsed = json.loads(html_lib.unescape(raw.strip()))
            except (TypeError, json.JSONDecodeError):
                continue
            for obj in self._walk_json(parsed):
                if isinstance(obj, dict) and "product" in self._type_names(obj):
                    products.append(obj)
        if not products:
            return {}
        if target:
            for product in products:
                for candidate in self._jsonld_urls(product):
                    if normalize_url(candidate) == target:
                        return product
        return products[0]

    @staticmethod
    def _jsonld_urls(product: Dict[str, Any]) -> List[str]:
        urls: List[str] = []
        for key in ("url", "@id"):
            if isinstance(product.get(key), str):
                urls.append(product[key])
        main_page = product.get("mainEntityOfPage")
        if isinstance(main_page, dict):
            for key in ("url", "@id"):
                if isinstance(main_page.get(key), str):
                    urls.append(main_page[key])
        offers = product.get("offers")
        offer_items = offers if isinstance(offers, list) else [offers]
        for offer in offer_items:
            if isinstance(offer, dict) and isinstance(offer.get("url"), str):
                urls.append(offer["url"])
        return urls

    def _apply_jsonld_product(self, data: Dict[str, Any], product: Dict[str, Any]) -> None:
        title = self._clean_title(product.get("name"))
        if title:
            data.setdefault("title", title)

        description = self._clean(product.get("description"))
        if description:
            data.setdefault("description", description)

        sku = self._clean(product.get("sku"))
        if sku:
            data.setdefault("reference", sku)
            data.setdefault("sku", sku)

        category = self._clean(product.get("category"))
        if category:
            data.setdefault("categories", [part for part in re.split(r"\s*>\s*", category) if part])


def get_scraper(logger: logging.Logger) -> KrichenScraper:
    return KrichenScraper(logger)
