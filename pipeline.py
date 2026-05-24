#!/usr/bin/env python3
"""
Pipeline - Automated Scraping
===============================

Simple automated scraper that scrapes all websites one by one and saves JSON data.

Usage:
    python pipeline.py run --once
    python pipeline.py run --interval 720
"""

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from export_db import MongoDBExporter, export_latest_run
# from merge_products import merge_latest_products  # MERGE DISABLED
from scrape import limit_products_in_data, run_full_scrape, setup_logger
from scraper.base import LOGS_DIR, TorPool, load_json, save_json
from scraper.sites import get_scraper
from track_history import track_history_for_shop


@dataclass
class SiteStats:
    """Statistics for a single site scrape."""

    site: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_seconds: float = 0
    products_total: int = 0
    details_scraped: int = 0
    success: bool = False
    status: str = "failed"
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SimplePipeline:
    """Simple scraping pipeline - scrape and save JSON data."""

    def __init__(
        self,
        sites: List[str],
        data_dir: str = "data",
        interval_minutes: int = 720,
        workers: int = 16,
        detail_workers: int = 64,
        site_options: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.sites = sites
        self.data_dir = Path(data_dir)
        self.interval_minutes = interval_minutes
        self.workers = workers
        self.detail_workers = detail_workers
        self.site_options = site_options or {}

        # Setup logger
        self.logger = self._setup_logger()

        self.run_stats: Dict[str, SiteStats] = {}

    def _setup_logger(self) -> logging.Logger:
        """Setup logger."""
        # Suppress ALL httpx logging (HTTP requests, warnings, etc.)
        logging.getLogger("httpx").setLevel(logging.CRITICAL)

        logger = logging.getLogger("pipeline")
        logger.setLevel(logging.INFO)
        logger.handlers = []

        # Create log file for pipeline runs
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = LOGS_DIR / f"pipeline_{timestamp}.log"

        # File handler - logs everything to file
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(file_handler)

        # Console handler - still shows INFO to terminal
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        return logger

    async def _process_site(self, site: str):
        """Process a single site: scrape and save JSON data."""
        stats = SiteStats(site=site, started_at=datetime.now().isoformat())
        self.logger.info(f"\n{'=' * 70}")
        self.logger.info(f"🔄 Processing: {site.upper()}")
        self.logger.info(f"{'=' * 70}")

        site_options = self.site_options.get(site, {})
        use_tor = bool(site_options.get("use_tor", False))
        tor_pool = TorPool.get()
        tor_pool.activate(use_tor)
        if use_tor and tor_pool.active:
            self.logger.info(f"  Tor enabled for {site}")
        elif use_tor:
            self.logger.warning(
                f"  Tor requested for {site}, but USE_TOR/Tor instances are not active"
            )

        try:
            # Run full scrape (categories + products + details)
            result = await run_full_scrape(
                site_name=site,
                num_workers=self.workers,
                detail_workers=self.detail_workers,
                limit=None,
                logger=self.logger,
                scrape_details=True,
            )

            if not result.get("success"):
                stats.error = result.get("error", "Unknown error")
                stats.success = False
                stats.status = result.get("status", "failed")
                return

            stats.products_total = result.get("stats", {}).get("total_products", 0)
            stats.details_scraped = result.get("stats", {}).get("details_scraped", 0)
            stats.success = True
            stats.status = result.get("status", "ok")

            # Track history immediately after this site finishes
            try:
                self.logger.info(f"  📈 Tracking price history for {site}...")
                track_history_for_shop(site)
                self.logger.info(f"  ✅ History tracked for {site}")
            except Exception as hist_err:
                self.logger.error(f"  ❌ History tracking failed for {site}: {hist_err}")

            # Export this site to MongoDB immediately (don't wait for all sites)
            try:
                self.logger.info(f"  📤 Exporting {site} to MongoDB...")
                output_path = result.get("output_path")
                if output_path:
                    site_dir = Path(output_path).parent
                    exporter = MongoDBExporter()
                    if exporter.clients:
                        exporter.export_shop_data(site, site_dir)
                    exporter.close()
                self.logger.info(f"  ✅ MongoDB export done for {site}")
            except Exception as exp_err:
                self.logger.error(f"  ❌ MongoDB export failed for {site}: {exp_err}")

        except Exception as e:
            stats.error = str(e)
            stats.success = False
            stats.status = "failed"
            self.logger.error(f"  ❌ Error processing {site}: {e}")

        finally:
            tor_pool.activate(False)
            stats.ended_at = datetime.now().isoformat()
            if stats.started_at:
                start = datetime.fromisoformat(stats.started_at)
                end = datetime.fromisoformat(stats.ended_at)
                stats.duration_seconds = (end - start).total_seconds()

            self.run_stats[site] = stats
            duration_str = f"{int(stats.duration_seconds // 60)}m {int(stats.duration_seconds % 60)}s"
            self.logger.info(f"  ⏱️  Completed in {duration_str}")

    async def run(self, continuous: bool = False):
        """Run the pipeline."""
        run_start = datetime.now()
        mode = "Continuous" if continuous else "Single Run"

        self.logger.info(f"\n{'=' * 70}")
        self.logger.info(f"🚀 PIPELINE STARTED")
        self.logger.info(f"{'=' * 70}")
        self.logger.info(f"  Sites: {', '.join(self.sites)}")
        self.logger.info(f"  Started: {run_start.strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"  Mode: {mode}")

        while True:
            self.run_stats = {}  # Reset stats for each run

            for site in self.sites:
                await self._process_site(site)
                await asyncio.sleep(2)  # Small delay between sites

            run_end = datetime.now()
            duration = run_end - run_start
            duration_str = str(duration).split(".")[0]

            # Print summary
            self.logger.info(f"\n{'=' * 70}")
            self.logger.info(f"✅ PIPELINE COMPLETE")
            self.logger.info(f"{'=' * 70}")
            self.logger.info(f"  Duration: {duration_str}")
            self.logger.info(f"  Sites processed: {len(self.sites)}")
            for site, stats in self.run_stats.items():
                status = "✅" if stats.success else "❌"
                self.logger.info(
                    f"    {status} {site}: {stats.products_total} products, "
                    f"{stats.details_scraped} details, status={stats.status}"
                )

            # all_success = all(stats.success for stats in self.run_stats.values())  # MERGE DISABLED
            success_count = sum(1 for stats in self.run_stats.values() if stats.success)

            # Price tracking and per-site MongoDB export are done inside _process_site
            # immediately after each site finishes. The final export below is a safety net.

            # MERGE DISABLED — re-enable by uncommenting this block
            # if len(self.sites) >= 3 and all_success:
            #     self.logger.info(f"\n{'=' * 70}")
            #     self.logger.info("🔄 STARTING PRODUCT MERGE")
            #     self.logger.info(f"{'=' * 70}")
            #     try:
            #         merge_result = merge_latest_products()
            #         self.logger.info(f"\n{'=' * 70}")
            #         self.logger.info("✅ MERGE SUCCESSFUL")
            #         self.logger.info(f"{'=' * 70}")
            #         self.logger.info(f"  Total merged products: {merge_result['total_products']}")
            #         self.logger.info(f"  Output: {merge_result['output_path']}")
            #     except Exception as e:
            #         self.logger.error(f"\n{'=' * 70}")
            #         self.logger.error("❌ MERGE FAILED")
            #         self.logger.error(f"{'=' * 70}")
            #         self.logger.error(f"  Error: {e}")
            #         import traceback
            #         self.logger.debug(traceback.format_exc())
            # elif not all_success:
            #     self.logger.warning("\n⚠️  Skipping merge: Some sites failed")
            # elif len(self.sites) < 3:
            #     self.logger.warning(f"\n⚠️  Skipping merge: Need at least 3 sites (got {len(self.sites)})")

            # Note: per-site MongoDB export already ran inside _process_site() after
            # each successful scrape, so we skip the global export_latest_run() here
            # to avoid re-uploading old data from sites that weren't part of this run.

            if not continuous:
                break

            self.logger.info(f"\n⏰ Next run in {self.interval_minutes} minutes")
            next_run = datetime.now() + timedelta(minutes=self.interval_minutes)
            self.logger.info(f"   Expected: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
            run_start = next_run  # Reset for next run timing
            await asyncio.sleep(self.interval_minutes * 60)


def load_config(config_path: str = "configs/pipeline_config.yaml") -> dict:
    """Load configuration from YAML file."""
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_file) as f:
        return yaml.safe_load(f)


def create_pipeline(
    config_path: str = "configs/pipeline_config.yaml",
) -> SimplePipeline:
    """Create pipeline from config file."""
    config = load_config(config_path)

    scraping_config = config.get("scraping", {})

    sites_raw = config.get("sites", [])
    sites = [s["name"] if isinstance(s, dict) else s for s in sites_raw]
    site_options = {
        s["name"]: {k: v for k, v in s.items() if k != "name"}
        for s in sites_raw
        if isinstance(s, dict) and s.get("name")
    }

    return SimplePipeline(
        sites=sites,
        data_dir=config.get("data_dir", "data"),
        interval_minutes=config.get("interval_minutes", 720),
        workers=scraping_config.get("workers", 16),
        detail_workers=scraping_config.get("detail_workers", 64),
        site_options=site_options,
    )


async def main():
    parser = argparse.ArgumentParser(description="Automated scraping pipeline")
    subparsers = parser.add_subparsers(dest="cmd", help="Command to run")

    # Run command (automated scraping pipeline)
    run_parser = subparsers.add_parser(
        "run", help="Run the automated scraping pipeline"
    )
    run_parser.add_argument("--once", action="store_true", help="Run once and exit")
    run_parser.add_argument(
        "--interval", type=int, help="Interval in minutes (default from config)"
    )
    run_parser.add_argument("--sites", nargs="+", help="Specific sites to scrape")
    run_parser.add_argument(
        "--config", default="configs/pipeline_config.yaml", help="Config file path"
    )

    args = parser.parse_args()

    if args.cmd == "run":
        # Automated scraping run
        pipeline = create_pipeline(args.config)

        if args.sites:
            pipeline.sites = args.sites

        if args.interval:
            pipeline.interval_minutes = args.interval

        await pipeline.run(continuous=not args.once)

    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
