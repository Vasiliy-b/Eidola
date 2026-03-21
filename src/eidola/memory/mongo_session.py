"""MongoDB-based session service for ADK."""

import uuid
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from ..config import settings

# Try to import from google.adk.sessions
# Fall back to creating a minimal Session class if not available
try:
    from google.adk.sessions import BaseSessionService, Session, Event
    _HAS_ADK_SESSIONS = True
except ImportError:
    _HAS_ADK_SESSIONS = False

    # Minimal Session implementation for compatibility
    class Session:
        """Minimal Session class when ADK is not available."""

        def __init__(
            self,
            id: str,
            app_name: str,
            user_id: str,
            state: dict[str, Any] | None = None,
            events: list | None = None,
            last_update_time: float | None = None,
        ):
            self.id = id
            self.app_name = app_name
            self.user_id = user_id
            self.state = state or {}
            self.events = events or []
            self.last_update_time = last_update_time or datetime.now(timezone.utc).timestamp()

    class Event:
        """Minimal Event class when ADK is not available."""
        pass

    class BaseSessionService:
        """Minimal base class when ADK is not available."""
        pass


class MongoSessionService(BaseSessionService):
    """
    Custom SessionService that persists sessions to MongoDB.

    This allows session state to survive restarts and enables
    long-term memory tracking across multiple runs.
    """

    def __init__(
        self,
        mongo_uri: str | None = None,
        db_name: str | None = None,
    ):
        """
        Initialize MongoDB session service.

        Args:
            mongo_uri: MongoDB connection URI. Defaults to config setting.
            db_name: Database name. Defaults to config setting.
        """
        uri = mongo_uri or settings.mongo_uri
        database = db_name or settings.mongo_db_name

        self.client: AsyncIOMotorClient = AsyncIOMotorClient(uri)
        self.db: AsyncIOMotorDatabase = self.client[database]
        self.sessions_collection = self.db["sessions"]
        self.events_collection = self.db["events"]

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
    ) -> Session:
        """
        Create a new session and persist it to MongoDB.

        Args:
            app_name: Application name.
            user_id: User identifier (e.g., Instagram account).
            state: Initial session state.

        Returns:
            New Session object.
        """
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        initial_state = state or {}

        # Add tracking fields to state
        initial_state.setdefault("session_started_at", now.isoformat())
        initial_state.setdefault("actions_count", 0)
        initial_state.setdefault("likes_count", 0)
        initial_state.setdefault("comments_count", 0)

        session_doc = {
            "_id": session_id,
            "app_name": app_name,
            "user_id": user_id,
            "state": initial_state,
            "events": [],
            "created_at": now,
            "updated_at": now,
        }

        await self.sessions_collection.insert_one(session_doc)

        return Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            state=initial_state,
            events=[],
            last_update_time=now.timestamp(),
        )

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> Session | None:
        """
        Retrieve a session from MongoDB.

        Args:
            app_name: Application name.
            user_id: User identifier.
            session_id: Session ID.

        Returns:
            Session if found, None otherwise.
        """
        session_doc = await self.sessions_collection.find_one(
            {
                "_id": session_id,
                "app_name": app_name,
                "user_id": user_id,
            }
        )

        if not session_doc:
            return None

        return Session(
            id=session_doc["_id"],
            app_name=session_doc["app_name"],
            user_id=session_doc["user_id"],
            state=session_doc.get("state", {}),
            events=session_doc.get("events", []),
            last_update_time=session_doc.get("updated_at", datetime.now(timezone.utc)).timestamp(),
        )

    async def list_sessions(
        self,
        *,
        app_name: str,
        user_id: str,
    ) -> list[Session]:
        """
        List all sessions for a user.

        Args:
            app_name: Application name.
            user_id: User identifier.

        Returns:
            List of Session objects.
        """
        cursor = self.sessions_collection.find(
            {"app_name": app_name, "user_id": user_id}
        ).sort("created_at", -1)

        sessions = []
        async for doc in cursor:
            sessions.append(
                Session(
                    id=doc["_id"],
                    app_name=doc["app_name"],
                    user_id=doc["user_id"],
                    state=doc.get("state", {}),
                    events=doc.get("events", []),
                    last_update_time=doc.get("updated_at", datetime.now(timezone.utc)).timestamp(),
                )
            )

        return sessions

    async def delete_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> None:
        """
        Delete a session from MongoDB.

        Args:
            app_name: Application name.
            user_id: User identifier.
            session_id: Session ID.
        """
        await self.sessions_collection.delete_one(
            {
                "_id": session_id,
                "app_name": app_name,
                "user_id": user_id,
            }
        )

        # Also delete associated events
        await self.events_collection.delete_many({"session_id": session_id})

    async def append_event(
        self,
        session: Session,
        event: Any,
    ) -> Session:
        """
        Append an event to a session and update state.

        This is called by the ADK Runner after each agent interaction
        to save the event history and update session state.

        Args:
            session: The session to update.
            event: The event to append (contains state delta).

        Returns:
            Updated Session object.
        """
        now = datetime.now(timezone.utc)

        # Serialize event for storage
        event_doc = {
            "session_id": session.id,
            "timestamp": now,
            "event_data": self._serialize_event(event),
        }

        # Insert event into events collection
        await self.events_collection.insert_one(event_doc)

        # Extract state delta from event if available (defensive access)
        state_delta = {}
        if hasattr(event, "actions"):
            actions = event.actions
            if actions is not None and hasattr(actions, "state_delta"):
                state_delta = actions.state_delta or {}

        # Update session with new event and state changes
        update_ops = {
            "$push": {"events": event_doc["event_data"]},
            "$set": {"updated_at": now},
        }

        # Apply state delta
        if state_delta:
            for key, value in state_delta.items():
                update_ops["$set"][f"state.{key}"] = value

        await self.sessions_collection.update_one(
            {"_id": session.id},
            update_ops,
        )

        # Update the session object in memory (use serialized data for consistency)
        session.events.append(event_doc["event_data"])
        if state_delta:
            session.state.update(state_delta)
        session.last_update_time = now.timestamp()

        return session

    def _serialize_event(self, event: Any) -> dict:
        """Serialize an event for MongoDB storage."""
        try:
            # Try to convert event to dict if it has a method for that
            if hasattr(event, "to_dict"):
                return event.to_dict()
            elif hasattr(event, "__dict__"):
                return {
                    "id": getattr(event, "id", str(uuid.uuid4())),
                    "author": getattr(event, "author", "unknown"),
                    "content": str(getattr(event, "content", "")),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            else:
                return {"raw": str(event)}
        except Exception:
            return {"raw": str(event)}

    async def update_session_state(
        self,
        session_id: str,
        state_updates: dict[str, Any],
    ) -> None:
        """
        Update session state in MongoDB.

        Args:
            session_id: Session ID.
            state_updates: Dictionary of state keys to update.
        """
        update_fields = {f"state.{k}": v for k, v in state_updates.items()}
        update_fields["updated_at"] = datetime.now(timezone.utc)

        await self.sessions_collection.update_one(
            {"_id": session_id},
            {"$set": update_fields},
        )

    async def increment_counter(
        self,
        session_id: str,
        counter_name: str,
        amount: int = 1,
    ) -> int:
        """
        Atomically increment a counter in session state.

        Args:
            session_id: Session ID.
            counter_name: Name of the counter field.
            amount: Amount to increment by.

        Returns:
            New counter value.
        """
        result = await self.sessions_collection.find_one_and_update(
            {"_id": session_id},
            {
                "$inc": {f"state.{counter_name}": amount},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
            return_document=True,
        )

        if result:
            return result.get("state", {}).get(counter_name, 0)
        return 0

    async def close(self) -> None:
        """Close the MongoDB connection."""
        self.client.close()
