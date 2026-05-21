#!/usr/bin/env python3
"""
Scoop Gaming (scoopgaming.com.tn) specific scraper implementation.
Hybrid: Playwright for frontpage categories (JS-rendered TvCMS mega-menu),
httpx + selectolax for listing pages and product details.
"""
import json
import logging
import re
from typing import List, Optional
from selectolax.parser import HTMLParser

from scraper.base import FastScraper, save_text_atomic


class ScoopScraper(FastScraper):
    """Hybrid scraper for scoopgaming.com.tn (PrestaShop + TvCMS MegaMenu)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("scoop", logger)

    # ------------------------------------------------------------------
    # Hybrid override: Playwright for frontpage (JS-rendered TvCMS menu)
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        """Download frontpage using Playwright (TvCMS menu requires JS)."""
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"📥 Downloading (Playwright): {self.base_url}")

        fp = self.selectors.get("frontpage", {})
        wait_sel = fp.get("wait_selector", "div#tvdesktop-megamenu ul.menu-content > li.level-1 > a")

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
                    await page.wait_for_selector(wait_sel, timeout=15000)
                except Exception:
                    self.logger.warning(f"Wait selector '{wait_sel}' not found, continuing")
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
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return f"{self.base_url}{url}"
        return f"{self.base_url}/{url}"

    def _parse_price(self, text: str) -> Optional[float]:
        """Extract numeric price from text like '1 234,500 DT'."""
        if not text:
            return None
        cleaned = re.sub(r"[^\d.,]", "", text)
        # Handle comma as decimal separator (European: 1234,500)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Categories – TvCMS dual-menu
    # ------------------------------------------------------------------

    def _extract_menu_items(self, container, depth_prefix: str = "") -> List[dict]:
        """Extract categories from a TvCMS menu container (dropdown or mega)."""
        categories = []

        top_items = container.css("li[data-depth='0']")
        if not top_items:
            top_items = container.css("li.level-1")

        for top_li in top_items:
            top_link = top_li.css_first("a")
            if not top_link:
                continue

            # Name from span.tvcms_menu_name or direct text
            name_span = top_link.css_first("span.tvcms_menu_name")
            top_name = self._clean_text(
                name_span.text(strip=True) if name_span else top_link.text(strip=True)
            )
            top_url = self._make_absolute_url(top_link.attributes.get("href", ""))

            if not top_name or top_name == "#":
                continue

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            # Low-level: depth="1" items
            low_items = top_li.css("ul > li[data-depth='1']")
            if not low_items:
                low_items = top_li.css("ul.menu-dropdown > li.level-2, div.menu-dropdown li.tvmega-menu-link.item-header")

            for low_li in low_items:
                low_link = low_li.css_first("a")
                if not low_link:
                    continue

                low_name_span = low_link.css_first("span.tvcms_menu_name")
                low_name = self._clean_text(
                    low_name_span.text(strip=True)
                    if low_name_span
                    else low_link.text(strip=True)
                )
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

                # Subcategories: depth="2" items
                sub_items = low_li.css("ul > li[data-depth='2']")
                if not sub_items:
                    sub_items = low_li.css("ul.menu-dropdown > li.level-3, ul > li.tvmega-menu-link.item-line")

                for sub_li in sub_items:
                    sub_link = sub_li.css_first("a")
                    if not sub_link:
                        continue
                    sub_name_span = sub_link.css_first("span.tvcms_menu_name")
                    sub_name = self._clean_text(
                        sub_name_span.text(strip=True)
                        if sub_name_span
                        else sub_link.text(strip=True)
                    )
                    sub_url = self._make_absolute_url(
                        sub_link.attributes.get("href", "")
                    )
                    if sub_name:
                        low_cat["subcategories"].append(
                            {"name": sub_name, "url": sub_url, "level": "subcategory"}
                        )

                top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

        return categories

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract category hierarchy from TvCMS dual-menu structure."""
        tree = HTMLParser(html)
        all_categories = []
        seen_names = set()

        # Menu 1: tvcms-dropdown-menu
        dropdown_menu = tree.css_first("ul.tvcms-dropdown-menu")
        if dropdown_menu:
            cats = self._extract_menu_items(dropdown_menu)
            for cat in cats:
                if cat["name"] not in seen_names:
                    seen_names.add(cat["name"])
                    all_categories.append(cat)

        # Menu 2: tvcms-mega-menu
        mega_menu = tree.css_first("ul.tvcms-mega-menu")
        if mega_menu:
            cats = self._extract_menu_items(mega_menu)
            for cat in cats:
                if cat["name"] not in seen_names:
                    seen_names.add(cat["name"])
                    all_categories.append(cat)

        # Fallback: general TvCMS menu container
        if not all_categories:
            container = tree.css_first(
                "div#tvdesktop-megamenu ul.menu-content, "
                "div.tvcms-main-menu ul"
            )
            if container:
                all_categories = self._extract_menu_items(container)

        self.logger.info(f"Found {len(all_categories)} top-level categories")

        if all_categories and all(not c.get("low_level_categories") for c in all_categories):
            # Fallback: synthesize low categories from visible category links.
            links = tree.css("a[href*='/category/'], a[href*='/product-category/']")
            low = []
            seen = set()
            for l in links:
                href = self._make_absolute_url(l.attributes.get("href", ""))
                name = self._clean_text(l.text(strip=True))
                if not href or not name or href in seen:
                    continue
                seen.add(href)
                low.append({"name": name, "url": href, "level": "low", "subcategories": []})
            if low:
                all_categories[0]["low_level_categories"] = low

        # Stats
        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in all_categories:
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

        return {"categories": all_categories, "stats": stats}

    # ------------------------------------------------------------------
    # Products (listing page)
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from a PrestaShop category listing page."""
        tree = HTMLParser(html)
        products = []
        seen_ids = set()

        items = tree.css("article.product-miniature.js-product-miniature")
        if not items:
            # Fixture-backed fallback: homepage popup cards use tvproduct-wrapper.
            items = tree.css("div.tvproduct-wrapper")

        for item in items:
            product_id = item.attributes.get("data-id-product")

            if product_id and product_id in seen_ids:
                continue
            if product_id:
                seen_ids.add(product_id)

            # Name
            name_el = item.css_first(
                "h2.product-title a, "
                "h3.product-title a, "
                "h6[itemprop='name'], "
                "div.tvproduct-name.product-title a h6, "
                "div.tvproduct-name a"
            )
            product_name = self._clean_text(name_el.text(strip=True)) if name_el else ""

            # URL
            link_el = item.css_first(
                "h2.product-title a, "
                "h3.product-title a, "
                "div.tvproduct-name.product-title a, "
                "a.thumbnail.product-thumbnail"
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
                "img.product-thumbnail-first, "
                "img.tvproduct-defult-img, "
                "a.thumbnail.product-thumbnail img, "
                "img.img-responsive"
            )
            if img_el:
                image_url = (
                    img_el.attributes.get("src")
                    or img_el.attributes.get("data-src")
                    or img_el.attributes.get("data-lazy-src")
                )
                if image_url:
                    product_data["image"] = self._make_absolute_url(image_url)

            # Price
            price_el = item.css_first(
                "span.price, "
                "div.product-price-and-shipping span.price"
            )
            product_data["price"] = self._parse_price(
                price_el.text() if price_el else None
            )

            # Old price
            old_price_el = item.css_first(
                "span.regular-price, "
                "div.product-price-and-shipping span.regular-price"
            )
            if old_price_el:
                product_data["old_price"] = self._parse_price(old_price_el.text())
                if product_data.get("old_price") and product_data.get("price"):
                    product_data["discount_percent"] = round(
                        (1 - product_data["price"] / product_data["old_price"]) * 100
                    )

            # Brand
            brand_el = item.css_first(
                "span.product-manufacturer a.brand, "
                "span.manufacturer a, "
                "div.product-manufacturer a"
            )
            if brand_el:
                product_data["brand"] = self._clean_text(brand_el.text(strip=True))

            # Out of stock flag
            oos_flag = item.css_first(
                "ul.product-flags > li.product-flag.out_of_stock, "
                "li.out_of_stock"
            )
            if oos_flag:
                product_data["availability"] = "Rupture de stock"
                product_data["available"] = False

            products.append(product_data)

        if products:
            return products

        # JSON-LD fallback
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
                if not isinstance(block, dict) or block.get("@type") != "Product":
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
        """PrestaShop pagination: ?page={n} query parameter."""
        # Remove existing page param
        base = re.sub(r"[?&]page=\d+", "", base_url)
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}page={page_num}"

    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from PrestaShop category page."""
        tree = HTMLParser(html)
        max_page = 1
        current_page = 1

        # Check for next page link
        next_link = tree.css_first("a.next.js-search-link[rel='next']")
        has_next = next_link is not None

        # Parse all page links for max
        for page_link in tree.css("nav.pagination a.js-search-link, div.pagination a.js-search-link"):
            href = page_link.attributes.get("href", "")
            page_match = re.search(r"[?&]page=(\d+)", href)
            if page_match:
                try:
                    num = int(page_match.group(1))
                    if num > max_page:
                        max_page = num
                except ValueError:
                    pass
            # Also try text content
            try:
                num = int(page_link.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass

        # Current page from active/current element
        current_el = tree.css_first(
            "nav.pagination li.current a, "
            "nav.pagination li.active a, "
            "div.pagination li.current a"
        )
        if current_el:
            try:
                current_page = int(current_el.text(strip=True))
            except ValueError:
                pass

        return {
            "current_page": current_page,
            "total_pages": max_page,
            "has_next": has_next,
        }

    # ------------------------------------------------------------------
    # Product details
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape detailed product info from a PrestaShop product page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Try JSON-primary approach: data-product attribute
        json_el = tree.css_first("div.tab-pane#product-details[data-product]")
        product_json = None
        if json_el:
            raw = json_el.attributes.get("data-product", "")
            if raw:
                try:
                    product_json = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    pass

        if product_json:
            data["product_id"] = str(product_json.get("id_product", "")) or None
            data["title"] = product_json.get("name")
            data["sku"] = product_json.get("reference")

            # Price from JSON
            price_val = product_json.get("price_amount")
            if price_val is not None:
                try:
                    data["price"] = float(price_val)
                except (ValueError, TypeError):
                    data["price"] = None
            else:
                data["price"] = self._parse_price(product_json.get("price"))

            # Availability
            data["availability"] = product_json.get("availability_message")
            avail = product_json.get("availability")
            if avail == "available":
                data["available"] = True
            elif avail == "unavailable" or avail == "last_remaining_items":
                data["available"] = avail == "last_remaining_items"
            else:
                data["available"] = (
                    product_json.get("quantity", 0) > 0
                    if isinstance(product_json.get("quantity"), (int, float))
                    else None
                )
        else:
            # Product ID from URL (PrestaShop: /123-slug.html)
            url_match = re.search(r"/(\d+)-", url)
            data["product_id"] = url_match.group(1) if url_match else None

            # Title
            title_el = tree.css_first("h1.h1[itemprop='name'], h1.h1, h1[itemprop='name']")
            data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

            # SKU
            sku_el = tree.css_first(
                "div.product-reference span[itemprop='sku'], "
                "span[itemprop='sku']"
            )
            data["sku"] = self._clean_text(sku_el.text(strip=True)) if sku_el else None

            # Price from content attribute
            price_el = tree.css_first(
                "div.current-price span[itemprop='price'], "
                "span[itemprop='price']"
            )
            if price_el:
                price_content = price_el.attributes.get("content")
                if price_content:
                    try:
                        data["price"] = float(price_content)
                    except ValueError:
                        data["price"] = self._parse_price(price_el.text())
                else:
                    data["price"] = self._parse_price(price_el.text())
            else:
                price_el = tree.css_first("span.price, div.current-price .price")
                data["price"] = self._parse_price(
                    price_el.text() if price_el else None
                )

            # Availability
            avail_el = tree.css_first(
                "span#product-availability, "
                "#product-availability"
            )
            if avail_el:
                avail_text = avail_el.text(strip=True)
                data["availability"] = avail_text
                lower = avail_text.lower()
                data["available"] = (
                    ("en stock" in lower or "disponible" in lower or "in stock" in lower)
                    and "rupture" not in lower
                    and "indisponible" not in lower
                )
            else:
                # Schema.org fallback
                avail_link = tree.css_first("link[itemprop='availability'][href]")
                if avail_link:
                    href = avail_link.attributes.get("href", "")
                    if "InStock" in href:
                        data["availability"] = "En stock"
                        data["available"] = True
                    elif "OutOfStock" in href:
                        data["availability"] = "Rupture de stock"
                        data["available"] = False
                    else:
                        data["availability"] = None
                        data["available"] = None
                else:
                    data["availability"] = None
                    data["available"] = None

        # Old price (always from HTML, JSON rarely has it)
        old_price_el = tree.css_first(
            "span.regular-price, "
            "div.product-discount span.regular-price"
        )
        if old_price_el:
            data["old_price"] = self._parse_price(old_price_el.text())
            if data.get("old_price") and data.get("price"):
                data["discount_percent"] = round(
                    (1 - data["price"] / data["old_price"]) * 100
                )
        else:
            data["old_price"] = None

        # Brand
        brand_img = tree.css_first(
            "div.product-manufacturer img, "
            "a.tvproduct-brand img"
        )
        if brand_img:
            data["brand"] = brand_img.attributes.get("alt")
            data["brand_logo"] = self._make_absolute_url(
                brand_img.attributes.get("src")
            )
        else:
            brand_el = tree.css_first("div.product-manufacturer a, a.tvproduct-brand")
            data["brand"] = (
                self._clean_text(brand_el.text(strip=True)) if brand_el else None
            )

        # Description
        desc_el = tree.css_first(
            "div.product-description, "
            "div[id^='product-description-short-'], "
            "div.tab-pane#description div.product-description"
        )
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        # Specifications
        specs = {}
        features = tree.css_first(".product-features, #product-details section")
        if features:
            for dt in features.css("dt"):
                dd = dt.next
                while dd and dd.tag != "dd":
                    dd = dd.next
                if dd:
                    k = self._clean_text(dt.text(strip=True))
                    v = self._clean_text(dd.text(strip=True))
                    if k and v:
                        specs[k] = v
        data["specifications"] = specs

        # Images
        images = []

        # Main product cover image
        main_img = tree.css_first(
            "div.product-cover img.js-qv-product-cover, "
            "div.product-cover img, "
            "img#main-image"
        )
        if main_img:
            src = (
                main_img.attributes.get("data-image-large-src")
                or main_img.attributes.get("data-src")
                or main_img.attributes.get("src")
            )
            if src:
                images.append(self._make_absolute_url(src))

        # Thumbnails
        for img in tree.css(
            "ul.product-images img.thumb, "
            "ul.product-images img, "
            ".js-thumb img, "
            ".thumb-container img"
        ):
            src = (
                img.attributes.get("data-image-large-src")
                or img.attributes.get("data-src")
                or img.attributes.get("src")
            )
            if src:
                abs_src = self._make_absolute_url(src)
                if abs_src not in images:
                    images.append(abs_src)

        data["images"] = images[:10] if images else None

        return data


def get_scraper(logger: logging.Logger) -> ScoopScraper:
    """Factory function to get scraper."""
    return ScoopScraper(logger)
