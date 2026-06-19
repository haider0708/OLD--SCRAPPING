#!/usr/bin/env python3
"""
MyKenza scraper - WordPress/WooCommerce storefront, HTTP/selectolax.
"""

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from selectolax.parser import HTMLParser

from scraper.base import FastScraper, save_text_atomic
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    clean_text,
    dedupe_products,
    extract_gtins_from_text,
    finalize_product_record,
    html_product_metadata,
    parse_price,
)


class MykenzaScraper(FastScraper):
    """HTTP scraper for mykenza.tn WooCommerce pages."""

    CATEGORY_SLUGS = {
        "bijoux-montres": "Montres",
        "lunettes-cadres": "Lunettes",
    }
    CATEGORY_IDS = {
        "bijoux-montres": "271",
        "lunettes-cadres": "277",
    }
    BAD_CATEGORY_PARTS = (
        "account",
        "cart",
        "checkout",
        "contact",
        "connexion",
        "login",
        "mon-compte",
        "my-account",
        "order",
        "panier",
        "password",
        "product-tag",
        "recherche",
        "search",
        "tag/",
        "wishlist",
        "wp-json",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("mykenza", logger)
        self.headers.update(self.config.get("headers", {}))

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _absolute_url(self, value: Any, base_url: Optional[str] = None) -> Optional[str]:
        return absolute_url(value, base_url or self.base_url)

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
    def _dedupe_urls(urls: Iterable[Any]) -> List[str]:
        seen = set()
        out = []
        for url in urls:
            text = clean_text(url)
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out

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

    @staticmethod
    def _clean_reference(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(
            r"^(r[e\u00e9]f[e\u00e9]rence|reference|sku)\s*:?",
            "",
            text,
            flags=re.I,
        )
        return clean_text(text.strip(" :;|-"))

    @staticmethod
    def _same_token(left: Any, right: Any) -> bool:
        a = re.sub(r"[^a-z0-9]", "", str(left or "").lower())
        b = re.sub(r"[^a-z0-9]", "", str(right or "").lower())
        return bool(a and b and a == b)

    def _meaningful_reference(self, value: Any, title: Any = None) -> Optional[str]:
        reference = self._clean_reference(value)
        if not reference:
            return None
        if title and self._same_token(reference, title):
            return None
        if len(reference) > 80:
            return None
        return reference

    def _product_url(self, href: Any) -> Optional[str]:
        url = self._absolute_url(href)
        if not url:
            return None
        parsed = urlsplit(url)
        if parsed.netloc.lower() not in {"mykenza.tn", "www.mykenza.tn"}:
            return None
        path = parsed.path.rstrip("/")
        if not path.startswith("/produit/"):
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, path + "/", "", ""))

    def _category_url(self, href: Any) -> Optional[str]:
        url = self._absolute_url(href)
        if not url:
            return None
        parsed = urlsplit(url)
        if parsed.netloc.lower() not in {"mykenza.tn", "www.mykenza.tn"}:
            return None
        path = parsed.path.rstrip("/")
        lower = f"{path}?{parsed.query}".lower()
        if any(part in lower for part in self.BAD_CATEGORY_PARTS):
            return None
        match = re.match(r"^/categorie-produit/([^/]+)", path, flags=re.I)
        if not match:
            return None
        slug = match.group(1).lower()
        if slug not in self.CATEGORY_SLUGS:
            return None
        return f"https://www.mykenza.tn/categorie-produit/{slug}/"

    @staticmethod
    def _discount_percent(value: Any) -> Optional[int]:
        text = clean_text(value)
        if not text:
            return None
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
        if not match:
            return None
        parsed = parse_price(match.group(1))
        return int(round(parsed)) if parsed is not None else None

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

    def _price_pair(
        self,
        root: Any,
        current_selectors: Iterable[str],
        old_selectors: Iterable[str],
        fallback_selectors: Iterable[str],
    ) -> Tuple[Optional[float], Optional[float]]:
        current = None
        old = None
        for selector in current_selectors:
            if not selector:
                continue
            current = self._price_from_node(root.css_first(selector))
            if current is not None:
                break
        for selector in old_selectors:
            if not selector:
                continue
            old = self._price_from_node(root.css_first(selector))
            if old is not None:
                break
        if current is None:
            for selector in fallback_selectors:
                if not selector:
                    continue
                node = root.css_first(selector)
                if not node:
                    continue
                if node.css_first("ins") or node.css_first("del"):
                    continue
                current = self._price_from_node(node)
                if current is not None:
                    break
        return current, old

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
                    url = self._absolute_url(candidate)
                    if url:
                        return url
                return None
            return self._absolute_url(text)

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

    def _availability(
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
        pieces = re.split(r"\s+(?:-|\u2013|\u2014)\s+", source)
        for piece in pieces:
            match = re.match(r"([^:]{2,70})\s*:\s*(.+)", piece)
            if not match:
                continue
            label = clean_text(match.group(1).strip(" ."))
            value = clean_text(match.group(2).strip(" ."))
            if not label or not value:
                continue
            label_key = label.lower()
            if "marque" in label_key:
                label = "Marque"
            elif "r" in label_key and "f" in label_key and "rence" in label_key:
                label = "Reference"
            specs[label] = value
        return specs

    def _reference_from_description(self, text: Any, title: Any = None) -> Optional[str]:
        specs = self._specs_from_description(text)
        for key, value in specs.items():
            if re.search(r"r[e\u00e9]f[e\u00e9]rence|reference", key, re.I):
                reference = self._meaningful_reference(value, title)
                if reference:
                    return reference

        source = clean_text(text)
        if not source:
            return None
        match = re.search(
            r"r[e\u00e9]f[e\u00e9]rence\s*:?\s*([A-Z0-9][A-Z0-9 ./_-]{1,40})",
            source,
            flags=re.I,
        )
        if not match:
            return None
        raw = re.split(r"\s+(?:-|\u2013|\u2014)\s+", match.group(1))[0]
        return self._meaningful_reference(raw, title)

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
                parsed = json.loads(raw.strip())
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

    def _brand_from_jsonld(self, product: Dict[str, Any]) -> Optional[str]:
        brand = product.get("brand")
        if isinstance(brand, dict):
            return clean_text(brand.get("name") or brand.get("@id"))
        return clean_text(brand)

    def _images_from_jsonld(self, product: Dict[str, Any]) -> List[str]:
        values = product.get("image")
        if not isinstance(values, list):
            values = [values]
        urls = []
        for value in values:
            if isinstance(value, dict):
                value = value.get("url") or value.get("contentUrl")
            url = self._absolute_url(value)
            if url:
                urls.append(url)
        return self._dedupe_urls(urls)

    def _category_stats(self, categories: List[Dict[str, Any]]) -> Dict[str, int]:
        return {
            "top_level": len(categories),
            "low_level": 0,
            "subcategory": 0,
            "total_urls": sum(1 for item in categories if item.get("url")),
        }

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        configured = self.config.get("fixed_categories") or []
        categories: List[Dict[str, Any]] = []
        seen = set()

        discovered = set()
        for link in tree.css(
            "header a[href*='/categorie-produit/'], nav a[href*='/categorie-produit/'], "
            ".menu a[href*='/categorie-produit/'], a[href*='/categorie-produit/']"
        ):
            category_url = self._category_url(link.attributes.get("href"))
            if category_url:
                discovered.add(category_url)

        for item in configured:
            url = self._category_url(item.get("url"))
            if not url or url in seen:
                continue
            slug = urlsplit(url).path.rstrip("/").split("/")[-1]
            name = clean_text(item.get("name")) or self.CATEGORY_SLUGS.get(slug)
            categories.append(
                {
                    "name": name,
                    "url": url,
                    "category_id": clean_text(item.get("category_id")) or self.CATEGORY_IDS.get(slug),
                    "low_level_categories": [],
                    "discovery_method": "fixed_category",
                    "validated_on_frontpage": url in discovered,
                }
            )
            seen.add(url)

        if not categories:
            categories.extend(self._categories_from_store_api())

        return {
            "categories": categories,
            "stats": self._category_stats(categories),
        }

    def _categories_from_store_api(self) -> List[Dict[str, Any]]:
        api_url = (
            self.selectors.get("frontpage", {}).get("store_categories_api")
            or "https://www.mykenza.tn/wp-json/wc/store/v1/products/categories"
        )
        categories: List[Dict[str, Any]] = []
        try:
            with httpx.Client(headers=self.headers, follow_redirects=True, timeout=20) as client:
                response = client.get(api_url)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            self.logger.debug(f"Store categories API fallback failed: {exc}")
            return categories

        for item in payload if isinstance(payload, list) else []:
            slug = clean_text(item.get("slug"))
            if slug not in self.CATEGORY_SLUGS:
                continue
            url = self._category_url(item.get("permalink")) or (
                f"https://www.mykenza.tn/categorie-produit/{slug}/"
            )
            categories.append(
                {
                    "name": self.CATEGORY_SLUGS[slug],
                    "url": url,
                    "category_id": str(item.get("id") or self.CATEGORY_IDS.get(slug)),
                    "low_level_categories": [],
                    "discovery_method": "woocommerce_store_api",
                    "product_count_hint": item.get("count"),
                }
            )
        return categories

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------

    async def scrape_category_page(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {
                "products": [],
                "pagination": {"total_pages": 1},
                "error": "Failed to fetch",
            }

        try:
            products = self.extract_products_from_html(html)
            if products and not (self.html_dir / "listing_sample_1.html").exists():
                save_text_atomic(html, self.html_dir / "listing_sample_1.html", self.logger)
            pagination = self.extract_pagination_from_html(html)
        except Exception as exc:
            self.logger.debug(f"Error parsing {url}: {exc}")
            return {"products": [], "pagination": {"total_pages": 1}, "error": str(exc)}

        return {"products": products, "pagination": pagination}

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        result = await self.scrape_category_page(category_url)
        if result.get("error"):
            return []

        all_products = list(result.get("products", []))
        if limit and len(all_products) >= limit:
            return dedupe_products(all_products[:limit], self.logger, "mykenza limited listing")

        pagination = result.get("pagination", {})
        total_pages = pagination.get("total_pages") or 1
        max_pages = self.config.get("settings", {}).get("max_pagination_pages", 80)
        total_pages = min(total_pages, max_pages)

        page = 2
        while page <= total_pages:
            page_result = await self.scrape_category_page(self.build_page_url(category_url, page))
            if not page_result.get("error"):
                all_products.extend(page_result.get("products", []))
            if limit and len(all_products) >= limit:
                break
            page += 1

        deduped = dedupe_products(all_products, self.logger, "mykenza listing")
        return deduped[:limit] if limit else deduped

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cards = tree.css("ul.products li.product, li.product.type-product, .products .product")
        products: List[Dict[str, Any]] = []
        cp = self.selectors.get("category_page", {})

        for card in cards:
            link = self._first(
                card,
                [
                    cp.get("item_url", ""),
                    "a.woocommerce-LoopProduct-link[href*='/produit/']",
                    "a.woocommerce-loop-product__link[href*='/produit/']",
                    "a[href*='/produit/']",
                ],
            )
            url = self._product_url(self._attr(link, "href"))
            if not url:
                continue

            title = self._text(
                self._first(
                    card,
                    [
                        cp.get("item_title", ""),
                        ".woocommerce-loop-product__title",
                        "h2",
                        "h3",
                    ],
                )
            )
            if not title:
                title = self._text(link)
            if not title:
                continue

            id_node = self._first(
                card,
                [
                    cp.get("item_id", ""),
                    "button.loop_add_to_cart_express[data-product_id]",
                    "button[data-product_id]",
                    "[data-product_id]",
                    "[data-product-id]",
                ],
            )
            product_id = (
                self._attr(id_node, "data-product_id")
                or self._attr(id_node, "data-product-id")
                or self._attr(id_node, "value")
                or self._card_class_id(card)
            )

            price, old_price = self._price_pair(
                card,
                [
                    cp.get("item_current_price", ""),
                    ".price ins .woocommerce-Price-amount",
                    ".price ins",
                    "ins .woocommerce-Price-amount",
                    "ins",
                ],
                [
                    cp.get("item_old_price", ""),
                    ".price del .woocommerce-Price-amount",
                    ".price del",
                    "del .woocommerce-Price-amount",
                    "del",
                ],
                [
                    cp.get("item_price", ""),
                    ".price .woocommerce-Price-amount",
                    ".price",
                ],
            )

            discount_text = self._text(
                self._first(
                    card,
                    [
                        cp.get("item_discount", ""),
                        ".MK-badges-product-onsale .percentage",
                        ".MK-badges-product-onsale-badge",
                        ".onsale",
                        ".discount",
                    ],
                )
            )
            discount_percent = self._discount_percent(discount_text)
            if discount_percent is None:
                discount_percent = self._computed_discount(price, old_price)

            image = self._image_from_node(
                self._first(
                    card,
                    [
                        cp.get("item_image", ""),
                        ".product-thumbnail img",
                        "img.wp-post-image",
                        "img.attachment-woocommerce_thumbnail",
                        "img",
                    ],
                )
            )

            brand_node = self._first(
                card,
                [
                    cp.get("item_brand", ""),
                    ".product-brand img[alt]",
                    ".product-brand a",
                    ".product-brand",
                ],
            )
            brand = self._attr(brand_node, "alt") or self._text(brand_node)

            classes = card.attributes.get("class", "")
            add_button = self._first(card, ["button[data-product_id]", "a.add_to_cart_button", "button"])
            availability, available = self._availability(classes=classes, add_button=add_button)

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "url": url,
                "name": title,
                "price": price,
                "old_price": old_price,
                "discount_percent": discount_percent,
                "image": image,
                "brand": brand,
                "availability": availability,
                "available": available,
            }

            products.append(finalize_product_record({k: v for k, v in product.items() if v is not None}))

        return dedupe_products(products, self.logger, "mykenza listing")

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parts = urlsplit(base_url)
        path = re.sub(r"/page/\d+/?$", "/", parts.path)
        path = path.rstrip("/") + "/"

        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            lower = key.lower()
            if lower == "srsltid" or lower.startswith("utm_") or lower in {"paged", "product-page"}:
                continue
            query.append((key, value))

        if page_num and page_num > 1:
            path = path.rstrip("/") + f"/page/{page_num}/"

        return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        page_numbers = {1}

        current_page = 1
        current = tree.css_first(
            "nav.woocommerce-pagination .page-numbers.current, "
            ".woocommerce-pagination .page-numbers.current, .page-numbers.current"
        )
        if current:
            current_text = self._text(current)
            if current_text and current_text.isdigit():
                current_page = int(current_text)
                page_numbers.add(current_page)

        has_next = False
        for link in tree.css(
            "nav.woocommerce-pagination a.page-numbers[href], "
            ".woocommerce-pagination a.page-numbers[href], a.page-numbers[href]"
        ):
            text = self._text(link)
            href = self._attr(link, "href") or ""
            if "next" in (link.attributes.get("class", "") or "").lower():
                has_next = True
            if text and text.isdigit():
                page_numbers.add(int(text))
            match = re.search(r"/page/(\d+)/?", href)
            if match:
                page_numbers.add(int(match.group(1)))

        if tree.css_first("nav.woocommerce-pagination a.next.page-numbers[href], a.next.page-numbers[href]"):
            has_next = True

        total_pages = max(page_numbers) if page_numbers else current_page
        max_pages = self.config.get("settings", {}).get("max_pagination_pages", 80)
        total_pages = min(total_pages, max_pages)

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next or current_page < total_pages,
        }

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch product"}

        if not (self.html_dir / "detail_sample_1.html").exists():
            save_text_atomic(html, self.html_dir / "detail_sample_1.html", self.logger)

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = html_product_metadata(html, url, self.base_url)

        title = self._text(
            self._first(
                tree,
                [
                    pp.get("title", ""),
                    "h1.product_title.entry-title",
                    "h1.product_title",
                    "h1.entry-title",
                    "h1",
                ],
            )
        )
        if title:
            data["title"] = title
            data["name"] = title

        if not self._meaningful_reference(data.get("reference"), title):
            data.pop("reference", None)
            data.pop("sku", None)

        jsonld_products = self._jsonld_products(html)
        if jsonld_products:
            product_json = jsonld_products[0]
            brand = self._brand_from_jsonld(product_json)
            if brand:
                data.setdefault("brand", brand)
            images = self._images_from_jsonld(product_json)
            if images:
                data.setdefault("images", images)
                data.setdefault("image", images[0])

        product_id = self._body_class_id(tree)
        id_node = self._first(
            tree,
            [
                pp.get("product_id", ""),
                "button[name='add-to-cart'][value]",
                "form.cart [name='add-to-cart'][value]",
                "input[name='product_id'][value]",
                "[data-product_id]",
                "[data-product-id]",
            ],
        )
        product_id = (
            self._attr(id_node, "value")
            or self._attr(id_node, "data-product_id")
            or self._attr(id_node, "data-product-id")
            or product_id
            or data.get("product_id")
        )
        if product_id:
            data["id"] = product_id
            data["product_id"] = product_id

        price, old_price = self._price_pair(
            tree,
            [
                pp.get("current_price", ""),
                ".summary .price ins .woocommerce-Price-amount",
                ".summary .price ins",
                "p.price ins .woocommerce-Price-amount",
                "p.price ins",
            ],
            [
                pp.get("old_price", ""),
                ".summary .price del .woocommerce-Price-amount",
                ".summary .price del",
                "p.price del .woocommerce-Price-amount",
                "p.price del",
            ],
            [
                pp.get("price", ""),
                ".summary .price .woocommerce-Price-amount",
                "p.price .woocommerce-Price-amount",
                ".summary .price",
                "p.price",
            ],
        )
        if price is not None:
            data["price"] = price
        if old_price is not None:
            data["old_price"] = old_price
        discount_percent = self._discount_percent(self._text(tree.css_first(".summary .price, p.price")))
        if discount_percent is None:
            discount_percent = self._computed_discount(data.get("price"), data.get("old_price"))
        if discount_percent is not None:
            data["discount_percent"] = discount_percent

        sku_node = self._first(
            tree,
            [
                pp.get("sku", ""),
                ".sku_wrapper .sku",
                "span.sku",
                "[itemprop='sku']",
            ],
        )
        sku = self._meaningful_reference(self._attr(sku_node, "content") or self._text(sku_node), title)
        if sku:
            data["reference"] = sku
            data["sku"] = sku

        short_node = tree.css_first(".woocommerce-product-details__short-description")
        short_description = self._text(short_node)
        desc_nodes = tree.css(
            pp.get("description", "")
            or "#tab-description, .woocommerce-Tabs-panel--description, .woocommerce-product-details__short-description"
        )
        descriptions = self._dedupe_urls([self._text(node) for node in desc_nodes])
        description = descriptions[0] if descriptions else short_description
        if short_description:
            data["short_description"] = short_description
        if description:
            data["description"] = description

        specs = self._specs_from_tables(tree)
        specs.update(self._specs_from_description(description))
        if specs:
            data["specifications"] = specs
            data["specs"] = specs

        reference = self._reference_from_description(description, title)
        if not reference and specs:
            for key, value in specs.items():
                if re.search(r"r[e\u00e9]f[e\u00e9]rence|reference|sku", key, re.I):
                    reference = self._meaningful_reference(value, title)
                    if reference:
                        break
        if reference:
            data["reference"] = reference
            data["sku"] = reference

        if not data.get("brand") and specs:
            for key, value in specs.items():
                if "marque" in key.lower():
                    data["brand"] = clean_text(value)
                    break

        brand_node = self._first(
            tree,
            [
                pp.get("brand", ""),
                ".product-brand img[alt]",
                ".product-brands a",
                ".brand a",
            ],
        )
        brand = self._attr(brand_node, "alt") or self._text(brand_node)
        if brand:
            data["brand"] = brand

        availability_node = self._first(
            tree,
            [
                pp.get("availability", ""),
                ".summary .stock",
                "p.stock",
                ".stock",
            ],
        )
        add_button = self._first(tree, ["button[name='add-to-cart']", ".single_add_to_cart_button"])
        body = tree.css_first("body")
        body_classes = body.attributes.get("class", "") if body else ""
        availability, available = self._availability(
            text=self._text(availability_node) or data.get("availability"),
            classes=body_classes,
            add_button=add_button,
        )
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        image_urls = list(data.get("images") or [])
        for node in tree.css(
            pp.get("image_gallery", "")
            or ".woocommerce-product-gallery__image img, .woocommerce-product-gallery img, "
            "img.wp-post-image, meta[property='og:image']"
        ):
            image = self._image_from_node(node)
            if image:
                image_urls.append(image)
        image_urls = self._dedupe_urls(image_urls)
        if image_urls:
            data["images"] = image_urls
            data["image"] = image_urls[0]

        breadcrumbs = []
        categories = []
        seen_breadcrumbs = set()
        for link in tree.css(
            pp.get("breadcrumbs", "")
            or ".woocommerce-breadcrumb a, nav.woocommerce-breadcrumb a, .posted_in a[href*='/categorie-produit/']"
        ):
            name = self._text(link)
            href = self._absolute_url(self._attr(link, "href"))
            if not name or name.lower() in {"accueil", "home"}:
                continue
            crumb_key = (name, href)
            if crumb_key in seen_breadcrumbs:
                continue
            seen_breadcrumbs.add(crumb_key)
            breadcrumbs.append({"name": name, "url": href})
            if href and "/categorie-produit/" in href:
                categories.append({"name": name, "url": href})
        if breadcrumbs:
            data["breadcrumbs"] = breadcrumbs
        if categories:
            data["categories"] = categories

        gtins = extract_gtins_from_text(" ".join(str(v) for v in [description, json.dumps(specs, ensure_ascii=True)]))
        if gtins:
            data["barcode"] = gtins[0]

        data["url"] = self._product_url(url) or url
        data["shop"] = self.site_name
        return finalize_product_record({k: v for k, v in data.items() if v not in (None, "", [], {})})


def get_scraper(logger: logging.Logger) -> MykenzaScraper:
    """Factory used by scraper.sites registry."""
    return MykenzaScraper(logger)
