#!/usr/bin/env python3
"""
alkitab.tn scraper — epagine-based PHP bookstore.
Product listings are JS-rendered (AJAX into #liste_livres / #Affiche).
Uses Playwright for category pages, httpx for homepage/detail pages.
Categories: /{slug}/ssh-{id}  and  /listeliv.php?rayon=X&codegtl1=Y
Pagination: ?page=N
Price format: "36.00 DT"
SKU: EAN-13/ISBN-13 from data-gencod attribute or URL
"""
import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from selectolax.parser import HTMLParser

from scraper.base import FastScraper, playwright_launch_args

BASE = "https://www.alkitab.tn"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class AlkitabScraper(FastScraper):
    """Playwright (category pages) + httpx (detail pages) scraper for alkitab.tn."""

    def __init__(self, logger: logging.Logger):
        super().__init__("alkitab", logger)
        self._pw = None
        self._browser = None
        self._ctx = None
        self._fetch_sem = asyncio.Semaphore(3)

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True, args=playwright_launch_args()
        )
        self._ctx = await self._browser.new_context(user_agent=UA)

    async def close(self):
        await super().close()
        if self._ctx:
            await self._ctx.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _fetch_with_playwright(self, url: str) -> Optional[str]:
        """Fetch page with Playwright waiting for networkidle so AJAX calls complete."""
        await self._ensure_browser()
        async with self._fetch_sem:
            page = await self._ctx.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(2000)
                return await page.content()
            except Exception as e:
                self.logger.warning(f"Playwright error {url}: {e}")
                return None
            finally:
                await page.close()

    async def _fetch_category_livre_links(self, category_url: str) -> list:
        """
        alkitab loads its product listing via AJAX into #Affiche which Playwright
        cannot intercept reliably. Instead, use the epagine search API directly:
        /listeliv.php?base=paper&rayon={slug}&page=N returns HTML with product cards.
        Fallback: scan Playwright-rendered HTML for /livre/{isbn}-{slug}/ links.
        """
        import re as _re
        # Try httpx first on the category URL — alkitab may serve the list server-side
        # to non-JS clients if we send an XHR hint
        all_links: list[str] = []
        page = 1
        while True:
            page_url = category_url if page == 1 else self.build_page_url(category_url, page)
            html = await self.fetch_html(page_url)
            if not html:
                break
            links = _re.findall(r'href=["\'](/livre/97[89]\d{10}-[^"\']+)["\']', html)
            if not links:
                # Last resort: Playwright render
                html = await self._fetch_with_playwright(page_url)
                if html:
                    links = _re.findall(r'href=["\'](/livre/97[89]\d{10}-[^"\']+)["\']', html)
            if not links:
                break
            new_links = [l for l in links if l not in all_links]
            all_links.extend(new_links)
            if not self.has_next_page(html, page):
                break
            page += 1
            await asyncio.sleep(0.3)
        return list(dict.fromkeys(all_links))  # dedupe preserving order

    def build_page_url(self, base_url: str, page_num: int) -> str:
        parsed = urlparse(base_url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params["page"] = [str(page_num)]
        new_query = urlencode({k: v[0] for k, v in params.items()})
        return urlunparse(parsed._replace(query=new_query))

    def _abs(self, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return "https:" + url
        return urljoin(BASE, url)

    def _clean(self, text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _parse_price(self, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        # "36.00 DT" — dot = decimal
        cleaned = re.sub(r"[^\d.]", "", str(text)).strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    def extract_categories_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        categories = []
        seen = set()

        for a in tree.css("nav a, .menu a, header a, ul.nav a"):
            href = self._abs(a.attributes.get("href", ""))
            name = self._clean(a.text())
            if not href or not name or href in seen:
                continue
            if "#" in href:
                continue
            path = href.replace(BASE, "").strip("/")
            # Pattern 1: /slug/ssh-{id}
            if re.search(r"/ssh-\d+", path):
                seen.add(href)
                categories.append({"name": name, "url": href, "level": "top", "low_level_categories": []})
            # Pattern 2: /listeliv.php?rayon=...&codegtl1=...
            elif "listeliv.php" in path and "codegtl1" in path:
                seen.add(href)
                categories.append({"name": name, "url": href, "level": "top", "low_level_categories": []})

        return {"categories": categories}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        tree = HTMLParser(html)
        products = []
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        # alkitab: ul#liste_livres li  — each li has a zone_image + meta_produit structure
        # Fallback selectors cover edge cases (search results, alternate views)
        container_sel = "#liste_livres li, ul.resultsList li, .resultsList li"
        for item in tree.css(container_sel):
            product = {}

            link = item.css_first("a[href*='/livre/']")
            if not link:
                continue
            href = self._abs(link.attributes.get("href", ""))
            if not href:
                continue
            product["url"] = href

            # ISBN/EAN from URL: /livre/{isbn}-{slug}/
            m = re.search(r"/livre/(97[89]\d{10})-", href)
            if m:
                product["sku"] = m.group(1)

            # EAN from data-gencod on the add-to-cart button (more reliable)
            btn = item.css_first("button[data-gencod]")
            if btn:
                gencod = btn.attributes.get("data-gencod", "").strip()
                if gencod and re.match(r"97[89]\d{10}$", gencod):
                    product["sku"] = gencod
                    product["barcode"] = gencod

            # Title from h2.livre_titre or img alt
            title_el = item.css_first("h2.livre_titre, h3.livre_titre, .livre_titre")
            if title_el:
                name_link = title_el.css_first("a")
                product["name"] = self._clean((name_link or title_el).text())
            else:
                img = item.css_first("img")
                if img:
                    alt = self._clean(img.attributes.get("alt", ""))
                    if alt:
                        product["name"] = alt

            img = item.css_first("img[src]")
            if img:
                src = img.attributes.get("src", "")
                if src and not src.startswith("data:"):
                    # Prefer large image version
                    large = re.sub(r"_\d+_m\.jpg", "_1_l.jpg", src)
                    product["image"] = self._abs(large)

            # Price from span.item_prix or any span/p matching DT price pattern
            price_el = item.css_first("span.item_prix")
            if price_el:
                price_link = price_el.css_first("a")
                price_text = (price_link or price_el).text(strip=True)
                product["price"] = self._parse_price(price_text)
            else:
                for el in item.css("span, strong, p, a"):
                    txt = el.text(strip=True)
                    if re.search(r"\d+[\.,]\d+\s*(DT|TND)", txt, re.IGNORECASE):
                        product["price"] = self._parse_price(txt)
                        break

            product["shop"] = "alkitab"
            product["top_category"] = top_cat
            product["low_category"] = low_cat
            product["subcategory"] = subcat
            products.append(product)

        return products

    def has_next_page(self, html: str, current_page: int) -> bool:
        tree = HTMLParser(html)
        return tree.css_first(
            f"a[href*='page={current_page + 1}'], a[rel='next'], .pagination a:last-child"
        ) is not None

    def extract_pagination_from_html(self, html: str) -> dict:
        tree = HTMLParser(html)
        max_page = 1
        for a in tree.css(".pagination a, ul.pagination a"):
            try:
                num = int(a.text(strip=True))
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
        has_next = tree.css_first("a[rel='next']") is not None
        return {"current_page": 1, "total_pages": max_page, "has_next": has_next}

    async def scrape_product_details(self, url: str) -> dict:
        html = await self.fetch_html(url)
        if not html:
            return {"url": url, "error": "Failed to fetch"}
        return self.extract_product_details(html, {"url": url})

    def extract_product_details(self, html: str, product: dict) -> dict:
        tree = HTMLParser(html)
        details = dict(product)

        name_el = tree.css_first("h1")
        if name_el:
            details["name"] = self._clean(name_el.text())

        # Price: div.info-prix is the reliable selector on alkitab product pages
        price_el = tree.css_first("div.info-prix")
        if price_el:
            details["price"] = self._parse_price(price_el.text())
        else:
            # Fallback: scan any element whose short text matches a price pattern
            for el in tree.css("span, div, p, strong, td"):
                txt = el.text(strip=True)
                if len(txt) < 25 and re.search(r"\d+[\.,]\d+\s*(DT|TND)", txt, re.IGNORECASE):
                    details["price"] = self._parse_price(txt)
                    break

        # EAN/ISBN: from URL first, then gencod div, then page scan
        if not details.get("sku"):
            details["sku"] = self._isbn_from_url(details.get("url", ""))
        if not details.get("sku"):
            gencod = tree.css_first("div.gencod, [data-gencod], button[data-gencod]")
            if gencod:
                val = gencod.text(strip=True) or gencod.attributes.get("data-gencod", "")
                if re.match(r"97[89]\d{10}$", val.strip()):
                    details["sku"] = val.strip()
        if not details.get("sku"):
            for el in tree.css("td, span, li, p, div"):
                txt = el.text(strip=True)
                m = re.search(r"(97[89]\d{10})", txt)
                if m:
                    details["sku"] = m.group(1)
                    break

        desc_el = tree.css_first("div.resume, .resume, #resume, .description, .product-description")
        if desc_el:
            details["description"] = self._clean(desc_el.text())

        images = []
        # Product images are on images.epagine.fr/is/{company_id}/{isbn}_1_75.jpg
        # Upgrade to large version by replacing _75 → _l
        for img in tree.css("img[src*='images.epagine.fr/is/']"):
            src = img.attributes.get("src", "")
            if src:
                large = re.sub(r"_1_\d+\.jpg$", "_1_l.jpg", src)
                if large not in images:
                    images.append(large)
        if images:
            details["images"] = images
            if not details.get("image"):
                details["image"] = images[0]

        return details

    def _isbn_from_url(self, url: str) -> Optional[str]:
        if not url:
            return None
        m = re.search(r"/livre/(97[89]\d{10})-", url)
        return m.group(1) if m else None

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        """
        Override FastScraper.scrape_all_pages.
        alkitab loads its product listing via AJAX so #liste_livres never populates
        in the static HTML. We extract /livre/{isbn}-{slug}/ links from the page
        (they appear in rendered HTML) and build minimal product records from them.
        Full details are fetched in the details pass.
        """
        livre_links = await self._fetch_category_livre_links(category_url)
        if not livre_links:
            return []
        products = []
        for path in livre_links:
            href = self._abs(path)
            m = re.search(r"/livre/(97[89]\d{10})-([^/?#]+)", path)
            isbn = m.group(1) if m else None
            slug = m.group(2).replace("-", " ").title() if m else path.split("/")[-1]
            products.append({
                "url": href,
                "name": slug,
                "sku": isbn,
                "shop": "alkitab",
            })
        self.logger.info(f"  alkitab: {len(products)} products from {category_url}")
        return products[:limit] if limit else products

    async def scrape_category(self, category: dict) -> List[dict]:
        return await self.scrape_all_pages(category.get("url", ""))

    async def scrape(self) -> dict:
        self.logger.info("Starting alkitab scrape")
        html = await self.fetch_html(self.base_url)
        if not html:
            self.logger.error("Failed to fetch homepage")
            return {"products": [], "categories": []}
        cat_data = self.extract_categories_from_html(html)
        categories = cat_data.get("categories", [])
        self.logger.info(f"Found {len(categories)} categories")
        all_products = []
        seen_ids = set()
        for cat in categories:
            cat_info = {"url": cat["url"], "top_category": cat["name"], "low_category": "", "subcategory": ""}
            prods = await self.scrape_category(cat_info)
            for p in prods:
                uid = p.get("sku") or p.get("url")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    all_products.append(p)
        self.logger.info(f"Total unique products: {len(all_products)}")
        return {"products": all_products, "categories": categories}


def get_scraper(logger: logging.Logger) -> AlkitabScraper:
    return AlkitabScraper(logger)
