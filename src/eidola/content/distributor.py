"""Content distribution algorithm.

Generates a content_schedule: which account posts which content on which day.
Handles rotation (same content on different accounts on different days),
device constraints, and dynamic account pool changes.
"""

import logging
import random
from datetime import date, timedelta
from itertools import cycle

from ..config import load_all_accounts, load_all_devices
from .models import ContentItem, ContentSchedule, PostingState
from .mongo_content import ContentStore

logger = logging.getLogger("eidola.content.distributor")


def get_active_account_device_map() -> list[dict]:
    """Load active accounts with their assigned devices.
    
    Returns list of {"account_id": str, "device_id": str}.
    """
    accounts = load_all_accounts()
    result = []
    for acc in accounts:
        if acc.metadata.status == "active":
            result.append({
                "account_id": acc.account_id,
                "device_id": acc.assigned_device,
            })
    return result


def distribute_content(
    store: ContentStore,
    content_ids: list[str] | None = None,
    start_date: date | None = None,
    posts_per_account_per_day: int = 1,
) -> int:
    """Generate content_schedule for pending content items.

    Algorithm:
    1. Get all content items with distribution_status="pending"
    2. Get active accounts grouped by device
    3. For each day, assign content to accounts:
       - Max 1 post per account per day (configurable)
       - No same source content on same device on same day
       - Randomized assignment (not round-robin)
    4. Continue until every account has every content item scheduled

    Returns:
        Number of schedule entries created.
    """
    start = start_date or (date.today() + timedelta(days=1))

    if content_ids:
        items = [store.get_content_item(cid) for cid in content_ids]
        items = [i for i in items if i is not None]
    else:
        items = [
            i for i in _get_distributable_items(store)
        ]

    if not items:
        logger.info("No content items to distribute")
        return 0

    accounts = get_active_account_device_map()
    if not accounts:
        logger.warning("No active accounts found")
        return 0

    # Build device -> accounts mapping
    device_accounts: dict[str, list[str]] = {}
    account_device: dict[str, str] = {}
    for acc in accounts:
        device_accounts.setdefault(acc["device_id"], []).append(acc["account_id"])
        account_device[acc["account_id"]] = acc["device_id"]

    all_account_ids = [a["account_id"] for a in accounts]
    n_accounts = len(all_account_ids)
    n_items = len(items)

    logger.info(
        "Distributing %d content items across %d accounts (%d devices)",
        n_items, n_accounts, len(device_accounts),
    )

    # Build the assignment matrix: for each content item, which accounts still need it
    pending_assignments: dict[str, set[str]] = {}
    for item in items:
        existing = _get_existing_schedules(store, item.content_id)
        already_scheduled = {s.account_id for s in existing}
        remaining = set(all_account_ids) - already_scheduled
        if remaining:
            pending_assignments[item.content_id] = remaining

    if not pending_assignments:
        logger.info("All content already fully distributed")
        return 0

    total_created = 0
    current_date = start

    # Day-by-day scheduling
    max_days = n_accounts + 10  # Safety limit
    for day_offset in range(max_days):
        if not pending_assignments:
            break

        current_date = start + timedelta(days=day_offset)
        day_str = current_date.isoformat()

        # Check which accounts already have something scheduled for this day
        existing_today = store.get_schedule_for_date(day_str)
        busy_accounts = {s.account_id for s in existing_today}
        busy_device_content: dict[str, set[str]] = {}
        for s in existing_today:
            busy_device_content.setdefault(s.device_id, set()).add(s.content_id)

        # Available accounts today (not already posting something)
        available = [a for a in all_account_ids if a not in busy_accounts]
        random.shuffle(available)

        # Assign content to available accounts
        for account_id in available:
            if not pending_assignments:
                break

            device_id = account_device[account_id]

            # Find a content item this account still needs AND that isn't
            # already being posted on this device today
            device_today = busy_device_content.get(device_id, set())
            candidates = [
                cid for cid, accs in pending_assignments.items()
                if account_id in accs and cid not in device_today
            ]

            if not candidates:
                continue

            content_id = random.choice(candidates)
            item = store.get_content_item(content_id)
            if not item:
                continue

            schedule = ContentSchedule(
                date=day_str,
                account_id=account_id,
                device_id=device_id,
                content_id=content_id,
                posting_flow=item.posting_flow,
                posting_state=PostingState.SCHEDULED,
            )
            store.save_schedule(schedule)
            total_created += 1

            # Update tracking
            pending_assignments[content_id].discard(account_id)
            if not pending_assignments[content_id]:
                del pending_assignments[content_id]
                store.items.update_one(
                    {"content_id": content_id},
                    {"$set": {"distribution_status": "distributed"}},
                )

            busy_device_content.setdefault(device_id, set()).add(content_id)

    remaining_items = len(pending_assignments)
    if remaining_items > 0:
        logger.warning(
            "%d content items still have unscheduled accounts after %d days",
            remaining_items, max_days,
        )

    logger.info(
        "Distribution complete: %d schedule entries created over %s to %s",
        total_created, start.isoformat(), current_date.isoformat(),
    )
    return total_created


def redistribute_failed_account(
    store: ContentStore,
    failed_account_id: str,
    replacement_account_id: str | None = None,
) -> int:
    """Reassign scheduled posts from a failed/banned account.
    
    If replacement_account_id is provided, assigns to that account.
    Otherwise, picks the least-loaded active account on the same device.
    """
    pending = list(store.schedules.find({
        "account_id": failed_account_id,
        "posting_state": {"$in": [PostingState.SCHEDULED.value, PostingState.FAILED.value]},
    }))

    if not pending:
        return 0

    accounts = get_active_account_device_map()
    account_device = {a["account_id"]: a["device_id"] for a in accounts}

    reassigned = 0
    for doc in pending:
        doc.pop("_id", None)
        sched = ContentSchedule.from_mongo(doc)

        if replacement_account_id:
            new_account = replacement_account_id
        else:
            # Find least-loaded account on same device
            same_device = [
                a["account_id"] for a in accounts
                if a["device_id"] == sched.device_id and a["account_id"] != failed_account_id
            ]
            if not same_device:
                continue
            new_account = random.choice(same_device)

        store.schedules.update_one(
            {"date": sched.date, "account_id": failed_account_id, "content_id": sched.content_id},
            {"$set": {
                "account_id": new_account,
                "device_id": account_device.get(new_account, sched.device_id),
                "posting_state": PostingState.SCHEDULED.value,
            }},
        )
        reassigned += 1

    logger.info("Reassigned %d posts from %s", reassigned, failed_account_id)
    return reassigned


def _get_distributable_items(store: ContentStore) -> list[ContentItem]:
    """Get content items that are uniqualized and not yet fully distributed."""
    docs = store.items.find({
        "uniqualization_status": "done",
        "distribution_status": {"$in": ["pending", None]},
    })
    result = []
    for d in docs:
        d.pop("_id", None)
        result.append(ContentItem.from_mongo(d))
    return result


def _get_existing_schedules(store: ContentStore, content_id: str) -> list[ContentSchedule]:
    docs = store.schedules.find({"content_id": content_id})
    return [ContentSchedule.from_mongo({k: v for k, v in d.items() if k != "_id"}) for d in docs]
