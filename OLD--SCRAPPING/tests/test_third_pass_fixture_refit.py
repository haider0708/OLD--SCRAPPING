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
REQUIRED_PRODUCT_KEYS = {"id", "url", "name"}


def _fixture(shop: str) -> str:
    return (ROOT / "data" / shop / "html" / "frontpage.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("shop", "scraper_cls", "expect_products"),
    [
        ("expert_gaming", ExpertGamingScraper, True),
        ("scoop", ScoopScraper, True),
        ("wiki", WikiScraper, True),
        ("spacenet", SpaceNetScraper, True),
        ("batam", BatamScraper, True),
        ("geant", GeantScraper, False),
        ("mapara", MaparaScraper, True),
    ],
)
def test_frontpage_fixture_product_parsing(shop, scraper_cls, expect_products):
    scraper = scraper_cls(logging.getLogger(f"fixture_{shop}"))
    html = _fixture(shop)

    products = scraper.extract_products_from_html(html)

    if expect_products:
        assert products, f"{shop} fixture should yield products"
        assert REQUIRED_PRODUCT_KEYS.issubset(products[0].keys())
        assert products[0]["url"]
        assert products[0]["name"]
    else:
        assert products == []


@pytest.mark.parametrize(
    ("shop", "scraper_cls"),
    [
        ("expert_gaming", ExpertGamingScraper),
        ("scoop", ScoopScraper),
        ("wiki", WikiScraper),
        ("spacenet", SpaceNetScraper),
        ("batam", BatamScraper),
        ("geant", GeantScraper),
        ("mapara", MaparaScraper),
    ],
)
def test_frontpage_fixture_category_discovery(shop, scraper_cls):
    scraper = scraper_cls(logging.getLogger(f"fixture_cat_{shop}"))
    html = _fixture(shop)
    data = scraper.extract_categories_from_html(html)

    assert "categories" in data
    assert "stats" in data
    assert data["stats"].get("total_urls", 0) >= 1


@pytest.mark.parametrize(
    ("scraper_cls", "html"),
    [
        (ExpertGamingScraper, "<section class='product'><a href='/p/1'>X</a></section>"),
        (ScoopScraper, "<div class='tvproduct-wrapper'><a class='thumbnail product-thumbnail' href='/p/1'></a></div>"),
        (WikiScraper, "<div class='product-card'><figure class='product-card__image'><a href='/p/1'><img alt='X'></a></figure></div>"),
        (SpaceNetScraper, "<div class='product-miniature js-product-miniature'><a class='thumbnail product-thumbnail' href='/p/1'></a></div>"),
        (BatamScraper, "<form class='product_addtocart_form'><a class='product-item-link' href='/p/1'>X</a></form>"),
        (MaparaScraper, "<div class='product-small box'><p class='name product-title'><a class='woocommerce-LoopProduct-link' href='/p/1'>X</a></p></div>"),
    ],
)
def test_missing_optional_fields_do_not_raise(scraper_cls, html):
    scraper = scraper_cls(logging.getLogger("fixture_optional"))
    products = scraper.extract_products_from_html(html)
    assert isinstance(products, list)
