import logging

from scraper.sites.batam import BatamScraper
from scraper.sites.spacenet import SpaceNetScraper


def test_spacenet_category_fallback_extracts_links():
    scraper = SpaceNetScraper(logging.getLogger("test_spacenet"))
    html = """
    <html><body>
      <a href="https://spacenet.tn/195-refrigerateur-tunisie">Refrigerateur</a>
      <a href="https://spacenet.tn/226-congelateur-tunisie">Congelateur</a>
    </body></html>
    """
    data = scraper.extract_categories_from_html(html)
    assert data["stats"]["total_urls"] >= 2


def test_batam_category_fallback_extracts_links():
    scraper = BatamScraper(logging.getLogger("test_batam"))
    html = """
    <html><body>
      <a href="https://batam.com.tn/ordinateur-portable">Ordinateur portable</a>
      <a href="https://batam.com.tn/smartphone-et-telephone-portable">Smartphone</a>
    </body></html>
    """
    data = scraper.extract_categories_from_html(html)
    assert data["stats"]["total_urls"] >= 2
