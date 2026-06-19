#!/usr/bin/env python3
"""
Debug script: open benyaghlane category page in Playwright,
log ALL network responses so we can see what TikTak endpoints fire.
"""
import asyncio
import json

async def main():
    from playwright.async_api import async_playwright

    url = "https://www.benyaghlane.tn/product-list/57230/les-salaisons"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        page = await ctx.new_page()

        all_responses = []

        async def on_response(response):
            resp_url = response.url
            status = response.status
            ct = response.headers.get("content-type", "")
            entry = {"url": resp_url, "status": status, "ct": ct}

            if "json" in ct and status == 200:
                try:
                    body = await response.json()
                    # Summarize body
                    if isinstance(body, list):
                        entry["body_type"] = f"list[{len(body)}]"
                        if body and isinstance(body[0], dict):
                            entry["first_keys"] = list(body[0].keys())[:10]
                    elif isinstance(body, dict):
                        entry["body_type"] = "dict"
                        entry["keys"] = list(body.keys())[:15]
                        for k, v in body.items():
                            if isinstance(v, list) and v:
                                entry[f"key_{k}_len"] = len(v)
                                if isinstance(v[0], dict):
                                    entry[f"key_{k}_first_keys"] = list(v[0].keys())[:10]
                except Exception as e:
                    entry["parse_error"] = str(e)

            all_responses.append(entry)

        page.on("response", on_response)

        print(f"Loading: {url}")
        print("Waiting for networkidle (up to 60s)...")
        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print(f"goto error: {e}")

        await page.wait_for_timeout(3000)

        print(f"\n{'='*70}")
        print(f"TOTAL RESPONSES: {len(all_responses)}")
        print(f"{'='*70}\n")

        print("--- ALL JSON responses ---")
        json_responses = [r for r in all_responses if "json" in r.get("ct", "")]
        if not json_responses:
            print("  (none)")
        for r in json_responses:
            print(f"  [{r['status']}] {r['url']}")
            for k, v in r.items():
                if k not in ("url", "status", "ct"):
                    print(f"        {k}: {v}")
            print()

        print("--- tiktak.space responses (all) ---")
        tiktak = [r for r in all_responses if "tiktak" in r.get("url", "")]
        if not tiktak:
            print("  (none — no tiktak.space calls at all!)")
        for r in tiktak:
            print(f"  [{r['status']}] {r['url']}")
            for k, v in r.items():
                if k not in ("url", "status"):
                    print(f"        {k}: {v}")
            print()

        print("--- benyaghlane.tn API calls ---")
        api = [r for r in all_responses if "benyaghlane" in r.get("url", "") and "api" in r.get("url","").lower()]
        if not api:
            print("  (none)")
        for r in api:
            print(f"  [{r['status']}] {r['url']}")

        # Also check page HTML for product links
        html = await page.content()
        import re
        product_links = re.findall(r'href=["\']([^"\']*product[^"\']*)["\']', html)
        print(f"\n--- product links in rendered HTML: {len(product_links)} ---")
        for l in product_links[:20]:
            print(f"  {l}")

        await browser.close()

asyncio.run(main())
