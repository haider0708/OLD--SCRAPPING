import logging

from scraper.sites.expert_gaming import ExpertGamingScraper
from scraper.sites.geant import GeantScraper
from scraper.sites.mapara import MaparaScraper
from scraper.sites.scoop import ScoopScraper
from scraper.sites.wiki import WikiScraper


def test_product_jsonld_fallback_expert_gaming():
    scraper = ExpertGamingScraper(logging.getLogger("t"))
    html = """
    <script type="application/ld+json">
    {"@type":"Product","name":"Test GPU","url":"https://expert-gaming.tn/p/1","offers":{"price":"1299.000"}}
    </script>
    """
    products = scraper.extract_products_from_html(html)
    assert products and products[0]["name"] == "Test GPU"


def test_product_jsonld_fallback_scoop():
    scraper = ScoopScraper(logging.getLogger("t"))
    html = """
    <script type="application/ld+json">
    {"@type":"Product","name":"Test Mouse","url":"https://scoopgaming.com.tn/p/2","offers":{"price":"199.000"}}
    </script>
    """
    products = scraper.extract_products_from_html(html)
    assert products and products[0]["name"] == "Test Mouse"


def test_product_jsonld_fallback_wiki():
    scraper = WikiScraper(logging.getLogger("t"))
    html = """
    <script type="application/ld+json">
    {"@type":"Product","name":"Test Phone","url":"https://wiki.tn/p/3","offers":{"price":"999.000"}}
    </script>
    """
    products = scraper.extract_products_from_html(html)
    assert products and products[0]["name"] == "Test Phone"


def test_category_fallback_geant():
    scraper = GeantScraper(logging.getLogger("t"))
    html = '<a href="https://geantdrive.tn/epicerie">Epicerie</a>'
    data = scraper.extract_categories_from_html(html)
    assert data["stats"]["total_urls"] >= 1


def test_category_fallback_mapara():
    scraper = MaparaScraper(logging.getLogger("t"))
    html = '<a href="https://www.maparatunisie.tn/categorie-beaute">Beaute</a>'
    data = scraper.extract_categories_from_html(html)
    assert data["stats"]["total_urls"] >= 1
