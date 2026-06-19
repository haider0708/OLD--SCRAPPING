import tempfile, os, re

td = tempfile.gettempdir()

# ============================================================
# BILL.TN ANALYSIS
# ============================================================
bill_path = os.path.join(td, 'bill_tn.html')
with open(bill_path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

print("=" * 60)
print("BILL.TN ANALYSIS")
print("=" * 60)
print(f"File size: {len(content)} chars\n")

# Locate the vertical menu div
vm_idx = content.find('header-bottom_vertical-menu')
# Go back to find the opening <div
div_start = content.rfind('<div', 0, vm_idx)
print(f"Vertical menu div starts at char {div_start}")

# Extract from the vertical menu to end of page (menu can be large)
section = content[div_start:]

# Find all hrefs
all_hrefs = re.findall(r'href=["\']([^"\']+)["\']', section)
print(f"Total hrefs in vertical menu section: {len(all_hrefs)}")

# Filter /collections/
collection_urls = []
seen = set()
for h in all_hrefs:
    if '/collections/' in h:
        # Normalize: strip domain if present, keep path only
        if h.startswith('http'):
            h = re.sub(r'https?://[^/]+', '', h)
        if h not in seen:
            seen.add(h)
            collection_urls.append(h)

print(f"\nUnique /collections/ URLs: {len(collection_urls)}")
for i, url in enumerate(collection_urls, 1):
    print(f"  {i:3}. {url}")

# Also look for template/temp-id sections to check they are captured
template_count = content.count('temp-id')
print(f"\nNote: 'temp-id' appears {template_count} times in full page")

# Show the raw vertical menu HTML for inspection
print("\n--- RAW VERTICAL MENU HTML (first 8000 chars) ---")
# Find the menu-list inside the vertical menu
ml_idx = content.find('menu-list', vm_idx)
chunk_start = max(0, ml_idx - 200)
print(content[chunk_start:chunk_start + 8000])
