#!/usr/bin/env python3
import json
from pathlib import Path


EXPECTED_PRODUCT_KEYS = {
    "id",
    "url",
    "name",
    "shop",
    "top_category",
    "low_category",
    "subcategory",
}

EXPECTED_DETAIL_KEYS = {
    "url",
    "shop",
    "scraped_at",
    "top_category",
    "low_category",
    "subcategory",
    "available",
}


def latest_run_dir(shop_dir: Path):
    runs = [p for p in shop_dir.iterdir() if p.is_dir() and p.name != "html"]
    if not runs:
        return None
    return sorted(runs, key=lambda p: p.name)[-1]


def validate_shop(shop: str, data_root: Path):
    shop_dir = data_root / shop
    run_dir = latest_run_dir(shop_dir)
    if not run_dir:
        return {"shop": shop, "ok": False, "error": "no_run_dir"}

    product_file = run_dir / "products.json"
    detail_file = run_dir / "products_detailed.json"

    result = {"shop": shop, "ok": True, "run_dir": str(run_dir)}

    try:
        products = json.loads(product_file.read_text(encoding="utf-8"))
        if not isinstance(products, list):
            raise ValueError("products.json not list")
        if products:
            missing = sorted(EXPECTED_PRODUCT_KEYS - set(products[0].keys()))
            result["products_missing_keys"] = missing
            if missing:
                result["ok"] = False
    except Exception as exc:
        result["ok"] = False
        result["products_error"] = str(exc)

    if detail_file.exists():
        try:
            details = json.loads(detail_file.read_text(encoding="utf-8"))
            if not isinstance(details, list):
                raise ValueError("products_detailed.json not list")
            if details:
                missing = sorted(EXPECTED_DETAIL_KEYS - set(details[0].keys()))
                result["details_missing_keys"] = missing
                if missing:
                    result["ok"] = False
        except Exception as exc:
            result["ok"] = False
            result["details_error"] = str(exc)
    else:
        result["details_error"] = "missing_file"
        result["ok"] = False

    return result


def main():
    data_root = Path("data")
    shops = [p.name for p in data_root.iterdir() if p.is_dir() and p.name != "merged"]
    report = [validate_shop(shop, data_root) for shop in sorted(shops)]
    out = Path("data/schema_validation_report.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
