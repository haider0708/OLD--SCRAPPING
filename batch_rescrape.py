"""
Re-scrape the CLEAN shops in 3-at-a-time batches.
Ordered smallest first so wins land fast.
"""
import asyncio
import json
import pathlib
import subprocess
import sys
import time

# Remaining shops (after batches 1+2 in 6-parallel run).
# Already done: allani, itechstore, qsnet, chaktech, informatica, electrochaabani,
#               emh, sbs, bestbuytunisie, bill, psstore, agora.
# wiki + mbm were interrupted mid-run — re-add.
CLEAN_SHOPS = [
    "wiki",           # interrupted
    "mbm",            # interrupted
    "maalejaudio",    # 1,828
    "krichen",        # 2,254
    "kamounhome",     # 2,714
    "expert_gaming",  # 2,678
    "zoom",           # 3,026
    "tokyo_store",    # 3,088
    "try_and_buy",    # 3,145
    "infotec",        # 3,624
    "jumbo",          # 3,962
    "batam",          # 4,575
    "electrohadjkacem", # 4,729
    "gamershop",      # 6,060
    "technopro",      # 7,649
    "carthagoinformatique",  # 12,188
    "mytek",          # 21,669
    "spacenet",       # 29,646
]

LOG_DIR = pathlib.Path("logs/batch_rescrape")
LOG_DIR.mkdir(parents=True, exist_ok=True)


def run_shop(shop: str) -> tuple[str, int]:
    log_file = LOG_DIR / f"{shop}.log"
    print(f"  [start] {shop} -> {log_file}", flush=True)
    t0 = time.time()
    with open(log_file, "w", encoding="utf-8") as fh:
        proc = subprocess.run(
            [sys.executable, "scrape.py", "full", "--site", shop],
            stdout=fh, stderr=subprocess.STDOUT,
            env={"PYTHONIOENCODING": "utf-8", **__import__("os").environ},
        )
    dt = int(time.time() - t0)
    print(f"  [done]  {shop}  rc={proc.returncode}  ({dt}s)", flush=True)
    return shop, proc.returncode, dt


def batch3(items):
    for i in range(0, len(items), 2):
        yield items[i:i + 2]


def main():
    results = []
    overall_t0 = time.time()
    for batch in batch3(CLEAN_SHOPS):
        print(f"\n==== Batch: {batch} ====", flush=True)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(run_shop, s) for s in batch]
            for fut in as_completed(futs):
                results.append(fut.result())
    elapsed = int(time.time() - overall_t0)
    print(f"\n==== ALL DONE in {elapsed}s ====", flush=True)
    for shop, rc, dt in results:
        flag = "OK" if rc == 0 else f"FAIL({rc})"
        print(f"  {shop:<30} {flag:<10} {dt}s", flush=True)


if __name__ == "__main__":
    main()
