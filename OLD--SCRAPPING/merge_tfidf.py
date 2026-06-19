#!/usr/bin/env python3
"""
TF-IDF + Cosine Similarity Product Merge  (v2)
================================================

Strategy:
  Stage 1 — Hard exact-ID match on normalized barcode / EAN / SKU / reference.
             Applied across ALL products. Zero false positives.

  Stage 2 — TF-IDF on ALL remaining unmatched products (not just those without
             an ID). Text = title + name + brand + top_category + low_category
             + subcategory + description/overview (first 200 chars).
             Comparison is done PER CATEGORY (same-category pairs only) to
             prevent cross-category false positives.
             Cosine similarity >= TITLE_THRESHOLD (0.85).

  Hard-ID match takes priority: if Stage 2 finds a pair that was already
  linked by Stage 1, it is ignored (no duplicates).

Excluded sites (alimentation, animaux, mode/vetements, livres):
  benyaghlane, bricola, sweetbaby, bambinos, bb_store, petit_bateau,
  kiabi, capricelingerie, try_and_buy, lesportif, tuttosport, supersport,
  kastelo, mbm, ceresbookshop, alkitab, culturel, oriflame, farmasi,
  drest, lamode, toopty

Output:
  data/merged/products_tfidf_merged.json
  data/merged/products_tfidf_summary.json
"""

import json
import logging
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity



BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
MERGED_DIR = DATA_DIR / "merged"
OUTPUT_FILE = MERGED_DIR / "products_tfidf_merged.json"
SUMMARY_FILE = MERGED_DIR / "products_tfidf_summary.json"

TITLE_THRESHOLD = 0.85   
MIN_SHOPS       = 2      
DESC_CHARS      = 200    
TFIDF_BLOCK     = 1000   



SITES: Dict[str, str] = {
    # informatique
    "mytek": "informatique", "tunisianet": "informatique", "technopro": "informatique",
    "spacenet": "informatique", "zoom": "informatique", "allani": "informatique",
    "sigshop": "informatique", "qsnet": "informatique", "techgate": "informatique",
    "acspace": "informatique", "emh": "informatique", "megapc": "informatique",
    "chaktech": "informatique", "techland": "informatique", "bstech": "informatique",
    "itechstore": "informatique", "ispace": "informatique", "bestbuytunisie": "informatique",
    "infotec": "informatique", "carthagoinformatique": "informatique", "imag": "informatique",
    "tunewtec": "informatique", "el_farabi": "informatique", "informatica": "informatique",
    # gaming
    "expert_gaming": "gaming", "psstore": "gaming", "tokyo_store": "gaming",
    "mageekstore": "gaming", "gamershop": "gaming",
    # electromenager
    "geant": "electromenager", "darty": "electromenager", "jumbo": "electromenager",
    "graiet": "electromenager", "batam": "electromenager",
    "electrohadjkacem": "electromenager", "electrochaabani": "electromenager",
    "electrobennjima": "electromenager", "maalejaudio": "electromenager",
    "kamounhome": "electromenager", "koktahome": "electromenager",
    "ikitchen": "electromenager", "dokani": "electromenager", "eleganza": "electromenager",
    "yatoo": "electromenager", "affariyet": "electromenager",
    "benzarti-electromenager": "electromenager",
    # parapharmacie
    "mapara": "parapharmacie", "parafendri": "parapharmacie", "parashop": "parapharmacie",
    "pharmacieplus": "parapharmacie", "pharmashop": "parapharmacie",
    "parahouse": "parapharmacie", "tunisiepara": "parapharmacie",
    "paraexpert": "parapharmacie", "pointm": "parapharmacie",
    "alarabia": "parapharmacie", "paraland": "parapharmacie",
    "totaltunisia": "parapharmacie",
    # cosmetique
    "cosmetique": "cosmetique", "beautystore": "cosmetique",
    # divers
    "sbs": "divers", "scoop": "divers", "skymill": "divers", "wiki": "divers",
    "promouv": "divers", "bill": "divers", "agora": "divers", "jmb": "divers",
    "taktek": "divers", "krichen": "divers", "topbureau": "divers",
    "informatica": "informatique", "sangour": "divers",
}

# ── Logger ────────────────────────────────────────────────────────────────────

def _make_logger() -> logging.Logger:
    log = logging.getLogger("merge_tfidf")
    log.setLevel(logging.INFO)
    log.handlers = []
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(h)
    return log

logger = _make_logger()

# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_id(raw: str) -> str:
    if not raw:
        return ""
    return "".join(c for c in raw if c.isalnum()).upper()


def extract_id(product: dict) -> str:
    """Best hard identifier: barcode > sku > reference. Returns '' if none."""
    for field in ("barcode", "sku", "reference"):
        val = product.get(field)
        if not val or not isinstance(val, str):
            continue
        val = val.strip()
        norm = normalize_id(val)
        if len(norm) < 4:
            continue
        if norm in ("NONE", "NULL", "NA", "N/A", "REFERENCE", "REF"):
            continue
       
        if val == val.lower() and "-" in val and len(val) > 25 and " " not in val:
            continue
        
        if re.match(r'^(r[ée]f[ée]rence|r[ée]f|sku|ugs|ean|barcode)\s*[:\-]?\s*$', val, re.IGNORECASE):
            continue
        return norm
    return ""


def build_text(product: dict) -> str:
    parts = []
    for field in ("title", "name"):
        v = product.get(field)
        if v and isinstance(v, str) and v.strip():
            parts.append(v.strip())
    for field in ("brand",):
        v = product.get(field)
        if v and isinstance(v, str) and v.strip():
            # repeat brand twice to boost its weight
            parts.append(v.strip())
            parts.append(v.strip())
    for field in ("top_category", "low_category", "subcategory"):
        v = product.get(field)
        if v and isinstance(v, str) and v.strip():
            parts.append(v.strip())
    # description / overview snippet
    for field in ("description", "overview"):
        v = product.get(field)
        if v and isinstance(v, str) and v.strip():
            parts.append(v.strip()[:DESC_CHARS])
            break  # only one
    return " ".join(parts)


def clean_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def find_latest_file(site: str) -> Optional[Path]:
    site_dir = DATA_DIR / site
    if not site_dir.exists():
        return None
    dirs = sorted(
        (d for d in site_dir.iterdir()
         if d.is_dir() and not d.name.startswith(".") and d.name != "html"),
        reverse=True,
    )
    for d in dirs:
        f = d / "products_detailed.json"
        if f.exists():
            return f
    return None


def load_products(filepath: Path) -> List[dict]:
    with open(filepath, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    return data.get("products", [])



class UnionFind:
    def __init__(self, n: int):
        self._p = list(range(n))

    def find(self, x: int) -> int:
        while self._p[x] != x:
            self._p[x] = self._p[self._p[x]]
            x = self._p[x]
        return x

    def union(self, a: int, b: int):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._p[rb] = ra

    def same(self, a: int, b: int) -> bool:
        return self.find(a) == self.find(b)



def stage1_exact(
    all_products: List[Tuple[str, dict, int]]   
) -> Tuple[UnionFind, Dict[str, int]]:         
    """
    Group every product that shares a normalized hard ID.
    Returns a UnionFind over global indices and a dict of id->root index.
    """
    n = len(all_products)
    uf = UnionFind(n)
    id_to_first: Dict[str, int] = {}

    for idx, (shop, product, gidx) in enumerate(all_products):
        norm = extract_id(product)
        if not norm:
            continue
        if norm in id_to_first:
            uf.union(id_to_first[norm], idx)
        else:
            id_to_first[norm] = idx

    return uf, id_to_first




def stage2_tfidf_per_category(
    all_products: List[Tuple[str, dict, int]],
    uf: UnionFind,
    threshold: float = TITLE_THRESHOLD,
) -> int:
    """
    For each category separately:
      - Build TF-IDF matrix of product texts in that category
      - Find pairs from different shops with cosine >= threshold
      - Union them in uf (only if not already in the same group)
    Returns total new unions made.
    """
  
    cat_indices: Dict[str, List[int]] = defaultdict(list)
    for idx, (shop, product, gidx) in enumerate(all_products):
        cat = SITES.get(shop, "divers")
        cat_indices[cat].append(idx)

    total_new_unions = 0

    for cat, indices in sorted(cat_indices.items()):
        if len(indices) < 2:
            continue

        texts = [clean_text(build_text(all_products[i][1])) for i in indices]
        non_empty = [(local_i, t) for local_i, t in enumerate(texts) if t.strip()]
        if len(non_empty) < 2:
            continue

        local_indices = [li for li, _ in non_empty]
        clean_texts   = [t  for _, t  in non_empty]
        global_indices = [indices[li] for li in local_indices]
        shops_list     = [all_products[gi][0] for gi in global_indices]

        logger.info(f"  [{cat}] TF-IDF on {len(clean_texts):,} products...")
        t0 = time.perf_counter()

        vec = TfidfVectorizer(
            analyzer="word",
            ngram_range=(1, 2),
            min_df=1,
            sublinear_tf=True,
            max_features=80_000,
        )
        mat = vec.fit_transform(clean_texts)

        n = mat.shape[0]
        new_unions = 0
        pairs_checked = 0

        for start in range(0, n, TFIDF_BLOCK):
            block = mat[start:start + TFIDF_BLOCK]
            sims = cosine_similarity(block, mat)   
            for bi, row in enumerate(sims):
                gi = start + bi
                for gj in range(gi + 1, n):
                    pairs_checked += 1
                    if row[gj] < threshold:
                        continue
                    
                    if shops_list[gi] == shops_list[gj]:
                        continue
                    real_gi = global_indices[gi]
                    real_gj = global_indices[gj]
                    if not uf.same(real_gi, real_gj):
                        uf.union(real_gi, real_gj)
                        new_unions += 1

        total_new_unions += new_unions
        elapsed = time.perf_counter() - t0
        logger.info(f"  [{cat}] {pairs_checked:,} pairs, {new_unions:,} new matches  [{elapsed:.1f}s]")

    return total_new_unions




def merge_group(
    members: List[Tuple[str, dict]],  
    canonical_id: str,
    match_method: str,
) -> dict:
    
    title = ""
    for _, p in members:
        t = p.get("title") or p.get("name") or ""
        if len(t) > len(title):
            title = t

    shops: Dict[str, dict] = {}
    for shop, p in members:
        if shop in shops:
            continue
        shops[shop] = {
            "url":          p.get("url"),
            "price":        p.get("price"),
            "old_price":    p.get("old_price"),
            "name":         p.get("title") or p.get("name"),
            "sku":          p.get("sku") or p.get("barcode") or p.get("reference"),
            "barcode":      p.get("barcode"),
            "brand":        p.get("brand"),
            "image":        p.get("image"),
            "images":       p.get("images", []),
            "top_category": p.get("top_category"),
            "low_category": p.get("low_category"),
            "availability": p.get("availability"),
            "available":    p.get("available"),
        }

    prices = [
        v["price"] for v in shops.values()
        if v["price"] is not None and isinstance(v["price"], (int, float)) and v["price"] > 0
    ]

    return {
        "canonical_id":   canonical_id,
        "match_method":   match_method,
        "title":          title,
        "found_in_shops": sorted(shops.keys()),
        "shop_count":     len(shops),
        "min_price":      round(min(prices), 3) if prices else None,
        "max_price":      round(max(prices), 3) if prices else None,
        "shops":          shops,
    }




def infer_category(found_in_shops: List[str]) -> str:
    votes: Dict[str, int] = defaultdict(int)
    for shop in found_in_shops:
        votes[SITES.get(shop, "divers")] += 1
    return max(votes, key=votes.get) if votes else "divers"




def run():
    logger.info("=" * 70)
    logger.info("TF-IDF PRODUCT MERGE  v2")
    logger.info("=" * 70)
    logger.info(f"  Sites: {len(SITES)}  |  Min shops: {MIN_SHOPS}  |  Threshold: {TITLE_THRESHOLD}")


    logger.info("\n[1] Loading products...")
    all_products: List[Tuple[str, dict, int]] = []  
    loaded_sites: Dict[str, dict] = {}
    skipped_sites: List[str] = []
    by_category: Dict[str, List[str]] = defaultdict(list)

    for site, category in SITES.items():
        fp = find_latest_file(site)
        if fp is None:
            skipped_sites.append(site)
            continue
        try:
            prods = load_products(fp)
        except Exception as e:
            logger.warning(f"  {site}: failed ({e})")
            skipped_sites.append(site)
            continue

        usable = [p for p in prods if build_text(p).strip()]
        if not usable:
            skipped_sites.append(site)
            continue

        start_idx = len(all_products)
        for p in usable:
            all_products.append((site, p, len(all_products)))

        loaded_sites[site] = {"file": str(fp), "count": len(usable), "category": category}
        by_category[category].append(site)
        logger.info(f"  {site:30s} [{category:15s}]  {len(usable):6,} products")

    total = len(all_products)
    logger.info(f"\n  Total: {total:,} products from {len(loaded_sites)} sites")

    logger.info("\n  Sites by category:")
    for cat in sorted(by_category):
        logger.info(f"    {cat:20s} ({len(by_category[cat])} sites): {', '.join(by_category[cat])}")

   
    logger.info("\n[2] Stage 1 — Hard-ID exact match (all products)...")
    t0 = time.perf_counter()
    uf, id_to_root = stage1_exact(all_products)

   
    id_groups_with_id: Dict[int, List[int]] = defaultdict(list)
    for idx, (shop, product, _) in enumerate(all_products):
        norm = extract_id(product)
        if norm:
            id_groups_with_id[uf.find(idx)].append(idx)

    s1_multi = sum(
        1 for root, members in id_groups_with_id.items()
        if len({all_products[i][0] for i in members}) >= MIN_SHOPS
    )
    logger.info(f"  Hard-ID groups spanning >= {MIN_SHOPS} shops: {s1_multi:,}  [{time.perf_counter()-t0:.2f}s]")

   
    logger.info("\n[3] Stage 2 — TF-IDF per category (all products, threshold={})...".format(TITLE_THRESHOLD))
    t0 = time.perf_counter()
    new_unions = stage2_tfidf_per_category(all_products, uf, threshold=TITLE_THRESHOLD)
    logger.info(f"  Total new TF-IDF unions: {new_unions:,}  [{time.perf_counter()-t0:.1f}s total]")

 
    logger.info("\n[4] Assembling merged records...")
    root_to_members: Dict[int, List[int]] = defaultdict(list)
    for idx in range(total):
        root_to_members[uf.find(idx)].append(idx)

    merged: List[dict] = []
    stat_s1 = stat_s2 = stat_both = stat_dropped = 0

    for root, member_indices in root_to_members.items():
        shops_in_group = {all_products[i][0] for i in member_indices}
        if len(shops_in_group) < MIN_SHOPS:
            stat_dropped += 1
            continue

        has_id = any(extract_id(all_products[i][1]) for i in member_indices)

        
        canonical_id = ""
        for i in member_indices:
            cid = extract_id(all_products[i][1])
            if cid:
                canonical_id = cid
                break
        if not canonical_id:
            
            for i in member_indices:
                t = clean_text(build_text(all_products[i][1]))
                if t:
                    canonical_id = f"TFIDF:{t[:50]}"
                    break

    
        if has_id:
            method = "exact_id"
            stat_s1 += 1
        else:
            method = "tfidf"
            stat_s2 += 1

        members = [(all_products[i][0], all_products[i][1]) for i in member_indices]
        merged.append(merge_group(members, canonical_id, method))

    logger.info(f"  exact_id groups : {stat_s1:,}")
    logger.info(f"  tfidf groups    : {stat_s2:,}")
    logger.info(f"  dropped (1 shop): {stat_dropped:,}")
    logger.info(f"  Total merged    : {len(merged):,}")

    
    dist: Dict[int, int] = defaultdict(int)
    for m in merged:
        dist[m["shop_count"]] += 1
    logger.info("\n  Shop-count distribution:")
    for n in sorted(dist):
        logger.info(f"    In {n:2d} shops: {dist[n]:,}")

    
    logger.info("\n[5] Saving...")
    MERGED_DIR.mkdir(parents=True, exist_ok=True)

    tmp = OUTPUT_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=2)
    if OUTPUT_FILE.exists():
        OUTPUT_FILE.unlink()
    tmp.rename(OUTPUT_FILE)
    logger.info(f"  Saved {len(merged):,} records -> {OUTPUT_FILE}")

    summary = {
        "run_at":            datetime.now().isoformat(),
        "title_threshold":   TITLE_THRESHOLD,
        "min_shops":         MIN_SHOPS,
        "desc_chars":        DESC_CHARS,
        "total_products_in": total,
        "total_merged":      len(merged),
        "exact_id_groups":   stat_s1,
        "tfidf_groups":      stat_s2,
        "new_tfidf_unions":  new_unions,
        "loaded_sites":      loaded_sites,
        "skipped_sites":     skipped_sites,
        "by_category":       {k: v for k, v in by_category.items()},
        "shop_count_dist":   {str(k): v for k, v in dist.items()},
    }
    tmp2 = SUMMARY_FILE.with_suffix(".json.tmp")
    with open(tmp2, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    if SUMMARY_FILE.exists():
        SUMMARY_FILE.unlink()
    tmp2.rename(SUMMARY_FILE)
    logger.info(f"  Summary -> {SUMMARY_FILE}")

    logger.info("\n" + "=" * 70)
    logger.info("DONE")
    logger.info("=" * 70)
    return summary


def main():
    try:
        run()
        sys.exit(0)
    except KeyboardInterrupt:
        logger.info("Interrupted.")
        sys.exit(1)
    except Exception as e:
        import traceback
        logger.error(f"FAILED: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
