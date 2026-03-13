"""MongoDB operations for content distribution system.

Synchronous PyMongo client (same pattern as sync_memory.py).
Thread-safe, works in both sync and async contexts.
"""

import logging
from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.collection import Collection
from pymongo.database import Database

from ..config import settings
from .models import (
    ContentItem,
    ContentSchedule,
    ContentVariant,
    PostingState,
    UniqualizationStatus,
    VariantStatus,
)

logger = logging.getLogger("eidola.content.mongo")


class ContentStore:
    """MongoDB store for content items, variants, and schedules."""

    def __init__(self, mongo_uri: str | None = None, db_name: str | None = None):
        uri = mongo_uri or settings.mongo_uri
        database = db_name or settings.mongo_db_name

        self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        self.db: Database = self.client[database]

        self.items: Collection = self.db["content_items"]
        self.variants: Collection = self.db["content_variants"]
        self.schedules: Collection = self.db["content_schedule"]

        self._ensure_indexes()

    def _ensure_indexes(self):
        self.items.create_index("content_id", unique=True)
        self.items.create_index("uniqualization_status")

        self.variants.create_index([("content_id", ASCENDING), ("account_id", ASCENDING)])
        self.variants.create_index("status")
        self.variants.create_index([("content_id", ASCENDING), ("variant_index", ASCENDING)])

        self.schedules.create_index([("date", ASCENDING), ("account_id", ASCENDING)])
        self.schedules.create_index([("date", ASCENDING), ("device_id", ASCENDING)])
        self.schedules.create_index("posting_state")

    # --- Content Items ---

    def save_content_item(self, item: ContentItem) -> None:
        self.items.update_one(
            {"content_id": item.content_id},
            {"$set": item.to_mongo()},
            upsert=True,
        )
        logger.info("Saved content item %s (type=%s)", item.content_id, item.type.value)

    def get_content_item(self, content_id: str) -> ContentItem | None:
        doc = self.items.find_one({"content_id": content_id})
        if doc:
            doc.pop("_id", None)
            return ContentItem.from_mongo(doc)
        return None

    def get_pending_uniqualizations(self) -> list[ContentItem]:
        docs = self.items.find({"uniqualization_status": UniqualizationStatus.PENDING.value})
        return [ContentItem.from_mongo({k: v for k, v in d.items() if k != "_id"}) for d in docs]

    def update_uniqualization_status(
        self, content_id: str, status: UniqualizationStatus, error: str | None = None
    ) -> None:
        update: dict = {"uniqualization_status": status.value}
        if error:
            update["uniqualization_error"] = error
        self.items.update_one({"content_id": content_id}, {"$set": update})

    def increment_variants_done(self, content_id: str) -> None:
        self.items.update_one(
            {"content_id": content_id},
            {"$inc": {"variants_done": 1}},
        )

    # --- Content Variants ---

    def save_variant(self, variant: ContentVariant) -> None:
        self.variants.update_one(
            {"content_id": variant.content_id, "account_id": variant.account_id},
            {"$set": variant.to_mongo()},
            upsert=True,
        )

    def get_variant(self, content_id: str, account_id: str) -> ContentVariant | None:
        doc = self.variants.find_one(
            {"content_id": content_id, "account_id": account_id}
        )
        if doc:
            return ContentVariant.from_mongo(doc)
        return None

    def get_variants_for_content(self, content_id: str) -> list[ContentVariant]:
        docs = self.variants.find({"content_id": content_id}).sort("variant_index", ASCENDING)
        return [ContentVariant.from_mongo(d) for d in docs]

    def update_variant_status(
        self, content_id: str, account_id: str, status, error: str | None = None
    ) -> None:
        status_str = status.value if hasattr(status, "value") else str(status)
        update: dict = {"status": status_str}
        if error:
            update["error"] = error
        if status_str == "posted":
            update["posted_at"] = datetime.now(timezone.utc)
        result = self.variants.update_one(
            {"content_id": content_id, "account_id": account_id},
            {"$set": update},
        )
        if result.matched_count == 0:
            logger.critical(
                "update_variant_status matched 0 docs! content_id=%s account_id=%s — "
                "possible hallucinated ID from agent",
                content_id, account_id,
            )

    def get_pending_variants(self, content_id: str) -> list[ContentVariant]:
        docs = self.variants.find({
            "content_id": content_id,
            "status": VariantStatus.PENDING.value,
        })
        return [ContentVariant.from_mongo(d) for d in docs]

    # --- Content Schedule ---

    def save_schedule(self, schedule: ContentSchedule) -> None:
        self.schedules.update_one(
            {"date": schedule.date, "account_id": schedule.account_id, "content_id": schedule.content_id},
            {"$set": schedule.to_mongo()},
            upsert=True,
        )

    def get_schedule_for_date(self, date: str) -> list[ContentSchedule]:
        docs = self.schedules.find({"date": date}).sort("account_id", ASCENDING)
        return [ContentSchedule.from_mongo(d) for d in docs]

    def get_schedule_for_account(self, account_id: str, date: str) -> ContentSchedule | None:
        doc = self.schedules.find_one({"date": date, "account_id": account_id})
        if doc:
            return ContentSchedule.from_mongo(doc)
        return None

    def update_posting_state(
        self,
        date: str,
        account_id: str,
        content_id: str,
        state,
        error: str | None = None,
    ) -> None:
        state_str = state.value if hasattr(state, "value") else str(state)
        update: dict = {"posting_state": state_str}
        if error:
            update["error"] = error
            doc = self.schedules.find_one(
                {"date": date, "account_id": account_id, "content_id": content_id}
            )
            update["retry_count"] = (doc.get("retry_count", 0) + 1) if doc else 1
        if state_str == "posted":
            update["completed_at"] = datetime.now(timezone.utc)
        result = self.schedules.update_one(
            {"date": date, "account_id": account_id, "content_id": content_id},
            {"$set": update},
        )
        if result.matched_count == 0:
            logger.critical(
                "update_posting_state matched 0 docs! date=%s account_id=%s "
                "content_id=%s state=%s — possible hallucinated ID from agent",
                date, account_id, content_id, state_str,
            )

    def get_todays_pending_posts(self, date: str) -> list[ContentSchedule]:
        docs = self.schedules.find({
            "date": date,
            "posting_state": {"$in": [PostingState.SCHEDULED.value, PostingState.FAILED.value]},
        })
        results = []
        for d in docs:
            sched = ContentSchedule.from_mongo(d)
            if sched.posting_state == PostingState.FAILED and sched.retry_count >= sched.max_retries:
                continue
            results.append(sched)
        return results

    # --- Cancel / Edit ---

    def cancel_content(self, content_id: str) -> bool:
        """Cancel a content item. Removes pending schedules, marks variants cancelled.

        Returns True if content was found and cancelled.
        """
        item = self.get_content_item(content_id)
        if not item:
            return False

        if item.uniqualization_status == UniqualizationStatus.DONE:
            self.schedules.delete_many({
                "content_id": content_id,
                "posting_state": {"$in": [
                    PostingState.SCHEDULED.value,
                    PostingState.FAILED.value,
                ]},
            })

        self.variants.update_many(
            {"content_id": content_id, "status": {"$nin": ["posted"]}},
            {"$set": {"status": VariantStatus.FAILED.value, "error": "cancelled"}},
        )
        self.items.update_one(
            {"content_id": content_id},
            {"$set": {
                "uniqualization_status": UniqualizationStatus.CANCELLED.value,
                "distribution_status": "cancelled",
            }},
        )
        logger.info("Cancelled content: %s", content_id)
        return True

    def update_content_caption(self, content_id: str, new_caption: str) -> bool:
        """Update original caption. Only works for PENDING items (pre-uniqualization)."""
        result = self.items.update_one(
            {
                "content_id": content_id,
                "uniqualization_status": {"$in": [
                    UniqualizationStatus.PENDING.value,
                ]},
            },
            {"$set": {"original_caption": new_caption}},
        )
        return result.modified_count > 0

    def get_recent_items(self, limit: int = 10) -> list[ContentItem]:
        """Get most recently uploaded content items."""
        docs = self.items.find().sort("uploaded_at", DESCENDING).limit(limit)
        return [ContentItem.from_mongo({k: v for k, v in d.items() if k != "_id"}) for d in docs]

    def get_all_posted_today(self, date: str) -> list[ContentSchedule]:
        """Get all successfully posted schedules for a date."""
        docs = self.schedules.find({
            "date": date,
            "posting_state": PostingState.POSTED.value,
        })
        return [ContentSchedule.from_mongo(d) for d in docs]

    # --- Stats ---

    def get_content_stats(self) -> dict:
        total = self.items.count_documents({})
        pending = self.items.count_documents({"uniqualization_status": "pending"})
        processing = self.items.count_documents({"uniqualization_status": "processing"})
        done = self.items.count_documents({"uniqualization_status": "done"})
        failed = self.items.count_documents({"uniqualization_status": "failed"})
        posted_variants = self.variants.count_documents({"status": "posted"})
        return {
            "total_items": total,
            "pending": pending,
            "processing": processing,
            "done": done,
            "failed": failed,
            "posted_variants": posted_variants,
        }
