import asyncio
import logging
from pathlib import Path

import pytest

from scraper.sites.batam import BatamScraper
from scraper.sites.expert_gaming import ExpertGamingScraper
from scraper.sites.geant import GeantScraper
from scraper.sites.mapara import MaparaScraper
from scraper.sites.scoop import ScoopScraper
from scraper.sites.spacenet import SpaceNetScraper
from scraper.sites.wiki import WikiScraper


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_LISTING_KEYS = {"id", "url", "name"}
SHOPS = [
    ("expert_gaming", ExpertGamingScraper),
    ("scoop", ScoopScraper),
    ("wiki", WikiScraper),
    ("spacenet", SpaceNetScraper),
    ("batam", BatamScraper),
    ("geant", GeantScraper),
    ("mapara", MaparaScraper),
]


def read_sample(shop: str, kind: str) -> str | None:
    path = ROOT / "data" / shop / "html" / f"{kind}_sample_1.html"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="ignore")


@pytest.mark.parametrize("shop,scraper_cls", SHOPS)
def test_listing_sample_extraction(shop, scraper_cls):
    html = read_sample(shop, "listing")
    assert html, f"missing listing sample for {shop}"
    scraper = scraper_cls(logging.getLogger(f"listing_{shop}"))

    products = scraper.extract_products_from_html(html)
    assert len(products) >= 1
    assert REQUIRED_LISTING_KEYS.issubset(products[0].keys())


@pytest.mark.parametrize("shop,scraper_cls", SHOPS)
def test_pagination_extraction_from_listing(shop, scraper_cls):
    html = read_sample(shop, "listing")
    assert html, f"missing listing sample for {shop}"
    scraper = scraper_cls(logging.getLogger(f"pagination_{shop}"))
    page = scraper.extract_pagination_from_html(html)

    assert "current_page" in page
    assert "has_next" in page
    assert (
        "total_pages" in page or "max_page" in page
    ), f"pagination should expose page count for {shop}"


@pytest.mark.parametrize("shop,scraper_cls", SHOPS)
def test_detail_sample_extraction(shop, scraper_cls):
    html = read_sample(shop, "detail")
    if not html:
        pytest.skip(f"missing detail sample for {shop}")

    scraper = scraper_cls(logging.getLogger(f"detail_{shop}"))

    async def _run():
        async def fake_fetch_html(url: str, raise_on_error: bool = False):
            return html

        scraper.fetch_html = fake_fetch_html  # type: ignore[method-assign]
        return await scraper.scrape_product_details("https://example.com/product")

    data = asyncio.run(_run())
    assert isinstance(data, dict)
    assert "url" in data
    assert any(
        key in data for key in ("title", "price", "availability", "sku", "images")
    )


@pytest.mark.parametrize("shop,scraper_cls", SHOPS)
def test_missing_optional_fields_safe(shop, scraper_cls):
    scraper = scraper_cls(logging.getLogger(f"optional_{shop}"))
    minimal = "<html><body><a href='https://example.com/p/1'>x</a></body></html>"
    products = scraper.extract_products_from_html(minimal)
    assert isinstance(products, list)
