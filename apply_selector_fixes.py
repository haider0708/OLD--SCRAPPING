"""
Apply selector fixes to configs/sites/<shop>.yaml based on Phase B output.

Strategy:
  For each shop with a probe result:
    For each of the 4 matching-critical fields (title, price, sku, availability):
      - If current selector returned a value, leave it.
      - If current selector failed AND suggestions has a working one, update it.

Also: where the YAML has alternate field names (e.g. `description` vs `desc`),
map them.

Saves a unified diff log to selector_fixes.log so we can audit what changed.
"""
import json
import pathlib
import re

CFG_DIR = pathlib.Path("configs/sites")
PROBE_DIR = pathlib.Path("audit_phase_b")
LOG = pathlib.Path("selector_fixes.log")

# Map probe suggestion key -> list of possible YAML keys in product_page
KEY_MAP = {
    "title": ["title", "product_name", "name", "h1"],
    "price": ["price", "current_price", "product_price"],
    "sku":   ["sku", "reference", "product_reference", "product_sku"],
    "desc":  ["description", "product_description", "short_description"],
    "avail": ["availability", "stock", "in_stock"],
}


def shop_to_cfg(shop: str) -> pathlib.Path | None:
    for variant in (shop, shop.replace("_", "-"), shop.replace("-", "_")):
        p = CFG_DIR / f"{variant}.yaml"
        if p.exists():
            return p
    return None


def apply_fixes_for(shop: str, probe: dict, log_lines: list[str]) -> bool:
    cfg_path = shop_to_cfg(shop)
    if not cfg_path:
        log_lines.append(f"[{shop}] no config file")
        return False
    text = cfg_path.read_text(encoding="utf-8")
    original = text

    probes = probe.get("probes", {}) or {}
    sugg = probe.get("suggestions", {}) or {}

    for sugg_key, yaml_keys in KEY_MAP.items():
        suggestion = sugg.get(sugg_key)
        if not suggestion:
            continue
        new_sel = suggestion["sel"]

        # Find the yaml key actually present in the file for this field
        for yk in yaml_keys:
            # Look for `  <yk>: "<old>"` line under product_page section
            # Simple regex (preserves indentation and quoting)
            pat = re.compile(
                rf'(\n[ \t]+{re.escape(yk)}:\s*)"([^"\n]*)"',
                re.MULTILINE,
            )
            m = pat.search(text)
            if not m:
                continue
            old_sel = m.group(2)
            # Only replace if current selector did NOT work in probe
            probe_result = None
            # probes keys may use yaml-style names; check by yk first then sugg_key
            for pname in (yk, sugg_key):
                if pname in probes:
                    probe_result = probes[pname]
                    break
            currently_broken = (probe_result is not None and not probe_result.get("found"))
            if not currently_broken:
                # Field works fine, don't touch
                continue
            # Update
            text = pat.sub(rf'\1"{new_sel}"', text, count=1)
            log_lines.append(f"[{shop}] {yk}: {old_sel!r} -> {new_sel!r}")
            break

    if text != original:
        cfg_path.write_text(text, encoding="utf-8")
        return True
    return False


def main():
    log = ["# Selector auto-fix log", ""]
    changed = 0
    for jf in sorted(PROBE_DIR.glob("*.json")):
        if jf.name.startswith("_"):
            continue
        try:
            probe = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            log.append(f"[{jf.stem}] could not load: {e}")
            continue
        if apply_fixes_for(jf.stem, probe, log):
            changed += 1
    LOG.write_text("\n".join(log), encoding="utf-8")
    print(f"Updated configs: {changed}. See {LOG}")


if __name__ == "__main__":
    main()
