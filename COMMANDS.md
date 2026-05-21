# Tunisia E-commerce Scraper - Command Reference

## 📋 Overview

This project contains two main entry points for scraping Tunisian e-commerce websites:

1. **`scrape.py`** - Manual scraping & testing tool
2. **`pipeline.py`** - Automated continuous scraping pipeline

## 🚀 Quick Start

### Setup Environment
```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers (for sites that need JS)
playwright install chromium
```

## 📊 Available Sites

The scraper supports **21 Tunisian e-commerce sites**:

- `mytek` - mytek.tn (Playwright-based)
- `tunisianet` - tunisianet.com.tn
- `technopro` - technopro.com.tn
- `darty` - darty.tn
- `spacenet` - spacenet.tn
- `jumbo` - jumbo.tn
- `graiet` - graiet.tn
- `batam` - batam.com.tn
- `zoom` - zoom.com.tn
- `allani` - allani.com.tn
- `expert_gaming` - expert-gaming.tn
- `geant` - geantdrive.tn
- `mapara` - maparatunisie.tn
- `parafendri` - parafendri.tn
- `parashop` - parashop.tn
- `pharmacieplus` - pharmacieplus.tn (Playwright-based)
- `pharmashop` - pharmashop.tn
- `sbs` - sbs.tn (Playwright-based)
- `scoop` - scoop.tn
- `skymill` - skymill.tn (Playwright-based)
- `wiki` - wiki.tn (Playwright-based)

## 🛠️ Command Reference

### 1. `scrape.py` - Manual Scraping & Testing

#### List Available Sites
```bash
python scrape.py list
```

#### Test Single Site (Recommended for development)
```bash
# Basic test with default limits
python scrape.py test --site parashop --categories 3 --products 5

# Test with custom limits
python scrape.py test --site mytek --categories 2 --products 10 --detail-workers 32

# Test with fewer details workers
python scrape.py test --site batam --categories 1 --products 3 --detail-workers 8
```

#### Test All Sites
```bash
# Test all sites with limits
python scrape.py test --all-sites --categories 2 --products 3 --detail-workers 16
```

#### Full Scrape (Production)
```bash
# Full scrape with details
python scrape.py full --site parashop

# Full scrape without details (faster)
python scrape.py full --site mytek --no-details

# Custom worker counts
python scrape.py full --site tunisianet --workers 32 --detail-workers 128
```

**Options:**
- `--site`: Site name (required for test/full)
- `--all-sites`: Test all sites (for test command)
- `--categories`: Number of categories to scrape (default: 3)
- `--products`: Products per category (default: 5)
- `--workers`: Parallel workers for category scraping (default: 16)
- `--detail-workers`: Parallel workers for product details (default: 64/16)
- `--no-details`: Skip product detail scraping

### 2. `pipeline.py` - Automated Pipeline

#### Run Once (All Sites)
```bash
python pipeline.py run --once
```

#### Run Once (Specific Sites)
```bash
# Run specific sites
python pipeline.py run --once --sites mytek tunisianet parashop

# Single site
python pipeline.py run --once --sites batam
```

#### Continuous Mode (Production)
```bash
# Run continuously with default interval (12 hours)
python pipeline.py run

# Custom interval (6 hours = 360 minutes)
python pipeline.py run --interval 360

# Continuous with specific sites
python pipeline.py run --sites mytek tunisianet --interval 720
```

**Options:**
- `--once`: Run once and exit (default: continuous)
- `--sites`: Space-separated list of sites
- `--interval`: Interval in minutes (default: 720 = 12 hours)
- `--config`: Custom config file path (default: configs/pipeline_config.yaml)

## 📁 Project Structure

```
tunisia-scraper/
├── scrape.py                    # Manual scraping & testing
├── pipeline.py                  # Automated pipeline
├── COMMANDS.md                  # This command reference
├── README.md                    # Project documentation
├── requirements.txt             # Python dependencies
├── configs/
│   ├── pipeline_config.yaml     # Pipeline settings
│   └── sites/                   # Site-specific configs
│       ├── _template.yaml       # Template for new sites
│       ├── batam.yaml          # Batam configuration
│       ├── parashop.yaml       # Parashop configuration
│       └── ...                 # Other site configs
├── scraper/
│   ├── base.py                 # Base scraper classes
│   ├── __init__.py
│   └── sites/                  # Site-specific scrapers
│       ├── __init__.py
│       ├── batam.py            # Batam scraper
│       ├── parashop.py         # Parashop scraper
│       └── ...                 # Other site scrapers
├── data/                       # Output data (created during runs)
│   ├── parashop/
│   │   └── 2025-12-18_10-45-15/
│   │       ├── categories.json      # Category hierarchy
│   │       ├── products.json        # Product listings
│   │       └── products_detailed.json # Full product details
│   └── ...                      # Other sites
└── logs/                       # Log files (created during runs)
    ├── parashop_20251218_104515.log
    └── ...
```

## 📁 Output Structure

Data is saved in timestamped folders: `data/{site}/{YYYY-MM-DD_HH-MM-SS}/`

Each scrape creates:
- **`categories.json`** - Complete category hierarchy (top → low → sub)
- **`products.json`** - Product listings with basic info
- **`products_detailed.json`** - Full product details with descriptions, images, pricing

Example data structure:
```json
{
  "site": "parashop",
  "total_products": 150,
  "products": [
    {
      "id": null,
      "url": "https://www.parashop.tn/...",
      "name": "Product Name",
      "price": 99.99,
      "old_price": 129.99,
      "availability": "in_stock",
      "available": true,
      "shop": "parashop"
    }
  ]
}
```

## ⚙️ Configuration Files

### Pipeline Configuration
**File:** `configs/pipeline_config.yaml`
```yaml
sites:
  - name: mytek
    use_tor: false
  - name: tunisianet
    use_tor: false
  # ... all 21 sites listed with use_tor flag

interval_minutes: 1440  # 24 hours

data_dir: "data"

scraping:
  workers: 16
  detail_workers: 16
```

### Site-Specific Configuration
**Files:** `configs/sites/{site}.yaml`

Each site has its own configuration with:
- Base URL
- Scraping settings (timeouts, retries)
- CSS selectors for different page elements

## 🎯 Common Use Cases

### Development & Testing
```bash
# Test new scraper implementation
python scrape.py test --site parashop --categories 1 --products 2

# Debug category extraction
python scrape.py test --site mytek --categories 5 --products 0

# Performance testing
python scrape.py test --all-sites --categories 1 --products 1
```

### Production Scraping
```bash
# Daily full scrape
python pipeline.py run --once

# Continuous monitoring (every 6 hours)
python pipeline.py run --interval 360

# Emergency single site scrape
python scrape.py full --site tunisianet
```

### Data Analysis
```bash
# Quick data check
ls -la data/*/20*/products.json | head -5

# Count products
find data -name "products.json" -exec jq '.total_products' {} \;
```

## 🔧 Troubleshooting

### Common Issues

**Module Import Errors:**
```bash
# Ensure virtual environment is activated
source .venv/bin/activate

# Reinstall dependencies
pip install -r requirements.txt
```

**Playwright Browser Issues:**
```bash
# Install/update browsers
playwright install chromium

# Check browser installation
playwright --version
```

**Permission Errors:**
```bash
# Ensure write permissions for data directory
chmod 755 data/
```

**Rate Limiting:**
- Reduce worker counts: `--workers 8 --detail-workers 32`
- Add delays between requests (modify site config)

## 📊 Performance Tuning

### Worker Configuration
- **Category scraping**: 16-32 workers (CPU intensive)
- **Product details**: 64-128 workers (I/O intensive)
- **Memory usage**: ~2GB for full pipeline run

### Recommended Settings

**Development:**
```bash
python scrape.py test --site parashop --categories 2 --products 5 --detail-workers 16
```

**Production:**
```bash
python pipeline.py run --once  # Uses config defaults
```

**High-Performance:**
```bash
python scrape.py full --site tunisianet --workers 32 --detail-workers 128
```

## 🚨 Error Handling

The scraper includes:
- **Exponential backoff** for failed requests
- **Automatic retries** (configurable per site)
- **Rate limiting** detection and handling
- **Graceful degradation** (continues with partial failures)

Check logs in `logs/` directory for detailed error information.

## 🔄 Adding New Sites

1. **Create config:** `configs/sites/newsite.yaml`
2. **Create scraper:** `scraper/sites/newsite.py`
3. **Register site:** Add to `scraper/sites/__init__.py`
4. **Add to pipeline:** Add to `configs/pipeline_config.yaml`

See `_template.yaml` and existing sites for examples.

## 🔍 Current Project Status

### Active Sites (21/21 configured)
✅ All sites are configured and ready:
- **Playwright-based**: mytek, pharmacieplus, sbs, skymill, wiki, graiet
- **Fast HTTP-based**: tunisianet, technopro, darty, spacenet, jumbo, batam, zoom, allani, expert_gaming, geant, mapara, parafendri, parashop, pharmashop, scoop

### Configuration
- **Pipeline Interval**: 1440 minutes (24 hours)
- **Default Workers**: 16 (categories) + 16 (details)
- **Data Directory**: `data/`
- **Logs Directory**: `logs/`

## 🚀 Quick Examples

### Test a Single Site
```bash
# Linux / macOS
source .venv/bin/activate

# Windows
.venv\Scripts\activate

python scrape.py test --site parashop --categories 2 --products 5
```

### Run Full Pipeline Once
```bash
python pipeline.py run --once
```

### Check Available Sites
```bash
python scrape.py list
```

---

**Last Updated:** 2025
**Version:** 2.0.0
**Sites Supported:** 21 Tunisian e-commerce platforms
**Status:** ✅ Fully Operational
