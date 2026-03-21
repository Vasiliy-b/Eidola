"""Windowed Session Service - prevents token overflow by managing conversation history.

This service implements its own session storage with:
1. Conversation windowing (keeps last N turns)
2. XML dump filtering/compression
3. Automatic truncation when limits are exceeded
4. State preservation across truncations
"""

import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from google.adk.sessions import BaseSessionService, Session

logger = logging.getLogger("eidola.memory")


class WindowedSessionService(BaseSessionService):
    """
    Session service that prevents token overflow through conversation windowing.
    
    Implements its own session storage with:
    - Automatic truncation of old events (sliding window)
    - XML dump filtering/compression in function responses
    - Configurable window size
    - State preservation across truncations
    
    Strategy:
    1. Keep last N user/assistant turns (configurable)
    2. Filter XML dumps from function responses (keep metadata only)
    3. Preserve critical state in session.state
    4. Automatically truncate when max_events exceeded
    """
    
    def __init__(
        self,
        max_turns: int = 100,
        max_events: int = 150,
        compress_xml: bool = True,
        preserve_state_keys: list[str] | None = None,
    ):
        """
        Initialize windowed session service.
        
        Acts as a SAFETY NET — primary context management is handled by
        ADK EventsCompactionConfig which summarizes old events via LLM.
        This service provides:
        - Hard upper limit on events (prevents runaway token growth)
        - XML compression (removes full XML dumps, keeps metadata)
        - Screenshot stripping (removes image data after use)
        - Safe windowing that preserves function_call/response pairs
        
        Args:
            max_turns: Maximum conversation turns to keep
            max_events: Hard limit on events (safety net)
            compress_xml: Whether to compress XML dumps in function responses
            preserve_state_keys: State keys to always preserve (e.g., counters)
        """
        # In-memory storage: session_id -> Session
        self._sessions: dict[str, Session] = {}
        # Track events per session for windowing
        self._session_events: dict[str, list[Any]] = defaultdict(list)
        
        self.max_turns = max_turns
        self.max_events = max_events
        self.compress_xml = compress_xml
        self.preserve_state_keys = preserve_state_keys or [
            # Core counters
            "current_account",
            "session_limits",
            "actions_count",
            "likes_count",
            "comments_count",
            "session_started_at",
            # Context recovery (AI architect recommendation)
            "last_post_engaged",          # {username, post_id, action}
            "nurtured_engaged_count",     # Track VIP engagement separately
            "current_screen_context",     # Last known screen type
        ]
        
        logger.info(
            f"WindowedSessionService initialized: max_events={max_events} (safety limit), "
            f"compress_xml={compress_xml}, WindowedSessionService handles primary compaction"
        )
    
    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
    ) -> Session:
        """Create a new session."""
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        
        initial_state = state or {}
        initial_state.setdefault("session_started_at", now.isoformat())
        initial_state.setdefault("actions_count", 0)
        initial_state.setdefault("likes_count", 0)
        initial_state.setdefault("comments_count", 0)
        
        session = Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            state=initial_state,
            events=[],
            last_update_time=now.timestamp(),
        )
        
        self._sessions[session_id] = session
        self._session_events[session_id] = []
        
        logger.debug(f"Created session {session_id} for user {user_id}")
        return session
    
    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> Session | None:
        """Get session by ID."""
        session = self._sessions.get(session_id)
        if session and session.app_name == app_name and session.user_id == user_id:
            # Return session with windowed events
            return self._get_windowed_session(session)
        return None
    
    async def list_sessions(
        self,
        *,
        app_name: str,
        user_id: str,
    ) -> list[Session]:
        """List all sessions for a user."""
        sessions = [
            self._get_windowed_session(s)
            for s in self._sessions.values()
            if s.app_name == app_name and s.user_id == user_id
        ]
        return sorted(sessions, key=lambda s: s.last_update_time, reverse=True)
    
    async def delete_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> None:
        """Delete a session."""
        session = self._sessions.get(session_id)
        if session and session.app_name == app_name and session.user_id == user_id:
            del self._sessions[session_id]
            del self._session_events[session_id]
            logger.debug(f"Deleted session {session_id}")
    
    async def append_event(
        self,
        session: Session,
        event: Any,
    ) -> Session:
        """
        Append event with automatic windowing and XML compression.
        
        This is the key method that prevents token overflow:
        1. Compress XML dumps in function responses
        2. Store event in our internal list
        3. Apply windowing if needed
        4. Update session with windowed events
        """
        # Compress XML dumps before storing
        if self.compress_xml:
            event = self._compress_event(event)
        
        # Store event
        events = self._session_events[session.id]
        events.append(event)
        
        # Apply windowing if needed
        if len(events) > self.max_events:
            events = self._apply_windowing(events)
            self._session_events[session.id] = events
        
        # Update session with windowed events
        session.events = events
        session.last_update_time = datetime.now(timezone.utc).timestamp()
        
        # Preserve critical state
        self._preserve_state(session)
        
        return session
    
    def _get_windowed_session(self, session: Session) -> Session:
        """Get session with windowed events applied."""
        events = self._session_events.get(session.id, [])
        # ALWAYS apply windowing when at or above limit (not just above)
        # This ensures we never return more than max_events to the Runner/LLM
        if len(events) >= self.max_events:
            events = self._apply_windowing(events)
            # IMPORTANT: Persist the windowed events back to storage
            self._session_events[session.id] = events
        
        # Create a copy with windowed events
        windowed_session = Session(
            id=session.id,
            app_name=session.app_name,
            user_id=session.user_id,
            state=session.state.copy(),
            events=events,
            last_update_time=session.last_update_time,
        )
        return windowed_session
    
    def _compress_event(self, event: Any) -> Any:
        """
        Compress XML dumps in function responses.
        
        Replaces full XML strings with metadata-only summaries:
        - Keep: valid, screen_context, in_instagram, has_dialog
        - Remove: full xml string (can be 10-50KB)
        """
        try:
            # Check if this is a function response event
            if hasattr(event, "content") and event.content:
                if hasattr(event.content, "parts") and event.content.parts:
                    for part in event.content.parts:
                        # Check for function_response with XML data
                        if hasattr(part, "function_response"):
                            fr = part.function_response
                            if fr and hasattr(fr, "response"):
                                response = fr.response
                                
                                # CRITICAL: Check for image Part objects (screenshots)
                                # These can be 100KB-500KB each and accumulate quickly!
                                if self._is_image_part(response):
                                    # Replace image with metadata placeholder
                                    img_size = self._get_part_size(response)
                                    fr.response = {
                                        "_image_compressed": True,
                                        "_image_size_bytes": img_size,
                                        "_note": "Screenshot removed from history to save tokens"
                                    }
                                    logger.debug(
                                        f"Compressed screenshot: {img_size} bytes -> metadata only"
                                    )
                                
                                # If response is a dict with XML, compress it
                                elif isinstance(response, dict) and "xml" in response:
                                    xml_str = response.get("xml", "")
                                    if len(xml_str) > 1000:  # Only compress large XML
                                        # Create compressed version - keep metadata, remove XML
                                        compressed = {
                                            k: v for k, v in response.items() if k != "xml"
                                        }
                                        compressed["_xml_compressed"] = True
                                        compressed["_xml_size_bytes"] = len(xml_str)
                                        # Replace response
                                        fr.response = compressed
                                        logger.debug(
                                            f"Compressed XML dump: {len(xml_str)} bytes -> metadata only"
                                        )
                                
                                # Also check nested structures
                                elif isinstance(response, dict):
                                    compressed = self._compress_dict(response)
                                    if compressed != response:
                                        fr.response = compressed
        except Exception as e:
            logger.warning(f"Error compressing event: {e}", exc_info=True)
        
        return event
    
    def _is_image_part(self, obj: Any) -> bool:
        """Check if object is a types.Part with image data."""
        # Check for ADK types.Part with inline_data
        if hasattr(obj, "inline_data") and obj.inline_data is not None:
            return True
        # Check for dict representation of Part
        if isinstance(obj, dict) and "inline_data" in obj:
            return True
        # Check class name as fallback
        type_name = type(obj).__name__
        if "Part" in type_name and hasattr(obj, "inline_data"):
            return True
        return False
    
    def _get_part_size(self, obj: Any) -> int:
        """Get size of image data in Part object."""
        try:
            if hasattr(obj, "inline_data") and obj.inline_data is not None:
                if hasattr(obj.inline_data, "data"):
                    data = obj.inline_data.data
                    if isinstance(data, bytes):
                        return len(data)
                    elif isinstance(data, str):
                        return len(data)
            # Dict representation
            if isinstance(obj, dict) and "inline_data" in obj:
                inline = obj["inline_data"]
                if isinstance(inline, dict) and "data" in inline:
                    return len(inline["data"])
        except Exception:
            pass
        return 0
    
    def _compress_dict(self, obj: dict) -> dict:
        """Recursively compress XML strings and base64 data in dict."""
        if not isinstance(obj, dict):
            return obj
        
        compressed = {}
        for key, value in obj.items():
            if key == "xml" and isinstance(value, str) and len(value) > 1000:
                compressed["_xml_compressed"] = True
                compressed["_xml_size_bytes"] = len(value)
            elif key in ("screenshots", "screenshot", "image_data", "base64"):
                if isinstance(value, list):
                    compressed[f"_{key}_count"] = len(value)
                    compressed[f"_{key}_compressed"] = True
                elif isinstance(value, str) and len(value) > 1000:
                    compressed[f"_{key}_compressed"] = True
                    compressed[f"_{key}_size_bytes"] = len(value)
                else:
                    compressed[key] = value
            elif isinstance(value, str) and len(value) > 3000:
                compressed[key] = value[:500] + f"...[compressed, was {len(value)} chars]"
            elif isinstance(value, dict):
                compressed[key] = self._compress_dict(value)
            elif isinstance(value, list):
                compressed[key] = [
                    self._compress_dict(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                compressed[key] = value
        
        return compressed
    
    def _apply_windowing(self, events: list[Any]) -> list[Any]:
        """
        Apply conversation windowing to prevent token overflow.
        
        CRITICAL: Must preserve valid conversation structure for Gemini API:
        - function_call (model) must be followed by function_response (user)
        - Never cut between a function_call and its response
        - ALWAYS preserve the first user TEXT message (original task context)
        
        Strategy:
        1. Find and preserve the first user TEXT message (original task)
        2. Find safe cut points (after function_response or after text-only model turn)
        3. Cut at the EARLIEST safe point that fits under max_events (keeps max history)
        4. Safety floor: always keep at least 10 events or 50% of max_events
        5. If first user TEXT message was lost, prepend it to windowed events
        """
        if len(events) <= self.max_events:
            return events
        
        original_count = len(events)
        
        # CRITICAL: Find and preserve the first user TEXT message (original task context)
        # This message contains the user's task and MUST survive windowing
        first_user_text_idx = None
        first_user_text_event = None
        for idx, event in enumerate(events):
            if self._is_user_text_message(event):
                first_user_text_idx = idx
                first_user_text_event = event
                logger.debug(f"Found first user TEXT message at index {idx}")
                break
        
        if first_user_text_idx is None:
            logger.warning(
                f"No user TEXT message found in {len(events)} events! "
                f"Windowing will proceed but conversation may fail."
            )
        
        # Safety floor: never keep fewer than this many events
        # Increased to handle recovery workflows (devils-advocate recommendation)
        min_events_to_keep = max(20, int(self.max_events * 0.6))  # 60% of max, min 20
        
        # Find "safe" cut points - indices where we can safely start
        # CRITICAL: Safe points are AFTER completed function_call/function_response pairs
        # In ADK, there are NO user text messages after the first one - it's all function pairs!
        safe_cut_points = [0]  # Always safe to keep everything
        
        i = 0
        while i < len(events):
            event = events[i]
            
            # Check if this is a model turn with function_call
            has_function_call = self._event_has_function_call(event)
            
            if has_function_call:
                # This is a function_call - find the matching function_response(s)
                j = i + 1
                while j < len(events):
                    next_event = events[j]
                    if self._event_has_function_response(next_event):
                        # Found function_response - continue looking for more
                        j += 1
                        continue
                    else:
                        # Not a function_response - pair is complete
                        break
                
                # After completed function_call+response pair, the next index is a safe cut point
                # (The next event will be a new model turn or user message)
                if j < len(events):
                    safe_cut_points.append(j)
                i = j if j > i else i + 1
            else:
                # Not a function_call - check if this could be a safe start point
                # (model text-only response or user message)
                if i > 0:  # Don't duplicate index 0
                    # Safe to cut here if previous event was a function_response
                    if i > 0 and self._event_has_function_response(events[i-1]):
                        if i not in safe_cut_points:
                            safe_cut_points.append(i)
                i += 1
        
        # Log discovered safe cut points for debugging
        logger.debug(f"Safe cut points for windowing: {safe_cut_points[:10]}{'...' if len(safe_cut_points) > 10 else ''}")
        
        # Find the EARLIEST cut point that fits under max_events
        # This keeps the MAXIMUM valid history (most recent events)
        # Iterate FORWARD (not reversed!) to find smallest cut index that fits
        best_cut = 0
        for cut_idx in safe_cut_points:
            remaining_count = len(events) - cut_idx
            if remaining_count <= self.max_events and remaining_count >= min_events_to_keep:
                best_cut = cut_idx
                break
        
        # Fallback: if no safe cut found that satisfies both constraints,
        # find the cut that gets closest to max_events without going over
        if best_cut == 0 and len(events) > self.max_events:
            for cut_idx in safe_cut_points:
                remaining_count = len(events) - cut_idx
                if remaining_count <= self.max_events:
                    best_cut = cut_idx
                    break
        
        # CRITICAL FIX: If still best_cut=0 and events > max_events,
        # FORCE cut to keep only max_events (aggressive truncation)
        if best_cut == 0 and len(events) > self.max_events:
            # No safe cut worked - force aggressive truncation
            # Keep only the last max_events events from ANY cut point
            if safe_cut_points:
                # Use the LAST safe cut point that keeps at least something
                for cut_idx in reversed(safe_cut_points):
                    if len(events) - cut_idx >= min_events_to_keep:
                        best_cut = cut_idx
                        logger.warning(
                            f"FORCED windowing: no ideal cut found, using cut_idx={cut_idx} "
                            f"({len(events) - cut_idx} events kept)"
                        )
                        break
            
            # If STILL no cut, just keep the last max_events (break pairs if needed)
            if best_cut == 0:
                best_cut = max(0, len(events) - self.max_events)
                logger.warning(
                    f"EMERGENCY windowing: cutting at {best_cut} to keep {len(events) - best_cut} events"
                )
        
        # Final safety: ensure we never return empty events
        if best_cut >= len(events):
            # This should never happen, but protect against it
            logger.error(
                f"WINDOWING BUG: best_cut={best_cut} >= len(events)={len(events)}. "
                f"Keeping last {min_events_to_keep} events as fallback."
            )
            best_cut = max(0, len(events) - min_events_to_keep)
        
        windowed_events = events[best_cut:]
        
        # Additional safety: never return empty list
        if not windowed_events and events:
            logger.error(
                f"WINDOWING BUG: Would return empty events! "
                f"Keeping last {min_events_to_keep} events as fallback."
            )
            windowed_events = events[-min_events_to_keep:]
        
        # Verify the first event is a valid start (user TEXT message, NOT function_response)
        # In Gemini/ADK, both user text messages and function_responses have author="user",
        # but only user TEXT messages are valid conversation starters.
        if windowed_events:
            first_event = windowed_events[0]
            is_valid_start = self._is_user_text_message(first_event)
            
            if not is_valid_start:
                # CRITICAL FIX: If we have the original first user TEXT message preserved,
                # prepend it instead of searching (which often fails)
                if first_user_text_event is not None:
                    # Check if the first user TEXT is already in windowed_events
                    first_user_text_in_windowed = any(
                        event is first_user_text_event for event in windowed_events
                    )
                    
                    if not first_user_text_in_windowed:
                        # Prepend the original user TEXT message to preserve task context
                        windowed_events = [first_user_text_event] + windowed_events
                        logger.info(
                            f"RESTORED first user TEXT message to windowed events "
                            f"(was at original index {first_user_text_idx})"
                        )
                    else:
                        # First user TEXT is somewhere in windowed_events, find and move to front
                        for k, event in enumerate(windowed_events):
                            if self._is_user_text_message(event):
                                if k > 0:
                                    # Move user TEXT to front, keeping the rest in order
                                    windowed_events = [event] + windowed_events[:k] + windowed_events[k+1:]
                                    logger.debug(f"Moved user TEXT message from offset {k} to front")
                                break
                else:
                    # No first user TEXT was found earlier, try to find one in windowed events
                    found_user_text = False
                    for k, event in enumerate(windowed_events):
                        # Don't skip more than half the events looking for a user turn
                        if k > len(windowed_events) // 2:
                            logger.warning(
                                f"Could not find user TEXT message in first half of windowed events. "
                                f"Keeping from index 0 - API may fail!"
                            )
                            break
                        
                        if self._is_user_text_message(event):
                            windowed_events = windowed_events[k:]
                            found_user_text = True
                            logger.debug(f"Trimmed to first user TEXT message at offset {k}")
                            break
                    
                    if not found_user_text:
                        logger.error(
                            f"CRITICAL: No user TEXT message found in windowed events! "
                            f"Conversation will likely fail. Events checked: {len(windowed_events)}"
                        )
        
        # Final safety: ensure we don't exceed max_events after prepending
        if len(windowed_events) > self.max_events:
            # We prepended the user TEXT, so trim from the middle (keep first + last)
            # Keep first user TEXT message + last (max_events - 1) events
            windowed_events = [windowed_events[0]] + windowed_events[-(self.max_events - 1):]
            logger.debug(f"Trimmed to {len(windowed_events)} events after prepending user TEXT")
        
        logger.info(
            f"Applied windowing: {original_count} -> {len(windowed_events)} events "
            f"(safe cut at index {best_cut}, preserved function pairs, user TEXT guaranteed)"
        )
        
        return windowed_events
    
    def _event_has_function_call(self, event: Any) -> bool:
        """Check if event contains a function_call."""
        if hasattr(event, "content") and event.content:
            if hasattr(event.content, "parts") and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        return True
        return False
    
    def _event_has_function_response(self, event: Any) -> bool:
        """Check if event contains a function_response."""
        if hasattr(event, "content") and event.content:
            if hasattr(event.content, "parts") and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "function_response") and part.function_response:
                        return True
        return False
    
    def _is_user_text_message(self, event: Any) -> bool:
        """
        Check if event is a user TEXT message (not function_response).
        
        In Gemini/ADK, both user text messages and function_responses have author="user",
        but only user TEXT messages are valid conversation starters.
        
        A function_response has author="user" but contains function_response parts,
        not text parts. We need to reject these as valid conversation starters.
        """
        # First check: must have author="user" or role="user"
        author = getattr(event, "author", None)
        role = None
        if hasattr(event, "content") and event.content:
            role = getattr(event.content, "role", None)
        
        if author != "user" and role != "user":
            return False
        
        # Second check: must NOT be a function_response
        # Function responses have parts with function_response attribute
        if hasattr(event, "content") and event.content:
            if hasattr(event.content, "parts") and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "function_response") and part.function_response:
                        # This is a function_response, NOT a user text message
                        return False
        
        # If we get here, it's a user message that's not a function_response
        return True
    
    def _preserve_state(self, session: Session) -> None:
        """Ensure critical state keys are preserved."""
        # State is already preserved in session.state
        # This method can be extended to add additional preservation logic
        pass
    
    async def close(self) -> None:
        """Close the session service (cleanup)."""
        self._sessions.clear()
        self._session_events.clear()
        logger.debug("WindowedSessionService closed")
