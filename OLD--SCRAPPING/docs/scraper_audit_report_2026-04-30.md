# Scraper Audit Report (2026-04-30)

## Scope Executed
- End-to-end audit on scraping pipeline (`scrape.py`, `scraper/base.py`, and site scrapers).
- Live per-shop test runs executed for all 21 supported shops with:
  - `python scrape.py test --site <shop> --categories 1 --products 1 --detail-workers 2`
- Output schema validation executed with:
  - `python scripts/validate_schema.py`

## Shop Status Matrix (After Second Pass)
- working: `mytek`, `tunisianet`, `darty`, `jumbo`, `graiet`, `zoom`, `allani`, `parashop`, `pharmacieplus`, `pharmashop`, `sbs`, `skymill`, `parafendri`
- degraded (categories discovered, sampled category returned 0 products): `expert_gaming`, `scoop`, `wiki`, `spacenet`, `batam`, `geant`, `mapara`
- category discovery is fixed (no longer 0 categories) for: `spacenet`, `batam`, `geant`, `mapara`
- externally unreachable/blocked: `technopro` (connection refused consistently from this environment)

## Third pass: HTML fixture selector refit
- Fixture files used as source of truth:
  - `data/expert_gaming/html/frontpage.html`
  - `data/scoop/html/frontpage.html`
  - `data/wiki/html/frontpage.html`
  - `data/spacenet/html/frontpage.html`
  - `data/batam/html/frontpage.html`
  - `data/geant/html/frontpage.html`
  - `data/mapara/html/frontpage.html`
- `expert_gaming`
  - Product: `section.product-category.product` + name fallback from `img[alt]`
  - Categories: `ul#menu-notre-boutique` and nested `ul.sub-menu a[href]`
  - Fixture result: product extraction passes on fixture
  - Live result: degraded (sampled category returns 0 products)
- `scoop`
  - Product: `article.product-miniature.js-product-miniature` with fixture fallback `div.tvproduct-wrapper`
  - Categories: `div#tvdesktop-megamenu ul.menu-content > li.level-1 > a`
  - Fixture result: product extraction passes on fixture
  - Live result: degraded (sampled category returns 0 products)
- `wiki`
  - Product: `div.product-card--grid` with fixture fallback `div.product-card`
  - URL/name fallback: `figure.product-card__image a` and image `alt`
  - Categories: `nav.brxe-nav-nested.desktop-nav ul.brx-nav-nested-items > li.brxe-dropdown`
  - Fixture result: product extraction passes on fixture
  - Live result: degraded (sampled category returns 0 products)
- `spacenet`
  - Product: `.product-miniature.js-product-miniature[data-id-product]`
  - Price: `.product-price-and-shipping .price` with robust locale-safe parsing
  - Categories: `#sp-vermegamenu > ul`
  - Fixture result: product extraction passes on fixture
  - Live result: degraded (sampled category returns 0 products)
- `batam`
  - Product: `form.product_addtocart_form`
  - Price: `[data-price-type='finalPrice'] .price`
  - Categories: `li.level-0.parent-ul-list` with nested `ul.level-1` / `ul.level-2`
  - Fixture result: product extraction passes on fixture
  - Live result: degraded (sampled category returns 0 products)
- `geant`
  - Categories only in fixture: `ul.menu-content.top-menu`, `a.category_header`
  - Product cards in fixture: not present
  - Fixture result: category extraction passes; product extraction expected empty
  - Live result: degraded (sampled category returns 0 products)
- `mapara`
  - Product: config selector fallback to fixture-backed `div.product-small.box`
  - Price: `span.price span.woocommerce-Price-amount bdi` (including sale/non-sale fallback)
  - Image: fallback `div.box-image div.image-cover picture img`
  - Categories: `ul.nav.header-nav.header-bottom-nav > li.menu-item.has-dropdown`
  - Fixture result: product extraction passes on fixture
  - Live result: degraded (sampled category returns 0 products)

## Selector/Parsing Changes Made
- `scraper/sites/spacenet.py`
  - Added robust category fallback path when menu container is missing.
  - Added `_extract_categories_fallback()` using category-like link heuristics and exclusion filters.
- `scraper/sites/batam.py`
  - Added fallback category extraction when legacy `li.level-0.parent-ul-list` structure is absent.
  - Added `_extract_categories_fallback()` to build low-level categories from stable internal links with account/cart exclusions.
- `scraper/sites/geant.py`
  - Added `_extract_categories_fallback()` for changed menu layouts.
  - Added Playwright-based frontpage rendering to recover dynamic category menus.
- `scraper/sites/mapara.py`
  - Added `_extract_categories_fallback()` for changed menu layouts.
  - Added Playwright-based frontpage rendering to recover dynamic category menus.
- `scraper/sites/expert_gaming.py`
  - Added JSON-LD product fallback parser in listing extraction.
  - Added category-link enrichment when low-level menu groups are missing.
- `scraper/sites/scoop.py`
  - Added JSON-LD product fallback parser in listing extraction.
  - Added low-level category synthesis fallback for sparse/flat menu snapshots.
- `scraper/sites/wiki.py`
  - Added JSON-LD product fallback parser in listing extraction.
  - Strengthened Playwright page fetch wait strategy (`networkidle` + product selector wait).
- `scraper/sites/parafendri.py`
  - Added broad listing fallback when strict product card selectors miss.

## Robustness and Error Handling Hardening
- `scraper/base.py`
  - Added `classify_http_status()` retry policy utility.
  - Hardened `fetch_html()` with invalid URL guards and status-code-based retry decisions.
- `scrape.py`
  - Added safe console output encoding fallback to prevent Windows cp1252 crashes.
  - Added non-fatal structured logger events for phase success/failure and partial failures.
  - Removed duplicate detail success print.

## Validation Evidence
- Tests added and passing:
  - `tests/test_cli_output_safety.py`
  - `tests/test_http_resilience.py`
  - `tests/test_category_fallbacks.py`
- Second-pass tests added and passing:
  - `tests/test_second_pass_fallbacks.py`
- Third-pass fixture tests added and passing:
  - `tests/test_third_pass_fixture_refit.py`
- Existing tests still passing:
  - `tests/test_sku_matching.py`
- Full run:
  - `python -m pytest tests/test_cli_output_safety.py tests/test_http_resilience.py tests/test_category_fallbacks.py tests/test_sku_matching.py -q`
  - Result: 47 passed.
- Second-pass focused run:
  - `python -m pytest tests/test_second_pass_fallbacks.py -q`
  - Result: 5 passed.
- Third-pass fixture run:
  - `python -m pytest tests/test_third_pass_fixture_refit.py -q`
  - Result: 20 passed.
- Schema check:
  - Output file: `data/schema_validation_report.json`

## Commands Run (Second Pass)
- `python -m pytest tests/test_second_pass_fallbacks.py -q` → 5 passed
- `python -m pytest tests/test_cli_output_safety.py tests/test_http_resilience.py tests/test_category_fallbacks.py tests/test_sku_matching.py -q` → 47 passed
- `python scrape.py test --site expert_gaming --categories 1 --products 1` → 0 products
- `python scrape.py test --site parafendri --categories 1 --products 1` → products extracted (40 on sampled category)
- `python scrape.py test --site scoop --categories 1 --products 1` → 0 products
- `python scrape.py test --site wiki --categories 1 --products 1` → 0 products
- `python scrape.py test --site spacenet --categories 1 --products 1` → 0 categories
- `python scrape.py test --site batam --categories 1 --products 1` → 0 categories
- `python scrape.py test --site geant --categories 1 --products 1` → 0 categories
- `python scrape.py test --site mapara --categories 1 --products 1` → 0 categories
- `python scrape.py test --site technopro --categories 1 --products 1` → connection failures
- `python -c "import requests; ... requests.get('https://www.technopro-online.com') ..."` → WinError 10061 on 3/3 attempts
- `python scripts/validate_schema.py` → saved `data/schema_validation_report.json`
- Re-test after deeper category fixes:
  - `spacenet` → categories recovered (`14 top / 90 low / 523 sub`), sampled category still 0 products
  - `batam` → categories recovered (`12 top / 63 low / 124 sub`), sampled category still 0 products
  - `geant` → categories recovered (`14 top / 93 low`), sampled category still 0 products
  - `mapara` → categories recovered (`9 top / 90 low`), sampled category still 0 products
- Final test run:
  - `python -m pytest tests/test_cli_output_safety.py tests/test_http_resilience.py tests/test_category_fallbacks.py tests/test_second_pass_fallbacks.py tests/test_sku_matching.py -q` → 52 passed
- Third-pass live smoke run:
  - `python scrape.py test --site expert_gaming --categories 1 --products 1` → 0 products
  - `python scrape.py test --site scoop --categories 1 --products 1` → 0 products
  - `python scrape.py test --site wiki --categories 1 --products 1` → 0 products
  - `python scrape.py test --site spacenet --categories 1 --products 1` → 0 products
  - `python scrape.py test --site batam --categories 1 --products 1` → 0 products
  - `python scrape.py test --site geant --categories 1 --products 1` → 0 products
  - `python scrape.py test --site mapara --categories 1 --products 1` → 0 products

## Remaining Risks / Manual Follow-up
- Some shops still produce 0 categories or 0 products in sampled runs and require deeper selector refits:
  - `expert_gaming`, `scoop`, `wiki`, `spacenet`, `batam`, `geant`, `mapara`
- `technopro` is externally unreachable from this environment (TCP connection refused on repeated direct requests and scraper runs).
- Some runs show Windows asyncio transport cleanup warnings on process exit (non-fatal, noisy logs).

## Recommended Next Improvements
1. Add per-shop health thresholds (minimum categories/products) to auto-mark degraded shops.
2. Add structured machine-readable run summary JSON from `scrape.py` for every test/full run.
3. Add shared selector helper utilities for category extraction fallback and consistent product-card detection.
4. Add targeted shop fixtures (HTML snapshots) for failing shops and lock selectors with regression tests.

## Next Phase: Full Selector and Extraction Refit (listing + pagination + detail)

### Additional HTML evidence captured
- `data/expert_gaming/html/listing_sample_1.html`
- `data/expert_gaming/html/detail_sample_1.html`
- `data/scoop/html/listing_sample_1.html`
- `data/scoop/html/detail_sample_1.html`
- `data/wiki/html/listing_sample_1.html`
- `data/wiki/html/detail_sample_1.html`
- `data/spacenet/html/listing_sample_1.html`
- `data/spacenet/html/detail_sample_1.html`
- `data/batam/html/listing_sample_1.html`
- `data/batam/html/detail_sample_1.html`
- `data/geant/html/listing_sample_1.html`
- `data/geant/html/detail_sample_1.html`
- `data/mapara/html/listing_sample_1.html`
- `data/mapara/html/detail_sample_1.html`

### Refit updates applied
- `scraper/sites/expert_gaming.py`
  - Category fallback broadened to include stable slug category URLs (not only `/product-category/`) with non-category exclusions.
- `scraper/sites/batam.py`
  - Pagination output normalized to use `total_pages` instead of `max_page`.
- `scraper/sites/geant.py`
  - Replaced detail stub with real detail extraction:
    - title via `h1.h1[itemprop='name'], h1.h1.product-head1`
    - sku/reference via `.product-reference span`
    - price via `span[itemprop='price'][content]` fallback to visible price text
    - availability via `#product-availability` + schema availability fallback
    - description via `#description .product-description`
    - images via `.product-images img, .js-modal-product-images img, .thumb-container img`
    - specs via `.product-features dt/dd`

### New test suite for listing/pagination/detail
- Added: `tests/test_selector_refit_listing_pagination_detail.py`
  - validates listing product extraction on listing samples
  - validates pagination parsing shape
  - validates detail extraction on detail samples
  - validates missing optional field safety

### Validation commands and results (next phase)
- `python -m pytest tests/test_selector_refit_listing_pagination_detail.py -q` → `28 passed`
- `python -m pytest tests/test_cli_output_safety.py tests/test_http_resilience.py tests/test_category_fallbacks.py tests/test_second_pass_fallbacks.py tests/test_third_pass_fixture_refit.py tests/test_sku_matching.py -q` → `72 passed`
- `python scripts/validate_schema.py` → `saved data/schema_validation_report.json`

### Live smoke status after this refit phase
- `expert_gaming`: categories discovered, sampled category still `0 products`
- `scoop`: categories discovered, sampled category still `0 products`
- `wiki`: categories discovered, sampled category still `0 products`
- `spacenet`: categories discovered, sampled category still `0 products`
- `batam`: categories discovered, sampled category still `0 products`
- `geant`: categories discovered, sampled category still `0 products`
- `mapara`: categories discovered, sampled category still `0 products`

These live sampled runs remain degraded even though listing/detail fixture parsing is now validated from saved evidence.

## Fifth pass: live product-bearing category selection and detail smoke validation

### Runtime changes
- `scrape.py`
  - Added `LIVE_CATEGORY_EXCLUDE_PATTERNS` and reusable helpers:
    - `normalize_category_url()`
    - `classify_category_url()`
    - `filter_live_categories()`
    - `select_live_product_category()`
  - Reworked `test_site()` flow:
    - no longer scrapes first discovered category blindly
    - discovers categories, classifies URLs, filters invalid/navigation/duplicates
    - probes filtered candidates (bounded by probe limit) via live HTML + `extract_products_from_html`
    - selects first product-bearing category or reports explicit degraded reason
    - attempts detail phase when listing yields products and logs detail summary
    - saves `products.json` / `products_detailed.json` summaries in run directory

### New tests
- Added `tests/test_live_category_selection.py`
  - invalid URL filtering
  - duplicate normalization/dedupe
  - skip first empty category
  - select first product-bearing category
  - probe limit stop behavior
  - resilience when some probes fail
  - absolute/stable selected URL behavior
- Validation: `python -m pytest tests/test_live_category_selection.py -q` → `6 passed`

### Fifth-pass required validation commands
- `python -m pytest tests/test_live_category_selection.py -q` → `6 passed`
- `python -m pytest tests/test_selector_refit_listing_pagination_detail.py -q` → `28 passed`
- `python -m pytest tests/test_cli_output_safety.py tests/test_http_resilience.py tests/test_category_fallbacks.py tests/test_second_pass_fallbacks.py tests/test_third_pass_fixture_refit.py tests/test_sku_matching.py -q` → `72 passed`
- `python scripts/validate_schema.py` → `saved data/schema_validation_report.json`

### Live smoke (fifth pass)
- `python scrape.py test --site expert_gaming --categories 1 --products 1`
  - discovered `81`, filtered `17`, probed `5`, selected `none`, listing `0`, detail `not attempted`
- `python scrape.py test --site scoop --categories 1 --products 1`
  - discovered `43`, filtered `3`, probed `5`, selected `none`, listing `0`, detail `not attempted`
- `python scrape.py test --site wiki --categories 1 --products 1`
  - discovered `349`, filtered `196`, probed `5`, selected `none`, listing `0`, detail `not attempted`
- `python scrape.py test --site spacenet --categories 1 --products 1`
  - discovered `531`, filtered `20`, probed `5`, selected `none`, listing `0`, detail `not attempted`
- `python scrape.py test --site batam --categories 1 --products 1`
  - discovered `159`, filtered `1`, probed `5`, selected `none`, listing `0`, detail `not attempted`
- `python scrape.py test --site geant --categories 1 --products 1`
  - discovered `93`, filtered `1`, probed `5`, selected `none`, listing `0`, detail `not attempted`
- `python scrape.py test --site mapara --categories 1 --products 1`
  - discovered `90`, filtered `2`, probed `5`, selected `none`, listing `0`, detail `not attempted`

### Saved live HTML evidence
- No `live_selected_category.html` or `live_product_detail.html` files were produced in this run because no shop reached a product-bearing category within probe limit.
- No `live_failed_category_<n>.html` snapshots were produced for the sampled probes where fetch returned empty/failed responses before parseable HTML could be persisted.

### Remaining blockers after fifth pass
- `expert_gaming`, `scoop`, `wiki`, `spacenet`, `batam`, `geant`, `mapara`
  - live category probing window (`5` probes in current command shape) did not encounter a product-bearing category despite fixture-proven selectors.
  - root issue is now narrowed to live category candidate ordering/content and/or probe depth, not listing/detail parser contracts.

## Sixth pass: ranked deep category probe and live detail reachability

### Runtime changes
- `scrape.py`
  - Added ranking and deep-probe helpers:
    - `rank_category_candidate()`
    - `resolve_probe_limit()`
    - `_extract_subcategory_links()`
    - `_make_category_like()`
    - `save_candidate_audit()`
  - Extended `filter_live_categories()` to emit structured candidate records.
  - Reworked `select_live_product_category()` to:
    - rank candidates before probing,
    - support bounded subcategory traversal (`max_depth`),
    - persist candidate lifecycle (`probe_status`, `product_count`, `selected`),
    - write per-shop audit JSON to `data/<shop>/audit/category_candidates.json`.
  - Added CLI option `--category-probe-limit` (default `30`) and decoupled probe depth from `--categories`.

### Sixth-pass tests
- Added `tests/test_live_category_ranking_and_deep_probe.py` covering:
  - ranking behavior,
  - probe-limit decoupling,
  - failed/selected snapshot behavior,
  - bounded traversal and audit JSON schema.
- Validation:
  - `python -m pytest tests/test_live_category_selection.py tests/test_live_category_ranking_and_deep_probe.py -q` → `11 passed`

### Sixth-pass required validation commands
- `python -m pytest tests/test_live_category_selection.py tests/test_live_category_ranking_and_deep_probe.py -q` → `11 passed`
- `python -m pytest tests/test_selector_refit_listing_pagination_detail.py -q` → `28 passed`
- `python -m pytest tests/test_cli_output_safety.py tests/test_http_resilience.py tests/test_category_fallbacks.py tests/test_second_pass_fallbacks.py tests/test_third_pass_fixture_refit.py tests/test_sku_matching.py -q` → `72 passed`
- `python scripts/validate_schema.py` → `saved data/schema_validation_report.json`

### Per-shop live smoke with deep probe (`--category-probe-limit 30`)
- `expert_gaming`
  - discovered `81`, filtered `17`, probed `30`, selected `none`, listing `0`, detail `not attempted`
- `scoop`
  - discovered `7`, filtered `0`, probed `7`, selected `none`, listing `0`, detail `not attempted`
- `wiki`
  - run repeatedly stalls during frontpage/download phase after selector wait warning; no complete probe summary captured in this environment
- `spacenet`
  - discovered `531`, filtered `20`, probed `30`, selected `none`, listing `0`, detail `not attempted`
- `batam`
  - discovered `159`, filtered `1`, probed `30`, selected `none`, listing `0`, detail `not attempted`
- `geant`
  - discovered `93`, filtered `1`, probed `30`, selected `none`, listing `0`, detail `not attempted`
- `mapara`
  - discovered `90`, filtered `2`, probed `30`, selected `none`, listing `0`, detail `not attempted`

### Sixth-pass evidence artifacts
- Candidate audits produced for all target shops:
  - `data/expert_gaming/audit/category_candidates.json`
  - `data/scoop/audit/category_candidates.json`
  - `data/wiki/audit/category_candidates.json`
  - `data/spacenet/audit/category_candidates.json`
  - `data/batam/audit/category_candidates.json`
  - `data/geant/audit/category_candidates.json`
  - `data/mapara/audit/category_candidates.json`
- `live_failed_category_<n>.html`, `live_selected_category.html`, and `live_product_detail.html` were not produced in this run because no product-bearing category was reached in completed shop runs.

### Remaining blockers after sixth pass
- `expert_gaming`, `scoop`, `spacenet`, `batam`, `geant`, `mapara`
  - deeper ranked probing (up to 30) still did not locate a product-bearing category in live responses.
- `wiki`
  - live run stability blocker in frontpage/download phase prevented complete deep-probe execution and therefore listing/detail verification.

## Root-cause phase: evidence-driven live recovery

### Root cause found from candidate + probe evidence
- The new `probe_summary.json` and `live_probe_*` evidence showed a consistent pattern on 6 shops:
  - HTTP status `200`
  - probe classification `empty_category`
  - probe HTML snapshots contained compressed/garbled payloads instead of parseable markup
- This was a transport/content-decoding issue in fast HTTP path rather than selector quality:
  - category URLs were valid
  - ranking/probing reached real category URLs
  - parser operated on unreadable payload bytes and returned 0 products
- `wiki` remained different: probe summary recorded `403` fetch failures across candidates (access control/challenge behavior).

### Fixes applied
- `scraper/base.py`
  - Forced fast scraper request header:
    - `Accept-Encoding: gzip, deflate`
  - Added metadata-returning fetch path:
    - `fetch_html_with_meta()` (status code, final URL, content type/encoding, error)
  - Kept `fetch_html()` as compatibility wrapper.
- `scrape.py`
  - Added robust probe evidence pipeline:
    - always writes `data/<shop>/html/live_probe_<n>_<status>.html` for successful probe pages with non-selected outcomes
    - still writes first five `live_failed_category_<n>.html`
  - Added `data/<shop>/audit/probe_summary.json` with:
    - probed URL
    - depth
    - status code
    - final URL
    - HTML file path
    - detected product/subcategory counts
    - selectors tested
    - blocker/JS-render signals
    - classification

### Re-run commands (root-cause phase)
- `python scrape.py test --site expert_gaming --categories 1 --products 1 --category-probe-limit 80`
- `python scrape.py test --site scoop --categories 1 --products 1 --category-probe-limit 80`
- `python scrape.py test --site wiki --categories 1 --products 1 --category-probe-limit 80`
- `python scrape.py test --site spacenet --categories 1 --products 1 --category-probe-limit 80`
- `python scrape.py test --site batam --categories 1 --products 1 --category-probe-limit 80`
- `python scrape.py test --site geant --categories 1 --products 1 --category-probe-limit 80`
- `python scrape.py test --site mapara --categories 1 --products 1 --category-probe-limit 80`

### Live outcomes after root-cause fix
- `expert_gaming`: selected on first probe; listing `1`; detail `1` (live-fixed)
- `scoop`: selected on first probe; listing `1`; detail `1` (live-fixed)
- `spacenet`: selected on first probe; listing `1`; detail `1` (live-fixed)
- `batam`: selected on first probe; listing `1`; detail `1` (live-fixed)
- `geant`: selected on first probe; listing `1`; detail `1` (live-fixed)
- `mapara`: selected on first probe; listing `1`; detail `1` (live-fixed)
- `wiki`: discovery works but candidate probes return `403 Forbidden`; no selected category; listing/detail not reached (external blocking/degraded)

### Evidence files now present
- Candidate and probe audits:
  - `data/<shop>/audit/category_candidates.json`
  - `data/<shop>/audit/probe_summary.json`
- Probe HTML snapshots:
  - `data/<shop>/html/live_probe_<n>_<status>.html`
  - `data/<shop>/html/live_failed_category_<n>.html` (first five failures)
- For live-fixed shops:
  - `data/<shop>/html/live_selected_category.html`
  - `data/<shop>/html/live_product_detail.html`
