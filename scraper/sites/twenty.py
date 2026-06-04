#!/usr/bin/env python3
"""
Twenty scraper - PrestaShop/Iqit, HTTP/selectolax.
"""

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional
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
    parse_price,
)


class TwentyScraper(FastScraper):
    """HTTP scraper for twenty.tn PrestaShop pages."""

    BAD_CATEGORY_PARTS = (
        "authentication",
        "account",
        "addresses",
        "cart",
        "checkout",
        "cms",
        "contact",
        "identity",
        "login",
        "module",
        "order",
        "password",
        "search",
        "sitemap",
        "supplier",
        "manufacturer",
        "brand",
        "seller",
        "wishlist",
        "blog",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("twenty", logger)

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
    def _html_to_text(value: Any) -> Optional[str]:
        text = clean_text(value)
        if not text:
            return None
        if "<" not in text or ">" not in text:
            return text
        try:
            return clean_text(HTMLParser(f"<div>{text}</div>").text(separator=" ", strip=True))
        except Exception:
            return re.sub(r"<[^>]+>", " ", text).strip() or None

    def _category_url(self, href: Any, allow_broad: bool = False) -> Optional[str]:
        url = self._absolute_url(href)
        if not url:
            return None

        parsed = urlsplit(url)
        if parsed.netloc.lower() != "www.twenty.tn":
            return None

        path = parsed.path.rstrip("/")
        lower_url = f"{path}?{parsed.query}".lower()
        if not path.startswith("/en/"):
            return None
        if path.endswith(".html"):
            return None
        if any(part in lower_url for part in self.BAD_CATEGORY_PARTS):
            return None
        if "all-our-departments" in lower_url and not allow_broad:
            return None
        if not re.search(r"^/en/\d+-", path):
            return None

        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

    def _is_product_url(self, href: Any) -> bool:
        url = self._absolute_url(href)
        if not url:
            return False
        parsed = urlsplit(url)
        return parsed.netloc.lower() == "www.twenty.tn" and parsed.path.endswith(".html")

    def _product_id_from_url(self, url: str) -> Optional[str]:
        match = re.search(r"/(\d+)-[^/]*\.html(?:$|\?)", url or "")
        return match.group(1) if match else None

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
        seen_top_urls = set()

        main_tab = tree.css_first(fp.get("main_tab", "li#cbp-hrmenu-tab-2"))
        if main_tab:
            for top_link in main_tab.css(fp.get("top_level_category", "a.cbp-category-title[href]")):
                top_name = self._text(top_link)
                top_url = self._category_url(self._attr(top_link, "href"))
                if not top_name or not top_url or top_url in seen_top_urls:
                    continue

                top_cat = {
                    "name": top_name,
                    "url": top_url,
                    "level": "top",
                    "low_level_categories": [],
                }
                seen_top_urls.add(top_url)

                top_container = getattr(top_link, "parent", None)
                seen_low_urls = set()
                for low_link in (top_container.css(
                    fp.get(
                        "low_level_category",
                        "ul.cbp-category-tree > li > div.cbp-category-link-w > a[href]",
                    )
                ) if top_container else []):
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

                    low_li = getattr(getattr(low_link, "parent", None), "parent", None)
                    sub_links = (
                        low_li.css(fp.get("subcategory", "ul.cbp-hrsub-level2 a[href], ul.cbp-hrsub-level3 a[href]"))
                        if low_li
                        else []
                    )
                    seen_sub_urls = set()
                    for sub_link in sub_links:
                        sub_name = self._text(sub_link)
                        sub_url = self._category_url(self._attr(sub_link, "href"))
                        if not sub_name or not sub_url or sub_url in seen_sub_urls:
                            continue
                        if sub_url in {top_url, low_url}:
                            continue
                        low_cat["subcategories"].append(
                            {"name": sub_name, "url": sub_url, "level": "subcategory"}
                        )
                        seen_sub_urls.add(sub_url)

                    top_cat["low_level_categories"].append(low_cat)

                categories.append(top_cat)

        for promo_link in tree.css(fp.get("promo_tabs", "#cbp-hrmenu > ul > li.cbp-hrmenu-tab:not(#cbp-hrmenu-tab-2) > a[href]")):
            name = self._text(promo_link)
            url = self._category_url(self._attr(promo_link, "href"))
            if not name or not url or url in seen_top_urls:
                continue
            categories.append(
                {"name": name, "url": url, "level": "top", "low_level_categories": []}
            )
            seen_top_urls.add(url)

        if not categories:
            fallback = []
            seen_urls = set()
            for link in tree.css(fp.get("fallback_links", "a[href]")):
                name = self._text(link)
                url = self._category_url(self._attr(link, "href"), allow_broad=True)
                if not name or not url or url in seen_urls:
                    continue
                fallback.append({"name": name, "url": url, "level": "low", "subcategories": []})
                seen_urls.add(url)
            if fallback:
                categories.append(
                    {
                        "name": "Catalog",
                        "url": None,
                        "level": "top",
                        "low_level_categories": fallback,
                    }
                )

        self.logger.info(
            "Found %s Twenty categories (%s queued URLs)",
            len(categories),
            self._category_stats(categories)["total_urls"],
        )
        return {"categories": categories, "stats": self._category_stats(categories)}

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
        if not sample_path.exists() and "article" in html and "product-miniature" in html:
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
        items = tree.css(cp.get("item_selector", "#js-product-list article.product-miniature.js-product-miniature"))
        if not items:
            items = tree.css("#js-product-list article.product-miniature, article.product-miniature")

        products: List[Dict[str, Any]] = []
        for item in items:
            product_id = clean_text(item.attributes.get("data-id-product"))
            link = self._first(
                item,
                [
                    "h2.product-title a[href]",
                    ".product-title a[href]",
                    "a.thumbnail.product-thumbnail[href]",
                    "a.product-thumbnail[href]",
                ],
            )
            product_url = self._absolute_url(self._attr(link, "href"))
            if not product_url or not self._is_product_url(product_url):
                continue

            title_node = self._first(item, ["h2.product-title a", ".product-title a", "h2", "h3"])
            name = self._text(title_node) or self._text(link)
            price = self._price_from_node(
                self._first(
                    item,
                    [
                        ".product-price-and-shipping .product-price[content]",
                        ".product-price[content]",
                        ".product-price",
                    ],
                )
            )
            old_price = self._price_from_node(
                self._first(item, [".product-price-and-shipping .regular-price", ".regular-price"])
            )
            discount_percent = self._discount_percent(
                self._text(self._first(item, [".product-flag.discount", ".discount-percentage", ".discount"]))
            )
            discount_percent = discount_percent or self._computed_discount(price, old_price)

            image_node = self._first(
                item,
                [
                    "a.thumbnail.product-thumbnail img",
                    "a.product-thumbnail img",
                    ".thumbnail-container img",
                    "img",
                ],
            )
            image = self._image_from_node(image_node)

            reference = self._text(self._first(item, [".product-reference a", ".product-reference"]))
            seller = self._text(self._first(item, [".product_list_shop_by a", ".product_list_shop_by"]))
            brand = self._text(self._first(item, [".product-brand a", ".product-brand"]))

            availability_node = self._first(item, [".product-availability"])
            availability, available = availability_from_text(self._text(availability_node))
            classes = clean_text(availability_node.attributes.get("class")) if availability_node else ""
            if available is None and classes and "product-unavailable" in classes.lower():
                availability, available = "Out-of-Stock", False
            if available is None and item.css_first(".add-to-cart, [data-button-action='add-to-cart']"):
                availability, available = "In stock", True

            product: Dict[str, Any] = {
                "id": product_id or self._product_id_from_url(product_url),
                "product_id": product_id or self._product_id_from_url(product_url),
                "url": product_url,
                "name": name,
                "price": price,
                "old_price": old_price,
                "discount_percent": discount_percent,
                "availability": availability,
                "available": available,
            }
            if image:
                product["image"] = image
            if reference:
                product["reference"] = reference
            if seller:
                product["seller"] = seller.replace("By:", "").strip() or seller
            if brand:
                product["brand"] = brand

            products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "twenty listing")

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
            text = self._text(link) or ""
            classes = (self._attr(link, "class") or "").lower()
            rel = (self._attr(link, "rel") or "").lower()

            if "next" in classes or rel == "next":
                if "disabled" not in classes:
                    has_next = True

            parsed_page = None
            if text.isdigit():
                parsed_page = int(text)
            else:
                query = dict(parse_qsl(urlsplit(href).query))
                if query.get("page", "").isdigit():
                    parsed_page = int(query["page"])

            if parsed_page:
                total_pages = max(total_pages, parsed_page)
                if "disabled" in classes or "current" in classes or "active" in classes:
                    current_page = parsed_page

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    # ------------------------------------------------------------------
    # Product detail
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        sample_path = self.html_dir / "detail_sample_1.html"
        if not sample_path.exists() and "#product-details" in html or (
            not sample_path.exists() and "data-product" in html
        ):
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

        reference = clean_text(product.get("reference"))
        if reference:
            data["reference"] = reference

        for key in ("ean13", "upc", "isbn", "mpn"):
            value = clean_text(product.get(key))
            if value:
                data.setdefault("barcode" if key in {"ean13", "upc"} else "reference", value)

        brand = clean_text(product.get("manufacturer_name"))
        if brand:
            data["brand"] = brand

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

        discount = self._discount_percent(product.get("discount_percentage"))
        data["discount_percent"] = discount or self._computed_discount(
            data.get("price"), data.get("old_price")
        )

        quantity = parse_price(product.get("quantity"))
        if quantity is not None:
            data["quantity"] = int(quantity)

        availability_raw = clean_text(product.get("availability"))
        if availability_raw == "available":
            data["availability"] = "In stock"
            data["available"] = True
        elif availability_raw == "unavailable":
            data["availability"] = "Out-of-Stock"
            data["available"] = False
        elif quantity is not None:
            data["availability"] = "In stock" if quantity > 0 else "Out-of-Stock"
            data["available"] = quantity > 0

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

        specs = self._specs_from_html(product.get("description_short"))
        if specs:
            data["specifications"] = specs

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
            ) or self._product_id_from_url(url)
            if product_id:
                data["product_id"] = product_id
                data["id"] = product_id

        reference = self._text(
            self._first(
                tree,
                [
                    ".product-prices .product-reference span",
                    ".product-reference span",
                    "[itemprop='sku']",
                ],
            )
        )
        if reference:
            data.setdefault("reference", reference)

        if not data.get("brand"):
            brand_node = self._first(
                tree,
                [
                    ".product-manufacturer a",
                    ".product-manufacturer img[alt]",
                    ".product-information .product-brand a",
                ],
            )
            brand = self._attr(brand_node, "alt") or self._text(brand_node)
            if brand:
                data["brand"] = brand

        price = self._price_from_node(
            self._first(
                tree,
                [
                    ".current-price [content]",
                    ".product-price[content]",
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
            data.setdefault("discount_percent", self._computed_discount(data.get("price"), old_price))

        availability_node = self._first(tree, ["#product-availability", ".product-prices #product-availability"])
        availability, available = availability_from_text(self._text(availability_node))
        if availability:
            data.setdefault("availability", availability)
        if available is not None:
            data.setdefault("available", available)

        short_description = self._text(
            self._first(tree, ["#product-description-short", ".product-description-short"])
        )
        if short_description:
            data.setdefault("short_description", short_description)
            data.setdefault("overview", short_description)

        description = self._text(
            self._first(
                tree,
                [
                    ".product-tabs .product-description",
                    ".product-information .product-description",
                    ".product-description",
                ],
            )
        )
        if description:
            data.setdefault("description", description)
            data.setdefault("full_description", description)

        specs = data.get("specifications") or {}
        specs.update(self._specs_from_dom(tree))
        if specs:
            data["specifications"] = specs

        breadcrumbs = [
            crumb
            for crumb in (self._text(node) for node in tree.css(".breadcrumb a[href]"))
            if crumb and crumb.lower() != "home"
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

    def _images_from_product_json(self, product: Dict[str, Any]) -> List[str]:
        urls = []
        images = product.get("images")
        if not isinstance(images, list):
            return []
        for image in images:
            if not isinstance(image, dict):
                continue
            by_size = image.get("bySize") if isinstance(image.get("bySize"), dict) else {}
            for size in ("thickbox_default", "large_default", "home_default", "medium_default"):
                size_info = by_size.get(size)
                if isinstance(size_info, dict):
                    url = self._absolute_url(size_info.get("url"))
                    if url:
                        urls.append(url)
                        break
            else:
                for key in ("large", "medium", "small"):
                    value = image.get(key)
                    if isinstance(value, dict):
                        url = self._absolute_url(value.get("url"))
                    else:
                        url = self._absolute_url(value)
                    if url:
                        urls.append(url)
                        break
        return self._dedupe_urls(urls)

    def _specs_from_html(self, value: Any) -> Dict[str, str]:
        html = clean_text(value)
        if not html or "<" not in html:
            return {}
        specs: Dict[str, str] = {}
        tree = HTMLParser(f"<div>{html}</div>")
        for li in tree.css("li"):
            label_node = li.css_first("strong")
            if not label_node:
                continue
            label = (self._text(label_node) or "").rstrip(":")
            full = self._text(li) or ""
            value_text = clean_text(full.replace(self._text(label_node) or "", "", 1).lstrip(":"))
            if label and value_text:
                specs[label] = value_text
        return specs

    def _specs_from_dom(self, tree: HTMLParser) -> Dict[str, str]:
        specs: Dict[str, str] = {}
        for dl in tree.css(".product-features dl, .data-sheet dl"):
            labels = dl.css("dt")
            values = dl.css("dd")
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


def get_scraper(logger: logging.Logger) -> TwentyScraper:
    """Factory used by scraper.sites registry."""
    return TwentyScraper(logger)
