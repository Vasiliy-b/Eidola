#!/usr/bin/env python3
"""Content cleanup runner.

Can run once (--once) or as a periodic daemon (default: every 6 hours).
Handles: variant file cleanup, original cleanup, content expiry, disk monitoring.

Usage:
    python scripts/run_cleanup.py              # daemon mode (every 6h)
    python scripts/run_cleanup.py --once       # single run
    python scripts/run_cleanup.py --interval 3600   # every hour
    python scripts/run_cleanup.py --disk-only  # only check disk space
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv()

from eidola.content.cleanup_manager import CleanupManager, check_disk_space


def main():
    parser = argparse.ArgumentParser(description="Content cleanup manager")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=21600, help="Poll interval in seconds (default: 6h)")
    parser.add_argument("--disk-only", action="store_true", help="Only check disk space")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger("cleanup")

    if args.disk_only:
        free_gb, is_ok = check_disk_space()
        status = "OK" if is_ok else "LOW"
        print(f"Disk: {free_gb:.1f} GB free [{status}]")
        return

    manager = CleanupManager()

    if args.once:
        results = manager.run_full_cleanup()
        free_gb, _ = check_disk_space()
        print(f"Cleanup: variants={results['variant_files_deleted']}, "
              f"originals={results['original_files_deleted']}, "
              f"expired={results['expired_items']}, "
              f"freed={results['freed_bytes'] / 1024 / 1024:.1f} MB, "
              f"disk_free={free_gb:.1f} GB")
        return

    logger.info("Cleanup daemon started (interval=%ds)", args.interval)
    while True:
        try:
            manager.run_full_cleanup()
            check_disk_space()
        except Exception as e:
            logger.error("Cleanup error: %s", e)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
