"""
Schedule Helper - Simple wrapper for time-based scheduling in fleet launch.

Provides a simple interface for checking if a session should run now
based on schedule.yaml configuration.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .scheduler import Scheduler, ScheduledSession

logger = logging.getLogger("eidola.scheduler.helper")


@dataclass
class SessionDecision:
    """Result of checking if a session should run."""
    should_run: bool
    duration_seconds: Optional[int] = None
    mode: Optional[str] = None
    scheduled_time: Optional[datetime] = None


class ScheduleHelper:
    """
    Simple wrapper around Scheduler for fleet launch flow.
    
    Usage:
        helper = ScheduleHelper("config/schedule.yaml")
        while True:
            decision = helper.should_run_now()
            if decision.should_run:
                run_session(duration=decision.duration_seconds, mode=decision.mode)
                helper.record_session(decision.duration_seconds // 60)
            else:
                sleep(60)  # Check again in 1 minute
    """
    
    def __init__(self, schedule_path: Path | str = "config/schedule.yaml"):
        """
        Initialize schedule helper.
        
        Args:
            schedule_path: Path to schedule.yaml
        """
        self.schedule_path = Path(schedule_path)
        self.scheduler = Scheduler(schedule_path=self.schedule_path)
        self.scheduler.load_config()
        self._last_date = None
    
    def should_run_now(self) -> SessionDecision:
        """
        Check if a session should run right now based on schedule.
        
        Returns:
            SessionDecision with should_run flag and duration/mode if applicable
        """
        # Reset daily counters at midnight
        now = datetime.now(self.scheduler.config.timezone)
        if self._last_date != now.date():
            self.scheduler.config.reset_daily_counters()
            self._last_date = now.date()
            logger.info(f"New day: {now.date()}, counters reset")
        
        # Check if we can run a session (limits, breaks, etc.)
        if not self.scheduler.config.can_run_session():
            return SessionDecision(should_run=False)
        
        # Get next session from schedule
        result = self.scheduler.get_next_session()
        if not result:
            return SessionDecision(should_run=False)
        
        session: ScheduledSession
        scheduled_time: datetime
        session, scheduled_time = result
        
        # Check if current time is within the scheduled window
        current_time = now.time()
        if not session.window.contains(current_time):
            # Session is scheduled for later
            logger.debug(
                f"Session scheduled for {scheduled_time.strftime('%H:%M')}, "
                f"current time is {current_time.strftime('%H:%M')}"
            )
            return SessionDecision(should_run=False)
        
        # Check probability
        if not session.should_run():
            logger.debug(f"Session skipped due to probability check")
            return SessionDecision(should_run=False)
        
        # Session should run now!
        duration_seconds = session.get_duration_seconds()
        logger.info(
            f"Schedule says: RUN NOW - mode={session.mode}, "
            f"duration={duration_seconds // 60} min"
        )
        
        return SessionDecision(
            should_run=True,
            duration_seconds=duration_seconds,
            mode=session.mode,
            scheduled_time=scheduled_time,
        )
    
    def record_session(self, duration_minutes: int):
        """
        Record that a session was completed.
        
        Args:
            duration_minutes: Duration of the completed session
        """
        self.scheduler.config.record_session(duration_minutes)
        logger.info(
            f"Recorded session: {duration_minutes} min. "
            f"Today: {self.scheduler.config.sessions_today} sessions, "
            f"{self.scheduler.config.minutes_today} min"
        )
    
    def reload_config(self):
        """Reload schedule configuration from file."""
        self.scheduler.load_config()
        logger.info("Schedule config reloaded")
    
    def get_status(self) -> dict:
        """Get current schedule status."""
        return {
            "sessions_today": self.scheduler.config.sessions_today,
            "minutes_today": self.scheduler.config.minutes_today,
            "max_sessions_per_day": self.scheduler.config.max_sessions_per_day,
            "max_minutes_per_day": self.scheduler.config.max_total_minutes_per_day,
            "timezone": str(self.scheduler.config.timezone),
        }
