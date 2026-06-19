#!/usr/bin/env python3
"""
WatchesTunisia scraper - PrestaShop, HTTPX/selectolax.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


class WatchesTunisiaScraper(FastScraper):
    """HTTP scraper for watchestunisia.com."""

    CATEGORY_RE = re.compile(r"^/\d+-[^/?#]+/?$", re.I)
    PRODUCT_RE = re.compile(r"/(\d+)(?:-\d+)?-[^/]+\.html(?:$|[?#])", re.I)
    LOC_RE = re.compile(
        r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>",
        re.I | re.S,
    )
    EXCLUDE_URL_TOKENS = (
        "mon-compte",
        "connexion",
        "login",
        "logout",
        "panier",
        "cart",
        "commande",
        "checkout",
        "order",
        "search",
        "recherche",
        "contact",
        "nous-contacter",
        "content/",
        "module/",
        "blog",
        "blogs",
        "wishlist",
        "compare",
        "productcompare",
        "facebook.",
        "instagram.",
        "tiktok.",
        "elyosdigital",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("watchestunisia", logger)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _abs(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

    def _same_host(self, url: str) -> bool:
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        return host == base_host

    @staticmethod
    def _text(node: Any, separator: str = " ") -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=separator, strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

    @staticmethod
    def _first_attr(node: Any, attrs: Iterable[str]) -> Optional[str]:
        if not node:
            return None
        for attr in attrs:
            value = clean_text(node.attributes.get(attr))
            if value:
                return value
        return None

    @staticmethod
    def _direct_children(node: Any, tags: Optional[set] = None) -> List[Any]:
        if not node:
            return []
        children = []
        for child in node.iter():
            if child.parent != node:
                continue
            if tags is None or child.tag in tags:
                children.append(child)
        return children

    def _direct_link(self, node: Any) -> Optional[Any]:
        for child in self._direct_children(node, {"a"}):
            if child.attributes.get("href"):
                return child
        return node.css_first("a[href]") if node else None

    def _child_lists(self, node: Any) -> List[Any]:
        lists = self._direct_children(node, {"ul", "ol"})
        for child in self._direct_children(node, {"div"}):
            lists.extend(self._direct_children(child, {"ul", "ol"}))
        return lists

    def _strip_url(self, url: Optional[str], keep_query: bool = False) -> Optional[str]:
        abs_url = self._abs(url)
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

    def _is_category_url(self, url: Optional[str]) -> bool:
        url = self._strip_url(url)
        if not url:
            return False
        parts = urlsplit(url)
        low = url.lower()
        if not parts.scheme.startswith("http") or not self._same_host(url):
            return False
        if parts.query or parts.fragment:
            return False
        if parts.path.lower().endswith(".html"):
            return False
        if parts.path.rstrip("/") == "/2-accueil":
            return False
        if any(token in low for token in self.EXCLUDE_URL_TOKENS):
            return False
        return bool(self.CATEGORY_RE.match(parts.path))

    def _category_name_from_url(self, url: str) -> str:
        slug = urlsplit(url).path.strip("/").split("-", 1)[-1]
        return clean_text(re.sub(r"[-_]+", " ", slug).title()) or slug

    @staticmethod
    def _clean_html_fragment(value: Any) -> Optional[str]:
        if value is None:
            return None
        raw = html_lib.unescape(str(value))
        if "<" in raw and ">" in raw:
            raw = re.sub(r"<[^>]+>", " ", raw)
        return clean_text(raw)

    @staticmethod
    def _discount_percent(
        value: Any,
        price: Optional[float],
        old_price: Optional[float],
    ) -> Optional[float]:
        text = clean_text(value)
        if text:
            match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
            if match:
                return parse_price(match.group(1))
        if price is not None and old_price and old_price > price:
            return round((1 - price / old_price) * 100, 2)
        return None

    # ------------------------------------------------------------------
    # Frontpage/category discovery
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"Downloading WatchesTunisia frontpage: {self.base_url}")
        html = await self.fetch_html(self.base_url, raise_on_error=True)
        save_text_atomic(html, output_path, self.logger)

        fp = self.selectors.get("frontpage", {})
        await self._fetch_optional_text(
            fp.get("robots_url", f"{self.base_url.rstrip('/')}/robots.txt"),
            "robots.txt",
        )

        sitemap_urls = [
            fp.get("sitemap_index_url"),
            fp.get("sitemap_url"),
        ]
        fetched_sitemaps = []
        for url in [u for u in sitemap_urls if u]:
            text = await self._fetch_optional_text(url, self._filename_from_url(url))
            if text:
                fetched_sitemaps.append(text)

        for sitemap_text in fetched_sitemaps:
            for loc in self._locs_from_sitemap_text(sitemap_text):
                if not loc.lower().endswith(".xml"):
                    continue
                if any(loc == u for u in sitemap_urls if u):
                    continue
                await self._fetch_optional_text(loc, self._filename_from_url(loc))

        for url in fp.get("seed_category_urls", []):
            filename = f"category_seed_{urlsplit(url).path.strip('/').replace('/', '_')}.html"
            await self._fetch_optional_text(url, filename)

        return output_path

    async def _fetch_optional_text(self, url: str, filename: str) -> Optional[str]:
        meta = await self.fetch_html_with_meta(url)
        text = meta.get("html")
        if not text:
            self.logger.debug(
                f"WatchesTunisia optional fetch failed url={url} error={meta.get('error')}"
            )
            return None
        save_text_atomic(text, self.html_dir / filename, self.logger)
        return text

    @staticmethod
    def _filename_from_url(url: str) -> str:
        name = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "download.html"
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)

    def _locs_from_sitemap_text(self, text: str) -> List[str]:
        locs = []
        for raw in self.LOC_RE.findall(text or ""):
            value = clean_text(html_lib.unescape(raw))
            if value:
                locs.append(value)
        return locs

    def extract_categories_from_html(self, html: str) -> dict:
        seed_html = self._read_seed_category_html()
        categories = self._extract_categories_from_tree(seed_html or html)
        if not categories:
            categories = self._extract_flat_categories(html)

        stats = self._category_stats(categories)
        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )
        return {"categories": categories, "stats": stats}

    def _read_seed_category_html(self) -> Optional[str]:
        for path in sorted(self.html_dir.glob("category_seed_*.html")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "category-sub-menu" in text and "#js-product-list" not in path.name:
                return text
        return None

    def _category_link_from_node(self, node: Any) -> Tuple[Optional[str], Optional[str]]:
        link = self._direct_link(node)
        if not link:
            return None, None
        url = self._strip_url(link.attributes.get("href"))
        if not self._is_category_url(url):
            return None, None
        name = self._text(link) or self._category_name_from_url(url)
        if name and name.lower() in {"accueil", "home"}:
            return None, None
        return url, name

    def _extract_categories_from_tree(self, html: str) -> List[Dict[str, Any]]:
        tree = HTMLParser(html)
        selector = self.selectors.get("frontpage", {}).get(
            "category_tree",
            ".block-categories .category-sub-menu, .category-sub-menu",
        )
        root = tree.css_first(selector)
        if not root:
            return []

        categories: List[Dict[str, Any]] = []
        seen_urls = set()
        for top_li in self._direct_children(root, {"li"}):
            top_url, top_name = self._category_link_from_node(top_li)
            if not top_url or top_url in seen_urls:
                continue
            seen_urls.add(top_url)
            top = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            for low_ul in self._child_lists(top_li):
                for low_li in self._direct_children(low_ul, {"li"}):
                    low_url, low_name = self._category_link_from_node(low_li)
                    if not low_url or low_url in seen_urls:
                        continue
                    seen_urls.add(low_url)
                    low = {
                        "name": low_name,
                        "url": low_url,
                        "level": "low",
                        "subcategories": [],
                    }

                    for sub_ul in self._child_lists(low_li):
                        for sub_li in self._direct_children(sub_ul, {"li"}):
                            sub_url, sub_name = self._category_link_from_node(sub_li)
                            if not sub_url or sub_url in seen_urls:
                                continue
                            seen_urls.add(sub_url)
                            low["subcategories"].append(
                                {
                                    "name": sub_name,
                                    "url": sub_url,
                                    "level": "subcategory",
                                }
                            )

                    top["low_level_categories"].append(low)

            categories.append(top)

        return categories

    def _extract_flat_categories(self, html: str) -> List[Dict[str, Any]]:
        tree = HTMLParser(html)
        links: List[Tuple[str, str]] = []
        seen = set()
        selector = self.selectors.get("frontpage", {}).get(
            "nav_links",
            "header a[href], .tv-menu-horizontal a[href], footer a[href]",
        )
        for node in tree.css(selector):
            url = self._strip_url(node.attributes.get("href"))
            if not self._is_category_url(url) or url in seen:
                continue
            seen.add(url)
            links.append((url, self._text(node) or self._category_name_from_url(url)))

        for path in sorted(self.html_dir.glob("*sitemap*.xml")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for loc in self._locs_from_sitemap_text(text):
                url = self._strip_url(loc)
                if not self._is_category_url(url) or url in seen:
                    continue
                seen.add(url)
                links.append((url, self._category_name_from_url(url)))

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

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "page"
        ]
        if page_num and page_num > 1:
            query.append(("page", str(page_num)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        item_selector = cp.get(
            "item_selector",
            "#js-product-list .product-miniature.js-product-miniature",
        )
        cards = tree.css(item_selector)
        if not cards:
            product_list = tree.css_first("#js-product-list")
            cards = product_list.css("[data-id-product]") if product_list else []

        products = []
        for card in cards:
            product = self._product_from_card(card, cp)
            if product:
                products.append(product)
        return dedupe_products(products, self.logger, "watchestunisia listing")

    def _product_from_card(self, card: Any, selectors: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        product_id = clean_text(card.attributes.get(selectors.get("item_id_attr", "data-id-product")))
        attribute_id = clean_text(
            card.attributes.get(selectors.get("item_attribute_id_attr", "data-id-product-attribute"))
        )

        link = card.css_first(selectors.get("item_url", "a.thumbnail.product-thumbnail[href], .product-title a[href]"))
        url = self._strip_url(link.attributes.get("href")) if link else None
        if not url:
            return None

        name_node = card.css_first(selectors.get("item_name", ".product-title a, .tvproduct-name a"))
        name = self._text(name_node) or self._text(link)
        if not name:
            return None

        price_node = card.css_first(selectors.get("item_price", ".price"))
        price = parse_price(
            self._first_attr(price_node, ["content", "data-price"])
            or self._text(price_node)
        )
        old_node = card.css_first(selectors.get("item_old_price", ".regular-price"))
        old_price = parse_price(
            self._first_attr(old_node, ["content", "data-price"])
            or self._text(old_node)
        )
        discount_node = card.css_first(
            selectors.get("item_discount", ".discount-percentage, .product-flag.discount, .discount")
        )
        discount_percent = self._discount_percent(
            self._text(discount_node),
            price,
            old_price,
        )

        image = self._image_from_node(
            card,
            selectors.get("item_image", ".tvproduct-image img, img[itemprop='image']"),
            selectors.get("item_image_attrs", ["data-original", "data-src", "src"]),
        )

        availability_node = card.css_first(
            selectors.get("item_availability", ".product-availability, [itemprop='availability']")
        )
        availability_value = self._first_attr(availability_node, ["content", "href"]) or self._text(availability_node)
        availability, available = availability_from_text(availability_value)

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "product_attribute_id": attribute_id,
            "url": url,
            "name": name,
            "title": name,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": image,
            "availability": availability,
            "available": available,
            "shop": self.site_name,
        }
        return finalize_product_record({k: v for k, v in record.items() if v is not None})

    def _image_from_node(self, root: Any, selector: str, attrs: Iterable[str]) -> Optional[str]:
        image = root.css_first(selector)
        if not image:
            return None
        for attr in attrs:
            value = image.attributes.get(attr)
            if not value or value.startswith("data:"):
                continue
            url = self._abs(value)
            if url:
                return url
        return None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        current_page = 1
        total_pages = 1

        current = tree.css_first(
            ".pagination .current a, .pagination li.current a, .pagination .active a, .pagination li.current"
        )
        if current:
            text = self._text(current)
            if text and text.isdigit():
                current_page = int(text)
                total_pages = max(total_pages, current_page)

        selector = self.selectors.get("category_page", {}).get(
            "pagination_links",
            ".pagination a.js-search-link, .page-list a, .pagination a, link[rel='next'], link[rel='prev']",
        )
        for link in tree.css(selector):
            text = self._text(link)
            if text and text.isdigit():
                total_pages = max(total_pages, int(text))
            href = link.attributes.get("href", "")
            match = re.search(r"[?&]page=(\d+)", href)
            if match:
                total_pages = max(total_pages, int(match.group(1)))

        next_selector = self.selectors.get("category_page", {}).get(
            "pagination_next",
            "a.next.js-search-link, a[rel='next'], link[rel='next']",
        )
        next_link = tree.css_first(next_selector)
        has_next = bool(next_link)
        if next_link:
            classes = next_link.attributes.get("class", "").lower()
            href = next_link.attributes.get("href")
            if "disabled" in classes or not href:
                has_next = False

        if current_page >= total_pages:
            has_next = False

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        sample_path = self.html_dir / "detail_sample_1.html"
        if not sample_path.exists():
            save_text_atomic(html, sample_path, self.logger)

        tree = HTMLParser(html)
        payload = self._product_json(tree)
        data: Dict[str, Any] = {"url": self._strip_url(url) or url, "shop": self.site_name}
        data.update(self._data_from_product_json(payload))

        product_id_match = self.PRODUCT_RE.search(url)
        if product_id_match:
            data.setdefault("product_id", product_id_match.group(1))
            data.setdefault("id", product_id_match.group(1))

        metadata = html_product_metadata(html, url, self.base_url)
        for key, value in metadata.items():
            if value not in (None, "", [], {}):
                data.setdefault(key, value)

        self._apply_detail_fallbacks(tree, data)
        specs = self._extract_specs(tree, payload)
        if specs:
            data["specifications"] = specs

        breadcrumbs = self._extract_breadcrumbs(tree)
        if breadcrumbs:
            data["breadcrumbs"] = breadcrumbs
            data.setdefault("categories", breadcrumbs)

        images = list(data.get("images") or [])
        for image in self._extract_images_from_html(tree):
            if image not in images:
                images.append(image)
        if images:
            data["images"] = images
            data.setdefault("image", images[0])

        return finalize_product_record({k: v for k, v in data.items() if v is not None})

    def _product_json(self, tree: HTMLParser) -> Dict[str, Any]:
        selector = self.selectors.get("product_page", {}).get(
            "product_json",
            "#product-details[data-product], .js-product-details[data-product]",
        )
        node = tree.css_first(selector)
        if not node:
            return {}
        raw = node.attributes.get("data-product")
        if not raw:
            return {}
        for value in (html_lib.unescape(raw), raw):
            try:
                return json.loads(value)
            except Exception:
                continue
        return {}

    def _data_from_product_json(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        if not payload:
            return data

        product_id = clean_text(payload.get("id_product") or payload.get("id"))
        if product_id:
            data["product_id"] = product_id
            data["id"] = product_id

        name = clean_text(payload.get("name"))
        if name:
            data["title"] = name
            data["name"] = name

        reference = clean_text(payload.get("reference"))
        if reference:
            if normalize_gtin(reference):
                data["barcode"] = reference
            else:
                data["reference"] = reference
                data["sku"] = reference

        price = parse_price(payload.get("price_amount") or payload.get("price"))
        if price is not None:
            data["price"] = price
        old_price = parse_price(
            payload.get("price_without_reduction")
            or payload.get("regular_price")
            or payload.get("old_price")
        )
        if old_price is not None and (price is None or old_price > price):
            data["old_price"] = old_price

        discount = self._discount_percent(
            payload.get("discount_percentage")
            or payload.get("discount_percentage_absolute")
            or payload.get("discount_amount_to_display"),
            price,
            old_price,
        )
        if discount is None:
            specific = payload.get("specific_prices")
            if isinstance(specific, dict) and specific.get("reduction_type") == "percentage":
                reduction = parse_price(specific.get("reduction"))
                if reduction is not None:
                    discount = round(reduction * 100, 2) if reduction <= 1 else reduction
        if discount is not None:
            data["discount_percent"] = discount

        brand = clean_text(payload.get("manufacturer_name"))
        if brand:
            data["brand"] = brand

        short_description = self._clean_html_fragment(payload.get("description_short"))
        description = self._clean_html_fragment(payload.get("description"))
        if short_description:
            data["short_description"] = short_description
        if description:
            data["description"] = description

        availability_text = clean_text(
            payload.get("availability_message")
            or payload.get("available_now")
            or payload.get("availability")
        )
        availability, available = availability_from_text(availability_text)
        if availability_text or availability:
            data["availability"] = availability_text or availability
        if available is not None:
            data["available"] = available
        elif clean_text(payload.get("availability")) == "available":
            data["availability"] = "En stock"
            data["available"] = True
        elif payload.get("quantity") is not None:
            quantity = parse_price(payload.get("quantity")) or 0
            data["availability"] = "En stock" if quantity > 0 else "Rupture de stock"
            data["available"] = quantity > 0

        category_name = clean_text(payload.get("category_name"))
        if category_name:
            data["categories"] = [category_name]
        category_slug = clean_text(payload.get("category"))
        if category_slug:
            data["category_slug"] = category_slug

        link = self._strip_url(payload.get("link"))
        if link:
            data.setdefault("canonical_url", link)

        images = self._images_from_product_json(payload)
        if images:
            data["images"] = images
            data["image"] = images[0]

        return data

    def _images_from_product_json(self, payload: Dict[str, Any]) -> List[str]:
        images = []

        def add(candidate: Any) -> None:
            if isinstance(candidate, dict):
                candidate = candidate.get("url")
            url = self._abs(candidate)
            if url and url not in images:
                images.append(url)

        for container in (payload.get("cover"), *(payload.get("images") or [])):
            if not isinstance(container, dict):
                add(container)
                continue
            by_size = container.get("bySize") or {}
            before = len(images)
            for size in ("large_default", "medium_default", "home_default"):
                sized = by_size.get(size)
                if isinstance(sized, dict):
                    add(sized.get("url"))
                    break
            if len(images) > before:
                continue
            for key in ("large", "medium", "url"):
                add(container.get(key))
                if len(images) > before:
                    break
        return images

    def _apply_detail_fallbacks(self, tree: HTMLParser, data: Dict[str, Any]) -> None:
        pp = self.selectors.get("product_page", {})

        title = self._text(tree.css_first(pp.get("title", "h1[itemprop='name'], h1")))
        if title:
            data.setdefault("title", title)
            data.setdefault("name", title)

        sku_node = tree.css_first(pp.get("sku", ".product-reference span[itemprop='sku'], .product-reference span"))
        sku = self._text(sku_node)
        if sku:
            if normalize_gtin(sku):
                data.setdefault("barcode", sku)
            else:
                data.setdefault("reference", sku)
                data.setdefault("sku", sku)

        price_node = tree.css_first(pp.get("price", ".current-price .price, .product-prices .price"))
        price = parse_price(
            self._first_attr(price_node, ["content", "data-price"])
            or self._text(price_node)
        )
        if price is not None:
            data.setdefault("price", price)

        old_node = tree.css_first(pp.get("old_price", ".product-prices .regular-price, .regular-price"))
        old_price = parse_price(
            self._first_attr(old_node, ["content", "data-price"])
            or self._text(old_node)
        )
        if old_price is not None and old_price > (data.get("price") or 0):
            data.setdefault("old_price", old_price)
            data.setdefault("discount_percent", self._discount_percent(None, data.get("price"), old_price))

        discount_node = tree.css_first(pp.get("discount", ".discount-percentage, .discount"))
        discount = self._discount_percent(self._text(discount_node), data.get("price"), data.get("old_price"))
        if discount is not None:
            data.setdefault("discount_percent", discount)

        brand_node = tree.css_first(pp.get("brand", ".product-manufacturer img, .product-manufacturer a"))
        if brand_node:
            brand = clean_text(
                brand_node.attributes.get(pp.get("brand_attr", "alt"))
                or brand_node.attributes.get("title")
                or self._text(brand_node)
            )
            if brand:
                data.setdefault("brand", brand)

        availability_node = tree.css_first(pp.get("availability", "#product-availability, .js-product-availability"))
        availability, available = availability_from_text(self._text(availability_node))
        if availability:
            data.setdefault("availability", availability)
        if available is not None:
            data.setdefault("available", available)

        short_node = tree.css_first(pp.get("short_description", "#product-description-short"))
        desc_node = tree.css_first(pp.get("description", ".tabs .product-description, .product-description"))
        short_description = self._text(short_node)
        description = self._text(desc_node)
        if short_description:
            data.setdefault("short_description", short_description)
        if description:
            data.setdefault("description", description)

    def _extract_specs(self, tree: HTMLParser, payload: Dict[str, Any]) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        pp = self.selectors.get("product_page", {})
        container_selector = pp.get("specs_container", ".product-features dl.data-sheet, .product-features")
        for block in tree.css(container_selector):
            keys = block.css(pp.get("specs_key", "dt.name, dt"))
            values = block.css(pp.get("specs_value", "dd.value, dd"))
            for key_node, value_node in zip(keys, values):
                key = self._text(key_node)
                value = self._text(value_node)
                if key and value:
                    specs[key] = value

        for feature in payload.get("features") or []:
            if not isinstance(feature, dict):
                continue
            key = clean_text(feature.get("name"))
            value = clean_text(feature.get("value"))
            if key and value:
                specs.setdefault(key, value)
        return specs

    def _extract_images_from_html(self, tree: HTMLParser) -> List[str]:
        pp = self.selectors.get("product_page", {})
        selectors = [
            pp.get("image_main", ".product-cover img, .images-container .product-cover img"),
            pp.get("image_thumbnails", ".product-images img, .thumb-container img, img.thumb.js-thumb"),
        ]
        images = []
        for selector in selectors:
            for img in tree.css(selector):
                src = self._first_attr(
                    img,
                    [
                        "data-zoom-image",
                        "data-image-large-src",
                        "data-full-size-image-url",
                        "data-src",
                        "src",
                        "content",
                    ],
                )
                if not src or src.startswith("data:"):
                    continue
                url = self._abs(src)
                if url and url not in images:
                    images.append(url)
        return images

    def _extract_breadcrumbs(self, tree: HTMLParser) -> List[str]:
        pp = self.selectors.get("product_page", {})
        breadcrumbs = []
        for link in tree.css(pp.get("breadcrumbs", "nav.breadcrumb a, .breadcrumb a")):
            name = self._text(link)
            if not name or name.lower() in {"accueil", "home"}:
                continue
            if name not in breadcrumbs:
                breadcrumbs.append(name)
        return breadcrumbs


def get_scraper(logger: logging.Logger) -> WatchesTunisiaScraper:
    return WatchesTunisiaScraper(logger)
