import logging
from pathlib import Path

import httpx
import pytest

import pipeline
from export_db import flatten_history_data, history_sources_for_shop
from scrape import close_scraper_resources, should_scrape_details
from scraper import base as base_module
from scraper.base import FastScraper, get_configured_proxy_url, load_jsonl, parse_retry_after
from scripts.validate_selectors import collect_selector_entries, validate_section


class DummyFastScraper(FastScraper):
    def __init__(self, responses):
        self._responses = list(responses)
        self._calls = 0
        logger = logging.getLogger("dummy-fast-scraper")
        super().__init__("allani", logger)
        self.retry_config.max_retries = max(1, len(self._responses))
        self.retry_config.base_delay = 0
        self.retry_config.jitter = False

    async def get_client(self, slot: int = 0) -> httpx.AsyncClient:
        async def handler(request):
            idx = min(self._calls, len(self._responses) - 1)
            self._calls += 1
            status_code, text, headers = self._responses[idx]
            return httpx.Response(status_code, text=text, headers=headers, request=request)

        if not self._clients:
            self._clients[-1] = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                follow_redirects=True,
            )
        return self._clients[-1]

    def extract_categories_from_html(self, html: str) -> dict:
        return {}

    def extract_products_from_html(self, html: str):
        return []

    def extract_pagination_from_html(self, html: str) -> dict:
        return {}

    def build_page_url(self, base_url: str, page_num: int) -> str:
        return base_url

    async def scrape_product_details(self, url: str) -> dict:
        return {}


def test_load_jsonl_malformed_returns_empty(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"ok": true}\n{"broken": ', encoding="utf-8")

    assert load_jsonl(path) == []


def test_proxy_url_prefers_site_env(monkeypatch):
    monkeypatch.setenv("SCRAPER_PROXY_ALLANI", "http://127.0.0.1:8888")
    monkeypatch.setenv("SCRAPER_PROXY", "http://127.0.0.1:9999")

    assert get_configured_proxy_url("allani", {}) == "http://127.0.0.1:8888"


def test_parse_retry_after_seconds():
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("0") == 0.0


@pytest.mark.asyncio
async def test_fetch_html_retries_429_retry_after(monkeypatch):
    monkeypatch.setattr(base_module, "random_delay", lambda *_args, **_kwargs: 0)
    scraper = DummyFastScraper(
        [
            (429, "rate limited", {"retry-after": "0"}),
            (200, "<html><body>ok</body></html>", {"content-type": "text/html"}),
        ]
    )

    try:
        result = await scraper.fetch_html_with_meta("https://example.com/test")
    finally:
        await scraper.close()

    assert result["error"] is None
    assert result["status_code"] == 200
    assert result["attempts"] == 2
    assert result["html"] == "<html><body>ok</body></html>"


@pytest.mark.asyncio
async def test_fetch_html_retries_empty_html(monkeypatch):
    monkeypatch.setattr(base_module, "random_delay", lambda *_args, **_kwargs: 0)
    scraper = DummyFastScraper(
        [
            (200, "   ", {"content-type": "text/html"}),
            (200, "<html><body>ok</body></html>", {"content-type": "text/html"}),
        ]
    )

    try:
        result = await scraper.fetch_html_with_meta("https://example.com/empty")
    finally:
        await scraper.close()

    assert result["error"] is None
    assert result["attempts"] == 2


@pytest.mark.asyncio
async def test_close_scraper_resources_calls_http_and_browser_closers():
    class Dummy:
        def __init__(self):
            self.closed = False
            self.browser_closed = False

        async def close(self):
            self.closed = True

        async def _close_browser(self):
            self.browser_closed = True

    scraper = Dummy()
    await close_scraper_resources(scraper)

    assert scraper.closed is True
    assert scraper.browser_closed is True


def test_should_scrape_details_respects_site_skip():
    class Dummy:
        config = {"skip_details": True}

    assert should_scrape_details(Dummy(), requested=True) is False
    assert should_scrape_details(Dummy(), requested=False) is False


@pytest.mark.asyncio
async def test_pipeline_activates_and_deactivates_tor(monkeypatch):
    calls = []

    class FakeTorPool:
        active = False

        def activate(self, on):
            calls.append(on)
            self.active = on

    async def fake_run_full_scrape(**_kwargs):
        return {
            "success": True,
            "status": "ok",
            "stats": {"total_products": 1, "details_scraped": 1},
        }

    fake_pool = FakeTorPool()
    monkeypatch.setattr(pipeline.TorPool, "get", classmethod(lambda _cls: fake_pool))
    monkeypatch.setattr(pipeline, "run_full_scrape", fake_run_full_scrape)

    scraper_pipeline = pipeline.SimplePipeline(
        sites=["demo"],
        site_options={"demo": {"use_tor": True}},
    )
    await scraper_pipeline._process_site("demo")

    assert calls == [True, False]
    assert scraper_pipeline.run_stats["demo"].success is True


def test_export_history_sources_match_track_history_paths():
    sources = history_sources_for_shop("demo")

    assert Path("data/price_history/demo.json") in sources
    assert sources[Path("data/products_added/demo.json")] == "_products_added"
    assert flatten_history_data({"sku1": [{"price": 1}]}) == [
        {"product_id": "sku1", "history": [{"price": 1}]}
    ]


def test_selector_validator_supports_legacy_config_shape():
    config = {
        "top_level_blocks": "li.menu-item",
        "top_level_link": "a.nav-top-link",
        "max_pages": 10,
        "type": "text",
    }

    entries = collect_selector_entries(config)
    selectors = {entry["selector"] for entry in entries}
    rows = validate_section(
        site="demo",
        section="frontpage",
        selector_config=config,
        html='<ul><li class="menu-item"><a class="nav-top-link">Cat</a></li></ul>',
        source="fixture",
    )

    assert "li.menu-item" in selectors
    assert "a.nav-top-link" in selectors
    assert {row["status"] for row in rows} == {"ok"}
