"""
Stealth helpers to avoid bot detection and rate-limiting.

- Rotating User-Agent strings (real Chrome/Firefox on Win/Mac/Linux)
- Realistic browser headers (Accept, Accept-Language, Sec-Fetch-*)
- Random human-like delays between requests
"""
import random
from typing import Dict

# ── Realistic User-Agent pool ─────────────────────────────────────
# Updated Chrome / Firefox strings that match real browser traffic.
USER_AGENTS = [
    # Chrome 131 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 131 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 131 – Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 130
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Chrome 129
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    # Firefox 132 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    # Firefox 132 – Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
    # Firefox 131
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
]

# ── Languages that look natural for Tunisian sites ────────────────
ACCEPT_LANGUAGES = [
    "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "fr-TN,fr;q=0.9,fr-FR;q=0.8,en;q=0.7",
    "fr-FR,fr;q=0.9,ar;q=0.8,en-US;q=0.7,en;q=0.6",
    "fr,en-US;q=0.9,en;q=0.8",
    "fr-FR,fr;q=0.9,en;q=0.8",
]


def random_ua() -> str:
    """Return a random realistic User-Agent string."""
    return random.choice(USER_AGENTS)


def random_headers() -> Dict[str, str]:
    """Return a full set of realistic browser headers.

    These match what Chrome/Firefox actually send and include
    Sec-Fetch-* headers that many WAFs check for.
    """
    ua = random_ua()
    is_firefox = "Firefox" in ua

    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

    if not is_firefox:
        # Chrome-specific
        headers["sec-ch-ua"] = '"Chromium";v="131", "Not_A Brand";v="24"'
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = random.choice(['"Windows"', '"Linux"', '"macOS"'])

    return headers


def random_delay(min_s: float = 0.5, max_s: float = 2.0) -> float:
    """Return a random delay (seconds) with slight bias toward shorter waits."""
    # Use triangular distribution: most delays cluster near the low end
    return random.triangular(min_s, max_s, min_s + (max_s - min_s) * 0.3)
