"""Long-term memory storage for Eidola using MongoDB."""

from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from ..config import settings


class AgentMemory:
    """
    Long-term memory for tracking interactions across sessions.

    Stores:
    - Interacted posts (to avoid re-engaging)
    - Interacted accounts (with timestamps)
    - Comment history (to avoid repetition)
    - Nurtured accounts list
    """

    def __init__(
        self,
        mongo_uri: str | None = None,
        db_name: str | None = None,
    ):
        """
        Initialize agent memory.

        Args:
            mongo_uri: MongoDB connection URI.
            db_name: Database name.
        """
        uri = mongo_uri or settings.mongo_uri
        database = db_name or settings.mongo_db_name

        self.client: AsyncIOMotorClient = AsyncIOMotorClient(uri)
        self.db: AsyncIOMotorDatabase = self.client[database]

        # Collections
        self.posts_collection = self.db["interacted_posts"]
        self.accounts_collection = self.db["interacted_accounts"]
        self.comments_collection = self.db["comment_history"]
        self.nurtured_collection = self.db["nurtured_accounts"]

    # --- Post Interactions ---

    async def record_post_interaction(
        self,
        user_id: str,
        post_id: str,
        action: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Record an interaction with a post.

        Args:
            user_id: Our account identifier.
            post_id: Instagram post ID.
            action: Type of interaction (like, comment, share).
            metadata: Additional data about the interaction.
        """
        await self.posts_collection.update_one(
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

    async def has_interacted_with_post(
        self,
        user_id: str,
        post_id: str,
        action: str | None = None,
    ) -> bool:
        """
        Check if we've interacted with a post.

        Args:
            user_id: Our account identifier.
            post_id: Instagram post ID.
            action: Specific action to check for, or None for any.

        Returns:
            True if interacted, False otherwise.
        """
        query = {"user_id": user_id, "post_id": post_id}
        if action:
            query["actions"] = action

        result = await self.posts_collection.find_one(query)
        return result is not None

    # --- Account Interactions ---

    async def record_account_interaction(
        self,
        user_id: str,
        target_account: str,
        action: str,
    ) -> None:
        """
        Record an interaction with an account.

        Args:
            user_id: Our account identifier.
            target_account: Instagram username we interacted with.
            action: Type of interaction.
        """
        await self.accounts_collection.update_one(
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

    async def get_last_interaction_with_account(
        self,
        user_id: str,
        target_account: str,
    ) -> datetime | None:
        """
        Get the last interaction time with an account.

        Args:
            user_id: Our account identifier.
            target_account: Instagram username.

        Returns:
            Last interaction datetime or None.
        """
        result = await self.accounts_collection.find_one(
            {"user_id": user_id, "target_account": target_account}
        )
        if result:
            return result.get("last_interaction")
        return None

    # --- Comment History ---

    async def record_comment(
        self,
        user_id: str,
        post_id: str,
        comment_text: str,
    ) -> None:
        """
        Record a comment we made.

        Args:
            user_id: Our account identifier.
            post_id: Instagram post ID.
            comment_text: The comment text.
        """
        await self.comments_collection.insert_one(
            {
                "user_id": user_id,
                "post_id": post_id,
                "comment_text": comment_text,
                "created_at": datetime.now(timezone.utc),
            }
        )

    async def get_recent_comments(
        self,
        user_id: str,
        limit: int = 20,
    ) -> list[str]:
        """
        Get recent comments for variety checking.

        Args:
            user_id: Our account identifier.
            limit: Number of recent comments to return.

        Returns:
            List of recent comment texts.
        """
        cursor = (
            self.comments_collection.find({"user_id": user_id})
            .sort("created_at", -1)
            .limit(limit)
        )

        comments = []
        async for doc in cursor:
            comments.append(doc["comment_text"])

        return comments

    # --- Nurtured Accounts ---

    async def add_nurtured_account(
        self,
        user_id: str,
        target_account: str,
        priority: int = 1,
    ) -> None:
        """
        Add an account to the nurtured list.

        Args:
            user_id: Our account identifier.
            target_account: Instagram username to nurture.
            priority: Priority level (higher = more attention).
        """
        await self.nurtured_collection.update_one(
            {"user_id": user_id, "target_account": target_account},
            {
                "$set": {
                    "priority": priority,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {
                    "user_id": user_id,
                    "target_account": target_account,
                    "added_at": datetime.now(timezone.utc),
                },
            },
            upsert=True,
        )

    async def get_nurtured_accounts(
        self,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """
        Get list of nurtured accounts.

        Args:
            user_id: Our account identifier.

        Returns:
            List of nurtured account records.
        """
        cursor = (
            self.nurtured_collection.find({"user_id": user_id})
            .sort("priority", -1)
        )

        accounts = []
        async for doc in cursor:
            accounts.append(
                {
                    "account": doc["target_account"],
                    "priority": doc.get("priority", 1),
                }
            )

        return accounts

    async def is_nurtured_account(
        self,
        user_id: str,
        target_account: str,
    ) -> bool:
        """
        Check if an account is in the nurtured list.

        Args:
            user_id: Our account identifier.
            target_account: Instagram username to check.

        Returns:
            True if nurtured, False otherwise.
        """
        result = await self.nurtured_collection.find_one(
            {"user_id": user_id, "target_account": target_account}
        )
        return result is not None

    async def remove_nurtured_account(
        self,
        user_id: str,
        target_account: str,
    ) -> None:
        """
        Remove an account from the nurtured list.

        Args:
            user_id: Our account identifier.
            target_account: Instagram username to remove.
        """
        await self.nurtured_collection.delete_one(
            {"user_id": user_id, "target_account": target_account}
        )

    async def close(self) -> None:
        """Close the MongoDB connection."""
        self.client.close()
