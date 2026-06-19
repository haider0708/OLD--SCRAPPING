#!/usr/bin/env python3
"""Validate configured CSS selectors against fixture and optional live HTML."""

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx
import yaml
from selectolax.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs" / "sites"
DATA_DIR = ROOT / "data"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper.base import detect_blocked_signals

SKIP_SCALAR_KEYS = {
    "attribute",
    "attr",
    "base_url",
    "brand_attr",
    "date",
    "deduplicate",
    "fields",
    "frontpage_url",
    "item_id_attr",
    "item_image_attrs",
    "item_reference_attr",
    "limit",
    "max_pages",
    "multiple",
    "name",
    "optional",
    "pagination_param",
    "pagination_path",
    "shop",
    "site_name",
    "transform",
    "type",
    "url",
}

SELECTOR_KEYS = {
    "container",
    "item_selector",
    "key_selector",
    "next_selector",
    "pagination_next",
    "product_id",
    "product_name",
    "product_url",
    "selector",
    "value_selector",
    "wait_selector",
}


def load_config(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def site_name_from_config(path: Path, config: Dict[str, Any]) -> str:
    site = config.get("site")
    if isinstance(site, dict) and site.get("name"):
        return str(site["name"])
    return str(config.get("site_name") or path.stem)


def base_url_from_config(config: Dict[str, Any]) -> Optional[str]:
    site = config.get("site")
    if isinstance(site, dict):
        return site.get("frontpage_url") or site.get("base_url")
    return config.get("frontpage_url") or config.get("base_url")


def selector_sections(config: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Return normalized selector sections across current and legacy config shapes."""
    selectors = config.get("selectors") if isinstance(config.get("selectors"), dict) else {}
    sections: List[Tuple[str, Dict[str, Any]]] = []

    for name in ("frontpage", "categories"):
        section = selectors.get(name)
        if isinstance(section, dict):
            sections.append((f"frontpage.{name}", section))
    if isinstance(config.get("frontpage"), dict):
        sections.append(("frontpage", config["frontpage"]))

    section = selectors.get("category_page") or config.get("category_page")
    if isinstance(section, dict):
        sections.append(("category_page", section))

    for name in ("product_page", "product_details"):
        section = selectors.get(name) or config.get(name)
        if isinstance(section, dict):
            sections.append((name, section))

    return sections


def section_fixture(site: str, section: str) -> Optional[Path]:
    html_dir = DATA_DIR / site / "html"
    if section.startswith("frontpage"):
        return html_dir / "frontpage.html"
    if section == "category_page":
        for name in ("listing_sample_1.html", "live_selected_category.html"):
            candidate = html_dir / name
            if candidate.exists():
                return candidate
        return html_dir / "listing_sample_1.html"
    if section in {"product_page", "product_details"}:
        return html_dir / "detail_sample_1.html"
    return None


def value_looks_like_selector(value: str) -> bool:
    value = value.strip()
    if not value or value.startswith(("http://", "https://", "/")):
        return False
    if value in {"text", "href", "src", "value", "link", "list", "float", "clean_price"}:
        return False
    css_markers = ".#[:>+~ =,'\"*|$^(),"
    if any(marker in value for marker in css_markers):
        return True
    return value in {"a", "article", "button", "div", "form", "h1", "h2", "img", "input", "li", "script", "span", "table", "td", "th", "tr", "ul"}


def collect_selector_entries(
    value: Any,
    path: str = "",
    parent_key: str = "",
    optional: bool = False,
) -> List[Dict[str, str]]:
    """Collect CSS selector strings from a mixed YAML selector config."""
    rows: List[Dict[str, str]] = []
    if isinstance(value, dict):
        child_optional = optional or value.get("optional") is True
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            rows.extend(collect_selector_entries(child, child_path, str(key), child_optional))
        return rows

    if isinstance(value, list):
        for idx, child in enumerate(value):
            rows.extend(collect_selector_entries(child, f"{path}[{idx}]", parent_key, optional))
        return rows

    if not isinstance(value, str):
        return rows

    key_lower = parent_key.lower()
    value = value.strip()
    if not value:
        return rows
    if (
        key_lower in SKIP_SCALAR_KEYS
        or key_lower.endswith("_attr")
        or key_lower.startswith("attr_")
    ):
        return rows

    explicit_selector_key = (
        key_lower in SELECTOR_KEYS
        or key_lower.endswith("_selector")
        or key_lower.endswith("_selectors")
        or key_lower.endswith("_link")
        or key_lower.endswith("_links")
        or key_lower.endswith("_blocks")
        or key_lower.endswith("_header")
        or key_lower.endswith("_list")
        or key_lower.startswith("item_")
        or key_lower.startswith("image_")
    )

    if explicit_selector_key or value_looks_like_selector(value):
        rows.append({"path": path, "selector": value, "optional": optional})
    return rows


def validate_selector(html: str, selector: str) -> Tuple[str, int, Optional[str]]:
    tree = HTMLParser(html)
    css_selector = selector.strip()
    if css_selector.startswith(":scope"):
        css_selector = "*" + css_selector[len(":scope") :]
    if css_selector.startswith((">", "+", "~")):
        css_selector = f"* {css_selector}"
    try:
        matches = tree.css(css_selector)
    except Exception as exc:
        return "invalid", 0, str(exc)
    count = len(matches)
    return ("ok" if count > 0 else "missing"), count, None


def normalize_fallback_statuses(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Downgrade missing selectors when a configured fallback chain has already matched."""
    matched_prefixes = set()
    for row in rows:
        path = row.get("path") or ""
        if row.get("status") != "ok":
            continue
        if ".selectors[" in path:
            matched_prefixes.add(path.split(".selectors[", 1)[0])
        elif ".fallback." not in path:
            matched_prefixes.add(path)

    for row in rows:
        if row.get("status") != "missing":
            continue
        path = row.get("path") or ""
        if ".selectors[" in path:
            prefix = path.split(".selectors[", 1)[0]
            if prefix in matched_prefixes:
                row["status"] = "fallback_miss"
        elif ".fallback." in path:
            prefix = path.split(".fallback.", 1)[0]
            if prefix in matched_prefixes:
                row["status"] = "fallback_not_needed"
    return rows


def validate_section(
    site: str,
    section: str,
    selector_config: Dict[str, Any],
    html: Optional[str],
    source: str,
    source_path: Optional[Path] = None,
    source_meta: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    selectors = collect_selector_entries(selector_config)
    rows: List[Dict[str, Any]] = []
    if not selectors:
        rows.append(
            {
                "site": site,
                "section": section,
                "path": None,
                "selector": None,
                "source": source,
                "source_path": str(source_path) if source_path else None,
                "status": "no_selectors",
                "count": 0,
                "error": None,
                "source_meta": source_meta or {},
            }
        )
        return rows

    if html is None:
        missing_status = "unreachable" if (source_meta or {}).get("error") else "skipped_no_html"
        for item in selectors:
            rows.append(
                {
                    "site": site,
                    "section": section,
                    "path": item["path"],
                    "selector": item["selector"],
                    "optional": bool(item.get("optional")),
                    "source": source,
                    "source_path": str(source_path) if source_path else None,
                    "status": missing_status,
                    "count": 0,
                    "error": (source_meta or {}).get("error"),
                    "source_meta": source_meta or {},
                }
            )
        return rows

    for item in selectors:
        status, count, error = validate_selector(html, item["selector"])
        if status == "missing" and (source_meta or {}).get("blocked_signals"):
            status = "blocked"
            error = ",".join((source_meta or {}).get("blocked_signals") or [])
        if status == "missing" and item.get("optional"):
            status = "optional_missing"
        rows.append(
            {
                "site": site,
                "section": section,
                "path": item["path"],
                "selector": item["selector"],
                "optional": bool(item.get("optional")),
                "source": source,
                "source_path": str(source_path) if source_path else None,
                "status": status,
                "count": count,
                "error": error,
                "source_meta": source_meta or {},
            }
        )
    return normalize_fallback_statuses(rows)


async def fetch_live_frontpage(
    client: httpx.AsyncClient, site: str, base_url: Optional[str]
) -> Tuple[Optional[str], Dict[str, Any]]:
    if not base_url:
        return None, {"error": "missing_base_url"}
    try:
        response = await client.get(base_url)
        html = response.text
        meta = {
            "url": base_url,
            "status_code": response.status_code,
            "final_url": str(response.url),
            "content_type": response.headers.get("content-type"),
            "blocked_signals": detect_blocked_signals(html, response.status_code),
            "error": None,
        }
        if not html.strip():
            meta["error"] = "empty_response"
            return None, meta
        return html, meta
    except Exception as exc:
        return None, {"url": base_url, "error": f"{exc.__class__.__name__}: {exc}"}


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    by_status = Counter(row["status"] for row in rows)
    by_site_status: Dict[str, Counter] = {}
    for row in rows:
        by_site_status.setdefault(row["site"], Counter())[row["status"]] += 1
    return {
        "by_status": dict(by_status),
        "by_site": {site: dict(counter) for site, counter in sorted(by_site_status.items())},
    }


async def validate_sites(args: argparse.Namespace) -> Dict[str, Any]:
    config_paths = [
        path
        for path in sorted(CONFIG_DIR.glob("*.yaml"))
        if not path.name.startswith("_")
    ]
    if args.sites:
        selected = set(args.sites)
        config_paths = [path for path in config_paths if path.stem in selected]

    rows: List[Dict[str, Any]] = []
    live_client = httpx.AsyncClient(
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        },
        timeout=httpx.Timeout(args.timeout, connect=min(args.timeout, 10.0)),
    )

    try:
        for config_path in config_paths:
            config = load_config(config_path)
            site = site_name_from_config(config_path, config)
            sections = selector_sections(config)

            for section, selector_config in sections:
                fixture_path = section_fixture(site, section)
                html = None
                if fixture_path and fixture_path.exists():
                    html = fixture_path.read_text(encoding="utf-8", errors="replace")
                rows.extend(
                    validate_section(
                        site=site,
                        section=section,
                        selector_config=selector_config,
                        html=html,
                        source="fixture",
                        source_path=fixture_path,
                    )
                )

            if args.live_frontpage:
                html, meta = await fetch_live_frontpage(live_client, site, base_url_from_config(config))
                for section, selector_config in sections:
                    if section.startswith("frontpage"):
                        rows.extend(
                            validate_section(
                                site=site,
                                section=section,
                                selector_config=selector_config,
                                html=html,
                                source="live_frontpage",
                                source_meta=meta,
                            )
                        )
    finally:
        await live_client.aclose()

    report = {
        "generated_at": datetime.now().isoformat(),
        "live_frontpage": bool(args.live_frontpage),
        "sites": [site_name_from_config(path, load_config(path)) for path in config_paths],
        "summary": summarize(rows),
        "rows": rows,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate scraper YAML selectors.")
    parser.add_argument("--sites", nargs="+", help="Optional site names to validate")
    parser.add_argument(
        "--output",
        default=str(DATA_DIR / "selector_validation_report.json"),
        help="JSON report path",
    )
    parser.add_argument(
        "--live-frontpage",
        action="store_true",
        help="Also validate frontpage selectors against live base URLs",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when any selector is invalid or missing",
    )
    args = parser.parse_args()

    report = asyncio.run(validate_sites(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = report["summary"]["by_status"]
    print(f"Selector validation wrote {output}")
    print("Status counts:", ", ".join(f"{k}={v}" for k, v in sorted(summary.items())))

    if args.strict and any(summary.get(status, 0) for status in ("invalid", "missing")):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
