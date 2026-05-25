#!/usr/bin/env python3
"""
Skymill Shop (skymil-shop.com) specific scraper implementation.
Full Playwright: site is a modern React/Next.js + Tailwind CSS store,
all pages require JS execution. Categories at /catalogue/{slug}.
base_url in config: skymil-informatique.com (redirects to skymil-shop.com).
"""

import json
import logging
import re
import time
from typing import List, Optional

from selectolax.parser import HTMLParser

from scraper.base import (
    FastScraper,
    TorPool,
    detect_blocked_signals,
    is_blocked_response,
    playwright_launch_args,
    proxy_url_to_playwright,
    save_text_atomic,
)

STEALTH_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
STEALTH_JS = 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'


class SkymillScraper(FastScraper):
    """Full-Playwright scraper for skymil-shop.com (modern React/Tailwind store)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("skymill", logger)
        self._pw = None
        self._browser = None
        self._pw_context = None
        self._tor_slot = hash("skymill") % max(TorPool.get().size, 1)

    # ------------------------------------------------------------------
    # Shared Playwright browser (lazy init, reused across all fetches)
    # ------------------------------------------------------------------

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=playwright_launch_args(),
        )
        pool = TorPool.get()
        self._pw_context = await self._browser.new_context(
            user_agent=STEALTH_UA,
            proxy=pool.pw_proxy(self._tor_slot)
            or proxy_url_to_playwright(self.proxy_url),
        )
        await self._pw_context.add_init_script(STEALTH_JS)

    async def _close_browser(self):
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    # ------------------------------------------------------------------
    # Override: Playwright-based frontpage download
    # ------------------------------------------------------------------

    async def download_frontpage(self):
        """Download frontpage using Playwright (React/Next.js, JS-rendered nav)."""
        output_path = self.html_dir / "frontpage.html"
        self.logger.info(f"📥 Downloading (Playwright): {self.base_url}")

        await self._ensure_browser()
        page = await self._pw_context.new_page()
        try:
            await page.goto(self.base_url, wait_until="networkidle", timeout=60000)
            # Wait for category nav links to be present
            try:
                await page.wait_for_selector(
                    "nav[aria-label='Catégories populaires'] a, a[href*='/catalogue/']",
                    timeout=15000,
                )
            except Exception:
                self.logger.warning("Category nav not found, continuing with page content")
            html = await page.content()
        finally:
            await page.close()

        save_text_atomic(html, output_path, self.logger)
        self.logger.info(f"✓ Saved: {output_path} ({len(html):,} bytes)")
        return output_path

    # ------------------------------------------------------------------
    # Override: Playwright-based fetch_html (replaces httpx for all pages)
    # ------------------------------------------------------------------

    async def fetch_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        meta = await self.fetch_html_with_meta(url, raise_on_error=raise_on_error)
        return meta.get("html")

    async def fetch_html_with_meta(self, url: str, raise_on_error: bool = False) -> dict:
        """Fetch HTML via Playwright. Skymill 403s subsequent requests on a shared
        context, so each fetch uses a fresh context and we retry once with a
        delay if blocked."""
        started = time.monotonic()
        status_code = None
        final_url = url
        html = None
        error = None
        attempts = 0

        for attempt in range(1, 4):  # up to 3 tries with fresh context each time
            attempts = attempt
            try:
                await self._ensure_browser()
                ctx = await self._browser.new_context(
                    user_agent=STEALTH_UA,
                )
                await ctx.add_init_script(STEALTH_JS)
                page = await ctx.new_page()
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    status_code = resp.status if resp else None
                    final_url = page.url
                    try:
                        await page.wait_for_selector(
                            "a[class*='bg-card'], a[href*='/produit/']",
                            timeout=15000,
                        )
                    except Exception:
                        pass
                    html = await page.content()
                finally:
                    await page.close()
                    await ctx.close()

                if status_code and status_code >= 400:
                    error = f"HTTP {status_code}"
                    if attempt < 3:
                        await asyncio.sleep(2 + attempt * 2)
                        continue
                    break
                if not html or not html.strip():
                    error = "empty_response"
                    if attempt < 3:
                        await asyncio.sleep(2)
                        continue
                    break
                if is_blocked_response(html, status_code):
                    error = "blocked_response"
                    if attempt < 3:
                        await asyncio.sleep(2 + attempt * 2)
                        continue
                    break
                error = None
                break
            except Exception as e:
                error = str(e) or e.__class__.__name__
                self.logger.debug(f"  Attempt {attempt} error fetching {url}: {e}")
                if attempt < 3:
                    await asyncio.sleep(2)
                    continue
                if raise_on_error:
                    raise

        blocked_signals = detect_blocked_signals(html, status_code)
        if raise_on_error and error:
            raise RuntimeError(error)
        return {
            "html": None if error else html,
            "status_code": status_code,
            "final_url": final_url,
            "content_type": None,
            "content_encoding": None,
            "attempts": attempts,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "blocked_signals": blocked_signals,
            "error": error,
        }

    # ------------------------------------------------------------------
    # Override: close browser when scraping finishes
    # ------------------------------------------------------------------

    async def run_full_scrape(
        self, category_limit=None, product_limit=None, detail_limit=None, on_result=None
    ):
        try:
            return await super().run_full_scrape(
                category_limit=category_limit,
                product_limit=product_limit,
                detail_limit=detail_limit,
                on_result=on_result,
            )
        finally:
            await self._close_browser()

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
        # skymil-shop.com is the actual domain after redirect
        base = "https://www.skymil-shop.com"
        if url.startswith("/"):
            return f"{base}{url}"
        return f"{base}/{url}"

    def _parse_price(self, text: str) -> Optional[float]:
        """Extract numeric price from text like '1 549 DT' or '1549 DT'."""
        if not text:
            return None
        cleaned = re.sub(r"[^\d\s.,]", "", text).strip()
        # Remove spaces used as thousand separators
        cleaned = re.sub(r"\s+", "", cleaned)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Category extraction — from rendered frontpage HTML
    # ------------------------------------------------------------------

    def extract_categories_from_html(self, html: str) -> dict:
        """Extract categories from skymil-shop.com navigation.

        Primary source: nav[aria-label='Catégories populaires'] quick links.
        Secondary: megamenu dropdown links at /catalogue/{slug}.
        """
        tree = HTMLParser(html)
        categories = []
        seen_urls = set()

        # Build a flat top-level list from the "Catégories populaires" nav
        # These are direct /catalogue/* links displayed as pills
        quick_nav = tree.css_first("nav[aria-label='Catégories populaires']")
        if quick_nav:
            for a in quick_nav.css("a[href]"):
                href = a.attributes.get("href", "")
                if not href or "/catalogue/" not in href:
                    continue
                abs_url = self._make_absolute_url(href)
                if abs_url in seen_urls:
                    continue
                seen_urls.add(abs_url)
                name = self._clean_text(a.text(strip=True))
                if not name:
                    continue
                categories.append({
                    "name": name,
                    "url": abs_url,
                    "level": "top",
                    "low_level_categories": [],
                })

        # Also scan all anchor tags for /catalogue/ links as fallback
        if not categories:
            for a in tree.css("a[href*='/catalogue/']"):
                href = a.attributes.get("href", "")
                abs_url = self._make_absolute_url(href)
                if abs_url in seen_urls:
                    continue
                seen_urls.add(abs_url)
                name = self._clean_text(a.text(strip=True))
                if not name or len(name) < 2:
                    continue
                categories.append({
                    "name": name,
                    "url": abs_url,
                    "level": "top",
                    "low_level_categories": [],
                })

        # Group sub-categories: URLs like /catalogue/composants/ssd are children of /catalogue/composants
        # Build hierarchy: find parent categories for child paths
        top_cats = {}
        sub_cats = []
        for cat in categories:
            path = cat["url"].replace("https://www.skymil-shop.com", "")
            parts = [p for p in path.split("/") if p]
            # e.g. ['catalogue', 'composants'] → top level
            # e.g. ['catalogue', 'composants', 'ssd-nvme'] → sub of composants
            if len(parts) == 2:
                top_cats[cat["url"]] = cat
            else:
                sub_cats.append(cat)

        # Attach sub-categories to their parents
        for sub in sub_cats:
            path = sub["url"].replace("https://www.skymil-shop.com", "")
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 3:
                parent_url = f"https://www.skymil-shop.com/{parts[0]}/{parts[1]}"
                if parent_url in top_cats:
                    top_cats[parent_url]["low_level_categories"].append({
                        "name": sub["name"],
                        "url": sub["url"],
                        "level": "low",
                        "subcategories": [],
                    })
                else:
                    # Parent not in top_cats yet — add it as a top cat
                    top_cats[parent_url] = {
                        "name": parts[1].replace("-", " ").title(),
                        "url": parent_url,
                        "level": "top",
                        "low_level_categories": [{
                            "name": sub["name"],
                            "url": sub["url"],
                            "level": "low",
                            "subcategories": [],
                        }],
                    }

        final_cats = list(top_cats.values())
        # Add any remaining flat cats (sub_cats without a parent found above)
        added_urls = {c["url"] for c in final_cats}
        for sub in sub_cats:
            if sub["url"] not in added_urls:
                final_cats.append({
                    "name": sub["name"],
                    "url": sub["url"],
                    "level": "top",
                    "low_level_categories": [],
                })

        stats = {"top_level": 0, "low_level": 0, "subcategory": 0, "total_urls": 0}
        for top in final_cats:
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

        self.logger.info(
            f"Extracted {stats['top_level']} top, {stats['low_level']} low, "
            f"{stats['subcategory']} sub categories ({stats['total_urls']} URLs)"
        )

        return {"categories": final_cats, "stats": stats}

    # ------------------------------------------------------------------
    # Product listing extraction
    # ------------------------------------------------------------------

    def build_page_url(self, base_url: str, page_num: int) -> str:
        """Pagination: ?page={n} query parameter."""
        base = re.sub(r"[?&]page=\d+", "", base_url)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page_num}"

    def extract_products_from_html(self, html: str) -> List[dict]:
        """Extract products from skymil-shop.com catalogue page (div.card-product)."""
        tree = HTMLParser(html)
        products = []
        seen_urls = set()

        # Primary: anchor cards with bg-card class (works on /catalogue/{top})
        items = tree.css("a[class*='bg-card']")

        # Fallback: nested categories like /catalogue/composants/disque-dur use a
        # different layout — group <a href="/produit/..."> links by href and take
        # an ancestor div as the card.
        if not items:
            seen_hrefs = set()
            for a in tree.css("a[href*='/produit/']"):
                href = a.attributes.get("href", "")
                if not href or href in seen_hrefs:
                    continue
                seen_hrefs.add(href)
                # Walk up to find a card-like container
                node = a
                for _ in range(6):
                    if node.parent is None:
                        break
                    node = node.parent
                    cls = node.attributes.get("class", "") if hasattr(node, "attributes") else ""
                    if any(x in cls for x in ("group", "flex-col", "rounded", "border", "bg-white")):
                        break
                items.append(node)

        for item in items:
            # Prefer /produit/ link; fall back to first any-link
            if item.tag == "a" and "/produit/" in item.attributes.get("href", ""):
                link_el = item
            else:
                link_el = item.css_first("a[href*='/produit/']") or item.css_first("a[href]")
            if not link_el:
                continue

            href = link_el.attributes.get("href", "")
            # Skip comparator / cart / non-product URLs
            if any(x in href for x in ("/comparateur", "/cart", "?add=", "/login")):
                continue
            product_url = self._make_absolute_url(href)
            if not product_url or product_url in seen_urls:
                continue
            seen_urls.add(product_url)

            # Extract slug as product_id (no numeric ID available)
            slug_match = re.search(r"/produit/(.+?)(?:-tunisie)?(?:/|$)", href)
            product_id = slug_match.group(1) if slug_match else href.rsplit("/", 1)[-1]

            # Name — try the /produit/ anchor with font-heading font-bold (name link),
            # then any anchor text, then img alt, then non-price p.
            product_name = ""
            for a in item.css("a[href*='/produit/']"):
                a_cls = a.attributes.get("class", "")
                if "font-bold" in a_cls or "line-clamp" in a_cls:
                    txt = self._clean_text(a.text(strip=True))
                    if txt and "DT" not in txt and not re.match(r"^\d", txt):
                        product_name = txt
                        break
            if not product_name:
                img_el = item.css_first("img[alt]")
                if img_el:
                    product_name = self._clean_text(img_el.attributes.get("alt", ""))
            if not product_name:
                # Fallback: any non-price text inside the card
                for p in item.css("p"):
                    txt = self._clean_text(p.text(strip=True))
                    if txt and "DT" not in txt and not re.match(r"^[\d\s.,]+$", txt):
                        product_name = txt
                        break

            product_data = {
                "id": product_id,
                "url": product_url,
                "name": product_name,
            }

            # Price — current price (font-extrabold text-primary)
            price_el = item.css_first("p.font-extrabold, p.text-primary, p[class*='font-extrabold']")
            if not price_el:
                # Grab first price-like element
                for p in item.css("p"):
                    text = p.text(strip=True)
                    if "DT" in text or re.search(r"\d{3,}", text):
                        price_el = p
                        break
            product_data["price"] = self._parse_price(price_el.text() if price_el else None)

            # Old price — line-through (crossed out original price)
            old_price_el = item.css_first("p.line-through, p[class*='line-through']")
            if old_price_el:
                product_data["old_price"] = self._parse_price(old_price_el.text())
                if product_data.get("old_price") and product_data.get("price"):
                    product_data["discount_percent"] = round(
                        (1 - product_data["price"] / product_data["old_price"]) * 100
                    )

            # Availability — stock badge
            stock_el = item.css_first("span[class*='gaming-success'], span[class*='text-gaming-success']")
            if stock_el:
                product_data["availability"] = self._clean_text(stock_el.text(strip=True))
                product_data["available"] = True
            else:
                oos_el = item.css_first("span[class*='gaming-warning'], span[class*='gaming-danger']")
                if oos_el:
                    product_data["availability"] = self._clean_text(oos_el.text(strip=True))
                    product_data["available"] = False

            # Image
            img_el = item.css_first("img[src], img[data-src]")
            if img_el:
                src = img_el.attributes.get("src") or img_el.attributes.get("data-src")
                if src and not src.startswith("data:"):
                    product_data["image"] = src  # Supabase CDN URL, already absolute

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
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                products.append({
                    "id": str(block.get("sku") or block.get("productID") or ""),
                    "url": url,
                    "name": self._clean_text(block.get("name", "")),
                    "price": self._parse_price(
                        str((block.get("offers") or {}).get("price", ""))
                    ),
                })
        return products

    def extract_pagination_from_html(self, html: str) -> dict:
        """Extract pagination from skymil-shop.com catalogue page."""
        tree = HTMLParser(html)

        current_page = 1
        total_pages = 1
        has_next = False

        # Look for page number buttons/links
        # Pattern: ?page=N in href attributes
        for a in tree.css("a[href*='page=']"):
            href = a.attributes.get("href", "")
            m = re.search(r"[?&]page=(\d+)", href)
            if m:
                try:
                    num = int(m.group(1))
                    if num > total_pages:
                        total_pages = num
                except ValueError:
                    pass

        # Look for "next" indicators
        next_el = tree.css_first("a[aria-label='Next'], a[aria-label='Suivant'], a[rel='next']")
        if not next_el:
            # Check for buttons with next/arrow text
            for btn in tree.css("button, a"):
                label = btn.attributes.get("aria-label", "")
                if label.lower() in ("next", "suivant", "page suivante"):
                    next_el = btn
                    break
        if next_el:
            has_next = True
            if total_pages <= current_page:
                total_pages = current_page + 1

        return {
            "current_page": current_page,
            "total_pages": total_pages,
            "has_next": has_next,
        }

    # ------------------------------------------------------------------
    # Product detail scraping
    # ------------------------------------------------------------------

    async def scrape_product_details(self, url: str) -> dict:
        """Scrape product detail from skymil-shop.com /produit/{slug} page."""
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}

        tree = HTMLParser(html)
        data = {"url": url}

        # Product ID from URL slug
        slug_match = re.search(r"/produit/(.+?)(?:-tunisie)?(?:/|\?|$)", url)
        data["product_id"] = slug_match.group(1) if slug_match else url.rsplit("/", 1)[-1]

        # Title — largest heading on page
        title_el = tree.css_first("h1")
        data["title"] = self._clean_text(title_el.text(strip=True)) if title_el else None

        # Price — look for price elements (font-extrabold / text-primary pattern)
        price_el = tree.css_first("p.font-extrabold, p[class*='font-extrabold'], span[class*='font-extrabold']")
        if not price_el:
            # Try any element containing DT price
            for el in tree.css("p, span, div"):
                text = (el.text(strip=True) or "")
                if re.match(r"^\d[\d\s]*(?:DT|TND)$", text):
                    price_el = el
                    break
        data["price"] = self._parse_price(price_el.text() if price_el else None)

        # Old price
        old_price_el = tree.css_first("p.line-through, p[class*='line-through'], span.line-through")
        if old_price_el:
            data["old_price"] = self._parse_price(old_price_el.text())
            if data.get("old_price") and data.get("price"):
                data["discount_percent"] = round(
                    (1 - data["price"] / data["old_price"]) * 100
                )
        else:
            data["old_price"] = None

        # Availability
        stock_el = tree.css_first(
            "span[class*='gaming-success'], span[class*='text-gaming-success'], "
            "[class*='En stock'], [class*='en-stock']"
        )
        if stock_el:
            data["availability"] = self._clean_text(stock_el.text(strip=True))
            data["available"] = True
        else:
            oos_el = tree.css_first("span[class*='gaming-warning'], [class*='Rupture']")
            if oos_el:
                data["availability"] = self._clean_text(oos_el.text(strip=True))
                data["available"] = False
            else:
                # Infer from add-to-cart button
                cart_btn = tree.css_first("button[aria-label*='panier'], button[aria-label*='cart']")
                data["available"] = cart_btn is not None
                data["availability"] = "En stock" if data["available"] else None

        # Brand — look for brand section
        brand_el = tree.css_first("img[alt][class*='brand'], div[class*='brand'] img")
        if brand_el:
            data["brand"] = self._clean_text(brand_el.attributes.get("alt", ""))
        else:
            brand_text_el = tree.css_first("div[class*='brand'], span[class*='brand']")
            if brand_text_el:
                data["brand"] = self._clean_text(brand_text_el.text(strip=True))
            else:
                data["brand"] = None

        # Description
        desc_el = tree.css_first("div[class*='description'], section[class*='description'], p[class*='description']")
        if not desc_el:
            # Try the second paragraph after the title
            paragraphs = tree.css("main p")
            for p in paragraphs:
                text = self._clean_text(p.text(strip=True))
                if len(text) > 50:
                    desc_el = p
                    break
        data["description"] = self._clean_text(desc_el.text(strip=True)) if desc_el else None

        # Specs — look for specification tables or lists
        specs = {}
        specs_table = tree.css_first("table")
        if specs_table:
            for row in specs_table.css("tr"):
                cells = row.css("td, th")
                if len(cells) >= 2:
                    k = self._clean_text(cells[0].text(strip=True))
                    v = self._clean_text(cells[1].text(strip=True))
                    if k and v:
                        specs[k] = v
        if not specs:
            # Try definition lists
            for dl in tree.css("dl"):
                keys = dl.css("dt")
                vals = dl.css("dd")
                for k, v in zip(keys, vals):
                    k_text = self._clean_text(k.text(strip=True))
                    v_text = self._clean_text(v.text(strip=True))
                    if k_text:
                        specs[k_text] = v_text
        data["specifications"] = specs if specs else None

        # Images — Supabase CDN images
        images = []
        for img in tree.css("img[src*='supabase.co'], img[src*='product-images']"):
            src = img.attributes.get("src", "")
            if src and src not in images:
                images.append(src)
        if not images:
            # Any product image
            main_img = tree.css_first("main img[src]")
            if main_img:
                src = main_img.attributes.get("src", "")
                if src and not src.startswith("data:"):
                    images.append(src)
        data["images"] = images if images else None

        # JSON-LD enrichment
        for script in tree.css('script[type="application/ld+json"]'):
            raw = (script.text() or "").strip()
            if not raw:
                continue
            try:
                ld = json.loads(raw)
            except Exception:
                continue
            if isinstance(ld, dict) and ld.get("@type") == "Product":
                if not data.get("title"):
                    data["title"] = ld.get("name")
                if not data.get("brand"):
                    brand_info = ld.get("brand", {})
                    if isinstance(brand_info, dict):
                        data["brand"] = brand_info.get("name")
                    elif isinstance(brand_info, str):
                        data["brand"] = brand_info
                offers = ld.get("offers", {})
                if isinstance(offers, dict):
                    if not data.get("price"):
                        try:
                            data["price"] = float(offers.get("price", 0)) or None
                        except (ValueError, TypeError):
                            pass
                    avail_schema = offers.get("availability", "")
                    if "InStock" in avail_schema:
                        data["availability"] = "En stock"
                        data["available"] = True
                    elif "OutOfStock" in avail_schema:
                        data["availability"] = "Rupture de stock"
                        data["available"] = False
                if not data.get("description"):
                    data["description"] = self._clean_text(ld.get("description", "")) or None
                break

        return data


def get_scraper(logger: logging.Logger) -> SkymillScraper:
    return SkymillScraper(logger)
