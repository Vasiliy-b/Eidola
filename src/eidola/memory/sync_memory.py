"""Synchronous memory client using PyMongo for FunctionTools.

ADK FunctionTools must be synchronous. Motor (async) doesn't work here because:
1. run_until_complete() crashes when called from within async context
2. ADK already runs in an event loop

PyMongo is thread-safe by default and works in any context.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import ConnectionFailure, PyMongoError

from ..config import settings

logger = logging.getLogger("eidola.memory.sync")


class SyncAgentMemory:
    """
    Synchronous memory client for use in FunctionTools.
    
    Thread-safe, works in sync and async contexts.
    Stores:
    - Post interactions (to avoid re-engaging)
    - Account interactions (with timestamps)
    - Comment history (for variety)
    - Nurtured accounts list
    """
    
    def __init__(
        self,
        mongo_uri: str | None = None,
        db_name: str | None = None,
    ):
        """Initialize sync memory client."""
        uri = mongo_uri or settings.mongo_uri
        database = db_name or settings.mongo_db_name
        
        # PyMongo is thread-safe by default
        self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        self.db: Database = self.client[database]
        
        # Collections
        self.posts = self.db["interacted_posts"]
        self.accounts = self.db["interacted_accounts"]
        self.comments = self.db["comment_history"]
        self.nurtured = self.db["nurtured_accounts"]
        self.nurtured_visits = self.db["nurtured_visits"]
        
        # Ensure indexes
        self._ensure_indexes()
        
        logger.info(f"SyncAgentMemory connected to {uri}")
    
    def _ensure_indexes(self) -> None:
        """Create indexes for efficient queries."""
        try:
            # Post interactions - unique per user+post
            self.posts.create_index(
                [("user_id", 1), ("post_id", 1)],
                unique=True,
                background=True,
            )
            # Account interactions
            self.accounts.create_index(
                [("user_id", 1), ("target_account", 1)],
                unique=True,
                background=True,
            )
            # Comments - for recent lookup
            self.comments.create_index(
                [("user_id", 1), ("created_at", -1)],
                background=True,
            )
            # Nurtured visits - for rotation queries
            self.nurtured_visits.create_index(
                [("user_id", 1), ("target_account", 1)],
                background=True,
            )
            self.nurtured_visits.create_index(
                [("user_id", 1), ("visited_at", -1)],
                background=True,
            )
            # Daily comment limits — one random limit per account per calendar day
            self.db["daily_limits"].create_index(
                [("user_id", 1), ("date", 1)],
                unique=True,
                background=True,
            )
            self.db["daily_limits"].create_index(
                "created_at", expireAfterSeconds=172800, background=True,
            )
            # Session comment budgets — per-session comment counters
            self.db["session_budgets"].create_index(
                [("user_id", 1), ("session_id", 1)],
                unique=True,
                background=True,
            )
            self.db["session_budgets"].create_index(
                "created_at", expireAfterSeconds=86400, background=True,
            )
        except PyMongoError as e:
            logger.warning(f"Index creation failed (may already exist): {e}")
    
    def is_connected(self) -> bool:
        """Check if MongoDB connection is alive."""
        try:
            self.client.admin.command("ping")
            return True
        except ConnectionFailure:
            return False
    
    # --- Post ID Generation ---
    
    @staticmethod
    def generate_post_id(
        author_username: str,
        timestamp_text: str | None = None,
        caption_snippet: str | None = None,  # DEPRECATED: not used in hash anymore
    ) -> str:
        """
        Generate stable composite post ID from visible information.

        Uses date-bucket normalization: relative timestamps like "18h" and "19h"
        are converted to the same calendar date (e.g. "2026-03-10"), producing
        a stable hash across sessions.

        Args:
            author_username: Post author's @username
            timestamp_text: Relative or absolute timestamp shown on post
            caption_snippet: DEPRECATED - ignored for stability

        Returns:
            Composite ID like "author_abc123def"
        """
        import re as _re
        from datetime import datetime, timedelta, timezone

        author = author_username.lower().strip().lstrip("@")

        parts = [author]
        if not timestamp_text or not timestamp_text.strip():
            import uuid
            parts.append(f"nots_{uuid.uuid4().hex[:8]}")
        elif timestamp_text:
            ts = timestamp_text.lower().strip()
            now = datetime.now(timezone.utc)
            date_bucket = None

            # Extract post type for disambiguation
            post_type = "photo"
            if _re.search(r'\ba\s+video\b', ts):
                post_type = "video"
            elif _re.search(r'\ba\s+carousel\b', ts):
                post_type = "carousel"
            elif _re.search(r'\breel\b', ts):
                post_type = "reel"
            parts.append(post_type)

            # Months → approximate (before minutes to avoid "3months" matching "3m")
            m = _re.search(r'(\d+)\s*months?\s*(?:ago)?', ts)
            if m:
                date_bucket = (now - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")

            # Minutes → same day
            if not date_bucket:
                m = _re.search(r'(\d+)\s*m(?:in(?:utes?)?)?\s*(?:ago)?', ts)
            if m:
                date_bucket = now.strftime("%Y-%m-%d")

            # Hours → compute date
            if not date_bucket:
                m = _re.search(r'(\d+)\s*h(?:ours?)?\s*(?:ago)?', ts)
                if m:
                    date_bucket = (now - timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%d")

            # Days
            if not date_bucket:
                m = _re.search(r'(\d+)\s*d(?:ays?)?\s*(?:ago)?', ts)
                if m:
                    date_bucket = (now - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")

            # Weeks
            if not date_bucket:
                m = _re.search(r'(\d+)\s*w(?:eeks?)?\s*(?:ago)?', ts)
                if m:
                    date_bucket = (now - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")

            # Absolute date ("February 9", "December 7, 2023")
            if not date_bucket:
                date_match = _re.search(
                    r'(january|february|march|april|may|june|july|august|'
                    r'september|october|november|december)\s+\d{1,2}(?:,?\s*\d{4})?', ts
                )
                if date_match:
                    date_bucket = date_match.group(0)

            # "just now" / "seconds ago"
            if not date_bucket:
                if "just now" in ts or "second" in ts:
                    date_bucket = now.strftime("%Y-%m-%d")

            if date_bucket:
                parts.append(date_bucket)
            else:
                parts.append(ts)

        composite = "|".join(parts)
        hash_value = hashlib.sha256(composite.encode()).hexdigest()[:12]

        return f"{author}_{hash_value}"
    
    # --- Post Interactions ---
    
    def has_interacted_with_post(
        self,
        user_id: str,
        post_id: str,
        action: str | None = None,
    ) -> bool:
        """
        Check if we've interacted with a post.
        
        Args:
            user_id: Our account identifier
            post_id: Composite post ID
            action: Specific action to check (like/comment) or None for any
            
        Returns:
            True if interacted, False otherwise
        """
        try:
            query = {"user_id": user_id, "post_id": post_id}
            if action:
                query["actions"] = action
            
            result = self.posts.find_one(query)
            return result is not None
        except PyMongoError as e:
            logger.error(f"Error checking post interaction: {e}")
            return False  # Will be converted to skip=True at tool level
    
    def record_post_interaction(
        self,
        user_id: str,
        post_id: str,
        action: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """
        Record an interaction with a post.
        
        Args:
            user_id: Our account identifier
            post_id: Composite post ID
            action: Type of interaction (like, comment, share, save)
            metadata: Additional data (post_author, etc.)
            
        Returns:
            True if recorded, False on error
        """
        try:
            self.posts.update_one(
                {"user_id": user_id, "post_id": post_id},
                {
                    "$set": {
                        "last_action": action,
                        "last_interaction": datetime.now(timezone.utc),
                        "metadata": metadata or {},
                    },
                    "$addToSet": {"actions": action},
                    "$setOnInsert": {
                        "user_id": user_id,
                        "post_id": post_id,
                        "first_seen": datetime.now(timezone.utc),
                    },
                },
                upsert=True,
            )
            return True
        except PyMongoError as e:
            logger.error(f"Error recording post interaction: {e}")
            return False
    
    # --- Comment History ---
    
    def record_comment(
        self,
        user_id: str,
        post_id: str,
        comment_text: str,
    ) -> bool:
        """Record a comment we made."""
        try:
            self.comments.insert_one({
                "user_id": user_id,
                "post_id": post_id,
                "comment_text": comment_text,
                "created_at": datetime.now(timezone.utc),
            })
            return True
        except PyMongoError as e:
            logger.error(f"Error recording comment: {e}")
            return False
    
    def get_comment_count_24h(self, user_id: str) -> int:
        """Count comments made in the last 24 hours for this account.
        
        Uses the comment_history collection which stores comments with
        created_at timestamps and has an index on (user_id, created_at).
        
        Args:
            user_id: Our account identifier
            
        Returns:
            Number of comments in last 24 hours (0 on error)
        """
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            count = self.comments.count_documents({
                "user_id": user_id,
                "created_at": {"$gte": cutoff},
            })
            return count
        except PyMongoError as e:
            logger.error(f"Error counting 24h comments: {e}")
            return 0
    
    def get_recent_comments(
        self,
        user_id: str,
        limit: int = 20,
    ) -> list[str]:
        """Get recent comments for variety checking."""
        try:
            cursor = (
                self.comments.find({"user_id": user_id})
                .sort("created_at", -1)
                .limit(limit)
            )
            return [doc["comment_text"] for doc in cursor]
        except PyMongoError as e:
            logger.error(f"Error getting recent comments: {e}")
            return []
    
    # --- Account Interactions ---
    
    def record_account_interaction(
        self,
        user_id: str,
        target_account: str,
        action: str,
    ) -> bool:
        """Record interaction with an account."""
        try:
            self.accounts.update_one(
                {"user_id": user_id, "target_account": target_account},
                {
                    "$set": {
                        "last_action": action,
                        "last_interaction": datetime.now(timezone.utc),
                    },
                    "$inc": {"interaction_count": 1},
                    "$setOnInsert": {
                        "user_id": user_id,
                        "target_account": target_account,
                        "first_interaction": datetime.now(timezone.utc),
                    },
                },
                upsert=True,
            )
            return True
        except PyMongoError as e:
            logger.error(f"Error recording account interaction: {e}")
            return False
    
    def get_last_interaction_with_account(
        self,
        user_id: str,
        target_account: str,
    ) -> datetime | None:
        """Get the last interaction time with an account."""
        try:
            result = self.accounts.find_one(
                {"user_id": user_id, "target_account": target_account}
            )
            if result:
                return result.get("last_interaction")
            return None
        except PyMongoError as e:
            logger.error(f"Error getting account interaction: {e}")
            return None
    
    # --- Nurtured Accounts ---
    
    # Priority string to numeric mapping for sorting
    PRIORITY_MAP = {"vip": 100, "high": 75, "medium": 50, "low": 25}
    
    @staticmethod
    def normalize_username(username: str) -> str:
        """
        Normalize Instagram username for consistent storage and lookup.
        
        Handles:
        - Lowercase conversion
        - Whitespace stripping
        - Leading @ removal
        - Preserves dots and underscores (they're part of the username)
        
        Args:
            username: Instagram username (may have @ prefix, whitespace, etc.)
            
        Returns:
            Normalized username ready for storage/lookup
        """
        if not username:
            return ""
        # Remove @ prefix if present
        username = username.lstrip("@")
        # Strip whitespace and lowercase
        username = username.strip().lower()
        return username
    
    def add_nurtured_account(
        self,
        user_id: str,
        target_account: str,
        priority: str | int = "medium",
        notes: str = "",
    ) -> bool:
        """Add account to nurtured list.
        
        Args:
            user_id: Account identifier
            target_account: Instagram username to nurture
            priority: "vip", "high", "medium", "low" or numeric 1-100
            notes: Optional notes about the account
        """
        try:
            # Convert string priority to numeric for sorting
            if isinstance(priority, str):
                priority_lower = priority.lower()
                if priority_lower not in self.PRIORITY_MAP:
                    logger.warning(f"Unknown priority '{priority}' for {target_account}, using 'medium'")
                    priority_lower = "medium"
                priority_num = self.PRIORITY_MAP[priority_lower]
                priority_str = priority_lower
            else:
                priority_num = priority
                priority_str = "medium"
            
            # Normalize username for consistent storage
            normalized_account = self.normalize_username(target_account)
            
            self.nurtured.update_one(
                {"user_id": user_id, "target_account": normalized_account},
                {
                    "$set": {
                        "priority": priority_num,
                        "priority_level": priority_str,
                        "notes": notes,
                        "updated_at": datetime.now(timezone.utc),
                    },
                    "$setOnInsert": {
                        "user_id": user_id,
                        "target_account": normalized_account,
                        "added_at": datetime.now(timezone.utc),
                    },
                },
                upsert=True,
            )
            return True
        except PyMongoError as e:
            logger.error(f"Error adding nurtured account: {e}")
            return False
    
    def get_nurtured_accounts(self, user_id: str) -> list[dict[str, Any]]:
        """Get list of nurtured accounts sorted by priority (highest first)."""
        try:
            cursor = self.nurtured.find({"user_id": user_id}).sort("priority", -1)
            return [
                {
                    "account": doc["target_account"],
                    "priority": doc.get("priority_level", "medium"),
                    "notes": doc.get("notes", ""),
                }
                for doc in cursor
            ]
        except PyMongoError as e:
            logger.error(f"Error getting nurtured accounts: {e}")
            return []
    
    def is_nurtured_account(self, user_id: str, target_account: str) -> dict:
        """Check if account is nurtured. Returns dict with priority info."""
        try:
            # Normalize username for consistent lookup
            # This ensures dots, underscores, and case are handled consistently
            normalized_account = self.normalize_username(target_account)
            
            result = self.nurtured.find_one(
                {"user_id": user_id, "target_account": normalized_account}
            )
            
            if result:
                return {
                    "is_nurtured": True,
                    "priority": result.get("priority_level", "medium"),
                    "notes": result.get("notes", ""),
                }
            
            return {"is_nurtured": False}
        except PyMongoError as e:
            logger.error(f"Error checking nurtured status: {e}")
            return {"is_nurtured": False, "error": str(e)}
    
    # --- Nurtured Visit Tracking (Round-Robin Rotation) ---
    
    def record_nurtured_visit(
        self,
        user_id: str,
        target_account: str,
        device_id: str | None = None,
    ) -> bool:
        """
        Record a profile visit to a nurtured account.
        
        Args:
            user_id: Our account identifier
            target_account: Nurtured account username visited
            device_id: Optional device identifier
            
        Returns:
            True if recorded, False on error
        """
        try:
            normalized = self.normalize_username(target_account)
            self.nurtured_visits.insert_one({
                "user_id": user_id,
                "target_account": normalized,
                "visited_at": datetime.now(timezone.utc),
                "device_id": device_id or "",
            })
            logger.info(f"Recorded nurtured visit: {user_id} → {normalized}")
            return True
        except PyMongoError as e:
            logger.error(f"Error recording nurtured visit: {e}")
            return False
    
    def get_next_nurtured_to_visit(
        self,
        user_id: str,
        nurtured_list: list[str],
        exclude_usernames: list[str] | None = None,
        top_n: int = 5,
    ) -> dict[str, Any]:
        """
        Pick a nurtured account to visit using randomized top-N selection.
        
        Logic:
        1. Normalize and filter out already-visited accounts (this session)
        2. Query nurtured_visits for visit history
        3. Sort by oldest/never-visited first
        4. Pick randomly from top-N candidates (not always the first)
        
        Args:
            user_id: Our account identifier
            nurtured_list: List of nurtured account usernames to rotate through
            exclude_usernames: Accounts already visited this session (skip them)
            top_n: Pick randomly from this many least-recently-visited candidates
            
        Returns:
            Dict with username, last_visited (ISO string or None), reason
        """
        import random
        
        if not nurtured_list:
            return {
                "username": None,
                "last_visited": None,
                "reason": "No nurtured accounts provided",
            }
        
        try:
            normalized = [self.normalize_username(u) for u in nurtured_list]
            
            # Filter out accounts already visited this session
            if exclude_usernames:
                excluded_set = {self.normalize_username(u) for u in exclude_usernames}
                normalized = [u for u in normalized if u not in excluded_set]
                if not normalized:
                    return {
                        "username": None,
                        "last_visited": None,
                        "reason": f"All {len(excluded_set)} nurtured accounts already visited this session",
                    }
            
            # Aggregate: get the MAX(visited_at) per target_account for this user
            pipeline = [
                {"$match": {
                    "user_id": user_id,
                    "target_account": {"$in": normalized},
                }},
                {"$group": {
                    "_id": "$target_account",
                    "last_visited": {"$max": "$visited_at"},
                }},
            ]
            visited_map: dict[str, datetime] = {}
            for doc in self.nurtured_visits.aggregate(pipeline):
                visited_map[doc["_id"]] = doc["last_visited"]
            
            candidates = []
            for username in normalized:
                last = visited_map.get(username)
                candidates.append((username, last))
            
            # Sort: None (never visited) first, then oldest visited_at
            candidates.sort(key=lambda x: (x[1] is not None, x[1] or datetime.min.replace(tzinfo=timezone.utc)))
            
            # Randomized top-N: pick from the N least-recently-visited
            pool_size = min(top_n, len(candidates))
            pool = candidates[:pool_size]
            winner, last_visited = random.choice(pool)
            
            if last_visited is None:
                reason = f"Never visited (random from top-{pool_size})"
            else:
                reason = f"Oldest visit: {last_visited.isoformat()} (random from top-{pool_size})"
            
            return {
                "username": winner,
                "last_visited": last_visited.isoformat() if last_visited else None,
                "reason": reason,
                "pool_size": pool_size,
                "total_candidates": len(candidates),
            }
        except PyMongoError as e:
            logger.error(f"Error getting next nurtured to visit: {e}")
            fallback = self.normalize_username(nurtured_list[random.randint(0, len(nurtured_list) - 1)])
            return {
                "username": fallback,
                "last_visited": None,
                "reason": f"Random fallback (DB error): {e}",
            }
    
    @staticmethod
    def get_nurtured_for_device(
        all_accounts: list[dict],
        device_id: str,
        total_devices: int,
    ) -> list[dict]:
        """Deterministically assign nurtured accounts to devices.
        
        VIP accounts: ALL devices get them (shared engagement).
        HIGH/MEDIUM accounts: Hash-based sharding — each device gets a unique subset.
        This prevents all 10 devices from visiting the same medium-priority accounts.
        
        Args:
            all_accounts: Full list of nurtured account dicts with 'account' and 'priority' keys
            device_id: Device identifier (e.g., "phone_01")
            total_devices: Total number of active devices
            
        Returns:
            Filtered list of accounts for this specific device
        """
        if total_devices <= 1:
            return all_accounts
        
        result = []
        for acc in all_accounts:
            priority = acc.get("priority", "medium")
            if priority == "vip":
                result.append(acc)
            else:
                # Hash-based sharding for non-VIP accounts
                username = acc.get("account", "")
                shard = hash(username) % total_devices
                device_idx = hash(device_id) % total_devices
                if shard == device_idx:
                    result.append(acc)
        
        # Always return at least VIP accounts
        return result if result else [a for a in all_accounts if a.get("priority") == "vip"]
    
    # --- Daily Comment Limits (MongoDB-backed, survives ContextVar loss) ---

    def get_or_create_daily_comment_limit(
        self, user_id: str, min_limit: int = 9, max_limit: int = 15,
    ) -> int:
        """Get today's comment limit, creating it if it doesn't exist.

        Uses findOneAndUpdate with $setOnInsert + upsert for atomic
        create-if-not-exists. Randomized once per UTC calendar day.
        """
        import random

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            result = self.db["daily_limits"].find_one_and_update(
                {"user_id": user_id, "date": today},
                {"$setOnInsert": {
                    "comment_limit": random.randint(min_limit, max_limit),
                    "created_at": datetime.now(timezone.utc),
                }},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return result["comment_limit"]
        except PyMongoError as e:
            logger.error(f"Error getting daily limit: {e}")
            return min_limit

    # --- Session Comment Budgets (MongoDB-backed) ---

    def create_session_budget(
        self, user_id: str, session_id: str, comments_limit: int,
    ) -> dict:
        """Create session budget doc (atomic upsert)."""
        try:
            result = self.db["session_budgets"].find_one_and_update(
                {"user_id": user_id, "session_id": session_id},
                {"$setOnInsert": {
                    "comments_limit": comments_limit,
                    "comments_done": 0,
                    "created_at": datetime.now(timezone.utc),
                }},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return result
        except PyMongoError as e:
            logger.error(f"Error creating session budget: {e}")
            return {"comments_limit": comments_limit, "comments_done": 0}

    def increment_session_comments(self, user_id: str, session_id: str) -> dict:
        """Atomically increment session comment count. Returns updated state."""
        try:
            result = self.db["session_budgets"].find_one_and_update(
                {"user_id": user_id, "session_id": session_id},
                {"$inc": {"comments_done": 1}},
                return_document=ReturnDocument.AFTER,
            )
            if result:
                return {
                    "comments_done": result["comments_done"],
                    "comments_limit": result["comments_limit"],
                    "can_comment": result["comments_done"] < result["comments_limit"],
                }
            return {"comments_done": 0, "comments_limit": 3, "can_comment": False}
        except PyMongoError as e:
            logger.error(f"Error incrementing session comments: {e}")
            return {"comments_done": 0, "comments_limit": 3, "can_comment": False}

    def get_session_comment_budget(self, user_id: str, session_id: str) -> dict:
        """Read current session comment budget."""
        try:
            doc = self.db["session_budgets"].find_one(
                {"user_id": user_id, "session_id": session_id}
            )
            if doc:
                return {
                    "comments_done": doc.get("comments_done", 0),
                    "comments_limit": doc.get("comments_limit", 5),
                    "can_comment": doc["comments_done"] < doc["comments_limit"],
                }
            return {"comments_done": 0, "comments_limit": 5, "can_comment": True}
        except PyMongoError:
            return {"comments_done": 0, "comments_limit": 5, "can_comment": False}

    def close(self) -> None:
        """Close MongoDB connection."""
        self.client.close()
        logger.debug("SyncAgentMemory connection closed")
