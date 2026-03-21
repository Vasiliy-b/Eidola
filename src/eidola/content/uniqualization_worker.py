"""Batch uniqualization worker.

Monitors MongoDB for pending content items and generates N uniqualized
variants (one per active account). Uses semaphore-based concurrency:
- NVENC video: 5 concurrent (GPU encoder limit)
- CPU image: 6 concurrent (i7 cores)
- Caption: 10 concurrent (Gemini API, async)

Crash-safe: per-variant status tracking in MongoDB.
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings
from .caption_uniqualizer import generate_caption_variations
from .image_uniqualizer import ImageUnikalizer, ImageUniqualizerError
from .models import (
    ContentItem,
    ContentType,
    ContentVariant,
    MediaFile,
    UniqualizationStatus,
    VariantStatus,
)
from .mongo_content import ContentStore
from .video_uniqualizer import VideoUnikalizer, VideoUniqualizerError

logger = logging.getLogger("eidola.content.worker")

NVENC_CONCURRENCY = 5
CPU_IMAGE_CONCURRENCY = 6
CAPTION_CONCURRENCY = 10

CONTENT_DIR = Path(settings.content_dir).resolve()
ORIGINALS_DIR = CONTENT_DIR / "originals"
VARIANTS_DIR = CONTENT_DIR / "variants"


def get_active_accounts() -> list[dict[str, str]]:
    """Load active accounts from config YAML files."""
    accounts_dir = Path(__file__).parent.parent.parent.parent / "config" / "accounts"
    accounts = []
    if not accounts_dir.exists():
        logger.warning("Accounts dir not found: %s", accounts_dir)
        return accounts

    import yaml

    for yaml_file in sorted(accounts_dir.glob("*.yaml")):
        try:
            with open(yaml_file, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data and data.get("metadata", {}).get("status") == "active":
                accounts.append({
                    "account_id": data["account_id"],
                    "device_id": data.get("assigned_device", ""),
                })
        except Exception as e:
            logger.warning("Error loading account %s: %s", yaml_file.name, e)

    return accounts


class UniqualizationWorker:

    def __init__(self, store: ContentStore | None = None):
        self.store = store or ContentStore()
        self.image_uniq = ImageUnikalizer()
        self.video_uniq = VideoUnikalizer(prefer_nvenc=True)
        self.video_sem = asyncio.Semaphore(NVENC_CONCURRENCY)
        self.image_sem = asyncio.Semaphore(CPU_IMAGE_CONCURRENCY)
        self.caption_sem = asyncio.Semaphore(CAPTION_CONCURRENCY)

    async def process_pending(self) -> int:
        """Process all pending content items. Returns count processed."""
        pending = self.store.get_pending_uniqualizations()
        if not pending:
            logger.debug("No pending content to process")
            return 0

        logger.info("Found %d pending content items", len(pending))
        count = 0
        for item in pending:
            try:
                await self.process_item(item)
                count += 1
            except Exception as e:
                logger.error("Failed to process %s: %s", item.content_id, e)
                self.store.update_uniqualization_status(
                    item.content_id, UniqualizationStatus.FAILED, str(e)
                )
                try:
                    from ..bot.alerts import alert_uniqualization_failed
                    alert_uniqualization_failed(item.content_id, str(e))
                except Exception:
                    pass
        return count

    async def process_item(self, item: ContentItem) -> None:
        """Generate all variants for a single content item."""
        accounts = get_active_accounts()
        if not accounts:
            raise RuntimeError("No active accounts found")

        n = len(accounts)
        logger.info(
            "Processing %s: type=%s, %d variants needed",
            item.content_id, item.type.value, n,
        )

        self.store.update_uniqualization_status(
            item.content_id, UniqualizationStatus.PROCESSING
        )
        self.store.items.update_one(
            {"content_id": item.content_id},
            {"$set": {"total_variants_needed": n}},
        )

        # Generate caption variations
        captions: list[str] = []
        if item.original_caption:
            async with self.caption_sem:
                captions = await generate_caption_variations(item.original_caption, n)
        else:
            captions = [""] * n

        # Create variant records and process media
        tasks = []
        for i, account in enumerate(accounts):
            variant = self._get_or_create_variant(item, account, i, captions[i])
            if variant.status not in (VariantStatus.PENDING, VariantStatus.ENCODING):
                logger.debug("Variant %s/%s already done, skipping", item.content_id, account["account_id"])
                continue
            tasks.append(self._process_variant(item, variant))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Check final status
        done_count = len(self.store.get_variants_for_content(item.content_id))
        failed = self.store.variants.count_documents({
            "content_id": item.content_id, "status": VariantStatus.FAILED.value,
        })

        if failed > 0:
            logger.warning("%s: %d variants failed out of %d", item.content_id, failed, n)

        self.store.update_uniqualization_status(
            item.content_id, UniqualizationStatus.DONE
        )
        self.store.items.update_one(
            {"content_id": item.content_id},
            {"$set": {"variants_done": done_count}},
        )
        logger.info("Completed %s: %d variants", item.content_id, done_count)

        # Auto-distribute after successful uniqualization
        try:
            from .distributor import distribute_content
            scheduled = distribute_content(self.store, content_ids=[item.content_id])
            logger.info("Auto-distributed %s: %d schedule entries", item.content_id, scheduled)
        except Exception as e:
            logger.error("Auto-distribute failed for %s: %s", item.content_id, e)

        # Alert admin
        try:
            from ..bot.alerts import alert_uniqualization_done
            alert_uniqualization_done(item.content_id, done_count)
        except Exception:
            pass

    def _get_or_create_variant(
        self, item: ContentItem, account: dict, index: int, caption: str,
    ) -> ContentVariant:
        existing = self.store.get_variant(item.content_id, account["account_id"])
        if existing:
            return existing

        variant = ContentVariant(
            content_id=item.content_id,
            account_id=account["account_id"],
            variant_index=index,
            media=[],
            caption=caption,
            status=VariantStatus.PENDING,
        )
        self.store.save_variant(variant)
        return variant

    async def _process_variant(self, item: ContentItem, variant: ContentVariant) -> None:
        """Process a single variant — uniqualize all media files."""
        account_dir = VARIANTS_DIR / variant.account_id / item.content_id
        account_dir.mkdir(parents=True, exist_ok=True)

        self.store.update_variant_status(
            item.content_id, variant.account_id, VariantStatus.ENCODING
        )

        try:
            media_files: list[MediaFile] = []
            for original in item.original_media:
                output_filename = os.path.basename(original.path)
                output_path = str(account_dir / output_filename)

                if item.type in (ContentType.VIDEO, ContentType.REEL, ContentType.STORY_VIDEO):
                    await self._uniqualize_video(original.path, output_path)
                else:
                    await self._uniqualize_image(original.path, output_path)

                media_files.append(MediaFile(
                    path=output_path,
                    order=original.order,
                    mime=original.mime,
                    filename=output_filename,
                ))

            # Update variant with media paths
            self.store.variants.update_one(
                {"content_id": item.content_id, "account_id": variant.account_id},
                {"$set": {
                    "media": [m.model_dump() for m in media_files],
                    "status": VariantStatus.READY.value,
                }},
            )
            self.store.increment_variants_done(item.content_id)

        except Exception as e:
            logger.error(
                "Variant %s/%s failed: %s",
                item.content_id, variant.account_id, e,
            )
            self.store.update_variant_status(
                item.content_id, variant.account_id, VariantStatus.FAILED, str(e)
            )

    async def _uniqualize_image(self, input_path: str, output_path: str) -> dict[str, Any]:
        async with self.image_sem:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self.image_uniq.uniqualize_image, input_path, output_path
            )

    async def _uniqualize_video(self, input_path: str, output_path: str) -> dict[str, Any]:
        async with self.video_sem:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self.video_uniq.uniqualize_video, input_path, output_path
            )


async def run_worker_loop(poll_interval: int = 30):
    """Run the worker in a loop, checking for pending items."""
    worker = UniqualizationWorker()
    logger.info(
        "Uniqualization worker started (video=%s, image=CPU, poll=%ds)",
        "NVENC" if worker.video_uniq.use_nvenc else "libx264",
        poll_interval,
    )

    while True:
        try:
            count = await worker.process_pending()
            if count > 0:
                logger.info("Processed %d items this cycle", count)
        except Exception as e:
            logger.error("Worker cycle error: %s", e)

        await asyncio.sleep(poll_interval)


def main():
    """Entry point for running the worker standalone."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(run_worker_loop())


if __name__ == "__main__":
    main()
