# Tunisia E-commerce Scraper

A multi-site web scraper for Tunisian e-commerce and pharmacy websites. Scrapes product data and saves structured JSON files.

## Features

- **Multi-site support** - Tunisian e-commerce and pharmacy sites, including mapara, parafendri, parashop, and pharmacieplus
- **Parallel processing** - 16 workers for products, 64 for details
- **JSON output** - Saves structured data to timestamped folders
- **Two modes** - Testing/debugging and automated production scraping
- **Data quality guards** - Normalizes GTIN/barcode fields, separates references from barcodes, and removes obvious duplicate products

## Quick Start

```bash
# Activate virtual environment
source .venv/bin/activate

# Test a single site (limited scope)
python scrape.py test --site mapara --categories 3 --products 5

# Test all sites
python scrape.py test --all-sites --categories 3 --products 5

# Run automated pipeline once for the pharmacy sites
python pipeline.py run --once --sites mapara parafendri parashop pharmacieplus

# Run automated pipeline continuously (every 12 hours)
python pipeline.py run
```

## Project Structure

```
Scrapping/
├── scrape.py           # Manual scraping & testing
├── pipeline.py         # Automated pipeline
├── requirements.txt    # Python dependencies
├── configs/
│   ├── pipeline_config.yaml    # Pipeline configuration
│   └── sites/                  # Site-specific configurations
└── scraper/
    ├── base.py         # Base scraper classes
    └── sites/          # Site-specific scrapers
```

## Scripts

### `scrape.py` - Manual Scraping & Testing

Use this for testing and debugging:

```bash
# Test with limited scope
python scrape.py test --site mapara --categories 3 --products 5
python scrape.py test --all-sites --categories 3 --products 5

# Full scrape (all categories, all products)
python scrape.py full --site mapara

# List available sites
python scrape.py list
```

### `pipeline.py` - Automated Pipeline

Use this for automated scraping:

```bash
# Run once for all sites
python pipeline.py run --once

# Run once for specific sites
python pipeline.py run --once --sites mapara parafendri parashop pharmacieplus

# Run continuously (every 12 hours)
python pipeline.py run

# Use custom interval (minutes)
python pipeline.py run --interval 360  # Every 6 hours
```

## Available Sites

Run `python scrape.py list` for the current registry. The pharmacy-focused scrapers covered by selector tests are:

- `mapara` - maparatunisie.tn
- `parafendri` - parafendri.tn
- `parashop` - parashop.tn
- `pharmacieplus` - parapharmacieplus.tn

Other configured sites include `mytek`, `tunisianet`, `technopro`, `darty`, `spacenet`, `jumbo`, `graiet`, `batam`, `zoom`, `allani`, `expert_gaming`, `geant`, `pharmashop`, `sbs`, `scoop`, `skymill`, and `wiki`.

## Configuration

Edit `configs/pipeline_config.yaml`:

```yaml
# Sites to process
sites:
  - mytek
  - tunisianet
  # ...

# Run interval (minutes)
interval_minutes: 720  # 12 hours

# Data directory
data_dir: "data"

# Scraping settings
scraping:
  workers: 16
  detail_workers: 64
```

## Output

Data is saved in `data/{site}/{date_timestamp}/` (e.g., `2025-12-12_14-30-00`):
- `categories.json` - Category hierarchy
- `products.json` - Product listings
- `products_detailed.json` - Full product details

Each scrape creates a new timestamped folder.

## How It Works

1. **Download frontpage** - Get main page HTML
2. **Extract categories** - Parse 3-level category hierarchy (top → low → subcategory)
3. **Scrape products** - Get product listings from all categories (parallel)
4. **Scrape details** - Get full product details (parallel)
5. **Save to JSON** - Store in timestamped folder

## Installation

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers (required for JavaScript-rendered sites)
playwright install chromium
```

## Verification Before Production

```bash
# Syntax/import check
python -m compileall scraper tests scrape.py

# Full automated test suite
python -m pytest -q

# Tiny live smoke for a single shop
python scrape.py test --site pharmacieplus --categories 1 --products 1 --detail-workers 1 --category-probe-limit 2
```

Deploy/run with environment variables for proxies or credentials if your environment needs them, then use `python pipeline.py run --once --sites mapara parafendri parashop pharmacieplus` for a one-off production scrape or `python pipeline.py run` for the scheduled loop.

## Requirements

- Python 3.8+
