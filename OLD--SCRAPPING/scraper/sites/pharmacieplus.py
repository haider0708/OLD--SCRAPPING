#!/usr/bin/env python3
"""
Pharmacie Plus (parapharmacieplus.tn) specific scraper implementation.

Full CSR: Playwright for all phases: frontpage, listing pages, and product details.
Platform: Custom PHP + Bootstrap 4 + Htmlstream MegaMenu + Fotorama gallery.
"""
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from selectolax.parser import HTMLParser
from playwright.async_api import Page

from scraper.base import BaseScraper, save_text_atomic
from scraper.product_utils import (
    absolute_url,
    availability_from_text,
    dedupe_products,
    extract_gtins_from_text,
    finalize_product_record,
    first_attr,
    first_text,
    html_product_metadata,
    parse_price,
)


class PharmaciePlusScraper(BaseScraper):
    """Full-CSR Playwright scraper for parapharmacieplus.tn."""

    def __init__(self, logger: logging.Logger):
        super().__init__("pharmacieplus", logger)

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
        """Extract numeric price from text like '12,500 DT' or '1 234.500'."""
        return parse_price(text)

    # ------------------------------------------------------------------
    # Abstract implementations
    # ------------------------------------------------------------------

    def get_wait_selector(self) -> str:
        return self.selectors.get("category_page", {}).get(
            "wait_selector",
            "li.col-md-mc-5.col-fix.item-prod",
        )

    def build_page_url(self, base_url: str, page_num: int) -> str:
        """Pagination via ?page={n} query parameter."""
        base = re.sub(r"[?&]page=\d+", "", base_url)
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}page={page_num}"

    # ------------------------------------------------------------------
    # Frontpage: Playwright override (CSR mega-menu)
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        """Download frontpage using Playwright (Htmlstream MegaMenu requires JS)."""
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"ðŸ“¥ Downloading (Playwright): {self.base_url}")

        fp = self.selectors.get("frontpage", {})
        wait_sel = fp.get("wait_selector", "ul.navbar-nav.u-header__navbar-nav")

        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            from scraper.base import playwright_launch_args, get_playwright_proxy
            browser = await pw.chromium.launch(headless=True, args=playwright_launch_args())
            page = await browser.new_page(
                proxy=get_playwright_proxy(self.site_name, self.config.get("settings", {}))
            )
            try:
                await page.goto(self.base_url, wait_until="networkidle", timeout=30000)
                try:
                    await page.wait_for_selector(wait_sel, timeout=10000)
                except Exception:
                    self.logger.warning(f"Wait selector '{wait_sel}' not found, continuing")
                await asyncio.sleep(self.wait_after_load)
                html = await page.content()
            finally:
                await page.close()
                await browser.close()

        save_text_atomic(html, output_path, self.logger)
        self.logger.info(f"Saved: {output_path} ({len(html):,} bytes)")
        return output_path

    # ------------------------------------------------------------------
    # Categories: Htmlstream MegaMenu
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        """
        Extract category hierarchy from Htmlstream MegaMenu.

        Structure:
        - top: li.nav-item.hs-has-mega-menu > a.nav-link
        - low: div.hs-mega-menu div.col-3 > a  (with span.u-header__sub-menu-title)
        - sub: ul.u-header__sub-menu-nav-group > li > a.u-header__sub-menu-nav-link
        """
        tree = HTMLParser(html)
        fp = self.selectors.get("frontpage", {})
        categories = []

        top_sel = fp.get(
            "top_level_blocks",
            "ul.navbar-nav.u-header__navbar-nav > li.nav-item.hs-has-mega-menu",
        )
        top_blocks = tree.css(top_sel)
        self.logger.info(f"Found {len(top_blocks)} top-level categories")

        for top_block in top_blocks:
            top_link = top_block.css_first(
                fp.get("top_level_link", "a.nav-link")
            )
            if not top_link:
                continue

            top_name = self._clean_text(top_link.text(strip=True))
            top_url = self._make_absolute_url(top_link.attributes.get("href"))

            if not top_name:
                continue

            top_cat = {
                "name": top_name,
                "url": top_url,
                "level": "top",
                "low_level_categories": [],
            }

            # Low-level categories
            low_sel = fp.get(
                "low_level_items",
                "div.hs-mega-menu.u-header__sub-menu div.col-3 > a",
            )
            low_items = top_block.css(low_sel)

            for low_link in low_items:
                # Name from nested span or direct text
                name_span = low_link.css_first(
                    fp.get("low_level_name", "span.u-header__sub-menu-title")
                )
                low_name = self._clean_text(
                    name_span.text(strip=True) if name_span else low_link.text(strip=True)
                )
                low_url = self._make_absolute_url(low_link.attributes.get("href"))

                if not low_name:
                    continue

                low_cat = {
                    "name": low_name,
                    "url": low_url,
                    "level": "low",
                    "subcategories": [],
                }

                # Subcategories: sibling ul after this anchor
                parent = low_link.parent
                if parent:
                    sub_sel = fp.get(
                        "subcategory_items",
                        "ul.u-header__sub-menu-nav-group > li > a.u-header__sub-menu-nav-link",
                    )
                    sub_links = parent.css(sub_sel)
                    for sub_link in sub_links:
                        sub_name = self._clean_text(sub_link.text(strip=True))
                        sub_url = self._make_absolute_url(sub_link.attributes.get("href"))
                        if sub_name and sub_url:
                            low_cat["subcategories"].append({
                                "name": sub_name,
                                "url": sub_url,
                                "level": "subcategory",
                            })

                top_cat["low_level_categories"].append(low_cat)

            categories.append(top_cat)

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
    # Products: listing page
    # ------------------------------------------------------------------

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract listing products from static HTML using tested selectors."""
        tree = HTMLParser(html)
        cp = self.selectors.get("category_page", {})
        item_selector = cp.get("item_selector", "li.col-md-mc-5.col-fix.item-prod")
        items = tree.css(item_selector)
        if not items:
            items = tree.css("li.item-prod, .products-group, .product-item")

        products: List[Dict[str, Any]] = []
        for item in items:
            link = (
                item.css_first(cp.get("item_url", "a.text-gray-100.justify-content-around"))
                or item.css_first("a[href]")
            )
            href = link.attributes.get("href") if link else None
            product_url = absolute_url(href, self.base_url)

            name = (
                first_text(
                    item,
                    [
                        cp.get("item_name", "div.text-truncate.name-prod-card"),
                        "div.product_name",
                        "h3.product_title",
                        ".name-prod-card",
                    ],
                )
                or (link.attributes.get("title") if link else None)
            )

            product_id = None
            if product_url:
                path_match = re.search(r"/a/(\d+)(?:/|$)", product_url)
                if path_match:
                    product_id = path_match.group(1)
                else:
                    parsed = urlparse(product_url)
                    query = parse_qs(parsed.query)
                    product_id = (
                        (query.get("id") or query.get("product_id") or query.get("id_product") or [None])[0]
                    )
            product_id = product_id or item.attributes.get("data-id")

            image_url = first_attr(
                item,
                [
                    cp.get("item_image", "div.product-item__inner.position-relative img"),
                    "img.product_img",
                    "img",
                ],
                cp.get("item_image_attrs", ["src", "data-src", "data-original"]),
            )
            image_url = absolute_url(image_url, self.base_url) if image_url else None
            if not name and image_url:
                img = item.css_first("img")
                name = img.attributes.get("alt") if img else None

            price_node = item.css_first(
                cp.get("item_price", "div.info-ligne-card div.text-red")
            ) or item.css_first("div.price-head-home, div.prodcut-price, div.product_price, span.prix")
            price = None
            if price_node:
                price_html = price_node.html or ""
                current_price_html = re.split(r"<del\b", price_html, maxsplit=1, flags=re.I)[0]
                current_price_text = re.sub(r"<[^>]+>", " ", current_price_html)
                price = parse_price(current_price_text) or parse_price(price_node.text())
            old_price = parse_price(
                first_text(
                    item,
                    [
                        cp.get("item_old_price", "del.font-size-12"),
                        "span.ancien_prix",
                        "del",
                    ],
                )
            )

            availability, available = availability_from_text(
                first_text(
                    item,
                    [
                        cp.get("item_availability", ".info-prod-cart.badge-stock"),
                        "span.stock_status",
                        "div.disponibilite",
                        ".availability",
                    ],
                )
            )

            barcode = next(
                iter(
                    extract_gtins_from_text(
                        " ".join(value for value in [product_url, image_url, name] if value)
                    )
                ),
                None,
            )

            product: Dict[str, Any] = {
                "id": product_id,
                "product_id": product_id,
                "url": product_url,
                "name": name,
                "price": price,
            }
            if image_url:
                product["image"] = image_url
            if old_price is not None:
                product["old_price"] = old_price
                if price:
                    product["discount_percent"] = round((1 - price / old_price) * 100)
            if availability:
                product["availability"] = availability
            if available is not None:
                product["available"] = available
            if barcode:
                product["barcode"] = barcode

            if product_id or product_url:
                products.append(finalize_product_record(product))

        return dedupe_products(products, self.logger, "pharmacieplus listing")

    async def extract_products_from_page(self, page: Page) -> List[dict]:
        """Extract products from a loaded category page."""
        return self.extract_products_from_html(await page.content())

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def extract_pagination_info(self, page: Page) -> dict:
        """Extract pagination info from the loaded page."""
        cp = self.selectors.get("category_page", {})
        next_icon_sel = cp.get(
            "pagination_next_icon",
            "li.page-item a.page-link i.fa.fa-angle-right",
        )

        pagination = await page.evaluate(
            """(nextIconSel) => {
                let maxPage = 1;
                let currentPage = 1;

                // Current page from active item
                const activeEl = document.querySelector(
                    'li.page-item.active a.page-link, li.page-item.active span.page-link'
                );
                if (activeEl) {
                    const n = parseInt(activeEl.innerText);
                    if (!isNaN(n)) currentPage = n;
                }

                // Max page from all page links
                document.querySelectorAll('li.page-item a.page-link').forEach(el => {
                    const href = el.getAttribute('href') || '';
                    // Try ?page=N in URL
                    const m = href.match(/[?&]page=(\\d+)/);
                    if (m) {
                        const n = parseInt(m[1]);
                        if (n > maxPage) maxPage = n;
                    }
                    // Try text content
                    const n = parseInt(el.innerText);
                    if (!isNaN(n) && n > maxPage) maxPage = n;
                });

                // Detect if next button exists
                const nextIcon = document.querySelector(nextIconSel);
                const hasNext = !!nextIcon;

                return {
                    current_page: currentPage,
                    total_pages: Math.max(maxPage, currentPage),
                    has_next: hasNext,
                };
            }""",
            next_icon_sel,
        )

        return pagination

    # ------------------------------------------------------------------
    # Product details (Playwright)
    # ------------------------------------------------------------------

    def extract_product_details_from_html(self, html: str, product_url: str) -> dict:
        """Extract product detail data from rendered or saved HTML."""
        tree = HTMLParser(html)
        pp = self.selectors.get("product_page", {})
        data: Dict[str, Any] = {"url": product_url}
        data.update(html_product_metadata(html, product_url, self.base_url))

        title = first_text(
            tree,
            [
                pp.get("title", "h1.font-size-25.text-lh-1dot2"),
                "h1.font-size-25",
                "h1.product_title",
                "div.product_name h1",
            ],
        )
        if title:
            data["title"] = title

        price = parse_price(
            first_attr(tree, [pp.get("price_schema", "meta[itemprop='price'][content]")], ["content"])
        )
        if price is None:
            price = parse_price(
                first_text(
                    tree,
                    [
                        pp.get("price", "ins.font-size-36.text-decoration-none"),
                        "ins.font-size-36.text-decoration-none",
                        ".product-price",
                    ],
                )
            )
        if price is not None:
            data["price"] = price

        old_price = parse_price(
            first_text(
                tree,
                [
                    pp.get("old_price", "del.font-size-20.ml-2.text-gray-6"),
                    "span.ancien_prix",
                    "del",
                ],
            )
        )
        if old_price is not None:
            data["old_price"] = old_price
            if data.get("price"):
                data["discount_percent"] = round((1 - data["price"] / old_price) * 100)

        availability, available = availability_from_text(
            first_attr(
                tree,
                [pp.get("availability_schema", "link[itemprop='availability'][href]")],
                ["href", "content"],
            )
        )
        if not availability:
            availability, available = availability_from_text(
                first_text(tree, ["#stock_span", "span.stock_status", "div.disponibilite", "div.availability"])
            )
        if availability:
            data["availability"] = availability
        if available is not None:
            data["available"] = available

        brand = first_text(tree, ["span.marque", "div.brand", "div.product-manufacturer"])
        if brand:
            data["brand"] = brand

        description = first_text(
            tree,
            [
                pp.get("description", "div#tab-description"),
                "div.product_description",
                "div.description",
            ],
        )
        if description:
            data["description"] = description

        product_id = first_attr(
            tree,
            ["input#id_product[value]", "input[name='id_product'][value]", "input[name='product_id'][value]"],
            ["value"],
        )
        if not product_id:
            path_match = re.search(r"/a/(\d+)(?:/|$)", product_url)
            if path_match:
                product_id = path_match.group(1)
            else:
                query = parse_qs(urlparse(product_url).query)
                product_id = (query.get("id") or query.get("product_id") or query.get("id_product") or [None])[0]
        if product_id:
            data["product_id"] = product_id

        specs: Dict[str, str] = {}
        for row in tree.css("div.product_specs tr, table.product_attributes tr, table tr"):
            key_node = row.css_first("th, td:first-child")
            value_node = row.css_first("td:last-child")
            if key_node and value_node and key_node != value_node:
                key = self._clean_text(key_node.text(strip=True))
                value = self._clean_text(value_node.text(strip=True))
                if key and value:
                    specs[key] = value
        for dt in tree.css("div.product-features dt, div.product_specs dt, dl dt"):
            dd = dt.next
            while dd and getattr(dd, "tag", None) != "dd":
                dd = dd.next
            if dd:
                key = self._clean_text(dt.text(strip=True))
                value = self._clean_text(dd.text(strip=True))
                if key and value:
                    specs[key] = value
        if specs:
            data["specifications"] = specs
            for key, value in specs.items():
                if re.search(r"ean|gtin|code\s*bar|barcode", key, re.I):
                    data.setdefault("barcode", value)
                elif re.search(r"reference|ref\b|sku", key, re.I):
                    data.setdefault("reference", value)

        images: List[str] = []

        def add_image(value: Any) -> None:
            url = absolute_url(value, self.base_url)
            if url and url not in images:
                images.append(url)

        for selector in [
            f"{pp.get('image_gallery', 'div.fotorama')} div[data-img]",
            f"{pp.get('image_gallery', 'div.fotorama')} a[href]",
            f"{pp.get('image_gallery', 'div.fotorama')} img",
            pp.get("image_main", "div.fotorama__stage__frame.fotorama__active img.fotorama__img"),
            pp.get("image_all", "div.fotorama img.fotorama__img"),
            "meta[itemprop='image'][content]",
            "meta[property='og:image'][content]",
        ]:
            for node in tree.css(selector):
                add_image(
                    node.attributes.get("data-img")
                    or node.attributes.get("href")
                    or node.attributes.get("src")
                    or node.attributes.get("data-src")
                    or node.attributes.get("content")
                )
        if images:
            data["images"] = images
            data["image"] = images[0]

        return finalize_product_record(data)

    async def scrape_product_details(self, page: Page, product_url: str) -> dict:
        """Scrape a single product detail page with Fotorama gallery support."""
        pp = self.selectors.get("product_page", {})

        wait_sel = pp.get("wait_selector", "h1.font-size-25")

        try:
            await page.goto(product_url, wait_until="networkidle", timeout=30000)
            try:
                await page.wait_for_selector(wait_sel, timeout=10000)
            except Exception:
                pass
            await asyncio.sleep(self.wait_after_load)
        except Exception as e:
            return {"url": product_url, "error": str(e)}

        return self.extract_product_details_from_html(await page.content(), product_url)


# Factory function: required
def get_scraper(logger: logging.Logger) -> PharmaciePlusScraper:
    """Factory function to create scraper instance."""
    return PharmaciePlusScraper(logger)

