import asyncio
import logging
from pathlib import Path

from scraper.sites.mapara import MaparaScraper
from scraper.sites.parafendri import ParafendriScraper
from scraper.sites.parashop import ParashopScraper
from scraper.sites.pharmacieplus import PharmaciePlusScraper
from scraper.product_utils import dedupe_products, finalize_product_record, html_product_metadata


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8", errors="ignore")


def test_mapara_listing_extracts_woocommerce_sku_reference():
    scraper = MaparaScraper(logging.getLogger("test_mapara_listing_identifiers"))
    html = _read("data/mapara/html/listing_sample_1.html")

    products = scraper.extract_products_from_html(html)

    first = next(p for p in products if p.get("id") == "1679")
    assert first["reference"] == "4N966"
    assert first["sku"] == "4N966"
    assert "barcode" not in first


def test_mapara_detail_separates_jsonld_sku_from_gtin13_barcode():
    scraper = MaparaScraper(logging.getLogger("test_mapara_detail_identifiers"))
    html = _read("data/mapara/html/detail_sample_1.html")

    async def _run():
        async def fake_fetch_html(url: str, raise_on_error: bool = False):
            return html

        scraper.fetch_html = fake_fetch_html  # type: ignore[method-assign]
        return await scraper.scrape_product_details(
            "https://www.maparatunisie.tn/produit/cetaphil-lotion-nettoyante-peaux-seches-et-sensibles-500ml/"
        )

    data = asyncio.run(_run())
    assert data["barcode"] == "3499320012430"
    assert data["reference"] == "4N966"
    assert data["sku"] == "4N966"


def test_parafendri_listing_uses_h3_product_title_from_real_frontpage():
    scraper = ParafendriScraper(logging.getLogger("test_parafendri_listing_title"))
    html = _read("data/parafendri/html/frontpage.html")

    products = scraper.extract_products_from_html(html)

    first = next(p for p in products if p.get("id") == "1781")
    assert first["name"] == "XEN INTIMEL - GEL INTIME PH 5.5 100ML"
    assert first["price"] == 5.5
    assert first["old_price"] == 8.5
    assert first["available"] is True


def test_parafendri_detail_moves_numeric_reference_to_barcode():
    scraper = ParafendriScraper(logging.getLogger("test_parafendri_detail_barcode"))
    html = """
    <html><body>
      <div id="product-details" data-product='{
        "id_product": 1781,
        "name": "XEN INTIMEL - GEL INTIME PH 5.5 100ML",
        "reference": "6192440600446",
        "price_amount": 5.5,
        "quantity": 12,
        "images": [{"large": {"url": "https://parafendri.tn/xen.webp"}}]
      }'></div>
      <div class="product-manufacturer"><img class="manufacturer-logo" alt="xen"></div>
      <dl class="data-sheet"><dt class="name">EAN13</dt><dd class="value">6192440600446</dd></dl>
    </body></html>
    """

    async def _run():
        async def fake_fetch_html(url: str, raise_on_error: bool = False):
            return html

        scraper.fetch_html = fake_fetch_html  # type: ignore[method-assign]
        return await scraper.scrape_product_details(
            "https://parafendri.tn/hygiene/1781-xen-intimel-ph-55-100ml-hygiene-xen.html"
        )

    data = asyncio.run(_run())
    assert data["barcode"] == "6192440600446"
    assert data.get("reference") is None
    assert data["available"] is True


def test_parashop_detail_uses_mpn_barcode_and_avoids_brand_as_reference():
    scraper = ParashopScraper(logging.getLogger("test_parashop_detail_barcode"))
    html = """
    <html><body>
      <div class="product-details"><div class="title page-title">GAMARDE Lait Nettoyant Douceur 200ML</div></div>
      <div class="product-price-group"><div class="product-price">47,124DT</div></div>
      <ul class="list-unstyled">
        <li class="product-stock in-stock"><b>Stock:</b> <span>En Stock</span></li>
        <li class="product-model"><b>Modèle:</b> <span>GAMARDE</span></li>
        <li class="product-mpn"><b>MPN:</b> <span>3760141876809</span></li>
      </ul>
      <div class="brand-image product-manufacturer"><a><span>GAMARDE</span></a></div>
      <input id="product-id" type="hidden" name="product_id" value="6926" />
    </body></html>
    """

    async def _run():
        async def fake_fetch_html(url: str, raise_on_error: bool = False):
            return html

        scraper.fetch_html = fake_fetch_html  # type: ignore[method-assign]
        return await scraper.scrape_product_details(
            "https://www.parashop.tn/visage/nettoyant-demaquillant/lait/gamarde-lait-nettoyant-douceur-200ml"
        )

    data = asyncio.run(_run())
    assert data["product_id"] == "6926"
    assert data["barcode"] == "3760141876809"
    assert data.get("reference") is None
    assert data["brand"] == "GAMARDE"
    assert data["price"] == 47.124


def test_pharmacieplus_static_listing_extracts_id_from_path_and_barcode_from_slug():
    scraper = PharmaciePlusScraper(logging.getLogger("test_pharmacieplus_listing"))
    html = """
    <li class="col-md-mc-5 col-fix item-prod remove-divider list-unstyled">
      <a href="https://parapharmacieplus.tn/a/9067/dermaceutic-coffret-reveal-4x-15mladvanced-cleanser-50ml-offert-3760135013357"
         class="text-gray-100 justify-content-around" title="DERMACEUTIC COFFRET REVEAL 4X 15ML">
        <div class="product-item__inner position-relative">
          <img src="https://parapharmacieplus.tn/media/produits/dermaceutic-coffret-reveal-4x-15mladvanced-cleanser-50ml-offert-3760135013357_1775895016.png"
               alt="DERMACEUTIC COFFRET REVEAL 4X 15ML" />
          <div class="text-truncate name-prod-card">DERMACEUTIC COFFRET REVEAL 4X 15ML</div>
          <div class="info-ligne-card"><div class="text-red">120.501 DT</div></div>
        </div>
      </a>
    </li>
    """

    products = scraper.extract_products_from_html(html)

    assert products == [
        {
            "id": "9067",
            "product_id": "9067",
            "url": "https://parapharmacieplus.tn/a/9067/dermaceutic-coffret-reveal-4x-15mladvanced-cleanser-50ml-offert-3760135013357",
            "name": "DERMACEUTIC COFFRET REVEAL 4X 15ML",
            "price": 120.501,
            "image": "https://parapharmacieplus.tn/media/produits/dermaceutic-coffret-reveal-4x-15mladvanced-cleanser-50ml-offert-3760135013357_1775895016.png",
            "barcode": "3760135013357",
            "sku": "3760135013357",
        }
    ]


def test_pharmacieplus_listing_keeps_current_price_separate_from_old_price():
    scraper = PharmaciePlusScraper(logging.getLogger("test_pharmacieplus_listing_prices"))
    html = """
    <li class="col-md-mc-5 col-fix item-prod remove-divider list-unstyled">
      <a href="https://parapharmacieplus.tn/a/9140/beesline-roll-on-eclair-sport-beesline-sport"
         class="text-gray-100 justify-content-around" title=" BEESLINE ROLL-ON ECLAIR SPORT  ">
        <div class="product-item__inner position-relative bg-offer-card">
          <img src="https://parapharmacieplus.tn/media/produits/_1776943142_1776943142.webp"
               alt=" BEESLINE ROLL-ON ECLAIR SPORT  " />
          <div class="font-weight-bold font-size-15 info-prod-cart badge-stock">
            <i class="fa fa-check"></i> En stock
          </div>
          <div class="text-truncate name-prod-card">BEESLINE ROLL-ON ECLAIR SPORT</div>
          <div class="info-ligne-card">
            <div class="float-left font-weight-bold font-size-17">
              <div class="text-red">
                14.000 <pp class="dt-ligne-card">DT</pp>
                <del class="font-size-12 tex-gray-100 bottom-100 grey-impornat">
                  20.000 <pp class="dt-ligne-card">DT</pp>
                </del>
              </div>
            </div>
          </div>
        </div>
      </a>
    </li>
    """

    products = scraper.extract_products_from_html(html)

    first = next(product for product in products if product.get("id") == "9140")
    assert first["name"] == "BEESLINE ROLL-ON ECLAIR SPORT"
    assert first["price"] == 14.0
    assert first["old_price"] == 20.0
    assert first["available"] is True


def test_identifier_validation_rejects_invalid_barcode_and_preserves_reference():
    product = finalize_product_record(
        {
            "barcode": "1234567890123",
            "reference": "REF-ABC-123",
            "brand": "ACME",
        }
    )

    assert "barcode" not in product
    assert product["reference"] == "REF-ABC-123"
    assert product["sku"] == "REF-ABC-123"
    assert product["data_quality"]["invalid_barcode"] == "1234567890123"


def test_identifier_validation_deduplicates_by_barcode_before_url():
    products = [
        finalize_product_record({"id": "1", "url": "https://example.test/p/1", "barcode": "3760135013357"}),
        finalize_product_record({"id": "2", "url": "https://example.test/p/2", "barcode": "3760135013357"}),
        finalize_product_record({"id": "3", "url": "https://example.test/p/3", "reference": "REF-3"}),
    ]

    deduped = dedupe_products(products)

    assert [product["id"] for product in deduped] == ["1", "3"]


def test_parashop_detail_handles_malformed_html_without_identifier_confusion():
    scraper = ParashopScraper(logging.getLogger("test_parashop_malformed_html"))
    html = "<html><body><div class='product-details'><div class='title page-title'>Broken Product"

    async def _run():
        async def fake_fetch_html(url: str, raise_on_error: bool = False):
            return html

        scraper.fetch_html = fake_fetch_html  # type: ignore[method-assign]
        return await scraper.scrape_product_details("https://www.parashop.tn/broken-product")

    data = asyncio.run(_run())
    assert data["url"] == "https://www.parashop.tn/broken-product"
    assert data["title"] == "Broken Product"
    assert data.get("barcode") is None
    assert data.get("reference") is None


def test_shared_metadata_reads_barcode_and_reference_from_spec_sections():
    html = """
    <html><body>
      <table class="product_attributes">
        <tr><th>Code barre</th><td>3499320012430</td></tr>
        <tr><th>Reference produit</th><td>4N966</td></tr>
      </table>
      <dl class="data-sheet">
        <dt>EAN13</dt><dd>6192440600446</dd>
      </dl>
    </body></html>
    """

    data = html_product_metadata(html, "https://example.test/product", "https://example.test")

    assert data["barcode"] == "3499320012430"
    assert data["reference"] == "4N966"
    assert data["sku"] == "4N966"
