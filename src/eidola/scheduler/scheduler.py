"""
Scheduler - Cron-like daemon for 24/7 session scheduling.

Reads schedule.yaml and triggers sessions at appropriate times
based on timezone, day of week, and probability settings.
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger("eidola.scheduler")


@dataclass
class TimeWindow:
    """A time window during which sessions can be scheduled."""
    start: time
    end: time
    
    @classmethod
    def from_dict(cls, data: dict | str, windows_registry: dict[str, "TimeWindow"] = None) -> "TimeWindow":
        """Create TimeWindow from dict or window name reference."""
        if isinstance(data, str):
            # Reference to named window
            if windows_registry and data in windows_registry:
                return windows_registry[data]
            raise ValueError(f"Unknown time window: {data}")
        
        return cls(
            start=time.fromisoformat(data["start"]),
            end=time.fromisoformat(data["end"]),
        )
    
    def contains(self, t: time) -> bool:
        """Check if time falls within this window."""
        if self.start <= self.end:
            return self.start <= t <= self.end
        else:
            # Window spans midnight (e.g., 22:00-02:00)
            return t >= self.start or t <= self.end
    
    def random_time_in_window(self) -> time:
        """Get a random time within this window."""
        start_minutes = self.start.hour * 60 + self.start.minute
        end_minutes = self.end.hour * 60 + self.end.minute
        
        if end_minutes < start_minutes:
            # Window spans midnight
            end_minutes += 24 * 60
        
        random_minutes = random.randint(start_minutes, end_minutes)
        random_minutes = random_minutes % (24 * 60)
        
        return time(hour=random_minutes // 60, minute=random_minutes % 60)


@dataclass
class ScheduledSession:
    """A scheduled session configuration."""
    window: TimeWindow
    mode: str
    duration_minutes: tuple[int, int]  # (min, max)
    probability: float = 1.0
    targets: str | None = None  # "vip", "high", etc.
    
    @classmethod
    def from_dict(cls, data: dict, windows_registry: dict[str, TimeWindow]) -> "ScheduledSession":
        """Create ScheduledSession from dict."""
        window_data = data.get("window")
        if isinstance(window_data, str):
            window = windows_registry[window_data]
        else:
            window = TimeWindow.from_dict(window_data, windows_registry)
        
        duration = data.get("duration_minutes", [10, 20])
        if isinstance(duration, list):
            duration = tuple(duration)
        else:
            duration = (duration, duration)
        
        return cls(
            window=window,
            mode=data["mode"],
            duration_minutes=duration,
            probability=data.get("probability", 1.0),
            targets=data.get("targets"),
        )
    
    def should_run(self) -> bool:
        """Decide if session should run based on probability."""
        return random.random() < self.probability
    
    def get_duration_seconds(self) -> int:
        """Get random duration within configured range."""
        minutes = random.randint(self.duration_minutes[0], self.duration_minutes[1])
        return minutes * 60


@dataclass
class ScheduleConfig:
    """Parsed schedule configuration."""
    timezone: ZoneInfo
    timezone_source: str
    gps_enabled: bool
    gps_source: str
    gps_variance_meters: int
    
    # Limits
    max_sessions_per_day: int
    max_total_minutes_per_day: int
    min_break_between_sessions_minutes: int
    
    # Rate limits
    rate_limits: dict[str, int]
    
    # Time windows
    windows: dict[str, TimeWindow]
    
    # Schedules
    weekday_sessions: list[ScheduledSession]
    weekend_sessions: list[ScheduledSession]
    
    # Modes
    modes: dict[str, dict[str, Any]]
    
    # Special days
    special_days: dict[str, list[ScheduledSession]]
    
    # State tracking
    sessions_today: int = 0
    minutes_today: int = 0
    last_session_end: datetime | None = None
    
    @classmethod
    def from_yaml(cls, yaml_path: Path) -> "ScheduleConfig":
        """Load and parse schedule from YAML file."""
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        
        global_config = data.get("global", {})
        
        # Timezone
        tz_fallback = global_config.get("timezone_fallback", "UTC")
        timezone = ZoneInfo(tz_fallback)  # Will be updated from proxy if needed
        
        # GPS
        gps_config = global_config.get("gps", {})
        
        # Limits
        limits = global_config.get("limits", {})
        rate_limits = global_config.get("rate_limits", {})
        
        # Parse time windows
        windows = {}
        for name, window_data in data.get("time_windows", {}).items():
            windows[name] = TimeWindow.from_dict(window_data)
        
        # Parse weekday sessions
        weekday_sessions = []
        for session_data in data.get("weekday", {}).get("sessions", []):
            weekday_sessions.append(ScheduledSession.from_dict(session_data, windows))
        
        # Parse weekend sessions
        weekend_sessions = []
        for session_data in data.get("weekend", {}).get("sessions", []):
            weekend_sessions.append(ScheduledSession.from_dict(session_data, windows))
        
        # Parse special days
        special_days = {}
        for special in data.get("special_days", []):
            date_str = special.get("date")
            sessions = []
            for session_data in special.get("sessions", []):
                sessions.append(ScheduledSession.from_dict(session_data, windows))
            special_days[date_str] = sessions
        
        return cls(
            timezone=timezone,
            timezone_source=global_config.get("timezone_source", "config"),
            gps_enabled=gps_config.get("enabled", False),
            gps_source=gps_config.get("source", "manual"),
            gps_variance_meters=gps_config.get("variance_meters", 500),
            max_sessions_per_day=limits.get("max_sessions_per_day", 8),
            max_total_minutes_per_day=limits.get("max_total_minutes_per_day", 180),
            min_break_between_sessions_minutes=limits.get("min_break_between_sessions_minutes", 30),
            rate_limits=rate_limits,
            windows=windows,
            weekday_sessions=weekday_sessions,
            weekend_sessions=weekend_sessions,
            modes=data.get("modes", {}),
            special_days=special_days,
        )
    
    def reset_daily_counters(self):
        """Reset daily session/minute counters."""
        self.sessions_today = 0
        self.minutes_today = 0
    
    def can_run_session(self) -> bool:
        """Check if we can run another session today."""
        if self.sessions_today >= self.max_sessions_per_day:
            logger.info(f"Daily session limit reached: {self.sessions_today}/{self.max_sessions_per_day}")
            return False
        
        if self.minutes_today >= self.max_total_minutes_per_day:
            logger.info(f"Daily time limit reached: {self.minutes_today}/{self.max_total_minutes_per_day} min")
            return False
        
        if self.last_session_end:
            elapsed = datetime.now(self.timezone) - self.last_session_end
            if elapsed.total_seconds() < self.min_break_between_sessions_minutes * 60:
                remaining = self.min_break_between_sessions_minutes - elapsed.total_seconds() / 60
                logger.debug(f"Break time remaining: {remaining:.1f} min")
                return False
        
        return True
    
    def record_session(self, duration_minutes: int):
        """Record a completed session."""
        self.sessions_today += 1
        self.minutes_today += duration_minutes
        self.last_session_end = datetime.now(self.timezone)


class Scheduler:
    """
    Cron-like scheduler for Instagram automation.
    
    Reads schedule.yaml and triggers sessions at appropriate times.
    """
    
    def __init__(
        self,
        schedule_path: Path | str = "config/schedule.yaml",
        session_callback: callable = None,
    ):
        """
        Initialize scheduler.
        
        Args:
            schedule_path: Path to schedule.yaml
            session_callback: Async function to call when session should start.
                             Signature: async def callback(mode: str, duration_seconds: int, config: dict)
        """
        self.schedule_path = Path(schedule_path)
        self.session_callback = session_callback
        self.config: ScheduleConfig | None = None
        self._running = False
        self._last_date: datetime.date | None = None
    
    def load_config(self) -> ScheduleConfig:
        """Load or reload schedule configuration."""
        self.config = ScheduleConfig.from_yaml(self.schedule_path)
        logger.info(f"Loaded schedule config from {self.schedule_path}")
        return self.config
    
    def get_sessions_for_today(self) -> list[ScheduledSession]:
        """Get the list of scheduled sessions for today."""
        if not self.config:
            self.load_config()
        
        now = datetime.now(self.config.timezone)
        
        # Check for special day override
        date_key = now.strftime("%m-%d")
        if date_key in self.config.special_days:
            logger.info(f"Special day schedule active: {date_key}")
            return self.config.special_days[date_key]
        
        # Weekday vs weekend
        if now.weekday() < 5:  # Mon-Fri = 0-4
            return self.config.weekday_sessions
        else:
            return self.config.weekend_sessions
    
    def get_next_session(self) -> tuple[ScheduledSession, datetime] | None:
        """
        Find the next session to run based on current time.
        
        Returns:
            (session, scheduled_time) or None if no more sessions today
        """
        if not self.config:
            self.load_config()
        
        now = datetime.now(self.config.timezone)
        current_time = now.time()
        
        sessions = self.get_sessions_for_today()
        
        for session in sessions:
            # Check if current time is within window
            if session.window.contains(current_time):
                # Should we run this session?
                if session.should_run() and self.config.can_run_session():
                    return session, now
            
            # Check if window is upcoming today
            # Handle both normal windows and windows spanning midnight
            window_start = session.window.start
            window_end = session.window.end
            
            # Window is upcoming if:
            # 1. Normal window (start < end): start is after current time
            # 2. Midnight window (start > end): current time is before end OR after start
            is_upcoming = False
            if window_start <= window_end:
                # Normal window (e.g., 08:00-10:00)
                is_upcoming = window_start > current_time
            else:
                # Window spans midnight (e.g., 22:00-02:00)
                # It's upcoming if we're before the start on the same day
                # or if it's currently in the "after midnight" portion
                is_upcoming = current_time < window_end or current_time < window_start
            
            if is_upcoming and not session.window.contains(current_time):
                # Schedule for random time within window
                scheduled_time = datetime.combine(
                    now.date(),
                    session.window.random_time_in_window(),
                    tzinfo=self.config.timezone,
                )
                # If the scheduled time is before now (midnight window), add a day
                if scheduled_time < now:
                    scheduled_time = scheduled_time + timedelta(days=1)
                return session, scheduled_time
        
        return None
    
    async def run_once(self) -> bool:
        """
        Check schedule and run a session if appropriate.
        
        Returns:
            True if a session was started, False otherwise
        """
        if not self.config:
            self.load_config()
        
        # Reset counters at midnight
        now = datetime.now(self.config.timezone)
        if self._last_date != now.date():
            self.config.reset_daily_counters()
            self._last_date = now.date()
            logger.info(f"New day: {now.date()}, counters reset")
        
        # Check if we can run a session
        if not self.config.can_run_session():
            return False
        
        # Get next session
        result = self.get_next_session()
        if not result:
            logger.debug("No sessions available for current time")
            return False
        
        session, scheduled_time = result
        
        # If session is in the future, wait
        wait_seconds = (scheduled_time - now).total_seconds()
        if wait_seconds > 60:  # More than 1 minute away
            logger.info(f"Next session at {scheduled_time.strftime('%H:%M')}, waiting...")
            return False
        
        # Run the session
        duration_seconds = session.get_duration_seconds()
        mode_config = self.config.modes.get(session.mode, {})
        
        logger.info(
            f"Starting session: mode={session.mode}, "
            f"duration={duration_seconds // 60} min"
        )
        
        if self.session_callback:
            try:
                await self.session_callback(
                    mode=session.mode,
                    duration_seconds=duration_seconds,
                    config=mode_config,
                    rate_limits=self.config.rate_limits,
                )
                
                # Record successful session
                self.config.record_session(duration_seconds // 60)
                logger.info(
                    f"Session complete. Today: {self.config.sessions_today} sessions, "
                    f"{self.config.minutes_today} min"
                )
                return True
                
            except Exception as e:
                logger.error(f"Session failed: {e}", exc_info=True)
                return False
        else:
            logger.warning("No session callback configured")
            return False
    
    async def run_daemon(self, check_interval_seconds: int = 60):
        """
        Run as a daemon, checking schedule periodically.
        
        Args:
            check_interval_seconds: How often to check schedule (default: 1 min)
        """
        self._running = True
        logger.info("Scheduler daemon started")
        
        try:
            while self._running:
                try:
                    await self.run_once()
                except Exception as e:
                    logger.error(f"Scheduler error: {e}", exc_info=True)
                
                await asyncio.sleep(check_interval_seconds)
                
        except asyncio.CancelledError:
            logger.info("Scheduler daemon cancelled")
        finally:
            self._running = False
            logger.info("Scheduler daemon stopped")
    
    def stop(self):
        """Stop the daemon."""
        self._running = False
