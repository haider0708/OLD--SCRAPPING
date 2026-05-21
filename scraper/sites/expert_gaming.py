#!/usr/bin/env python3
"""
Expert-Gaming.tn specific scraper implementation.
Hybrid: Playwright for frontpage categories (JS-rendered WooCommerce menu),
httpx + selectolax for listing pages and product details.
"""
import json
import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser

from scraper.base import FastScraper, save_text_atomic


class ExpertGamingScraper(FastScraper):
    """Hybrid scraper for expert-gaming.tn (WooCommerce + Elementor + YITH)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("expert_gaming", logger)

    # ------------------------------------------------------------------
    # Hybrid override: Playwright for frontpage (JS-rendered menu)
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        """Download frontpage using Playwright (menu requires JS rendering)."""
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"📥 Downloading (Playwright): {self.base_url}")

        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            from scraper.base import playwright_launch_args, get_playwright_proxy
            browser = await pw.chromium.launch(headless=True, args=playwright_launch_args())
            page = await browser.new_page(
                proxy=get_playwright_proxy(self.site_name, self.config.get("settings", {}))
            )
            try:
                await page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_selector("ul#menu-notre-boutique", state="attached", timeout=10000)
                except Exception:
                    self.logger.warning("Menu selector not found, continuing with page content")
                html = await page.content()
            finally:
                await page.close()
                await browser.close()

        save_text_atomic(html, output_path, self.logger)
        self.logger.info(f"✓ Saved: {output_path} ({len(html):,} bytes)")
        return output_path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def _make_absolute_url(self, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("/"):
            return f"{self.base_url}{url}"
        return f"{self.base_url}/{url}"

    def _parse_price(self, text: str) -> Optional[float]:
        """Extract numeric price from text like '1,234.500 TND'."""
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", text)
        # Handle comma as thousands separator (1,234.500)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract category hierarchy from WooCommerce nav menu."""
        tree = HTMLParser(html)
        categories = []

        menu = tree.css_first("ul#menu-notre-boutique")
        if not menu:
            self.logger.warning("Could not find ul#menu-notre-boutique")
            return {"categories": [], "stats": {}}

        # Top-level: direct li children that are taxonomy product_cat items
        top_items = menu.css(
            "li.menu-item-type-taxonomy.menu-item-object-product_cat"
        )
        # Fallback: any direct li.menu-item children
        if not top_items:
            top_items = menu.css("li.menu-item")

        self.logger.info(f"Found {len(top_items)} top-level menu items")

        for top_li in top_items:
            top_link = top_li.css_first("a")
            if not top_link:
                continue

            top_name = self._clean_text(top_link.text(strip=True))
            top_url = self._make_absolute_url(top_link.attributes.get("href", ""))

            if not top_name:
                continue

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            # Low-level: sub-menu children
            sub_menu = top_li.css_first("ul.sub-menu")
            if sub_menu:
                low_items = sub_menu.css(
                    "li.menu-item-type-taxonomy.menu-item-object-product_cat"
                )
                if not low_items:
                    low_items = sub_menu.css("li.menu-item")

                for low_li in low_items:
                    low_link = low_li.css_first("a")
                    if not low_link:
                        continue

                    low_name = self._clean_text(low_link.text(strip=True))
                    low_url = self._make_absolute_url(
                        low_link.attributes.get("href", "")
                    )

                    if not low_name:
                        continue

                    low_cat = {
                        "name": low_name,
                        "url": low_url,
                        "level": "low",
                        "subcategories": [],
                    }

                    # Subcategories (nested sub-menu)
                    sub_sub_menu = low_li.css_first("ul.sub-menu")
                    if sub_sub_menu:
                        sub_items = sub_sub_menu.css(
                            "li.menu-item-type-taxonomy.menu-item-object-product_cat"
                        )
                        if not sub_items:
                            sub_items = sub_sub_menu.css("li.menu-item")

                        for sub_li in sub_items:
                            sub_link = sub_li.css_first("a")
                            if not sub_link:
                                continue
                            sub_name = self._clean_text(sub_link.text(strip=True))
                            sub_url = self._make_absolute_url(
                                sub_link.attributes.get("href", "")
                            )
                            if sub_name:
                                low_cat["subcategories"].append(
                                    {
                                        "name": sub_name,
                                        "url": sub_url,
                                        "level": "subcategory",
                                    }
                                )

                    top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        if categories and all(not c.get("low_level_categories") for c in categories):
            # Fallback: enrich with direct menu links so queue targets product categories first.
            anchors = menu.css("a[href]")
            fallback_lows = []
            seen = set()
            for a in anchors:
                href = self._make_absolute_url(a.attributes.get("href", ""))
                name = self._clean_text(a.text(strip=True))
                if not href or not name:
                    continue
                href_l = href.lower()
                if any(
                    bad in href_l
                    for bad in (
                        "/my-account",
                        "/panier",
                        "/wishlist",
                        "/contact",
                        "/a-propos",
                    )
                ):
                    continue
                # Expert category URLs are often slug-based and not always /product-category/.
                if not re.search(r"/[a-z0-9-]{3,}/?$", href_l):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                fallback_lows.append(
                    {"name": name, "url": href, "level": "low", "subcategories": []}
                )
            if fallback_lows:
                categories[0]["low_level_categories"] = fallback_lows

        # Stats
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

        return {"categories": categories, "stats": stats}

    # ------------------------------------------------------------------
    # Products (listing page)
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from a WooCommerce category listing page."""
        tree = HTMLParser(html)
        products = []
        seen_ids = set()

        for item in tree.css("li.product.type-product, section.product"):
            # Product ID from class like "post-12345"
            classes = item.attributes.get("class", "")
            product_id = None
            id_match = re.search(r"post-(\d+)", classes)
            if id_match:
                product_id = id_match.group(1)
            # Fallback to data attribute
            if not product_id:
                product_id = item.attributes.get("data-product_id")

            if product_id and product_id in seen_ids:
                continue
            if product_id:
                seen_ids.add(product_id)

            # Name
            name_el = item.css_first(
                "h2.woocommerce-loop-product__title, "
                "a.woocommerce-LoopProduct-link h2, "
                "h3.heading-title.product-name a, "
                "h2.product-title a"
            )
            product_name = self._clean_text(name_el.text(strip=True)) if name_el else ""
            if not product_name:
                img_name = item.css_first("img[alt]")
                if img_name:
                    product_name = self._clean_text(img_name.attributes.get("alt", ""))

            # URL
            link_el = item.css_first(
                "a.woocommerce-LoopProduct-link, "
                "h3.heading-title.product-name a, "
                "a[href]"
            )
            product_url = (
                self._make_absolute_url(link_el.attributes.get("href", ""))
                if link_el
                else None
            )

            if not product_id and not product_url:
                continue

            product_data = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }

            # Image
            img_el = item.css_first(
                "a.woocommerce-LoopProduct-link img, "
                "div.thumbnail-wrapper figure img.wp-post-image, "
                "img.attachment-woocommerce_thumbnail"
            )
            if img_el:
                image_url = (
                    img_el.attributes.get("src")
                    or img_el.attributes.get("data-src")
                    or img_el.attributes.get("data-lazy-src")
                )
                if image_url:
                    product_data["image"] = self._make_absolute_url(image_url)

            # Price – WooCommerce price structure
            # If <del> exists, first amount is old, <ins> amount is current
            del_el = item.css_first("span.price del span.woocommerce-Price-amount.amount bdi")
            ins_el = item.css_first("span.price ins span.woocommerce-Price-amount.amount bdi")

            if del_el and ins_el:
                product_data["old_price"] = self._parse_price(del_el.text())
                product_data["price"] = self._parse_price(ins_el.text())
            else:
                price_el = item.css_first(
                    "span.woocommerce-Price-amount.amount bdi, "
                    "span.price span.woocommerce-Price-amount bdi"
                )
                product_data["price"] = self._parse_price(
                    price_el.text() if price_el else None
                )

            if product_data.get("old_price") and product_data.get("price"):
                product_data["discount_percent"] = round(
                    (1 - product_data["price"] / product_data["old_price"]) * 100
                )

            # Brand (from loop categories link)
            brand_el = item.css_first("span.loop-product-categories a")
            if brand_el:
                product_data["brand"] = self._clean_text(brand_el.text(strip=True))

            products.append(product_data)

        if products:
            return products

        # Fallback: parse JSON-LD Product blocks
        for script in tree.css('script[type="application/ld+json"]'):
            raw = (script.text() or "").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            blocks = data if isinstance(data, list) else [data]
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("@type") != "Product":
                    continue
                url = self._make_absolute_url(block.get("url", ""))
                if not url:
                    continue
                products.append(
                    {
                        "id": str(block.get("sku") or block.get("productID") or ""),
                        "url": url,
                        "name": self._clean_text(block.get("name", "")),
                        "price": self._parse_price(
                            str((block.get("offers") or {}).get("price", ""))
                        ),
                    }
                )
        return products

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        """WooCommerce pagination: /page/{n}/ suffix."""
        # Strip trailing slash for consistency
        base = base_url.rstrip("/")
        # Remove existing /page/N/ if present
        base = re.sub(r"/page/\d+/?$", "", base)
        return f"{base}/page/{page_num}/"

    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from WooCommerce category page."""
        tree = HTMLParser(html)
        max_page = 1
        current_page = 1

        # Get all non-next page number links
        page_links = tree.css(
            "nav.woocommerce-pagination ul.page-numbers li a.page-numbers:not(.next):not(.prev), "
            "ul.page-numbers li a.page-numbers:not(.next):not(.prev)"
        )
        for link in page_links:
            try:
                num = int(link.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass

        # Current page (span, not link)
        current_el = tree.css_first(
            "nav.woocommerce-pagination ul.page-numbers li span.page-numbers.current, "
            "ul.page-numbers li span.page-numbers.current"
        )
        if current_el:
            try:
                current_page = int(current_el.text(strip=True))
                if current_page > max_page:
                    max_page = current_page
            except ValueError:
                pass

        has_next = tree.css_first(
            "nav.woocommerce-pagination ul.page-numbers li a.next, "
            "ul.page-numbers li a.next"
        ) is not None

        return {
            "current_page": current_page,
            "total_pages": max_page,
            "has_next": has_next,
        }

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape detailed product info from a WooCommerce product page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Product ID from URL or body class
        body = tree.css_first("body")
        if body:
            body_cls = body.attributes.get("class", "")
            id_match = re.search(r"postid-(\d+)", body_cls)
            if id_match:
                data["product_id"] = id_match.group(1)
        if "product_id" not in data:
            url_match = re.search(r"/product/[^/]+-(\d+)/?", url)
            data["product_id"] = url_match.group(1) if url_match else None

        # Title
        title_el = tree.css_first("h1.product_title.entry-title, h1.product_title")
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        # SKU
        sku_el = tree.css_first("span.sku, div.sku-wrapper span.sku")
        data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

        # Brand
        brand_el = tree.css_first(
            "div.product_meta span.posted_in a, "
            "div.product-brands a, "
            "div.product_meta .brand a"
        )
        data["brand"] = self._clean_text(brand_el.text(strip=True)) if brand_el else None

        # Price – handle del/ins for sale pricing
        del_el = tree.css_first("p.price del span.woocommerce-Price-amount.amount bdi")
        ins_el = tree.css_first("p.price ins span.woocommerce-Price-amount.amount bdi")

        if del_el and ins_el:
            data["old_price"] = self._parse_price(del_el.text())
            data["price"] = self._parse_price(ins_el.text())
        else:
            price_el = tree.css_first(
                "p.price span.woocommerce-Price-amount.amount bdi, "
                "span.woocommerce-Price-amount.amount bdi"
            )
            data["price"] = self._parse_price(price_el.text() if price_el else None)
            data["old_price"] = None

        if data.get("old_price") and data.get("price"):
            data["discount_percent"] = round(
                (1 - data["price"] / data["old_price"]) * 100
            )

        # Availability
        stock_el = tree.css_first("p.stock.in-stock")
        if stock_el:
            data["availability"] = self._clean_text(stock_el.text(strip=True))
            data["available"] = True
        else:
            oos_el = tree.css_first("p.stock.out-of-stock")
            if oos_el:
                data["availability"] = self._clean_text(oos_el.text(strip=True))
                data["available"] = False
            else:
                avail_el = tree.css_first(
                    "div.availability.stock span.availability-text, "
                    "p.stock"
                )
                if avail_el:
                    avail_text = avail_el.text(strip=True).lower()
                    data["availability"] = avail_el.text(strip=True)
                    data["available"] = (
                        "in stock" in avail_text
                        or "en stock" in avail_text
                        or "disponible" in avail_text
                    ) and "rupture" not in avail_text
                else:
                    data["availability"] = None
                    data["available"] = None

        # Description
        desc_el = tree.css_first(
            "div.woocommerce-product-details__short-description, "
            "div#tab-description .panel-body, "
            "div#tab-description"
        )
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        # Specifications (WooCommerce attributes table)
        specs = {}
        for row in tree.css(
            "table.woocommerce-product-attributes.shop_attributes tr, "
            "table.shop_attributes tr"
        ):
            key_el = row.css_first(
                "th.woocommerce-product-attributes-item__label, th"
            )
            val_el = row.css_first(
                "td.woocommerce-product-attributes-item__value, td"
            )
            if key_el and val_el:
                k = self._clean_text(key_el.text(strip=True))
                v = self._clean_text(val_el.text(strip=True))
                if k and v:
                    specs[k] = v
        data["specifications"] = specs

        # Images
        images = []

        # Main gallery image
        main_img = tree.css_first(
            "div.woocommerce-product-gallery__image img"
        )
        if main_img:
            src = (
                main_img.attributes.get("data-large_image")
                or main_img.attributes.get("data-src")
                or main_img.attributes.get("src")
            )
            if src:
                images.append(self._make_absolute_url(src))

        # Thumbnails / gallery items
        for img in tree.css(
            "ol.flex-control-thumbs li img, "
            "div.woocommerce-product-gallery__image:not(:first-child) img, "
            "div.woocommerce-product-gallery .woocommerce-product-gallery__image img"
        ):
            src = (
                img.attributes.get("data-large_image")
                or img.attributes.get("data-src")
                or img.attributes.get("src")
            )
            if src:
                abs_src = self._make_absolute_url(src)
                if abs_src not in images:
                    images.append(abs_src)

        data["images"] = images[:10] if images else None

        return data


def get_scraper(logger: logging.Logger) -> ExpertGamingScraper:
    """Factory function to get scraper."""
    return ExpertGamingScraper(logger)
