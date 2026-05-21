#!/usr/bin/env python3
"""
Scrape - Fast E-commerce Scraper

High-performance scraper for Tunisian e-commerce sites.

Usage:
    python scrape.py test --site mytek --categories 3 --products 5
    python scrape.py full --site mytek
    python scrape.py list
"""
import argparse
import asyncio
import inspect
import logging
import re
import sys
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode
from selectolax.parser import HTMLParser

try:
    from tqdm import tqdm
except ImportError:
    # Fallback if tqdm not installed
    class tqdm:
        def __init__(self, total=0, desc="", bar_format="", ncols=80):
            self.total = total
            self.n = 0
            self.desc = desc
        def update(self, n=1):
            self.n += n
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

from scraper.base import (
    BASE_DIR, DATA_DIR, LOGS_DIR,
    ScrapeStats, CategoryInfo,
    load_json, save_json, format_duration, get_date_folder,
    save_jsonl, load_jsonl,
    playwright_launch_args,
    save_text_atomic,
)
from scraper.sites import get_scraper, list_available_sites


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _safe_text(text: str) -> str:
    """Strip non-encodable unicode when console encoding is limited."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding, errors="strict")
        return text
    except UnicodeEncodeError:
        # Keep content readable while preserving ANSI color codes.
        plain = ANSI_RE.sub("", text)
        safe_plain = plain.encode(encoding, errors="replace").decode(encoding)
        return safe_plain


def _safe_print(text: str = ""):
    print(_safe_text(text))


def print_header(text, char="=", width=70):
    _safe_print(f"\n{Colors.CYAN}{char * width}{Colors.RESET}")
    _safe_print(f"{Colors.BOLD}{Colors.WHITE}  {text}{Colors.RESET}")
    _safe_print(f"{Colors.CYAN}{char * width}{Colors.RESET}")


def print_step(step, text):
    _safe_print(f"\n{Colors.BLUE}[{step}]{Colors.RESET} {Colors.BOLD}{text}{Colors.RESET}")


def print_success(text):
    _safe_print(f"  {Colors.GREEN}✓{Colors.RESET} {text}")


def print_error(text):
    _safe_print(f"  {Colors.RED}✗{Colors.RESET} {text}")


def print_info(text):
    _safe_print(f"  {Colors.DIM}→{Colors.RESET} {text}")


def print_stat(label, value, color=None):
    if color is None:
        color = Colors.WHITE
    _safe_print(f"  {Colors.DIM}{label}:{Colors.RESET} {color}{value}{Colors.RESET}")


def is_fast_scraper(scraper):
    from scraper.base import FastScraper
    return isinstance(scraper, FastScraper)


LIVE_CATEGORY_EXCLUDE_PATTERNS = (
    "cart",
    "checkout",
    "account",
    "login",
    "register",
    "wishlist",
    "contact",
    "about",
    "blog",
    "search",
)

LIVE_CATEGORY_POSITIVE_TERMS = (
    "categorie", "category", "catalog", "rayon", "shop", "produit", "products", "gaming",
    "informatique", "pc", "laptop", "phone", "smartphone", "accessoire",
)

LIVE_CATEGORY_NEGATIVE_TERMS = (
    "home", "contact", "account", "cart", "login", "search", "wishlist",
    "conditions", "livraison", "sav", "blog", "faq", "about",
)

def _sanitize_status_label(label: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", (label or "unknown").strip().lower())
    return safe[:60] or "unknown"


def normalize_category_url(url: str) -> Optional[str]:
    """Normalize category URL for stable dedupe/probing."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return None
    parts = urlsplit(url)
    query_items = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.startswith("utm_")]
    query = urlencode(query_items)
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, query, ""))


def classify_category_url(url: str, seen: set) -> str:
    """Classify a category URL for filtering/probing."""
    normalized = normalize_category_url(url)
    if not normalized:
        return "invalid/non-product URL"
    if normalized in seen:
        return "duplicate"
    low = normalized.lower()
    if any(token in low for token in LIVE_CATEGORY_EXCLUDE_PATTERNS):
        return "navigation-only page"
    if "/brand/" in low or "/marque/" in low:
        return "brand page"
    return "real product category"


def rank_category_candidate(site_name: str, normalized_url: str, anchor_text: str, discovery_method: str) -> int:
    """Return deterministic rank score for category candidates."""
    score = 0
    low_url = (normalized_url or "").lower()
    low_text = (anchor_text or "").lower()
    method = (discovery_method or "").lower()
    path = urlparse(low_url).path or "/"
    depth = len([p for p in path.split("/") if p])

    score += min(depth, 6) * 2
    if any(t in low_url for t in LIVE_CATEGORY_POSITIVE_TERMS):
        score += 20
    if any(t in low_text for t in LIVE_CATEGORY_POSITIVE_TERMS):
        score += 12
    if "menu" in method or "breadcrumb" in method or "category" in method:
        score += 10
    if any(t in low_url for t in LIVE_CATEGORY_NEGATIVE_TERMS):
        score -= 30
    if any(t in low_text for t in LIVE_CATEGORY_NEGATIVE_TERMS):
        score -= 25
    if "/brand/" in low_url or "/marque/" in low_url:
        score -= 20
    if depth <= 1:
        score -= 8
    if site_name in low_url:
        score += 1
    return score


def resolve_probe_limit(categories_limit: int, category_probe_limit: Optional[int]) -> int:
    """Use explicit probe limit when provided, otherwise keep high default."""
    if category_probe_limit and category_probe_limit > 0:
        return category_probe_limit
    return 30


def save_candidate_audit(path: Path, rows: List[Dict[str, Any]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = []
    for row in rows:
        item = dict(row)
        item.pop("category", None)
        serializable.append(item)
    save_json(serializable, path)

def save_probe_summary(path: Path, rows: List[Dict[str, Any]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(rows, path)


async def _fetch_probe_page(scraper, url: str) -> Dict[str, Any]:
    """Fetch page and return metadata-rich response where available."""
    if hasattr(scraper, "fetch_html_with_meta"):
        data = await scraper.fetch_html_with_meta(url)
        if isinstance(data, dict):
            return data
    html = await _fetch_detail_html_snapshot(scraper, url)
    return {
        "html": html,
        "status_code": None,
        "final_url": url,
        "content_type": None,
        "content_encoding": None,
        "error": None if html else "fetch_failed",
    }


async def _fetch_detail_html_snapshot(scraper, url: str) -> Optional[str]:
    """Fetch raw product HTML for live evidence across scraper backends."""
    if hasattr(scraper, "fetch_html"):
        return await scraper.fetch_html(url)

    from playwright.async_api import async_playwright

    timeout = getattr(scraper, "page_timeout", 30000)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=playwright_launch_args())
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=timeout)
            await asyncio.sleep(getattr(scraper, "wait_after_load", 0))
            return await page.content()
        finally:
            await page.close()
            await browser.close()


def _extract_subcategory_links(html: str, base_url: str, max_links: int = 25) -> List[Dict[str, str]]:
    """Extract probable subcategory links from HTML for bounded traversal."""
    tree = HTMLParser(html)
    out = []
    base_host = urlparse(base_url).netloc.lower()
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(base_url, href)
        norm = normalize_category_url(absolute)
        if not norm:
            continue
        host = urlparse(norm).netloc.lower()
        if host != base_host:
            continue
        text = (node.text() or "").strip()
        low = (norm + " " + text.lower())
        if any(tok in low for tok in LIVE_CATEGORY_EXCLUDE_PATTERNS):
            continue
        out.append({"url": norm, "anchor_text": text, "discovery_method": "probe_subcategory"})
        if len(out) >= max_links:
            break
    return out


def _make_category_like(proto: Any, url: str, name: str):
    """Create category-like object for queue traversal."""
    cls = type(proto)
    kwargs = {"url": url, "name": name}
    if hasattr(proto, "location"):
        kwargs["location"] = getattr(proto, "location")
    if hasattr(proto, "level"):
        kwargs["level"] = getattr(proto, "level")
    if hasattr(proto, "parent_names"):
        kwargs["parent_names"] = getattr(proto, "parent_names")
    try:
        return cls(**kwargs)
    except TypeError:
        return cls(url=url, name=name)


def filter_live_categories(categories: List[Any]) -> Dict[str, Any]:
    """
    Filter and dedupe category candidates before live probing.
    Returns dict with kept CategoryInfo and detailed filtered reasons.
    """
    seen = set()
    kept = []
    candidates = []
    filtered = []
    all_rows = []

    for cat in categories:
        url = getattr(cat, "url", "")
        label = classify_category_url(url, seen)
        all_rows.append({"name": getattr(cat, "name", ""), "url": url, "classification": label})
        normalized = normalize_category_url(url)
        if label == "real product category" and normalized:
            seen.add(normalized)
            if normalized != url:
                cat.url = normalized
            kept.append(cat)
            candidates.append(
                {
                    "category": cat,
                    "original_url": url,
                    "normalized_url": normalized,
                    "anchor_text": getattr(cat, "name", ""),
                    "discovery_method": "menu",
                    "classification_reason": label,
                    "rank_score": 0,
                    "probe_status": "pending",
                    "product_count": 0,
                    "selected": False,
                }
            )
        else:
            filtered.append({"name": getattr(cat, "name", ""), "url": url, "reason": label})

    return {"kept": kept, "filtered": filtered, "all_rows": all_rows, "candidates": candidates}


async def select_live_product_category(
    scraper,
    site_name: Optional[str] = None,
    candidates: Optional[List[Dict[str, Any]]] = None,
    probe_limit: int = 30,
    max_depth: int = 2,
    logger: Optional[logging.Logger] = None,
    categories: Optional[List[Any]] = None,
):
    """
    Probe candidate categories and return first product-bearing category.
    Saves probing HTML evidence files for traceability.
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    if site_name is None:
        site_name = getattr(scraper, "site_name", "unknown")
    if candidates is None and categories is not None:
        filtered = filter_live_categories(categories)
        candidates = filtered["candidates"]
    candidates = candidates or []

    html_dir = scraper.html_dir
    base_dir = getattr(scraper, "_base_data_dir", None)
    if base_dir is None:
        base_dir = html_dir.parent
    audit_path = Path(base_dir) / "audit" / "category_candidates.json"
    for row in candidates:
        row["rank_score"] = rank_category_candidate(
            site_name, row.get("normalized_url", ""), row.get("anchor_text", ""), row.get("discovery_method", "")
        )
    candidates.sort(key=lambda r: r["rank_score"], reverse=True)
    save_candidate_audit(audit_path, candidates)

    q = deque()
    for c in candidates:
        q.append((c, 0))
    seen = {c.get("normalized_url") for c in candidates}
    probed = 0
    failed_rows = []
    failed_written = 0
    probe_summary_rows = []
    selector_info = []
    try:
        product_selectors = scraper.selectors.get("products")
        if isinstance(product_selectors, dict):
            selector_info = list(product_selectors.keys())
        elif isinstance(product_selectors, list):
            selector_info = [str(x) for x in product_selectors]
        elif isinstance(product_selectors, str):
            selector_info = [product_selectors]
    except Exception:
        selector_info = []

    while q and probed < probe_limit:
        cand, depth = q.popleft()
        cat = cand["category"]
        probed += 1
        cand["probe_status"] = "probed"
        fetch_meta = await _fetch_probe_page(scraper, cat.url)
        html = fetch_meta.get("html")
        status_code = fetch_meta.get("status_code")
        final_url = fetch_meta.get("final_url") or cat.url
        content_type = fetch_meta.get("content_type")
        content_encoding = fetch_meta.get("content_encoding")
        meta_blocked_signals = fetch_meta.get("blocked_signals") or []
        if not html:
            cand["probe_status"] = "failed_fetch"
            failed_rows.append({"url": cat.url, "reason": "fetch_failed"})
            probe_summary_rows.append(
                {
                    "probed_url": cat.url,
                    "depth": depth,
                    "status_code": status_code,
                    "final_url": final_url,
                    "html_file_path": None,
                    "detected_product_count": 0,
                    "detected_subcategory_count": 0,
                    "selectors_tested": selector_info,
                    "blocker_signals": meta_blocked_signals,
                    "js_render_signals": [],
                    "classification": "fetch_failed",
                    "content_type": content_type,
                    "content_encoding": content_encoding,
                }
            )
            continue

        products = []
        parse_error = None
        if not hasattr(scraper, "extract_products_from_html"):
            # BaseScraper (Playwright) — cannot parse HTML without a browser.
            # Treat the first successfully fetched page as the selected category.
            save_text_atomic(html, html_dir / "live_selected_category.html")
            cand["selected"] = True
            cand["probe_status"] = "selected_playwright"
            probe_summary_rows.append({
                "probed_url": cat.url, "depth": depth, "status_code": status_code,
                "final_url": final_url,
                "html_file_path": str(html_dir / "live_selected_category.html"),
                "detected_product_count": 0, "detected_subcategory_count": 0,
                "selectors_tested": [], "blocker_signals": meta_blocked_signals, "js_render_signals": [],
                "classification": "selected_playwright", "content_type": content_type,
                "content_encoding": content_encoding,
            })
            probe_summary_path = Path(base_dir) / "audit" / "probe_summary.json"
            save_probe_summary(probe_summary_path, probe_summary_rows)
            save_candidate_audit(audit_path, candidates)
            logger.info(f"[live.category.selected.playwright] url={cat.url} probed={probed}")
            return {
                "selected": cat, "selected_html": html, "selected_products_preview": [],
                "probed": probed, "failed_rows": failed_rows, "candidates": candidates,
            }
        try:
            products = scraper.extract_products_from_html(html) or []
        except Exception as exc:
            parse_error = str(exc)
            products = []
        cand["product_count"] = len(products)

        if products:
            save_text_atomic(html, html_dir / "live_selected_category.html")
            cand["selected"] = True
            cand["probe_status"] = "selected"
            probe_summary_rows.append(
                {
                    "probed_url": cat.url,
                    "depth": depth,
                    "status_code": status_code,
                    "final_url": final_url,
                    "html_file_path": str(html_dir / "live_selected_category.html"),
                    "detected_product_count": len(products),
                    "detected_subcategory_count": 0,
                    "selectors_tested": selector_info,
                    "blocker_signals": meta_blocked_signals,
                    "js_render_signals": [],
                    "classification": "selected",
                    "content_type": content_type,
                    "content_encoding": content_encoding,
                }
            )
            probe_summary_path = Path(base_dir) / "audit" / "probe_summary.json"
            save_probe_summary(probe_summary_path, probe_summary_rows)
            save_candidate_audit(audit_path, candidates)
            logger.info(f"[live.category.selected] url={cat.url} probed={probed} products={len(products)}")
            return {
                "selected": cat,
                "selected_html": html,
                "selected_products_preview": products,
                "probed": probed,
                "failed_rows": failed_rows,
                "candidates": candidates,
            }

        sub_links = _extract_subcategory_links(html, final_url)
        subcategory_count = len(sub_links)
        blocker_signals = list(meta_blocked_signals)
        js_render_signals = []
        low_html = html.lower()
        reason = "empty_category" if not parse_error else f"parser_error: {parse_error}"
        if "captcha" in low_html:
            blocker_signals.append("captcha")
        if "cloudflare" in low_html:
            blocker_signals.append("cloudflare")
        if "attention required" in low_html:
            blocker_signals.append("challenge_page")
        if "<script" in low_html and "product" not in low_html:
            js_render_signals.append("script_without_product_markup")
        if subcategory_count > 0 and not parse_error:
            js_render_signals.append("subcategory_links_present")
        if not parse_error and blocker_signals:
            reason = "blocked_or_challenge"
        elif not parse_error and js_render_signals:
            reason = "likely_js_rendered_or_subcategory_only"
        cand["probe_status"] = reason

        probe_path = html_dir / f"live_probe_{probed}_{_sanitize_status_label(reason)}.html"
        save_text_atomic(html, probe_path)
        saved_html = str(probe_path)
        if failed_written < 5:
            fail_path = html_dir / f"live_failed_category_{failed_written+1}.html"
            save_text_atomic(html, fail_path)
            failed_written += 1

        failed_rows.append(
            {
                "url": cat.url,
                "reason": reason,
                "saved_html": saved_html,
            }
        )
        probe_summary_rows.append(
            {
                "probed_url": cat.url,
                "depth": depth,
                "status_code": status_code,
                "final_url": final_url,
                "html_file_path": saved_html,
                "detected_product_count": len(products),
                "detected_subcategory_count": subcategory_count,
                "selectors_tested": selector_info,
                "blocker_signals": blocker_signals,
                "js_render_signals": js_render_signals,
                "classification": reason,
                "content_type": content_type,
                "content_encoding": content_encoding,
            }
        )

        if depth < max_depth and not products:
            for sub in sub_links:
                norm = sub["url"]
                if norm in seen:
                    continue
                seen.add(norm)
                sub_cat = _make_category_like(
                    cat,
                    url=norm,
                    name=sub["anchor_text"] or norm.rstrip("/").split("/")[-1],
                )
                new_cand = {
                    "category": sub_cat,
                    "original_url": sub["url"],
                    "normalized_url": norm,
                    "anchor_text": sub["anchor_text"],
                    "discovery_method": sub["discovery_method"],
                    "classification_reason": "subcategory_discovered_from_probe",
                    "rank_score": rank_category_candidate(site_name, norm, sub["anchor_text"], sub["discovery_method"]),
                    "probe_status": "pending",
                    "product_count": 0,
                    "selected": False,
                }
                candidates.append(new_cand)
                q.append((new_cand, depth + 1))

        save_candidate_audit(audit_path, candidates)

    logger.warning(f"[live.category.none] probed={probed} limit={probe_limit}")
    probe_summary_path = Path(base_dir) / "audit" / "probe_summary.json"
    save_probe_summary(probe_summary_path, probe_summary_rows)
    save_candidate_audit(audit_path, candidates)
    return {"selected": None, "probed": probed, "failed_rows": failed_rows, "candidates": candidates}


def setup_logger(site_name, log_level=logging.WARNING):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOGS_DIR / f"{site_name}_{timestamp}.log"
    logger = logging.getLogger(f"scraper_{site_name}_{timestamp}")
    logger.setLevel(logging.DEBUG)
    logger.handlers = []
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(log_level)
    ch.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(ch)
    return logger


async def close_scraper_resources(scraper, logger: Optional[logging.Logger] = None):
    """Close HTTPX clients and shared Playwright browsers if a scraper owns them."""
    for method_name in ("close", "_close_browser"):
        method = getattr(scraper, method_name, None)
        if not callable(method):
            continue
        try:
            result = method()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            if logger:
                logger.debug(f"[scraper.cleanup.failed] method={method_name} error={exc}")


def should_scrape_details(scraper, requested: bool = True) -> bool:
    """Respect site-level skip_details while preserving CLI --no-details."""
    if not requested:
        return False
    return not bool(getattr(scraper, "config", {}).get("skip_details"))


def save_run_summary(scraper, filename: str, payload: Dict[str, Any], logger=None):
    if scraper is None:
        return
    try:
        save_json(payload, scraper.data_dir / filename, logger)
    except Exception as exc:
        if logger:
            logger.warning(f"[run.summary.failed] file={filename} error={exc}")


async def scrape_categories_fast(scraper, categories_list, num_workers, pbar):
    results = {}
    sem = asyncio.Semaphore(num_workers)

    async def worker(cat):
        async with sem:
            try:
                prods = await scraper.scrape_all_pages(cat.url)
                results[cat.url] = {"category": cat, "products": prods, "success": True}
            except Exception as e:
                results[cat.url] = {"category": cat, "products": [], "success": False, "error": str(e)}
            finally:
                if pbar is not None:
                    pbar.update(1)

    await asyncio.gather(*[worker(c) for c in categories_list])
    return results


async def scrape_categories_playwright(scraper, categories_list, num_workers, stats, pbar):
    from playwright.async_api import async_playwright
    results = {}
    queue = asyncio.Queue()
    for c in categories_list:
        await queue.put(c)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=playwright_launch_args())
        try:
            async def worker(wid):
                ctx = await browser.new_context()
                page = await ctx.new_page()
                try:
                    while True:
                        try:
                            cat = await asyncio.wait_for(queue.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            if queue.empty():
                                break
                            continue
                        try:
                            prods = await scraper.scrape_category_all_pages(ctx, cat, stats)
                            results[cat.url] = {"category": cat, "products": prods, "success": True}
                        except Exception as e:
                            results[cat.url] = {"category": cat, "products": [], "success": False, "error": str(e)}
                        finally:
                            if pbar is not None:
                                pbar.update(1)
                            queue.task_done()
                finally:
                    await page.close()
                    await ctx.close()

            await asyncio.gather(*[worker(i) for i in range(num_workers)])
            await queue.join()
        finally:
            await browser.close()
    return results


async def scrape_details_fast(scraper, items, num_workers, pbar):
    results = {}
    sem = asyncio.Semaphore(num_workers)

    async def worker(item):
        async with sem:
            url = item["url"]
            try:
                det = await scraper.scrape_product_details(url)
                results[url] = {"details": det, "item": item, "success": True}
            except Exception as e:
                results[url] = {"item": item, "success": False, "error": str(e)}
            finally:
                if pbar is not None:
                    pbar.update(1)

    await asyncio.gather(*[worker(i) for i in items])
    return results


async def scrape_details_playwright(scraper, items, num_workers, pbar):
    from playwright.async_api import async_playwright
    results = {}
    queue = asyncio.Queue()
    for i in items:
        await queue.put(i)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=playwright_launch_args())
        try:
            async def worker(wid):
                ctx = await browser.new_context()
                page = await ctx.new_page()
                try:
                    while True:
                        try:
                            item = await asyncio.wait_for(queue.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            if queue.empty():
                                break
                            continue
                        url = item["url"]
                        try:
                            det = await scraper.scrape_product_details(page, url)
                            results[url] = {"details": det, "item": item, "success": True}
                        except Exception as e:
                            results[url] = {"item": item, "success": False, "error": str(e)}
                        finally:
                            if pbar is not None:
                                pbar.update(1)
                            queue.task_done()
                finally:
                    await page.close()
                    await ctx.close()

            await asyncio.gather(*[worker(i) for i in range(num_workers)])
            await queue.join()
        finally:
            await browser.close()
    return results


async def run_full_scrape(site_name, num_workers=16, detail_workers=64, limit=None, logger=None, scrape_details=True):
    if logger is None:
        logger = setup_logger(site_name)
    
    start_time = time.time()
    ts = get_date_folder()
    scraper = None
    result = {"site": site_name, "success": False, "status": "failed", "error": None, "stats": None, "output_path": None, "duration_seconds": 0}

    print_header(f"🚀 SCRAPING: {site_name.upper()}")
    if logger:
        logger.info(f"[shop.start] site={site_name} workers={num_workers} detail_workers={detail_workers} limit={limit}")
    print_stat("Started", datetime.now().strftime('%Y-%m-%d %H:%M:%S'), Colors.CYAN)
    print_stat("Folder", ts, Colors.DIM)
    print_stat("Workers", f"{num_workers} / {detail_workers}", Colors.DIM)
    if limit:
        print_stat("Limit", f"{limit} categories", Colors.YELLOW)
    
    try:
        scraper = get_scraper(site_name, logger)
        scraper._current_data_dir = DATA_DIR / site_name / ts
        scraper._current_data_dir.mkdir(parents=True, exist_ok=True)
        if scrape_details and not should_scrape_details(scraper, requested=True):
            scrape_details = False
            print_info("Details disabled by site config")
            if logger:
                logger.info(f"[shop.details.skipped] site={site_name} reason=site_config")
    except ValueError as e:
        print_error(str(e))
        result["error"] = str(e)
        return result
    
    stats = ScrapeStats()
    stats.site = site_name
    stats.start_time = datetime.now().isoformat()
    stats.workers_used = num_workers
    
    # Step 1: Download
    print_step(1, "Downloading frontpage")
    t0 = time.time()
    try:
        await scraper.download_frontpage()
        print_success(f"Done in {time.time()-t0:.1f}s")
        if logger:
            logger.info(f"[shop.frontpage.ok] site={site_name}")
    except Exception as e:
        print_error(str(e))
        if logger:
            logger.error(f"[shop.frontpage.failed] site={site_name} error={e}")
        result["error"] = str(e)
        save_run_summary(scraper, "run_summary.json", result, logger)
        await close_scraper_resources(scraper, logger)
        return result
    
    # Step 2: Categories
    print_step(2, "Extracting categories")
    t0 = time.time()
    try:
        cat_path = scraper.extract_categories()
        cat_data = load_json(cat_path)
        # Use the proper build_scrape_queue method with fallback logic
        cat_list = scraper.build_scrape_queue(cat_data)
        stats.total_categories = len(cat_list)
        cs = cat_data.get("stats", {})
        print_success(f"Found {cs.get('top_level',0)} top → {cs.get('low_level',0)} low → {cs.get('subcategory',0)} sub")
        print_info(f"Total: {len(cat_list)} categories")
        if logger:
            logger.info(f"[shop.categories.ok] site={site_name} queue={len(cat_list)}")
    except Exception as e:
        print_error(str(e))
        if logger:
            logger.error(f"[shop.categories.failed] site={site_name} error={e}")
        result["error"] = str(e)
        save_run_summary(scraper, "run_summary.json", result, logger)
        await close_scraper_resources(scraper, logger)
        return result
    
    # Step 3: Products
    print_step(3, "Scraping products")
    t0 = time.time()
    if limit:
        cat_list = cat_list[:limit]
        print_info(f"Limited to {limit}")

    pbar = tqdm(total=len(cat_list), desc=f"  {Colors.GREEN}Categories{Colors.RESET}", bar_format="{desc}: {percentage:3.0f}%|{bar:30}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]", ncols=80)
    try:
        if is_fast_scraper(scraper):
            cat_res = await scrape_categories_fast(scraper, cat_list, num_workers, pbar)
        else:
            cat_res = await scrape_categories_playwright(scraper, cat_list, num_workers, stats, pbar)
    finally:
        pbar.close()

    # Track failures for better reporting
    failed_categories = []
    for url, r in cat_res.items():
        if not r.get("success"):
            cat = r.get("category")
            error = r.get("error", "Unknown error")
            failed_categories.append({"url": url, "name": cat.name if cat else url, "error": error})
            continue
        cat = r["category"]
        prods = r["products"]
        loc = cat.location
        try:
            if len(loc) == 1:
                # Top-level category
                cat_data["categories"][loc[0]]["products"] = prods
            elif len(loc) == 2:
                # Low-level category
                cat_data["categories"][loc[0]]["low_level_categories"][loc[1]]["products"] = prods
            elif len(loc) == 3:
                # Subcategory
                cat_data["categories"][loc[0]]["low_level_categories"][loc[1]]["subcategories"][loc[2]]["products"] = prods
            stats.categories_scraped += 1
        except (IndexError, KeyError) as e:
            print_error(f"Failed to merge category {cat.name}: {e}")
            failed_categories.append({"url": url, "name": cat.name, "error": f"Merge error: {e}"})

    total_prods = 0
    for tc in cat_data.get("categories", []):
        # Count products in top-level categories
        tp = tc.get("products", [])
        for p in tp:
            p["shop"] = site_name
        total_prods += len(tp)
        tc["product_count"] = len(tp)

        for lc in tc.get("low_level_categories", []):
            lp = lc.get("products", [])
            for p in lp:
                p["shop"] = site_name
            total_prods += len(lp)
            lc["product_count"] = len(lp)
            for sc in lc.get("subcategories", []):
                sp = sc.get("products", [])
                for p in sp:
                    p["shop"] = site_name
                total_prods += len(sp)
                sc["product_count"] = len(sp)

    stats.total_products = total_prods
    ok = sum(1 for r in cat_res.values() if r.get("success"))
    fail = len(cat_res) - ok
    print_success(f"Scraped {stats.categories_scraped}/{len(cat_list)} in {time.time()-t0:.1f}s")
    print_info(f"Found {Colors.GREEN}{total_prods:,}{Colors.RESET} products")
    if fail > 0:
        print_info(f"{Colors.RED}{fail}{Colors.RESET} categories failed")
        if logger:
            logger.warning(f"[shop.categories.partial] site={site_name} failed={fail} successful={ok}")
        # Log first few failures for debugging
        if failed_categories and logger:
            for fc in failed_categories[:5]:  # Log first 5 failures
                logger.warning(f"  Failed category: {fc['name']} - {fc['error']}")

        # Save category failures to JSON file
        if failed_categories:
            failure_data = {
                "site": site_name,
                "scraped_at": datetime.now().isoformat(),
                "total_failures": len(failed_categories),
                "failures": failed_categories
            }
            failure_path = scraper.data_dir / f"{site_name}_categories_failures.json"
            save_json(failure_data, failure_path, logger)
            print_info(f"Saved {len(failed_categories)} category failures to {failure_path}")

    # Flatten products into a flat list with category information
    flattened_products = []
    for tc in cat_data.get("categories", []):
        top_category = tc.get("name", "")

        # Add products directly in top-level categories (fallback categories)
        for p in tc.get("products", []):
            product_copy = p.copy()
            product_copy.update({
                "top_category": top_category,
                "low_category": None,  # No low category for top-level products
                "subcategory": None   # No subcategory for top-level products
            })
            flattened_products.append(product_copy)

        for lc in tc.get("low_level_categories", []):
            low_category = lc.get("name", "")
            # Add products directly in low-level categories
            for p in lc.get("products", []):
                product_copy = p.copy()
                product_copy.update({
                    "top_category": top_category,
                    "low_category": low_category,
                    "subcategory": None  # No subcategory for low-level products
                })
                flattened_products.append(product_copy)

            # Add products in subcategories
            for sc in lc.get("subcategories", []):
                subcategory = sc.get("name", "")
                for p in sc.get("products", []):
                    product_copy = p.copy()
                    product_copy.update({
                        "top_category": top_category,
                        "low_category": low_category,
                        "subcategory": subcategory
                    })
                    flattened_products.append(product_copy)

    out_path = scraper.data_dir / "products.json"
    save_json(flattened_products, out_path, logger)

    # Save summary separately
    summary = {
        "site": site_name,
        "shop": site_name,
        "scraped_at": datetime.now().isoformat(),
        "duration_seconds": time.time() - t0,
        "total_products": len(flattened_products),
        "status": "ok" if len(flattened_products) > 0 else "degraded",
        "scrape_stats": asdict(stats),
        "failed_categories": failed_categories,  # Include specific failures
    }
    summary_path = scraper.data_dir / "products_summary.json"
    save_json(summary, summary_path, logger)

    # Phase 3: Details (completely separate from products listing)
    det_count = 0
    if scrape_details and total_prods > 0:
        print_step(4, "Scraping product details")
        t0 = time.time()

        # Collect all product URLs with category information
        product_items = []
        for tc in cat_data.get("categories", []):
            top_category = tc.get("name", "")

            # Add products directly in top-level categories
            for p in tc.get("products", []):
                if p.get("url"):
                    product_items.append({
                        "id": p.get("id"),
                        "url": p["url"],
                        "top_category": top_category,
                        "low_category": None,
                        "subcategory": None
                    })

            for lc in tc.get("low_level_categories", []):
                low_category = lc.get("name", "")
                # Add products directly in low-level categories
                for p in lc.get("products", []):
                    if p.get("url"):
                        product_items.append({
                            "id": p.get("id"),
                            "url": p["url"],
                            "top_category": top_category,
                            "low_category": low_category,
                            "subcategory": None
                        })
                # Add products in subcategories
                for sc in lc.get("subcategories", []):
                    subcategory = sc.get("name", "")
                    for p in sc.get("products", []):
                        if p.get("url"):
                            product_items.append({
                                "id": p.get("id"),
                                "url": p["url"],
                                "top_category": top_category,
                                "low_category": low_category,
                                "subcategory": subcategory
                            })

        print_info(f"Found {len(product_items):,} product URLs to scrape details")

        # Scrape details for each product URL (completely independent)
        items = product_items

        pbar = tqdm(total=len(items), desc=f"  {Colors.MAGENTA}Details{Colors.RESET}", bar_format="{desc}:   {percentage:3.0f}%|{bar:30}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]", ncols=80)
        try:
            if is_fast_scraper(scraper):
                det_res = await scrape_details_fast(scraper, items, detail_workers, pbar)
            else:
                det_res = await scrape_details_playwright(scraper, items, detail_workers, pbar)
        finally:
            pbar.close()

        # Create completely separate detailed products data structure
        detailed_products = []
        failed_details = []

        for item in items:
            url = item["url"]
            r = det_res.get(url)
            if not r or not r.get("success"):
                error = r.get("error", "Unknown error") if r else "No result"
                failed_details.append({"id": item.get("id"), "url": url, "error": error})
                continue

            det = r["details"]

            # Ensure available is always boolean or null, never inconsistent
            available_value = det.get("available")
            if available_value is None and det.get("availability"):
                # Try to infer from availability text if available is null
                avail_text = str(det.get("availability", "")).lower()
                if "en stock" in avail_text or "disponible" in avail_text:
                    available_value = True
                elif "epuisé" in avail_text or "rupture" in avail_text or "indisponible" in avail_text:
                    available_value = False

            # Build complete detailed product record with category information
            detailed_product = {
                "url": url,
                "shop": site_name,
                "scraped_at": datetime.now().isoformat(),
                "top_category": item.get("top_category"),
                "low_category": item.get("low_category"),
                "subcategory": item.get("subcategory"),
                **det,  # Include all fields from detailed scraping
                "available": available_value  # Override with processed value
            }

            detailed_products.append(detailed_product)
            det_count += 1

        fail_det = len(items) - det_count
        print_success(f"Scraped {det_count:,}/{len(items):,} product details in {time.time()-t0:.1f}s")
        if fail_det > 0:
            print_info(f"{Colors.RED}{fail_det:,}{Colors.RESET} details failed")
            if logger:
                logger.warning(f"[shop.details.partial] site={site_name} failed={fail_det} successful={det_count}")
            # Log first few failures for debugging
            if failed_details and logger:
                for fd in failed_details[:5]:  # Log first 5 failures
                    logger.warning(f"  Failed detail: {fd['url'][:60]}... - {fd['error']}")

            # Save details failures to JSON file
            if failed_details:
                failure_data = {
                    "site": site_name,
                    "scraped_at": datetime.now().isoformat(),
                    "total_failures": len(failed_details),
                    "failures": failed_details
                }
                failure_path = scraper.data_dir / f"{site_name}_details_failures.json"
                save_json(failure_data, failure_path, logger)
                print_info(f"Saved {len(failed_details)} details failures to {failure_path}")

        # Save detailed products as completely separate file
        # Save detailed products as a direct list (JSON Array)
        det_path = scraper.data_dir / "products_detailed.json"
        save_json(detailed_products, det_path, logger)

        # Save details summary separately
        det_summary = {
            "site": site_name,
            "shop": site_name,
            "scraped_at": datetime.now().isoformat(),
            "total_products": len(detailed_products),
            "scrape_stats": {
                "total_attempted": len(items),
                "successful": det_count,
                "failed": fail_det
            },
            "failed_details": failed_details,  # Include specific failures with URLs
        }
        summary_path = scraper.data_dir / "products_detailed_summary.json"
        save_json(det_summary, summary_path, logger)

    dur = time.time() - start_time
    print_header(f"✅ COMPLETE: {site_name.upper()}", "-")
    print_stat("Duration", format_duration(dur), Colors.CYAN)
    print_stat("Categories", f"{stats.categories_scraped}/{stats.total_categories}", Colors.WHITE)
    print_stat("Products", f"{stats.total_products:,}", Colors.GREEN)
    print_stat("Details", f"{det_count:,}", Colors.MAGENTA)
    print_stat("Output", str(scraper.data_dir), Colors.DIM)
    print()

    result["success"] = True
    result["status"] = "ok" if stats.total_products > 0 else "degraded"
    result["stats"] = asdict(stats)
    result["stats"]["details_scraped"] = det_count
    result["output_path"] = str(out_path)
    result["duration_seconds"] = dur
    if logger:
        logger.info(
            f"[shop.end] site={site_name} success=true categories={stats.categories_scraped}/{stats.total_categories} "
            f"products={stats.total_products} details={det_count} duration={dur:.1f}s"
        )
    save_run_summary(scraper, "run_summary.json", result, logger)
    await close_scraper_resources(scraper, logger)
    return result


def limit_products_in_data(data, n):
    # For flattened structure, limit products per category combination
    if "products" in data:
        # Group products by category combination and limit each group
        from collections import defaultdict
        category_groups = defaultdict(list)

        for product in data["products"]:
            key = (product.get("top_category", ""), product.get("low_category", ""), product.get("subcategory", ""))
            category_groups[key].append(product)

        limited_products = []
        for group_products in category_groups.values():
            if len(group_products) > n:
                limited_products.extend(group_products[:n])
            else:
                limited_products.extend(group_products)

        data["products"] = limited_products
        return len(limited_products)
    else:
        # Fallback for old nested structure (shouldn't be used anymore)
        total = 0
        for tc in data.get("categories", []):
            for lc in tc.get("low_level_categories", []):
                ps = lc.get("products", [])
                if len(ps) > n:
                    lc["products"] = ps[:n]
                total += len(lc.get("products", []))
                for sc in lc.get("subcategories", []):
                    ps = sc.get("products", [])
                    if len(ps) > n:
                        sc["products"] = ps[:n]
                    total += len(sc.get("products", []))
        return total


async def test_site(site, categories_limit=3, products_per_category=5, detail_workers=16, category_probe_limit=30):
    logger = setup_logger(site)
    print_header(f"🧪 TESTING: {site.upper()}")
    print_stat("Categories", categories_limit, Colors.YELLOW)
    print_stat("Products/cat", products_per_category, Colors.YELLOW)

    t0 = time.time()
    scraper = None
    result = {
        "site": site,
        "success": False,
        "status": "failed",
        "categories_scraped": 0,
        "products_found": 0,
        "details_scraped": 0,
        "duration": 0,
        "errors": [],
    }

    try:
        scraper = get_scraper(site, logger)
        scraper._current_data_dir = DATA_DIR / site / get_date_folder()
        scraper._current_data_dir.mkdir(parents=True, exist_ok=True)

        print_step(1, "Downloading frontpage")
        await scraper.download_frontpage()

        print_step(2, "Extracting categories")
        cat_path = scraper.extract_categories()
        cat_data = load_json(cat_path)
        raw_categories = scraper.build_scrape_queue(cat_data)
        result["categories_scraped"] = len(raw_categories)
        print_info(f"Discovered categories: {len(raw_categories)}")

        filtered = filter_live_categories(raw_categories)
        kept_categories = filtered["kept"]
        filtered_out = filtered["filtered"]
        print_info(f"Categories filtered out: {len(filtered_out)}")
        logger.info(f"[live.categories.discovered] total={len(raw_categories)}")
        for row in filtered["all_rows"]:
            logger.info(
                f"[live.category.classified] classification={row['classification']} name={row['name']} url={row['url']}"
            )

        probe_limit = resolve_probe_limit(categories_limit, category_probe_limit)
        print_info(f"Probe limit: {probe_limit}")
        selection = await select_live_product_category(
            scraper=scraper,
            site_name=site,
            candidates=filtered["candidates"],
            probe_limit=probe_limit,
            max_depth=2,
            logger=logger,
        )
        selected = selection.get("selected")
        print_info(f"Categories probed: {selection.get('probed', 0)}")

        if not selected:
            reason = "No product-bearing category found within probe limit"
            result["errors"].append(reason)
            result["status"] = "degraded"
            print_error(reason)
            return result

        print_success(f"Selected category: {selected.url}")
        logger.info(
            f"[live.selection.summary] discovered={len(raw_categories)} filtered={len(filtered_out)} "
            f"probed={selection.get('probed', 0)} selected={selected.url}"
        )

        print_step(3, "Scraping products (selected category)")
        if is_fast_scraper(scraper):
            products_list = await scraper.scrape_all_pages(selected.url, limit=products_per_category)
        else:
            # BaseScraper (Playwright) — use single-category playwright helper
            fake_stats = ScrapeStats()
            from playwright.async_api import async_playwright as _apw
            async with _apw() as _p:
                _browser = await _p.chromium.launch(headless=True, args=playwright_launch_args())
                _ctx = await _browser.new_context()
                try:
                    products_list = await scraper.scrape_category_all_pages(_ctx, selected, fake_stats)
                    products_list = products_list[:products_per_category]
                finally:
                    await _ctx.close()
                    await _browser.close()
        for p in products_list:
            p["shop"] = site
            p["top_category"] = selected.parent_names[0] if selected.parent_names else selected.name
            p["low_category"] = selected.name if selected.level != "top" else None
            p["subcategory"] = selected.name if selected.level == "subcategory" else None

        save_json(products_list, scraper.data_dir / "products.json", logger)
        result["products_found"] = len(products_list)
        print_info(f"Products found in selected category: {result['products_found']}")

        print_step(4, "Scraping details (live smoke)")
        items = []
        # For flat structure, collect URLs with category information
        for p in products_list:
            if p.get("url"):
                items.append({
                    "id": p.get("id"),
                    "url": p["url"],
                    "top_category": p.get("top_category"),
                    "low_category": p.get("low_category"),
                    "subcategory": p.get("subcategory")
                })

        if items:
            # Save first live detail HTML evidence if fetch succeeds.
            first_url = items[0]["url"]
            first_html = await _fetch_detail_html_snapshot(scraper, first_url)
            if first_html:
                save_text_atomic(first_html, scraper.html_dir / "live_product_detail.html")

            pbar = tqdm(total=len(items), desc=f"  {Colors.MAGENTA}Details{Colors.RESET}", bar_format="{desc}:   {percentage:3.0f}%|{bar:30}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]", ncols=80)
            try:
                if is_fast_scraper(scraper):
                    det_res = await scrape_details_fast(scraper, items, detail_workers, pbar)
                else:
                    det_res = await scrape_details_playwright(scraper, items, detail_workers, pbar)
            finally:
                pbar.close()

            # Create separate detailed products data structure
            detailed_products = []
            ok = 0

            # Track specific failures
            failed_details = []
            for item in items:
                url = item["url"]
                r = det_res.get(url)
                if not r or not r.get("success"):
                    error = r.get("error", "Unknown error") if r else "No result"
                    failed_details.append({"id": item.get("id"), "url": url, "error": error})
                    continue

                det = r["details"]

                # Ensure available is always boolean or null, never inconsistent
                available_value = det.get("available")
                if available_value is None and det.get("availability"):
                    avail_text = str(det.get("availability", "")).lower()
                    if "en stock" in avail_text or "disponible" in avail_text:
                        available_value = True
                    elif "epuisé" in avail_text or "rupture" in avail_text or "indisponible" in avail_text:
                        available_value = False

                # Build complete detailed product record with category information
                detailed_product = {
                    "url": url,
                    "shop": site,
                    "scraped_at": datetime.now().isoformat(),
                    "top_category": item.get("top_category"),
                    "low_category": item.get("low_category"),
                    "subcategory": item.get("subcategory"),
                    **det,  # Include all fields from detailed scraping
                    "available": available_value  # Override with processed value
                }

                detailed_products.append(detailed_product)
                ok += 1

            result["details_scraped"] = ok
            logger.info(
                f"[live.detail.summary] attempted={len(items)} success={ok} failed={len(items)-ok}"
            )

            # Save detailed products as direct list (JSON Array)
            save_json(detailed_products, scraper.data_dir / "products_detailed.json", logger)
            
            # Save summary separately
            det_summary = {
                "site": site,
                "shop": site,
                "scraped_at": datetime.now().isoformat(),
                "total_products": len(detailed_products),
                "scrape_stats": {
                    "total_attempted": len(items),
                    "successful": ok,
                    "failed": len(items) - ok
                },
                "failed_details": failed_details,
            }
            save_json(det_summary, scraper.data_dir / "products_detailed_summary.json", logger)
            
            print_success(f"Scraped {ok}/{len(items)} details")
        else:
            logger.warning("[live.detail.summary] attempted=0 success=0 failed=0 reason=no_products")

        result["success"] = True
        result["status"] = "ok" if result["products_found"] > 0 else "degraded"
    except Exception as e:
        print_error(str(e))
        result["errors"].append(str(e))
    finally:
        result["duration"] = time.time() - t0
        if scraper is not None:
            save_run_summary(scraper, "test_summary.json", result, logger)
            await close_scraper_resources(scraper, logger)

    print_header(f"{'✅' if result['success'] else '❌'} RESULT: {site.upper()}", "-")
    print_stat("Duration", format_duration(result['duration']), Colors.CYAN)
    print_stat("Categories", result['categories_scraped'], Colors.WHITE)
    print_stat("Products", result['products_found'], Colors.GREEN)
    print_stat("Details", result['details_scraped'], Colors.MAGENTA)
    print()
    return result


async def test_all_sites(categories_limit=3, products_per_category=5, detail_workers=16, category_probe_limit=30):
    sites = list_available_sites()
    print_header(f"🧪 TESTING ALL ({len(sites)} sites)")
    results = {}
    for i, site in enumerate(sites, 1):
        print(f"\n{Colors.DIM}[{i}/{len(sites)}]{Colors.RESET}")
        try:
            results[site] = await test_site(site, categories_limit, products_per_category, detail_workers, category_probe_limit)
            await asyncio.sleep(2)
        except Exception as e:
            print_error(f"Failed: {e}")
            results[site] = {"success": False, "error": str(e)}
    
    print_header("📊 SUMMARY")
    for site, r in results.items():
        st = f"{Colors.GREEN}✅{Colors.RESET}" if r.get("success") else f"{Colors.RED}❌{Colors.RESET}"
        print(f"  {st} {site.upper():12} {r.get('products_found',0):5} prods, {r.get('details_scraped',0):5} dets")
    print()
    return results


async def main():
    parser = argparse.ArgumentParser(description="Fast E-commerce Scraper")
    sub = parser.add_subparsers(dest="cmd")

    tp = sub.add_parser("test", help="Test site with limits")
    tp.add_argument("--site", help="Site to test")
    tp.add_argument("--all-sites", action="store_true", help="Test all")
    tp.add_argument("--categories", type=int, default=3)
    tp.add_argument("--products", type=int, default=5)
    tp.add_argument("--detail-workers", type=int, default=16)
    tp.add_argument("--category-probe-limit", type=int, default=30)
    tp.add_argument("--export", action="store_true", help="Export to DB after scrape")

    fp = sub.add_parser("full", help="Full scrape")
    fp.add_argument("--site", required=True)
    fp.add_argument("--workers", type=int, default=16)
    fp.add_argument("--detail-workers", type=int, default=64)
    fp.add_argument("--no-details", action="store_true")
    fp.add_argument("--export", action="store_true", help="Export to DB after scrape")

    sub.add_parser("list", help="List sites")
    
    args = parser.parse_args()
    
    if args.cmd == "test":
        if args.all_sites:
            await test_all_sites(args.categories, args.products, args.detail_workers, args.category_probe_limit)
        elif args.site:
            await test_site(args.site, args.categories, args.products, args.detail_workers, args.category_probe_limit)
        else:
            parser.error("--site or --all-sites required")
            
        if args.export:
            from export_db import export_latest_run
            export_latest_run()
            
    elif args.cmd == "full":
        await run_full_scrape(args.site, args.workers, args.detail_workers, scrape_details=not args.no_details)
        if args.export:
            from export_db import export_latest_run
            export_latest_run()
            
    elif args.cmd == "list":
        print_header("📋 AVAILABLE SITES")
        for s in list_available_sites():
            print(f"  {Colors.GREEN}•{Colors.RESET} {s}")
        print()
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
