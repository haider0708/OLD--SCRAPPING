#!/usr/bin/env python3
"""
Mapara Tunisie (maparatunisie.tn) specific scraper implementation.
"""
import logging
import re
from typing import List, Dict, Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, save_text_atomic
from scraper.product_utils import (
    absolute_url,
    dedupe_products,
    finalize_product_record,
    html_product_metadata,
    parse_price,
)


class MaparaScraper(FastScraper):
    """HTTPX/selectolax-based scraper for maparatunisie.tn (WooCommerce + Flatsome)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("mapara", logger)

    async def download_frontpage(self):
        """Use Playwright for dynamic menu rendering."""
        output_path = self.html_dir / "frontpage.html"
        from playwright.async_api import async_playwright
        from scraper.base import playwright_launch_args

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=playwright_launch_args())
            page = await browser.new_page()
            try:
                await page.goto(self.base_url, wait_until="networkidle", timeout=45000)
                try:
                    await page.wait_for_selector("a[href*='mapara']", timeout=8000)
                except Exception:
                    pass
                html = await page.content()
            finally:
                await page.close()
                await browser.close()
        save_text_atomic(html, output_path, self.logger)
        return output_path

    def build_page_url(self, base_url: str, page_num: int) -> str:
        """Path-based pagination: /page/{n}/"""
        base = base_url.rstrip("/")
        # Remove existing /page/N/ if present
        base = re.sub(r"/page/\d+/?$", "", base)
        if page_num <= 1:
            return base + "/"
        return f"{base}/page/{page_num}/"

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract categories from maparatunisie.tn using dual dropdown navigation."""
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories = []

        top_blocks = tree.css(fp.get("top_level_blocks", "ul.header-nav.header-bottom-nav > li.menu-item.has-dropdown"))
        self.logger.info(f"Found {len(top_blocks)} top-level category blocks")
        if not top_blocks:
            return self._extract_categories_fallback(tree)

        for top_block in top_blocks:
            top_link = top_block.css_first(fp.get("top_level_link", "a.nav-top-link"))
            if not top_link:
                continue

            top_name = top_link.text(strip=True)
            top_url = top_link.attributes.get("href", "")
            if top_url and not top_url.startswith("http"):
                top_url = urljoin(self.base_url, top_url)

            # Skip non-category links (e.g. "#", javascript)
            if not top_url or top_url.endswith("#"):
                continue

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            seen_urls = set()

            # Mega dropdown links
            mega_links = top_block.css(fp.get("mega_low", "div.sub-menu.nav-dropdown div.text.text-mega p > a"))
            for link in mega_links:
                name = link.text(strip=True)
                url = link.attributes.get("href", "")
                if not name or not url:
                    continue
                if url and not url.startswith("http"):
                    url = urljoin(self.base_url, url)
                if url not in seen_urls:
                    top_cat["low_level_categories"].append({
                        "name": name,
                        "url": url,
                        "level": "low",
                        "subcategories": [],
                    })
                    seen_urls.add(url)

            # Simple dropdown links
            simple_links = top_block.css(fp.get("simple_low", "ul.sub-menu.nav-dropdown.nav-dropdown-simple > li.menu-item > a"))
            for link in simple_links:
                name = link.text(strip=True)
                url = link.attributes.get("href", "")
                if not name or not url:
                    continue
                if url and not url.startswith("http"):
                    url = urljoin(self.base_url, url)
                if url not in seen_urls:
                    top_cat["low_level_categories"].append({
                        "name": name,
                        "url": url,
                        "level": "low",
                        "subcategories": [],
                    })
                    seen_urls.add(url)

            categories.append(top_cat)

        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in categories:
            stats["top_level"] += 1
            if top.get("url"):
                stats["total_urls"] += 1
            for low in top.get("low_level_categories", []):
                stats["low_level"] += 1
                if low.get("url"):
                    stats["total_urls"] += 1

        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low categories ({stats['total_urls']} URLs)"
        )

        return {"categories": categories, "stats": stats}

    def _extract_categories_fallback(self, tree: HTMLParser) -> dict:
        seen = set()
        low = []
        for link in tree.css("a[href]"):
            href = (link.attributes.get("href") or "").strip()
            name = (link.text(strip=True) or "").strip()
            if not href or not name:
                continue
            if "mapara" not in href.lower():
                continue
            if any(
                bad in href.lower()
                for bad in ("/product/", "/cart", "/checkout", "/account", "/my-account")
            ):
                continue
            if href in seen:
                continue
            seen.add(href)
            low.append({"name": name, "url": href, "level": "low", "subcategories": []})

        cats = (
            [{"name": "Catalog", "url": None, "level": "top", "low_level_categories": low}]
            if low
            else []
        )
        return {
            "categories": cats,
            "stats": {
                "top_level": len(cats),
                "low_level": len(low),
                "subcategory": 0,
                "total_urls": len(low),
            },
        }

    def extract_products_from_html(self, html: str) -> List[Dict[str, Any]]:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        products = []

        items = tree.css(cp.get("item_selector", "div.product-small.col"))
        if not items:
            # Fixture-backed homepage cards.
            items = tree.css("div.product-small.box")

        for item in items:
            # Product ID from add-to-cart button
            id_el = item.css_first(cp.get("item_id_selector", "a.add_to_cart_button"))
            product_id = id_el.attributes.get(cp.get("item_id_attr", "data-product_id")) if id_el else None
            product_reference = (
                id_el.attributes.get(cp.get("item_reference_attr", "data-product_sku"))
                if id_el
                else None
            )

            # Name / URL
            name_el = item.css_first(cp.get("item_name", "p.name.product-title a"))
            if not name_el:
                continue
            product_name = name_el.text(strip=True)
            product_url = name_el.attributes.get("href", "")
            if not product_url:
                continue
            if not product_url.startswith("http"):
                product_url = urljoin(self.base_url, product_url)

            product_data: Dict[str, Any] = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }
            if product_reference:
                product_data["reference"] = product_reference

            # Price (sale price inside <ins>)
            price_el = item.css_first(cp.get("item_price", "ins span.woocommerce-Price-amount bdi"))
            if price_el:
                product_data["price"] = parse_price(price_el.text())
            else:
                # Fallback: single price (no sale)
                single_price = item.css_first("span.price span.woocommerce-Price-amount bdi, span.woocommerce-Price-amount bdi")
                if single_price:
                    product_data["price"] = parse_price(single_price.text())
                else:
                    product_data["price"] = None

            # Old price
            old_price_el = item.css_first(cp.get("item_old_price", "del span.woocommerce-Price-amount bdi"))
            if old_price_el:
                product_data["old_price"] = parse_price(old_price_el.text())

            # Availability from CSS classes
            item_classes = item.attributes.get("class", "")
            parent = item.parent
            parent_classes = parent.attributes.get("class", "") if parent else ""
            combined_classes = item_classes + " " + parent_classes
            if "instock" in combined_classes:
                product_data["availability"] = "En stock"
                product_data["available"] = True
            elif "outofstock" in combined_classes:
                product_data["availability"] = "En rupture"
                product_data["available"] = False

            # Image
            img_el = item.css_first(cp.get("item_image", "div.image-fade_in_back picture img"))
            if not img_el:
                img_el = item.css_first("div.box-image div.image-cover picture img, div.box-image div.image-cover img")
            if img_el:
                image_url = None
                for attr in cp.get("item_image_attrs", ["src", "data-src"]):
                    image_url = img_el.attributes.get(attr)
                    if image_url and not image_url.startswith("data:"):
                        break
                if image_url:
                    image_url = absolute_url(image_url, self.base_url)
                    if image_url:
                        product_data["image"] = image_url

            products.append(finalize_product_record(product_data))

        return dedupe_products(products, self.logger, "mapara listing")

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})

        current_page = 1
        total_pages = 1
        has_next = False

        # Page numbers from pagination links
        page_links = tree.css("a.page-number, span.page-number")
        for el in page_links:
            classes = el.attributes.get("class", "")
            try:
                num = int(el.text(strip=True))
                if num > total_pages:
                    total_pages = num
                if "current" in classes:
                    current_page = num
            except (ValueError, TypeError):
                continue

        next_link = tree.css_first(cp.get("pagination_next", "a.next.page-number"))
        if next_link:
            has_next = True

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = {"url": url}

        # Product ID from URL or body class
        url_match = re.search(r"[\-/](\d+)[\-.]", url)
        if url_match:
            data["product_id"] = url_match.group(1)
        else:
            # Try WooCommerce body class postid-NNN
            body = tree.css_first("body")
            if body:
                body_class = body.attributes.get("class", "")
                id_match = re.search(r"postid-(\d+)", body_class)
                data["product_id"] = id_match.group(1) if id_match else None
            else:
                data["product_id"] = None

        # Title
        title_el = tree.css_first(pp.get("title", "h1.product-title.product_title.entry-title"))
        data["title"] = title_el.text(strip=True) if title_el else None

        # Price (sale price)
        price_el = tree.css_first(pp.get("price", "p.price ins span.woocommerce-Price-amount bdi"))
        if price_el:
            price_text = re.sub(r"[^\d.,]", "", price_el.text()).replace(",", ".")
            try:
                data["price"] = float(price_text) if price_text else None
            except ValueError:
                data["price"] = None
        else:
            # Fallback: single price (no sale)
            single_price = tree.css_first("p.price span.woocommerce-Price-amount bdi")
            if single_price:
                price_text = re.sub(r"[^\d.,]", "", single_price.text()).replace(",", ".")
                try:
                    data["price"] = float(price_text) if price_text else None
                except ValueError:
                    data["price"] = None
            else:
                data["price"] = None

        # Old price
        old_price_el = tree.css_first(pp.get("old_price", "p.price del span.woocommerce-Price-amount bdi"))
        if old_price_el:
            old_text = re.sub(r"[^\d.,]", "", old_price_el.text()).replace(",", ".")
            try:
                data["old_price"] = float(old_text) if old_text else None
            except ValueError:
                data["old_price"] = None

        # Brand from image alt
        brand_el = tree.css_first(pp.get("brand", "a[href*='/nos-marques/'] img"))
        if brand_el:
            data["brand"] = brand_el.attributes.get(pp.get("brand_attr", "alt"))
        else:
            data["brand"] = None

        # Availability
        add_btn = tree.css_first(pp.get("availability_add_to_cart", "form.cart button[name='add-to-cart']"))
        if add_btn:
            data["availability"] = "En stock"
            data["available"] = True
        else:
            # Check for out-of-stock notice
            oos = tree.css_first("p.stock.out-of-stock, .out-of-stock")
            if oos:
                data["availability"] = "En rupture"
                data["available"] = False
            else:
                data["availability"] = None
                data["available"] = None

        # Description
        desc_el = tree.css_first(pp.get("description", "div.woocommerce-Tabs-panel--description"))
        data["description"] = desc_el.text(strip=True) if desc_el else None

        # Images from gallery
        images = []
        for gallery_img in tree.css(pp.get("image_gallery", "div.woocommerce-product-gallery__image.slide a")):
            href = gallery_img.attributes.get("href")
            if href and href not in images:
                images.append(absolute_url(href, self.base_url) or href)

        # Fallback: main image
        if not images:
            main_img = tree.css_first(pp.get("image_main", "div.woocommerce-product-gallery__image.slide.first img.wp-post-image"))
            if main_img:
                src = main_img.attributes.get("src")
                if src:
                    images.append(absolute_url(src, self.base_url) or src)

        data["images"] = images if images else None

        metadata = html_product_metadata(html, url, self.base_url)
        for key, value in metadata.items():
            if key in {"barcode", "reference", "sku", "currency"}:
                data[key] = value
            elif data.get(key) in (None, "", [], {}):
                data[key] = value

        return finalize_product_record(data)


def get_scraper(logger: logging.Logger) -> MaparaScraper:
    return MaparaScraper(logger)
