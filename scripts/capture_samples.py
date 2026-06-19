import re
from pathlib import Path

import requests


TARGETS = {
    "expert_gaming": "https://www.expert-gaming.tn/./pc-gaming-bureautique/",
    "scoop": "https://www.scoopgaming.com.tn/56-pc-gamer",
    "spacenet": "https://spacenet.tn/18-ordinateur-portable",
    "batam": "https://batam.com.tn/chauffage-et-climatisation/chauffage.html",
    "geant": "https://www.geantdrive.tn/tunis-city/333-epicerie-et-boissons",
    "mapara": "https://www.maparatunisie.tn/categorie-produit/visages/soins-hydratants-et-nourrissants/",
    "wiki": "https://wiki.tn/smartphone-mobile",
}

PATS = [
    re.compile(r'href="(https?://[^"]+/product/[^"]+)"', re.I),
    re.compile(r'href="(https?://www\.maparatunisie\.tn/produit/[^"]+/?)"', re.I),
    re.compile(r'href="(https?://www\.scoopgaming\.com\.tn/\d+[^"]+\.html)"', re.I),
    re.compile(r'href="(https?://wiki\.tn/[^"]+/?)"', re.I),
    re.compile(r'href="(https?://[^"]+\.html)"', re.I),
]


def main():
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "fr,en;q=0.8"}
    for shop, url in TARGETS.items():
        out = Path("data") / shop / "html"
        out.mkdir(parents=True, exist_ok=True)
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if not resp.ok or not resp.text:
                print(shop, "NO_LISTING", resp.status_code)
                continue
            listing_html = resp.text
            (out / "listing_sample_1.html").write_text(listing_html, encoding="utf-8")
            print(shop, "LISTING", resp.status_code, len(listing_html))

            detail_url = None
            for pat in PATS:
                m = pat.search(listing_html)
                if m:
                    detail_url = m.group(1)
                    break
            if not detail_url:
                print(shop, "NO_DETAIL_LINK")
                continue
            detail_resp = requests.get(detail_url, headers=headers, timeout=30)
            if detail_resp.ok and detail_resp.text:
                (out / "detail_sample_1.html").write_text(
                    detail_resp.text, encoding="utf-8"
                )
                print(shop, "DETAIL", detail_resp.status_code, len(detail_resp.text))
            else:
                print(shop, "NO_DETAIL", detail_url, detail_resp.status_code)
        except Exception as exc:
            print(shop, "ERR", exc)


if __name__ == "__main__":
    main()
