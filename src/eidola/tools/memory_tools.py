"""Memory tools for tracking interactions with MongoDB.

These tools allow the agent to:
1. Generate stable post IDs from visible information
2. Check if already interacted with a post (avoid re-engaging)
3. Record new interactions (for future sessions)
4. Get recent comments (for variety)
5. Manage nurtured accounts list

IMPORTANT: These tools use synchronous PyMongo client, not async Motor.
This is because ADK FunctionTools must be synchronous.

ARCHITECTURE: Uses contextvars for thread-safe, async-safe per-session state.
This allows multiple accounts to run in parallel without state collision.
"""

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from google.adk.tools import FunctionTool

if TYPE_CHECKING:
    from ..memory.sync_memory import SyncAgentMemory

logger = logging.getLogger("eidola.tools.memory")


# =============================================================================
# CONTEXT-BASED STATE (replaces global variables)
# =============================================================================
# Using contextvars ensures each async task/thread has its own isolated state.
# This is critical for multi-account support.

@dataclass
class MemoryContext:
    """Per-session memory context. Thread-safe via contextvars."""
    memory: "SyncAgentMemory"
    account_id: str
    instagram_username: str
    device_id: str | None = None


# ContextVar holds per-context state (async-safe, thread-safe)
_memory_context: ContextVar[MemoryContext | None] = ContextVar(
    "memory_context", default=None
)

# 24-hour rolling comment limit (randomized per day, enforced via MongoDB)
# Range: 12-23 comments per 24h, picked once per session to avoid predictable patterns.
_daily_comment_limit: ContextVar[int | None] = ContextVar(
    "daily_comment_limit", default=None
)


def _get_daily_comment_limit() -> int:
    """Get or generate the daily comment limit (12-23, once per session)."""
    import random
    limit = _daily_comment_limit.get(None)
    if limit is None:
        limit = random.randint(14, 21)
        _daily_comment_limit.set(limit)
        logger.info(f"Daily comment limit set to {limit} (range 14-21)")
    return limit

# Session-level comment dedup (in-memory, fast, no DB round-trip)
# Populated by record_post_interaction(action="comment"), checked by check_post_interaction()
_session_commented_posts: ContextVar[set | None] = ContextVar(
    "session_commented_posts", default=None
)

# Session-level comment TEXT dedup — tracks exact comment texts used this session.
# Prevents the LLM from re-using the same comment on different posts (survives context windowing).
_session_comment_texts: ContextVar[list | None] = ContextVar(
    "session_comment_texts", default=None
)

# Session-level nurtured visit tracking — which accounts were visited this session.
# Prevents visiting the same nurtured account twice in one session.
_session_visited_nurtured: ContextVar[set | None] = ContextVar(
    "session_visited_nurtured", default=None
)


def _get_session_visited_nurtured() -> set:
    """Get or create the per-session set of visited nurtured accounts."""
    s = _session_visited_nurtured.get(None)
    if s is None:
        s = set()
        _session_visited_nurtured.set(s)
    return s


def set_memory(
    memory: "SyncAgentMemory",
    account_id: str,
    instagram_username: str | None = None,
    device_id: str | None = None,
) -> None:
    """
    Initialize memory for this session.
    
    Uses contextvars for thread-safe, async-safe state isolation.
    Each account/session gets its own isolated context.
    
    Args:
        memory: SyncAgentMemory instance
        account_id: Internal account identifier
        instagram_username: Our Instagram @username (for visual comment detection)
    """
    ctx = MemoryContext(
        memory=memory,
        account_id=account_id,
        instagram_username=instagram_username or account_id,
        device_id=device_id,
    )
    _memory_context.set(ctx)
    _session_commented_posts.set(set())
    _session_comment_texts.set([])
    _daily_comment_limit.set(None)
    _session_visited_nurtured.set(None)
    logger.info(
        f"Memory tools initialized: account={account_id}, "
        f"instagram_username={ctx.instagram_username}, "
        f"device_id={ctx.device_id or 'n/a'}"
    )


def get_memory_context() -> MemoryContext | None:
    """Get current memory context (for internal use)."""
    return _memory_context.get()


def _filter_nurtured_for_current_device(accounts: list[dict]) -> list[dict]:
    """Filter nurtured accounts to current device assignment (VIP stays global)."""
    ctx = _memory_context.get()
    if ctx is None or not ctx.device_id:
        return accounts

    try:
        from ..config import load_all_accounts

        username_to_device: dict[str, str] = {}
        for acc_cfg in load_all_accounts():
            username_to_device[acc_cfg.account_id.lower()] = acc_cfg.assigned_device
            username_to_device[acc_cfg.instagram.username.lower()] = acc_cfg.assigned_device

        filtered: list[dict] = []
        for item in accounts:
            username = str(item.get("account", "")).lower().lstrip("@")
            priority = str(item.get("priority", "medium")).lower()
            if priority == "vip":
                filtered.append(item)
                continue
            assigned = username_to_device.get(username)
            if not assigned or assigned == ctx.device_id:
                filtered.append(item)

        logger.info(
            f"Nurtured filter: device={ctx.device_id} "
            f"candidates={len(accounts)} selected={len(filtered)}"
        )
        return filtered
    except Exception as e:
        logger.warning(f"Nurtured device filtering failed: {e}")
        return accounts


def check_comment_text_duplicate(comment_text: str) -> bool:
    """Check if this exact comment text was already used this session.
    
    Returns True if DUPLICATE (should be rejected), False if unique.
    Uses normalized comparison (lowercase, stripped).
    """
    texts = _session_comment_texts.get()
    if texts is None:
        return False
    normalized = comment_text.strip().lower()
    for prev in texts:
        if prev.strip().lower() == normalized:
            logger.warning(f"COMMENT TEXT DEDUP: '{comment_text}' already used this session!")
            return True
    return False


def record_comment_text(comment_text: str) -> None:
    """Record a comment text as used in this session."""
    texts = _session_comment_texts.get()
    if texts is None:
        texts = []
        _session_comment_texts.set(texts)
    texts.append(comment_text)
    logger.info(f"Recorded comment text #{len(texts)}: '{comment_text[:50]}'")


def get_session_comment_texts() -> list[str]:
    """Get all comment texts used in this session (for dedup)."""
    return _session_comment_texts.get() or []


def get_instagram_username() -> str | None:
    """Get our Instagram username for visual detection."""
    ctx = _memory_context.get()
    return ctx.instagram_username if ctx else None


def _ensure_memory() -> tuple["SyncAgentMemory", str] | dict:
    """
    Ensure memory is initialized. 
    
    Returns:
        (memory, account_id) tuple if initialized
        Error dict if not initialized
    """
    ctx = _memory_context.get()
    if ctx is None:
        logger.warning("Memory not initialized - tools will fail-safe")
        return {"error": "Memory not initialized", "skip": True}
    return (ctx.memory, ctx.account_id)


# === Post ID Generation ===

def generate_post_id(
    author_username: str,
    timestamp_text: str | None = None,
    caption_snippet: str | None = None,  # DEPRECATED - ignored for stability
) -> dict:
    """
    Generate a stable post ID from visible information.
    
    Since Instagram doesn't show post IDs in UI, we create a composite ID from:
    - Author username (required)
    - Timestamp text like "2h", "1d", "3 days ago" (required for uniqueness!)
    
    ⚠️ caption_snippet is IGNORED - it caused duplicate comments because
    different screen positions show different caption parts.
    
    Use this BEFORE calling check_post_interaction or record_post_interaction.
    
    Args:
        author_username: Post author's @username
        timestamp_text: Timestamp shown on post - ALWAYS PROVIDE THIS!
        caption_snippet: DEPRECATED - ignored, do not rely on this
        
    Returns:
        post_id: Stable composite ID to use for memory operations
    """
    from ..memory.sync_memory import SyncAgentMemory
    
    post_id = SyncAgentMemory.generate_post_id(
        author_username=author_username,
        timestamp_text=timestamp_text,
        # caption_snippet intentionally not passed for stability
    )
    
    return {
        "post_id": post_id,
        "author": author_username,
        "note": "Use this post_id for check_post_interaction and record_post_interaction",
    }


# === Post Interaction Tools ===

def check_post_interaction(
    author_username: str,
    timestamp_text: str,
    action: str | None = None,
    post_id: str | None = None,  # DEPRECATED - ignored, kept for backwards compat
) -> dict:
    """
    Check if we already interacted with a post.
    
    Call this BEFORE engaging with any post to avoid:
    - Liking posts we already liked
    - Commenting on posts we already commented
    
    ⚠️ IMPORTANT: Pass the EXACT author_username and timestamp_text from the post!
    The function generates a stable post_id internally.
    
    Args:
        author_username: Post author's @username (e.g., "example_user")
        timestamp_text: Timestamp shown on post (e.g., "3 days ago", "2h")
        action: Specific action to check ('like', 'comment') or None for any
        post_id: DEPRECATED - ignored, ID is generated from username+timestamp
        
    Returns:
        - interacted: True if already interacted
        - skip: True if should skip this post
        - post_id: The generated post_id (use this in record_post_interaction)
        
    On error: Returns skip=True (FAIL-SAFE to avoid duplicates)
    """
    result = _ensure_memory()
    if isinstance(result, dict):
        return result  # Error dict
    memory, account_id = result
    
    # Generate stable post_id internally - this is the fix!
    from ..memory.sync_memory import SyncAgentMemory
    generated_post_id = SyncAgentMemory.generate_post_id(
        author_username=author_username,
        timestamp_text=timestamp_text,
    )
    
    try:
        # Layer 1: Fast in-memory session check (no DB round-trip)
        session_set = _session_commented_posts.get()
        if session_set and action == "comment" and generated_post_id in session_set:
            logger.warning(
                f"SESSION DEDUP: Already commented on {generated_post_id} in THIS session!"
            )
            return {
                "post_id": generated_post_id,
                "author": author_username,
                "interacted": True,
                "skip": True,
                "note": "DUPLICATE: Already commented on this post IN THIS SESSION! SKIP immediately!",
            }
        
        # Layer 2: MongoDB check (across all sessions)
        interacted = memory.has_interacted_with_post(
            account_id, generated_post_id, action
        )
        
        if interacted and action == "comment":
            logger.warning(
                f"MONGODB DEDUP: Already commented on {generated_post_id} (previous session)"
            )
        
        return {
            "post_id": generated_post_id,  # Return so agent can use in record_post_interaction
            "author": author_username,
            "interacted": interacted,
            "skip": interacted,
            "note": "DUPLICATE: Already commented on this post! SKIP!" if interacted else "New post, can engage",
        }
    except Exception as e:
        logger.error(f"Error checking post interaction: {e}")
        # FAIL-SAFE: On error, skip to avoid potential duplicate
        return {
            "post_id": generated_post_id,
            "author": author_username,
            "interacted": None,  # Unknown
            "skip": True,  # FAIL-SAFE
            "error": str(e),
            "note": "Memory check failed - skipping to avoid duplicate (fail-safe)",
        }


def record_post_interaction(
    author_username: str,
    timestamp_text: str,
    action: str,
    comment_text: str | None = None,
    post_id: str | None = None,  # DEPRECATED - ignored, kept for backwards compat
    post_author: str | None = None,  # DEPRECATED - use author_username
) -> dict:
    """
    Record an interaction AFTER successfully engaging with a post.
    
    Call this AFTER:
    - Successfully liking a post
    - Successfully posting a comment
    
    ⚠️ IMPORTANT: Pass the SAME author_username and timestamp_text used in check_post_interaction!
    
    Args:
        author_username: Post author's @username (e.g., "example_user")
        timestamp_text: Timestamp shown on post (e.g., "3 days ago", "2h")
        action: Action taken ('like', 'comment', 'share', 'save')
        comment_text: If commenting, the text of the comment (for variety tracking)
        post_id: DEPRECATED - ignored
        post_author: DEPRECATED - use author_username
        
    Returns:
        - recorded: True if successfully recorded
    """
    result = _ensure_memory()
    if isinstance(result, dict):
        # Still return success=False but don't block - recording failure is non-critical
        return {"recorded": False, "error": result.get("error")}
    memory, account_id = result
    
    # Generate stable post_id internally - same logic as check_post_interaction
    from ..memory.sync_memory import SyncAgentMemory
    generated_post_id = SyncAgentMemory.generate_post_id(
        author_username=author_username,
        timestamp_text=timestamp_text,
    )
    
    # ⚠️ COMMENT LIMIT GUARD: 24-hour rolling limit via MongoDB
    if action == "comment":
        count_24h = memory.get_comment_count_24h(account_id)
        max_24h = _get_daily_comment_limit()

        if count_24h >= max_24h:
            logger.warning(
                f"24H COMMENT LIMIT REACHED: {count_24h}/{max_24h} comments in last 24h. "
                f"Blocking comment on {generated_post_id}."
            )
            return {
                "recorded": False,
                "post_id": generated_post_id,
                "action": action,
                "error": f"24H COMMENT LIMIT: {count_24h}/{max_24h} comments in last 24 hours. No more comments allowed!",
                "blocked_by": "mongodb_24h_guard",
                "comments_24h": count_24h,
                "max_24h": max_24h,
            }
        
        # ⚠️ DUPLICATE CHECK Layer 1: Fast session-level check
        session_set = _session_commented_posts.get()
        if session_set and generated_post_id in session_set:
            logger.warning(
                f"SESSION DUPLICATE BLOCKED: Already commented on {generated_post_id} in THIS session!"
            )
            return {
                "recorded": False,
                "post_id": generated_post_id,
                "action": action,
                "error": "DUPLICATE BLOCKED - already commented on this post IN THIS SESSION!",
                "note": "You should have called check_post_interaction() BEFORE commenting!",
            }
        
        # ⚠️ DUPLICATE CHECK Layer 2: MongoDB check (across sessions)
        already_commented = memory.has_interacted_with_post(
            account_id, generated_post_id, "comment"
        )
        if already_commented:
            logger.warning(f"DUPLICATE BLOCKED: Already commented on {generated_post_id}")
            return {
                "recorded": False,
                "post_id": generated_post_id,
                "action": action,
                "error": "DUPLICATE BLOCKED - already commented on this post!",
                "note": "You should have called check_post_interaction() BEFORE commenting!",
            }
    
    try:
        from datetime import datetime, timezone
        
        metadata = {
            "post_author": author_username,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        # Record post interaction
        memory.record_post_interaction(account_id, generated_post_id, action, metadata)
        
        # Reset scroll tracker - we just engaged!
        try:
            from .firerpa_tools import reset_scroll_tracker
            reset_scroll_tracker()
        except ImportError:
            pass  # Scroll tracker not available
        
        # Log to activity logger for human-readable output
        try:
            from ..utils.agent_logging import get_activity_logger
            activity = get_activity_logger()
            
            if action == "like":
                activity.action_like(author_username)
            elif action == "save":
                activity.action_save(author_username)
            # Comments are logged separately with the actual text
        except ImportError:
            pass  # Activity logger not available
        
        # Log 24h comment count after recording
        if action == "comment":
            count_24h = memory.get_comment_count_24h(account_id)
            logger.info(f"24h comment count: {count_24h}/{_get_daily_comment_limit()}")
        
        # If commenting, also record comment text for variety checking
        if action == "comment" and comment_text:
            memory.record_comment(account_id, generated_post_id, comment_text)
            
            # Log comment to activity logger
            try:
                from ..utils.agent_logging import get_activity_logger
                activity = get_activity_logger()
                # Detect if this looks like a CTA comment (short, single word)
                is_cta = len(comment_text.split()) <= 2 and len(comment_text) <= 20
                activity.action_comment(
                    author_username,
                    comment_text,
                    is_cta=is_cta,
                )
            except ImportError:
                pass  # Activity logger not available
        
        # If we know the author, record account interaction
        if post_author:
            memory.record_account_interaction(account_id, post_author, action)
        
        # Track in session-level dedup set (fast in-memory check for same session)
        if action == "comment":
            session_set = _session_commented_posts.get()
            if session_set is not None:
                session_set.add(generated_post_id)
                logger.info(
                    f"Session dedup: added {generated_post_id} "
                    f"(total {len(session_set)} commented posts this session)"
                )
        
        logger.info(f"Recorded {action} on post {generated_post_id}")
        return {
            "recorded": True,
            "post_id": generated_post_id,
            "action": action,
        }
    except Exception as e:
        logger.error(f"Error recording interaction: {e}")
        return {
            "recorded": False,
            "error": str(e),
            "note": "Recording failed but engagement was successful",
        }


def get_recent_comments(limit: int = 10) -> dict:
    """
    Get recent comment TEXTS we made — for VARIETY checking only.
    
    ⚠️ WARNING: This does NOT check if you already commented on a specific post!
    To check if you already commented on THIS post, use: check_post_interaction(author, timestamp, "comment")
    
    Use this to:
    - Avoid writing similar comments
    - Maintain variety in engagement style
    - Check your writing patterns
    
    Args:
        limit: Number of recent comments to return (default 10)
        
    Returns:
        - comments: List of recent comment texts
        - count: Number returned
    """
    result = _ensure_memory()
    if isinstance(result, dict):
        return {"comments": [], "count": 0, "error": result.get("error")}
    memory, account_id = result
    
    try:
        comments = memory.get_recent_comments(account_id, limit)
        return {
            "comments": comments,
            "count": len(comments),
            "note": "Ensure your new comment is different from these",
        }
    except Exception as e:
        logger.error(f"Error getting recent comments: {e}")
        return {"comments": [], "count": 0, "error": str(e)}


# === 24-Hour Comment Limit Tool ===

def get_24h_comment_count() -> dict:
    """
    Check how many comments were made in the last 24 hours.
    
    The system enforces a randomized daily limit (12-23 comments per 24h rolling window)
    across ALL sessions. Call this BEFORE commenting to check remaining budget.
    
    Returns:
        - count: Comments made in last 24 hours
        - limit: Today's randomized limit (12-23)
        - can_comment: True if under the limit
        - remaining: How many more comments allowed
    """
    daily_limit = _get_daily_comment_limit()
    
    result = _ensure_memory()
    if isinstance(result, dict):
        return {
            "count": 0,
            "limit": daily_limit,
            "can_comment": False,
            "remaining": 0,
            "error": result.get("error"),
            "note": "Memory not initialized — commenting blocked for safety",
        }
    memory, account_id = result
    
    try:
        count = memory.get_comment_count_24h(account_id)
        remaining = max(0, daily_limit - count)
        return {
            "count": count,
            "limit": daily_limit,
            "can_comment": count < daily_limit,
            "remaining": remaining,
            "note": f"{count}/{daily_limit} comments used in last 24h"
                    + (f" — {remaining} remaining" if remaining > 0 else " — LIMIT REACHED, no more comments!"),
        }
    except Exception as e:
        logger.error(f"Error checking 24h comment count: {e}")
        return {
            "count": 0,
            "limit": daily_limit,
            "can_comment": False,
            "remaining": 0,
            "error": str(e),
            "note": "Error checking count — commenting blocked for safety",
        }


# === Nurtured Accounts Tools ===

def get_nurtured_accounts() -> dict:
    """
    Get list of accounts we should pay extra attention to.
    
    Nurtured accounts are accounts we actively want to engage with more:
    - Engage more frequently
    - Write more thoughtful comments
    - Respond to their replies
    
    Returns:
        - accounts: List of nurtured usernames with priority
    """
    result = _ensure_memory()
    if isinstance(result, dict):
        return {"accounts": [], "count": 0, "error": result.get("error")}
    memory, account_id = result
    
    try:
        accounts = memory.get_nurtured_accounts(account_id)
        accounts = _filter_nurtured_for_current_device(accounts)
        return {
            "accounts": accounts,
            "count": len(accounts),
        }
    except Exception as e:
        logger.error(f"Error getting nurtured accounts: {e}")
        return {"accounts": [], "count": 0, "error": str(e)}


def is_nurtured_account(username: str) -> dict:
    """
    Check if an account is in our nurtured list.
    
    If True, engage more thoughtfully with their content:
    - Spend more time analyzing their posts
    - Write more personalized comments
    - Higher engagement probability
    
    Priority levels (affects engagement intensity):
    - vip: Maximum engagement, comment on every post
    - high: Strong engagement, comment frequently
    - medium: Regular boosted engagement
    
    Args:
        username: Instagram username to check
        
    Returns:
        - is_nurtured: True if in nurtured list
        - priority: "vip", "high", or "medium" (if nurtured)
        - MUST_ENGAGE: True if this is a VIP that MUST be engaged
        - notes: Any notes about the account (if nurtured)
    """
    result = _ensure_memory()
    if isinstance(result, dict):
        return {"username": username, "is_nurtured": False, "error": result.get("error")}
    memory, account_id = result
    
    try:
        nurtured_result = memory.is_nurtured_account(account_id, username)
        
        if nurtured_result.get("is_nurtured"):
            priority = nurtured_result.get("priority", "medium")
            # VIP and HIGH priority = MUST engage
            must_engage = priority in ("vip", "high")
            
            return {
                "username": username,
                "is_nurtured": True,
                "priority": priority,
                "MUST_ENGAGE": must_engage,
                "notes": nurtured_result.get("notes", ""),
                # Strong signal for the agent
                "action": "⚠️ VIP DETECTED - DO NOT SCROLL AWAY UNTIL ENGAGED!" if must_engage else f"Nurtured ({priority}) - boost engagement",
            }
        
        return {
            "username": username,
            "is_nurtured": False,
        }
    except Exception as e:
        logger.error(f"Error checking nurtured status: {e}")
        return {"username": username, "is_nurtured": False, "error": str(e)}


# === Nurtured Profile Visit Rotation Tools ===

def get_next_nurtured_to_visit() -> dict:
    """
    Get which nurtured profile to visit next (randomized rotation).
    
    Uses randomized top-N selection from least-recently-visited accounts.
    Automatically excludes accounts already visited this session.
    
    Returns:
        - username: The nurtured account to visit next
        - last_visited: ISO timestamp of last visit, or null if never visited
        - reason: Why this account was selected
    """
    result = _ensure_memory()
    if isinstance(result, dict):
        return {"username": None, "error": result.get("error")}
    memory, account_id = result
    
    try:
        nurtured_accounts = memory.get_nurtured_accounts(account_id)
        nurtured_accounts = _filter_nurtured_for_current_device(nurtured_accounts)
        if not nurtured_accounts:
            return {
                "username": None,
                "reason": "No nurtured accounts found for this user",
            }
        
        nurtured_usernames = [acc["account"] for acc in nurtured_accounts]
        
        session_visited = list(_get_session_visited_nurtured())
        
        next_visit = memory.get_next_nurtured_to_visit(
            account_id,
            nurtured_usernames,
            exclude_usernames=session_visited if session_visited else None,
            top_n=5,
        )
        
        return next_visit
    except Exception as e:
        logger.error(f"Error getting next nurtured to visit: {e}")
        return {"username": None, "error": str(e)}


def record_profile_visit(target_username: str) -> dict:
    """
    Record that we visited a nurtured account's profile.
    
    Call this AFTER visiting a profile and engaging with their posts.
    This updates the rotation tracker so next session picks a different account.
    
    Args:
        target_username: Instagram username of the profile we visited
        
    Returns:
        - recorded: True if successfully recorded
        - username: The normalized username recorded
    """
    result = _ensure_memory()
    if isinstance(result, dict):
        return {"recorded": False, "error": result.get("error")}
    memory, account_id = result
    ctx = _memory_context.get()
    
    try:
        success = memory.record_nurtured_visit(
            user_id=account_id,
            target_account=target_username,
            device_id=ctx.device_id if ctx else None,
        )
        
        if success:
            logger.info(f"Recorded profile visit: {account_id} → {target_username}")
            
            # Track in session-level set (prevents revisiting same account this session)
            _get_session_visited_nurtured().add(target_username.lower().lstrip("@"))
            
            try:
                from ..utils.agent_logging import get_activity_logger
                activity = get_activity_logger()
                activity.action_like(f"[profile visit] {target_username}")
            except ImportError:
                pass
        
        return {
            "recorded": success,
            "username": target_username,
            "session_visited_count": len(_get_session_visited_nurtured()),
            "note": "Visit tracked for rotation" if success else "Failed to record",
        }
    except Exception as e:
        logger.error(f"Error recording profile visit: {e}")
        return {"recorded": False, "username": target_username, "error": str(e)}


# === Create FunctionTools ===

# NOTE: generate_post_id is NOT exposed to agent!
# check_post_interaction and record_post_interaction generate IDs internally.
# This prevents the agent from constructing malformed post_ids.

check_post_interaction_tool = FunctionTool(func=check_post_interaction)
record_post_interaction_tool = FunctionTool(func=record_post_interaction)
get_recent_comments_tool = FunctionTool(func=get_recent_comments)
get_24h_comment_count_tool = FunctionTool(func=get_24h_comment_count)
get_nurtured_accounts_tool = FunctionTool(func=get_nurtured_accounts)
is_nurtured_account_tool = FunctionTool(func=is_nurtured_account)
get_next_nurtured_to_visit_tool = FunctionTool(func=get_next_nurtured_to_visit)
record_profile_visit_tool = FunctionTool(func=record_profile_visit)

# Tools for Engager agent
engager_memory_tools = [
    check_post_interaction_tool,
    record_post_interaction_tool,
    get_recent_comments_tool,
    get_24h_comment_count_tool,
    is_nurtured_account_tool,
]

# Tools for Observer agent  
observer_memory_tools = [
    is_nurtured_account_tool,
    get_nurtured_accounts_tool,
]

# All memory tools
all_memory_tools = [
    check_post_interaction_tool,
    record_post_interaction_tool,
    get_recent_comments_tool,
    get_24h_comment_count_tool,
    get_nurtured_accounts_tool,
    is_nurtured_account_tool,
    get_next_nurtured_to_visit_tool,
    record_profile_visit_tool,
]


def create_memory_tools() -> list[FunctionTool]:
    """Create all memory tools for the unified Instagram agent."""
    return all_memory_tools
