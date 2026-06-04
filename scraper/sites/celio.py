#!/usr/bin/env python3
"""
Celio.tn scraper - Magento 2, HTTP/selectolax.
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


class CelioScraper(FastScraper):
    """HTTP scraper for celio.tn Magento category and product pages."""

    BAD_CATEGORY_PARTS = (
        "/customer",
        "/checkout",
        "/cart",
        "/wishlist",
        "/account",
        "/sales",
        "/catalogsearch",
        "/search",
        "/contact",
        "/localisateur",
        "/guide-des-tailles",
        "/privacy",
        "/conditions",
        "/be-normal",
        "/feel-good",
        "/media/",
        "/static/",
        "mailto:",
        "tel:",
        "javascript:",
        "facebook",
        "instagram",
        "youtube",
        "tiktok",
        "linkedin",
    )
    CATEGORY_ROOTS = {
        "/collection.html",
        "/nouveautes.html",
        "/collabs.html",
        "/soldes.html",
    }
    CATEGORY_PREFIXES = (
        "/collection/",
        "/nouveautes/",
        "/collabs/",
        "/soldes/",
    )
    FILTER_QUERY_KEYS = {
        "price",
        "color",
        "couleur",
        "celio_size",
        "size",
        "product_list_order",
        "product_list_dir",
        "product_list_limit",
        "cat",
    }

    def __init__(self, logger: logging.Logger):
        super().__init__("celio", logger)

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
    def _name_from_url(url: str) -> str:
        path = urlsplit(url).path.strip("/").rstrip("/")
        slug = path.split("/")[-1] if path else "celio"
        slug = re.sub(r"\.html$", "", slug, flags=re.I)
        slug = re.sub(r"[-_]+", " ", slug).strip()
        return slug.title() if slug else "Celio"

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

    def _category_path_allowed(self, path: str) -> bool:
        normalized = (path or "/").rstrip("/").lower()
        if normalized in self.CATEGORY_ROOTS:
            return True
        return normalized.endswith(".html") and normalized.startswith(self.CATEGORY_PREFIXES)

    def _category_url(self, value: Any) -> Optional[str]:
        url = self._absolute_url(value)
        if not url or not self._is_site_url(url):
            return None
        parts = urlsplit(url)
        low = url.lower()
        if any(token in low for token in self.BAD_CATEGORY_PARTS):
            return None
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        if any(key in self.FILTER_QUERY_KEYS for key, _ in query_pairs):
            return None
        if not self._category_path_allowed(parts.path):
            return None
        return self._strip_url(url, keep_query=False)

    def _product_url(self, value: Any) -> Optional[str]:
        url = self._absolute_url(value)
        if not url or not self._is_site_url(url):
            return None
        parts = urlsplit(url)
        if not parts.path.lower().endswith(".html"):
            return None
        if self._category_path_allowed(parts.path):
            return None
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))

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
            self._attr(node, "data-price-amount")
            or self._attr(node, "content")
            or self._attr(node, "value")
            or self._text(node)
        )

    def _image_from_node(self, node: Any) -> Optional[str]:
        if not node:
            return None
        for attr in ("data-src", "data-original", "data-lazy", "data-image", "content", "src"):
            value = self._absolute_url(node.attributes.get(attr))
            if value:
                return value
        srcset = self._first_srcset_url(node.attributes.get("srcset"), prefer_largest=True)
        return self._absolute_url(srcset) if srcset else None

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
        return clean_text(text)

    @staticmethod
    def _strip_reference_label(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"^\s*(?:sku|reference|ref)\s*:?\s*", "", text, flags=re.I)
        return clean_text(text)

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = self._extract_categories_from_menu(tree)
        sitemap_categories = self._extract_categories_from_sitemap()
        if categories:
            self._append_missing_top_categories(categories, sitemap_categories)
        else:
            categories = sitemap_categories

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _extract_categories_from_menu(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        fp = self.selectors.get("frontpage", {})
        categories: List[Dict[str, Any]] = []
        seen_top = set()

        for block in tree.css(fp.get("menu_blocks", "nav.navigation li.level0, .navigation li.level0")):
            top_link = block.css_first("a[href]")
            top_url = self._category_url(self._attr(top_link, "href"))
            top_name = self._text(top_link) or (self._name_from_url(top_url) if top_url else None)

            low_categories: List[Dict[str, Any]] = []
            seen_low = set()
            for low_block in block.css(fp.get("low_blocks", "li.level1")):
                low_link = low_block.css_first("a[href]")
                low_url = self._category_url(self._attr(low_link, "href"))
                low_name = self._text(low_link) or (self._name_from_url(low_url) if low_url else None)
                if not low_url or not low_name or low_url in seen_low or low_url == top_url:
                    continue

                subcategories: List[Dict[str, Any]] = []
                seen_sub = set()
                for sub_link in low_block.css(fp.get("sub_links", "li.level2 > a[href], li.level3 > a[href]")):
                    sub_url = self._category_url(self._attr(sub_link, "href"))
                    sub_name = self._text(sub_link) or (self._name_from_url(sub_url) if sub_url else None)
                    if not sub_url or not sub_name or sub_url in seen_sub or sub_url == low_url:
                        continue
                    subcategories.append({"name": sub_name, "url": sub_url, "level": "subcategory"})
                    seen_sub.add(sub_url)

                low_categories.append(
                    {
                        "name": low_name,
                        "url": low_url,
                        "level": "low",
                        "subcategories": subcategories,
                    }
                )
                seen_low.add(low_url)

            if not top_url and not low_categories:
                continue
            top_key = top_url or top_name
            if not top_key or top_key in seen_top:
                continue
            categories.append(
                {
                    "name": top_name or "Celio",
                    "url": top_url,
                    "level": "top",
                    "low_level_categories": low_categories,
                }
            )
            seen_top.add(top_key)

        return categories

    def _append_missing_top_categories(
        self,
        categories: List[Dict[str, Any]],
        sitemap_categories: List[Dict[str, Any]],
    ) -> None:
        seen_urls = self._category_urls_in_tree(categories)
        seen_names = {clean_text(cat.get("name", "")).lower() for cat in categories if clean_text(cat.get("name", ""))}
        for top in sitemap_categories:
            top_url = top.get("url")
            top_name = clean_text(top.get("name", ""))
            if not top_url or top_url in seen_urls:
                continue
            if top_name and top_name.lower() in seen_names:
                continue
            categories.append(top)
            seen_urls.update(self._category_urls_in_tree([top]))
            if top_name:
                seen_names.add(top_name.lower())

    @staticmethod
    def _category_urls_in_tree(categories: List[Dict[str, Any]]) -> set:
        urls = set()
        for top in categories:
            if top.get("url"):
                urls.add(top["url"])
            for low in top.get("low_level_categories", []):
                if low.get("url"):
                    urls.add(low["url"])
                for sub in low.get("subcategories", []):
                    if sub.get("url"):
                        urls.add(sub["url"])
        return urls

    def _extract_categories_from_sitemap(self) -> List[Dict[str, Any]]:
        endpoint = self.selectors.get("frontpage", {}).get("sitemap", f"{self.base_url.rstrip('/')}/pub/sitemap.xml")
        urls: List[str] = []
        try:
            with httpx.Client(headers=self._sync_headers(), follow_redirects=True, timeout=self.request_timeout) as client:
                response = client.get(endpoint)
                response.raise_for_status()
                for match in re.finditer(r"<loc>(.*?)</loc>", response.text):
                    url = self._category_url(html_lib.unescape(match.group(1)))
                    if url and url not in urls:
                        urls.append(url)
        except Exception as exc:
            self.logger.debug(f"Celio sitemap category fallback failed: {exc}")
            return []

        return self._hierarchy_from_category_urls(urls)

    def _hierarchy_from_category_urls(self, urls: List[str]) -> List[Dict[str, Any]]:
        tops: Dict[str, Dict[str, Any]] = {}
        low_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}

        def top_node(key: str, url: Optional[str] = None) -> Dict[str, Any]:
            if key not in tops:
                tops[key] = {
                    "name": self._segment_name(key),
                    "url": url,
                    "level": "top",
                    "low_level_categories": [],
                }
            elif url and not tops[key].get("url"):
                tops[key]["url"] = url
            return tops[key]

        for url in urls:
            path = urlsplit(url).path.strip("/")
            parts = [part for part in path.split("/") if part]
            if not parts:
                continue

            first = parts[0]
            if len(parts) == 1:
                key = re.sub(r"\.html$", "", first, flags=re.I)
                top_node(key, url)
                continue

            top = top_node(first)
            low_key = re.sub(r"\.html$", "", parts[1], flags=re.I)
            low_id = (first, low_key)
            if low_id not in low_by_key:
                low_by_key[low_id] = {
                    "name": self._segment_name(low_key),
                    "url": url if len(parts) == 2 else None,
                    "level": "low",
                    "subcategories": [],
                }
                top["low_level_categories"].append(low_by_key[low_id])
            elif len(parts) == 2 and not low_by_key[low_id].get("url"):
                low_by_key[low_id]["url"] = url

            if len(parts) > 2:
                sub_name = self._name_from_url(url)
                if not any(sub.get("url") == url for sub in low_by_key[low_id]["subcategories"]):
                    low_by_key[low_id]["subcategories"].append(
                        {"name": sub_name, "url": url, "level": "subcategory"}
                    )

        return list(tops.values())

    @staticmethod
    def _segment_name(segment: str) -> str:
        segment = re.sub(r"\.html$", "", segment or "", flags=re.I)
        segment = re.sub(r"[-_]+", " ", segment).strip()
        return segment.title() if segment else "Celio"

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
            "li.product-item, .products-grid .product-item, .product-items .product-item",
        )
        products = []
        for card in tree.css(selector):
            product = self._product_from_card(card)
            if product:
                products.append(product)
        return dedupe_products(products, self.logger, "celio listing")

    def _product_from_card(self, card: Any) -> Optional[Dict[str, Any]]:
        cp = self.selectors.get("category_page", {})
        link = None
        for candidate in card.css(cp.get("item_url", "a.product-item-link[href], a.product.photo.product-item-photo[href]")):
            if self._product_url(self._attr(candidate, "href")):
                link = candidate
                break
        url = self._product_url(self._attr(link, "href"))
        if not url:
            return None

        name = self._text(link)
        if not name:
            name = self._first_text(card, [cp.get("item_name", "a.product-item-link"), "a.product-item-link", ".product-item-name"])
        if not name:
            image_node = card.css_first(cp.get("item_image", "img.product-image-photo, .product-item-photo img"))
            name = self._attr(image_node, "alt") or self._name_from_url(url)
        if not name:
            return None

        id_node = card.css_first(cp.get("item_id", ".price-box[data-product-id], span[id^='product-price-']"))
        product_id = self._attr(id_node, "data-product-id")
        if not product_id:
            price_box = self._attr(id_node, "data-price-box") or self._attr(id_node, "id")
            match = re.search(r"product(?:-price|-id)?-(\d+)", price_box or "")
            if match:
                product_id = match.group(1)

        price = self._node_price(card.css_first(cp.get("item_price", "[data-price-type='finalPrice'][data-price-amount], .price-box .price")))
        old_price = self._node_price(card.css_first(cp.get("item_old_price", ".old-price [data-price-amount], [data-price-type='oldPrice'], .old-price .price")))
        if old_price is not None and price is not None and old_price <= price:
            old_price = None
        discount_percent = self._discount_percent(self._text(card.css_first(".discount, .discount-percent, .product-label-sale")))
        if discount_percent is None:
            discount_percent = self._computed_discount(price, old_price)

        image = None
        for img_node in card.css(cp.get("item_image", "img.product-image-photo, .product-item-photo img")):
            image = self._image_from_node(img_node)
            if image:
                break

        class_text = card.attributes.get("class", "")
        text = self._text(card) or ""
        availability = None
        available = None
        if re.search(r"out[-_ ]?of[-_ ]?stock|rupture|indisponible|epuis", f"{class_text} {text}", re.I):
            availability, available = "Rupture de stock", False

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
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
        next_href = None

        page_numbers: List[int] = []
        prev_numbers: List[int] = []
        next_numbers: List[int] = []
        for link in tree.css(cp.get("pagination_pages", ".pages a[href], link[rel='next'][href]")):
            href = self._attr(link, "href") or ""
            page_num = self._safe_int(dict(parse_qsl(urlsplit(href).query)).get("p"))
            if not page_num:
                continue
            page_numbers.append(page_num)
            text = (self._text(link) or "").lower()
            class_name = link.attributes.get("class", "").lower()
            if "prev" in class_name or "chevron-left" in class_name or "precedent" in text:
                prev_numbers.append(page_num)
            if "next" in class_name or "chevron-right" in class_name or "suivant" in text:
                next_numbers.append(page_num)

        if next_numbers:
            next_href_node = tree.css_first(cp.get("pagination_next", "link[rel='next'][href], .pages a.action.next[href], .pages a.c-pager_page.chevron-right[href]"))
            next_href = self._attr(next_href_node, "href") if next_href_node else "next"
            current_page = max(1, min(next_numbers) - 1)
        elif prev_numbers:
            current_page = max(prev_numbers) + 1

        if page_numbers:
            total_pages = max(total_pages, max(page_numbers), current_page)

        page_text = " ".join(
            self._text(node) or ""
            for node in tree.css(cp.get("result_count", ".toolbar-amount"))
        )
        result_match = re.search(r"(\d[\d\s.,]*)\s+articles?", page_text, re.I)
        if result_match:
            total_results = self._safe_int(re.sub(r"\D", "", result_match.group(1)))
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
        pairs = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in {"p", "page"}
        ]
        if page_num > 1:
            pairs.append(("p", str(page_num)))
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
            self.logger.debug(f"Celio detail parse failed: {product_url} ({exc})")
            return {"url": product_url, "error": str(exc)}

    def extract_product_details_from_html(self, html: str, product_url: str) -> dict:
        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = html_product_metadata(html, product_url, self.base_url)
        canonical = self._product_url(product_url) or self._product_url(
            self._first_attr_or_text(tree, ["link[rel='canonical'][href]", "meta[property='og:url']"], ["href", "content"])
        )
        data["url"] = canonical or product_url

        product_id = self._first_attr_or_text(
            tree,
            [pp.get("product_id", "input[name='product'][value], .product-info-main .price-box[data-product-id]")],
            ["value", "data-product-id"],
        )
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        title = self._first_attr_or_text(
            tree,
            [pp.get("title", ".product-info-main [itemprop='name'], .product-name, meta[property='og:title']")],
            ["content"],
        )
        if title:
            data["title"] = title
            data["name"] = title

        reference = self._strip_reference_label(
            self._first_attr_or_text(
                tree,
                [pp.get("reference", ".product.attribute.sku .value[itemprop='sku'], .sku .value, [itemprop='sku']")],
                ["content"],
            )
        )
        if reference:
            data["reference"] = reference
            data["sku"] = reference

        price = self._node_price(tree.css_first(pp.get("price", ".product-info-main [data-price-type='finalPrice'][data-price-amount], .product-info-main .price-wrapper")))
        if price is not None:
            data["price"] = price
        old_price = self._node_price(tree.css_first(pp.get("old_price", ".product-info-main .old-price [data-price-amount], .product-info-main [data-price-type='oldPrice']")))
        if old_price is not None and data.get("price") is not None and old_price > data["price"]:
            data["old_price"] = old_price
        discount_percent = self._computed_discount(data.get("price"), data.get("old_price"))
        if discount_percent is not None:
            data["discount_percent"] = discount_percent

        availability_text = self._first_attr_or_text(
            tree,
            [pp.get("availability", ".stock, .product-info-stock-sku, .box-tocart, button.tocart")],
            ["content", "href"],
        )
        body = tree.css_first("body")
        body_class = body.attributes.get("class", "") if body else ""
        availability, available = availability_from_text(availability_text)
        if available is None:
            if re.search(r"out[-_ ]?of[-_ ]?stock|rupture|indisponible|epuis", body_class, re.I):
                availability, available = "Rupture de stock", False
            elif tree.css_first("button.tocart, #product_addtocart_form"):
                availability, available = "En stock", True
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        description = self._clean_description(
            self._first_attr_or_text(
                tree,
                [pp.get("description", ".product.attribute.description .value[itemprop='description'], meta[property='og:description'], meta[name='description']")],
                ["content"],
            )
        )
        if description:
            data["description"] = description
            data["overview"] = description
            data["short_description"] = description

        full_description = self._clean_description(
            self._first_attr_or_text(
                tree,
                [pp.get("full_description", ".product.attribute.description .value[itemprop='description'], .product.data.items, meta[property='og:description']")],
                ["content"],
            )
        )
        if full_description:
            data["full_description"] = full_description

        specifications, options = self._extract_specifications_and_options(tree)
        if specifications:
            data["specifications"] = specifications
            brand = self._brand_from_specs(specifications)
            if brand:
                data["brand"] = brand
        if options:
            data["options"] = options

        images = []
        for node in tree.css(pp.get("image_gallery", ".product.media img, .gallery-placeholder img, img.fotorama__img, meta[property='og:image']")):
            image = self._image_from_node(node)
            if image:
                images.append(image)
        images = self._dedupe_list(images)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = []
        for link in tree.css(pp.get("breadcrumbs", ".breadcrumbs a[href]")):
            category_url = self._category_url(self._attr(link, "href"))
            category_name = self._text(link)
            if category_url and category_name:
                categories.append({"name": category_name, "url": category_url})
        if categories:
            data["categories"] = categories

        data["shop"] = self.site_name
        return finalize_product_record({k: v for k, v in data.items() if v is not None})

    def _extract_specifications_and_options(self, tree: HTMLParser) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        pp = self.selectors.get("product_page", {})
        specs: Dict[str, Any] = {}
        options: Dict[str, Any] = {}

        for row in tree.css(pp.get("specs_rows", ".additional-attributes tr, table.data.table.additional-attributes tr")):
            key_node = row.css_first("th, .label, td:first-child")
            value_node = row.css_first("td:last-child, .data, .value")
            key = self._text(key_node)
            value = self._text(value_node)
            if key and value and key != value:
                specs[key] = value

        reference = self._strip_reference_label(self._first_text(tree, [".product.attribute.sku"]))
        if reference:
            specs.setdefault("Reference", reference)

        swatch_config = self._extract_magento_swatch_config(tree)
        attributes = swatch_config.get("attributes") if isinstance(swatch_config, dict) else None
        if isinstance(attributes, dict):
            for attr in attributes.values():
                if not isinstance(attr, dict):
                    continue
                label = clean_text(attr.get("label") or attr.get("code"))
                values = [
                    clean_text(option.get("label"))
                    for option in attr.get("options", [])
                    if isinstance(option, dict) and clean_text(option.get("label"))
                ]
                values = self._dedupe_list(values)
                if label and values:
                    options[label] = values
                    specs.setdefault(label, ", ".join(values))

        for key in ("parentsku", "modelecoloris"):
            value = self._simple_json_config_value(swatch_config.get(key)) if isinstance(swatch_config, dict) else None
            if value:
                specs.setdefault(key, value)

        skus = swatch_config.get("skus") if isinstance(swatch_config, dict) else None
        if isinstance(skus, dict) and skus:
            variant_skus = {str(k): clean_text(v) for k, v in skus.items() if clean_text(v)}
            if variant_skus:
                options["variant_skus"] = variant_skus

        quantities = swatch_config.get("quantities") if isinstance(swatch_config, dict) else None
        if isinstance(quantities, dict) and quantities:
            options["variant_quantities"] = quantities

        return specs, options

    def _extract_magento_swatch_config(self, tree: HTMLParser) -> Dict[str, Any]:
        for script in tree.css("script[type='text/x-magento-init'], script"):
            raw = script.text()
            if not raw or "jsonConfig" not in raw:
                continue
            try:
                parsed = json.loads(html_lib.unescape(raw.strip()))
            except (TypeError, json.JSONDecodeError):
                continue
            config = self._find_json_config(parsed)
            if config:
                return config
        return {}

    def _find_json_config(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            config = value.get("jsonConfig")
            if isinstance(config, dict):
                return config
            for child in value.values():
                found = self._find_json_config(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = self._find_json_config(child)
                if found:
                    return found
        return {}

    @staticmethod
    def _simple_json_config_value(value: Any) -> Optional[str]:
        if value is None or isinstance(value, (dict, list)):
            return None
        text = clean_text(value)
        if not text:
            return None
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            return None
        if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
            stripped = stripped[1:-1]
        return clean_text(stripped)

    @staticmethod
    def _brand_from_specs(specifications: Dict[str, Any]) -> Optional[str]:
        for key, value in specifications.items():
            if re.search(r"marque|brand", str(key), re.I):
                return clean_text(value)
        return None


def get_scraper(logger: logging.Logger) -> CelioScraper:
    return CelioScraper(logger)
