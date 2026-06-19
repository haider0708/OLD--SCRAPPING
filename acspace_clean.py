import tempfile, os, re

td = tempfile.gettempdir()
acspace_path = os.path.join(td, 'acspace_tn.html')
with open(acspace_path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

all_hrefs = re.findall(r'href=["\']([^"\']+)["\']', content)

# TRUE category pages: https://acspace.tn/<slug>/ — single-level slugs
# Must exclude:
#   - wp-* paths
#   - feed, xmlrpc, comments, contact, mon-compte
#   - marque/* (brand pages, not categories per se) — keeping them as they ARE category pages
#   - /?page_id=* (WordPress page IDs, not categories)
#   - specific product pages that are NOT parent categories
#     e.g. /refrigerateur-302l-geant-grf-d420l-inox/ = individual product
#     vs /refrigerateur/ = category
#   - individual scooter product pages like /scooter-gsm-bwx-adventure-125cc/
#     vs /scooters/ or /scooter-electrique/ (these are sub-categories)

# Strategy:
#  - Keep single-slug acspace.tn paths
#  - Exclude: page_id, wp-*, feeds, xmlrpc, contact, mon-compte
#  - For specific product pages with numbers + dimensions in slug, exclude them
#  - Scooter/moto specific model pages (identifiable by having brand/model + cc/numbers)

def classify(url):
    if not url.startswith('https://acspace.tn/'):
        return None
    path = url.replace('https://acspace.tn', '')

    # Exclude non-category paths
    if any(exc in path for exc in [
        'wp-content', 'wp-json', 'wp-login', 'xmlrpc', '/feed', 'comments',
        'page_id', '/contact', '/mon-compte', '/aid-el-idha'
    ]):
        return None

    # Must be single-level slug
    parts = [p for p in path.strip('/').split('/') if p]
    if len(parts) != 1:
        return None

    slug = parts[0]

    # Detect individual product pages (not categories)
    # These have model numbers, dimensions like "302l", "341l", "18000-btu", etc.
    # Pattern: slug contains digits that look like model specs
    product_patterns = [
        r'\d{3}l\b',          # e.g. 302l, 341l (liter capacity)
        r'\d{4,}-btu',        # e.g. 18000-btu
        r'\d{2,}btu',         # btu ratings
        r'\d{2,}cc\b',        # e.g. 125cc (engine cc)
        r'\d{4}w\b',          # watt ratings
        r'inv\d',             # inverter model numbers
        r'\bfrig-\d',         # fridge model numbers
        r'\bsa-s\d',          # model numbers
        r'mx-x\d',            # maxwell model numbers
        r'gw-b\d',            # LG model
        r'cdht\d',            # candy model
        r'\d{3,}di\b',        # model suffix
    ]

    is_product = any(re.search(pat, slug, re.I) for pat in product_patterns)

    # Specific known product pages (not categories)
    known_products = {
        'scooter-formula-bbm-49cc',
        'scooter-gsm-bwx-adventure-125cc',
        'scooter-gsm-mbx-brillant-125cc',
        'scooter-gsm-mobster-velocifero-125cc',
        'scooter-sym-symphony-st-125cc',
        'scooter-tennis-velocifero-125cc',
        'scooter-electrique-elegeant-2000w',
        'scooter-electrique-gsm-shark-2000w',
        'motocylce-slc-flora-125cc',
        'moto-ftm-hammer-3-120-cc',
        'moto-forza',  # This is both a product and a brand page — include
        'refrigerateur-302l-geant-grf-d420l-inox',
        'refrigerateur-341l-lg-gw-b459nllm-inverter-smart-nofrost-silver',
        'refrigerateur-341l-lg-gw-b459nqfm-combine-inverter-nofrost-mate-black',
        'refrigerateur-439l-focus-f-7045b-lessfrost-noir',
        'refrigerateur-478l-telefunken-frig-473di-nofrost-dark-inox',
        'refrigerateur-478l-telefunken-frig-473w-nofrost-blanc',
        'refrigerateur-483l-focus-f-5070x-nofrost-inox',
        'refrigerateur-505l-side-by-side-hyundai-shyn-91sbs2dbg-inverter-noir',
        'refrigerateur-570l-candy-cdht570fwb-nofrost-noir',
        'refrigerateur-side-by-side-520l-focus-smart-6300-4-portes-nofrost-inox',
    }

    if slug in known_products or is_product:
        return 'product'

    # Handle marque (brand) pages — these are category-like navigation pages
    # Keeping them but flagging separately
    if path.startswith('/marque/'):
        return 'brand'

    return 'category'

categories = []
brands = []
products_found = []
seen = set()

for h in all_hrefs:
    if h in seen:
        continue
    seen.add(h)
    result = classify(h)
    path = h.replace('https://acspace.tn', '')
    if result == 'category':
        categories.append(path)
    elif result == 'brand':
        brands.append(path)
    elif result == 'product':
        products_found.append(path)

categories.sort()
brands.sort()

print("=" * 70)
print("ACSPACE.TN - CLEAN CATEGORY ANALYSIS")
print("=" * 70)
print(f"\nTrue category/section URLs: {len(categories)}")
print(f"Brand/marque pages (excluded from main count): {len(brands)}")
print(f"Individual product pages identified (excluded): {len(products_found)}")
print()
print("COMPLETE LIST OF CATEGORY URLs (path only):")
print("-" * 70)
for i, path in enumerate(categories, 1):
    print(f"  {i:3}. {path}")

print()
print("BRAND PAGES (not categories per se):")
for i, path in enumerate(brands, 1):
    print(f"  {i:3}. {path}")
