import logging
from pathlib import Path

import pytest

import scrape
from scraper.base import CategoryInfo


def _cat(url: str, name: str = "cat", level: str = "low"):
    return CategoryInfo(url=url, name=name, location=(0,), level=level, parent_names=[])


def test_invalid_and_navigation_urls_filtered():
    cats = [
        _cat(""),
        _cat("notaurl"),
        _cat("https://example.com/cart"),
        _cat("https://example.com/login"),
        _cat("https://example.com/category/pc"),
    ]
    out = scrape.filter_live_categories(cats)
    assert len(out["kept"]) == 1
    assert out["kept"][0].url == "https://example.com/category/pc"


def test_duplicate_categories_are_normalized_and_deduped():
    cats = [
        _cat("https://example.com/category/pc/"),
        _cat("https://example.com/category/pc"),
        _cat("https://example.com/category/pc?utm_source=x"),
    ]
    out = scrape.filter_live_categories(cats)
    assert len(out["kept"]) == 1
    assert out["kept"][0].url == "https://example.com/category/pc"


@pytest.mark.asyncio
async def test_first_empty_category_is_skipped_and_product_one_selected(tmp_path):
    class Dummy:
        html_dir = tmp_path

        async def fetch_html(self, url: str):
            if "empty" in url:
                return "<html><body>no products</body></html>"
            return "<html><div class='p' data-id='1'></div></html>"

        def extract_products_from_html(self, html: str):
            return [{"id": "1", "url": "https://x/p/1", "name": "X"}] if "data-id" in html else []

    cats = [_cat("https://shop.tn/empty"), _cat("https://shop.tn/has-products")]
    sel = await scrape.select_live_product_category(
        scraper=Dummy(),
        categories=cats,
        probe_limit=3,
        logger=logging.getLogger("t"),
    )
    assert sel["selected"] is not None
    assert sel["selected"].url == "https://shop.tn/has-products"
    assert (tmp_path / "live_selected_category.html").exists()


@pytest.mark.asyncio
async def test_probe_limit_stops_early(tmp_path):
    class Dummy:
        html_dir = tmp_path
        calls = 0

        async def fetch_html(self, url: str):
            self.calls += 1
            return "<html><body>no products</body></html>"

        def extract_products_from_html(self, html: str):
            return []

    d = Dummy()
    cats = [_cat(f"https://shop.tn/c{i}") for i in range(10)]
    sel = await scrape.select_live_product_category(
        scraper=d,
        categories=cats,
        probe_limit=4,
        logger=logging.getLogger("t"),
    )
    assert sel["selected"] is None
    assert d.calls == 4


@pytest.mark.asyncio
async def test_probe_continues_when_some_categories_error(tmp_path):
    class Dummy:
        html_dir = tmp_path

        async def fetch_html(self, url: str):
            if "err" in url:
                return None
            if "empty" in url:
                return "<html></html>"
            return "<html><div class='ok'></div></html>"

        def extract_products_from_html(self, html: str):
            return [{"id": "1", "url": "https://x/p/1", "name": "ok"}] if "ok" in html else []

    cats = [_cat("https://shop.tn/err"), _cat("https://shop.tn/empty"), _cat("https://shop.tn/ok")]
    sel = await scrape.select_live_product_category(
        scraper=Dummy(),
        categories=cats,
        probe_limit=5,
        logger=logging.getLogger("t"),
    )
    assert sel["selected"] is not None
    assert sel["selected"].url.endswith("/ok")


def test_selected_category_url_is_absolute_and_stable():
    cats = [_cat("https://EXAMPLE.com/cat/path/?utm_source=x")]
    out = scrape.filter_live_categories(cats)
    assert out["kept"][0].url == "https://example.com/cat/path"
