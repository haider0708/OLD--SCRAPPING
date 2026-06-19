#!/usr/bin/env python3
"""
Export TF-IDF merged products to cluster3 MongoDB.
One collection per category:
  merged_tfidf_informatique
  merged_tfidf_gaming
  merged_tfidf_electromenager
  merged_tfidf_parapharmacie
  merged_tfidf_cosmetique
  merged_tfidf_divers
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import certifi
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

load_dotenv()

BASE_DIR = Path(__file__).parent
TFIDF_FILE = BASE_DIR / "data" / "merged" / "products_tfidf_merged.json"

# Category each shop belongs to — mirrors merge_tfidf.py SITES dict
SHOP_CATEGORY = {
    "mytek": "informatique", "tunisianet": "informatique", "technopro": "informatique",
    "spacenet": "informatique", "zoom": "informatique", "allani": "informatique",
    "sigshop": "informatique", "qsnet": "informatique", "techgate": "informatique",
    "acspace": "informatique", "emh": "informatique", "megapc": "informatique",
    "chaktech": "informatique", "techland": "informatique", "bstech": "informatique",
    "itechstore": "informatique", "ispace": "informatique", "bestbuytunisie": "informatique",
    "infotec": "informatique", "carthagoinformatique": "informatique", "imag": "informatique",
    "tunewtec": "informatique", "el_farabi": "informatique", "informatica": "informatique",
    "expert_gaming": "gaming", "psstore": "gaming", "tokyo_store": "gaming",
    "mageekstore": "gaming", "gamershop": "gaming",
    "geant": "electromenager", "darty": "electromenager", "jumbo": "electromenager",
    "graiet": "electromenager", "batam": "electromenager", "electrohadjkacem": "electromenager",
    "electrochaabani": "electromenager", "electrobennjima": "electromenager",
    "maalejaudio": "electromenager", "kamounhome": "electromenager", "koktahome": "electromenager",
    "ikitchen": "electromenager", "dokani": "electromenager", "eleganza": "electromenager",
    "yatoo": "electromenager", "affariyet": "electromenager",
    "benzarti-electromenager": "electromenager",
    "mapara": "parapharmacie", "parafendri": "parapharmacie", "parashop": "parapharmacie",
    "pharmacieplus": "parapharmacie", "pharmashop": "parapharmacie", "parahouse": "parapharmacie",
    "tunisiepara": "parapharmacie", "paraexpert": "parapharmacie", "pointm": "parapharmacie",
    "alarabia": "parapharmacie", "paraland": "parapharmacie", "totaltunisia": "parapharmacie",
    "cosmetique": "cosmetique", "beautystore": "cosmetique",
    "sbs": "divers", "scoop": "divers", "skymill": "divers", "wiki": "divers",
    "promouv": "divers", "bill": "divers", "agora": "divers", "jmb": "divers",
    "taktek": "divers", "krichen": "divers", "topbureau": "divers", "sangour": "divers",
}


def make_logger():
    log = logging.getLogger("export_tfidf")
    log.setLevel(logging.INFO)
    log.handlers = []
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(h)
    return log


logger = make_logger()


def infer_category(record: dict) -> str:
    """
    Pick the dominant category for a merged record by majority-voting
    across the shops it was found in.
    """
    votes: dict[str, int] = {}
    for shop in record.get("found_in_shops", []):
        cat = SHOP_CATEGORY.get(shop, "divers")
        votes[cat] = votes.get(cat, 0) + 1
    if not votes:
        return "divers"
    return max(votes, key=votes.get)


def connect_cluster3() -> tuple:
    """Return (client, db). Raises if env var missing or connection fails."""
    uri = os.getenv("MONGO_URI_3")
    if not uri:
        raise RuntimeError("MONGO_URI_3 not set in .env")
    db_name = os.getenv("MONGO_DB_NAME_3", "Retails")
    client = MongoClient(uri, serverSelectionTimeoutMS=10000, tlsCAFile=certifi.where())
    client.server_info()  # raises if unreachable
    logger.info(f"Connected to cluster3  db={db_name}")
    return client, client[db_name]


def export_to_cluster3():
    logger.info("=" * 70)
    logger.info("EXPORT TF-IDF MERGED  ->  CLUSTER3")
    logger.info("=" * 70)

    # Load merged file
    if not TFIDF_FILE.exists():
        raise FileNotFoundError(f"TF-IDF merged file not found: {TFIDF_FILE}")

    logger.info(f"Loading {TFIDF_FILE} ...")
    with open(TFIDF_FILE, "r", encoding="utf-8") as fh:
        records = json.load(fh)
    logger.info(f"  Loaded {len(records):,} merged records")

    # Split by category
    by_cat: dict[str, list] = {}
    for rec in records:
        cat = infer_category(rec)
        by_cat.setdefault(cat, []).append(rec)

    logger.info("  Distribution by category:")
    for cat in sorted(by_cat):
        logger.info(f"    {cat:20s}: {len(by_cat[cat]):,} records")

    # Connect
    client, db = connect_cluster3()
    now = datetime.now()

    try:
        for cat, docs in sorted(by_cat.items()):
            collection_name = f"merged_tfidf_{cat}"
            coll = db[collection_name]

            # Full replace: drop existing, insert fresh
            coll.delete_many({})

            # Stamp each doc
            for d in docs:
                d["_category"] = cat
                d["_exported_at"] = now

            # Insert in chunks of 500
            CHUNK = 500
            inserted = 0
            for i in range(0, len(docs), CHUNK):
                coll.insert_many(docs[i:i + CHUNK], ordered=False)
                inserted += len(docs[i:i + CHUNK])

            # Index on canonical_id and found_in_shops for fast queries
            coll.create_index("canonical_id")
            coll.create_index("found_in_shops")
            coll.create_index("shop_count")
            coll.create_index("min_price")

            logger.info(f"  [{collection_name}]  {inserted:,} docs written")

        # Also store a summary document
        summary_coll = db["merged_tfidf_summary"]
        summary_coll.delete_many({})
        summary_coll.insert_one({
            "exported_at": now,
            "total_records": len(records),
            "by_category": {cat: len(docs) for cat, docs in by_cat.items()},
            "source_file": str(TFIDF_FILE),
        })
        logger.info("  [merged_tfidf_summary]  summary doc written")

    finally:
        client.close()

    logger.info("=" * 70)
    logger.info("EXPORT COMPLETE")
    logger.info("=" * 70)


if __name__ == "__main__":
    try:
        export_to_cluster3()
        sys.exit(0)
    except Exception as e:
        import traceback
        logger.error(f"FAILED: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)
