"""
find_matched_products.py
========================
Find cross-shop matched products (available, in-stock, price > 0) in:
  Electroménager, TV/son/photo, Informatique, Bureau/impression/scolaire,
  Téléphonie/tablettes, Gaming, Maison/jardin/bricolage,
  Énergie/alimentation électrique, Auto/moto/mobilité

Matching strategy (high-accuracy, two passes):
  1. Identical normalized SKU  (len >= 4, cross-shop only)
  2. Fuzzy name match           (token_sort_ratio >= FUZZY_THRESHOLD)
     + same broad category group

Outputs  (data/matched_output/):
  matched_products.jsonl   — one JSON cluster per line
  match_stats.json         — per-shop cross-match summary
"""

import hashlib
import json
import pathlib
import pickle
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime

from rapidfuzz import fuzz

# Force UTF-8 stdout so progress prints don't crash on Windows cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CACHE_DIR = pathlib.Path("data/matched_output/_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _print(*a, **kw):
    kw.setdefault("flush", True)
    print(*a, **kw)

# ── Configuration ─────────────────────────────────────────────────────────────

DATA_DIR = pathlib.Path("data")
OUTPUT_DIR = pathlib.Path("data/matched_output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FUZZY_THRESHOLD = 99          # token_sort_ratio — very strict, near-identical only
MIN_NAME_TOKENS = 3           # skip fuzzy if product name has fewer tokens
MIN_CLUSTER_SHOPS = 2         # only keep clusters with ≥ 2 distinct shops

# ── Category whitelist ────────────────────────────────────────────────────────
# Exact normalized (no accent, lowercase) top_category values to include.
# Covers all actual values seen in the data for the 9 requested super-categories.

TARGET_CATEGORIES: set[str] = {
    # ── Electroménager ────────────────────────────────────────────────────────
    "electromenager", "electromenagers", "gros electromenager", "gros electro",
    "petit electromenager", "petit electromenagers", "petit electro",
    "petits electromenagers", "petits appareils de cuisine",
    "machine a laver", "refrigerateur", "lave vaisselle",
    "congelateur", "cuisiniere", "four", "micro-onde",
    "climatisation", "clim", "climatiseur",
    "aspirateur", "fer a repasser",
    "preparation culinaire",
    # ── TV / son / photo ─────────────────────────────────────────────────────
    "tv | photo & son", "tv-son-photos", "tv-son-photo", "tv son photo",
    "son & image", "image & son", "son",
    "television", "tv", "photo",
    "audio", "video",
    # ── Informatique ─────────────────────────────────────────────────────────
    "informatique",
    "pc portable", "pc portable gamer", "pc gamer",
    "ordinateur portable", "ordinateur de bureau",
    "composants", "composant pc de bureau",
    "stockage",
    "peripheriques", "accessoires ordinateur",
    "imprimantes", "photocopieurs",
    "apple",
    # ── Bureau / impression / scolaire ────────────────────────────────────────
    "impression", "bureautique", "bureautique et fourniture scolaire",
    "bureautique & fourniture scolaire",
    "papeterie", "cahiers, blocs & papiers",
    "autres fournitures scolaires",
    # ── Téléphonie / tablettes / objets connectés ─────────────────────────────
    "telephonie", "telephonie et tablette", "telephonie & tablette",
    "telephonie | tablettes",
    "telephonie & montre connectee", "telephonie et montres connectees",
    "telephonie, montre connectee et accessoires",
    "telephonie, tablettes et objets connectes",
    "smartphone", "smartphone & mobile",
    "iphone",
    "tablettes tactiles",
    "accessoires telephonie",
    # ── Gaming / consoles ─────────────────────────────────────────────────────
    "gaming", "accessoires gaming", "console gaming",
    "jeux et jouet", "jeux & jouets",
    # ── Maison / jardin / bricolage ───────────────────────────────────────────
    "maison", "maison | brico & animalerie", "maison, jardin & brico",
    "maison et decoration", "meuble maison", "meuble", "meubles",
    "jardin", "bricolage & jardin",
    "electricite", "electricite & domotique",
    "outillage", "outillage a main",
    "quincaillerie", "plomberie", "menuiserie",
    "meuble & bricolage",
    "arts de la table, vaisselle et ustensiles de cuisine",
    # ── Energie / alimentation électrique ────────────────────────────────────
    "solaire", "solaires", "capteurs",
    "electricite",               # also in maison but duplicates are fine in a set
    "alimentation", "ups",
    # ── Auto / moto / mobilité ────────────────────────────────────────────────
    "auto, bricolage et plein air", "moto", "moto | sports & loisirs",
    "auto",
    # ── Réseau / sécurité (often bundled with IT) ─────────────────────────────
    "securite & reseaux", "reseaux & securite", "reseau & securite",
    "reseaux-securite", "reseaux et securite", "reseaux, securite",
}

# Map each TARGET_CATEGORY to a broad group for same-group fuzzy-only blocking
CATEGORY_GROUP: dict[str, str] = {}
_GROUP_MAP = [
    ("electro", [
        "electromenager","electromenagers","gros electromenager","gros electro",
        "petit electromenager","petit electromenagers","petit electro",
        "petits electromenagers","petits appareils de cuisine",
        "machine a laver","refrigerateur","lave vaisselle","congelateur",
        "cuisiniere","four","micro-onde","climatisation","clim","climatiseur",
        "aspirateur","fer a repasser","preparation culinaire",
    ]),
    ("tv_son", [
        "tv | photo & son","tv-son-photos","tv-son-photo","tv son photo",
        "son & image","image & son","son","television","tv","photo","audio","video",
    ]),
    ("informatique", [
        "informatique","pc portable","pc portable gamer","pc gamer",
        "ordinateur portable","ordinateur de bureau","composants",
        "composant pc de bureau","stockage","peripheriques",
        "accessoires ordinateur","imprimantes","photocopieurs","apple",
        "securite & reseaux","reseaux & securite","reseau & securite",
        "reseaux-securite","reseaux et securite","reseaux, securite",
    ]),
    ("bureau", [
        "impression","bureautique","bureautique et fourniture scolaire",
        "bureautique & fourniture scolaire","papeterie",
        "cahiers, blocs & papiers","autres fournitures scolaires",
    ]),
    ("telephonie", [
        "telephonie","telephonie et tablette","telephonie & tablette",
        "telephonie | tablettes","telephonie & montre connectee",
        "telephonie et montres connectees",
        "telephonie, montre connectee et accessoires",
        "telephonie, tablettes et objets connectes",
        "smartphone","smartphone & mobile","iphone","tablettes tactiles",
        "accessoires telephonie",
    ]),
    ("gaming", [
        "gaming","accessoires gaming","console gaming","jeux et jouet","jeux & jouets",
    ]),
    ("maison", [
        "maison","maison | brico & animalerie","maison, jardin & brico",
        "maison et decoration","meuble maison","meuble","meubles",
        "jardin","bricolage & jardin","electricite","electricite & domotique",
        "outillage","outillage a main","quincaillerie","plomberie","menuiserie",
        "meuble & bricolage",
        "arts de la table, vaisselle et ustensiles de cuisine",
    ]),
    ("energie", [
        "solaire","solaires","capteurs","alimentation","ups",
    ]),
    ("auto", [
        "auto, bricolage et plein air","moto","moto | sports & loisirs","auto",
    ]),
]
for grp, cats in _GROUP_MAP:
    for c in cats:
        CATEGORY_GROUP[c] = grp

# ── Availability helpers ──────────────────────────────────────────────────────

_OUT_PATTERNS = re.compile(
    r"rupture|hors.?stock|epuise|épuis|out.?of.?stock|en arrivage|arrivage|"
    r"sur commande|disponible sur commande|indisponible|non disponible|purge|"
    r"backorder|on.?backorder|bient[oô]t|pr[ée].?commande|pre.?order|"
    r"later.?stock",
    re.IGNORECASE,
)
_IN_PATTERNS = re.compile(
    r"en stock|disponible|in.?stock|available|derniers? articles?|"
    r"derniere piece|derni.res pi.ces|en magasin",
    re.IGNORECASE,
)


def is_available(prod: dict) -> bool:
    text = str(prod.get("availability") or "")
    # Defense in depth: trust availability TEXT over boolean.
    # Some scrapers wrongly set available=True when the page says
    # "En arrivage" / "Sur commande" / etc. Block those even if bool=True.
    if text and _OUT_PATTERNS.search(text):
        return False

    avail_bool = prod.get("available")
    if avail_bool is True:
        return True
    if avail_bool is False:
        return False

    if not text or text in ("None", "null", ""):
        # Strict: no availability info AND no boolean => exclude.
        return False

    if _IN_PATTERNS.search(text):
        return True
    # Unknown text — conservative: exclude
    return False


# ── Text normalization ────────────────────────────────────────────────────────

def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def normalize_text(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", _strip_accents(s).lower()).strip()


def normalize_sku(sku: str) -> str:
    if not sku:
        return ""
    return re.sub(r"[\s\-\./,]", "", sku.lower().strip())


# Tokens to strip from names before fuzzy matching
_NOISE = re.compile(
    r"\b(neuf|new|tunisie|tunisian|tun|pas cher|promo|solde|"
    r"garantie|officiel|original|import|fr|eu|version|vente|"
    r"achat|livraison|gratuite?|gratuit)\b",
    re.IGNORECASE,
)

def clean_name(name: str) -> str:
    n = normalize_text(name or "")
    n = _NOISE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# ── Load products ─────────────────────────────────────────────────────────────

_SKIP_DIRS = {
    "matched_output", "availability_history", "price_history",
    "products_added", "products_removed", "state",
    # User-excluded shops:
    "electrohadjkacem",
}


def load_all_products() -> list[dict]:
    all_products: list[dict] = []
    stats = defaultdict(int)

    for shop_dir in sorted(DATA_DIR.iterdir()):
        if not shop_dir.is_dir() or shop_dir.name in _SKIP_DIRS:
            continue
        runs = sorted(
            d for d in shop_dir.iterdir()
            if d.is_dir() and d.name[:4].isdigit()
        )
        if not runs:
            continue
        # Pick the latest run that has actual product data (not just an empty
        # folder from an aborted scrape). Prefer products_detailed.json,
        # fall back to products.json.
        latest = None
        for cand in reversed(runs):
            if (cand / "products_detailed.json").exists() or (cand / "products.json").exists():
                latest = cand
                break
        if latest is None:
            continue

        for fname in ("products_detailed.json", "products.json"):
            pf = latest / fname
            if not pf.exists():
                continue
            try:
                raw = json.loads(pf.read_text(encoding="utf-8"))
            except Exception as e:
                _print(f"  [WARN] {shop_dir.name}: {e}", file=sys.stderr)
                break

            for prod in raw:
                price = prod.get("price")
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None

                if not price or price <= 0:
                    stats["no_price"] += 1
                    continue

                top_cat_raw = (prod.get("top_category") or "").strip()
                top_cat_norm = normalize_text(top_cat_raw)

                if top_cat_norm not in TARGET_CATEGORIES:
                    stats["wrong_cat"] += 1
                    continue

                if not is_available(prod):
                    stats["out_of_stock"] += 1
                    continue

                shop_name = prod.get("shop") or shop_dir.name
                name = (prod.get("title") or prod.get("name") or "").strip()
                sku_raw = prod.get("sku") or prod.get("normalized_sku") or ""

                all_products.append({
                    "shop": shop_name,
                    "name": name,
                    "name_clean": clean_name(name),
                    "sku": normalize_sku(sku_raw),
                    "price": price,
                    "old_price": prod.get("old_price"),
                    "url": prod.get("url") or "",
                    "top_category": top_cat_raw,
                    "top_category_norm": top_cat_norm,
                    "low_category": prod.get("low_category") or "",
                    "availability": prod.get("availability") or "",
                    "scraped_at": prod.get("scraped_at") or latest.name,
                })
            break  # don't load both files for same shop

    _print(f"Loaded {len(all_products):,} in-scope available products")
    _print(f"  Excluded: no_price={stats['no_price']:,} | out_of_stock={stats['out_of_stock']:,} | wrong_cat={stats['wrong_cat']:,}")
    return all_products


# ── Union-Find ────────────────────────────────────────────────────────────────

class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        return True


# ── Matching ──────────────────────────────────────────────────────────────────

def _products_fingerprint(products: list[dict]) -> str:
    h = hashlib.sha1()
    h.update(str(len(products)).encode())
    for p in products:
        h.update(f"{p['shop']}|{p['sku']}|{p['name_clean']}|".encode("utf-8"))
    return h.hexdigest()[:16]


def _save_checkpoint(path: pathlib.Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)


def _load_checkpoint(path: pathlib.Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        _print(f"  [WARN] could not load checkpoint {path.name}: {e}")
        return None


def build_clusters(products: list[dict]) -> list[dict]:
    n = len(products)
    fp = _products_fingerprint(products)
    ckpt_path = CACHE_DIR / f"matching_{fp}.pkl"
    _print(f"  Checkpoint: {ckpt_path.name}")

    state = _load_checkpoint(ckpt_path) or {}
    if state.get("fingerprint") != fp:
        state = {"fingerprint": fp}

    if "uf_parent" in state:
        uf = UnionFind(n)
        uf.parent = state["uf_parent"]
        uf.rank = state["uf_rank"]
        _print(f"  Resumed union-find from checkpoint")
    else:
        uf = UnionFind(n)

    # ── Pass 1: SKU match (exact, cross-shop) ─────────────────────────────────
    if state.get("pass1_done"):
        _print(f"  Pass 1 — SKU: {state['sku_pairs']:,} pairs (cached)")
    else:
        sku_index: dict[str, list[int]] = defaultdict(list)
        for i, p in enumerate(products):
            sku = p["sku"]
            if not sku:
                continue
            # All-digit SKUs collide easily across shops (category codes, IDs)
            # so require >= 8 chars for digit-only; >= 6 otherwise.
            if sku.isdigit():
                if len(sku) < 8:
                    continue
            elif len(sku) < 6:
                continue
            sku_index[sku].append(i)

        sku_pairs = 0
        for sku, idxs in sku_index.items():
            if len(idxs) < 2:
                continue
            for a in range(len(idxs)):
                for b in range(a + 1, len(idxs)):
                    ia, ib = idxs[a], idxs[b]
                    if products[ia]["shop"] != products[ib]["shop"]:
                        if uf.union(ia, ib):
                            sku_pairs += 1

        state.update({
            "pass1_done": True,
            "sku_pairs": sku_pairs,
            "uf_parent": uf.parent,
            "uf_rank": uf.rank,
            "done_groups": [],
            "fuzzy_pairs": 0,
            "total_cmp": 0,
        })
        _save_checkpoint(ckpt_path, state)
        _print(f"  Pass 1 — SKU: {sku_pairs:,} pairs linked (checkpointed)")

    # ── Pass 2: Fuzzy name match (blocked by category group) ─────────────────
    by_group: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(products):
        grp = CATEGORY_GROUP.get(p["top_category_norm"], "other")
        by_group[grp].append(i)

    done_groups = set(state.get("done_groups", []))
    fuzzy_pairs = state.get("fuzzy_pairs", 0)
    total_cmp = state.get("total_cmp", 0)

    # Largest groups last? No — do smallest first so quick wins land in checkpoint.
    group_order = sorted(by_group.keys(), key=lambda g: len(by_group[g]))

    for grp in group_order:
        idxs = by_group[grp]
        if grp in done_groups:
            _print(f"  [skip] group '{grp}' ({len(idxs):,} items) — cached")
            continue
        if len(idxs) < 2:
            done_groups.add(grp)
            continue

        by_shop: dict[str, list[int]] = defaultdict(list)
        for i in idxs:
            by_shop[products[i]["shop"]].append(i)

        shop_list = list(by_shop.keys())
        grp_pairs_before = fuzzy_pairs
        grp_cmp_before = total_cmp
        t_grp = datetime.now()
        _print(f"  [grp] '{grp}': {len(idxs):,} items / {len(shop_list)} shops — matching...")

        for si in range(len(shop_list)):
            for sj in range(si + 1, len(shop_list)):
                shop_a, shop_b = shop_list[si], shop_list[sj]
                for ia in by_shop[shop_a]:
                    name_a = products[ia]["name_clean"]
                    if len(name_a.split()) < MIN_NAME_TOKENS:
                        continue
                    for ib in by_shop[shop_b]:
                        if uf.find(ia) == uf.find(ib):
                            continue
                        name_b = products[ib]["name_clean"]
                        if len(name_b.split()) < MIN_NAME_TOKENS:
                            continue
                        total_cmp += 1
                        if fuzz.token_sort_ratio(name_a, name_b) >= FUZZY_THRESHOLD:
                            if uf.union(ia, ib):
                                fuzzy_pairs += 1

        done_groups.add(grp)
        state.update({
            "done_groups": list(done_groups),
            "fuzzy_pairs": fuzzy_pairs,
            "total_cmp": total_cmp,
            "uf_parent": uf.parent,
            "uf_rank": uf.rank,
        })
        _save_checkpoint(ckpt_path, state)
        elapsed = (datetime.now() - t_grp).total_seconds()
        _print(
            f"  [done] '{grp}': +{fuzzy_pairs - grp_pairs_before:,} pairs "
            f"({total_cmp - grp_cmp_before:,} cmp, {elapsed:.0f}s) — checkpointed"
        )

    _print(f"  Pass 2 — Fuzzy: {fuzzy_pairs:,} pairs linked ({total_cmp:,} comparisons)")

    # ── Collect clusters ──────────────────────────────────────────────────────
    cluster_map: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        cluster_map[uf.find(i)].append(i)

    clusters = []
    for root, members in cluster_map.items():
        shops_in = {products[i]["shop"] for i in members}
        if len(shops_in) < MIN_CLUSTER_SHOPS:
            continue

        rep = max(members, key=lambda i: len(products[i]["name"]))
        prices = sorted(
            [
                {
                    "shop": products[i]["shop"],
                    "price": products[i]["price"],
                    "old_price": products[i]["old_price"],
                    "url": products[i]["url"],
                    "name": products[i]["name"],
                    "sku": products[i]["sku"] or None,
                    "scraped_at": products[i]["scraped_at"],
                }
                for i in members
            ],
            key=lambda x: x["price"],
        )

        clusters.append({
            "cluster_id": f"local_{root:07d}",
            "title": products[rep]["name"],
            "top_category": products[rep]["top_category"],
            "low_category": products[rep]["low_category"],
            "shop_count": len(shops_in),
            "prices": prices,
        })

    clusters.sort(key=lambda c: (-c["shop_count"], c["title"]))
    return clusters


# ── Stats ─────────────────────────────────────────────────────────────────────

def compute_stats(clusters: list[dict], products: list[dict]) -> dict:
    shop_to_clusters: dict[str, set[str]] = defaultdict(set)
    cluster_shops: dict[str, set[str]] = {}

    for c in clusters:
        cid = c["cluster_id"]
        shops = {p["shop"] for p in c["prices"]}
        cluster_shops[cid] = shops
        for s in shops:
            shop_to_clusters[s].add(cid)

    shop_total: dict[str, int] = defaultdict(int)
    for p in products:
        shop_total[p["shop"]] += 1

    stats: dict[str, dict] = {}
    for shop, my_clusters in sorted(shop_to_clusters.items()):
        partners: dict[str, int] = defaultdict(int)
        for cid in my_clusters:
            for other in cluster_shops[cid]:
                if other != shop:
                    partners[other] += 1
        stats[shop] = {
            "total_in_scope_available": shop_total.get(shop, 0),
            "matched_clusters": len(my_clusters),
            "cross_match_partners": dict(
                sorted(partners.items(), key=lambda x: -x[1])
            ),
        }

    return stats


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t0 = datetime.now()
    _print(f"[{t0:%H:%M:%S}] Loading products...")
    products = load_all_products()

    if not products:
        _print("No products found. Check DATA_DIR and filters.")
        return

    _print(f"\n[{datetime.now():%H:%M:%S}] Matching (SKU + fuzzy name, threshold={FUZZY_THRESHOLD})...")
    clusters = build_clusters(products)
    _print(f"  => {len(clusters):,} clusters (>= {MIN_CLUSTER_SHOPS} shops each)")

    # Write JSONL
    out_jsonl = OUTPUT_DIR / "matched_products.jsonl"
    with open(out_jsonl, "w", encoding="utf-8") as f:
        for c in clusters:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    _print(f"\nWrote {out_jsonl}  ({out_jsonl.stat().st_size / 1024:.0f} KB)")

    # Stats
    _print(f"\n[{datetime.now():%H:%M:%S}] Computing stats...")
    stats = compute_stats(clusters, products)

    out_stats = OUTPUT_DIR / "match_stats.json"
    out_stats.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    _print(f"Wrote {out_stats}")

    # Summary table
    _print("\n" + "=" * 70)
    _print(f"{'Shop':<26} {'In-scope avail':>14} {'Matched clusters':>16}")
    _print("-" * 70)
    for shop, s in sorted(stats.items(), key=lambda x: -x[1]["matched_clusters"]):
        _print(f"{shop:<26} {s['total_in_scope_available']:>14,} {s['matched_clusters']:>16,}")

    _print("=" * 70)
    _print(f"\nTotal clusters: {len(clusters):,}")

    # Top pairs
    pairs: dict[tuple, int] = defaultdict(int)
    for shop, s in stats.items():
        for other, cnt in s["cross_match_partners"].items():
            key = tuple(sorted([shop, other]))
            pairs[key] = max(pairs[key], cnt)
    top_pairs = sorted(pairs.items(), key=lambda x: -x[1])[:25]
    _print("\nTop 25 shop pairs by shared cluster count:")
    for (s1, s2), cnt in top_pairs:
        _print(f"  {s1:<24} <-> {s2:<24} : {cnt:,}")

    elapsed = (datetime.now() - t0).seconds
    _print(f"\n[{datetime.now():%H:%M:%S}] Done in {elapsed}s.")


if __name__ == "__main__":
    main()
