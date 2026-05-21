import asyncio
import logging
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from selectolax.parser import HTMLParser
from scraper.sites.wiki import WikiScraper


async def main():
    listing_path = Path("data/wiki/html/listing_sample_1.html")
    if not listing_path.exists():
        print("missing listing_sample_1.html")
        return
    html = listing_path.read_text(encoding="utf-8", errors="ignore")
    tree = HTMLParser(html)
    urls = []
    for a in tree.css("figure.product-card__image a[href]"):
        href = a.attributes.get("href", "")
        if href.startswith("https://wiki.tn/"):
            urls.append(href)

    scraper = WikiScraper(logging.getLogger("wiki_capture"))
    try:
        for url in urls[:40]:
            detail = await scraper.fetch_html(url)
            if detail and len(detail) > 20000:
                Path("data/wiki/html/detail_sample_1.html").write_text(
                    detail, encoding="utf-8"
                )
                print("DETAIL_OK", url, len(detail))
                return
        print("DETAIL_FAIL")
    finally:
        await scraper._close_browser()


if __name__ == "__main__":
    asyncio.run(main())
