#!/usr/bin/env python3
"""
Tunisiatech.tn scraper - PrestaShop, HTTPX/selectolax.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, get_date_folder, save_json, save_text_atomic
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


class TunisiaTechScraper(FastScraper):
    """HTTP scraper for tunisiatech.tn."""

    CATEGORY_RE = re.compile(r"^/\d+-[^/?#]+/?$", re.I)
    PRODUCT_RE = re.compile(r"/(\d+)-[^/]+\.html(?:$|[?#])", re.I)
    EXCLUDE_URL_TOKENS = (
        "mon-compte",
        "panier",
        "commande",
        "connexion",
        "login",
        "logout",
        "search",
        "recherche",
        "contact",
        "content/",
        "module/",
        "blog",
        "storefinder",
        "marques",
        "brand",
        "facebook.",
        "instagram.",
        "youtube.",
        "linkedin.",
        "tiktok.",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("tunisiatech", logger)

    # ------------------------------------------------------------------
    # URL/text helpers
    # ------------------------------------------------------------------

    def _abs(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

    def _same_host(self, url: str) -> bool:
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        return host == base_host

    def _is_category_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        parsed = urlsplit(url)
        low = url.lower()
        if not parsed.scheme.startswith("http") or not self._same_host(url):
            return False
        if any(token in low for token in self.EXCLUDE_URL_TOKENS):
            return False
        if parsed.query or parsed.fragment:
            return False
        if parsed.path.lower().endswith(".html"):
            return False
        return bool(self.CATEGORY_RE.match(parsed.path))

    def _category_name_from_url(self, url: str) -> str:
        slug = urlsplit(url).path.strip("/").split("-", 1)[-1]
        slug = re.sub(r"[-_]+", " ", slug)
        return clean_text(slug.title()) or url.rstrip("/").rsplit("/", 1)[-1]

    def _clean_html_fragment(self, value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if "<" in text and ">" in text:
            text = re.sub(r"<[^>]+>", " ", text)
        return clean_text(html_lib.unescape(text))

    def _first_attr(self, node: Any, attrs: Iterable[str]) -> Optional[str]:
        if not node:
            return None
        for attr in attrs:
            value = clean_text(node.attributes.get(attr))
            if value:
                return value
        return None

    # ------------------------------------------------------------------
    # Frontpage/category discovery
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        """Download homepage and cache sitemap.xml for category discovery."""
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"Downloading Tunisiatech frontpage: {self.base_url}")
        html = await self.fetch_html(self.base_url, raise_on_error=True)
        save_text_atomic(html, output_path, self.logger)

        sitemap_url = self.selectors.get("frontpage", {}).get(
            "sitemap_url", f"{self.base_url.rstrip('/')}/sitemap.xml"
        )
        meta = await self.fetch_html_with_meta(sitemap_url)
        sitemap = meta.get("html")
        if sitemap:
            save_text_atomic(sitemap, self.html_dir / "sitemap.xml", self.logger)
        else:
            self.logger.warning(
                f"Failed to fetch Tunisiatech sitemap: {meta.get('error')}"
            )
        return output_path

    def _category_links_from_html(self, html: str, base_url: str) -> List[Tuple[str, str]]:
        tree = HTMLParser(html)
        selector = self.selectors.get("frontpage", {}).get(
            "nav_links",
            "header a[href], #_desktop_top_menu a[href], .top-menu a[href], .menu a[href], footer a[href]",
        )
        links: List[Tuple[str, str]] = []
        seen = set()
        for node in tree.css(selector):
            href = node.attributes.get("href")
            url = self._abs(href, base_url)
            if not self._is_category_url(url):
                continue
            normalized = normalize_url(url) or url
            if normalized in seen:
                continue
            seen.add(normalized)
            name = clean_text(node.text(strip=True)) or self._category_name_from_url(url)
            links.append((normalized, name))
        return links

    def _locs_from_sitemap_text(self, text: str) -> List[str]:
        locs: List[str] = []
        try:
            root = ET.fromstring(text.encode("utf-8"))
            for elem in root.iter():
                if elem.tag.lower().endswith("loc") and elem.text:
                    value = clean_text(elem.text)
                    if value:
                        locs.append(value)
        except Exception:
            locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text, flags=re.I)
        return locs

    def _category_links_from_sitemap(self, text: str) -> List[Tuple[str, str]]:
        links: List[Tuple[str, str]] = []
        seen = set()
        for loc in self._locs_from_sitemap_text(text):
            url = self._abs(loc)
            if not self._is_category_url(url):
                continue
            normalized = normalize_url(url) or url
            if normalized in seen:
                continue
            seen.add(normalized)
            links.append((normalized, self._category_name_from_url(normalized)))
        return links

    def extract_categories_from_html(self, html: str) -> dict:
        """Synchronous fallback: use category links visible in the frontpage."""
        categories = []
        for url, name in self._category_links_from_html(html, self.base_url):
            categories.append(
                {
                    "name": name,
                    "url": url,
                    "level": "top",
                    "low_level_categories": [],
                }
            )

        stats = {
            "top_level": len(categories),
            "low_level": 0,
            "subcategory": 0,
            "total_urls": len(categories),
        }
        return {"categories": categories, "stats": stats}

    def _json_objects(self, html: str) -> Iterable[Any]:
        tree = HTMLParser(html)
        for script in tree.css("script[type='application/ld+json']"):
            raw = clean_text(script.text())
            if not raw:
                continue
            try:
                yield json.loads(html_lib.unescape(raw))
            except Exception:
                continue

    def _walk_json(self, value: Any) -> Iterable[Any]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from self._walk_json(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_json(child)

    def _extract_breadcrumbs(self, html: str) -> List[str]:
        for obj in self._json_objects(html):
            for item in self._walk_json(obj):
                if not isinstance(item, dict):
                    continue
                raw_type = item.get("@type")
                types = raw_type if isinstance(raw_type, list) else [raw_type]
                if "BreadcrumbList" not in [str(t) for t in types]:
                    continue
                crumbs = []
                elements = item.get("itemListElement") or []
                for element in elements:
                    if not isinstance(element, dict):
                        continue
                    name = clean_text(element.get("name"))
                    if not name and isinstance(element.get("item"), dict):
                        name = clean_text(element["item"].get("name"))
                    if name and name.lower() not in {"accueil", "home"}:
                        crumbs.append(name)
                if crumbs:
                    return crumbs

        tree = HTMLParser(html)
        crumbs = []
        for node in tree.css(".breadcrumb li, nav.breadcrumb li, .breadcrumb a, nav.breadcrumb a"):
            text = clean_text(node.text(strip=True))
            if text and text.lower() not in {"accueil", "home"} and text not in crumbs:
                crumbs.append(text)
        return crumbs

    async def _enrich_category(self, url: str, fallback_name: str) -> Optional[dict]:
        meta = await self.fetch_html_with_meta(url)
        html = meta.get("html")
        if not html:
            self.logger.debug(
                f"Category skipped url={url} error={meta.get('error')} status={meta.get('status_code')}"
            )
            return None

        breadcrumbs = self._extract_breadcrumbs(html)
        if not breadcrumbs:
            breadcrumbs = [fallback_name or self._category_name_from_url(url)]

        product_count = len(self.extract_products_from_html(html))
        return {
            "url": meta.get("final_url") or url,
            "breadcrumbs": breadcrumbs,
            "product_count": product_count,
        }

    async def _enrich_categories(self, links: List[Tuple[str, str]]) -> List[dict]:
        sem = asyncio.Semaphore(8)

        async def worker(url: str, name: str) -> Optional[dict]:
            async with sem:
                return await self._enrich_category(url, name)

        results = await asyncio.gather(
            *[worker(url, name) for url, name in links],
            return_exceptions=True,
        )

        records = []
        for result in results:
            if isinstance(result, Exception):
                self.logger.debug(f"Category enrichment failed: {result}")
            elif result:
                records.append(result)
        return records

    def _get_or_create_top(self, categories: List[dict], name: str) -> dict:
        for top in categories:
            if top["name"].lower() == name.lower():
                return top
        top = {"name": name, "url": None, "level": "top", "low_level_categories": []}
        categories.append(top)
        return top

    def _get_or_create_low(self, top: dict, name: str) -> dict:
        for low in top["low_level_categories"]:
            if low["name"].lower() == name.lower():
                return low
        low = {"name": name, "url": None, "level": "low", "subcategories": []}
        top["low_level_categories"].append(low)
        return low

    def _build_category_hierarchy(self, records: List[dict]) -> List[dict]:
        categories: List[dict] = []
        seen_urls = set()

        for record in records:
            url = normalize_url(record.get("url")) or record.get("url")
            if not self._is_category_url(url) or url in seen_urls:
                continue
            seen_urls.add(url)

            crumbs = [clean_text(c) for c in record.get("breadcrumbs", []) if clean_text(c)]
            if not crumbs:
                crumbs = [self._category_name_from_url(url)]

            top = self._get_or_create_top(categories, crumbs[0])
            if len(crumbs) == 1:
                top["url"] = top.get("url") or url
                continue

            low = self._get_or_create_low(top, crumbs[1])
            if len(crumbs) == 2:
                low["url"] = low.get("url") or url
                continue

            sub_name = crumbs[-1]
            if not any(s.get("url") == url for s in low["subcategories"]):
                low["subcategories"].append(
                    {"name": sub_name, "url": url, "level": "subcategory"}
                )

        return categories

    def _stats_for_categories(self, categories: List[dict]) -> Dict[str, int]:
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

    async def scrape_categories_async(self) -> dict:
        """Build a breadcrumb-aware hierarchy from homepage links and sitemap URLs."""
        html_path = self.html_dir / "frontpage.html"
        if not html_path.exists():
            raise FileNotFoundError(f"Frontpage not found: {html_path}")

        html = html_path.read_text(encoding="utf-8")
        link_map: Dict[str, str] = {}
        for url, name in self._category_links_from_html(html, self.base_url):
            link_map.setdefault(url, name)

        sitemap_path = self.html_dir / "sitemap.xml"
        sitemap_text = sitemap_path.read_text(encoding="utf-8") if sitemap_path.exists() else ""
        if not sitemap_text:
            sitemap_url = self.selectors.get("frontpage", {}).get(
                "sitemap_url", f"{self.base_url.rstrip('/')}/sitemap.xml"
            )
            meta = await self.fetch_html_with_meta(sitemap_url)
            sitemap_text = meta.get("html") or ""
            if sitemap_text:
                save_text_atomic(sitemap_text, sitemap_path, self.logger)

        for url, name in self._category_links_from_sitemap(sitemap_text):
            link_map.setdefault(url, name)

        links = list(link_map.items())
        if not links:
            data = self.extract_categories_from_html(html)
        else:
            self.logger.info(f"Enriching {len(links)} Tunisiatech category URLs")
            records = await self._enrich_categories(links)
            categories = self._build_category_hierarchy(records)
            data = {"categories": categories, "stats": self._stats_for_categories(categories)}

        data["site"] = self.site_name
        data["shop"] = self.site_name
        data["base_url"] = self.base_url
        data["extracted_at"] = datetime.now().isoformat()
        data["date"] = get_date_folder()

        output_path = self.data_dir / "categories.json"
        save_json(data, output_path, self.logger)
        self.logger.info(
            "Categories saved: "
            f"{data['stats'].get('top_level', 0)} top, "
            f"{data['stats'].get('low_level', 0)} low, "
            f"{data['stats'].get('subcategory', 0)} sub"
        )
        return data

    # ------------------------------------------------------------------
    # Product listings
    # ------------------------------------------------------------------

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

    def _image_from_item(self, item: Any) -> Optional[str]:
        cp = self.selectors.get("category_page", {})
        img = item.css_first(cp.get("item_image", ".product-thumbnail img"))
        attrs = cp.get("item_image_attrs", ["data-original", "data-src", "src"])
        src = self._first_attr(img, attrs)
        if src and not src.startswith("data:"):
            return self._abs(src)
        return None

    def _discount_percent(self, value: Any, price: Optional[float], old_price: Optional[float]) -> Optional[float]:
        text = clean_text(value)
        if text:
            match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
            if match:
                return parse_price(match.group(1))
        if price is not None and old_price and old_price > price:
            return round((1 - price / old_price) * 100, 2)
        return None

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        item_selector = cp.get("item_selector", "#js-product-list div.product-miniature.js-product-miniature")
        items = tree.css(item_selector)
        if not items:
            product_list = tree.css_first("#js-product-list")
            items = product_list.css("[data-id-product]") if product_list else []

        products = []
        for item in items:
            product_id = clean_text(item.attributes.get(cp.get("item_id_attr", "data-id-product")))
            link = item.css_first(cp.get("item_url", "a.product-cover-link, .product-name a"))
            product_url = self._abs(link.attributes.get("href") if link else None)
            if product_url:
                product_url = normalize_url(product_url) or product_url

            name_node = item.css_first(cp.get("item_name", ".product-name a"))
            name = clean_text(
                (name_node.attributes.get("title") if name_node else None)
                or (name_node.text(strip=True) if name_node else None)
            )
            if not name and link:
                name = clean_text(link.attributes.get("title") or link.text(strip=True))

            if not product_id and not product_url:
                continue

            price_node = item.css_first(cp.get("item_price", ".price.product-price"))
            price = parse_price(
                self._first_attr(price_node, ["content", "data-price"])
                or (price_node.text(strip=True) if price_node else None)
            )

            old_node = item.css_first(cp.get("item_old_price", ".regular-price"))
            old_price = parse_price(
                self._first_attr(old_node, ["content", "data-price"])
                or (old_node.text(strip=True) if old_node else None)
            )

            discount_node = item.css_first(cp.get("item_discount", ".product-flag.discount"))
            discount_percent = self._discount_percent(
                discount_node.text(strip=True) if discount_node else None,
                price,
                old_price,
            )

            availability_node = item.css_first(cp.get("item_availability", ".product-availability span"))
            availability_text, available = availability_from_text(
                availability_node.text(strip=True) if availability_node else None
            )
            availability_class = (
                availability_node.attributes.get("class", "").lower()
                if availability_node
                else ""
            )
            if available is None and "available" in availability_class and "unavailable" not in availability_class:
                available = True

            short_node = item.css_first(cp.get("item_short_description", ".product-description-short"))
            product = {
                "id": product_id,
                "product_id": product_id,
                "url": product_url,
                "name": name,
                "price": price,
                "old_price": old_price,
                "discount_percent": discount_percent,
                "image": self._image_from_item(item),
                "availability": availability_text,
                "available": available,
            }
            short_description = clean_text(short_node.text(strip=True)) if short_node else None
            if short_description:
                product["short_description"] = short_description

            products.append(finalize_product_record({k: v for k, v in product.items() if v is not None}))

        return dedupe_products(products, self.logger, "tunisiatech listing")

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        current_page = 1
        has_next = False

        next_link = tree.css_first("a.next.js-search-link, a[rel='next'], link[rel='next']")
        if next_link:
            classes = next_link.attributes.get("class", "").lower()
            has_next = "disabled" not in classes

        for link in tree.css(".pagination a.js-search-link, .pagination a, link[rel='next'], link[rel='prev']"):
            text = clean_text(link.text(strip=True))
            if text and text.isdigit():
                max_page = max(max_page, int(text))
            href = link.attributes.get("href", "")
            page_match = re.search(r"[?&]page=(\d+)", href)
            if page_match:
                max_page = max(max_page, int(page_match.group(1)))

        current = tree.css_first(".pagination .current a, .pagination li.current a, .pagination .active a, .pagination li.current")
        if current:
            text = clean_text(current.text(strip=True))
            if text and text.isdigit():
                current_page = int(text)

        return {
            "current_page": current_page,
            "total_pages": max_page,
            "has_next": has_next,
        }

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    def _product_json(self, tree: HTMLParser) -> Dict[str, Any]:
        node = tree.css_first("#product-details[data-product], .js-product-details[data-product]")
        if not node:
            return {}
        raw = node.attributes.get("data-product")
        if not raw:
            return {}
        try:
            return json.loads(html_lib.unescape(raw))
        except Exception:
            try:
                return json.loads(raw)
            except Exception:
                return {}

    def _images_from_product_json(self, payload: Dict[str, Any]) -> List[str]:
        images = []
        for image in payload.get("images") or []:
            candidates = []
            if isinstance(image, dict):
                by_size = image.get("bySize") or {}
                for size in ("large_default", "medium_default", "home_default"):
                    sized = by_size.get(size)
                    if isinstance(sized, dict):
                        candidates.append(sized.get("url"))
                        sources = sized.get("sources") or {}
                        if isinstance(sources, dict):
                            candidates.extend(sources.values())
                candidates.extend([image.get("large"), image.get("medium"), image.get("url")])
            elif isinstance(image, str):
                candidates.append(image)
            for candidate in candidates:
                url = self._abs(candidate)
                if url and url not in images:
                    images.append(url)
                    break
        return images

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
        discount = self._discount_percent(payload.get("discount_percentage"), price, old_price)
        if discount is not None:
            data["discount_percent"] = discount

        availability_text = clean_text(
            payload.get("availability_message")
            or payload.get("available_now")
            or payload.get("availability")
        )
        schema_availability = clean_text(payload.get("availability"))
        avail_from_text, available = availability_from_text(availability_text or schema_availability)
        if availability_text or avail_from_text:
            data["availability"] = availability_text or avail_from_text
        if available is not None:
            data["available"] = available
        elif schema_availability:
            data["available"] = schema_availability.lower() == "available"

        brand = clean_text(payload.get("manufacturer_name"))
        if brand:
            data["brand"] = brand

        short_description = self._clean_html_fragment(payload.get("description_short"))
        description = self._clean_html_fragment(payload.get("description"))
        if short_description:
            data["short_description"] = short_description
        if description:
            data["description"] = description

        images = self._images_from_product_json(payload)
        if images:
            data["images"] = images
            data["image"] = images[0]

        return data

    def _extract_specs(self, tree: HTMLParser, payload: Dict[str, Any]) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for block in tree.css(".product-features dl.data-sheet, .product-features"):
            keys = block.css("dt")
            values = block.css("dd")
            for key_node, value_node in zip(keys, values):
                key = clean_text(key_node.text(strip=True))
                value = clean_text(value_node.text(strip=True))
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
        images = []
        for img in tree.css(".product-cover img, .images-container img, .product-images img, img.thumb.js-thumb, img[itemprop='image']"):
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

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        sample_path = self.html_dir / "detail_sample_1.html"
        if not sample_path.exists():
            save_text_atomic(html, sample_path, self.logger)

        tree = HTMLParser(html)
        payload = self._product_json(tree)

        data: Dict[str, Any] = {"url": normalize_url(url) or url}
        data.update(self._data_from_product_json(payload))

        product_id_match = self.PRODUCT_RE.search(url)
        if product_id_match:
            data.setdefault("product_id", product_id_match.group(1))
            data.setdefault("id", product_id_match.group(1))

        metadata = html_product_metadata(html, url, self.base_url)
        for key, value in metadata.items():
            if value not in (None, "", [], {}):
                data.setdefault(key, value)

        pp = self.selectors.get("product_page", {})
        title_node = tree.css_first(pp.get("title", "h1.page-heading, h1.h1, h1"))
        if title_node:
            title = clean_text(title_node.text(strip=True))
            data.setdefault("title", title)
            data.setdefault("name", title)

        sku_node = tree.css_first(pp.get("sku", ".product-reference span"))
        sku = clean_text(sku_node.text(strip=True)) if sku_node else None
        if sku:
            if normalize_gtin(sku):
                data.setdefault("barcode", sku)
            else:
                data.setdefault("reference", sku)
                data.setdefault("sku", sku)

        price_node = tree.css_first(pp.get("price", ".current-price .price"))
        price = parse_price(
            self._first_attr(price_node, ["content", "data-price"])
            or (price_node.text(strip=True) if price_node else None)
        )
        if price is not None:
            data.setdefault("price", price)

        old_node = tree.css_first(pp.get("old_price", ".product-prices .regular-price"))
        old_price = parse_price(
            self._first_attr(old_node, ["content", "data-price"])
            or (old_node.text(strip=True) if old_node else None)
        )
        if old_price is not None and old_price > data.get("price", 0):
            data.setdefault("old_price", old_price)
            data.setdefault(
                "discount_percent",
                self._discount_percent(None, data.get("price"), old_price),
            )

        brand_node = tree.css_first(pp.get("brand", ".product-manufacturer img, .product-manufacturer a"))
        if brand_node:
            brand = clean_text(
                brand_node.attributes.get(pp.get("brand_attr", "alt"))
                or brand_node.attributes.get("title")
                or brand_node.text(strip=True)
            )
            if brand:
                data.setdefault("brand", brand)
            logo = self._first_attr(brand_node, ["src", "data-src"])
            if logo:
                data.setdefault("brand_logo", self._abs(logo))

        availability_node = tree.css_first(pp.get("availability", "#product-availability"))
        availability_text, available = availability_from_text(
            availability_node.text(strip=True) if availability_node else None
        )
        if availability_text:
            data.setdefault("availability", availability_text)
        if available is not None:
            data.setdefault("available", available)

        desc_node = tree.css_first(pp.get("description", ".product-description"))
        short_node = tree.css_first(pp.get("short_description", "div[id^='product-description-short-']"))
        if short_node:
            data.setdefault("short_description", clean_text(short_node.text(strip=True)))
        if desc_node:
            data.setdefault("description", clean_text(desc_node.text(strip=True)))

        specs = self._extract_specs(tree, payload)
        if specs:
            data["specifications"] = specs

        breadcrumbs = self._extract_breadcrumbs(html)
        if breadcrumbs:
            data["breadcrumbs"] = breadcrumbs

        images = data.get("images") or []
        for image in self._extract_images_from_html(tree):
            if image not in images:
                images.append(image)
        if images:
            data["images"] = images
            data.setdefault("image", images[0])

        return finalize_product_record({k: v for k, v in data.items() if v is not None})


def get_scraper(logger: logging.Logger) -> TunisiaTechScraper:
    return TunisiaTechScraper(logger)
