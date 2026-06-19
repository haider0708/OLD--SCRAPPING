import tempfile, os, re

td = tempfile.gettempdir()

# ============================================================
# BILL.TN - FINAL CLEAN ANALYSIS
# ============================================================
bill_path = os.path.join(td, 'bill_tn.html')
with open(bill_path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

# Locate vertical menu
vm_idx = content.find('header-bottom_vertical-menu')
div_start = content.rfind('<div', 0, vm_idx)
section = content[div_start:]

all_hrefs = re.findall(r'href=["\']([^"\']+)["\']', section)

# Separate pure category collections from /collections/.../products/... links
category_urls = []
product_in_collection_urls = []
seen = set()

for h in all_hrefs:
    if '/collections/' not in h:
        continue
    # Normalize: strip domain
    if h.startswith('http'):
        h = re.sub(r'https?://[^/]+', '', h)
    if h in seen:
        continue
    seen.add(h)
    # Check if it's a collection page or a product page within a collection
    # /collections/slug  vs  /collections/slug/products/product-slug
    if '/products/' in h:
        product_in_collection_urls.append(h)
    else:
        category_urls.append(h)

print("=" * 70)
print("BILL.TN - FINAL RESULTS")
print("=" * 70)
print(f"\nPure category /collections/ URLs: {len(category_urls)}")
print(f"Product-in-collection URLs (not categories): {len(product_in_collection_urls)}")
print(f"\nSelector: div.header-bottom_vertical-menu ul.menu-list li.menu-item a")
print(f"          (includes <template class='temp-id'> subcategory content)")
print()
print("COMPLETE LIST OF CATEGORY URLs:")
print("-" * 70)
for i, url in enumerate(category_urls, 1):
    print(f"  {i:3}. {url}")

# ============================================================
# ACSPACE.TN - FINAL CLEAN ANALYSIS
# ============================================================
acspace_path = os.path.join(td, 'acspace_tn.html')
with open(acspace_path, 'r', encoding='utf-8', errors='replace') as f:
    content2 = f.read()

all_hrefs2 = re.findall(r'href=["\']([^"\']+)["\']', content2)

# Filter category-type URLs: those that are https://acspace.tn/<slug>/
# Exclude: wp-content, wp-json, wp-login, feed, xmlrpc, comments, marque pages,
#          specific product pages (identified by being too long/descriptive), contact, mon-compte
#          and individual product listings vs category listings

def is_likely_category(url):
    # Must be acspace.tn internal
    if not url.startswith('https://acspace.tn/'):
        return False
    path = url.replace('https://acspace.tn', '')
    # Exclude non-category paths
    excludes = [
        'wp-content', 'wp-json', 'wp-login', 'xmlrpc', '/feed/', 'comments/feed',
        'wp-json/oembed', '/contact/', '/mon-compte/', '/marque/',
        '/aid-el-idha/', '/marque/'
    ]
    for ex in excludes:
        if ex in path:
            return False
    # Keep only short slug paths (category style)
    # A category slug is typically /word-word/ with no further nesting
    parts = [p for p in path.strip('/').split('/') if p]
    if len(parts) != 1:
        return False
    # Exclude pages that look like specific product pages (very long names with dimensions etc.)
    if len(parts[0]) > 60:
        return False
    return True

def is_product_page(url):
    """Detect specific product pages like /climatiseur-18000-btu-gree-..."""
    if not url.startswith('https://acspace.tn/'):
        return False
    path = url.replace('https://acspace.tn/', '').strip('/')
    # Product pages tend to have numbers like model numbers, BTU ratings etc.
    if re.search(r'\d{3,}', path) and len(path) > 40:
        return True
    return False

# Collect all unique acspace category URLs
acspace_cats = []
seen2 = set()
for h in all_hrefs2:
    if h in seen2:
        continue
    seen2.add(h)
    if is_likely_category(h) and not is_product_page(h):
        acspace_cats.append(h)

# Sort them
acspace_cats.sort()

print()
print("=" * 70)
print("ACSPACE.TN - FINAL RESULTS")
print("=" * 70)
print(f"\nTotal unique category URLs: {len(acspace_cats)}")
print(f"\nSelector strategy: ALL hrefs in full page HTML filtered to")
print(f"  https://acspace.tn/<single-slug>/ pattern")
print(f"  Sources: header nav menu + splide slider + dropdown menus throughout page")
print()
print("COMPLETE LIST OF CATEGORY URLs:")
print("-" * 70)
for i, url in enumerate(acspace_cats, 1):
    path = url.replace('https://acspace.tn', '')
    print(f"  {i:3}. {path}")
