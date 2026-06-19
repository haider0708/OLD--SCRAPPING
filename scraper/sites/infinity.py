#!/usr/bin/env python3
"""
My Infinity scraper - WordPress/WooCommerce Store API with DOM fallback.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import math
import re
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlsplit, urlunparse, urlunsplit

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, get_date_folder, save_json, save_text_atomic
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    clean_text,
    dedupe_products,
    extract_gtins_from_text,
    finalize_product_record,
    html_product_metadata,
    normalize_url,
    parse_price,
)


class InfinityScraper(FastScraper):
    """HTTP scraper for the My Infinity WooCommerce storefront."""

    PAYLOAD_SELECTOR = "script#infinity-listing-data"
    CATEGORY_SLUGS = {
        "montres-homme": {
            "name": "Montres Homme",
            "category_id": "16",
        },
        "montres-femme": {
            "name": "Montres Femme",
            "category_id": "17",
        },
    }

    def __init__(self, logger: logging.Logger):
        super().__init__("infinity", logger)
        self.headers.update(self.config.get("headers", {}))
        self.store_api_base_url = self.config.get(
            "store_api_base_url",
            "https://myinfinity.tn/wp-json/wc/store/v1",
        ).rstrip("/")
        settings = self.config.get("settings", {})
        self.page_size = int(settings.get("page_size", 12))
        self.max_pages = int(settings.get("max_pages", 100))
        self._products_by_slug: Dict[str, Dict[str, Any]] = {}
        self._products_by_id: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _abs(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

    def _store_api_url(self, endpoint: str) -> str:
        return f"{self.store_api_base_url}/{endpoint.lstrip('/')}"

    async def _store_api_get(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        referer: Optional[str] = None,
    ) -> Tuple[Any, Dict[str, str]]:
        client = await self.get_client()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": referer or self.base_url,
        }
        response = await client.get(self._store_api_url(endpoint), headers=headers, params=params)
        response.raise_for_status()
        return response.json(), dict(response.headers)

    @staticmethod
    def _text(node: Any) -> Optional[str]:
        if not node:
            return None
        try:
            return clean_text(node.text(separator=" ", strip=True))
        except TypeError:
            return clean_text(node.text(strip=True))

    @staticmethod
    def _attr(node: Any, name: str) -> Optional[str]:
        if not node:
            return None
        return clean_text(node.attributes.get(name))

    @staticmethod
    def _first(root: Any, selectors: Iterable[str]) -> Any:
        for selector in selectors:
            if not selector:
                continue
            node = root.css_first(selector)
            if node:
                return node
        return None

    @staticmethod
    def _dedupe_values(values: Iterable[Any]) -> List[str]:
        seen = set()
        out = []
        for value in values:
            text = clean_text(value)
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out

    @staticmethod
    def _html_to_text(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if "<" not in text or ">" not in text:
            return text
        try:
            return clean_text(HTMLParser(f"<div>{text}</div>").text(separator=" ", strip=True))
        except Exception:
            return clean_text(re.sub(r"<[^>]+>", " ", text))

    @staticmethod
    def _same_token(left: Any, right: Any) -> bool:
        a = re.sub(r"[^a-z0-9]", "", str(left or "").lower())
        b = re.sub(r"[^a-z0-9]", "", str(right or "").lower())
        return bool(a and b and a == b)

    def _meaningful_reference(self, value: Any, title: Any = None) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"^(r[e\u00e9]f[e\u00e9]rence|reference|sku)\s*:?", "", text, flags=re.I)
        text = clean_text(text.strip(" :;|-"))
        if not text:
            return None
        if title and self._same_token(text, title):
            return None
        if len(text) > 80:
            return None
        return text

    def _category_url(self, slug: str) -> str:
        return urljoin(self.base_url, f"/product-category/{slug}/")

    def _category_from_url(self, url: str) -> Optional[Dict[str, str]]:
        parsed = urlsplit(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0].lower() != "product-category":
            return None
        slug = parts[1].lower()
        configured = self.CATEGORY_SLUGS.get(slug)
        if not configured:
            return None
        return {"slug": slug, **configured}

    def _category_page_from_url(self, url: str) -> int:
        parsed = urlsplit(url)
        parts = [part for part in parsed.path.split("/") if part]
        for idx, part in enumerate(parts):
            if part.lower() == "page" and idx + 1 < len(parts):
                try:
                    return max(1, int(parts[idx + 1]))
                except (TypeError, ValueError):
                    return 1
        raw = parse_qs(parsed.query).get("page", ["1"])[0]
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _product_url(self, value: Any) -> Optional[str]:
        url = self._abs(value, self.base_url)
        if not url:
            return None
        parsed = urlsplit(url)
        if parsed.netloc.lower() not in {"myinfinity.tn", "www.myinfinity.tn"}:
            return None
        path = parsed.path.rstrip("/")
        if not path.startswith("/product/"):
            return None
        return normalize_url(urlunsplit((parsed.scheme, parsed.netloc, path + "/", "", ""))) or url

    def _slug_from_product_url(self, url: str) -> Optional[str]:
        parsed = urlsplit(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0].lower() == "product":
            return clean_text(parts[1])
        return None

    @staticmethod
    def _body_class_id(tree: HTMLParser) -> Optional[str]:
        body = tree.css_first("body")
        classes = body.attributes.get("class", "") if body else ""
        match = re.search(r"(?:^|\s)postid-(\d+)(?:\s|$)", classes)
        return match.group(1) if match else None

    @staticmethod
    def _card_class_id(card: Any) -> Optional[str]:
        classes = card.attributes.get("class", "") if card else ""
        match = re.search(r"(?:^|\s)post-(\d+)(?:\s|$)", classes)
        return match.group(1) if match else None

    def _image_from_node(self, node: Any) -> Optional[str]:
        if not node:
            return None

        def clean_image(value: Any) -> Optional[str]:
            text = clean_text(value)
            if not text or text.startswith("data:"):
                return None
            if "," in text and " " in text:
                candidates = [part.strip().split(" ")[0] for part in text.split(",")]
                for candidate in reversed(candidates):
                    url = self._abs(candidate, self.base_url)
                    if url:
                        return url
                return None
            return self._abs(text, self.base_url)

        for attr in (
            "data-large_image",
            "data-src",
            "data-lazy-src",
            "data-o_src",
            "srcset",
            "data-srcset",
            "src",
            "content",
        ):
            url = clean_image(node.attributes.get(attr))
            if url:
                return url
        return None

    @staticmethod
    def _price_value(value: Any, minor_unit: int = 0) -> Optional[float]:
        price = parse_price(value)
        if price is None:
            return None
        if minor_unit and minor_unit > 0:
            return round(price / (10**minor_unit), minor_unit)
        return price

    def _price_tuple_from_api(self, product: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        prices = product.get("prices") if isinstance(product.get("prices"), dict) else {}
        minor_unit = int(prices.get("currency_minor_unit") or 0)
        current = self._price_value(prices.get("price") or prices.get("sale_price"), minor_unit)
        regular = self._price_value(prices.get("regular_price"), minor_unit)
        sale = self._price_value(prices.get("sale_price"), minor_unit)
        if sale is not None and product.get("on_sale"):
            current = sale
        old_price = regular if regular is not None and current is not None and regular > current else None
        discount = self._computed_discount(current, old_price)
        return current, old_price, discount

    @staticmethod
    def _computed_discount(price: Optional[float], old_price: Optional[float]) -> Optional[int]:
        if price is None or old_price is None or old_price <= 0 or old_price <= price:
            return None
        return int(round((1 - (price / old_price)) * 100))

    def _price_from_node(self, node: Any) -> Optional[float]:
        if not node:
            return None
        return parse_price(
            self._attr(node, "content")
            or self._attr(node, "value")
            or self._text(node)
        )

    def _price_pair_from_dom(
        self,
        root: Any,
        current_selectors: Iterable[str],
        old_selectors: Iterable[str],
        fallback_selectors: Iterable[str],
    ) -> Tuple[Optional[float], Optional[float]]:
        current = None
        old = None
        for selector in current_selectors:
            current = self._price_from_node(root.css_first(selector))
            if current is not None:
                break
        for selector in old_selectors:
            old = self._price_from_node(root.css_first(selector))
            if old is not None:
                break
        if current is None:
            for selector in fallback_selectors:
                node = root.css_first(selector)
                if not node or node.css_first("ins") or node.css_first("del"):
                    continue
                current = self._price_from_node(node)
                if current is not None:
                    break
        return current, old

    def _availability_from_api(self, product: Dict[str, Any]) -> Tuple[Optional[str], Optional[bool]]:
        stock = product.get("stock_availability") if isinstance(product.get("stock_availability"), dict) else {}
        text = clean_text(stock.get("text") or stock.get("class"))
        if product.get("is_in_stock") is True:
            if text and text.lower() in {"in-stock", "instock", "in stock"}:
                text = "En stock"
            return text or "En stock", True
        if product.get("is_in_stock") is False:
            if text and text.lower() in {"out-of-stock", "outofstock", "out of stock"}:
                text = "Rupture de stock"
            return text or "Rupture de stock", False
        if product.get("is_on_backorder") is True:
            return text or "Sur commande", True
        return availability_from_text(text)

    def _availability_from_dom(
        self,
        text: Any = None,
        classes: Any = "",
        add_button: Any = None,
    ) -> Tuple[Optional[str], Optional[bool]]:
        stock_text = clean_text(text)
        cls = str(classes or "").lower()
        combined = f"{stock_text or ''} {cls}".lower()
        if "outofstock" in combined or "out-of-stock" in combined or "rupture" in combined:
            return stock_text or "Rupture de stock", False
        if "instock" in combined or "in-stock" in combined or "en stock" in combined:
            return stock_text or "En stock", True

        if add_button:
            disabled = self._attr(add_button, "disabled") or self._attr(add_button, "aria-disabled")
            button_text = self._text(add_button)
            if disabled:
                return button_text or stock_text or "Rupture de stock", False
            if button_text and re.search(r"ajouter|add to cart|commander", button_text, re.I):
                return stock_text or "En stock", True

        return availability_from_text(stock_text)

    def _images_from_api(self, product: Dict[str, Any]) -> List[str]:
        urls: List[str] = []
        for image in product.get("images") or []:
            if not isinstance(image, dict):
                url = self._abs(image, self.base_url)
                if url:
                    urls.append(url)
                continue
            for key in ("src", "thumbnail", "url"):
                url = self._abs(image.get(key), self.base_url)
                if url:
                    urls.append(url)
                    break
        return self._dedupe_values(urls)

    def _terms_from_attribute(self, attribute: Dict[str, Any]) -> List[str]:
        terms = []
        for term in attribute.get("terms") or []:
            if isinstance(term, dict):
                value = clean_text(term.get("name") or term.get("slug"))
                if value:
                    terms.append(value)
            else:
                value = clean_text(term)
                if value:
                    terms.append(value)
        return terms

    def _specs_from_api(self, product: Dict[str, Any]) -> Dict[str, Any]:
        specs: Dict[str, Any] = {}
        specs.update(self._specs_from_description(self._html_to_text(product.get("short_description"))))
        for attribute in product.get("attributes") or []:
            if not isinstance(attribute, dict):
                continue
            name = clean_text(attribute.get("name"))
            terms = self._terms_from_attribute(attribute)
            if name and terms:
                specs[name] = ", ".join(terms)
        return {k: v for k, v in specs.items() if v not in (None, "", [], {})}

    def _specs_from_tables(self, tree: HTMLParser) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for row in tree.css("table.shop_attributes tr, table.woocommerce-product-attributes tr"):
            cells = row.css("th, td")
            if len(cells) < 2:
                continue
            label = clean_text(self._text(cells[0]).rstrip(":") if self._text(cells[0]) else None)
            value = self._text(cells[-1])
            if label and value:
                specs[label] = value
        return specs

    def _specs_from_description(self, text: Any) -> Dict[str, str]:
        source = clean_text(text)
        if not source:
            return {}
        specs: Dict[str, str] = {}
        labels = [
            "Marque",
            "Référence",
            "Reference",
            "Fonctionnalités",
            "Fonctionnalité",
            "Mouvement",
            "Matière du boîtier",
            "Diamètre du boîtier",
            "Matière du bracelet",
            "Largeur du bracelet",
            "Type de verre",
            "Garantie",
        ]
        label_pattern = "|".join(re.escape(label) for label in labels)
        pattern = re.compile(
            rf"({label_pattern})\s*:\s*(.*?)(?=\s+(?:{label_pattern})\s*:|$)",
            flags=re.I | re.S,
        )
        for match in pattern.finditer(source):
            label = clean_text(match.group(1))
            value = clean_text(match.group(2).strip(" .;:-"))
            if not label or not value:
                continue
            if re.search(r"r[e\u00e9]f[e\u00e9]rence|reference", label, re.I):
                label = "Reference"
            if "marque" in label.lower():
                label = "Marque"
            specs[label] = value
        return specs

    def _brand_from_product(self, product: Dict[str, Any], specs: Optional[Dict[str, Any]] = None) -> Optional[str]:
        for brand in product.get("brands") or []:
            if isinstance(brand, dict):
                value = clean_text(brand.get("name") or brand.get("slug"))
                if value:
                    return value
        for attribute in product.get("attributes") or []:
            if not isinstance(attribute, dict):
                continue
            if clean_text(attribute.get("name")) and "marque" in clean_text(attribute.get("name")).lower():
                terms = self._terms_from_attribute(attribute)
                if terms:
                    return terms[0]
        specs = specs or {}
        for key, value in specs.items():
            if "marque" in key.lower():
                return clean_text(value)
        text = " ".join(
            value or ""
            for value in (
                self._html_to_text(product.get("short_description")),
                self._html_to_text(product.get("description")),
            )
        )
        match = re.search(r"marque\s*:\s*([A-Z0-9][A-Z0-9 ._-]{1,40})", text, flags=re.I)
        return clean_text(match.group(1)) if match else None

    def _reference_from_product(self, product: Dict[str, Any], specs: Optional[Dict[str, Any]] = None) -> Optional[str]:
        title = product.get("name")
        sku = self._meaningful_reference(product.get("sku"), title)
        if sku:
            return sku
        specs = specs or {}
        for key, value in specs.items():
            if re.search(r"r[e\u00e9]f[e\u00e9]rence|reference|sku", key, re.I):
                reference = self._meaningful_reference(value, title)
                if reference:
                    return reference
        text = " ".join(
            value or ""
            for value in (
                self._html_to_text(product.get("short_description")),
                self._html_to_text(product.get("description")),
            )
        )
        match = re.search(
            r"r[e\u00e9]f[e\u00e9]rence\s*:?\s*([A-Z0-9][A-Z0-9 ./_-]{1,50})",
            text,
            flags=re.I,
        )
        if not match:
            return None
        raw = re.split(
            r"\s+(?:Fonctionnalit[e\u00e9]s?|Mouvement|Mati[e\u00e8]re|Diam[e\u00e8]tre|Largeur|Type|Garantie)\s*:",
            match.group(1),
            flags=re.I,
        )[0]
        return self._meaningful_reference(raw, title)

    def _categories_from_api(self, product: Dict[str, Any]) -> List[Dict[str, str]]:
        categories = []
        for item in product.get("categories") or []:
            if not isinstance(item, dict):
                continue
            name = clean_text(item.get("name"))
            link = self._abs(item.get("link"), self.base_url)
            slug = clean_text(item.get("slug"))
            if not name:
                continue
            categories.append(
                {
                    "id": clean_text(item.get("id")),
                    "name": name,
                    "slug": slug,
                    "url": link,
                }
            )
        return categories

    def _product_from_api(self, product: Dict[str, Any]) -> Dict[str, Any]:
        price, old_price, discount_percent = self._price_tuple_from_api(product)
        images = self._images_from_api(product)
        availability, available = self._availability_from_api(product)
        specs = self._specs_from_api(product)
        brand = self._brand_from_product(product, specs)
        reference = self._reference_from_product(product, specs)
        product_id = clean_text(product.get("id"))
        url = self._product_url(product.get("permalink"))
        description = self._html_to_text(product.get("description"))
        short_description = self._html_to_text(product.get("short_description"))
        categories = self._categories_from_api(product)
        tags = [
            {
                "id": clean_text(item.get("id")),
                "name": clean_text(item.get("name")),
                "slug": clean_text(item.get("slug")),
                "url": self._abs(item.get("link"), self.base_url),
            }
            for item in product.get("tags") or []
            if isinstance(item, dict) and clean_text(item.get("name"))
        ]

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "url": url,
            "name": clean_text(product.get("name")),
            "title": clean_text(product.get("name")),
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": images[0] if images else None,
            "images": images,
            "reference": reference,
            "sku": reference,
            "brand": brand,
            "availability": availability,
            "available": available,
            "short_description": short_description,
            "description": description or short_description,
            "full_description": description or short_description,
            "description_html": clean_text(product.get("description")),
            "short_description_html": clean_text(product.get("short_description")),
            "specifications": specs,
            "specs": specs,
            "categories": categories,
            "breadcrumbs": categories,
            "tags": tags,
            "on_sale": product.get("on_sale"),
            "average_rating": parse_price(product.get("average_rating")),
            "review_count": product.get("review_count"),
            "attributes": product.get("attributes") or None,
        }
        gtins = extract_gtins_from_text(
            " ".join(
                str(value or "")
                for value in (
                    product.get("sku"),
                    product.get("short_description"),
                    product.get("description"),
                    json.dumps(specs, ensure_ascii=False),
                )
            )
        )
        if gtins:
            record["barcode"] = gtins[0]

        final = finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})
        slug = clean_text(product.get("slug"))
        if slug:
            self._products_by_slug[slug] = product
        if product_id:
            self._products_by_id[product_id] = product
        return final

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        output_path = await super().download_frontpage()
        try:
            payload, _headers = await self._store_api_get("/products/categories")
            save_text_atomic(
                json.dumps(payload, ensure_ascii=False, indent=2),
                self.html_dir / "categories_api.json",
                self.logger,
            )
        except Exception as exc:
            self.logger.debug(f"Infinity categories API evidence fetch failed: {exc}")
        return output_path

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        discovered = set()
        for link in tree.css("a[href*='/product-category/montres-homme/'], a[href*='/product-category/montres-femme/']"):
            href = self._abs(link.attributes.get("href"), self.base_url)
            category = self._category_from_url(href or "")
            if category:
                discovered.add(category["slug"])

        api_categories = {}
        api_path = self.html_dir / "categories_api.json"
        if api_path.exists():
            try:
                payload = json.loads(api_path.read_text(encoding="utf-8"))
                for item in payload if isinstance(payload, list) else []:
                    if isinstance(item, dict):
                        api_categories[clean_text(item.get("slug"))] = item
            except Exception:
                api_categories = {}

        categories = []
        seen = set()
        for item in self.config.get("fixed_categories") or []:
            slug = clean_text(item.get("slug"))
            if slug not in self.CATEGORY_SLUGS or slug in seen:
                continue
            seen.add(slug)
            api_item = api_categories.get(slug) or {}
            configured = self.CATEGORY_SLUGS[slug]
            categories.append(
                {
                    "name": clean_text(api_item.get("name")) or clean_text(item.get("name")) or configured["name"],
                    "url": self._category_url(slug),
                    "category_id": clean_text(api_item.get("id")) or clean_text(item.get("category_id")) or configured["category_id"],
                    "slug": slug,
                    "low_level_categories": [],
                    "discovery_method": "fixed_woocommerce_category",
                    "validated_on_frontpage": slug in discovered,
                    "validated_by_api": bool(api_item),
                    "product_count_hint": api_item.get("count"),
                }
            )

        return {
            "categories": categories,
            "stats": {
                "top_level": len(categories),
                "low_level": 0,
                "subcategory": 0,
                "total_urls": len(categories),
            },
        }

    async def scrape_categories_async(self) -> dict:
        await self.download_frontpage()
        data = self.extract_categories_from_html("")
        data["site"] = self.site_name
        data["shop"] = self.site_name
        data["base_url"] = self.base_url
        data["extracted_at"] = datetime.now().isoformat()
        data["date"] = get_date_folder()
        save_json(data, self.data_dir / "categories.json", self.logger)
        return data

    # ------------------------------------------------------------------
    # Listing API bridge
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        path = re.sub(r"/page/\d+/?$", "/", parts.path)
        path = path.rstrip("/") + "/"
        query_items = []
        for key, values in parse_qs(parts.query, keep_blank_values=True).items():
            if key.lower() in {"page", "paged", "product-page", "srsltid"} or key.lower().startswith("utm_"):
                continue
            for value in values:
                query_items.append((key, value))
        if page_num and page_num > 1:
            path = path.rstrip("/") + f"/page/{page_num}/"
        return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query_items), ""))

    async def _fetch_listing_payload(self, url: str) -> Dict[str, Any]:
        category = self._category_from_url(url)
        page = self._category_page_from_url(url)
        if not category:
            return {
                "__infinity_listing__": True,
                "url": url,
                "page": page,
                "per_page": self.page_size,
                "total": 0,
                "total_pages": 1,
                "products": [],
                "error": "not_category_url",
            }
        try:
            payload, headers = await self._store_api_get(
                "/products",
                {
                    "category": category["slug"],
                    "per_page": self.page_size,
                    "page": page,
                },
                referer=url,
            )
            products = payload if isinstance(payload, list) else []
            total = int(headers.get("x-wp-total") or headers.get("X-WP-Total") or len(products))
            total_pages = int(headers.get("x-wp-totalpages") or headers.get("X-WP-TotalPages") or 1)
            return {
                "__infinity_listing__": True,
                "url": url,
                "page": page,
                "per_page": self.page_size,
                "total": total,
                "total_pages": total_pages,
                "category": category,
                "products": products,
                "error": None,
            }
        except Exception as exc:
            return {
                "__infinity_listing__": True,
                "url": url,
                "page": page,
                "per_page": self.page_size,
                "total": 0,
                "total_pages": 1,
                "products": [],
                "category": category,
                "error": str(exc) or exc.__class__.__name__,
            }

    def _listing_payload_to_html(self, payload: Dict[str, Any]) -> str:
        cards = []
        for product in payload.get("products") or []:
            if not isinstance(product, dict):
                continue
            record = self._product_from_api(product)
            name = html_lib.escape(record.get("name") or "")
            url = html_lib.escape(record.get("url") or "")
            image = html_lib.escape(record.get("image") or "")
            cards.append(
                "\n".join(
                    [
                        f'<article class="product-card" data-id-product="{html_lib.escape(record.get("product_id") or "")}">',
                        f'  <a class="product-link" href="{url}"><img class="product-image" src="{image}" alt="{name}"></a>',
                        f'  <h2 class="product-title">{name}</h2>',
                        f'  <span class="price">{record.get("price", "")}</span>',
                        f'  <span class="old-price">{record.get("old_price", "")}</span>',
                        f'  <span class="discount">{record.get("discount_percent", "")}</span>',
                        f'  <span class="brand">{html_lib.escape(record.get("brand") or "")}</span>',
                        f'  <span class="reference">{html_lib.escape(record.get("reference") or "")}</span>',
                        f'  <span class="availability" data-available="{str(record.get("available")).lower()}">{html_lib.escape(record.get("availability") or "")}</span>',
                        "</article>",
                    ]
                )
            )

        payload_json = html_lib.escape(json.dumps(payload, ensure_ascii=False), quote=False)
        return (
            "<!doctype html><html><body>"
            f'<script id="infinity-listing-data" type="application/json">{payload_json}</script>'
            '<section id="infinity-products">'
            + "\n".join(cards)
            + "</section></body></html>"
        )

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> Dict[str, Any]:
        if self._category_from_url(url):
            started = time.monotonic()
            payload = await self._fetch_listing_payload(url)
            if not payload.get("error"):
                html = self._listing_payload_to_html(payload)
                if payload.get("products") and not (self.html_dir / "listing_sample_1.html").exists():
                    save_text_atomic(html, self.html_dir / "listing_sample_1.html", self.logger)
                return {
                    "html": html,
                    "status_code": 200,
                    "final_url": url,
                    "content_type": "text/html; charset=utf-8",
                    "content_encoding": None,
                    "attempts": 1,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "blocked_signals": [],
                    "error": None,
                }
            self.logger.debug(f"Infinity Store API listing failed, using DOM fallback: {payload.get('error')}")
        return await super().fetch_html_with_meta(url, raise_on_error=raise_on_error)

    def _listing_data_from_html(self, html: str) -> Dict[str, Any]:
        try:
            data = json.loads(html)
            if isinstance(data, dict) and data.get("__infinity_listing__"):
                return data
        except Exception:
            pass

        tree = HTMLParser(html)
        node = tree.css_first(self.PAYLOAD_SELECTOR)
        if not node:
            return {}
        try:
            return json.loads(html_lib.unescape(node.text()))
        except Exception:
            return {}

    def extract_products_from_html(self, html: str) -> List[dict]:
        listing = self._listing_data_from_html(html)
        if listing:
            products = [
                self._product_from_api(product)
                for product in listing.get("products", [])
                if isinstance(product, dict)
            ]
            return dedupe_products(products, self.logger, "infinity listing")

        tree = HTMLParser(html)
        products: List[Dict[str, Any]] = []
        cp = self.selectors.get("category_page", {})
        for card in tree.css(cp.get("product_card") or "ul.products li.product, li.product.type-product, .products .product"):
            link = self._first(
                card,
                [
                    cp.get("product_url", ""),
                    "a.woocommerce-LoopProduct-link[href*='/product/']",
                    "a.woocommerce-loop-product__link[href*='/product/']",
                    "a[href*='/product/']",
                ],
            )
            url = self._product_url(self._attr(link, "href"))
            if not url:
                continue
            title = self._text(self._first(card, [cp.get("product_name", ""), ".woocommerce-loop-product__title", "h2", "h3"])) or self._text(link)
            if not title:
                continue
            id_node = self._first(card, [cp.get("product_id", ""), ".owp-quick-view[data-product_id]", "[data-product_id]", "[data-product-id]"])
            product_id = (
                self._attr(id_node, "data-product_id")
                or self._attr(id_node, "data-product-id")
                or self._attr(id_node, "value")
                or self._card_class_id(card)
            )
            price, old_price = self._price_pair_from_dom(
                card,
                [cp.get("product_current_price", ""), ".price ins .woocommerce-Price-amount", ".price ins", "ins .woocommerce-Price-amount", "ins"],
                [cp.get("product_old_price", ""), ".price del .woocommerce-Price-amount", ".price del", "del .woocommerce-Price-amount", "del"],
                [cp.get("product_price", ""), ".price .woocommerce-Price-amount", ".price"],
            )
            image = self._image_from_node(self._first(card, [cp.get("product_image", ""), "img.woo-entry-image-main", "img.wp-post-image", "img"]))
            brand_node = self._first(card, [cp.get("product_brand", ""), ".product-brand img[alt]", ".product-brand a", ".product-brand"])
            brand = self._attr(brand_node, "alt") or self._text(brand_node)
            availability, available = self._availability_from_dom(
                classes=card.attributes.get("class", ""),
                add_button=self._first(card, ["a.add_to_cart_button", "button", "[data-product_id]"]),
            )
            product = {
                "id": product_id,
                "product_id": product_id,
                "url": url,
                "name": title,
                "title": title,
                "price": price,
                "old_price": old_price,
                "discount_percent": self._computed_discount(price, old_price),
                "image": image,
                "brand": brand,
                "availability": availability,
                "available": available,
            }
            products.append(finalize_product_record({k: v for k, v in product.items() if v not in (None, "", [], {})}))
        return dedupe_products(products, self.logger, "infinity listing fallback")

    def extract_pagination_from_html(self, html: str) -> dict:
        listing = self._listing_data_from_html(html)
        if listing:
            current = int(listing.get("page") or 1)
            total_pages = int(listing.get("total_pages") or current)
            return {
                "current_page": current,
                "total_pages": max(1, total_pages),
                "has_next": current < total_pages,
                "total_products": int(listing.get("total") or 0),
                "method": "store_api_page",
            }

        tree = HTMLParser(html)
        page_numbers = {1}
        current_page = 1
        current = tree.css_first("nav.woocommerce-pagination .page-numbers.current, .woocommerce-pagination .page-numbers.current, .page-numbers.current")
        if current:
            current_text = self._text(current)
            if current_text and current_text.isdigit():
                current_page = int(current_text)
                page_numbers.add(current_page)
        has_next = False
        for link in tree.css("nav.woocommerce-pagination a.page-numbers[href], .woocommerce-pagination a.page-numbers[href], a.page-numbers[href]"):
            text = self._text(link)
            href = self._attr(link, "href") or ""
            classes = link.attributes.get("class", "") or ""
            if "next" in classes.lower():
                has_next = True
            if text and text.isdigit():
                page_numbers.add(int(text))
            match = re.search(r"/page/(\d+)/?", href)
            if match:
                page_numbers.add(int(match.group(1)))
        total_pages = min(max(page_numbers) if page_numbers else current_page, self.max_pages)
        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next or current_page < total_pages,
            "method": "dom",
        }

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        products: List[dict] = []
        page = 1
        while page <= self.max_pages:
            result = await self.scrape_category_page(self.build_page_url(category_url, page))
            if result.get("error"):
                break
            page_products = result.get("products") or []
            if not page_products:
                break
            products.extend(page_products)
            if limit and len(products) >= limit:
                return dedupe_products(products[:limit], self.logger, "infinity limited listing")
            pagination = result.get("pagination") or {}
            if not pagination.get("has_next"):
                break
            page += 1
        deduped = dedupe_products(products, self.logger, "infinity category")
        return deduped[:limit] if limit else deduped

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def _fetch_product_by_slug(self, slug: str, referer: str) -> Optional[Dict[str, Any]]:
        try:
            payload, _headers = await self._store_api_get("/products", {"slug": slug}, referer=referer)
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                return payload[0]
        except Exception as exc:
            self.logger.debug(f"Infinity product slug API failed slug={slug}: {exc}")
        return None

    async def _fetch_product_by_id(self, product_id: str, referer: str) -> Optional[Dict[str, Any]]:
        try:
            payload, _headers = await self._store_api_get(f"/products/{product_id}", referer=referer)
            if isinstance(payload, dict):
                return payload
        except Exception as exc:
            self.logger.debug(f"Infinity product id API failed id={product_id}: {exc}")
        return None

    def _jsonld_products(self, html: str) -> List[Dict[str, Any]]:
        products: List[Dict[str, Any]] = []

        def walk(value: Any) -> Iterable[Any]:
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)

        tree = HTMLParser(html)
        for script in tree.css("script[type='application/ld+json']"):
            raw = script.text()
            if not raw:
                continue
            try:
                parsed = json.loads(html_lib.unescape(raw.strip()))
            except (TypeError, json.JSONDecodeError):
                continue
            for obj in walk(parsed):
                if not isinstance(obj, dict):
                    continue
                item_type = obj.get("@type")
                types = item_type if isinstance(item_type, list) else [item_type]
                if any(str(t).lower() == "product" for t in types):
                    products.append(obj)
        return products

    def _apply_dom_detail_fallbacks(self, record: Dict[str, Any], html: str, url: str) -> Dict[str, Any]:
        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        metadata = html_product_metadata(html, url, self.base_url)
        for key, value in metadata.items():
            if value not in (None, "", [], {}) and key not in record:
                record[key] = value

        title = self._text(self._first(tree, [pp.get("title", ""), "h1.page-header-title", "h2.single-post-title.product_title", "h1.product_title", "h2.product_title", "h1"]))
        if title:
            record["title"] = title
            record["name"] = title

        product_id = self._body_class_id(tree)
        id_node = self._first(tree, [pp.get("product_id", ""), "button[name='add-to-cart'][value]", "form.cart [name='add-to-cart'][value]", "input[name='product_id'][value]", "[data-product_id]", "[data-product-id]"])
        product_id = self._attr(id_node, "value") or self._attr(id_node, "data-product_id") or self._attr(id_node, "data-product-id") or product_id
        if product_id:
            record.setdefault("id", product_id)
            record.setdefault("product_id", product_id)

        if record.get("price") is None:
            price, old_price = self._price_pair_from_dom(
                tree,
                [pp.get("current_price", ""), ".summary .price ins .woocommerce-Price-amount", ".summary .price ins", "p.price ins .woocommerce-Price-amount", "p.price ins"],
                [pp.get("old_price", ""), ".summary .price del .woocommerce-Price-amount", ".summary .price del", "p.price del .woocommerce-Price-amount", "p.price del"],
                [pp.get("price", ""), ".summary .price .woocommerce-Price-amount", "p.price .woocommerce-Price-amount", ".summary .price", "p.price"],
            )
            if price is not None:
                record["price"] = price
            if old_price is not None:
                record["old_price"] = old_price
            discount = self._computed_discount(record.get("price"), record.get("old_price"))
            if discount is not None:
                record["discount_percent"] = discount

        short_node = tree.css_first(pp.get("short_description") or ".woocommerce-product-details__short-description")
        desc_nodes = tree.css(pp.get("description") or "#tab-description, .woocommerce-Tabs-panel--description")
        short_description = self._text(short_node)
        descriptions = self._dedupe_values([self._text(node) for node in desc_nodes])
        description = record.get("description") or (descriptions[0] if descriptions else short_description)
        if short_description:
            record.setdefault("short_description", short_description)
        if description:
            record["description"] = description
            record.setdefault("full_description", description)

        specs = self._specs_from_description(short_description or description)
        specs.update(record.get("specifications") or {})
        specs.update(self._specs_from_tables(tree))
        if specs:
            record["specifications"] = specs
            record["specs"] = specs

        if not record.get("reference"):
            reference = None
            for key, value in specs.items():
                if re.search(r"r[e\u00e9]f[e\u00e9]rence|reference|sku", key, re.I):
                    reference = self._meaningful_reference(value, record.get("title"))
                    if reference:
                        break
            if reference:
                record["reference"] = reference
                record["sku"] = reference

        if not record.get("brand"):
            for key, value in specs.items():
                if "marque" in key.lower():
                    record["brand"] = clean_text(value)
                    break

        availability_node = self._first(tree, [pp.get("availability", ""), ".summary .stock", "p.stock", ".stock"])
        add_button = self._first(tree, ["button[name='add-to-cart']", ".single_add_to_cart_button"])
        body = tree.css_first("body")
        availability, available = self._availability_from_dom(
            text=self._text(availability_node) or record.get("availability"),
            classes=body.attributes.get("class", "") if body else "",
            add_button=add_button,
        )
        if availability:
            record["availability"] = availability
        if available is not None:
            record["available"] = available

        images = list(record.get("images") or [])
        for node in tree.css(pp.get("image_gallery") or ".woocommerce-product-gallery__image img, .woocommerce-product-gallery img, img.wp-post-image"):
            image = self._image_from_node(node)
            if image:
                images.append(image)
        images = self._dedupe_values(images)
        if images:
            record["images"] = images
            record["image"] = images[0]

        if not record.get("breadcrumbs"):
            breadcrumbs = []
            seen = set()
            for link in tree.css(pp.get("breadcrumbs") or ".woocommerce-breadcrumb a, nav.woocommerce-breadcrumb a, .posted_in a[href*='/product-category/']"):
                name = self._text(link)
                href = self._abs(self._attr(link, "href"), self.base_url)
                if not name or name.lower() in {"accueil", "home"}:
                    continue
                key = (name, href)
                if key in seen:
                    continue
                seen.add(key)
                breadcrumbs.append({"name": name, "url": href})
            if breadcrumbs:
                record["breadcrumbs"] = breadcrumbs

        jsonld = self._jsonld_products(html)
        if jsonld and not record.get("brand"):
            brand = jsonld[0].get("brand")
            if isinstance(brand, dict):
                record["brand"] = clean_text(brand.get("name") or brand.get("@id"))
            else:
                record["brand"] = clean_text(brand)

        text_for_gtin = " ".join(str(value or "") for value in (description, json.dumps(specs, ensure_ascii=False)))
        gtins = extract_gtins_from_text(text_for_gtin)
        if gtins:
            record["barcode"] = gtins[0]

        return record

    async def scrape_product_details(self, url: str) -> dict:
        html = await super().fetch_html(url)
        if html and not (self.html_dir / "detail_sample_1.html").exists():
            save_text_atomic(html, self.html_dir / "detail_sample_1.html", self.logger)

        slug = self._slug_from_product_url(url)
        product = None
        if slug:
            product = self._products_by_slug.get(slug) or await self._fetch_product_by_slug(slug, url)

        if product is None and html:
            product_id = self._body_class_id(HTMLParser(html))
            if product_id:
                product = self._products_by_id.get(product_id) or await self._fetch_product_by_id(product_id, url)

        if product:
            record = self._product_from_api(product)
        else:
            record = html_product_metadata(html or "", url, self.base_url) if html else {}
            record["url"] = self._product_url(url) or normalize_url(url) or url
            if slug:
                record["slug"] = slug
            record["error"] = "store_api_product_not_found"

        if html:
            record = self._apply_dom_detail_fallbacks(record, html, url)

        record["url"] = record.get("url") or self._product_url(url) or normalize_url(url) or url
        record["shop"] = self.site_name
        return finalize_product_record({k: v for k, v in record.items() if v not in (None, "", [], {})})


def get_scraper(logger: logging.Logger) -> InfinityScraper:
    """Factory used by scraper.sites registry."""
    return InfinityScraper(logger)
