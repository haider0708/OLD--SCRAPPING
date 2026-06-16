#!/usr/bin/env python3
"""
Praga scraper - PrestaShop, HTTP/selectolax.
"""

import asyncio
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


class PragaScraper(FastScraper):
    """HTTP scraper for praga.tn PrestaShop pages."""

    ROOT_CATEGORY_ID = "2"
    CATEGORY_RE = re.compile(r"^/(\d+)-[a-z0-9][a-z0-9-]*/?$", re.I)
    PRODUCT_RE = re.compile(r"/(\d+)-[^/]+\.html$", re.I)
    BAD_CATEGORY_PARTS = (
        "/2-categories",
        "/brand/",
        "/marques",
        "/contact",
        "/contact-form/",
        "/content/",
        "/module/",
        "/panier",
        "/cart",
        "/commande",
        "/checkout",
        "/connexion",
        "/login",
        "/mon-compte",
        "/my-account",
        "/recherche",
        "/search",
        "/blog",
        "/magasins",
        "/stores",
        "/promotions",
        "/meilleures-ventes",
        "/nouveaux-produits",
        "facebook",
        "instagram",
        "tiktok",
        "youtube",
        "whatsapp",
        "google.com/maps",
        "mailto:",
        "tel:",
        "javascript:",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("praga", logger)
        self._sitemap_category_cache: Optional[List[str]] = None

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
    def _strip_url(url: Any, keep_query: bool = False) -> str:
        text = clean_text(url) or ""
        parts = urlsplit(text)
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
    def _name_from_url(url: str) -> str:
        slug = urlsplit(url).path.strip("/").split("/")[-1]
        slug = re.sub(r"^\d+-", "", slug)
        slug = re.sub(r"\.html$", "", slug, flags=re.I)
        return re.sub(r"[-_]+", " ", slug).strip().title() or slug

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
        return text

    @staticmethod
    def _strip_reference_label(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"^\s*r[eé]f(?:[eé]rence)?\s*:?\s*", "", text, flags=re.I)
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

    @staticmethod
    def _walk_json(value: Any) -> Iterable[Any]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from PragaScraper._walk_json(child)
        elif isinstance(value, list):
            for item in value:
                yield from PragaScraper._walk_json(item)

    @staticmethod
    def _type_names(value: Dict[str, Any]) -> List[str]:
        raw = value.get("@type")
        if isinstance(raw, list):
            return [str(item).lower() for item in raw]
        return [str(raw).lower()] if raw is not None else []

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
        category_id = self._category_id_from_url(url)
        if category_id == self.ROOT_CATEGORY_ID:
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

    def _product_id_from_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        match = self.PRODUCT_RE.search(urlsplit(url).path)
        return match.group(1) if match else None

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

    def _node_price(self, node: Any) -> Optional[float]:
        if not node:
            return None
        return parse_price(
            self._attr(node, "content")
            or self._attr(node, "value")
            or self._text(node)
        )

    def _image_from_node(self, node: Any, attrs: Optional[Iterable[str]] = None) -> Optional[str]:
        if not node:
            return None
        for attr in attrs or ("data-full-size-image-url", "data-image-large-src", "data-src", "data-lazy-src", "content", "src"):
            if attr == "srcset":
                value = self._first_srcset_url(node.attributes.get(attr), prefer_largest=True)
            else:
                value = node.attributes.get(attr)
            url = self._absolute_url(value)
            if url:
                return url
        return None

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        categories = self._extract_categories_from_sitemap()
        if not categories:
            categories = self._extract_categories_from_menu(HTMLParser(html))

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _fetch_sitemap_categories(self) -> List[str]:
        if self._sitemap_category_cache is not None:
            return self._sitemap_category_cache

        sitemap_url = self.selectors.get("frontpage", {}).get("sitemap", f"{self.base_url.rstrip('/')}/sitemap.xml")
        urls: List[str] = []
        try:
            with httpx.Client(headers=self._sync_headers(), follow_redirects=True, timeout=self.request_timeout) as client:
                response = client.get(sitemap_url)
                response.raise_for_status()
                for match in re.finditer(r"<loc>\s*(.*?)\s*</loc>", response.text, flags=re.I | re.S):
                    url = self._category_url(html_lib.unescape(match.group(1).strip()))
                    if url and url not in urls:
                        urls.append(url)
        except Exception as exc:
            self.logger.debug(f"Praga sitemap category extraction failed: {exc}")

        self._sitemap_category_cache = urls
        return urls

    def _category_metadata(self, client: httpx.Client, url: str, order: int) -> Optional[Dict[str, Any]]:
        category_id = self._category_id_from_url(url)
        meta: Dict[str, Any] = {
            "id": category_id,
            "name": self._name_from_url(url),
            "url": url,
            "parent_id": None,
            "depth": None,
            "order": order,
        }
        try:
            response = client.get(url)
            response.raise_for_status()
            tree = HTMLParser(response.text)
        except Exception as exc:
            self.logger.debug(f"Praga category metadata failed: {url} ({exc})")
            return None

        body = tree.css_first("body")
        body_class = body.attributes.get("class", "") if body else ""
        if "page-category" not in body_class:
            return None

        meta["id"] = self._extract_id_from_class(body_class, r"(?:^|\s)category-id-(\d+)(?:\s|$)") or category_id
        meta["parent_id"] = self._extract_id_from_class(body_class, r"(?:^|\s)category-id-parent-(\d+)(?:\s|$)")
        meta["depth"] = self._safe_int(self._extract_id_from_class(body_class, r"(?:^|\s)category-depth-level-(\d+)(?:\s|$)"))

        name = self._first_text(tree, [".block-category h1", "h1.h1", "h1"])
        if name:
            meta["name"] = name
        return meta

    def _extract_categories_from_sitemap(self) -> List[Dict[str, Any]]:
        return self._build_category_hierarchy_from_urls(self._fetch_sitemap_categories())

    def _extract_categories_from_menu(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        selector = self.selectors.get("frontpage", {}).get(
            "menu_links",
            ".ets_mm_megamenu a[href], #header a[href], header a[href], footer a[href]",
        )
        urls = []
        for link in tree.css(selector):
            url = self._category_url(self._attr(link, "href"))
            if url and url not in urls:
                urls.append(url)
        return self._build_category_hierarchy_from_urls(urls)

    def _build_category_hierarchy_from_urls(self, urls: List[str]) -> List[Dict[str, Any]]:
        if not urls:
            return []

        nodes: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        with httpx.Client(headers=self._sync_headers(), follow_redirects=True, timeout=self.request_timeout) as client:
            for index, url in enumerate(urls):
                meta = self._category_metadata(client, url, index)
                if not meta:
                    continue
                category_id = clean_text(meta.get("id"))
                if not category_id:
                    continue
                if category_id not in nodes:
                    order.append(category_id)
                nodes[category_id] = meta

        if not nodes:
            return []

        children: Dict[str, List[str]] = {}
        for category_id, meta in nodes.items():
            parent_id = clean_text(meta.get("parent_id"))
            if parent_id and parent_id in nodes and parent_id != category_id:
                children.setdefault(parent_id, []).append(category_id)

        for child_ids in children.values():
            child_ids.sort(key=lambda item_id: nodes[item_id].get("order", 0))

        top_ids = [
            category_id
            for category_id in order
            if not nodes[category_id].get("parent_id")
            or nodes[category_id].get("parent_id") == self.ROOT_CATEGORY_ID
            or nodes[category_id].get("parent_id") not in nodes
            or (nodes[category_id].get("depth") or 0) <= 2
        ]

        def make_node(category_id: str, level: str) -> Dict[str, Any]:
            meta = nodes[category_id]
            return {
                "name": meta.get("name") or self._name_from_url(meta.get("url", "")),
                "url": meta.get("url"),
                "level": level,
                "id": str(category_id),
                "category_id": str(category_id),
            }

        def append_subcategories(parent_id: str, out: List[Dict[str, Any]]) -> None:
            for child_id in children.get(parent_id, []):
                child = make_node(child_id, "subcategory")
                out.append(child)
                append_subcategories(child_id, out)

        categories = []
        for top_id in top_ids:
            top_cat = make_node(top_id, "top")
            top_cat["low_level_categories"] = []
            for low_id in children.get(top_id, []):
                low_cat = make_node(low_id, "low")
                low_cat["subcategories"] = []
                append_subcategories(low_id, low_cat["subcategories"])
                top_cat["low_level_categories"].append(low_cat)
            categories.append(top_cat)
        return categories

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
            "article.product-miniature, article.js-product-miniature, .thumbnail-container.product-miniature",
        )
        products = []
        for card in tree.css(selector):
            product = self._product_from_card(card)
            if product:
                products.append(product)
        return dedupe_products(products, self.logger, "praga listing")

    def _product_from_card(self, card: Any) -> Optional[Dict[str, Any]]:
        cp = self.selectors.get("category_page", {})
        link = None
        for candidate in card.css(cp.get("item_url", "a.product_name[href], a.thumbnail.product-thumbnail[href], a[href*='.html']")):
            if self._product_url(self._attr(candidate, "href")):
                link = candidate
                break
        url = self._product_url(self._attr(link, "href"))
        if not url:
            return None

        product_id = clean_text(card.attributes.get("data-id-product"))
        product_id = product_id or self._attr(card.css_first("input[name='id_product'][value]"), "value")
        product_id = product_id or self._product_id_from_url(url)
        attribute_id = clean_text(card.attributes.get("data-id-product-attribute"))

        name = self._text(link)
        name = name or self._attr(link, "title")
        if not name:
            name = self._first_text(card, [cp.get("item_name", "a.product_name[href], .product_name"), "[itemprop='name']"])
        if not name:
            image_node = card.css_first(cp.get("item_image", ".thumbnail-container img, img"))
            name = self._attr(image_node, "alt") or self._name_from_url(url)
        if not name:
            return None

        reference = self._strip_reference_label(self._text(card.css_first(cp.get("item_reference", ".references-list"))))
        brand = self._text(card.css_first(cp.get("item_brand", ".manufacturer a, .manufacturer")))
        price = self._node_price(card.css_first(cp.get("item_price", ".product-price-and-shipping .price, .price")))
        old_price = self._node_price(card.css_first(cp.get("item_old_price", ".product-price-and-shipping .regular-price, .regular-price")))
        if old_price is not None and price is not None and old_price <= price:
            old_price = None
        discount_percent = self._discount_percent(self._text(card.css_first(cp.get("item_discount", ".discount-percentage, .discount"))))
        if discount_percent is None:
            discount_percent = self._computed_discount(price, old_price)

        image = None
        for img_node in card.css(cp.get("item_image", ".thumbnail-container img, .product-thumbnail img, img[data-full-size-image-url], img")):
            image = self._image_from_node(img_node)
            if image:
                break

        availability_node = card.css_first(cp.get("item_availability", ".availability-list, .availability"))
        availability_text = self._text(availability_node) if availability_node else None
        class_text = f"{card.attributes.get('class', '')} {availability_node.attributes.get('class', '') if availability_node else ''}"
        if "out-of-stock" in class_text:
            availability, available = "Rupture de stock", False
        elif "in-stock" in class_text:
            availability, available = availability_text or "En stock", True
        else:
            availability, available = availability_from_text(availability_text or self._text(card))
            if available is None and card.css_first("button[data-button-action='add-to-cart'], .ajax_add_to_cart_button"):
                availability, available = "En stock", True

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "product_attribute_id": attribute_id,
            "url": url,
            "name": name,
            "title": name,
            "shop": self.site_name,
            "brand": brand,
            "reference": reference,
            "sku": reference,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": image,
            "availability": availability,
            "available": available,
            "currency": "TND",
        }
        return finalize_product_record({k: v for k, v in record.items() if v is not None})

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        all_products: List[dict] = []

        first = await self.scrape_category_page(category_url)
        if first.get("error"):
            return []
        all_products.extend(first.get("products") or [])
        total_pages = first.get("pagination", {}).get("total_pages", 1) or 1

        if limit and len(all_products) >= limit:
            return dedupe_products(all_products, self.logger, "praga category")[:limit]
        if total_pages <= 1:
            deduped = dedupe_products(all_products, self.logger, "praga category")
            return deduped[:limit] if limit else deduped

        if limit:
            for page_num in range(2, total_pages + 1):
                result = await self.scrape_category_page(self.build_page_url(category_url, page_num))
                if not result.get("error"):
                    all_products.extend(result.get("products") or [])
                if len(dedupe_products(all_products, None, "")) >= limit:
                    break
            deduped = dedupe_products(all_products, self.logger, "praga category")
            return deduped[:limit]

        page_sem = asyncio.Semaphore(4)

        async def fetch_page(page_num: int) -> List[dict]:
            async with page_sem:
                result = await self.scrape_category_page(self.build_page_url(category_url, page_num))
                if result.get("error"):
                    self.logger.debug(f"Praga page {page_num} failed for {category_url}: {result['error']}")
                    return []
                return result.get("products") or []

        page_results = await asyncio.gather(
            *[fetch_page(page_num) for page_num in range(2, total_pages + 1)],
            return_exceptions=True,
        )
        for page_result in page_results:
            if isinstance(page_result, Exception):
                self.logger.debug(f"Praga page fetch error: {page_result}")
            else:
                all_products.extend(page_result)
        return dedupe_products(all_products, self.logger, "praga category")

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        current_page = 1
        total_pages = 1
        total_results = None

        for link in tree.css(cp.get("pagination_pages", "link[rel='next'][href], .pagination a[href], .page-list a[href]")):
            href = self._attr(link, "href") or ""
            query_page = self._safe_int(dict(parse_qsl(urlsplit(href).query)).get("page"))
            text_page = self._safe_int(self._text(link))
            page_num = query_page or text_page
            if page_num:
                total_pages = max(total_pages, page_num)
                class_name = link.attributes.get("class", "")
                if "disabled" in class_name or "current" in class_name or "active" in class_name:
                    current_page = page_num

        next_node = tree.css_first(cp.get("pagination_next", "link[rel='next'][href], .pagination a[rel='next'][href], a.next[href]"))
        next_href = self._attr(next_node, "href") if next_node else None
        next_page = self._safe_int(dict(parse_qsl(urlsplit(next_href or "").query)).get("page"))
        if next_page:
            total_pages = max(total_pages, next_page)

        page_text = self._text(tree.css_first(cp.get("result_count", ".products-selection, #js-product-list-top, .total-products, body"))) or ""
        result_match = re.search(r"Il\s+y\s+a\s+([\d\s]+)\s+produits", page_text, re.I)
        if result_match:
            total_results = self._safe_int(result_match.group(1).replace(" ", ""))
            per_page = len(self.extract_products_from_html(html))
            if total_results and per_page:
                total_pages = max(total_pages, math.ceil(total_results / per_page))

        return {
            "current_page": current_page,
            "total_pages": max(1, total_pages),
            "has_next": bool(next_href) or total_pages > current_page,
            "total_results": total_results,
        }

    def build_page_url(self, base_url: str, page_num: int) -> str:
        url = self._absolute_url(base_url) or base_url
        parts = urlsplit(url)
        pairs = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in {"page", "p"}
        ]
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
            self.logger.debug(f"Praga detail parse failed: {product_url} ({exc})")
            return {"url": product_url, "error": str(exc)}

    def extract_product_details_from_html(self, html: str, product_url: str) -> dict:
        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = html_product_metadata(html, product_url, self.base_url)
        product_payload = self._extract_data_product(tree, pp.get("data_product", "#product-details[data-product]"))

        canonical = self._product_url(
            self._first_attr_or_text(tree, ["link[rel='canonical'][href]", "meta[property='og:url']"], ["href", "content"])
        )
        data["url"] = canonical or self._product_url(product_url) or product_url

        product_id = self._product_id_from_url(data["url"])
        body = tree.css_first("body")
        body_class = body.attributes.get("class", "") if body else ""
        product_id = self._extract_id_from_class(body_class, r"(?:^|\s)product-id-(\d+)(?:\s|$)") or product_id
        product_id = clean_text(product_payload.get("id_product") or product_payload.get("id")) or product_id
        product_id = self._attr(tree.css_first("input[name='id_product'][value]"), "value") or product_id
        if product_id:
            data["id"] = product_id
            data["product_id"] = product_id

        attribute_id = clean_text(product_payload.get("id_product_attribute"))
        if attribute_id:
            data["product_attribute_id"] = attribute_id

        title = self._first_attr_or_text(
            tree,
            [pp.get("title", "h1.h1.namne_details, h1.h1, h1, meta[property='og:title']")],
            ["content"],
        )
        title = self._clean_title(title)
        if title:
            data["title"] = title
            data["name"] = title

        reference = self._strip_reference_label(
            self._first_attr_or_text(
                tree,
                [pp.get("reference", ".product-reference, .product-reference-top, [itemprop='sku'], meta[itemprop='sku']")],
                ["content"],
            )
        )
        reference = reference or clean_text(product_payload.get("reference"))
        if reference:
            data["reference"] = reference
            data["sku"] = reference

        brand = clean_text(product_payload.get("manufacturer_name")) or clean_text(data.get("brand"))
        if not brand:
            brand = self._first_attr_or_text(
                tree,
                [pp.get("brand", ".product-manufacturer a, .manufacturer a, [itemprop='brand']")],
                ["content", "alt", "title"],
            )
        if brand:
            data["brand"] = brand

        price = self._parse_payload_price(product_payload.get("price_amount"))
        if price is None:
            price = self._node_price(tree.css_first(pp.get("price", ".product-prices .price, [itemprop='price'][content]")))
        if price is not None:
            data["price"] = price
        old_price = self._parse_payload_price(product_payload.get("price_without_reduction"))
        if old_price is None:
            old_price = self._node_price(tree.css_first(pp.get("old_price", ".product-prices .regular-price, .product-discount .regular-price")))
        if old_price is not None and data.get("price") is not None and old_price > data["price"]:
            data["old_price"] = old_price

        discount_percent = self._discount_percent(
            product_payload.get("discount_percentage_absolute")
            or product_payload.get("discount_percentage")
            or self._text(tree.css_first(pp.get("discount", ".product-prices .discount, .discount-percentage")))
        )
        if discount_percent is None:
            discount_percent = self._computed_discount(data.get("price"), data.get("old_price"))
        if discount_percent is not None:
            data["discount_percent"] = discount_percent

        availability, available = self._detail_availability(tree, pp, product_payload)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        description = self._clean_description(product_payload.get("description_short")) or self._clean_description(data.get("description"))
        if not description:
            description = self._clean_description(
                self._first_attr_or_text(
                    tree,
                    [pp.get("description", "meta[property='og:description'], #product-description-short, .product-description")],
                    ["content"],
                )
            )
        if description:
            data["description"] = description
            data["short_description"] = description
            data["overview"] = description

        full_description = self._clean_description(product_payload.get("description"))
        if not full_description:
            full_description = self._clean_description(
                self._first_attr_or_text(
                    tree,
                    [pp.get("full_description", "meta[property='og:description'], #description .product-description, .tabs .product-description")],
                    ["content"],
                )
            )
        if full_description:
            data["full_description"] = full_description

        specs = self._extract_specifications(tree, pp, product_payload)
        if specs:
            data["specifications"] = specs

        images = self._detail_images(tree, pp, product_payload, data)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = self._detail_categories(tree, pp)
        if categories:
            data["categories"] = categories
            data["breadcrumbs"] = [category["name"] for category in categories]

        data["currency"] = data.get("currency") or "TND"
        data["shop"] = self.site_name
        return finalize_product_record({k: v for k, v in data.items() if v is not None})

    @staticmethod
    def _clean_title(value: Any) -> Optional[str]:
        title = clean_text(value)
        if not title:
            return None
        title = re.sub(r"\s*\|\s*Praga\s*$", "", title, flags=re.I)
        title = re.sub(r"\s*-\s*Praga\s*$", "", title, flags=re.I)
        return clean_text(title)

    @staticmethod
    def _parse_payload_price(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        return parse_price(value)

    def _extract_data_product(self, tree: HTMLParser, selector: str) -> Dict[str, Any]:
        node = tree.css_first(selector)
        raw = self._attr(node, "data-product")
        if not raw:
            return {}
        try:
            parsed = json.loads(html_lib.unescape(raw))
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def _detail_availability(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        product_payload: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[bool]]:
        quantity = self._safe_int(product_payload.get("quantity"))
        if quantity is not None:
            return (f"{quantity} En stock", True) if quantity > 0 else ("Rupture de stock", False)

        availability_text = clean_text(product_payload.get("availability_message") or product_payload.get("availability"))
        if availability_text:
            availability, available = availability_from_text(availability_text)
            if available is not None:
                return availability, available

        availability_text = self._first_attr_or_text(
            tree,
            [pp.get("availability", "#product-availability, .product-availability, .product-add-to-cart, [itemprop='availability']")],
            ["href", "content"],
        )
        availability, available = availability_from_text(availability_text)
        if available is not None:
            return availability, available

        body = tree.css_first("body")
        body_class = body.attributes.get("class", "") if body else ""
        if "product-available-for-order" in body_class or tree.css_first("button[data-button-action='add-to-cart']"):
            return "En stock", True
        if "out-of-stock" in body_class:
            return "Rupture de stock", False
        return availability, available

    def _extract_specifications(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        product_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        specs: Dict[str, Any] = {}
        for key in ("reference", "category_name", "manufacturer_name", "quantity", "condition"):
            value = clean_text(product_payload.get(key))
            if value:
                specs[key] = value

        for feature in product_payload.get("features") or []:
            if not isinstance(feature, dict):
                continue
            key = clean_text(feature.get("name"))
            value = clean_text(feature.get("value"))
            if key and value:
                specs[key.rstrip(":")] = value

        for block in tree.css(pp.get("variants", ".product-variants .product-variants-item")):
            label = self._text(block.css_first(".control-label, label"))
            if not label:
                continue
            values = []
            for node in block.css("input, option, span[title], label, .radio-label, .color"):
                value = clean_text(node.attributes.get("title") or node.attributes.get("data-variant-name") or node.text(strip=True))
                if value and value != label and value not in values:
                    values.append(value)
            if values:
                specs[label.rstrip(":")] = ", ".join(values)

        for row in tree.css(pp.get("specs_rows", ".product-features .data-sheet tr, .product-features .data-sheet .row, .product-features .data-sheet div")):
            key = self._text(row.css_first("dt, th, .name, strong"))
            value = self._text(row.css_first("dd, td:last-child, .value, span"))
            if key and value and key != value:
                specs[key.rstrip(":")] = value
        return specs

    def _detail_images(
        self,
        tree: HTMLParser,
        pp: Dict[str, Any],
        product_payload: Dict[str, Any],
        data: Dict[str, Any],
    ) -> List[str]:
        images: List[str] = []
        for image_payload in product_payload.get("images") or []:
            if not isinstance(image_payload, dict):
                continue
            for key in ("large", "medium", "small"):
                item = image_payload.get(key)
                image = self._absolute_url(item.get("url") if isinstance(item, dict) else item)
                if image:
                    images.append(image)
                    break
            by_size = image_payload.get("bySize")
            if isinstance(by_size, dict):
                large = by_size.get("large_default") or by_size.get("home_default")
                image = self._absolute_url(large.get("url") if isinstance(large, dict) else None)
                if image:
                    images.append(image)

        for node in tree.css(pp.get("image_gallery", ".images-container img, .product-cover img, .thumb-container img, img[itemprop='image'], meta[property='og:image']")):
            image = self._image_from_node(node, ["data-full-size-image-url", "data-image-large-src", "data-src", "content", "srcset", "src"])
            if image:
                images.append(image)

        for image in data.get("images") or []:
            image_url = image.get("url") if isinstance(image, dict) else image
            image_url = self._absolute_url(image_url)
            if image_url:
                images.append(image_url)
        return self._dedupe_list(images)[:30]

    def _detail_categories(self, tree: HTMLParser, pp: Dict[str, Any]) -> List[Dict[str, str]]:
        categories: List[Dict[str, str]] = []
        seen = set()
        for link in tree.css(pp.get("breadcrumbs", "nav.breadcrumb a[href], .breadcrumb a[href]")):
            href = self._category_url(self._attr(link, "href"))
            name = self._text(link)
            if not href or not name:
                continue
            key = normalize_url(href) or href
            if key in seen:
                continue
            seen.add(key)
            categories.append({"name": name, "url": href})

        for crumb in self._jsonld_breadcrumbs(tree):
            href = self._category_url(crumb.get("url"))
            name = clean_text(crumb.get("name"))
            if not href or not name:
                continue
            key = normalize_url(href) or href
            if key in seen:
                continue
            seen.add(key)
            categories.append({"name": name, "url": href})
        return categories

    def _jsonld_breadcrumbs(self, tree: HTMLParser) -> List[Dict[str, str]]:
        crumbs: List[Dict[str, str]] = []
        for script in tree.css("script[type='application/ld+json']"):
            raw = script.text()
            if not raw:
                continue
            try:
                parsed = json.loads(html_lib.unescape(raw.strip()))
            except (TypeError, json.JSONDecodeError):
                continue
            for obj in self._walk_json(parsed):
                if not isinstance(obj, dict) or "breadcrumblist" not in self._type_names(obj):
                    continue
                for entry in obj.get("itemListElement") or []:
                    if not isinstance(entry, dict):
                        continue
                    item = entry.get("item")
                    if isinstance(item, dict):
                        name = clean_text(item.get("name") or entry.get("name"))
                        url = clean_text(item.get("@id") or item.get("url") or item.get("item"))
                    else:
                        name = clean_text(entry.get("name"))
                        url = clean_text(item)
                    if name and url:
                        crumbs.append({"name": name, "url": url})
        return crumbs


def get_scraper(logger: logging.Logger) -> PragaScraper:
    """Factory function used by scraper registry."""
    return PragaScraper(logger)
