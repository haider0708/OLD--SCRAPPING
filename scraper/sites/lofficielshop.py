#!/usr/bin/env python3
"""
Lofficielshop scraper - PrestaShop/POS theme, HTTP/selectolax.
"""

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
    extract_gtins_from_text,
    finalize_product_record,
    html_product_metadata,
    parse_price,
)


class LofficielshopScraper(FastScraper):
    """HTTP scraper for lofficielshop.tn."""

    BAD_CATEGORY_PARTS = (
        "authentication",
        "account",
        "adresse",
        "addresses",
        "brand",
        "brands",
        "cart",
        "checkout",
        "cms",
        "compare",
        "connexion",
        "contact",
        "content",
        "fabricant",
        "identity",
        "login",
        "magasins",
        "manufacturer",
        "module",
        "mon-compte",
        "order",
        "panier",
        "password",
        "recherche",
        "search",
        "social",
        "wishlist",
        "blog",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("lofficielshop", logger)

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
            node = root.css_first(selector)
            if node:
                return node
        return None

    @staticmethod
    def _clean_reference(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"^(r[eé]f[eé]rence|reference)\s*:?", "", text, flags=re.I).strip()
        text = text.strip("[] ")
        return text or None

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

    def _category_url(self, href: Any) -> Optional[str]:
        url = self._absolute_url(href)
        if not url:
            return None
        parsed = urlsplit(url)
        host = parsed.netloc.lower()
        path = parsed.path.rstrip("/")
        lower_url = f"{path}?{parsed.query}".lower()
        if host not in {"lofficielshop.tn", "www.lofficielshop.tn"}:
            return None
        if not path.startswith("/fr/"):
            return None
        if path.endswith(".html"):
            return None
        if not re.search(r"^/fr/\d+-", path):
            return None
        if any(part in lower_url for part in self.BAD_CATEGORY_PARTS):
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

    def _product_url(self, href: Any) -> Optional[str]:
        url = self._absolute_url(href)
        if not url:
            return None
        parsed = urlsplit(url)
        if parsed.netloc.lower() not in {"lofficielshop.tn", "www.lofficielshop.tn"}:
            return None
        if not parsed.path.startswith("/fr/") or not parsed.path.endswith(".html"):
            return None
        if not re.search(r"/fr/[^/]+/\d+(?:-\d+)?-", parsed.path):
            return None
        return url

    @staticmethod
    def _product_ids_from_url(url: Any) -> Tuple[Optional[str], Optional[str]]:
        path = urlsplit(str(url or "")).path
        match = re.search(r"/(\d+)(?:-(\d+))?-", path)
        if not match:
            return None, None
        return match.group(1), match.group(2)

    def _price_from_node(self, node: Any) -> Optional[float]:
        if not node:
            return None
        return parse_price(
            self._attr(node, "content")
            or self._attr(node, "value")
            or self._text(node)
        )

    def _image_from_node(self, node: Any) -> Optional[str]:
        if not node:
            return None
        for attr in ("data-full-size-image-url", "data-image-large-src", "data-src", "src"):
            url = self._absolute_url(node.attributes.get(attr))
            if url:
                return url
        return None

    def _availability(self, value: Any, classes: Any = "") -> Tuple[Optional[str], Optional[bool]]:
        text = clean_text(value)
        if text:
            text = clean_text(re.sub(r"[\ue000-\uf8ff]", " ", text))
        cls = str(classes or "").lower()
        combined = f"{text or ''} {cls}".lower()
        if any(token in combined for token in ("rupture", "hors stock", "out-of-stock", "out_of_stock", "indisponible")):
            return text or "Rupture de stock", False
        if any(token in combined for token in ("en stock", "disponible", "available", "in-stock", "last_remaining_items", "dernier")):
            return text or "En stock", True

        fallback_text, fallback_available = availability_from_text(text)
        return fallback_text, fallback_available

    @staticmethod
    def _computed_discount(price: Optional[float], old_price: Optional[float]) -> Optional[int]:
        if price is None or old_price is None or old_price <= 0 or old_price <= price:
            return None
        return int(round((1 - (price / old_price)) * 100))

    @staticmethod
    def _discount_amount(value: Any) -> Optional[float]:
        text = clean_text(value)
        if not text:
            return None
        amount = parse_price(text)
        return abs(amount) if amount is not None else None

    def _category_stats(self, categories: List[Dict[str, Any]]) -> Dict[str, int]:
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
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories: List[Dict[str, Any]] = []
        seen_urls = set()
        seen_top_labels = set()

        top_selector = fp.get(
            "vertical_menu",
            "#_desktop_vegamenu div.pos-menu-vertical > ul.menu-content > li.menu-item",
        )
        for top_li in tree.css(top_selector):
            top_link = top_li.css_first("a")
            top_name = self._text(top_link)
            if not top_name:
                continue
            top_name = top_name.strip()
            top_url = self._category_url(self._attr(top_link, "href"))

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }
            if top_url:
                seen_urls.add(top_url)
            seen_top_labels.add(top_name.lower())

            low_blocks = top_li.css(fp.get("low_category_block", "div.pos-sub-menu li.submenu-item"))
            seen_low_urls = set()
            for low_block in low_blocks:
                low_link = low_block.css_first("a[href]")
                low_name = self._text(low_link)
                low_url = self._category_url(self._attr(low_link, "href"))
                if not low_name or not low_url or low_url in seen_low_urls:
                    continue

                low_cat = {
                    "name": low_name,
                    "url": low_url,
                    "level": "low",
                    "subcategories": [],
                }
                seen_low_urls.add(low_url)
                seen_urls.add(low_url)

                seen_sub_urls = set()
                for sub_link in low_block.css(fp.get("subcategory", "ul.category-sub-menu > li > a[href]")):
                    sub_name = self._text(sub_link)
                    sub_url = self._category_url(self._attr(sub_link, "href"))
                    if not sub_name or not sub_url or sub_url in seen_sub_urls:
                        continue
                    if sub_url == low_url:
                        continue
                    low_cat["subcategories"].append(
                        {"name": sub_name, "url": sub_url, "level": "subcategory"}
                    )
                    seen_sub_urls.add(sub_url)
                    seen_urls.add(sub_url)

                top_cat["low_level_categories"].append(low_cat)

            for low_link in top_li.css(fp.get("standalone_low_category", "div.pos-sub-menu a.column_title[href]")):
                low_name = self._text(low_link)
                low_url = self._category_url(self._attr(low_link, "href"))
                if not low_name or not low_url or low_url in seen_low_urls:
                    continue
                top_cat["low_level_categories"].append(
                    {"name": low_name, "url": low_url, "level": "low", "subcategories": []}
                )
                seen_low_urls.add(low_url)
                seen_urls.add(low_url)

            if top_url or top_cat["low_level_categories"]:
                categories.append(top_cat)

        for link in tree.css(fp.get("horizontal_menu", "#_desktop_megamenu a[href]")):
            name = self._text(link)
            url = self._category_url(self._attr(link, "href"))
            if not name or not url or url in seen_urls:
                continue
            if name.lower() in seen_top_labels:
                continue
            categories.append(
                {"name": name, "url": url, "level": "top", "low_level_categories": []}
            )
            seen_urls.add(url)
            seen_top_labels.add(name.lower())

        if not categories:
            fallback_lows = []
            for link in tree.css(fp.get("fallback_links", "a[href]")):
                name = self._text(link)
                url = self._category_url(self._attr(link, "href"))
                if not name or not url or url in seen_urls:
                    continue
                fallback_lows.append(
                    {"name": name, "url": url, "level": "low", "subcategories": []}
                )
                seen_urls.add(url)
            if fallback_lows:
                categories.append(
                    {
                        "name": "Catalogue",
                        "url": None,
                        "level": "top",
                        "low_level_categories": fallback_lows,
                    }
                )

        stats = self._category_stats(categories)
        self.logger.info(
            "Found %s Lofficielshop top categories (%s queued URLs)",
            stats["top_level"],
            stats["total_urls"],
        )
        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        if page_num <= 1:
            return base_url
        parts = urlsplit(base_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["page"] = str(page_num)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    async def scrape_category_page(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {
                "products": [],
                "pagination": {"total_pages": 1},
                "error": "Failed to fetch",
            }

        sample_path = self.html_dir / "listing_sample_1.html"
        if not sample_path.exists() and "product-miniature" in html:
            save_text_atomic(html, sample_path, self.logger)

        try:
            products = self.extract_products_from_html(html)
            pagination = self.extract_pagination_from_html(html)
        except Exception as exc:
            self.logger.debug("Error parsing %s: %s", url, exc)
            return {"products": [], "pagination": {"total_pages": 1}, "error": str(exc)}

        return {"products": products, "pagination": pagination}

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        items = tree.css(
            cp.get(
                "item_selector",
                "#js-product-list article.product-miniature.js-product-miniature, article.product-miniature.js-product-miniature",
            )
        )
        if not items:
            items = tree.css("article.product-miniature, .js-product-miniature")

        products: List[Dict[str, Any]] = []
        for item in items:
            product_id = clean_text(item.attributes.get("data-id-product"))
            attribute_id = clean_text(item.attributes.get("data-id-product-attribute"))

            link = self._first(
                item,
                [
                    "a.thumbnail.product-thumbnail[href]",
                    ".product_desc h3 a.product_name[href]",
                    "h3 a[href]",
                    "a[href]",
                ],
            )
            product_url = self._product_url(self._attr(link, "href"))
            if not product_url:
                continue

            url_product_id, url_attribute_id = self._product_ids_from_url(product_url)
            product_id = product_id or url_product_id
            attribute_id = attribute_id or url_attribute_id

            name_node = self._first(
                item,
                [
                    ".product_desc h3 a.product_name",
                    "h3 a.product_name",
                    ".product_name",
                    "h3 a",
                ],
            )
            name = self._text(name_node) or self._attr(name_node, "title") or self._attr(link, "title")

            price = self._price_from_node(
                self._first(item, [".product-price-and-shipping .price", ".price"])
            )
            old_price = self._price_from_node(
                self._first(item, [".product-price-and-shipping .regular-price", ".regular-price"])
            )
            discount_amount = self._discount_amount(
                self._text(
                    self._first(
                        item,
                        [".discount-amount", ".product-flag li.discount", ".product-flag .discount"],
                    )
                )
            )
            discount_percent = self._computed_discount(price, old_price)

            reference = self._clean_reference(
                self._text(
                    self._first(
                        item,
                        [
                            ".product-reference [itemprop='sku']",
                            ".product-reference span",
                            ".product-reference",
                        ],
                    )
                )
            )

            brand_node = self._first(
                item,
                [".brand-img img[alt]", ".product-brand img[alt]", ".manufacturer img[alt]"],
            )
            brand = self._attr(brand_node, "alt") or self._text(brand_node)

            availability_node = self._first(
                item,
                [".availability", ".availability-list", ".product-flag li.out_of_stock"],
            )
            availability, available = self._availability(
                self._text(availability_node),
                self._attr(availability_node, "class"),
            )

            image = self._image_from_node(
                self._first(item, ["a.thumbnail.product-thumbnail img", ".img_block img", "img"])
            )

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "url": product_url,
                "name": name,
                "price": price,
                "old_price": old_price,
                "discount_percent": discount_percent,
                "availability": availability,
                "available": available,
            }
            if attribute_id:
                product["product_attribute_id"] = attribute_id
            if discount_amount is not None:
                product["discount_amount"] = discount_amount
            if image:
                product["image"] = image
            if reference:
                product["reference"] = reference
            if brand:
                product["brand"] = brand

            gtins = extract_gtins_from_text(product_url)
            if gtins:
                product.setdefault("barcode", gtins[0])

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "lofficielshop listing")

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        pagination = tree.css_first("nav.pagination, .pagination")
        current_page = 1
        total_pages = 1
        has_next = False

        if not pagination:
            return {
                "current_page": current_page,
                "total_pages": total_pages,
                "has_next": has_next,
            }

        for link in pagination.css("a[href]"):
            href = self._attr(link, "href") or ""
            label = self._text(link) or ""
            classes = (self._attr(link, "class") or "").lower()
            rel = (self._attr(link, "rel") or "").lower()

            if "next" in classes or rel == "next" or "suivant" in label.lower():
                if "disabled" not in classes:
                    has_next = True

            page_num = None
            if label.isdigit():
                page_num = int(label)
            else:
                query = dict(parse_qsl(urlsplit(href).query))
                if query.get("page", "").isdigit():
                    page_num = int(query["page"])

            if page_num:
                total_pages = max(total_pages, page_num)
                if "disabled" in classes or "current" in classes or "active" in classes:
                    current_page = page_num

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
        if not sample_path.exists() and "data-product" in html:
            save_text_atomic(html, sample_path, self.logger)

        tree = HTMLParser(html)
        data = html_product_metadata(html, url, self.base_url)
        data["url"] = url

        product_json = self._product_json(tree)
        if product_json:
            self._apply_product_json(data, product_json)

        self._apply_detail_dom(data, tree, url)
        return finalize_product_record(data)

    def _product_json(self, tree: HTMLParser) -> Dict[str, Any]:
        node = tree.css_first("#product-details[data-product]")
        raw = self._attr(node, "data-product")
        if not raw:
            return {}
        try:
            parsed = json.loads(html_lib.unescape(raw))
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def _apply_product_json(self, data: Dict[str, Any], product: Dict[str, Any]) -> None:
        product_id = product.get("id_product") or product.get("id")
        if product_id is not None:
            data["product_id"] = str(product_id)
            data["id"] = str(product_id)

        title = clean_text(product.get("name"))
        if title:
            data["title"] = title
            data.setdefault("name", title)

        reference = self._clean_reference(product.get("reference"))
        if reference:
            data["reference"] = reference

        for key in ("ean13", "upc", "isbn"):
            gtins = extract_gtins_from_text(product.get(key))
            if gtins:
                data.setdefault("barcode", gtins[0])
                break

        price = parse_price(
            product.get("price_amount")
            or product.get("price_tax_exc")
            or product.get("price")
        )
        if price is not None:
            data["price"] = price

        old_price = parse_price(product.get("price_without_reduction"))
        if old_price is not None and price is not None and old_price > price:
            data["old_price"] = old_price
        elif old_price == price:
            data.pop("old_price", None)

        data["discount_percent"] = self._computed_discount(data.get("price"), data.get("old_price"))

        quantity = parse_price(product.get("quantity"))
        if quantity is not None:
            data["quantity"] = int(quantity)

        availability_raw = clean_text(product.get("availability"))
        availability, available = self._availability(availability_raw)
        if availability_raw == "available":
            availability, available = "En stock", True
        elif availability_raw == "last_remaining_items":
            availability, available = "Derniers articles en stock", True
        elif availability_raw == "unavailable":
            availability, available = "Rupture de stock", False
        elif available is None and quantity is not None:
            availability = "En stock" if quantity > 0 else "Rupture de stock"
            available = quantity > 0
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        if product.get("category_name"):
            data["detail_category"] = clean_text(product.get("category_name"))
        if product.get("category"):
            data["detail_category_slug"] = clean_text(product.get("category"))
        if product.get("condition"):
            data["condition"] = clean_text(product.get("condition"))

        short_description = self._html_to_text(product.get("description_short"))
        if short_description:
            data["short_description"] = short_description
            data["overview"] = short_description

        full_description = self._html_to_text(product.get("description"))
        if full_description:
            data["description"] = full_description
            data["full_description"] = full_description

        images = self._images_from_product_json(product)
        if images:
            data["images"] = images
            data["image"] = images[0]

    def _apply_detail_dom(self, data: Dict[str, Any], tree: HTMLParser, url: str) -> None:
        title = self._text(self._first(tree, ["h1.h1", "h1[itemprop='name']", "h1"]))
        if title:
            data.setdefault("title", title)
            data.setdefault("name", title)

        if not data.get("product_id"):
            product_id = self._attr(
                self._first(
                    tree,
                    [
                        "input[name='id_product'][value]",
                        "#product_page_product_id[value]",
                        "[data-product-id]",
                    ],
                ),
                "value",
            )
            product_id = product_id or self._product_ids_from_url(url)[0]
            if product_id:
                data["product_id"] = product_id
                data["id"] = product_id

        attribute_id = self._product_ids_from_url(url)[1]
        if attribute_id:
            data.setdefault("product_attribute_id", attribute_id)

        reference = self._clean_reference(
            self._text(
                self._first(
                    tree,
                    [
                        ".product-reference [itemprop='sku']",
                        ".product-reference span",
                        "[itemprop='sku']",
                    ],
                )
            )
        )
        if reference:
            data.setdefault("reference", reference)

        brand_node = self._first(
            tree,
            [
                ".product-manufacturer img[alt]",
                ".product-manufacturer a",
                ".brand-img img[alt]",
            ],
        )
        brand = self._attr(brand_node, "alt") or self._text(brand_node)
        if brand:
            data["brand"] = brand

        price = self._price_from_node(
            self._first(
                tree,
                [
                    ".current-price-value[content]",
                    ".current-price [content]",
                    ".product-price [content]",
                    "meta[property='product:price:amount']",
                ],
            )
        )
        if price is not None:
            data.setdefault("price", price)

        old_price = self._price_from_node(
            self._first(tree, [".product-prices .regular-price", ".regular-price"])
        )
        if old_price is not None and old_price > (data.get("price") or 0):
            data.setdefault("old_price", old_price)
        data["discount_percent"] = self._computed_discount(data.get("price"), data.get("old_price"))

        availability_node = self._first(tree, ["#product-availability", ".product-availability", ".availability"])
        availability, available = self._availability(
            self._text(availability_node),
            self._attr(availability_node, "class"),
        )
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        short_description = self._text(
            self._first(
                tree,
                [
                    "#product-description-short",
                    ".product-description-short",
                    ".product-information .product-description",
                ],
            )
        )
        if short_description:
            if not data.get("short_description"):
                data["short_description"] = short_description
            if not data.get("overview"):
                data["overview"] = short_description

        description = self._text(
            self._first(
                tree,
                [
                    "#description .product-description",
                    ".product-tabs .product-description",
                    ".product-description",
                ],
            )
        )
        if description:
            if not data.get("description"):
                data["description"] = description
            if not data.get("full_description"):
                data["full_description"] = description
        elif data.get("short_description"):
            if not data.get("description"):
                data["description"] = data["short_description"]
            if not data.get("full_description"):
                data["full_description"] = data["short_description"]

        specs = self._specs_from_dom(tree)
        if specs:
            data["specifications"] = specs
            for label, value in specs.items():
                if re.search(r"ean|gtin|code\s*bar|barcode", label, re.I):
                    gtins = extract_gtins_from_text(value)
                    if gtins:
                        data.setdefault("barcode", gtins[0])

        breadcrumbs = [
            crumb
            for crumb in (self._text(node) for node in tree.css(".breadcrumb a[href]"))
            if crumb and crumb.lower() != "accueil"
        ]
        if breadcrumbs:
            data["breadcrumbs"] = breadcrumbs

        images = self._dedupe_urls(
            list(data.get("images") or [])
            + [
                self._image_from_node(node)
                for node in tree.css(".product-cover img, .product-images img, .js-thumb, img[itemprop='image']")
            ]
        )
        if images:
            data["images"] = images
            data["image"] = data.get("image") or images[0]

        if not data.get("barcode"):
            gtins = extract_gtins_from_text(url)
            if gtins:
                data["barcode"] = gtins[0]

    def _images_from_product_json(self, product: Dict[str, Any]) -> List[str]:
        urls = []
        images = product.get("images")
        if not isinstance(images, list):
            return []
        for image in images:
            if not isinstance(image, dict):
                continue
            by_size = image.get("bySize") if isinstance(image.get("bySize"), dict) else {}
            for size in ("large_default", "medium_default", "home_default", "cart_default"):
                size_info = by_size.get(size)
                if isinstance(size_info, dict):
                    url = self._absolute_url(size_info.get("url"))
                    if url:
                        urls.append(url)
                        break
            else:
                for key in ("large", "medium", "small"):
                    value = image.get(key)
                    url = self._absolute_url(value.get("url")) if isinstance(value, dict) else self._absolute_url(value)
                    if url:
                        urls.append(url)
                        break
        return self._dedupe_urls(urls)

    def _specs_from_dom(self, tree: HTMLParser) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for block in tree.css(".product-features dl, .data-sheet dl, dl.data-sheet"):
            labels = block.css("dt")
            values = block.css("dd")
            for label_node, value_node in zip(labels, values):
                label = (self._text(label_node) or "").rstrip(":")
                value = self._text(value_node)
                if label and value:
                    specs[label] = value

        for row in tree.css("table.product-features tr, table.product-attributes tr"):
            cells = row.css("th, td")
            if len(cells) < 2:
                continue
            label = (self._text(cells[0]) or "").rstrip(":")
            value = self._text(cells[-1])
            if label and value:
                specs[label] = value
        return specs


def get_scraper(logger: logging.Logger) -> LofficielshopScraper:
    """Factory used by scraper.sites registry."""
    return LofficielshopScraper(logger)
