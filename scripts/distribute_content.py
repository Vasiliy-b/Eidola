#!/usr/bin/env python3
"""Distribute uniqualized content across accounts.

Generates content_schedule entries in MongoDB.
Run after uniqualization worker completes processing.

Usage:
    python scripts/distribute_content.py                     # distribute all pending
    python scripts/distribute_content.py --start-date 2026-03-07
    python scripts/distribute_content.py --dry-run            # show what would be scheduled
    python scripts/distribute_content.py --status             # show distribution status
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")


def main():
    parser = argparse.ArgumentParser(description="Distribute content across accounts")
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD), default=tomorrow")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without saving")
    parser.add_argument("--status", action="store_true", help="Show current distribution status")
    parser.add_argument("--content-id", help="Distribute specific content item only")
    args = parser.parse_args()

    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    from eidola.content.mongo_content import ContentStore
    store = ContentStore()

    if args.status:
        _show_status(store)
        return

    start = date.fromisoformat(args.start_date) if args.start_date else None
    content_ids = [args.content_id] if args.content_id else None

    from eidola.content.distributor import distribute_content

    if args.dry_run:
        print("DRY RUN — would distribute:")
        from eidola.content.distributor import _get_distributable_items, get_active_account_device_map
        items = _get_distributable_items(store)
        accounts = get_active_account_device_map()
        print(f"  Content items: {len(items)}")
        for item in items:
            print(f"    - {item.content_id} ({item.type.value})")
        print(f"  Active accounts: {len(accounts)}")
        print(f"  Start date: {start or (date.today() + timedelta(days=1))}")
        print(f"  Estimated days: {max(1, len(accounts) // max(1, len(items)))}")
        return

    count = distribute_content(store, content_ids=content_ids, start_date=start)
    print(f"\nCreated {count} schedule entries.")

    if count > 0:
        _show_status(store)


def _show_status(store):
    stats = store.get_content_stats()
    print(f"\n=== Content Distribution Status ===")
    print(f"  Content items: {stats['total_items']}")
    print(f"    Pending uniqualization: {stats['pending']}")
    print(f"    Processing: {stats['processing']}")
    print(f"    Done (ready to distribute): {stats['done']}")
    print(f"    Failed: {stats['failed']}")
    print(f"  Posted variants: {stats['posted_variants']}")

    # Show schedule for next 7 days
    print(f"\n  Schedule (next 7 days):")
    for i in range(7):
        d = (date.today() + timedelta(days=i)).isoformat()
        entries = store.get_schedule_for_date(d)
        if entries:
            posted = sum(1 for e in entries if e.posting_state.value == "posted")
            scheduled = sum(1 for e in entries if e.posting_state.value == "scheduled")
            failed = sum(1 for e in entries if e.posting_state.value == "failed")
            print(f"    {d}: {len(entries)} total ({scheduled} scheduled, {posted} posted, {failed} failed)")


if __name__ == "__main__":
    main()
