#!/usr/bin/env python3
import json
import subprocess
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper.sites import list_available_sites


def main():
    results = []
    for site in list_available_sites():
        started = time.time()
        cmd = [
            "python",
            "scrape.py",
            "test",
            "--site",
            site,
            "--categories",
            "1",
            "--products",
            "1",
            "--detail-workers",
            "2",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            output = (proc.stdout or "") + (proc.stderr or "")
            ok = proc.returncode == 0 and "RESULT:" in output
            results.append(
                {
                    "site": site,
                    "returncode": proc.returncode,
                    "duration_sec": round(time.time() - started, 1),
                    "ok": ok,
                    "output_tail": output[-1200:],
                }
            )
            print(f"{site}: rc={proc.returncode} ok={ok}")
        except Exception as exc:
            results.append(
                {
                    "site": site,
                    "returncode": -1,
                    "duration_sec": round(time.time() - started, 1),
                    "ok": False,
                    "error": str(exc),
                }
            )
            print(f"{site}: exception={exc}")

    out = Path("data/live_audit_results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
