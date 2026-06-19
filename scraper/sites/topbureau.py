#!/usr/bin/env python3
"""
TopBureau scraper.

TopBureau is an OpenCart-style storefront with server-rendered category,
listing, pagination, and product detail pages. The site does not require
Playwright for the current HTML surface.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

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


class TopbureauScraper(FastScraper):
    """Fast HTTP scraper for topbureau.tn."""

    BRAND_FALLBACKS = {
        "84": "MINOLTA",
        "86": "RICOH",
        "87": "CANON",
        "76": "SHARP",
        "91": "EPSON",
        "92": "HP",
        "93": "BROTHER",
    }
    PRODUCT_FAMILY_CATEGORIES = (
        ("33", "PHOTOCOPIEURS"),
        ("24", "IMPRIMANTES"),
        ("181", "CONSOMMABLES"),
    )
    KNOWN_BRANDS = {
        "MINOLTA",
        "KONICA MINOLTA",
        "RICOH",
        "CANON",
        "SHARP",
        "EPSON",
        "HP",
        "BROTHER",
    }
    GENERIC_CATEGORY_NAMES = {"", "ACCUEIL", "INFORMATIONS", "AUTRES MARQUES"}
    NON_PRODUCT_CATEGORY_PATHS = {"184"}
    BAD_IMAGE_MARKERS = (
        "blank.gif",
        "/logo",
        "logo.",
        "language/",
        "fr-fr.png",
        "en-gb.png",
        "Logo-top-Bureau",
    )
    IMAGE_ATTRS = ("data-echo", "data-src", "data-original", "data-zoom-image", "src")
    DETAIL_IMAGE_ATTRS = (
        "data-zoom-image",
        "href",
        "data-image",
        "data-echo",
        "data-src",
        "data-original",
        "src",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("topbureau", logger)
        self._category_heading_cache: Dict[str, Optional[str]] = {}

    # ------------------------------------------------------------------
    # URL, text, image, and price helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, value: Any) -> Optional[str]:
        url = absolute_url(value, self.base_url)
        if not url:
            return None
        parts = urlsplit(url)
        scheme = "https" if parts.netloc.endswith("topbureau.tn") else parts.scheme
        netloc = "www.topbureau.tn" if parts.netloc.endswith("topbureau.tn") else parts.netloc
        return urlunsplit((scheme, netloc, parts.path, parts.query, ""))

    def _query(self, url: str) -> Dict[str, List[str]]:
        return parse_qs(urlsplit(url).query, keep_blank_values=True)

    def _category_path(self, url: Any) -> Optional[str]:
        absolute = self._absolute_url(url)
        if not absolute:
            return None
        query = self._query(absolute)
        if (query.get("route") or [""])[0] != "product/category":
            return None
        path = (query.get("path") or [""])[0]
        return path or None

    def _product_id_from_url(self, url: Any) -> Optional[str]:
        absolute = self._absolute_url(url)
        if not absolute:
            return None
        return (self._query(absolute).get("product_id") or [""])[0] or None

    def _category_url(self, value: Any) -> Optional[str]:
        absolute = self._absolute_url(value)
        if not absolute:
            return None
        path = self._category_path(absolute)
        if not path or path in self.NON_PRODUCT_CATEGORY_PATHS:
            return None
        query = urlencode({"route": "product/category", "path": path}, safe="/")
        return f"https://www.topbureau.tn/index.php?{query}"

    def _product_url(self, value: Any) -> Optional[str]:
        absolute = self._absolute_url(value)
        if not absolute:
            return None
        query = self._query(absolute)
        if (query.get("route") or [""])[0] != "product/product":
            return None
        product_id = (query.get("product_id") or [""])[0]
        if not product_id:
            return None
        params = {"route": "product/product"}
        path = (query.get("path") or [""])[0]
        if path:
            params["path"] = path
        params["product_id"] = product_id
        return f"https://www.topbureau.tn/index.php?{urlencode(params, safe='/')}"

    def _text(self, node: Any) -> Optional[str]:
        if not node:
            return None
        return clean_text(node.text(separator=" ", strip=True))

    def _first_text(self, root: Any, selectors: Iterable[str]) -> Optional[str]:
        for selector in selectors:
            text = self._text(root.css_first(selector))
            if text:
                return text
        return None

    def _is_bad_image(self, value: Any) -> bool:
        url = clean_text(value)
        if not url:
            return True
        lower = url.lower()
        if lower.startswith("data:"):
            return True
        return any(marker.lower() in lower for marker in self.BAD_IMAGE_MARKERS)

    def _image_from_node(self, node: Any, attrs: Iterable[str]) -> Optional[str]:
        if not node:
            return None
        for attr in attrs:
            image = node.attributes.get(attr)
            image = self._absolute_url(image)
            if image and not self._is_bad_image(image):
                return image
        return None

    def _collect_images(self, nodes: Iterable[Any], attrs: Iterable[str]) -> List[str]:
        images: List[str] = []
        for node in nodes:
            image = self._image_from_node(node, attrs)
            if image and image not in images:
                images.append(image)
        return images

    def _price_from_node(self, node: Any) -> Optional[float]:
        if not node:
            return None
        return parse_price(node.attributes.get("content") or node.text(separator=" ", strip=True))

    def _first_price(self, root: Any, selectors: Iterable[str]) -> Optional[float]:
        for selector in selectors:
            for node in root.css(selector):
                price = self._price_from_node(node)
                if price is not None:
                    return price
        return None

    def _discount_percent(
        self, price: Optional[float], old_price: Optional[float]
    ) -> Optional[int]:
        if price is None or old_price is None or old_price <= price or old_price <= 0:
            return None
        return round(((old_price - price) / old_price) * 100)

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def _heading_for_category(self, url: str) -> Optional[str]:
        if url in self._category_heading_cache:
            return self._category_heading_cache[url]

        heading = None
        client_kwargs: Dict[str, Any] = {
            "headers": self.headers,
            "follow_redirects": True,
            "timeout": httpx.Timeout(self.request_timeout, connect=10.0),
        }
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url

        try:
            with httpx.Client(**client_kwargs) as client:
                response = client.get(url)
                response.raise_for_status()
                tree = HTMLParser(response.text)
                heading = self._first_text(tree, ("#content h1", "h1"))
        except Exception as exc:
            self.logger.debug(f"TopBureau category heading fetch failed for {url}: {exc}")

        self._category_heading_cache[url] = heading
        return heading

    def _brand_from_image(self, link: Any) -> Optional[str]:
        image = link.css_first("img") if link else None
        if not image:
            return None
        text = clean_text(image.attributes.get("alt") or image.attributes.get("title"))
        if text:
            return text
        src = (image.attributes.get("src") or "").lower()
        if "minolta" in src:
            return "MINOLTA"
        if "ricoh" in src:
            return "RICOH"
        if "canon" in src:
            return "CANON"
        if "sharp" in src:
            return "SHARP"
        if "epson" in src or "espson" in src:
            return "EPSON"
        if re.search(r"/hp(?:\.|_|-)", src):
            return "HP"
        return None

    def _top_category_name(self, link: Any, url: str, path: str) -> str:
        visible = (self._text(link) or "").strip()
        if visible.upper() not in self.GENERIC_CATEGORY_NAMES:
            return visible
        image_brand = self._brand_from_image(link)
        if image_brand:
            return image_brand
        heading = self._heading_for_category(url)
        if heading:
            return heading
        return self.BRAND_FALLBACKS.get(path, path)

    def _category_link_name(self, link: Any, path: str) -> str:
        name = self._text(link)
        if name:
            return name
        return self.BRAND_FALLBACKS.get(path, path)

    def _category_link_rows(self, root: Any) -> List[Tuple[str, str, str]]:
        rows: List[Tuple[str, str, str]] = []
        seen_paths = set()
        for link in root.css("a[href*='route=product/category'][href*='path=']"):
            url = self._category_url(link.attributes.get("href"))
            path = self._category_path(url)
            if not url or not path or path in seen_paths:
                continue
            name = self._category_link_name(link, path)
            if name.upper() in self.GENERIC_CATEGORY_NAMES:
                continue
            seen_paths.add(path)
            rows.append((path, name, url))
        return rows

    def _leaf_subcategories(
        self,
        low_path: str,
        path_order: List[str],
        names: Dict[str, str],
        urls: Dict[str, str],
    ) -> List[Dict[str, str]]:
        descendants = [path for path in path_order if path.startswith(f"{low_path}_")]
        leaves = [
            path for path in descendants
            if not any(other.startswith(f"{path}_") for other in descendants)
        ]

        subcategories = []
        base_depth = len(low_path.split("_"))
        for leaf_path in leaves:
            pieces = leaf_path.split("_")
            names_in_path = []
            current = "_".join(pieces[:base_depth])
            for segment in pieces[base_depth:]:
                current = f"{current}_{segment}"
                name = names.get(current)
                if name:
                    names_in_path.append(name)
            sub_name = " > ".join(names_in_path) or names.get(leaf_path, leaf_path)
            subcategories.append({
                "name": sub_name,
                "url": urls[leaf_path],
                "level": "subcategory",
            })
        return subcategories

    def _build_brand_categories(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        categories: List[Dict[str, Any]] = []
        top_blocks = tree.css(".container-megamenu.vertical ul.megamenu > li")

        for top_block in top_blocks:
            top_link = top_block.css_first("a[href*='route=product/category'][href*='path=']")
            top_url = self._category_url(top_link.attributes.get("href")) if top_link else None
            top_path = self._category_path(top_url) if top_url else None
            if not top_url or not top_path or "_" in top_path or top_path in self.NON_PRODUCT_CATEGORY_PATHS:
                continue

            top_name = self._top_category_name(top_link, top_url, top_path)
            top_cat: Dict[str, Any] = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "category_id": top_path,
                "low_level_categories": [],
            }

            rows = [
                row for row in self._category_link_rows(top_block)
                if row[0].startswith(f"{top_path}_")
            ]
            path_order = [path for path, _, _ in rows]
            names = {path: name for path, name, _ in rows}
            urls = {path: url for path, _, url in rows}

            low_paths = [
                path for path in path_order
                if path.count("_") == top_path.count("_") + 1
            ]
            for low_path in low_paths:
                low_cat = {
                    "name": names[low_path],
                    "url": urls[low_path],
                    "level": "low",
                    "category_id": low_path,
                    "subcategories": self._leaf_subcategories(low_path, path_order, names, urls),
                }
                top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        return categories

    def _build_horizontal_fallback_categories(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        categories = []
        seen = set()
        for link in tree.css(".container-megamenu.horizontal ul.megamenu > li > a[href*='route=product/category'][href*='path=']"):
            url = self._category_url(link.attributes.get("href"))
            path = self._category_path(url)
            name = self._text(link)
            if not url or not path or path in seen or not name:
                continue
            if path in self.NON_PRODUCT_CATEGORY_PATHS or name.upper() in self.GENERIC_CATEGORY_NAMES:
                continue
            seen.add(path)
            categories.append({
                "name": name,
                "url": url,
                "level": "top",
                "category_id": path,
                "low_level_categories": [],
            })
        return categories

    def _build_product_family_categories(self, tree: HTMLParser) -> List[Dict[str, Any]]:
        """Use broad product families instead of the brand-based vertical menu."""
        configured = dict(self.PRODUCT_FAMILY_CATEGORIES)
        link_by_path: Dict[str, str] = {}

        for link in tree.css(".container-megamenu.horizontal ul.megamenu > li > a[href*='route=product/category'][href*='path=']"):
            url = self._category_url(link.attributes.get("href"))
            path = self._category_path(url)
            if path in configured and url:
                link_by_path[path] = url

        categories = []
        for path, name in self.PRODUCT_FAMILY_CATEGORIES:
            url = link_by_path.get(path)
            if not url:
                url = f"https://www.topbureau.tn/index.php?route=product/category&path={path}"
            categories.append({
                "name": name,
                "url": url,
                "level": "top",
                "category_id": path,
                "low_level_categories": [],
            })
        return categories

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
        tree = HTMLParser(html)
        categories = self._build_product_family_categories(tree)

        return {"categories": categories, "stats": self._category_stats(categories)}

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------

    def _product_id_from_card(self, card: Any, url: Optional[str]) -> Optional[str]:
        product_id = self._product_id_from_url(url)
        if product_id:
            return product_id
        match = re.search(r"cart\.add\(['\"](\d+)['\"]\)", card.html or "")
        return match.group(1) if match else None

    def _listing_prices(self, card: Any) -> Tuple[Optional[float], Optional[float]]:
        price = self._first_price(card, (".price-new",))
        old_price = self._first_price(card, (".price-old",))
        if price is None:
            price = self._first_price(card, (".price",))
        return price, old_price

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        products: List[Dict[str, Any]] = []

        for card in tree.css(".product-grid .product"):
            link = card.css_first(".name a[href*='route=product/product']")
            url = self._product_url(link.attributes.get("href")) if link else None
            if not url:
                continue

            name = self._text(link)
            product_id = self._product_id_from_card(card, url)
            price, old_price = self._listing_prices(card)
            image = self._collect_images(card.css(".image img, img"), self.IMAGE_ATTRS)
            availability, available = availability_from_text(
                self._first_text(card, (".availability", ".description", ".stock"))
            )

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "url": url,
                "name": name,
                "price": price,
                "shop": self.site_name,
            }
            if image:
                product["image"] = image[0]
            if old_price is not None:
                product["old_price"] = old_price
            discount_percent = self._discount_percent(price, old_price)
            if discount_percent is not None:
                product["discount_percent"] = discount_percent
            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "topbureau listing")

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        params = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "page"
        ]
        if page_num > 1:
            params.append(("page", str(page_num)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params, safe="/"), ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        current_page = 1
        total_pages = 1

        active = tree.css_first("ul.pagination li.active span, ul.pagination li.active a, .pagination li.active span, .pagination li.active a")
        active_text = self._text(active)
        if active_text and active_text.isdigit():
            current_page = int(active_text)

        for link in tree.css("ul.pagination a[href*='page='], .pagination a[href*='page='], a[href*='page=']"):
            href = self._absolute_url(link.attributes.get("href"))
            if not href:
                continue
            page = (self._query(href).get("page") or [""])[0]
            if page.isdigit():
                total_pages = max(total_pages, int(page))

        has_next = any(
            (self._text(link) or "").strip() in {">", "Next", "Suivant"}
            for link in tree.css("ul.pagination a[href*='page='], .pagination a[href*='page=']")
        )
        if current_page < total_pages:
            has_next = True

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

    def _summary_labels(self, tree: HTMLParser) -> Dict[str, str]:
        summary = tree.css_first(".product-center .description")
        if not summary:
            return {}

        labels: Dict[str, str] = {}
        spans = summary.css("span")
        for index, span in enumerate(spans):
            key = self._text(span)
            if not key or ":" not in key:
                continue
            key = key.rstrip(":").strip().lower()
            value = None
            for candidate in spans[index + 1:]:
                candidate_text = self._text(candidate)
                if candidate_text and ":" not in candidate_text:
                    value = candidate_text
                    break
            if value:
                labels[key] = value
        return labels

    def _detail_prices(self, tree: HTMLParser) -> Tuple[Optional[float], Optional[float]]:
        price = self._first_price(
            tree,
            (
                ".product-center .price .price-new [itemprop='price']",
                ".product-center .price .price-new",
                ".product-center .price [itemprop='price']",
            ),
        )
        old_price = self._first_price(tree, (".product-center .price .price-old",))
        return price, old_price

    def _extract_specs(self, tree: HTMLParser) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for row in tree.css(".product-specifications tr, #tab-description .product-specifications tr"):
            key = self._text(row.css_first("th, td:first-child"))
            value = self._text(row.css_first("td:last-child, .specs-attribute"))
            if not key or not value or key == value:
                continue
            key = key.rstrip(":")
            if key in specs and specs[key] != value:
                if value not in specs[key].split(" | "):
                    specs[key] = f"{specs[key]} | {value}"
            else:
                specs[key] = value
        return specs

    def _breadcrumbs(self, tree: HTMLParser, title: Optional[str]) -> List[str]:
        crumbs = [
            text for text in (self._text(node) for node in tree.css(".breadcrumb a, ul.breadcrumb a"))
            if text
        ]
        if title and crumbs and crumbs[-1].strip().lower() == title.strip().lower():
            crumbs = crumbs[:-1]
        return crumbs

    def _brand_from_breadcrumbs(self, breadcrumbs: List[str]) -> Optional[str]:
        for crumb in breadcrumbs:
            normalized = crumb.upper()
            if normalized in self.KNOWN_BRANDS:
                return crumb
        return None

    def _detail_images(self, tree: HTMLParser) -> List[str]:
        nodes = tree.css(".popup-gallery a, .popup-gallery img, #image")
        return self._collect_images(nodes, self.DETAIL_IMAGE_ATTRS)

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "fetch_failed"}

        tree = HTMLParser(html)
        data: Dict[str, Any] = html_product_metadata(html, product_url=url, base_url=self.base_url)
        data["url"] = self._product_url(data.get("url") or url) or self._absolute_url(url) or url

        title = self._first_text(
            tree,
            (
                "#content [itemtype='http://schema.org/Product'] .product-name",
                ".product-info .product-name",
                "[itemprop='name']",
                "h1",
            ),
        )
        if title:
            data["title"] = title
            data["name"] = title

        product_id_el = tree.css_first("input[name='product_id'][value]")
        product_id = product_id_el.attributes.get("value") if product_id_el else None
        product_id = clean_text(product_id) or self._product_id_from_url(url)
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        labels = self._summary_labels(tree)
        reference = labels.get("code produit")
        if reference:
            data["reference"] = reference
            data["sku"] = reference

        availability_text = labels.get("disponibilité") or labels.get("disponibilite")
        availability, available = availability_from_text(availability_text)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        price, old_price = self._detail_prices(tree)
        if price is not None:
            data["price"] = price
        if old_price is not None:
            data["old_price"] = old_price
        discount_percent = self._discount_percent(data.get("price"), data.get("old_price"))
        if discount_percent is not None:
            data["discount_percent"] = discount_percent

        description = self._first_text(tree, ("#tab-description",))
        if description:
            data["description"] = description
            data["full_description"] = description

        specs = self._extract_specs(tree)
        if specs:
            data["specifications"] = specs

        images = self._detail_images(tree)
        if images:
            data["images"] = images
            data["image"] = images[0]

        breadcrumbs = self._breadcrumbs(tree, data.get("title"))
        if breadcrumbs:
            data["breadcrumbs"] = breadcrumbs
            data["categories"] = breadcrumbs
        brand = self._brand_from_breadcrumbs(breadcrumbs)
        if brand:
            data["brand"] = brand

        return finalize_product_record(data)


def get_scraper(logger: logging.Logger) -> TopbureauScraper:
    return TopbureauScraper(logger)
