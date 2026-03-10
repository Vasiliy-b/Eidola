"""Memory and session management for Eidola."""

from .mongo_memory import AgentMemory
from .mongo_session import MongoSessionService
from .sync_memory import SyncAgentMemory

# Context management: ADK EventsCompactionConfig (primary) + WindowedSessionService (safety net).
# EventsCompaction summarizes old events via LLM. WindowedSessionService enforces
# hard event limits, compresses XML/screenshots, preserves function pairs.

__all__ = [
    "MongoSessionService",
    "AgentMemory",
    "SyncAgentMemory",
]
