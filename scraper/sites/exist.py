#!/usr/bin/env python3
"""
Exist scraper - PrestaShop/Alysum, HTTP/selectolax.
"""

import html as html_lib
import json
import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


class ExistScraper(FastScraper):
    """HTTP scraper for exist.com.tn PrestaShop pages."""

    BAD_CATEGORY_PARTS = (
        "/content/",
        "/module/",
        "/api",
        "/panier",
        "/cart",
        "/commande",
        "/checkout",
        "/connexion",
        "/login",
        "/mon-compte",
        "/my-account",
        "/nous-contacter",
        "/contact",
        "/magasins",
        "/storefinder",
        "/marque",
        "/manufacturer",
        "/recherche",
        "/search",
        "/blog",
        "/promotions",
        "/nouveaux-produits",
        "facebook",
        "instagram",
        "whatsapp",
        "mailto:",
        "tel:",
        "javascript:",
    )

    CATEGORY_RE = re.compile(r"^/(\d+)-[a-z0-9][a-z0-9-]*/?$", re.I)
    PRODUCT_RE = re.compile(r"^(.*/)(\d+)(?:-(\d+))?-([^/]+\.html)$", re.I)
    ROOT_CATEGORY_ID = "2"

    def __init__(self, logger: logging.Logger):
        super().__init__("exist", logger)
        self._category_sitemap_cache: Optional[List[str]] = None

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

    @staticmethod
    def _text(node: Any, separator: str = " ") -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=separator, strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

    @staticmethod
    def _attr(node: Any, name: str) -> Optional[str]:
        if not node:
            return None
        return clean_text(node.attributes.get(name))

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _name_from_url(url: str) -> str:
        slug = urlsplit(url).path.strip("/").split("/")[-1]
        slug = re.sub(r"^\d+-", "", slug)
        slug = re.sub(r"\.html$", "", slug, flags=re.I)
        return re.sub(r"[-_]+", " ", slug).strip().title() or slug

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

    def _sync_headers(self) -> Dict[str, str]:
        headers = dict(self.headers)
        headers["Accept-Encoding"] = "gzip, deflate"
        headers.setdefault("Accept-Language", "fr-FR,fr;q=0.9,en;q=0.8")
        headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        return headers

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        return host == base_host

    def _category_id_from_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        match = self.CATEGORY_RE.match(urlsplit(url).path.rstrip("/") or "/")
        return match.group(1) if match else None

    def _category_url(self, value: Any) -> Optional[str]:
        url = self._absolute_url(value)
        if not url or not self._is_site_url(url):
            return None
        parts = urlsplit(url)
        low = url.lower()
        if any(token in low for token in self.BAD_CATEGORY_PARTS):
            return None
        if parts.path.lower().endswith(".html"):
            return None
        if not self.CATEGORY_RE.match(parts.path.rstrip("/") or "/"):
            return None
        return self._strip_url(url, keep_query=False)

    def _product_url(self, value: Any) -> Optional[str]:
        url = self._absolute_url(value)
        if not url or not self._is_site_url(url):
            return None
        parts = urlsplit(url)
        if not parts.path.lower().endswith(".html"):
            return None
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))

    def _product_ids_from_url(self, url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        if not url:
            return None, None
        match = self.PRODUCT_RE.match(urlsplit(url).path)
        if not match:
            return None, None
        return match.group(2), match.group(3)

    def _node_price(self, node: Any) -> Optional[float]:
        if not node:
            return None
        return parse_price(
            self._attr(node, "content")
            or self._attr(node, "value")
            or self._text(node)
        )

    def _first_text(self, root: Any, selectors: Iterable[str]) -> Optional[str]:
        for selector in selectors:
            node = root.css_first(selector)
            value = self._text(node) if node else None
            if value:
                return value
        return None

    def _first_attr_or_text(self, root: Any, selectors: Iterable[str], attrs: Iterable[str]) -> Optional[str]:
        for selector in selectors:
            node = root.css_first(selector)
            if not node:
                continue
            for attr in attrs:
                value = clean_text(node.attributes.get(attr))
                if value:
                    return value
            value = self._text(node)
            if value:
                return value
        return None

    def _image_from_node(self, node: Any) -> Optional[str]:
        if not node:
            return None
        for attr in ("data-image-large-src", "data-image-medium-src", "data-src", "data-lazy-src", "src"):
            value = self._absolute_url(node.attributes.get(attr))
            if value:
                return value
        srcset = self._first_srcset_url(node.attributes.get("srcset"), prefer_largest=True)
        return self._absolute_url(srcset) if srcset else None

    @staticmethod
    def _dedupe_list(values: Iterable[Any]) -> List[str]:
        seen = set()
        out: List[str] = []
        for value in values:
            text = clean_text(value)
            if not text:
                continue
            key = normalize_url(text) or text
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
        return out

    @staticmethod
    def _extract_id_from_class(class_name: str, pattern: str) -> Optional[str]:
        match = re.search(pattern, class_name or "")
        return match.group(1) if match else None

    @staticmethod
    def _strip_reference_label(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"^\s*r[eé]f(?:[eé]rence)?\s*:?\s*", "", text, flags=re.I)
        return clean_text(text)

    @staticmethod
    def _clean_description(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if "<" in text and ">" in text:
            tree = HTMLParser(text)
            try:
                text = clean_text(tree.body.text(separator=" ", strip=True) if tree.body else tree.text(separator=" ", strip=True))
            except TypeError:
                text = clean_text(tree.text(strip=True))
            if not text:
                return None
        text = re.sub(r"_{2,}[^_]+_{2,}\d+_{2,}", " ", text)
        return clean_text(text)

    @staticmethod
    def _discount_percent(value: Any) -> Optional[int]:
        text = clean_text(value)
        if not text:
            return None
        match = re.search(r"-?\s*(\d+(?:[.,]\d+)?)\s*%", text)
        if not match:
            return None
        parsed = parse_price(match.group(1))
        return int(round(parsed)) if parsed is not None else None

    @staticmethod
    def _computed_discount(price: Optional[float], old_price: Optional[float]) -> Optional[int]:
        if price is None or old_price is None or old_price <= 0 or old_price <= price:
            return None
        return int(round((1 - (price / old_price)) * 100))

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        categories = self._extract_categories_from_sitemap()
        if not categories:
            tree = HTMLParser(html)
            categories = self._extract_categories_from_menu(tree)

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _fetch_category_sitemap(self) -> List[str]:
        if self._category_sitemap_cache is not None:
            return self._category_sitemap_cache

        endpoint = self.selectors.get("frontpage", {}).get(
            "category_sitemap",
            f"{self.base_url.rstrip('/')}/sitemap/category.xml",
        )
        urls: List[str] = []
        try:
            with httpx.Client(headers=self._sync_headers(), follow_redirects=True, timeout=self.request_timeout) as client:
                response = client.get(endpoint)
                response.raise_for_status()
                for match in re.finditer(r"<loc><!\[CDATA\[(.*?)\]\]></loc>|<loc>(.*?)</loc>", response.text):
                    url = self._category_url(match.group(1) or match.group(2))
                    if url and url not in urls:
                        urls.append(url)
        except Exception as exc:
            self.logger.debug(f"Exist category sitemap failed: {exc}")

        self._category_sitemap_cache = urls
        return urls

    def _category_metadata(self, client: httpx.Client, url: str, order: int) -> Dict[str, Any]:
        category_id = self._category_id_from_url(url)
        meta: Dict[str, Any] = {
            "id": category_id,
            "name": self._name_from_url(url),
            "url": url,
            "parent_id": None,
            "depth": None,
            "product_count": 0,
            "order": order,
        }
        try:
            response = client.get(url)
            response.raise_for_status()
            tree = HTMLParser(response.text)
        except Exception as exc:
            self.logger.debug(f"Exist category metadata failed: {url} ({exc})")
            return meta

        body = tree.css_first("body")
        body_class = body.attributes.get("class", "") if body else ""
        meta["id"] = self._extract_id_from_class(body_class, r"(?:^|\s)category-id-(\d+)(?:\s|$)") or category_id
        meta["parent_id"] = self._extract_id_from_class(body_class, r"(?:^|\s)category-id-parent-(\d+)(?:\s|$)")
        meta["depth"] = self._safe_int(self._extract_id_from_class(body_class, r"(?:^|\s)category-depth-level-(\d+)(?:\s|$)"))
        meta["product_count"] = len(tree.css(self.selectors.get("category_page", {}).get("item_selector", "article.product-miniature")))

        name = self._first_text(tree, [".block-category h1", "h1.page-title", "h1"])
        if name:
            meta["name"] = name

        breadcrumbs = []
        for link in tree.css("nav.breadcrumb a[href], .breadcrumb a[href]"):
            crumb_url = self._category_url(self._attr(link, "href"))
            crumb_name = self._text(link)
            if crumb_url and crumb_name and crumb_url != url:
                breadcrumbs.append({"name": crumb_name, "url": crumb_url, "id": self._category_id_from_url(crumb_url)})
        meta["breadcrumbs"] = breadcrumbs
        if not meta.get("parent_id") and breadcrumbs:
            meta["parent_id"] = breadcrumbs[-1].get("id")
        return meta

    def _extract_categories_from_sitemap(self) -> List[Dict[str, Any]]:
        urls = self._fetch_category_sitemap()
        if not urls:
            return []

        nodes: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        try:
            with httpx.Client(headers=self._sync_headers(), follow_redirects=True, timeout=self.request_timeout) as client:
                for index, url in enumerate(urls):
                    meta = self._category_metadata(client, url, index)
                    category_id = meta.get("id") or self._category_id_from_url(url)
                    if not category_id:
                        continue
                    if category_id not in nodes:
                        order.append(category_id)
                    nodes[category_id] = meta
        except Exception as exc:
            self.logger.debug(f"Exist sitemap hierarchy failed: {exc}")

        if not nodes:
            return []

        children: Dict[str, List[str]] = {}
        for category_id, meta in nodes.items():
            parent_id = meta.get("parent_id")
            if parent_id and parent_id in nodes and parent_id != category_id:
                children.setdefault(parent_id, []).append(category_id)

        top_ids = [
            category_id
            for category_id in order
            if not nodes[category_id].get("parent_id")
            or nodes[category_id].get("parent_id") == self.ROOT_CATEGORY_ID
            or nodes[category_id].get("parent_id") not in nodes
        ]

        def make_category(category_id: str, level: str) -> Dict[str, Any]:
            meta = nodes[category_id]
            data = {
                "name": meta.get("name") or self._name_from_url(meta.get("url", "")),
                "url": meta.get("url"),
                "level": level,
            }
            if meta.get("id"):
                data["id"] = meta["id"]
            return data

        categories = []
        for top_id in top_ids:
            top_cat = make_category(top_id, "top")
            top_cat["low_level_categories"] = []
            for low_id in children.get(top_id, []):
                low_cat = make_category(low_id, "low")
                low_cat["subcategories"] = []
                for sub_id in children.get(low_id, []):
                    sub_cat = make_category(sub_id, "subcategory")
                    low_cat["subcategories"].append(sub_cat)
                top_cat["low_level_categories"].append(low_cat)
            categories.append(top_cat)

        return categories

    def _extract_categories_from_menu(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        fp = self.selectors.get("frontpage", {})
        categories: List[Dict[str, Any]] = []
        seen_top = set()

        for block in tree.css(fp.get("menu_blocks", ".ets_mm_megamenu .mm_menus_li")):
            top_link = block.css_first("a[href]")
            top_url = self._category_url(self._attr(top_link, "href"))
            top_name = self._text(top_link) or (self._name_from_url(top_url) if top_url else None)

            low_categories = []
            seen_low = set()
            for link in block.css(fp.get("child_links", ".mm_block_type_category a[href], .mm_columns_ul a[href], ul a[href]")):
                low_url = self._category_url(self._attr(link, "href"))
                low_name = self._text(link) or (self._name_from_url(low_url) if low_url else None)
                if not low_url or not low_name or low_url in seen_low or low_url == top_url:
                    continue
                low_categories.append({"name": low_name, "url": low_url, "level": "low", "subcategories": []})
                seen_low.add(low_url)

            if not top_url and not low_categories:
                continue
            top_key = top_url or top_name
            if not top_key or top_key in seen_top:
                continue
            categories.append(
                {
                    "name": top_name or "Collection",
                    "url": top_url,
                    "level": "top",
                    "low_level_categories": low_categories,
                }
            )
            seen_top.add(top_key)

        if categories:
            return categories

        by_url = {}
        for link in tree.css(fp.get("menu_links", ".ets_mm_megamenu a[href], header a[href], footer a[href]")):
            url = self._category_url(self._attr(link, "href"))
            if not url or url in by_url:
                continue
            by_url[url] = {
                "name": self._text(link) or self._name_from_url(url),
                "url": url,
                "level": "top",
                "low_level_categories": [],
            }
        return list(by_url.values())

    @staticmethod
    def _category_stats(categories: List[Dict[str, Any]]) -> Dict[str, int]:
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
        return stats

    # ------------------------------------------------------------------
    # Listings and pagination
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        selector = self.selectors.get("category_page", {}).get(
            "item_selector",
            "article.product-miniature, article.js-product-miniature, .product-miniature",
        )
        products = []
        for card in tree.css(selector):
            product = self._product_from_card(card)
            if product:
                products.append(product)
        return dedupe_products(products, self.logger, "exist listing")

    def _product_from_card(self, card: Any) -> Optional[Dict[str, Any]]:
        cp = self.selectors.get("category_page", {})
        link = None
        for candidate in card.css(cp.get("item_url", "h3.product-title a[href], .product-title a[href], a[href*='.html']")):
            if self._product_url(self._attr(candidate, "href")):
                link = candidate
                break
        url = self._product_url(self._attr(link, "href"))
        if not url:
            return None

        product_id, attribute_id = self._product_ids_from_url(self._attr(link, "href"))
        product_id = clean_text(card.attributes.get("data-id-product")) or product_id
        attribute_id = clean_text(card.attributes.get("data-id-product-attribute")) or attribute_id

        name = self._text(link)
        if not name:
            name = self._first_text(card, [cp.get("item_name", ".product-title a, .product-title"), "[itemprop='name']"])
        if not name:
            image_node = card.css_first(cp.get("item_image", ".thumbnail-container img, img"))
            name = self._attr(image_node, "alt") or self._name_from_url(url)
        if not name:
            return None

        price = self._node_price(card.css_first(cp.get("item_price", ".price[content], .price")))
        old_price = self._node_price(card.css_first(cp.get("item_old_price", ".regular-price, .product-discount .regular-price")))
        if old_price is not None and price is not None and old_price <= price:
            old_price = None
        discount_percent = self._discount_percent(self._text(card.css_first(cp.get("item_discount", ".discount-percentage, .discount-amount, .discount"))))
        if discount_percent is None:
            discount_percent = self._computed_discount(price, old_price)

        image = None
        for img_node in card.css(cp.get("item_image", ".thumbnail-container img, .product-thumbnail img, img[data-image-large-src], img")):
            image = self._image_from_node(img_node)
            if image:
                break

        class_text = card.attributes.get("class", "")
        availability_source = f"{class_text} {self._text(card) or ''}"
        if "out-of-stock" in class_text:
            availability, available = "Rupture de stock", False
        elif "product-available-for-order" in class_text or "Ajouter au panier" in availability_source:
            availability, available = "En stock", True
        else:
            availability, available = availability_from_text(availability_source)

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "product_attribute_id": attribute_id,
            "url": url,
            "name": name,
            "title": name,
            "shop": self.site_name,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": image,
            "availability": availability,
            "available": available,
        }
        return finalize_product_record({k: v for k, v in record.items() if v is not None})

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1
        total_results = None

        for link in tree.css(cp.get("pagination_pages", ".pagination a[href], .page-list a[href]")):
            href = self._attr(link, "href") or ""
            page = self._safe_int(dict(parse_qsl(urlsplit(href).query)).get("page"))
            text_page = self._safe_int(self._text(link))
            page_num = page or text_page
            if page_num:
                total_pages = max(total_pages, page_num)
                class_name = link.attributes.get("class", "")
                if "disabled" in class_name or "current" in class_name or "active" in class_name:
                    current_page = page_num

        next_href = None
        next_node = tree.css_first(cp.get("pagination_next", "link[rel='next'][href], .pagination a[rel='next'][href], a.next[href]"))
        if next_node:
            next_href = self._attr(next_node, "href")
            next_page = self._safe_int(dict(parse_qsl(urlsplit(next_href or "").query)).get("page"))
            if next_page:
                total_pages = max(total_pages, next_page)

        page_text = self._text(tree.css_first(cp.get("result_count", ".pagination, body"))) or ""
        result_match = re.search(r"Montrer\s+\d+\s*-\s*\d+\s+de\s+(\d+)\s+produits", page_text, re.I)
        if result_match:
            total_results = self._safe_int(result_match.group(1))
            product_count = len(self.extract_products_from_html(html))
            if total_results and product_count:
                total_pages = max(total_pages, math.ceil(total_results / product_count))

        has_next = bool(next_href) or total_pages > current_page
        return {
            "current_page": current_page,
            "total_pages": max(1, total_pages),
            "has_next": has_next,
            "total_results": total_results,
        }

    def build_page_url(self, base_url: str, page_num: int) -> str:
        url = self._absolute_url(base_url) or base_url
        parts = urlsplit(url)
        pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "page"]
        if page_num > 1:
            pairs.append(("page", str(page_num)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        product_url = self._product_url(url) or url
        html = await self.fetch_html(product_url)
        if not html:
            return {"url": product_url, "error": "Failed to fetch product detail"}
        try:
            return self.extract_product_details_from_html(html, product_url)
        except Exception as exc:
            self.logger.debug(f"Exist detail parse failed: {product_url} ({exc})")
            return {"url": product_url, "error": str(exc)}

    def extract_product_details_from_html(self, html: str, product_url: str) -> dict:
        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = html_product_metadata(html, product_url, self.base_url)
        canonical = self._product_url(product_url) or self._product_url(self._first_attr_or_text(tree, ["link[rel='canonical'][href]", "meta[property='og:url']"], ["href", "content"]))
        data["url"] = canonical or product_url

        main = tree.css_first("#main")
        product_id, attribute_id = self._product_ids_from_url(product_url)
        rendered_attribute_id = None
        if main:
            product_id = clean_text(main.attributes.get("data-id_product")) or product_id
            rendered_attribute_id = clean_text(main.attributes.get("data-id_product_attribute"))
            attribute_id = attribute_id or rendered_attribute_id
        product_id = self._attr(tree.css_first("input[name='id_product'][value]"), "value") or product_id
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id
        if attribute_id:
            data["product_attribute_id"] = attribute_id
        if attribute_id and rendered_attribute_id and rendered_attribute_id != attribute_id:
            data.setdefault("data_quality", {})["rendered_product_attribute_id"] = rendered_attribute_id

        title = self._first_attr_or_text(
            tree,
            [pp.get("title", "h1.h1, h1, meta[property='og:title']"), "h1.h1", "h1", "meta[property='og:title']"],
            ["content"],
        )
        if title:
            data["title"] = title
            data["name"] = title

        reference = self._strip_reference_label(
            self._first_attr_or_text(
                tree,
                [pp.get("reference", ".product-reference-top, [itemprop='sku']"), ".product-reference-top", "[itemprop='sku']", "meta[itemprop='sku']"],
                ["content"],
            )
        )
        if reference:
            data["reference"] = reference
            data["sku"] = reference

        product_payload = self._extract_data_product(tree, pp.get("data_product", "#product-details[data-product]"))
        payload_ref = clean_text(product_payload.get("reference"))
        if payload_ref:
            data.setdefault("sku", payload_ref)
            data.setdefault("reference", payload_ref)

        price = self._node_price(tree.css_first(pp.get("price", ".product-prices [itemprop='price'][content], .product-prices .normal-price")))
        if price is not None:
            data["price"] = price
        old_price = self._node_price(tree.css_first(pp.get("old_price", ".product-prices .regular-price, .product-discounts .regular-price")))
        if old_price is not None and data.get("price") is not None and old_price > data["price"]:
            data["old_price"] = old_price
        discount_percent = self._discount_percent(self._text(tree.css_first(pp.get("discount", ".product-prices .discount, .product-discounts .discount"))))
        if discount_percent is None:
            discount_percent = self._computed_discount(data.get("price"), data.get("old_price"))
        if discount_percent is not None:
            data["discount_percent"] = discount_percent

        availability_text = None
        if product_payload:
            quantity = self._safe_int(product_payload.get("quantity"))
            if quantity is not None:
                availability_text = "En stock" if quantity > 0 else "Rupture de stock"
        if not availability_text:
            availability_text = self._first_attr_or_text(
                tree,
                [pp.get("availability", "#product-availability, .product-availability, .product-add-to-cart, [itemprop='availability']")],
                ["href", "content"],
            )
        if not availability_text:
            body = tree.css_first("body")
            body_class = body.attributes.get("class", "") if body else ""
            if "product-available-for-order" in body_class or tree.css_first("button[data-button-action='add-to-cart']"):
                availability_text = "En stock"
        availability, available = availability_from_text(availability_text)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        description = self._clean_description(product_payload.get("description"))
        if not description:
            description = self._clean_description(
                self._first_attr_or_text(
                    tree,
                    [
                        "meta[property='og:description']",
                        "#product-description-short",
                        ".product-information .product-description",
                        ".tabs .product-description",
                        pp.get("description", ".product-description"),
                    ],
                    ["content"],
                )
            )
        if description:
            data["description"] = description
            data["overview"] = description
            data["short_description"] = description

        full_description = self._clean_description(product_payload.get("description"))
        if not full_description:
            full_description = self._clean_description(
                self._first_attr_or_text(
                    tree,
                    [
                        "meta[property='og:description']",
                        "#description .product-description",
                        ".tabs .product-description",
                        pp.get("full_description", ".product-description"),
                    ],
                    ["content"],
                )
            )
        if full_description:
            data["full_description"] = full_description

        specifications = self._extract_specifications(tree, product_payload)
        if specifications:
            data["specifications"] = specifications

        images = []
        for node in tree.css(pp.get("image_gallery", ".images-container img, .product-cover img, .thumb-container img, img[itemprop='image']")):
            image = self._image_from_node(node)
            if image:
                images.append(image)
        images = self._dedupe_list(images)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = []
        for link in tree.css(pp.get("breadcrumbs", "nav.breadcrumb a[href], .breadcrumb a[href]")):
            category_url = self._category_url(self._attr(link, "href"))
            category_name = self._text(link)
            if category_url and category_name:
                categories.append({"name": category_name, "url": category_url})
        if categories:
            seen = set()
            deduped = []
            for category in categories:
                key = normalize_url(category["url"]) or category["url"]
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(category)
            data["categories"] = deduped
            data["breadcrumbs"] = [category["name"] for category in deduped]

        data["shop"] = self.site_name
        return finalize_product_record({k: v for k, v in data.items() if v is not None})

    def _extract_data_product(self, tree: HTMLParser, selector: str) -> Dict[str, Any]:
        node = tree.css_first(selector)
        raw = self._attr(node, "data-product")
        if not raw:
            return {}
        try:
            value = json.loads(html_lib.unescape(raw))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _extract_specifications(self, tree: HTMLParser, product_payload: Dict[str, Any]) -> Dict[str, Any]:
        specs: Dict[str, Any] = {}
        if product_payload:
            for key in ("reference", "category_name", "quantity", "condition"):
                value = clean_text(product_payload.get(key))
                if value:
                    specs[key] = value

        for block in tree.css(self.selectors.get("product_page", {}).get("variants", ".product-variants .product-variants-item")):
            label = self._text(block.css_first(".control-label, label"))
            if not label:
                continue
            values = []
            for node in block.css("input, option, span[title], label, .radio-label, .size-label"):
                value = clean_text(node.attributes.get("title") or node.attributes.get("data-variant-name") or node.text(strip=True))
                if value and value != label and value not in values:
                    values.append(value)
            if values:
                specs[label.rstrip(":")] = ", ".join(values)

        for row in tree.css(self.selectors.get("product_page", {}).get("specs_rows", ".product-features .data-sheet tr, .product-features .data-sheet .row, .product-features .data-sheet div")):
            key = self._text(row.css_first("dt, th, .name, strong"))
            value = self._text(row.css_first("dd, td:last-child, .value, span"))
            if key and value and key != value:
                specs[key.rstrip(":")] = value

        return specs


def get_scraper(logger: logging.Logger) -> ExistScraper:
    """Factory function used by scraper registry."""
    return ExistScraper(logger)
