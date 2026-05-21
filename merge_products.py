#!/usr/bin/env python3
"""
Product Merge System
====================

Merges product data from multiple sources based on SKU matching.
Includes products that exist in at least MIN_SHOPS_REQUIRED shops (default: 2).

Output: data/merged/products_merged.json (single file, replaces previous)
"""

import json
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# === Configuration ===
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
MERGED_DIR = DATA_DIR / "merged"
MERGED_FILE = MERGED_DIR / "products_merged.json"

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
MIN_SHOPS_REQUIRED = 2  # Product must exist in at least this many shops to be merged


# === Setup Logger ===
def setup_logger() -> logging.Logger:
    """Setup logger for merge operations."""
    logger = logging.getLogger("merge_products")
    logger.setLevel(logging.INFO)
    logger.handlers = []

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


logger = setup_logger()


# === SKU Normalization & Fuzzy Matching ===


def normalize_sku(sku: str) -> str:
    """
    Normalize a SKU by removing all non-alphanumeric characters and uppercasing.

    Examples:
        "AB-123_x"  -> "AB123X"
        "  hp/15s " -> "HP15S"
    """
    if not sku:
        return ""
    return "".join(c for c in sku if c.isalnum()).upper()


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(
                min(
                    prev[j + 1] + 1,  # deletion
                    curr[j] + 1,  # insertion
                    prev[j] + (c1 != c2),  # substitution
                )
            )
        prev = curr
    return prev[-1]


def _levenshtein_ratio(s1: str, s2: str) -> float:
    """Similarity ratio in [0, 1] based on Levenshtein distance."""
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    max_len = max(len(s1), len(s2))
    return 1.0 - (_levenshtein_distance(s1, s2) / max_len)


def are_skus_similar(a: str, b: str, threshold: float = 0.85) -> bool:
    """
    Determine whether two *raw* SKU strings refer to the same product.

    Rules
    -----
    1. null / empty SKUs never match.
    2. Both SKUs are normalized (symbols stripped, uppercased).
    3. Short normalized SKUs (len < 5) require exact normalized equality.
    4. Fast path: if one normalized SKU contains the other *and* the
       shorter / longer length ratio >= threshold  ->  match.
    5. Slow path: Levenshtein similarity ratio >= threshold  ->  match.
    """
    if not a or not b:
        return False

    na, nb = normalize_sku(a), normalize_sku(b)
    if not na or not nb:
        return False

    # Short SKUs -> exact only
    if len(na) < 5 or len(nb) < 5:
        return na == nb

    # Exact normalized match
    if na == nb:
        return True

    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    length_ratio = len(shorter) / len(longer)

    # Fast substring pass
    if shorter in longer and length_ratio >= threshold:
        return True

    # Quick reject: if length ratio itself is below threshold, Levenshtein
    # can never reach the threshold either.
    if length_ratio < threshold:
        return False

    # Slow Levenshtein pass
    return _levenshtein_ratio(na, nb) >= threshold


# === Configuration ===
# Windows caps ProcessPoolExecutor at 61; os.cpu_count() as fallback
_MAX_SYSTEM_WORKERS = 61 if sys.platform == "win32" else (os.cpu_count() or 4)
FUZZY_WORKERS = min(61, _MAX_SYSTEM_WORKERS)
FUZZY_CHUNK_SIZE = 20_000  # Pairs per chunk sent to each worker


class _UnionFind:
    """Simple union-find for transitive SKU grouping."""

    def __init__(self):
        self._parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        while self._parent.get(x, x) != x:
            self._parent[x] = self._parent.get(self._parent[x], self._parent[x])
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def _check_chunk(
    chunk: List[Tuple[str, str]], threshold: float = 0.85
) -> List[Tuple[str, str]]:
    """
    Worker function (must be top-level for pickling).
    Receives a list of (na, nb) pairs, returns those that are similar.
    Inlines the similarity logic to avoid import overhead in sub-processes.
    """
    matches = []
    for na, nb in chunk:
        # --- inlined _are_normalized_similar ---
        if len(na) < 5 or len(nb) < 5:
            if na == nb:
                matches.append((na, nb))
            continue
        if na == nb:
            matches.append((na, nb))
            continue

        shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
        length_ratio = len(shorter) / len(longer)

        if shorter in longer and length_ratio >= threshold:
            matches.append((na, nb))
            continue
        if length_ratio < threshold:
            continue

        # Levenshtein ratio
        s1, s2 = longer, shorter  # s1 is longer
        prev = list(range(len(s2) + 1))
        for _i, c1 in enumerate(s1):
            curr = [_i + 1]
            for _j, c2 in enumerate(s2):
                curr.append(
                    min(
                        prev[_j + 1] + 1,
                        curr[_j] + 1,
                        prev[_j] + (c1 != c2),
                    )
                )
            prev = curr
        ratio = 1.0 - (prev[-1] / len(s1))
        if ratio >= threshold:
            matches.append((na, nb))
    return matches


# === Core Functions ===


def find_latest_product_file(source: str) -> Optional[Path]:
    """
    Find the latest products_detailed.json file for a given source.

    Args:
        source: Source name (mytek, spacenet, tunisianet)

    Returns:
        Path to latest file or None if not found
    """
    source_dir = DATA_DIR / source

    if not source_dir.exists():
        logger.error(f"Source directory not found: {source_dir}")
        return None

    # Find all timestamped directories (format: YYYY-MM-DD_HH-MM-SS)
    # Exclude special directories like 'html'
    timestamp_dirs = []
    for d in source_dir.iterdir():
        if not d.is_dir():
            continue
        # Skip hidden directories and 'html' directory
        if d.name.startswith(".") or d.name == "html":
            continue
        # Check if directory name matches timestamp format (contains date pattern)
        if "-" in d.name and "_" in d.name:
            timestamp_dirs.append(d)

    if not timestamp_dirs:
        logger.error(f"No data directories found for {source}")
        return None

    # Sort by name (timestamp format YYYY-MM-DD_HH-MM-SS sorts correctly)
    latest_dir = sorted(timestamp_dirs, reverse=True)[0]

    product_file = latest_dir / "products_detailed.json"

    if not product_file.exists():
        logger.error(f"products_detailed.json not found in {latest_dir}")
        return None

    return product_file


def load_product_file(filepath: Path) -> Dict:
    """
    Load and validate a product JSON file.

    Args:
        filepath: Path to JSON file

    Returns:
        Loaded JSON data

    Raises:
        ValueError: If file is invalid or missing required fields
    """
    # Try loading as standard JSON
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            # New format: keys are inside list items
            return {"products": data}  # Wrap in dict to match expected interface
        elif isinstance(data, dict) and "products" in data:
            # Old format
            return data
        else:
            raise ValueError(
                f"Invalid format in {filepath}: expected list or dict with 'products' key"
            )

    except Exception as e:
        raise ValueError(f"Failed to load {filepath}: {e}")

    # Validate structure
    if isinstance(data, list):
        # New format: keys are inside list items
        return {"products": data}  # Wrap in dict to match expected interface
    elif isinstance(data, dict) and "products" in data:
        # Old format
        return data
    else:
        raise ValueError(
            f"Invalid format in {filepath}: expected list or dict with 'products' key"
        )


def deduplicate_products(products: List[Dict], source_name: str) -> List[Dict]:
    """
    Remove duplicate products based on 'product_id'.
    Keep the first occurrence.

    Args:
        products: List of product dictionaries
        source_name: Name of the source (for logging)

    Returns:
        Deduplicated list of products
    """
    seen_ids = set()
    unique_products = []
    duplicates = 0

    for product in products:
        pid = product.get("product_id")

        # If no product_id, fallback to keeping it (or skipping? safely keep for now)
        if not pid:
            unique_products.append(product)
            continue

        if pid in seen_ids:
            duplicates += 1
            continue

        seen_ids.add(pid)
        unique_products.append(product)

    if duplicates > 0:
        logger.info(
            f"  {source_name}: Removed {duplicates} duplicate products (duplicate product_id)"
        )

    return unique_products


def index_by_sku(products: List[Dict]) -> Dict[str, Dict]:
    """
    Create SKU → product mapping, excluding products without SKU.

    Args:
        products: List of product dictionaries

    Returns:
        Dictionary mapping SKU to product data
    """
    index = {}
    skipped = 0

    for product in products:
        sku = product.get("sku")

        # Skip products without SKU or with null SKU
        if not sku or sku is None:
            skipped += 1
            continue

        # Use first occurrence if duplicate SKUs exist
        if sku not in index:
            index[sku] = product

    if skipped > 0:
        logger.debug(f"Skipped {skipped} products without valid SKU")

    return index


def find_qualifying_skus(
    indexes: Dict[str, Dict],
    min_shops: int = MIN_SHOPS_REQUIRED,
) -> Dict[str, Dict[str, str]]:
    """
    Find SKUs present in at least *min_shops* sources using
    normalized + fuzzy matching.

    Two-stage approach:
      1. Group by **exact normalized** SKU (fast).
      2. Merge remaining groups with **fuzzy** similarity (≥ 0.85).

    Returns
    -------
    Dict mapping *canonical normalized SKU* -> {shop_name: raw_sku}
    Sorted by canonical SKU.
    """
    t_total = time.perf_counter()

    # --- Collect every (shop, raw_sku, normalized_sku) triple ----
    t0 = time.perf_counter()
    entries: List[Tuple[str, str, str]] = []  # (shop, raw, norm)
    for shop_name, sku_index in indexes.items():
        for raw_sku in sku_index:
            norm = normalize_sku(raw_sku)
            if norm:
                entries.append((shop_name, raw_sku, norm))
    logger.info(
        f"  Collected {len(entries):,} SKU entries  [{time.perf_counter() - t0:.2f}s]"
    )

    # --- Phase 1: exact normalized grouping -----------------------
    t0 = time.perf_counter()
    norm_groups: Dict[str, List[Tuple[str, str]]] = {}
    for shop, raw, norm in entries:
        norm_groups.setdefault(norm, []).append((shop, raw))

    exact_match_count = len(norm_groups)
    # Count how many were merged by exact normalization alone
    exact_merged = len(entries) - exact_match_count
    logger.info(
        f"  Phase 1 (exact normalized): {exact_match_count:,} unique groups "
        f"({exact_merged:,} collapsed)  [{time.perf_counter() - t0:.2f}s]"
    )

    # --- Phase 2: fuzzy merge via union-find (parallelized) -------
    uf = _UnionFind()
    norm_keys = list(norm_groups.keys())
    total_keys = len(norm_keys)
    total_pairs = total_keys * (total_keys - 1) // 2

    logger.info(
        f"  Phase 2: {total_keys:,} unique normalized SKUs, "
        f"{total_pairs:,} total pairs (before length filter)"
    )

    if total_pairs == 0:
        logger.info(f"  Phase 2: nothing to compare")
    else:
        # --- Build chunks via sort-by-length + sliding window ---
        # Sort keys by length so we only compare within a length-
        # compatible window.  This turns the O(n²) chunk-build into
        # an O(n * w) scan where w is the window width.
        t0 = time.perf_counter()
        sorted_keys = sorted(norm_keys, key=len)
        chunks: List[List[Tuple[str, str]]] = []
        current_chunk: List[Tuple[str, str]] = []
        candidate_count = 0

        for i in range(len(sorted_keys)):
            li = len(sorted_keys[i])
            # Walk forward while length is compatible
            j = i + 1
            while j < len(sorted_keys):
                lj = len(sorted_keys[j])
                # Once lj exceeds the 15% tolerance, all further
                # keys are even longer -> break
                if lj - li > li * 0.15:
                    break
                current_chunk.append((sorted_keys[i], sorted_keys[j]))
                candidate_count += 1
                if len(current_chunk) >= FUZZY_CHUNK_SIZE:
                    chunks.append(current_chunk)
                    current_chunk = []
                j += 1
        if current_chunk:
            chunks.append(current_chunk)

        build_time = time.perf_counter() - t0
        rejected = total_pairs - candidate_count
        logger.info(f"  Built chunks in {build_time:.2f}s")
        logger.info(
            f"  Length pre-filter: kept {candidate_count:,} / {total_pairs:,} pairs "
            f"(rejected {rejected:,}, {rejected / max(total_pairs, 1) * 100:.1f}%)"
        )
        logger.info(f"  Chunks: {len(chunks)} x ~{FUZZY_CHUNK_SIZE:,} pairs")

        # --- Dispatch to workers ----------------------------------
        if candidate_count <= FUZZY_CHUNK_SIZE:
            # Small enough to run in the main process
            t0 = time.perf_counter()
            all_pairs = chunks[0] if chunks else []
            matches = _check_chunk(all_pairs)
            for na, nb in matches:
                uf.union(na, nb)
            logger.info(
                f"  Phase 2 (in-process): checked {len(all_pairs):,} pairs, "
                f"{len(matches):,} fuzzy matches  [{time.perf_counter() - t0:.2f}s]"
            )
        else:
            logger.info(f"  Dispatching to {FUZZY_WORKERS} workers...")
            t0 = time.perf_counter()
            total_matches = 0
            done_chunks = 0
            with ProcessPoolExecutor(max_workers=FUZZY_WORKERS) as pool:
                for match_list in pool.map(_check_chunk, chunks):
                    for na, nb in match_list:
                        uf.union(na, nb)
                    total_matches += len(match_list)
                    done_chunks += 1
                    report_interval = max(1, len(chunks) // 10)
                    if done_chunks % report_interval == 0 or done_chunks == len(chunks):
                        elapsed = time.perf_counter() - t0
                        pct = done_chunks / len(chunks) * 100
                        pairs_done = sum(len(c) for c in chunks[:done_chunks])
                        rate = pairs_done / max(elapsed, 0.001)
                        logger.info(
                            f"    Progress: {done_chunks}/{len(chunks)} chunks "
                            f"({pct:.0f}%) | {total_matches:,} matches | "
                            f"{elapsed:.1f}s elapsed | {rate:,.0f} pairs/s"
                        )

            logger.info(
                f"  Phase 2 done: {total_matches:,} fuzzy matches found  "
                f"[{time.perf_counter() - t0:.2f}s workers, "
                f"{build_time + time.perf_counter() - t0:.2f}s total]"
            )

    # Build canonical groups:  canonical_norm -> {shop: raw_sku}
    t0 = time.perf_counter()
    canon_groups: Dict[str, Dict[str, str]] = {}
    for norm, pairs in norm_groups.items():
        canon = uf.find(norm)
        if canon not in canon_groups:
            canon_groups[canon] = {}
        for shop, raw in pairs:
            # Keep first raw SKU per shop
            if shop not in canon_groups[canon]:
                canon_groups[canon][shop] = raw

    merged_count = exact_match_count - len(canon_groups)
    logger.info(
        f"  Built {len(canon_groups):,} canonical groups  [{time.perf_counter() - t0:.2f}s]"
    )

    # --- Log per-shop SKU counts ----------------------------------
    for shop_name, sku_index in indexes.items():
        logger.info(f"  {shop_name}: {len(sku_index)} SKUs")

    if merged_count > 0:
        logger.info(f"  Fuzzy merge combined {merged_count} extra SKU groups")

    # --- Filter to >= min_shops -----------------------------------
    qualifying = {
        canon: shop_map
        for canon, shop_map in canon_groups.items()
        if len(shop_map) >= min_shops
    }

    # --- Log distribution -----------------------------------------
    shop_count_dist: Dict[int, int] = {}
    for shop_map in qualifying.values():
        n = len(shop_map)
        shop_count_dist[n] = shop_count_dist.get(n, 0) + 1

    logger.info(f"  Qualifying SKUs (in >= {min_shops} shops): {len(qualifying)}")
    for n in sorted(shop_count_dist.keys()):
        logger.info(f"    In {n} shops: {shop_count_dist[n]} SKUs")

    logger.info(
        f"  ⏱  Total find_qualifying_skus: {time.perf_counter() - t_total:.2f}s"
    )

    return dict(sorted(qualifying.items()))


def _are_normalized_similar(na: str, nb: str, threshold: float = 0.85) -> bool:
    """
    Compare two *already-normalized* SKU strings.
    Same logic as `are_skus_similar` but skips the normalization step.
    """
    if not na or not nb:
        return False
    if len(na) < 5 or len(nb) < 5:
        return na == nb
    if na == nb:
        return True

    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    length_ratio = len(shorter) / len(longer)

    if shorter in longer and length_ratio >= threshold:
        return True
    if length_ratio < threshold:
        return False

    return _levenshtein_ratio(na, nb) >= threshold


def merge_product_data(sku: str, shop_products: Dict[str, Dict]) -> Dict:
    """
    Merge product data from multiple sources into unified structure.

    Args:
        sku: Product SKU
        shop_products: Dict mapping shop name -> product data (only shops that have this SKU)

    Returns:
        Merged product dictionary
    """
    # Use title from first available source
    title = None
    for product in shop_products.values():
        title = product.get("title")
        if title:
            break

    # Build shops dict — only include shops that actually have this product
    shops = {}
    for shop_name, product in shop_products.items():
        shops[shop_name] = {
            "url": product.get("url"),
            "price": product.get("price"),
            "old_price": product.get("old_price"),
            "availability": product.get("availability"),
            "available": product.get("available"),
            "store_availability": product.get("store_availability"),
            "brand": product.get("brand"),
            "images": product.get("images", []),
            "specifications": product.get("specifications", {}),
            "scraped_at": product.get("scraped_at"),
        }

    merged = {
        "sku": sku,
        "title": title,
        "found_in_shops": sorted(shop_products.keys()),
        "shop_count": len(shop_products),
        "shops": shops,
    }

    return merged


def calculate_analytics(
    products: List[Dict], all_shops: Optional[List[str]] = None
) -> Dict:
    """
    Calculate price and discount statistics from merged products.

    Args:
        products: List of merged product dictionaries
        all_shops: List of all shop names (defaults to REQUIRED_SOURCES)
    """
    if all_shops is None:
        all_shops = REQUIRED_SOURCES

    stats = {
        "shops": {},
        "global": {
            "cheapest_basket": {"shop": None, "total_cost": float("inf")},
            "best_availability": {"shop": None, "count": 0},
        },
        "product_distribution": {},  # How many products in N shops
    }

    # Count distribution of shop_count
    for p in products:
        n = p.get("shop_count", 0)
        key = f"in_{n}_shops"
        stats["product_distribution"][key] = (
            stats["product_distribution"].get(key, 0) + 1
        )

    # Initialize shop stats
    shops = all_shops
    for shop in shops:
        stats["shops"][shop] = {
            "product_count": 0,
            "available_count": 0,
            "total_price": 0.0,
            "average_price": 0.0,
            "cheapest_product_count": 0,  # Times this shop was cheapest
            "discount_count": 0,
            "total_discount_value": 0.0,
            "average_discount_percent": 0.0,
            "sum_discount_percent": 0.0,  # Temp for calculation
        }

    for p in products:
        if not p:
            continue

        shop_data = p.get("shops", {})

        # Phase 1: Determine cheapest price for this product across all shops
        min_price = float("inf")
        cheapest_shops_for_item = []

        for shop, data in shop_data.items():
            price = data.get("price")
            if price is not None and isinstance(price, (int, float)) and price > 0:
                if price < min_price:
                    min_price = price
                    cheapest_shops_for_item = [shop]
                elif price == min_price:
                    cheapest_shops_for_item.append(shop)

        # Phase 2: Update per-shop stats
        for shop, data in shop_data.items():
            if shop not in stats["shops"]:
                continue

            s_stats = stats["shops"][shop]
            s_stats["product_count"] += 1

            # Availability
            if data.get("available") is True:
                s_stats["available_count"] += 1

            # Price
            price = data.get("price")
            if price is not None and isinstance(price, (int, float)):
                s_stats["total_price"] += price

                # Cheapest count
                if shop in cheapest_shops_for_item:
                    s_stats["cheapest_product_count"] += 1

                # Discount
                old_price = data.get("old_price")
                if (
                    old_price is not None
                    and isinstance(old_price, (int, float))
                    and old_price > price
                ):
                    s_stats["discount_count"] += 1
                    discount = old_price - price
                    s_stats["total_discount_value"] += discount
                    if old_price > 0:
                        pct = (discount / old_price) * 100
                        s_stats["sum_discount_percent"] += pct

    # Phase 3: Finalize averages and globals
    best_avail_count = -1

    for shop in all_shops:
        s = stats["shops"][shop]
        count = s["product_count"]

        # Averages
        if count > 0:
            s["average_price"] = round(s["total_price"] / count, 3)

        if s["discount_count"] > 0:
            s["average_discount_percent"] = round(
                s["sum_discount_percent"] / s["discount_count"], 2
            )

        # Cleanup temp
        del s["sum_discount_percent"]
        s["total_discount_value"] = round(s["total_discount_value"], 3)
        s["total_price"] = round(s["total_price"], 3)

        # Global: Cheapest Basket (Total cost of buying ALL items at this shop)
        if (
            s["total_price"] < stats["global"]["cheapest_basket"]["total_cost"]
            and count > 0
        ):
            stats["global"]["cheapest_basket"]["total_cost"] = s["total_price"]
            stats["global"]["cheapest_basket"]["shop"] = shop

        # Global: Best Availability
        if s["available_count"] > best_avail_count:
            best_avail_count = s["available_count"]
            stats["global"]["best_availability"]["count"] = best_avail_count
            stats["global"]["best_availability"]["shop"] = shop

    return stats


def delete_previous_merge(output_path: Path) -> None:
    """
    Delete previous merged file if it exists.

    Args:
        output_path: Path to merged file
    """
    if output_path.exists():
        try:
            output_path.unlink()
            logger.info(f"Deleted previous merged file: {output_path}")
        except Exception as e:
            logger.warning(f"Failed to delete previous merged file: {e}")


def save_merged_file(products: List[Dict], summary: Dict, output_path: Path) -> None:
    """
    Save merged data to NDJSON file and summary to JSON file.

    Args:
        products: List of merged product dictionaries
        summary: Metadata summary dictionary
        output_path: Path to products output file (will be NDJSON)
    """
    # Ensure directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Save Products (JSON Array)
    temp_path = output_path.with_suffix(".json.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)
        temp_path.rename(output_path)
        logger.info(f"✓ Saved merged products (JSON Array): {output_path}")
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise RuntimeError(f"Failed to save merged products: {e}")

    # 2. Save Summary (JSON)
    summary_path = output_path.parent / "products_merged_summary.json"
    temp_summary = summary_path.with_suffix(".json.tmp")
    try:
        with open(temp_summary, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        temp_summary.rename(summary_path)
        logger.info(f"✓ Saved merged summary: {summary_path}")
    except Exception as e:
        if temp_summary.exists():
            temp_summary.unlink()
        logger.error(f"Failed to save summary: {e}")


def merge_latest_products() -> Dict:
    """
    Main merge function: loads latest files, merges by SKU, saves output.
    Products are included if they exist in at least MIN_SHOPS_REQUIRED shops.

    Returns:
        Dictionary with merge statistics

    Raises:
        RuntimeError: If merge fails
    """
    logger.info("=" * 70)
    logger.info("🔄 PRODUCT MERGE STARTED")
    logger.info("=" * 70)
    logger.info(f"  Sources: {', '.join(REQUIRED_SOURCES)}")
    logger.info(f"  Minimum shops per product: {MIN_SHOPS_REQUIRED}")

    # Step 1: Find latest files for each source (skip sources with no data)
    logger.info("\nStep 1: Finding latest product files...")
    source_files = {}
    skipped_sources = []

    for source in REQUIRED_SOURCES:
        filepath = find_latest_product_file(source)
        if filepath is None:
            logger.warning(f"  ⚠️  {source}: No data found, skipping")
            skipped_sources.append(source)
            continue
        source_files[source] = filepath
        logger.info(f"  {source}: {filepath}")

    if len(source_files) < MIN_SHOPS_REQUIRED:
        raise RuntimeError(
            f"Only {len(source_files)} sources have data, need at least {MIN_SHOPS_REQUIRED}. "
            f"Missing: {', '.join(skipped_sources)}"
        )

    # Step 2: Load and validate files
    logger.info("\nStep 2: Loading product files...")
    source_data = {}

    for source, filepath in source_files.items():
        try:
            data = load_product_file(filepath)
            source_data[source] = data
            logger.info(f"  {source}: {len(data['products'])} products loaded")
        except ValueError as e:
            logger.warning(f"  ⚠️  {source}: Failed to load ({e}), skipping")
            skipped_sources.append(source)

    if len(source_data) < MIN_SHOPS_REQUIRED:
        raise RuntimeError(
            f"Only {len(source_data)} sources loaded successfully, need at least {MIN_SHOPS_REQUIRED}"
        )

    # Step 3: Index products by SKU
    logger.info("\nStep 3: Indexing products by SKU...")
    indexes = {}

    for source, data in source_data.items():
        # Deduplicate first
        unique_products = deduplicate_products(data["products"], source)

        # Then index by SKU
        index = index_by_sku(unique_products)
        indexes[source] = index
        logger.info(f"  {source}: {len(index)} unique products with valid SKU")

    # Step 4: Find qualifying SKUs (in >= MIN_SHOPS_REQUIRED shops)
    logger.info(f"\nStep 4: Finding SKUs in >= {MIN_SHOPS_REQUIRED} shops...")
    qualifying_skus = find_qualifying_skus(indexes, MIN_SHOPS_REQUIRED)

    if len(qualifying_skus) == 0:
        logger.warning(f"⚠️  No products found in >= {MIN_SHOPS_REQUIRED} shops!")

    # Step 5: Merge products
    logger.info("\nStep 5: Merging products...")
    merged_products = []

    for canonical_sku, shop_raw_skus in qualifying_skus.items():
        # Collect product data from each shop using its raw SKU
        shop_products = {}
        for shop_name, raw_sku in shop_raw_skus.items():
            shop_products[shop_name] = indexes[shop_name][raw_sku]

        merged_product = merge_product_data(canonical_sku, shop_products)
        merged_products.append(merged_product)

    logger.info(f"  Merged {len(merged_products)} products")

    # Step 6: Build output structure
    active_shops = list(source_data.keys())
    analytics = calculate_analytics(merged_products, active_shops)

    summary = {
        "merged_at": datetime.now().isoformat(),
        "source_files": {
            source: str(filepath) for source, filepath in source_files.items()
        },
        "total_products": len(merged_products),
        "min_shops_required": MIN_SHOPS_REQUIRED,
        "active_sources": active_shops,
        "skipped_sources": skipped_sources,
        "merge_stats": {f"{source}_total": len(indexes[source]) for source in indexes},
        "analytics": analytics,
    }
    summary["merge_stats"]["qualifying_products"] = len(qualifying_skus)

    # Step 7: Delete previous merge file
    logger.info("\nStep 6: Managing file lifecycle...")
    delete_previous_merge(MERGED_FILE)

    # Step 8: Save new merged file and Summary
    logger.info("Step 7: Saving merged files...")
    save_merged_file(merged_products, summary, MERGED_FILE)

    logger.info("\n" + "=" * 70)
    logger.info("✅ PRODUCT MERGE COMPLETE")
    logger.info("=" * 70)
    logger.info(f"Total products merged: {len(merged_products)}")
    logger.info(f"Active sources: {', '.join(active_shops)}")
    logger.info(f"Output: {MERGED_FILE}")
    logger.info("")

    return {
        "success": True,
        "total_products": len(merged_products),
        "output_path": str(MERGED_FILE),
        "source_files": source_files,
    }


import traceback


def main():
    """CLI entry point."""
    try:
        result = merge_latest_products()
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Merge failed: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
