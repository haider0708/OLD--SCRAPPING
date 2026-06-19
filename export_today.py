import sys, json, logging
from pathlib import Path

sys.path.insert(0, '.')
from export_db import MongoDBExporter

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-8s | %(message)s')

data_dir = Path('data')
skip = {'price_history', 'availability_history', 'products_added', 'products_removed', 'state', 'merged'}

exporter = MongoDBExporter()

exported = 0
for shop_dir in sorted(data_dir.iterdir()):
    if shop_dir.name in skip or not shop_dir.is_dir():
        continue
    runs = sorted([d for d in shop_dir.iterdir() if d.is_dir() and d.name[:4].isdigit()], reverse=True)
    if not runs:
        continue
    latest = runs[0]
    if not latest.name.startswith('2026-06'):
        continue
    # Only export if we have at least products
    pf = latest / 'products.json'
    if not pf.exists():
        print(f'SKIP {shop_dir.name} — no products.json')
        continue
    print(f'\n>>> Exporting {shop_dir.name} ({latest.name})')
    exporter.export_shop_data(shop_dir.name, latest)
    exported += 1

exporter.close()
print(f'\nDone — exported {exported} shops.')
