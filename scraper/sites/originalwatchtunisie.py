#!/usr/bin/env python3
"""
Original Watch Tunisie scraper - custom PHP ecommerce, HTTP/selectolax.
"""

import asyncio
import logging
import math
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


class OriginalWatchTunisieScraper(FastScraper):
    """HTTP scraper for originalwatchtunisie.com custom PHP pages."""

    CATEGORY_DEFINITIONS = (
        ("MH", "Montre Homme", "https://www.originalwatchtunisie.com/products.php?cat=MH"),
        ("Gggh", "Montre Femme", "https://www.originalwatchtunisie.com/products.php?cat=Gggh"),
        ("SM", "Smart Watch", "https://www.originalwatchtunisie.com/products.php?cat=SM"),
    )
    CATEGORY_ALIASES = {
        "mh": "MH",
        "ma": "MH",
        "mc": "MH",
        "chronographs": "MH",
        "gggh": "Gggh",
        "sm": "SM",
        "smartwatches": "SM",
    }
    BAD_URL_PARTS = (
        "wa.me",
        "facebook.com",
        "tiktok.com",
        "mailto:",
        "tel:",
        "javascript:",
    )

    def __init__(self, logger: logging.Logger):
        super().__init__("originalwatchtunisie", logger)
        self._category_by_code = {
            code: {"code": code, "name": name, "url": url}
            for code, name, url in self.CATEGORY_DEFINITIONS
        }

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
    def _fold(value: Any) -> str:
        text = clean_text(value) or ""
        text = text.replace("\u2212", "-").lower()
        return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")

    @staticmethod
    def _strip_url(url: Any, keep_query: bool = True) -> str:
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

    def _is_site_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        base_host = urlsplit(self.base_url).netloc.lower().removeprefix("www.")
        return host == base_host

    def _category_code_from_url(self, value: Any) -> Optional[str]:
        url = self._absolute_url(value)
        if not url or not self._is_site_url(url):
            return None
        parts = urlsplit(url)
        if not parts.path.endswith("products.php"):
            return None
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        raw_code = clean_text(params.get("cat") or params.get("category"))
        if not raw_code:
            return None
        return self.CATEGORY_ALIASES.get(raw_code.lower())

    def _canonical_category(self, value: Any) -> Optional[Dict[str, str]]:
        code = self._category_code_from_url(value)
        if not code:
            return None
        item = self._category_by_code.get(code)
        if not item:
            return None
        return {"name": item["name"], "url": item["url"], "code": item["code"]}

    def _product_url(self, value: Any) -> Optional[str]:
        url = self._absolute_url(value)
        if not url or not self._is_site_url(url):
            return None
        if any(token in url.lower() for token in self.BAD_URL_PARTS):
            return None
        parts = urlsplit(url)
        if not parts.path.endswith("product.php"):
            return None
        params = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if key in {"id", "slug"} and val]
        if not params:
            return None
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(params), ""))

    @staticmethod
    def _product_id_from_url(url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        if not url:
            return None, None
        params = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        product_id = clean_text(params.get("id"))
        slug = clean_text(params.get("slug"))
        if product_id:
            return product_id, None
        return slug, slug

    def _node_price(self, node: Any) -> Optional[float]:
        if not node:
            return None
        return parse_price(self._attr(node, "content") or self._text(node))

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

    def _image_from_node(self, node: Any, base_url: Optional[str] = None) -> Optional[str]:
        if not node:
            return None
        value = node.attributes.get("data-src") or node.attributes.get("content") or node.attributes.get("src")
        url = self._absolute_url(value, base_url)
        return self._clean_image_url(url)

    def _clean_image_url(self, value: Any) -> Optional[str]:
        url = self._absolute_url(value)
        if not url:
            return None
        low = url.lower()
        if "facebook.com/tr" in low or "/assets/images/logo" in low or "/uploads/videos/" in low:
            return None
        if not re.search(r"\.(?:jpe?g|png|webp|gif)(?:\?|$)", low):
            return None
        return url

    @staticmethod
    def _discount_percent(value: Any) -> Optional[int]:
        text = clean_text(value)
        if not text:
            return None
        text = text.replace("\u2212", "-")
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
        categories = [
            {
                "name": name,
                "url": url,
                "level": "top",
                "category_id": code,
                "low_level_categories": [],
            }
            for code, name, url in self.CATEGORY_DEFINITIONS
        ]
        stats = {"top_level": 3, "low_level": 0, "subcategory": 0, "total_urls": 3}
        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Listings and pagination
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        tree = HTMLParser(html)
        selector = self.selectors.get("category_page", {}).get("item_selector", "article.product-card")
        products = []
        for card in tree.css(selector):
            product = self._product_from_card(card)
            if product:
                products.append(product)
        return dedupe_products(products, self.logger, "originalwatchtunisie listing")

    def _product_from_card(self, card: Any) -> Optional[Dict[str, Any]]:
        cp = self.selectors.get("category_page", {})
        url = self._product_url(self._attr(card, "data-href"))
        if not url:
            return None

        product_id, slug = self._product_id_from_url(url)
        name = self._text(card.css_first(cp.get("item_name", ".product-title")))
        name = name or self._attr(card, "title")
        if not name:
            name = self._attr(card.css_first(cp.get("item_image", ".image-wrap img[src]")), "alt")
        if not name:
            return None

        brand = self._attr(card.css_first(".pc-brandLogo[alt]"), "alt")
        brand = brand or self._attr(card.css_first(".pc-brandLink[title]"), "title")
        if not brand:
            brand_href = self._attr(card.css_first(".pc-brandLink[href]"), "href") or ""
            brand = clean_text(dict(parse_qsl(urlsplit(brand_href).query)).get("brands"))

        price = self._node_price(card.css_first(cp.get("item_price", ".price .current")))
        old_price = self._node_price(card.css_first(cp.get("item_old_price", ".price .old")))
        if old_price is not None and price is not None and old_price <= price:
            old_price = None
        discount_percent = self._discount_percent(self._text(card.css_first(cp.get("item_discount", ".promo-tag"))))
        if discount_percent is None:
            discount_percent = self._computed_discount(price, old_price)

        image = self._image_from_node(card.css_first(cp.get("item_image", ".image-wrap img[src]")))

        record: Dict[str, Any] = {
            "id": product_id,
            "product_id": product_id,
            "slug": slug,
            "url": url,
            "name": name,
            "title": name,
            "shop": self.site_name,
            "brand": brand,
            "price": price,
            "old_price": old_price,
            "discount_percent": discount_percent,
            "image": image,
            "currency": "TND",
        }
        return finalize_product_record({key: value for key, value in record.items() if value is not None})

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        all_products: List[dict] = []
        first = await self.scrape_category_page(category_url)
        if first.get("error"):
            return []
        all_products.extend(first.get("products") or [])
        total_pages = first.get("pagination", {}).get("total_pages", 1) or 1

        if limit and len(all_products) >= limit:
            return dedupe_products(all_products, self.logger, "originalwatchtunisie category")[:limit]
        if total_pages <= 1:
            deduped = dedupe_products(all_products, self.logger, "originalwatchtunisie category")
            return deduped[:limit] if limit else deduped

        page_sem = asyncio.Semaphore(4)

        async def fetch_page(page_num: int) -> List[dict]:
            async with page_sem:
                result = await self.scrape_category_page(self.build_page_url(category_url, page_num))
                if result.get("error"):
                    self.logger.debug(f"Original Watch page {page_num} failed for {category_url}: {result['error']}")
                    return []
                return result.get("products") or []

        if limit:
            for page_num in range(2, total_pages + 1):
                all_products.extend(await fetch_page(page_num))
                if len(dedupe_products(all_products, None, "")) >= limit:
                    break
        else:
            page_results = await asyncio.gather(
                *[fetch_page(page_num) for page_num in range(2, total_pages + 1)],
                return_exceptions=True,
            )
            for page_result in page_results:
                if isinstance(page_result, Exception):
                    self.logger.debug(f"Original Watch page fetch error: {page_result}")
                else:
                    all_products.extend(page_result)

        deduped = dedupe_products(all_products, self.logger, "originalwatchtunisie category")
        return deduped[:limit] if limit else deduped

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        current_page = 1
        total_pages = 1
        total_results = None

        active = tree.css_first(".pagination span.active")
        active_page = self._safe_int(self._text(active))
        if active_page:
            current_page = active_page
            total_pages = max(total_pages, active_page)

        for node in tree.css(".pagination a[href*='page='], .pagination span"):
            href = self._attr(node, "href") or ""
            query_page = self._safe_int(dict(parse_qsl(urlsplit(href).query)).get("page"))
            text_page = self._safe_int(self._text(node))
            page_num = query_page or text_page
            if page_num:
                total_pages = max(total_pages, page_num)

        result_text = self._text(tree.css_first("body")) or ""
        result_match = re.search(r"Affichage\s+de\s+\d+\s*[–-]\s*\d+\s+sur\s+(\d+)\s+r[eé]sultats", result_text, re.I)
        if result_match:
            total_results = self._safe_int(result_match.group(1))
            per_page = len(self.extract_products_from_html(html))
            if total_results and per_page:
                total_pages = max(total_pages, math.ceil(total_results / per_page))

        next_link = None
        for node in tree.css(".pagination a[href*='page=']"):
            text = self._fold(self._text(node))
            class_name = (node.attributes.get("class") or "").lower()
            href = self._attr(node, "href") or ""
            page = self._safe_int(dict(parse_qsl(urlsplit(href).query)).get("page"))
            if "suivant" in text and "disabled" not in class_name:
                next_link = href
                break
            if page and page > current_page and "disabled" not in class_name:
                next_link = href

        return {
            "current_page": current_page,
            "total_pages": max(1, total_pages),
            "has_next": bool(next_link) or total_pages > current_page,
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
            self.logger.debug(f"Original Watch detail parse failed: {product_url} ({exc})")
            return {"url": product_url, "error": str(exc)}

    def extract_product_details_from_html(self, html: str, product_url: str) -> dict:
        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = html_product_metadata(html, product_url, self.base_url)

        canonical = self._product_url(
            self._first_attr_or_text(tree, ["link[rel='canonical'][href]", "meta[property='og:url']"], ["href", "content"])
        )
        data["url"] = canonical or self._product_url(product_url) or product_url

        product_id, slug = self._product_id_from_url(data["url"])
        if product_id:
            data["id"] = product_id
            data["product_id"] = product_id
        if slug:
            data["slug"] = slug

        title = self._clean_title(
            self._first_attr_or_text(
                tree,
                [pp.get("title", "section.meta h1, h1, meta[property='og:title']")],
                ["content"],
            )
        )
        if title:
            data["title"] = title
            data["name"] = title

        brand = self._attr(tree.css_first(pp.get("brand", "section.meta .brand-logo[alt], .brand-logo[alt]")), "alt")
        if brand:
            data["brand"] = brand

        price = self._node_price(tree.css_first(pp.get("price", "section.meta .price-now, .price-now, .price")))
        if price is not None:
            data["price"] = price
        old_price = self._node_price(tree.css_first(pp.get("old_price", "section.meta .price-old, .price-old")))
        if old_price is not None and data.get("price") is not None and old_price > data["price"]:
            data["old_price"] = old_price

        discount_percent = self._discount_percent(self._text(tree.css_first(pp.get("discount", "section.meta .promo-chip, .promo-chip"))))
        if discount_percent is None:
            discount_percent = self._computed_discount(data.get("price"), data.get("old_price"))
        if discount_percent is not None:
            data["discount_percent"] = discount_percent

        availability, available = self._detail_availability(tree, pp)
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        description, specs = self._detail_text_blocks(tree, pp)
        if description:
            data["description"] = description
            data["short_description"] = description
            data["overview"] = description
            data["full_description"] = description
        if specs:
            data["specifications"] = specs

        images = self._detail_images(tree, pp, data)
        if images:
            data["images"] = images
            data["image"] = images[0]

        categories = self._detail_categories(tree, pp)
        if categories:
            data["categories"] = categories
            data["breadcrumbs"] = [category["name"] for category in categories]

        data["currency"] = data.get("currency") or "TND"
        data["shop"] = self.site_name
        return finalize_product_record({key: value for key, value in data.items() if value is not None})

    @staticmethod
    def _clean_title(value: Any) -> Optional[str]:
        title = clean_text(value)
        if not title:
            return None
        title = re.sub(r"\s*\|\s*Original\s+Watch\s+TN\s*$", "", title, flags=re.I)
        title = re.sub(r"\s*-\s*Original\s+Watch\s+TN\s*$", "", title, flags=re.I)
        return clean_text(title)

    def _detail_availability(self, tree: HTMLParser, pp: Dict[str, Any]) -> Tuple[Optional[str], Optional[bool]]:
        text = self._text(tree.css_first(pp.get("availability", "section.meta .avail-badge, .avail-badge")))
        folded = self._fold(text)
        if "epuise" in folded or "rupture" in folded or "out of stock" in folded:
            return text or "Epuise", False
        if "en stock" in folded or "instock" in folded:
            return text or "En stock", True
        if "sur commande" in folded:
            return text or "Sur commande", None
        return availability_from_text(text)

    def _detail_text_blocks(self, tree: HTMLParser, pp: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
        description = None
        specs: Dict[str, Any] = {}

        for block in tree.css(pp.get("description_blocks", "details.spec")):
            label = self._text(block.css_first("summary"))
            folded_label = self._fold(label)
            body = self._text(block.css_first(".body") or block, separator=" ")
            if "description" in folded_label and body:
                body = re.sub(r"^\s*DESCRIPTION\s*", "", body, flags=re.I)
                description = clean_text(body) or description
            elif "caracteristiques" in folded_label:
                spec_text = self._text(block.css_first(".body"), separator=" ")
                if spec_text:
                    specs["caracteristiques"] = spec_text
                spec_img = block.css_first(".spec-img[src]")
                spec_img_url = self._image_from_node(spec_img)
                if spec_img_url:
                    specs["caracteristiques_image"] = spec_img_url
                    alt = self._attr(spec_img, "alt")
                    if alt:
                        specs["caracteristiques_image_alt"] = alt

        badges = []
        for item in tree.css("section.meta .badges li, .badges li"):
            text = self._text(item)
            if text:
                badges.append(text)
        if badges:
            specs["badges"] = self._dedupe_list(badges)

        return description, specs

    def _detail_images(self, tree: HTMLParser, pp: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
        images: List[str] = []
        for node in tree.css(pp.get("image_gallery", "section.gallery .gallery-main img[src], section.gallery .thumb[data-src], section.gallery img[src], meta[property='og:image']")):
            if "thumb" in (node.attributes.get("class") or "") and node.attributes.get("data-type") not in {None, "image"}:
                continue
            image = self._image_from_node(node)
            if image:
                images.append(image)

        for image in data.get("images") or []:
            image_url = image.get("url") if isinstance(image, dict) else image
            image_url = self._clean_image_url(image_url)
            if image_url:
                images.append(image_url)
        return self._dedupe_list(images)[:30]

    def _detail_categories(self, tree: HTMLParser, pp: Dict[str, Any]) -> List[Dict[str, str]]:
        categories: List[Dict[str, str]] = []
        seen = set()
        for link in tree.css(pp.get("breadcrumbs", ".breadcrumb a[href*='products.php'], [class*='breadcrumb'] a[href*='products.php']")):
            category = self._canonical_category(self._attr(link, "href"))
            if not category:
                continue
            key = category["code"]
            if key in seen:
                continue
            seen.add(key)
            categories.append({"name": category["name"], "url": category["url"]})
        return categories


def get_scraper(logger: logging.Logger) -> OriginalWatchTunisieScraper:
    return OriginalWatchTunisieScraper(logger)
