"""Posting scheduler — bridge between content system and fleet scheduler.

Provides functions that fleet_scheduler.py calls to check if an account
has content to post today, upload it to the device, and update status.
"""

import logging
from datetime import date

from ..config import load_device_config
from .device_uploader import cleanup_device_posting_folder, upload_content_to_device
from .models import PostingState, VariantStatus
from .mongo_content import ContentStore

logger = logging.getLogger("eidola.content.posting_scheduler")

_store: ContentStore | None = None


def get_store() -> ContentStore:
    global _store
    if _store is None:
        _store = ContentStore()
    return _store


def has_pending_post(account_id: str, today: str | None = None) -> bool:
    """Check if account has content scheduled to post today."""
    day = today or date.today().isoformat()
    store = get_store()
    sched = store.get_schedule_for_account(account_id, day)
    if not sched:
        return False
    if sched.posting_state == PostingState.FAILED and sched.retry_count >= sched.max_retries:
        return False
    # Include intermediate states (ON_DEVICE, UPLOADING) so content retries
    # if agent crashed before calling report_posting_result
    return sched.posting_state in (
        PostingState.SCHEDULED, PostingState.FAILED,
        PostingState.ON_DEVICE, PostingState.UPLOADING,
    )


def get_posting_info(account_id: str, today: str | None = None) -> dict | None:
    """Get posting details for today's scheduled post.

    Returns dict with content_id, posting_flow, caption, media_count
    or None if no post scheduled.
    """
    day = today or date.today().isoformat()
    store = get_store()
    sched = store.get_schedule_for_account(account_id, day)
    if not sched:
        return None
    if sched.posting_state not in (PostingState.SCHEDULED, PostingState.FAILED):
        return None
    if sched.posting_state == PostingState.FAILED and sched.retry_count >= sched.max_retries:
        return None

    variant = store.get_variant(sched.content_id, account_id)
    if not variant:
        return None
    # Accept any non-posted variant (ready, scheduled, on_device after failed attempt, etc.)
    if variant.status == VariantStatus.POSTED:
        return None

    return {
        "content_id": sched.content_id,
        "posting_flow": sched.posting_flow.value,
        "caption": variant.caption,
        "media_count": len(variant.media),
        "media": [m.model_dump() if hasattr(m, "model_dump") else m for m in variant.media],
    }


def prepare_device_for_posting(device, account_id: str, today: str | None = None) -> dict | None:
    """Upload content to device and return manifest info.

    Called by fleet_scheduler BEFORE launching the agent subprocess.
    The agent reads manifest.json from /sdcard/DCIM/ToPost/.

    Args:
        device: FIRERPA device instance
        account_id: Account that will post
        today: Date string (YYYY-MM-DD), defaults to today

    Returns:
        Dict with posting info (flow, caption, media_count) or None on failure.
    """
    day = today or date.today().isoformat()
    store = get_store()
    sched = store.get_schedule_for_account(account_id, day)
    if not sched:
        return None

    content_item = store.get_content_item(sched.content_id)
    if not content_item:
        logger.error("Content item not found: %s", sched.content_id)
        return None

    variant = store.get_variant(sched.content_id, account_id)
    if not variant:
        logger.error("Variant not found: %s / %s", sched.content_id, account_id)
        return None

    # Update state: uploading
    store.update_posting_state(day, account_id, sched.content_id, PostingState.UPLOADING)

    success = upload_content_to_device(device, variant, content_item, store)
    if not success:
        store.update_posting_state(
            day, account_id, sched.content_id,
            PostingState.FAILED, error="Upload to device failed",
        )
        return None

    # Update state: on_device
    store.update_posting_state(day, account_id, sched.content_id, PostingState.ON_DEVICE)

    return {
        "content_id": sched.content_id,
        "posting_flow": content_item.posting_flow.value,
        "caption": variant.caption,
        "media_count": len(variant.media),
    }


def mark_posting_result(
    account_id: str,
    content_id: str,
    success: bool,
    error: str | None = None,
    today: str | None = None,
) -> None:
    """Called after agent completes posting attempt."""
    day = today or date.today().isoformat()
    store = get_store()

    if success:
        store.update_posting_state(day, account_id, content_id, PostingState.POSTED)
        store.update_variant_status(content_id, account_id, VariantStatus.POSTED)
        logger.info("Post successful: %s on %s", content_id, account_id)
        try:
            from ..bot.alerts import alert_posting_success
            alert_posting_success(account_id, content_id)
        except Exception:
            pass
    else:
        store.update_posting_state(
            day, account_id, content_id,
            PostingState.FAILED, error=error,
        )
        logger.warning("Post failed: %s on %s — %s", content_id, account_id, error)
        try:
            from ..bot.alerts import alert_posting_failed
            alert_posting_failed(account_id, content_id, error or "unknown error")
        except Exception:
            pass


def cleanup_after_posting(device, account_id: str, content_id: str) -> None:
    """Clean device posting folder after successful post."""
    cleanup_device_posting_folder(device)
    logger.info("[%s] Cleaned device after posting %s", account_id, content_id)
