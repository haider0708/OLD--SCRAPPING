"""
Site-specific scraper modules.
Each site has its own module implementing BaseScraper.
"""
from typing import TYPE_CHECKING
import importlib
import logging

if TYPE_CHECKING:
    from scraper.base import BaseScraper


# Registry of available scrapers
AVAILABLE_SCRAPERS = {
    "mytek": "scraper.sites.mytek",
    "tunisianet": "scraper.sites.tunisianet",
    "technopro": "scraper.sites.technopro",
    "darty": "scraper.sites.darty",
    "spacenet": "scraper.sites.spacenet",
    "jumbo": "scraper.sites.jumbo",
    "graiet": "scraper.sites.graiet",
    "batam": "scraper.sites.batam",
    "zoom": "scraper.sites.zoom",
    "allani": "scraper.sites.allani",
    "expert_gaming": "scraper.sites.expert_gaming",
    "geant": "scraper.sites.geant",
    "mapara": "scraper.sites.mapara",
    "parafendri": "scraper.sites.parafendri",
    "parashop": "scraper.sites.parashop",
    "pharmacieplus": "scraper.sites.pharmacieplus",
    "pharmashop": "scraper.sites.pharmashop",
    "sbs": "scraper.sites.sbs",
    "scoop": "scraper.sites.scoop",
    "skymill": "scraper.sites.skymill",
    "wiki": "scraper.sites.wiki",
}


def get_scraper(site_name: str, logger: logging.Logger) -> "BaseScraper":
    """
    Factory function to get the appropriate scraper for a site.
    
    Args:
        site_name: Name of the site (e.g., 'mytek')
        logger: Logger instance
        
    Returns:
        Site-specific scraper instance
        
    Raises:
        ValueError: If site is not supported
    """
    if site_name not in AVAILABLE_SCRAPERS:
        available = ", ".join(AVAILABLE_SCRAPERS.keys())
        raise ValueError(f"Unknown site: {site_name}. Available: {available}")
    
    module_path = AVAILABLE_SCRAPERS[site_name]
    module = importlib.import_module(module_path)
    
    return module.get_scraper(logger)


def list_available_sites() -> list:
    """Return list of available site names."""
    return list(AVAILABLE_SCRAPERS.keys())
