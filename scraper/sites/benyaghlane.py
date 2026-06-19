#!/usr/bin/env python3
"""
benyaghlane.tn scraper — TikTak PRO v2 (Nuxt 3 SaaS), Cloudflare CDN.
Strategy: use Playwright network interception to capture the TikTak API
response the browser makes when loading a category page, then extract
product IDs and fetch details via the public products-read/{id}/ endpoint.
Price format: raw float e.g. 9.9 (TND)
SKU: API field "reference"
"""
import asyncio
import json
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

import httpx

from scraper.base import FastScraper, playwright_launch_args

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
STEALTH_JS = 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
BASE = "https://www.benyaghlane.tn"
API_BASE = "https://api.tiktak.space/api/v1"
COMPANY = "5LpPD0L"

# Top-level categories from site nav (used as fallback and primary source)
SEED_CATEGORIES = [
    {"id": 57230, "name": "Les Salaisons", "slug": "les-salaisons"},
    {"id": 56430, "name": "Épices", "slug": "epices"},
    {"id": 56440, "name": "Fruits Secs", "slug": "fruits-secs"},
    {"id": 57625, "name": "Snacks", "slug": "snacks"},
    {"id": 57602, "name": "Conserve", "slug": "conserve"},
    {"id": 57611, "name": "Hrissa", "slug": "hrissa"},
    {"id": 57847, "name": "Les Vinaigres", "slug": "les-vinaigres"},
    {"id": 58164, "name": "Huilerie", "slug": "huilerie"},
    {"id": 53415, "name": "Surgelés BY", "slug": "surgeles-by"},
    {"id": 56792, "name": "Fromagerie", "slug": "fromagerie"},
    {"id": 56348, "name": "Charcuterie", "slug": "charcuterie"},
    {"id": 57306, "name": "Pâtisserie BY", "slug": "patisserie-by"},
    {"id": 58102, "name": "Cafés Borbone", "slug": "cafes-borbone"},
]


class BenyaghlaneScraper(FastScraper):
    """Playwright + API scraper for benyaghlane.tn (TikTak PRO v2)."""

    def __init__(self, logger: logging.Logger):
        super().__init__("benyaghlane", logger)
        self._pw = None
        self._browser = None
        self._ctx = None
        self._fetch_sem = asyncio.Semaphore(2)
        self._api_client: Optional[httpx.AsyncClient] = None

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True, args=playwright_launch_args()
        )
        self._ctx = await self._browser.new_context(user_agent=UA)
        await self._ctx.add_init_script(STEALTH_JS)

    async def _ensure_api_client(self):
        if self._api_client is None or self._api_client.is_closed:
            self._api_client = httpx.AsyncClient(
                headers={"User-Agent": UA},
                follow_redirects=True,
                timeout=httpx.Timeout(30, connect=10),
            )

    async def close(self):
        await super().close()
        if self._api_client and not self._api_client.is_closed:
            await self._api_client.aclose()
        if self._ctx:
            await self._ctx.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def fetch_html(self, url: str, raise_on_error: bool = False) -> Optional[str]:
        await self._ensure_browser()
        async with self._fetch_sem:
            page = await self._ctx.new_page()
            html = None
            try:
                resp = await page.goto(url, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(2000)
                html = await page.content()
                if resp and resp.status >= 400:
                    self.logger.warning(f"HTTP {resp.status}: {url}")
            except Exception as e:
                self.logger.warning(f"Playwright error {url}: {e}")
            finally:
                await page.close()
            return html

    async def _fetch_category_products_via_interception(self, category_id: int, category_name: str) -> List[dict]:
        """
        Load category page in browser, intercept ALL TikTak API responses,
        pick the one that contains a product list.
        """
        await self._ensure_browser()
        url = f"{BASE}/product-list/{category_id}"
        intercepted_products = []
        done = asyncio.Event()

        async with self._fetch_sem:
            page = await self._ctx.new_page()
            try:
                async def handle_response(response):
                    try:
                        resp_url = response.url
                        # Only care about the TikTak products-read listing endpoint
                        if "api.tiktak.space" not in resp_url:
                            return
                        if "products-read" not in resp_url:
                            return
                        if response.status != 200:
                            return
                        # Read raw bytes first to avoid "body already consumed" errors
                        try:
                            raw = await response.body()
                            import json as _json
                            body = _json.loads(raw)
                        except Exception:
                            return

                        # The listing endpoint returns {"results": [...], "count": N, ...}
                        items = None
                        if isinstance(body, dict):
                            results = body.get("results")
                            if isinstance(results, list) and results:
                                items = results
                        elif isinstance(body, list) and body:
                            items = body

                        if items:
                            intercepted_products.extend(items)
                            self.logger.info(
                                f"  [benyaghlane] intercepted {len(items)} products "
                                f"(total so far: {len(intercepted_products)}) from {resp_url}"
                            )
                            done.set()
                    except Exception:
                        pass

                page.on("response", handle_response)
                # networkidle ensures Nuxt fully boots and all API calls complete
                await page.goto(url, wait_until="networkidle", timeout=90000)
                # Wait up to 20s for the product list API call
                try:
                    await asyncio.wait_for(done.wait(), timeout=20)
                except asyncio.TimeoutError:
                    self.logger.info(
                        f"  [benyaghlane] no product API response for cat {category_id} after 20s"
                    )
                await page.wait_for_timeout(1000)
            except Exception as e:
                self.logger.warning(f"Interception error for cat {category_id}: {e}")
            finally:
                await page.close()

        return intercepted_products

    async def _api_get_product(self, product_id: int) -> Optional[dict]:
        """Fetch a single product from the public products-read API."""
        await self._ensure_api_client()
        url = f"{API_BASE}/products-read/{product_id}/?company={COMPANY}"
        try:
            resp = await self._api_client.get(url)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            self.logger.warning(f"API error for product {product_id}: {e}")
        return None

    async def _api_list_category(self, category_id: int, page: int = 1) -> Optional[dict]:
        """
        Fetch one page of product listing for a category via the TikTak listing endpoint.
        Returns the full paginated response dict or None on error.
        URL pattern discovered via Playwright network interception:
          /api/v1/products-read/?company=...&page=N&size=20&no_parent=true&active=true
          &show-children=false&has_category={category_id}
        """
        await self._ensure_api_client()
        url = (
            f"{API_BASE}/products-read/"
            f"?company={COMPANY}&page={page}&ordering=&size=50"
            f"&no_parent=true&active=true&show-children=false"
            f"&has_attributs=&has_category={category_id}"
        )
        try:
            resp = await self._api_client.get(url)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            self.logger.warning(f"API list error cat {category_id} p{page}: {e}")
        return None

    def _api_product_to_record(self, data: dict, category_info: dict) -> dict:
        """Convert TikTak API product JSON to scraper product dict."""
        top_cat = (category_info or {}).get("top_category", "")
        low_cat = (category_info or {}).get("low_category", "")
        subcat = (category_info or {}).get("subcategory", "")

        product_id = data.get("id")
        seo_slug = data.get("seo_slug", "")
        price = data.get("price") or 0.0
        discount = data.get("discount") or 0.0
        discount_type = data.get("discount_type", "fixed_amount")

        if discount and discount_type == "fixed_amount":
            old_price = price
            price = round(price - discount, 3)
        elif discount and discount_type == "percentage":
            old_price = price
            price = round(price * (1 - discount / 100), 3)
        else:
            old_price = None

        record = {
            "id": str(product_id),
            "url": f"{BASE}/product/{product_id}/{seo_slug}",
            "name": (data.get("name") or "").strip(),
            "price": price,
            "shop": "benyaghlane",
            "top_category": top_cat,
            "low_category": low_cat,
            "subcategory": subcat,
            "sku": (data.get("reference") or "").strip() or None,
            "barcode": (data.get("bar_code") or "").strip() or None,
            "description": (data.get("description") or "").strip() or None,
        }
        if old_price:
            record["old_price"] = old_price
            record["discount_percent"] = round((1 - price / old_price) * 100)

        images = [img["image"] for img in (data.get("images") or []) if img.get("image")]
        photo = data.get("photo")
        if photo and photo not in images:
            images.insert(0, photo)
        if images:
            record["image"] = images[0]
            record["images"] = images

        return record

    def build_page_url(self, base_url: str, page_num: int) -> str:
        return base_url

    def extract_categories_from_html(self, html: str) -> dict:
        """Parse category links from nav HTML."""
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        categories = []
        seen = set()
        for a in tree.css("a[href*='/product-list/']"):
            href = a.attributes.get("href", "")
            m = re.search(r"/product-list/(\d+)/([^/?#]+)", href)
            if not m:
                continue
            cat_id = int(m.group(1))
            slug = m.group(2)
            if cat_id in seen:
                continue
            name = (a.text(strip=True) or slug.replace("-", " ").title()).strip()
            if not name:
                continue
            seen.add(cat_id)
            categories.append({
                "id": cat_id,
                "name": name,
                "url": f"{BASE}/product-list/{cat_id}/{slug}",
                "level": "top",
                "low_level_categories": [],
            })
        # Fallback to seed list
        if not categories:
            categories = [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "url": f"{BASE}/product-list/{c['id']}/{c['slug']}",
                    "level": "top",
                    "low_level_categories": [],
                }
                for c in SEED_CATEGORIES
            ]
        return {"categories": categories}

    def extract_products_from_html(self, html: str, category_info: dict = None) -> List[dict]:
        return []

    def has_next_page(self, html: str, current_page: int) -> bool:
        return False

    def extract_pagination_from_html(self, html: str) -> dict:
        return {"current_page": 1, "total_pages": 1, "has_next": False}

    async def scrape_all_pages(self, category_url: str, limit: int = None) -> List[dict]:
        """
        Override FastScraper.scrape_all_pages to use the TikTak API directly.
        The pipeline calls this with only a URL string — we extract the category ID
        from the URL pattern /product-list/{id}[/slug].
        """
        m = re.search(r"/product-list/(\d+)", category_url)
        if not m:
            self.logger.warning(f"[benyaghlane] cannot parse category ID from: {category_url}")
            return []
        cat_id = int(m.group(1))
        # Derive a display name from the URL slug if available
        slug_m = re.search(r"/product-list/\d+/([^/?#]+)", category_url)
        cat_name = slug_m.group(1).replace("-", " ").title() if slug_m else str(cat_id)
        cat_info = {"top_category": cat_name, "low_category": "", "subcategory": ""}

        await self._ensure_api_client()
        all_items: List[dict] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            resp = await self._api_list_category(cat_id, page)
            if not resp:
                break
            results = resp.get("results") or []
            all_items.extend(results)
            total_pages = resp.get("total_pages", 1)
            if not results or page >= total_pages:
                break
            page += 1
            await asyncio.sleep(0.15)

        products = [
            self._api_product_to_record(item, cat_info)
            for item in all_items
            if isinstance(item, dict) and item.get("id")
        ]
        return products[:limit] if limit else products

    async def scrape_product_details(self, url: str) -> dict:
        m = re.search(r"/product/(\d+)", url)
        if not m:
            return {"url": url, "error": "Cannot extract product ID"}
        data = await self._api_get_product(int(m.group(1)))
        if not data:
            return {"url": url, "error": "API returned no data"}
        return self._api_product_to_record(data, {})

    def extract_product_details(self, html: str, product: dict) -> dict:
        return product

    async def scrape_category(self, category: dict) -> List[dict]:
        cat_id = category.get("id")
        cat_name = category.get("name", category.get("top_category", ""))
        if not cat_id:
            return []

        cat_info = {
            "top_category": cat_name,
            "low_category": "",
            "subcategory": "",
        }

        # Strategy 1: direct paginated API (fast, no browser needed)
        # Endpoint discovered via Playwright network interception debug
        all_items: List[dict] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            resp = await self._api_list_category(cat_id, page)
            if not resp:
                break
            results = resp.get("results") or []
            all_items.extend(results)
            total_pages = resp.get("total_pages", 1)
            if page == 1:
                self.logger.info(
                    f"  [{cat_name}] API: {resp.get('count', '?')} products, "
                    f"{total_pages} pages"
                )
            if not results or page >= total_pages:
                break
            page += 1
            await asyncio.sleep(0.2)

        if all_items:
            products = [
                self._api_product_to_record(item, cat_info)
                for item in all_items
                if isinstance(item, dict) and item.get("id")
            ]
            self.logger.info(f"  [{cat_name}] built {len(products)} products via direct API")
            return products

        # Strategy 2: Playwright interception fallback (in case direct API returns 403)
        self.logger.info(f"  [{cat_name}] direct API empty, trying Playwright interception...")
        raw_items = await self._fetch_category_products_via_interception(cat_id, cat_name)
        if raw_items:
            products = [
                self._api_product_to_record(item, cat_info)
                for item in raw_items
                if isinstance(item, dict) and item.get("id")
            ]
            self.logger.info(f"  [{cat_name}] built {len(products)} products via interception")
            return products

        self.logger.info(f"  [{cat_name}] 0 products found")
        return []

    async def scrape(self) -> dict:
        self.logger.info("Starting benyaghlane scrape")
        await self._ensure_browser()
        await self._ensure_api_client()

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
            cat_info = {
                "id": cat["id"],
                "url": cat["url"],
                "name": cat.get("name", ""),
                "top_category": cat.get("name", ""),
                "low_category": "",
                "subcategory": "",
            }
            prods = await self.scrape_category(cat_info)
            for p in prods:
                uid = p.get("id") or p.get("url")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    all_products.append(p)
            await asyncio.sleep(1.0)

        self.logger.info(f"Total unique products: {len(all_products)}")
        return {"products": all_products, "categories": categories}


def get_scraper(logger: logging.Logger) -> BenyaghlaneScraper:
    return BenyaghlaneScraper(logger)
