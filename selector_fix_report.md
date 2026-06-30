# Selector Fix Report — 56-shop audit

## Summary

| Category | Count | Action |
|---|---|---|
| CLEAN (re-scrape only) | ~22 | Re-scrape immediately |
| BROKEN on matching-critical fields | ~25 | Selector fix needed before re-scrape |
| LIVE PROBE FAILED | 1 (taktek) | Cert expired — skip or fetch via http |
| SCRAPER OUTPUT EMPTY | 1 (techgate) | Investigate per-product scraper code |

Matching-critical fields = **title, price, sku, availability** (description is nice-to-have but not used by matching).

---

## CLEAN — re-scrape only (no selector edits)

These shops produced complete data in their last scrape. They just need fresh data.

`spacenet, technopro, jumbo, kamounhome, maalejaudio, zoom, bill, allani, krichen, agora, itechstore, expert_gaming, batam, tokyo_store, mytek, psstore, infotec, mbm, electrohadjkacem, gamershop, carthagoinformatique, try_and_buy`

## MINOR (only description/availability weak — still fine for matching)

`tunisianet` (desc 100% null but title/price/sku OK), `bstech` (desc 22%), `electrobennjima` (desc 100%), `taktek` (avail 36%), `acspace` (avail 36%), `alarabia` (desc 37%), `sangour` (desc 90%)

## BROKEN — recommended selector fixes (matching-critical fields only)

### bestbuytunisie  (8,695 products, 77% title/price/sku missing)
Live probe shows WooCommerce. Fix `configs/sites/bestbuytunisie.yaml` -> `selectors.product_page`:
- title: `h1.product_title`
- price: `.price .amount`
- sku: `.sku`
- availability: `.stock`

### informatica  (6,518, 98% missing)
WooCommerce. Same fix pattern:
- title: `h1.product_title`
- price: `.price .amount`
- sku: `.sku`
- availability: `.stock`

### electrochaabani  (7,105, 99% title/desc missing)
Custom platform — current selectors don't work for title/sku/desc.
Heuristic probe also failed. Needs manual HTML inspection at `data/_audit/electrochaabani/page.html`. The page may need an explicit XPath or attribute-based extraction.

### emh  (1,118, 60% missing)
Probe returned only the site name as `h1`. Looks like a single-page wrapper — product detail might be in a frame or JSON-LD. Needs manual inspection of saved HTML.

### sbs  (4,393, 22% all fields missing)
PrestaShop:
- title: `h1[itemprop="name"]`
- price: `[itemprop="price"]`
- sku: `[itemprop="sku"]`
- availability: `#product-availability`

### jmb  (2,236, 80% missing)
WooCommerce:
- title: `h1.product_title`
- price: `.product-price .price`
- sku: `.sku`
- availability: `.in-stock`

### scoop  (560, 28% missing)
PrestaShop:
- title: `h1[itemprop="name"]`
- price: `[itemprop="price"]`
- sku: `[itemprop="sku"]`
- availability: `#product-availability`

### wiki  (3,604, 29-67% missing)
WooCommerce:
- title: `h1`
- price: `.price .amount`
- sku: `.sku`
- availability: `.stock`

### benzarti-electromenager  (71, 87%+ missing; price 100%)
WooCommerce-ish but probe found price=0. Currently OUT_OF_STOCK across whole catalogue or selector wrong. Manual inspection needed.

### graiet  (537, 85% missing)
Looks PrestaShop:
- title: `h1`
- price: `.price`
- sku: `[itemprop="sku"]`
- availability: `.stock`

### imag  (1,580, 58% price / 100% sku missing)
WooCommerce:
- title: `h1.product_title`
- price: `.price .amount`
- sku: `.sku`
- availability: `.stock`

### megapc  (214, 100% sku missing)
Title found by `h1` only. Custom platform. Manual inspection needed for sku.

### topbureau  (233, 45% price / 69% desc missing)
OpenCart:
- price: `[itemprop="price"]`
- description: `[itemprop="description"]`

### dokani  (1,811, 100% sku/desc/avail missing)
Custom; probe found only title + price. Needs manual HTML inspection.

### darty  (2,744, 73% sku missing)
PrestaShop:
- title: `h1[itemprop="name"]`
- price: `[itemprop="price"]`
- description: `#description`

### qsnet  (1,497, 100% sku missing)
WooCommerce:
- title: `h1.product_title`
- price: `.price .amount`
- sku: `.product-sku`  (not the bare `.sku`)
- availability: `.stock`

### yatoo  (5,433, 96% avail / 26% sku missing)
Probe returned only title. Custom Tunisian site — manual inspection.

### chaktech  (1,832, 57% sku missing)
WooCommerce:
- title: `h1`
- price: `.price .amount`
- sku: `.sku`
- availability: `.stock`

### ispace  (107, 55% desc missing)
WooCommerce:
- title: `h1.product_title`
- price: `.price .amount`
- description: `.woocommerce-product-details__short-description`
- availability: `.stock`

### skymill  (196, 24% price/avail)
Page returned only title `Full Setup` — sample URL was a category, not a product. Re-pick sample URL needed.

### techland  (384, 31% sku missing)
Probe returned only title. WooCommerce likely:
- sku: `.sku`

### sigshop  (98, 100% avail missing)
WooCommerce:
- availability: `.stock`

### techgate  (0 products in scrape — empty output)
Scraper ran but produced nothing. Bug in scraper code, not selector. Needs separate investigation.

### koktahome  (11 products only — likely category filtering bug)
WooCommerce:
- title: `h1.product_title`
- price: `.price .amount`
- sku: `.sku`
- availability: `.stock`

### tunewtec  (8,128, 67% availability)
WooCommerce:
- availability: `.availability`

### affariyet  (7,330, 88% desc / 83% avail)
PrestaShop:
- description: (use product description div, not the configured one)
- availability: `.product-availability`

---

## Recommended next steps (in order)

1. **Now (automated):** Re-scrape the 22 CLEAN shops in 3-shop batches. This refreshes data without risk.
2. **You review (≈15 min):** Skim this file, confirm the WooCommerce/PrestaShop fix groups. I'll apply them in one pass.
3. **Manual deep-dive (≈1-2 hr):** emh, electrochaabani, dokani, yatoo, megapc, benzarti, skymill, techgate — these need real HTML inspection.
4. **Re-scrape fixed shops** in 3-shop batches.
5. **Re-match** with token_sort_ratio ≥ 99.
