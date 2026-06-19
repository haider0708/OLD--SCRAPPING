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
    "bestbuytunisie": "scraper.sites.bestbuytunisie",
    "sigshop": "scraper.sites.sigshop",
    "qsnet": "scraper.sites.qsnet",
    "promouv": "scraper.sites.promouv",
    "bill": "scraper.sites.bill",
    "techgate": "scraper.sites.techgate",
    "acspace": "scraper.sites.acspace",
    "krichen": "scraper.sites.krichen",
    "emh": "scraper.sites.emh",
    "maalejaudio": "scraper.sites.maalejaudio",
    "electrobennjima": "scraper.sites.electrobennjima",
    "kamounhome": "scraper.sites.kamounhome",
    "agora": "scraper.sites.agora",
    "tunewtec": "scraper.sites.tunewtec",
    "gamershop": "scraper.sites.gamershop",
    "megapc": "scraper.sites.megapc",
    "yatoo": "scraper.sites.yatoo",
    "affariyet": "scraper.sites.affariyet",
    "chaktech": "scraper.sites.chaktech",
    "techland": "scraper.sites.techland",
    "bstech": "scraper.sites.bstech",
    "drest": "scraper.sites.drest",
    "lamode": "scraper.sites.lamode",
    "itechstore": "scraper.sites.itechstore",
    "ispace": "scraper.sites.ispace",
    "psstore": "scraper.sites.psstore",
    "tokyo_store": "scraper.sites.tokyo_store",
    "el_farabi": "scraper.sites.el_farabi",
    "parahouse": "scraper.sites.parahouse",
    "beautystore": "scraper.sites.beautystore",
    "jmb": "scraper.sites.jmb",
    "topbureau": "scraper.sites.topbureau",
    "benzarti-electromenager": "scraper.sites.benzarti_electromenager",
    "taktek": "scraper.sites.taktek",
    "koktahome": "scraper.sites.koktahome",
    "carthagoinformatique": "scraper.sites.carthagoinformatique",
    "mageekstore": "scraper.sites.mageekstore",
    "electrohadjkacem": "scraper.sites.electrohadjkacem",
    "imag": "scraper.sites.imag",
    "electrochaabani": "scraper.sites.electrochaabani",
    "dokani": "scraper.sites.dokani",
    "ikitchen": "scraper.sites.ikitchen",
    "capricelingerie": "scraper.sites.capricelingerie",
    "tuttosport": "scraper.sites.tuttosport",
    "kastelo": "scraper.sites.kastelo",
    "mbm": "scraper.sites.mbm",
    "infotec": "scraper.sites.infotec",
    "eleganza": "scraper.sites.eleganza",
    "try_and_buy": "scraper.sites.try_and_buy",
    "kiabi": "scraper.sites.kiabi",
    "lesportif": "scraper.sites.lesportif",
    "supersport": "scraper.sites.supersport",
    "tunisiepara": "scraper.sites.tunisiepara",
    "paraexpert": "scraper.sites.paraexpert",
    "totaltunisia": "scraper.sites.totaltunisia",
    "pointm": "scraper.sites.pointm",
    "cosmetique": "scraper.sites.cosmetique",
    "alarabia": "scraper.sites.alarabia",
    "informatica": "scraper.sites.informatica",
    "paraland": "scraper.sites.paraland",
    "sangour": "scraper.sites.sangour",
    "farmasi": "scraper.sites.farmasi",
    "sweetbaby": "scraper.sites.sweetbaby",
    "bambinos": "scraper.sites.bambinos",
    "bb_store": "scraper.sites.bb_store",
    "toopty": "scraper.sites.toopty",
    "oriflame": "scraper.sites.oriflame",
    "petit_bateau": "scraper.sites.petit_bateau",
    "benyaghlane": "scraper.sites.benyaghlane",
    "ceresbookshop": "scraper.sites.ceresbookshop",
    "alkitab": "scraper.sites.alkitab",
    "culturel": "scraper.sites.culturel",
    "bricola": "scraper.sites.bricola",
    "piscineshop": "scraper.sites.piscineshop",
    "sqes": "scraper.sites.sqes",
    "petstore": "scraper.sites.petstore",
    "animalia": "scraper.sites.animalia",
    "zanimo": "scraper.sites.zanimo",
    "celio": "scraper.sites.celio",
    "animalzone": "scraper.sites.animalzone",
    "zanimax": "scraper.sites.zanimax",
    "kitty-city": "scraper.sites.kitty_city",
    "chicopets": "scraper.sites.chicopets",
    "coquette": "scraper.sites.coquette",
    "maparatunisie": "scraper.sites.maparatunisie",
    "lagrandepara": "scraper.sites.lagrandepara",
    "mycare": "scraper.sites.mycare",
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
