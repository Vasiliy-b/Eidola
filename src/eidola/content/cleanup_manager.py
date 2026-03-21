"""Content lifecycle cleanup manager.

Handles:
- Local variant file cleanup (posted variants older than N hours)
- Original file cleanup (all variants posted/cancelled)
- Content expiry (old content items beyond retention period)
- Disk space monitoring with Telegram alerts
"""

import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import settings
from .models import UniqualizationStatus, VariantStatus
from .mongo_content import ContentStore

logger = logging.getLogger("eidola.content.cleanup")

CONTENT_DIR = Path(settings.content_dir).resolve()
ORIGINALS_DIR = CONTENT_DIR / "originals"
VARIANTS_DIR = CONTENT_DIR / "variants"


class CleanupManager:

    def __init__(self, store: ContentStore | None = None):
        self.store = store or ContentStore()

    def run_full_cleanup(self) -> dict:
        """Run all cleanup tasks. Returns summary dict."""
        results = {
            "variant_files_deleted": 0,
            "original_files_deleted": 0,
            "expired_items": 0,
            "freed_bytes": 0,
        }

        before_usage = _dir_size(CONTENT_DIR)

        r1 = self.cleanup_posted_variants()
        results["variant_files_deleted"] = r1

        r2 = self.cleanup_completed_originals()
        results["original_files_deleted"] = r2

        r3 = self.expire_old_content()
        results["expired_items"] = r3

        after_usage = _dir_size(CONTENT_DIR)
        results["freed_bytes"] = max(0, before_usage - after_usage)

        freed_mb = results["freed_bytes"] / (1024 * 1024)
        logger.info(
            "Cleanup done: variants=%d, originals=%d, expired=%d, freed=%.1f MB",
            r1, r2, r3, freed_mb,
        )

        # Send Telegram report
        try:
            from ..bot.alerts import alert_cleanup_report
            alert_cleanup_report(r1, 0, r3, freed_mb)
        except Exception:
            pass

        return results

    def cleanup_posted_variants(self) -> int:
        """Delete local variant files for variants that have been posted.

        Only deletes if:
        - variant status is POSTED
        - posted_at is older than cleanup_variants_after_hours
        """
        max_age = timedelta(hours=settings.cleanup_variants_after_hours)
        cutoff = datetime.now(timezone.utc) - max_age
        deleted = 0

        docs = self.store.variants.find({
            "status": VariantStatus.POSTED.value,
            "posted_at": {"$lt": cutoff},
        })

        for doc in docs:
            account_id = doc.get("account_id", "")
            content_id = doc.get("content_id", "")
            variant_dir = VARIANTS_DIR / account_id / content_id

            if variant_dir.exists():
                try:
                    shutil.rmtree(variant_dir)
                    deleted += 1
                    logger.debug("Deleted variant dir: %s", variant_dir)
                except OSError as e:
                    logger.warning("Failed to delete %s: %s", variant_dir, e)

        # Clean empty account dirs
        if VARIANTS_DIR.exists():
            for account_dir in VARIANTS_DIR.iterdir():
                if account_dir.is_dir() and not any(account_dir.iterdir()):
                    account_dir.rmdir()

        if deleted:
            logger.info("Deleted %d posted variant directories", deleted)
        return deleted

    def cleanup_completed_originals(self) -> int:
        """Delete original files when ALL variants are posted or content is cancelled."""
        deleted = 0

        pipeline = [
            {"$match": {"uniqualization_status": {
                "$in": [UniqualizationStatus.DONE.value, UniqualizationStatus.CANCELLED.value]
            }}},
        ]
        items = list(self.store.items.aggregate(pipeline))

        for item_doc in items:
            content_id = item_doc.get("content_id", "")
            status = item_doc.get("uniqualization_status", "")

            should_delete = False
            if status == UniqualizationStatus.CANCELLED.value:
                should_delete = True
            else:
                non_posted = self.store.variants.count_documents({
                    "content_id": content_id,
                    "status": {"$nin": [
                        VariantStatus.POSTED.value,
                        VariantStatus.FAILED.value,
                    ]},
                })
                total_posted = self.store.variants.count_documents({
                    "content_id": content_id,
                    "status": VariantStatus.POSTED.value,
                })
                if non_posted == 0 and total_posted > 0:
                    should_delete = True

            if should_delete:
                orig_dir = ORIGINALS_DIR / content_id
                if orig_dir.exists():
                    try:
                        shutil.rmtree(orig_dir)
                        deleted += 1
                    except OSError as e:
                        logger.warning("Failed to delete originals %s: %s", orig_dir, e)

        if deleted:
            logger.info("Deleted %d completed original directories", deleted)
        return deleted

    def expire_old_content(self) -> int:
        """Mark old content as expired and clean up.

        Content older than cleanup_content_expiry_days that is still
        pending/failed gets cancelled.
        """
        max_age = timedelta(days=settings.cleanup_content_expiry_days)
        cutoff = datetime.now(timezone.utc) - max_age
        expired = 0

        docs = self.store.items.find({
            "uploaded_at": {"$lt": cutoff},
            "uniqualization_status": {"$in": [
                UniqualizationStatus.PENDING.value,
                UniqualizationStatus.FAILED.value,
            ]},
        })

        for doc in docs:
            content_id = doc.get("content_id", "")
            self.store.cancel_content(content_id)
            expired += 1
            logger.info("Expired old content: %s", content_id)

        return expired


def check_disk_space() -> tuple[float, bool]:
    """Check free disk space on content directory partition.

    Returns:
        (free_gb, is_ok) — free space in GB and whether it's above threshold.
    """
    stat = os.statvfs(str(CONTENT_DIR))
    free_bytes = stat.f_bavail * stat.f_frsize
    free_gb = free_bytes / (1024 ** 3)
    threshold = settings.disk_alert_threshold_gb
    is_ok = free_gb >= threshold

    if not is_ok:
        logger.warning("Disk space low: %.1f GB free (threshold: %.1f GB)", free_gb, threshold)
        try:
            from ..bot.alerts import alert_disk_space
            alert_disk_space(free_gb, threshold, str(CONTENT_DIR))
        except Exception:
            pass

    return free_gb, is_ok


def _dir_size(path: Path) -> int:
    """Get total size of directory in bytes."""
    if not path.exists():
        return 0
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total
