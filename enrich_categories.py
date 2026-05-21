#!/usr/bin/env python3
"""
Category Enrichment Script
==========================

Reads the merged products file and enriches each product with
top_category, low_category, and subcategory fields taken from the
source product data.

Uses the first shop in the sorted found_in_shops[] array as the
category source.

Usage:
    python enrich_categories.py
"""

import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# === Configuration ===
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
MERGED_FILE = DATA_DIR / "merged" / "products_merged.json"
REQUIRED_SOURCES = [
    "mytek",
    "tunisianet",
    "technopro",
    "darty",
    "spacenet",
    "jumbo",
    "graiet",
    "batam",
    "zoom",
    "allani",
    "expert_gaming",
    "geant",
    "mapara",
    "parafendri",
    "parashop",
    "pharmacieplus",
    "pharmashop",
    "sbs",
    "scoop",
    "skymill",
    "wiki",
]


# === Logger ===
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("enrich_categories")
    logger.setLevel(logging.INFO)
    logger.handlers = []
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


logger = setup_logger()


# === Helpers ===


def find_latest_product_file(source: str) -> Optional[Path]:
    """Find the latest products_detailed.json for a source."""
    source_dir = DATA_DIR / source
    if not source_dir.exists():
        return None

    timestamp_dirs = [
        d
        for d in source_dir.iterdir()
        if d.is_dir()
        and not d.name.startswith(".")
        and d.name != "html"
        and "-" in d.name
        and "_" in d.name
    ]
    if not timestamp_dirs:
        return None

    latest_dir = sorted(timestamp_dirs, reverse=True)[0]
    product_file = latest_dir / "products_detailed.json"
    return product_file if product_file.exists() else None


def load_source_products(filepath: Path) -> List[Dict]:
    """Load product list from a source JSON file."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "products" in data:
        return data["products"]
    return []


def normalize_sku(sku: str) -> str:
    """Normalize SKU: strip non-alphanumeric, uppercase."""
    if not sku:
        return ""
    return "".join(c for c in sku if c.isalnum()).upper()


def build_sku_category_index(products: List[Dict]) -> Dict[str, Dict]:
    """
    Build a mapping of normalized SKU -> {top_category, low_category, subcategory}
    from a list of source products.
    """
    index: Dict[str, Dict] = {}
    for p in products:
        sku = p.get("sku")
        if not sku:
            continue
        norm = normalize_sku(sku)
        if norm and norm not in index:
            index[norm] = {
                "top_category": p.get("top_category"),
                "low_category": p.get("low_category"),
                "subcategory": p.get("subcategory"),
            }
    return index


# === Main ===


def enrich_categories() -> None:
    """
    Load merged products, look up categories from source data for the
    first shop in found_in_shops[], and write the enriched file back.
    """
    t_start = time.perf_counter()

    logger.info("=" * 60)
    logger.info("📂 CATEGORY ENRICHMENT STARTED")
    logger.info("=" * 60)

    # --- Load merged products ------------------------------------
    if not MERGED_FILE.exists():
        logger.error(f"Merged file not found: {MERGED_FILE}")
        sys.exit(1)

    logger.info(f"Loading merged products from {MERGED_FILE} ...")
    with open(MERGED_FILE, "r", encoding="utf-8") as f:
        products: List[Dict] = json.load(f)
    logger.info(f"  {len(products):,} products loaded")

    # --- Determine which shops we need ---------------------------
    needed_shops = set()
    for p in products:
        shops = p.get("found_in_shops", [])
        if shops:
            needed_shops.add(sorted(shops)[0])
    logger.info(f"  Category source shops: {sorted(needed_shops)}")

    # --- Load source data & build category indexes ---------------
    logger.info("Loading source product files for category lookup...")
    shop_cat_indexes: Dict[str, Dict[str, Dict]] = {}

    for shop in sorted(needed_shops):
        filepath = find_latest_product_file(shop)
        if filepath is None:
            logger.warning(f"  ⚠️  {shop}: no source data found")
            continue
        source_products = load_source_products(filepath)
        idx = build_sku_category_index(source_products)
        shop_cat_indexes[shop] = idx
        logger.info(f"  {shop}: {len(idx):,} SKUs indexed from {filepath.parent.name}")

    # --- Enrich each product -------------------------------------
    logger.info("Enriching products with categories...")
    enriched = 0
    not_found = 0

    for p in products:
        shops = p.get("found_in_shops", [])
        if not shops:
            not_found += 1
            continue

        first_shop = sorted(shops)[0]
        cat_index = shop_cat_indexes.get(first_shop)
        if cat_index is None:
            not_found += 1
            continue

        # The merged SKU is already normalized (uppercase, alphanumeric only),
        # and the category index is keyed by normalized SKU — direct lookup.
        sku = p.get("sku", "")
        cats = cat_index.get(sku)

        if cats is None:
            not_found += 1
        else:
            p["top_category"] = cats["top_category"]
            p["low_category"] = cats["low_category"]
            p["subcategory"] = cats["subcategory"]
            p["category_source_shop"] = first_shop
            enriched += 1

    logger.info(f"  Enriched: {enriched:,} | Not found: {not_found:,}")

    # --- Save back -----------------------------------------------
    logger.info("Saving enriched merged products...")
    temp_path = MERGED_FILE.with_suffix(".json.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    temp_path.replace(MERGED_FILE)

    elapsed = time.perf_counter() - t_start
    logger.info(f"✅ Saved to {MERGED_FILE}")
    logger.info(f"⏱  Done in {elapsed:.2f}s")
    logger.info("=" * 60)


if __name__ == "__main__":
    enrich_categories()
