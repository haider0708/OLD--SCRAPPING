#!/usr/bin/env python3
"""
Geant Drive (geantdrive.tn) specific scraper implementation.
"""
import logging
import re
from typing import List, Dict, Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, save_text_atomic


class GeantScraper(FastScraper):
    """HTTPX/selectolax-based scraper for geantdrive.tn (PrestaShop + WB MegaMenu). No detail pages."""

    def __init__(self, logger: logging.Logger):
        super().__init__("geant", logger)

    async def download_frontpage(self):
        """Use Playwright for dynamic menu rendering."""
        output_path = self.html_dir / "frontpage.html"
        from playwright.async_api import async_playwright
        from scraper.base import get_playwright_proxy, playwright_launch_args

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=playwright_launch_args())
            page = await browser.new_page(
                proxy=get_playwright_proxy(self.site_name, self.config.get("settings", {}))
            )
            try:
                await page.goto(self.base_url, wait_until="networkidle", timeout=45000)
                try:
                    await page.wait_for_selector("a[href*='geant']", timeout=8000)
                except Exception:
                    pass
                html = await page.content()
            finally:
                await page.close()
                await browser.close()
        save_text_atomic(html, output_path, self.logger)
        return output_path

    def build_page_url(self, base_url: str, page_num: int) -> str:
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url}?page={page_num}"

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract 2-level category hierarchy from geantdrive.tn frontpage (top + low, no subcategories)."""
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories = []

        top_blocks = tree.css(fp.get("top_level_blocks", "ul.menu-content.top-menu > li.level-1"))
        self.logger.info(f"Found {len(top_blocks)} top-level category blocks")
        if not top_blocks:
            return self._extract_categories_fallback(tree)

        for top_block in top_blocks:
            top_link = top_block.css_first(fp.get("top_level_link", "a[href]"))
            if not top_link:
                continue

            # Name from nested span
            name_el = top_block.css_first(fp.get("top_level_name", "a > span"))
            top_name = name_el.text(strip=True) if name_el else top_link.text(strip=True)
            top_url = top_link.attributes.get("href", "")
            if top_url and not top_url.startswith("http"):
                top_url = urljoin(self.base_url, top_url)

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            # Low-level: WB sub-menu headers
            low_blocks = top_block.css(fp.get("low_level_blocks", "div.wb-sub-menu li.menu-item.item-header"))
            for low_block in low_blocks:
                low_link = low_block.css_first(fp.get("low_level_link", "a.category_header"))
                if not low_link:
                    continue

                low_name = low_link.text(strip=True)
                low_url = low_link.attributes.get("href", "")
                if not low_name:
                    continue
                if low_url and not low_url.startswith("http"):
                    low_url = urljoin(self.base_url, low_url)

                top_cat["low_level_categories"].append({
                    "name": low_name,
                    "url": low_url,
                    "level": "low",
                    "subcategories": [],
                })

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
        """Fallback category extraction when menu structure changes."""
        seen = set()
        low = []
        for link in tree.css("a[href]"):
            href = (link.attributes.get("href") or "").strip()
            name = (link.text(strip=True) or "").strip()
            if not href or not name:
                continue
            if "geant" not in href.lower():
                continue
            if href.endswith(".html"):
                continue
            if any(
                bad in href.lower()
                for bad in ("/cart", "/panier", "/checkout", "/account", "/login")
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

        items = tree.css(cp.get("item_selector", "article.product-miniature.js-product-miniature"))

        for item in items:
            product_id = item.attributes.get(cp.get("item_id_attr", "data-id-product"))

            name_el = item.css_first(cp.get("item_name", "h2.h3.product-title[itemprop='name'] a"))
            if not name_el:
                continue
            product_name = name_el.text(strip=True)

            url_el = item.css_first(cp.get("item_url", "h2.product-title a[href]"))
            product_url = url_el.attributes.get("href", "") if url_el else ""
            if not product_url:
                continue
            if not product_url.startswith("http"):
                product_url = urljoin(self.base_url, product_url)

            product_data: Dict[str, Any] = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }

            # Price
            price_el = item.css_first(cp.get("item_price", "span.price[itemprop='price']"))
            if price_el:
                price_content = price_el.attributes.get("content")
                if price_content:
                    try:
                        product_data["price"] = float(price_content)
                    except ValueError:
                        product_data["price"] = None
                else:
                    price_text = re.sub(r"[^\d.,]", "", price_el.text()).replace(",", ".")
                    try:
                        product_data["price"] = float(price_text) if price_text else None
                    except ValueError:
                        product_data["price"] = None
            else:
                product_data["price"] = None

            # Old price
            old_price_el = item.css_first(".regular-price")
            if old_price_el:
                old_text = re.sub(r"[^\d.,]", "", old_price_el.text()).replace(",", ".")
                try:
                    product_data["old_price"] = float(old_text) if old_text else None
                except ValueError:
                    product_data["old_price"] = None

            # Brand
            brand_el = item.css_first(cp.get("item_brand", "p.manufacturer_product"))
            if brand_el:
                product_data["brand"] = brand_el.text(strip=True)

            # Image
            img_el = item.css_first(cp.get("item_image", "a.thumbnail.product-thumbnail img.img-responsive"))
            if img_el:
                image_url = None
                for attr in cp.get("item_image_attrs", ["src", "data-src", "data-full-size-image-url"]):
                    image_url = img_el.attributes.get(attr)
                    if image_url and not image_url.startswith("data:"):
                        break
                if image_url:
                    if image_url.startswith("//"):
                        image_url = "https:" + image_url
                    elif image_url.startswith("/"):
                        image_url = urljoin(self.base_url, image_url)
                    if not image_url.startswith("data:"):
                        product_data["image"] = image_url

            # Availability from listing (structured data)
            avail_link = item.css_first('link[itemprop="availability"]')
            if avail_link:
                avail_href = avail_link.attributes.get("href", "")
                if "InStock" in avail_href:
                    product_data["availability"] = "En stock"
                    product_data["available"] = True
                elif "OutOfStock" in avail_href:
                    product_data["availability"] = "En rupture"
                    product_data["available"] = False

            products.append(product_data)

        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})

        current_page = 1
        total_pages = 1
        has_next = False

        page_links = tree.css(cp.get("pagination_pages", "ul.page-list a.js-search-link"))
        for el in page_links:
            classes = el.attributes.get("class", "")
            try:
                num = int(el.text(strip=True))
                if num > total_pages:
                    total_pages = num
                if "disabled" in classes or "current" in classes:
                    current_page = num
            except (ValueError, TypeError):
                continue

        next_link = tree.css_first(cp.get("pagination_next", "a.next.js-search-link[rel='next']"))
        if next_link and "disabled" not in next_link.attributes.get("class", ""):
            has_next = True

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape Geant product detail page when available."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        body = tree.css_first("body")
        if body:
            classes = body.attributes.get("class", "")
            m = re.search(r"product-id-(\d+)", classes)
            if m:
                data["product_id"] = m.group(1)

        title_el = tree.css_first("h1.h1[itemprop='name'], h1.h1.product-head1, h1.h1")
        data["title"] = title_el.text(strip=True) if title_el else None

        ref_el = tree.css_first(".product-reference span")
        data["sku"] = ref_el.text(strip=True) if ref_el else None

        price_el = tree.css_first(
            "span[itemprop='price'][content], .current-price span[itemprop='price']"
        )
        if price_el:
            raw = price_el.attributes.get("content") or price_el.text()
            cleaned = re.sub(r"[^\d.,]", "", raw).replace(",", ".")
            try:
                data["price"] = float(cleaned) if cleaned else None
            except ValueError:
                data["price"] = None
        else:
            data["price"] = None

        old_price_el = tree.css_first(".regular-price")
        if old_price_el:
            cleaned = re.sub(r"[^\d.,]", "", old_price_el.text()).replace(",", ".")
            try:
                data["old_price"] = float(cleaned) if cleaned else None
            except ValueError:
                data["old_price"] = None

        avail_el = tree.css_first("#product-availability, #product-availability span")
        if avail_el:
            txt = avail_el.text(strip=True)
            low = txt.lower()
            data["availability"] = txt
            data["available"] = "rupture" not in low and "indisponible" not in low
        else:
            schema_av = tree.css_first('link[itemprop="availability"][href]')
            if schema_av:
                href = schema_av.attributes.get("href", "")
                if "InStock" in href:
                    data["availability"] = "En stock"
                    data["available"] = True
                elif "OutOfStock" in href:
                    data["availability"] = "Rupture de stock"
                    data["available"] = False
                else:
                    data["availability"] = None
                    data["available"] = None

        desc_el = tree.css_first("#description .product-description, .product-description")
        data["description"] = desc_el.text(strip=True) if desc_el else None

        images = []
        for img in tree.css(".product-images img, .js-modal-product-images img, .thumb-container img"):
            src = (
                img.attributes.get("data-image-large-src")
                or img.attributes.get("data-src")
                or img.attributes.get("src")
            )
            if src and src not in images and not src.startswith("data:"):
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = urljoin(self.base_url, src)
                images.append(src)
        data["images"] = images[:10] if images else None

        specs = {}
        features = tree.css_first(".product-features, #product-details section")
        if features:
            for dt in features.css("dt"):
                dd = dt.next
                while dd and dd.tag != "dd":
                    dd = dd.next
                if dd:
                    k = dt.text(strip=True)
                    v = dd.text(strip=True)
                    if k and v:
                        specs[k] = v
        data["specifications"] = specs

        return data


def get_scraper(logger: logging.Logger) -> GeantScraper:
    return GeantScraper(logger)
