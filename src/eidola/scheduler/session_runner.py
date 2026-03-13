"""
SessionRunner - Manages session lifecycle with real-time budget.

Replaces the old action_budget system with actual time-based sessions.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator

logger = logging.getLogger("eidola.scheduler.session_runner")


@dataclass
class SessionBudget:
    """
    Real-time session budget based on actual elapsed time.
    
    Unlike action_budget which estimated time from action counts,
    this tracks actual wall-clock time.
    """
    duration_seconds: int
    start_time: float = field(default_factory=time.monotonic)
    
    # Rate limiting (per hour, scaled to session)
    likes_limit: int = 30
    comments_limit: int = 10
    saves_limit: int = 20
    follows_limit: int = 10
    
    # Counters
    likes_done: int = 0
    comments_done: int = 0
    saves_done: int = 0
    follows_done: int = 0
    posts_viewed: int = 0
    
    @property
    def elapsed_seconds(self) -> float:
        """Seconds elapsed since session start."""
        return time.monotonic() - self.start_time
    
    @property
    def remaining_seconds(self) -> float:
        """Seconds remaining in session."""
        return max(0, self.duration_seconds - self.elapsed_seconds)
    
    @property
    def progress_percent(self) -> float:
        """Session progress as percentage."""
        return min(100, (self.elapsed_seconds / self.duration_seconds) * 100)
    
    @property
    def is_exhausted(self) -> bool:
        """Check if session time is up."""
        return self.remaining_seconds <= 0
    
    def can_like(self) -> bool:
        """Check if we can like (within rate limit)."""
        hourly_rate = (self.likes_done / max(1, self.elapsed_seconds)) * 3600
        return hourly_rate < self.likes_limit
    
    def can_comment(self) -> bool:
        """Check if we can comment (within rate limit)."""
        hourly_rate = (self.comments_done / max(1, self.elapsed_seconds)) * 3600
        return hourly_rate < self.comments_limit
    
    def can_save(self) -> bool:
        """Check if we can save (within rate limit)."""
        hourly_rate = (self.saves_done / max(1, self.elapsed_seconds)) * 3600
        return hourly_rate < self.saves_limit
    
    def record_like(self):
        self.likes_done += 1
    
    def record_comment(self):
        self.comments_done += 1
    
    def record_save(self):
        self.saves_done += 1
    
    def record_follow(self):
        self.follows_done += 1
    
    def record_post_viewed(self):
        self.posts_viewed += 1
    
    def get_status(self) -> dict:
        """Get current session status."""
        return {
            "elapsed_seconds": round(self.elapsed_seconds),
            "remaining_seconds": round(self.remaining_seconds),
            "progress_percent": round(self.progress_percent, 1),
            "is_exhausted": self.is_exhausted,
            "posts_viewed": self.posts_viewed,
            "likes": self.likes_done,
            "comments": self.comments_done,
            "saves": self.saves_done,
            "follows": self.follows_done,
        }


class SessionRunner:
    """
    Runs Instagram agent sessions with time-based budgeting.
    
    This replaces the Orchestrator's action_budget checking with
    real wall-clock time tracking.
    """
    
    def __init__(
        self,
        device_ip: str,
        account_id: str,
        instagram_username: str,
    ):
        """
        Initialize session runner.
        
        Args:
            device_ip: FIRERPA device IP
            account_id: Account identifier for memory
            instagram_username: Instagram @username
        """
        self.device_ip = device_ip
        self.account_id = account_id
        self.instagram_username = instagram_username
        self.budget: SessionBudget | None = None
    
    async def run_session(
        self,
        mode: str,
        duration_seconds: int,
        config: dict[str, Any],
        rate_limits: dict[str, int] = None,
    ) -> AsyncIterator[str]:
        """
        Run an agent session with the specified mode and duration.
        
        Args:
            mode: Session mode (feed_scroll, active_engage, etc.)
            duration_seconds: How long the session should run
            config: Mode-specific configuration
            rate_limits: Rate limits from schedule
            
        Yields:
            Progress messages
        """
        # Initialize budget
        self.budget = SessionBudget(
            duration_seconds=duration_seconds,
            likes_limit=rate_limits.get("likes_per_hour", 30) if rate_limits else 30,
            comments_limit=rate_limits.get("comments_per_hour", 10) if rate_limits else 10,
            saves_limit=rate_limits.get("saves_per_hour", 20) if rate_limits else 20,
            follows_limit=rate_limits.get("follows_per_hour", 10) if rate_limits else 10,
        )
        
        logger.info(
            f"Starting session: mode={mode}, duration={duration_seconds}s, "
            f"config={list(config.keys())}"
        )
        
        yield f"Session started: mode={mode}, duration={duration_seconds // 60} min"
        
        try:
            # Import here to avoid circular imports
            from ..agents import create_instagram_agent
            from ..memory.sync_memory import SyncAgentMemory
            from ..tools.memory_tools import set_memory
            
            from google.adk.apps import App
            from google.adk.apps.app import EventsCompactionConfig
            from google.adk.agents.context_cache_config import ContextCacheConfig
            from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
            from google.adk.models import Gemini
            from google.adk.runners import Runner
            from google.genai import types
            
            from ..config import settings
            from ..memory.windowed_session import WindowedSessionService
            
            # Initialize memory
            memory = None
            try:
                import uuid as _uuid
                _session_id = str(_uuid.uuid4())
                memory = SyncAgentMemory()
                if memory.is_connected():
                    set_memory(
                        memory, self.account_id, self.instagram_username,
                        session_id=_session_id,
                    )
                    yield "Memory connected"
            except Exception as e:
                logger.warning(f"Memory init failed: {e}")
                yield f"Memory unavailable: {e}"
            
            # Create agent with mode-specific config
            agent = create_instagram_agent(
                device_ip=self.device_ip,
                mode=mode,
                mode_config=config,
            )
            
            # Create app and runner with context management
            session_service = WindowedSessionService(
                max_events=50,
                compress_xml=True,
            )
            app = App(
                name="eidola",
                root_agent=agent,
                events_compaction_config=EventsCompactionConfig(
                    compaction_interval=10,
                    overlap_size=2,
                    summarizer=LlmEventSummarizer(
                        llm=Gemini(model="gemini-2.5-flash"),
                    ),
                ),
                context_cache_config=ContextCacheConfig(
                    min_tokens=settings.context_cache_min_tokens,
                    ttl_seconds=settings.context_cache_ttl,
                    cache_intervals=settings.context_cache_intervals,
                ),
            )
            runner = Runner(app=app, session_service=session_service)
            
            # Create session with budget in state
            session = await session_service.create_session(
                user_id=self.account_id,
                app_name="eidola",
                state={
                    "mode": mode,
                    "our_username": self.instagram_username,
                    "session_duration_seconds": duration_seconds,
                    "mode_config": config,
                },
            )
            
            yield f"Agent ready, starting {mode} mode"
            
            # Build task based on mode
            task = self._build_task(mode, config)
            
            user_message = types.Content(
                role="user",
                parts=[types.Part(text=task)],
            )
            
            # Run agent loop with time-based termination
            turn_count = 0
            max_turns = 100  # Safety limit (was 200)
            
            while turn_count < max_turns and not self.budget.is_exhausted:
                turn_count += 1
                
                async for event in runner.run_async(
                    user_id=self.account_id,
                    session_id=session.id,
                    new_message=user_message,
                ):
                    # Check budget after each event
                    if self.budget.is_exhausted:
                        logger.info("Session time exhausted")
                        break
                    
                    # Log progress periodically
                    if turn_count % 5 == 0:
                        status = self.budget.get_status()
                        yield (
                            f"Progress: {status['progress_percent']}%, "
                            f"posts={status['posts_viewed']}, "
                            f"likes={status['likes']}, "
                            f"comments={status['comments']}"
                        )
                
                # Check if we should continue
                if self.budget.is_exhausted:
                    break
                
                # Send continue message
                user_message = types.Content(
                    role="user",
                    parts=[types.Part(text="continue")],
                )
            
            # Session complete
            final_status = self.budget.get_status()
            yield (
                f"Session complete: {final_status['elapsed_seconds']}s, "
                f"posts={final_status['posts_viewed']}, "
                f"likes={final_status['likes']}, "
                f"comments={final_status['comments']}"
            )
            
        except Exception as e:
            logger.error(f"Session error: {e}", exc_info=True)
            yield f"Session error: {e}"
            raise
        
        finally:
            if memory:
                memory.close()
    
    def _build_task(self, mode: str, config: dict[str, Any]) -> str:
        """Build the task instruction based on mode."""
        
        duration_min = self.budget.duration_seconds // 60
        
        tasks = {
            "feed_scroll": (
                f"Browse the Instagram feed for about {duration_min} minutes. "
                f"Scroll through posts naturally, occasionally liking interesting content. "
                f"For nurtured accounts, engage more actively."
            ),
            "active_engage": (
                f"Actively engage with Instagram feed for {duration_min} minutes. "
                f"Like interesting posts, comment naturally on compelling content. "
                f"Follow any CTAs in posts from nurtured accounts. "
                f"Take screenshots before commenting to understand the content."
            ),
            "nurture_accounts": (
                f"Focus on engaging with VIP/nurtured accounts for {duration_min} minutes. "
                f"Visit their profiles, like recent posts, leave thoughtful short comments. "
                f"ALWAYS follow CTAs in their posts (if they say 'comment TYPE', comment exactly 'TYPE')."
            ),
            "respond": (
                f"Check notifications and DMs for {duration_min} minutes. "
                f"Respond to comments on your posts, reply to DMs. "
                f"Keep responses brief and friendly."
            ),
        }
        
        return tasks.get(mode, f"Browse Instagram for {duration_min} minutes.")
    
    def check_budget(self) -> dict:
        """Get current budget status (for agent tools to call)."""
        if not self.budget:
            return {"error": "No active session"}
        return self.budget.get_status()
    
    def record_action(self, action_type: str):
        """Record an action (for tracking rate limits)."""
        if not self.budget:
            return
        
        if action_type == "like":
            self.budget.record_like()
        elif action_type == "comment":
            self.budget.record_comment()
        elif action_type == "save":
            self.budget.record_save()
        elif action_type == "follow":
            self.budget.record_follow()
        elif action_type == "view":
            self.budget.record_post_viewed()
