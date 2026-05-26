#!/usr/bin/env python3
import argparse
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
DATA_DIR = Path("data")
HISTORY_DIR_PRICE = DATA_DIR / "price_history"
HISTORY_DIR_AVAILABILITY = DATA_DIR / "availability_history"
SHOPS = [
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
    "bestbuytunisie",
    "sigshop",
    "qsnet",
    "promouv",
    "bill",
    "techgate",
    "acspace",
    "krichen",
    "emh",
    "maalejaudio",
    "electrobennjima",
    "kamounhome",
    "agora",
    "tunewtec",
    "gamershop",
    "megapc",
    "yatoo",
    "affariyet",
    "chaktech",
    "techland",
    "bstech",
    "drest",
    "lamode",
]


def load_json(path: Path) -> Any:
    """Load JSON file safely."""
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {path}: {e}")
        return {}


def save_json(path: Path, data: Any):
    """Save JSON file safely."""
    try:
        # Create temp file first
        temp_path = path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Atomic move
        shutil.move(str(temp_path), str(path))
        logger.info(f"Saved history to {path}")
    except Exception as e:
        logger.error(f"Error saving {path}: {e}")


def find_recent_product_files(shop: str, count: int = 1) -> List[Path]:
    """Find the N most recent products_detailed.json files for a shop."""
    shop_dir = DATA_DIR / shop
    if not shop_dir.exists():
        logger.warning(f"Shop directory not found: {shop_dir}")
        return []

    # Get all timestamp directories
    try:
        # Filter for directories that match YYYY-MM-DD_HH-MM-SS pattern
        dirs = []
        for d in shop_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                datetime.strptime(d.name, "%Y-%m-%d_%H-%M-%S")
                dirs.append(d)
            except ValueError:
                continue

        if not dirs:
            logger.warning(f"No valid timestamp directories found for {shop}")
            return []

        # Sort by name (timestamp) descending
        sorted_dirs = sorted(dirs, key=lambda x: x.name, reverse=True)

        found_files = []
        for d in sorted_dirs:
            p_file = d / "products_detailed.json"
            if p_file.exists():
                found_files.append(p_file)
                if len(found_files) >= count:
                    break

        return found_files

    except Exception as e:
        logger.error(f"Error finding recent files for {shop}: {e}")
        return []


def find_latest_product_file(shop: str) -> Optional[Path]:
    """Wrapper for backward compatibility."""
    files = find_recent_product_files(shop, 1)
    return files[0] if files else None


def update_price_history(shop: str, products: List[Dict], scraped_at: str):
    """Update price history for a specific shop."""
    HISTORY_DIR_PRICE.mkdir(parents=True, exist_ok=True)
    history_file = HISTORY_DIR_PRICE / f"{shop}.json"

    history_data = {}
    if history_file.exists():
        history_data = load_json(history_file)

    updates_count = 0
    processed_ids = set()

    for product in products:
        pid = product.get("product_id")
        price = product.get("price")

        if not pid or price is None:
            continue

        pid = str(pid)

        # Deduplication check
        if pid in processed_ids:
            continue
        processed_ids.add(pid)

        if pid not in history_data:
            history_data[pid] = []

        current_history = history_data[pid]

        # Check if we need to add a new entry (Cold start or change)
        should_add = False
        if not current_history:
            should_add = True
        else:
            last_entry = current_history[-1]
            last_price = last_entry.get("price")
            if last_price != price:
                should_add = True

        if should_add:
            history_data[pid].append({"price": price, "date": scraped_at})
            updates_count += 1

    if updates_count > 0:
        logger.info(f"Updated PRICE history for {updates_count} products")
        save_json(history_file, history_data)
    else:
        logger.info("No PRICE changes detected")


def update_availability_history(shop: str, products: List[Dict], scraped_at: str):
    """Update availability history for a specific shop."""
    HISTORY_DIR_AVAILABILITY.mkdir(parents=True, exist_ok=True)
    history_file = HISTORY_DIR_AVAILABILITY / f"{shop}.json"

    history_data = {}
    if history_file.exists():
        history_data = load_json(history_file)

    updates_count = 0
    processed_ids = set()

    for product in products:
        pid = product.get("product_id")

        # Get availability fields
        status = product.get("availability")
        is_available = product.get("available")

        if not pid:
            continue

        pid = str(pid)

        # Deduplication check
        if pid in processed_ids:
            continue
        processed_ids.add(pid)

        if pid not in history_data:
            history_data[pid] = []

        current_history = history_data[pid]

        # Check if we need to add a new entry
        should_add = False
        if not current_history:
            should_add = True
        else:
            last_entry = current_history[-1]
            last_status = last_entry.get("status")
            last_available = last_entry.get("available")

            # Check for changes in either text status or boolean availability
            if last_status != status or last_available != is_available:
                should_add = True

        if should_add:
            history_data[pid].append(
                {"status": status, "available": is_available, "date": scraped_at}
            )
            updates_count += 1

    if updates_count > 0:
        logger.info(f"Updated AVAILABILITY history for {updates_count} products")
        save_json(history_file, history_data)
    else:
        logger.info("No AVAILABILITY changes detected")


HISTORY_DIR_ADDED = DATA_DIR / "products_added"
HISTORY_DIR_REMOVED = DATA_DIR / "products_removed"
STATE_DIR = DATA_DIR / "state"


def update_product_changes(shop: str, products: List[Dict]):
    """Track added and removed products per run."""
    # Ensure directories exist
    HISTORY_DIR_ADDED.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR_REMOVED.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Get Current IDs and Map (deduplicated)
    current_ids = set()
    current_map = {}
    for p in products:
        pid = p.get("product_id")
        if pid:
            sid = str(pid)
            current_ids.add(sid)
            current_map[sid] = p

    # 2. Get Previous IDs from state file
    state_file = STATE_DIR / f"{shop}_active.json"
    previous_ids = set()

    if state_file.exists():
        try:
            previous_list = load_json(state_file)
            if isinstance(previous_list, list):
                previous_ids = set(previous_list)
        except Exception as e:
            logger.error(f"Error loading state file for {shop}: {e}")

    # 3. Calculate Deltas
    added_ids = list(current_ids - previous_ids)
    removed_ids = list(previous_ids - current_ids)

    # 4. Resolve Full Details

    # A. Added Products: Easy, get from current map
    added_details = []
    for pid in added_ids:
        if pid in current_map:
            added_details.append(current_map[pid])

    # B. Removed Products: Need to find from PREVIOUS scrape file
    removed_details = []
    if removed_ids:
        # Find N (Current) and N-1 (Previous) files
        recent_files = find_recent_product_files(shop, 2)
        # recent_files[0] is current, recent_files[1] is previous

        if len(recent_files) > 1:
            prev_file = recent_files[1]
            logger.info(f"Loading previous scrape for removed details: {prev_file}")

            try:
                prev_data = load_json(prev_file)
                if isinstance(prev_data, list):
                    prev_products = prev_data
                else:
                    prev_products = prev_data.get("products", [])

                # Index previous products
                prev_map = {
                    str(p.get("product_id")): p
                    for p in prev_products
                    if p.get("product_id")
                }

                for pid in removed_ids:
                    if pid in prev_map:
                        removed_details.append(prev_map[pid])
                    else:
                        # Fallback: just ID if not found (shouldn't happen if state matches files)
                        removed_details.append(
                            {"product_id": pid, "status": "removed_details_not_found"}
                        )

            except Exception as e:
                logger.error(f"Error reading previous file {prev_file}: {e}")
                # Fallback
                removed_details = [{"product_id": pid} for pid in removed_ids]
        else:
            logger.warning(
                "No previous scrape file found. Cannot retrieve details for removed products."
            )
            removed_details = [{"product_id": pid} for pid in removed_ids]

    # 5. Stamp detected_at and accumulate into local files (append-only)
    detected_at = datetime.now().isoformat()
    added_file = HISTORY_DIR_ADDED / f"{shop}.json"
    removed_file = HISTORY_DIR_REMOVED / f"{shop}.json"

    if added_details:
        for p in added_details:
            p["detected_at"] = detected_at
        existing_added = load_json(added_file) if added_file.exists() else []
        if not isinstance(existing_added, list):
            existing_added = []
        save_json(added_file, existing_added + added_details)
        logger.info(f"Detected {len(added_details)} ADDED products")

    if removed_details:
        for p in removed_details:
            p["detected_at"] = detected_at
        existing_removed = load_json(removed_file) if removed_file.exists() else []
        if not isinstance(existing_removed, list):
            existing_removed = []
        save_json(removed_file, existing_removed + removed_details)
        logger.info(f"Detected {len(removed_details)} REMOVED products")

    # 6. Update State File (Keep only IDs as requested)
    save_json(state_file, list(current_ids))


def track_history_for_shop(shop: str):
    """Track price, availability, and product changes for a shop."""
    latest_file = find_latest_product_file(shop)
    if not latest_file:
        logger.warning(f"Skipping {shop}: No data found")
        return

    logger.info(f"Processing {shop} using {latest_file}")

    products = load_json(latest_file)
    # Support both list and old dict format for compatibility
    if isinstance(products, dict):
        products = products.get("products", [])

    if not products:
        logger.warning(f"No products found in {latest_file}")
        return

    # Infer time from filename (YYYY-MM-DD_HH-MM-SS)
    try:
        # parent dir name is the timestamp
        timestamp_str = latest_file.parent.name
        # meaningful check
        datetime.strptime(timestamp_str, "%Y-%m-%d_%H-%M-%S")
        scraped_at = datetime.strptime(timestamp_str, "%Y-%m-%d_%H-%M-%S").isoformat()
    except ValueError:
        scraped_at = datetime.now().isoformat()

    update_price_history(shop, products, scraped_at)
    update_availability_history(shop, products, scraped_at)
    update_product_changes(shop, products)


def main():
    parser = argparse.ArgumentParser(
        description="Track product history (price & availability) per shop"
    )
    parser.add_argument("--shops", nargs="+", default=SHOPS, help="Shops to process")
    args = parser.parse_args()

    logger.info("Starting history tracking...")

    for shop in args.shops:
        try:
            track_history_for_shop(shop)
        except Exception as e:
            logger.error(f"Failed to process {shop}: {e}")

    logger.info("History tracking complete")


if __name__ == "__main__":
    main()
