import tempfile, os, re

td = tempfile.gettempdir()

# ============================================================
# ACSPACE.TN ANALYSIS
# ============================================================
acspace_path = os.path.join(td, 'acspace_tn.html')
with open(acspace_path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

print("=" * 60)
print("ACSPACE.TN ANALYSIS")
print("=" * 60)
print(f"File size: {len(content)} chars\n")

# Check for key HTML structures
for key in ['splide', 'splide__list', 'splide__slide', 'nav', '<nav', 'category', 'collection',
            'menu', 'header', 'footer']:
    count = content.count(key)
    idx = content.find(key)
    print(f"  [{key}]: count={count}, first at {idx}")

print()

# Extract all hrefs in the full page
all_hrefs = re.findall(r'href=["\']([^"\']+)["\']', content)
print(f"Total hrefs in page: {len(all_hrefs)}")

# Look for category-related URLs
category_patterns = ['/collections/', '/categorie', '/category', '/cat/', 'cat=']
for pat in category_patterns:
    matches = [h for h in all_hrefs if pat in h]
    if matches:
        print(f"\n  Pattern '{pat}' matches: {len(matches)}")
        for m in matches[:20]:
            print(f"    {m}")

print()
print("--- ALL UNIQUE HREFS (filtering internal/assets) ---")
unique_hrefs = sorted(set(all_hrefs))
for h in unique_hrefs:
    # Skip obvious non-category links
    if any(skip in h for skip in ['.css', '.js', '.png', '.jpg', '.svg', '.ico', 'font',
                                    'cdn', '#', 'javascript', 'mailto', 'tel:', 'whatsapp',
                                    'facebook', 'instagram', 'tiktok', 'youtube', 'twitter',
                                    'google', 'apple', 'android']):
        continue
    print(f"  {h}")

# Look specifically in nav and splide sections
print("\n--- NAV SECTIONS ---")
nav_sections = re.findall(r'<nav[^>]*>(.*?)</nav>', content, re.DOTALL)
print(f"Found {len(nav_sections)} <nav> elements")
for i, nav in enumerate(nav_sections):
    hrefs_in_nav = re.findall(r'href=["\']([^"\']+)["\']', nav)
    print(f"\nNav {i+1} ({len(hrefs_in_nav)} links):")
    for h in hrefs_in_nav:
        print(f"  {h}")

print("\n--- SPLIDE SECTIONS ---")
# Find splide list items
splide_matches = re.findall(r'splide__slide[^>]*>(.*?)</li>', content, re.DOTALL)
print(f"Found {len(splide_matches)} splide slides")
for i, slide in enumerate(splide_matches[:30]):
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', slide)
    texts = re.findall(r'<[^>]+>([^<]+)<', slide)
    text_clean = ' '.join(t.strip() for t in texts if t.strip())
    print(f"  Slide {i+1}: href={hrefs} text={text_clean[:80]}")

# Look for header structure
print("\n--- HEADER SECTION ---")
header_idx = content.find('<header')
if header_idx >= 0:
    header_section = content[header_idx:header_idx+10000]
    header_hrefs = re.findall(r'href=["\']([^"\']+)["\']', header_section)
    print(f"Links in header ({len(header_hrefs)}):")
    for h in header_hrefs:
        print(f"  {h}")

# Show raw HTML around categories/splide
print("\n--- RAW HTML SAMPLE (splide area) ---")
splide_idx = content.find('splide')
if splide_idx >= 0:
    print(content[max(0, splide_idx-500):splide_idx+3000])
else:
    print("No splide found")
    # Show first 3000 chars to understand structure
    print("\n--- FULL PAGE FIRST 5000 chars ---")
    print(content[:5000])
