"""
Phase B: Live fetch a sample product page per broken shop and dump
key selector probes so we can fix the YAML configs.

For each shop:
  1. Pick a product URL from the latest scrape (where url field is set)
  2. Fetch with Playwright (handles JS + Cloudflare gentler than requests)
  3. Try current config selectors -> mark hit/miss
  4. Also dump candidate selectors found in HTML for title/price/sku/desc
  5. Save HTML to data/_audit/<shop>/page.html for offline inspection
Output: audit_phase_b/<shop>.json with selector status + suggestions.
"""
import asyncio
import json
import pathlib
import re
import sys
import yaml
from playwright.async_api import async_playwright

ROOT = pathlib.Path(".")
OUT_DIR = ROOT / "audit_phase_b"
OUT_DIR.mkdir(exist_ok=True)
HTML_DIR = ROOT / "data" / "_audit"
HTML_DIR.mkdir(parents=True, exist_ok=True)

# Restrict strictly to the user-provided 56-shop list.
USER_SHOPS = [
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
PHASE_A = json.loads((ROOT / "audit_phase_a.json").read_text(encoding="utf-8"))
by_shop = {r["shop"]: r for r in PHASE_A}

CRITICAL = {"name", "title", "price", "sku", "normalized_sku",
            "description", "characteristics", "availability"}

def needs_live_audit(shop: str) -> bool:
    r = by_shop.get(shop)
    if not r or r.get("status") != "AUDITED":
        return True
    for issue in r.get("issues", []):
        f, pct_s = issue.split("=")
        pct = float(pct_s.rstrip("%"))
        if f in CRITICAL and pct >= 20:
            return True
    return False

TARGETS = [s for s in USER_SHOPS if needs_live_audit(s)]
print(f"Shops needing live audit: {len(TARGETS)}")
print("  " + ", ".join(TARGETS))


def latest_run(shop: str) -> pathlib.Path | None:
    d = ROOT / "data" / shop
    if not d.is_dir():
        return None
    runs = sorted(x for x in d.iterdir() if x.is_dir() and x.name[:4].isdigit())
    for cand in reversed(runs):
        if (cand / "products_detailed.json").exists() or (cand / "products.json").exists():
            return cand
    return None


def pick_sample_url(shop: str) -> str | None:
    run = latest_run(shop)
    if not run:
        return None
    for fname in ("products_detailed.json", "products.json"):
        f = run / fname
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            for p in data:
                url = p.get("url") or p.get("link")
                if url and url.startswith("http"):
                    return url
    return None


def load_config(shop: str) -> dict | None:
    candidates = [
        ROOT / "configs" / "sites" / f"{shop}.yaml",
        ROOT / "configs" / "sites" / f"{shop.replace('_', '-')}.yaml",
        ROOT / "configs" / "sites" / f"{shop.replace('-', '_')}.yaml",
    ]
    for c in candidates:
        if c.exists():
            try:
                return yaml.safe_load(c.read_text(encoding="utf-8"))
            except Exception as e:
                return {"_error": str(e)}
    return None


async def probe_shop(browser, shop: str) -> dict:
    url = pick_sample_url(shop)
    cfg = load_config(shop)
    res = {"shop": shop, "sample_url": url, "config_found": cfg is not None}

    if not url:
        res["error"] = "no sample url"
        return res

    sel_cfg = (cfg or {}).get("selectors", {}).get("product_page", {}) if cfg else {}
    res["configured_selectors"] = sel_cfg

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        viewport={"width": 1366, "height": 900},
    )
    page = await context.new_page()
    try:
        await page.goto(url, timeout=45000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        html = await page.content()
    except Exception as e:
        res["fetch_error"] = str(e)
        await context.close()
        return res

    # Save HTML for offline inspection
    (HTML_DIR / shop).mkdir(exist_ok=True)
    (HTML_DIR / shop / "page.html").write_text(html, encoding="utf-8")
    res["html_saved"] = str(HTML_DIR / shop / "page.html")

    # Probe each configured selector
    hits = {}
    for field, selector in sel_cfg.items():
        if not isinstance(selector, str) or not selector.strip():
            hits[field] = {"selector": selector, "found": False, "value": None}
            continue
        try:
            text = await page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    return (el.innerText || el.textContent || '').trim().slice(0, 200);
                }""",
                selector,
            )
            hits[field] = {
                "selector": selector,
                "found": text is not None and text != "",
                "value": text,
            }
        except Exception as e:
            hits[field] = {"selector": selector, "error": str(e)}
    res["probes"] = hits

    # Heuristic suggestions for common selectors
    suggest = await page.evaluate(
        """() => {
            const out = {};
            const grab = (sels) => {
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && (el.innerText || el.textContent || '').trim()) {
                        return {sel: s, val: (el.innerText || el.textContent).trim().slice(0,160)};
                    }
                }
                return null;
            };
            out.title = grab(['h1[itemprop="name"]', 'h1.product-title', 'h1.product_title',
                              'h1.product_name', 'h1.h1', '.product-name h1', '.product__title',
                              'h1.entry-title', 'h1']);
            out.price = grab(['[itemprop="price"]', '.product-price .price', '.price .amount',
                              '.current-price', '.price', '.product_price', 'span.price',
                              '.woocommerce-Price-amount', '.product-info .price']);
            out.sku = grab(['[itemprop="sku"]', '.sku', '.product-reference span',
                            '.product_reference', '.product-sku', '[data-sku]',
                            '.product-meta__reference', '.reference']);
            out.desc = grab(['#description', '.product-description', '.product__description',
                             '.woocommerce-product-details__short-description',
                             '[itemprop="description"]', '.description', '#tab-description']);
            out.avail = grab(['.product-availability', '.availability', '.stock',
                              '#product-availability', '[itemprop="availability"]',
                              '.in-stock', '.out-of-stock']);
            return out;
        }"""
    )
    res["suggestions"] = suggest

    await context.close()
    return res


async def main():
    out = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Sequential to stay polite & avoid Cloudflare flagging
        for shop in TARGETS:
            print(f"  [probe] {shop} ...", flush=True)
            try:
                r = await probe_shop(browser, shop)
            except Exception as e:
                r = {"shop": shop, "error": str(e)}
            (OUT_DIR / f"{shop}.json").write_text(
                json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            out.append(r)
            # Tiny pause between shops
            await asyncio.sleep(1.5)
        await browser.close()

    summary_lines = ["# Phase B — live selector probe", ""]
    for r in out:
        shop = r["shop"]
        if "error" in r or "fetch_error" in r:
            summary_lines.append(f"## {shop}\n  ERROR: {r.get('error') or r.get('fetch_error')}\n")
            continue
        probes = r.get("probes", {})
        broken = [f for f, v in probes.items() if not v.get("found")]
        sugg = r.get("suggestions", {})
        summary_lines.append(f"## {shop}")
        summary_lines.append(f"  sample: {r.get('sample_url')}")
        if broken:
            summary_lines.append(f"  BROKEN selectors: {', '.join(broken)}")
        if sugg:
            for k, v in sugg.items():
                if v:
                    summary_lines.append(f"    suggest {k}: `{v['sel']}` -> {v['val'][:80]}")
        summary_lines.append("")
    (OUT_DIR / "_summary.md").write_text("\n".join(summary_lines), encoding="utf-8")
    print(f"Wrote {OUT_DIR}/ ({len(out)} shops)")


if __name__ == "__main__":
    asyncio.run(main())
