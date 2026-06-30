"""
Phase A: Fast file-based selector audit.
For each target shop, load the latest scrape and compute:
  - total products
  - % null/empty name, price (or 0), sku, description, characteristics, availability, image
A field with >30% nulls is flagged as 'BROKEN'; 10-30% as 'WEAK'; <10% 'OK'.
Output: audit_phase_a.json + audit_phase_a.md
"""
import json
import pathlib
from collections import defaultdict

TARGET_SHOPS = [
    "spacenet", "tunisianet", "technopro", "affariyet", "tunewtec", "jumbo",
    "kamounhome", "maalejaudio", "zoom", "bill", "allani", "koktahome",
    "darty", "krichen", "bstech", "agora", "emh", "sbs", "jmb", "itechstore",
    "scoop", "taktek", "wiki", "techland", "electrobennjima", "acspace",
    "chaktech", "expert_gaming", "topbureau", "sigshop", "ispace", "yatoo",
    "batam", "qsnet", "sangour", "alarabia", "tokyo_store", "mytek",
    "bestbuytunisie", "psstore", "informatica", "skymill",
    "benzarti-electromenager", "carthagoinformatique", "dokani",
    "electrochaabani", "electrohadjkacem", "gamershop", "graiet", "imag",
    "infotec", "megapc", "mbm", "techgate", "try_and_buy",
]
DATA_DIR = pathlib.Path("data")
OUT_JSON = pathlib.Path("audit_phase_a.json")
OUT_MD = pathlib.Path("audit_phase_a.md")

FIELDS = ["title", "name", "price", "sku", "normalized_sku", "description",
          "characteristics", "availability", "image", "image_url", "url",
          "top_category", "low_category"]


def latest_run(shop: str) -> pathlib.Path | None:
    d = DATA_DIR / shop
    if not d.is_dir():
        return None
    runs = sorted(x for x in d.iterdir()
                  if x.is_dir() and x.name[:4].isdigit())
    for cand in reversed(runs):
        if (cand / "products_detailed.json").exists() or (cand / "products.json").exists():
            return cand
    return None


def load_products(run_dir: pathlib.Path) -> list[dict]:
    for fname in ("products_detailed.json", "products.json"):
        f = run_dir / fname
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                return []
    return []


def is_empty(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    if isinstance(v, (list, dict)) and len(v) == 0:
        return True
    if isinstance(v, (int, float)) and v == 0:
        return True
    return False


def audit_shop(shop: str) -> dict:
    run = latest_run(shop)
    if not run:
        return {"shop": shop, "status": "NO_DATA", "run": None}
    products = load_products(run)
    if not products:
        return {"shop": shop, "status": "EMPTY", "run": run.name}

    total = len(products)
    field_keys = set()
    for p in products[:200]:
        field_keys.update(p.keys())

    stats = {}
    for f in sorted(field_keys):
        nulls = sum(1 for p in products if is_empty(p.get(f)))
        pct = round(100 * nulls / total, 1)
        if pct >= 50:
            tag = "BROKEN"
        elif pct >= 20:
            tag = "WEAK"
        else:
            tag = "OK"
        stats[f] = {"null_pct": pct, "tag": tag}

    # Key fields summary
    critical = ["name", "title", "price", "sku", "normalized_sku",
                "description", "characteristics", "availability"]
    issues = []
    for cf in critical:
        if cf in stats and stats[cf]["tag"] in ("BROKEN", "WEAK"):
            issues.append(f"{cf}={stats[cf]['null_pct']}%")

    return {
        "shop": shop,
        "status": "AUDITED",
        "run": run.name,
        "total": total,
        "fields": stats,
        "issues": issues,
    }


def main():
    results = []
    for shop in TARGET_SHOPS:
        r = audit_shop(shop)
        results.append(r)
        if r["status"] == "NO_DATA":
            print(f"  [NO_DATA]   {shop}")
        elif r["status"] == "EMPTY":
            print(f"  [EMPTY]     {shop}  run={r['run']}")
        else:
            issues = ", ".join(r["issues"]) if r["issues"] else "clean"
            print(f"  [{r['total']:>5}]    {shop:<28} {r['run']}  -> {issues}")

    OUT_JSON.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    # Markdown report
    lines = ["# Phase A — file-based selector audit", "",
             "| Shop | Run | N | Issues |", "|---|---|---|---|"]
    for r in results:
        if r["status"] == "AUDITED":
            issues = ", ".join(r["issues"]) if r["issues"] else "clean"
            lines.append(f"| {r['shop']} | {r['run']} | {r['total']} | {issues} |")
        else:
            lines.append(f"| {r['shop']} | — | — | **{r['status']}** |")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {OUT_JSON} and {OUT_MD}")


if __name__ == "__main__":
    main()
