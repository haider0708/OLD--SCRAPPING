import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

import scrape


@dataclass
class _Cat:
    url: str
    name: str
    level: str = "low"
    parent_names: list[str] = None

    def __post_init__(self):
        if self.parent_names is None:
            self.parent_names = []


def test_ranking_promotes_category_like_urls():
    candidates = [
        {"normalized_url": "https://shop.tn/account", "anchor_text": "account", "discovery_method": "menu"},
        {"normalized_url": "https://shop.tn/categorie/pc-gamer", "anchor_text": "pc gamer", "discovery_method": "menu"},
    ]
    ranked = sorted(
        candidates,
        key=lambda x: scrape.rank_category_candidate("expert_gaming", x["normalized_url"], x["anchor_text"], x["discovery_method"]),
        reverse=True,
    )
    assert ranked[0]["normalized_url"].endswith("/categorie/pc-gamer")


def test_probe_limit_not_tied_to_categories_count():
    assert scrape.resolve_probe_limit(categories_limit=1, category_probe_limit=None) == 30
    assert scrape.resolve_probe_limit(categories_limit=1, category_probe_limit=12) == 12


@pytest.mark.asyncio
async def test_failed_and_selected_html_snapshots_written(tmp_path):
    class Dummy:
        html_dir = tmp_path
        site_name = "dummy"

        async def fetch_html(self, url: str):
            if url.endswith("/ok"):
                return "<html><div class='product'></div></html>"
            return "<html><div class='empty'></div></html>"

        def extract_products_from_html(self, html: str):
            return [{"url": "https://shop.tn/p/1"}] if "product" in html else []

    candidates = [
        {"category": _Cat("https://shop.tn/empty", "empty"), "original_url": "https://shop.tn/empty", "normalized_url": "https://shop.tn/empty", "anchor_text": "empty", "discovery_method": "menu", "classification_reason": "real"},
        {"category": _Cat("https://shop.tn/ok", "ok"), "original_url": "https://shop.tn/ok", "normalized_url": "https://shop.tn/ok", "anchor_text": "ok", "discovery_method": "menu", "classification_reason": "real"},
    ]
    out = await scrape.select_live_product_category(
        scraper=Dummy(),
        site_name="dummy",
        candidates=candidates,
        probe_limit=10,
        max_depth=2,
        logger=logging.getLogger("t"),
    )
    assert out["selected"] is not None
    assert (tmp_path / "live_probe_1_empty_category.html").exists()
    assert (tmp_path / "live_failed_category_1.html").exists()
    assert (tmp_path / "live_selected_category.html").exists()
    summary = scrape.load_json(tmp_path.parent / "audit" / "probe_summary.json")
    assert isinstance(summary, list) and len(summary) >= 1
    row = summary[0]
    for key in [
        "probed_url",
        "depth",
        "status_code",
        "final_url",
        "html_file_path",
        "detected_product_count",
        "detected_subcategory_count",
        "selectors_tested",
        "blocker_signals",
        "js_render_signals",
        "classification",
    ]:
        assert key in row


@pytest.mark.asyncio
async def test_subcategory_enqueued_and_depth_respected(tmp_path):
    class Dummy:
        html_dir = tmp_path
        site_name = "dummy"

        async def fetch_html(self, url: str):
            if url.endswith("/c1"):
                return "<html><a href='/c2'>next</a></html>"
            if url.endswith("/c2"):
                return "<html><a href='/c3'>next</a></html>"
            return "<html><div class='product'></div></html>"

        def extract_products_from_html(self, html: str):
            return [{"url": "https://shop.tn/p/1"}] if "product" in html else []

    candidates = [
        {"category": _Cat("https://shop.tn/c1", "c1"), "original_url": "https://shop.tn/c1", "normalized_url": "https://shop.tn/c1", "anchor_text": "c1", "discovery_method": "menu", "classification_reason": "real"},
    ]
    out = await scrape.select_live_product_category(
        scraper=Dummy(),
        site_name="dummy",
        candidates=candidates,
        probe_limit=10,
        max_depth=1,
        logger=logging.getLogger("t"),
    )
    assert out["selected"] is None
    assert out["probed"] >= 1


@pytest.mark.asyncio
async def test_candidate_audit_json_has_required_fields(tmp_path):
    rows = [
        {
            "original_url": "https://shop.tn/a",
            "normalized_url": "https://shop.tn/a",
            "anchor_text": "A",
            "discovery_method": "menu",
            "classification_reason": "real product category",
            "probe_status": "pending",
            "product_count": 0,
            "selected": False,
        }
    ]
    out = tmp_path / "category_candidates.json"
    scrape.save_candidate_audit(out, rows)
    data = scrape.load_json(out)
    one = data[0]
    for key in [
        "original_url",
        "normalized_url",
        "anchor_text",
        "discovery_method",
        "classification_reason",
        "probe_status",
        "product_count",
        "selected",
    ]:
        assert key in one
