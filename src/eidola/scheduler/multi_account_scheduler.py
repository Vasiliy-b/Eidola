"""
Multi-Account Scheduler - Extends base scheduler for multi-account support.

Manages running sessions across multiple Instagram accounts on a single device,
handling account rotation, switching, and per-account scheduling.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..config import (
    AccountConfig,
    DeviceConfig,
    FleetConfig,
    RotationConfig,
    load_account_config,
    load_device_config,
    load_fleet_config,
)
from .account_rotator import AccountRotator, AccountSession, AccountState
from .scheduler import Scheduler, ScheduleConfig
from .schedule_helper import ScheduleHelper
from .session_runner import SessionRunner

logger = logging.getLogger("eidola.scheduler.multi_account")


@dataclass
class DeviceSchedulerState:
    """Tracks scheduler state for a single device."""
    device_id: str
    device_ip: str
    rotator: AccountRotator
    current_session: AccountSession | None = None
    session_start_time: float | None = None
    sessions_today: int = 0
    last_date: Optional[datetime.date] = None
    
    @property
    def is_in_session(self) -> bool:
        """Check if device is currently running a session."""
        return self.current_session is not None and self.current_session.is_active
    
    @property
    def current_account(self) -> AccountState | None:
        """Get the current account being used."""
        return self.rotator.current_account
    
    @property
    def session_elapsed_minutes(self) -> int:
        """Minutes elapsed in current session."""
        if not self.session_start_time:
            return 0
        return int((time.monotonic() - self.session_start_time) / 60)


class MultiAccountScheduler:
    """
    Scheduler that manages multiple Instagram accounts across devices.
    
    Features:
    - Automatic account rotation based on RotationConfig
    - Per-account scheduling and limits
    - Account switching via Instagram UI
    - Integration with device isolation (proxy, fingerprint, GPS)
    
    Usage:
        scheduler = MultiAccountScheduler()
        scheduler.load_fleet_config()
        await scheduler.run_device("phone_01")  # Run single device
        # OR
        await scheduler.run_all_devices()  # Run all devices in parallel
    """
    
    def __init__(
        self,
        fleet_config: FleetConfig | None = None,
        schedule_path: Path | str = "config/schedule.yaml",
    ):
        """
        Initialize multi-account scheduler.
        
        Args:
            fleet_config: Optional fleet configuration (loaded if not provided)
            schedule_path: Path to schedule.yaml for session timing
        """
        self.fleet_config = fleet_config or load_fleet_config()
        self.schedule_path = Path(schedule_path)
        
        # Per-device state
        self._device_states: dict[str, DeviceSchedulerState] = {}
        
        # Session callback (to be set)
        self._session_callback: callable = None
        
        # Control
        self._running = False
        
        # Initialize device states from fleet config
        self._init_device_states()
    
    def _init_device_states(self):
        """Initialize state tracking for each device in fleet."""
        for device_config in self.fleet_config.devices:
            device_id = device_config.device_id
            
            # Create account rotator for this device
            rotator = AccountRotator(
                device_id=device_id,
                account_ids=device_config.accounts,
                rotation_config=self.fleet_config.rotation,
            )
            
            self._device_states[device_id] = DeviceSchedulerState(
                device_id=device_id,
                device_ip=device_config.device_ip,
                rotator=rotator,
            )
            
            logger.info(
                f"Initialized device {device_id} with {len(device_config.accounts)} accounts"
            )
    
    def _get_schedule_helper(self, device_id: str) -> ScheduleHelper:
        """Get or create schedule helper for a device."""
        state = self._device_states.get(device_id)
        if not state:
            return None
        
        # Create schedule helper if not exists (one per device)
        if not hasattr(state, '_schedule_helper'):
            state._schedule_helper = ScheduleHelper(schedule_path=self.schedule_path)
        
        return state._schedule_helper
    
    def set_session_callback(self, callback: callable):
        """
        Set the callback function for running sessions.
        
        Callback signature:
            async def callback(
                device_ip: str,
                account_id: str,
                username: str,
                mode: str,
                duration_seconds: int,
                config: dict,
            ) -> AsyncIterator[str]
        
        Args:
            callback: Async function to call for each session
        """
        self._session_callback = callback
    
    def get_device_state(self, device_id: str) -> DeviceSchedulerState | None:
        """Get the current state for a device."""
        return self._device_states.get(device_id)
    
    def get_all_device_states(self) -> dict[str, DeviceSchedulerState]:
        """Get states for all devices."""
        return self._device_states.copy()
    
    async def switch_account_on_device(
        self,
        device_id: str,
        target_username: str,
    ) -> dict[str, Any]:
        """
        Switch to a different account on a device.
        
        Uses the switch_instagram_account tool via agent.
        
        Args:
            device_id: Device identifier
            target_username: Instagram username to switch to
            
        Returns:
            dict with success status and details
        """
        from ..tools.auth_tools import switch_instagram_account
        
        state = self._device_states.get(device_id)
        if not state:
            return {"success": False, "error": f"Unknown device: {device_id}"}
        
        # Call the account switcher
        result = switch_instagram_account(target_username)
        
        if result.get("success"):
            # Update rotator state
            for acc_id, acc_state in state.rotator.accounts.items():
                if acc_state.username.lower() == target_username.lower():
                    acc_state.is_current = True
                    state.rotator._current_account = acc_state
                else:
                    acc_state.is_current = False
            
            logger.info(f"Switched device {device_id} to account {target_username}")
        
        return result
    
    async def run_session_for_account(
        self,
        device_id: str,
        account: AccountState,
        mode: str,
        duration_seconds: int,
        config: dict[str, Any] = None,
    ) -> AsyncIterator[str]:
        """
        Run a session for a specific account on a device.
        
        Args:
            device_id: Device identifier
            account: Account to run session for
            mode: Session mode (feed_scroll, warmup, etc.)
            duration_seconds: Session duration
            config: Mode configuration
            
        Yields:
            Progress messages
        """
        state = self._device_states.get(device_id)
        if not state:
            yield f"Error: Unknown device {device_id}"
            return
        
        # Start session in rotator
        session = state.rotator.start_session(account)
        state.current_session = session
        state.session_start_time = time.monotonic()
        
        yield f"Starting session for @{account.username} on {device_id}"
        
        try:
            # Check if account is logged in, switch if needed
            current = state.rotator.current_account
            if current and current.username.lower() != account.username.lower():
                yield f"Switching to account @{account.username}..."
                switch_result = await self.switch_account_on_device(
                    device_id, account.username
                )
                
                if not switch_result.get("success"):
                    if switch_result.get("need_login"):
                        yield f"Account @{account.username} needs login"
                        # TODO: Trigger login flow
                        return
                    else:
                        yield f"Failed to switch: {switch_result.get('error')}"
                        return
            
            # Run the actual session via callback
            if self._session_callback:
                async for msg in self._session_callback(
                    device_ip=state.device_ip,
                    account_id=account.account_id,
                    username=account.username,
                    mode=mode,
                    duration_seconds=duration_seconds,
                    config=config or {},
                ):
                    yield msg
                    
                    # Check if we should switch accounts mid-session
                    if state.rotator.should_switch_account(state.session_elapsed_minutes):
                        yield "Rotation time reached, will switch accounts"
                        break
            else:
                # No callback - just simulate session
                logger.warning("No session callback configured")
                await asyncio.sleep(min(5, duration_seconds))
                yield "Session simulated (no callback)"
            
        except Exception as e:
            logger.error(f"Session error: {e}", exc_info=True)
            state.rotator.mark_account_error(account.account_id, str(e))
            yield f"Session error: {e}"
        
        finally:
            # End session
            state.rotator.end_session(session)
            state.current_session = None
            state.session_start_time = None
            state.sessions_today += 1
            
            yield f"Session ended for @{account.username}"
    
    async def run_device_loop(
        self,
        device_id: str,
        default_mode: str = "warmup",
        check_interval_seconds: int = 60,
        use_schedule: bool = True,
    ):
        """
        Run continuous session loop for a device.
        
        Uses time-based scheduling from schedule.yaml if use_schedule=True,
        otherwise falls back to rotation-based scheduling.
        
        Args:
            device_id: Device identifier
            default_mode: Default session mode if not scheduled
            check_interval_seconds: How often to check for next session
            use_schedule: If True, use schedule.yaml for timing; if False, use rotation config
        """
        state = self._device_states.get(device_id)
        if not state:
            logger.error(f"Unknown device: {device_id}")
            return
        
        logger.info(f"Starting device loop for {device_id} (schedule={'enabled' if use_schedule else 'disabled'})")
        self._running = True
        
        # Initialize schedule helper if using schedule
        schedule_helper = None
        if use_schedule:
            schedule_helper = self._get_schedule_helper(device_id)
            if schedule_helper:
                logger.info(f"Time-based scheduling enabled for {device_id}")
            else:
                logger.warning(f"Could not initialize schedule helper for {device_id}, falling back to rotation")
                use_schedule = False
        
        try:
            while self._running:
                # Reset daily stats at midnight
                today = datetime.now().date()
                if state.last_date != today:
                    state.sessions_today = 0
                    state.rotator._reset_daily_stats_if_needed()
                    state.last_date = today
                
                # Get next account to use
                account = state.rotator.get_next_account()
                
                if not account:
                    logger.info(f"No accounts available for {device_id}, waiting...")
                    await asyncio.sleep(check_interval_seconds)
                    continue
                
                # Check schedule if enabled
                if use_schedule and schedule_helper:
                    decision = schedule_helper.should_run_now()
                    
                    if not decision.should_run:
                        # Schedule says not to run now - wait and check again
                        logger.debug(
                            f"[{device_id}] Schedule says wait. "
                            f"Today: {schedule_helper.get_status()['sessions_today']} sessions, "
                            f"{schedule_helper.get_status()['minutes_today']} min"
                        )
                        await asyncio.sleep(check_interval_seconds)
                        continue
                    
                    # Schedule says run now - use schedule's duration and mode
                    duration_seconds = decision.duration_seconds
                    mode = decision.mode or default_mode
                    
                    logger.info(
                        f"[{device_id}] Schedule triggered: mode={mode}, "
                        f"duration={duration_seconds // 60} min"
                    )
                    
                else:
                    # Fallback to rotation-based scheduling
                    rotation = self.fleet_config.rotation
                    duration_minutes = random.randint(
                        rotation.min_session_minutes,
                        rotation.max_session_minutes,
                    )
                    duration_seconds = duration_minutes * 60
                    mode = default_mode
                
                # Run session for this account
                async for msg in self.run_session_for_account(
                    device_id=device_id,
                    account=account,
                    mode=mode,
                    duration_seconds=duration_seconds,
                ):
                    logger.info(f"[{device_id}] {msg}")
                
                # Record session in schedule if using schedule
                if use_schedule and schedule_helper:
                    duration_minutes = duration_seconds // 60
                    schedule_helper.record_session(duration_minutes)
                
                # Wait between accounts (or until next scheduled time)
                if use_schedule and schedule_helper:
                    # With schedule, wait for next check interval
                    # The schedule will determine when next session runs
                    await asyncio.sleep(check_interval_seconds)
                else:
                    # Without schedule, use rotation break time
                    rotation = self.fleet_config.rotation
                    break_minutes = rotation.break_between_accounts_minutes
                    if break_minutes > 0:
                        logger.info(
                            f"Break between accounts: {break_minutes} min"
                        )
                        await asyncio.sleep(break_minutes * 60)
        
        except asyncio.CancelledError:
            logger.info(f"Device loop cancelled: {device_id}")
        except Exception as e:
            logger.error(f"Device loop error: {e}", exc_info=True)
        finally:
            logger.info(f"Device loop stopped: {device_id}")
    
    async def run_all_devices(self, default_mode: str = "warmup"):
        """
        Run all devices in parallel.
        
        Creates a task for each device's session loop.
        
        Args:
            default_mode: Default session mode
        """
        if not self._device_states:
            logger.error("No devices configured")
            return
        
        logger.info(f"Starting {len(self._device_states)} device loops")
        
        tasks = [
            asyncio.create_task(
                self.run_device_loop(device_id, default_mode),
                name=f"device_{device_id}",
            )
            for device_id in self._device_states.keys()
        ]
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("All device loops cancelled")
            for task in tasks:
                task.cancel()
    
    def stop(self):
        """Stop all device loops."""
        self._running = False
        logger.info("Stopping all device loops")
    
    def get_fleet_status(self) -> dict[str, Any]:
        """
        Get current status of all devices in fleet.
        
        Returns:
            dict with device statuses
        """
        status = {
            "fleet_name": self.fleet_config.name,
            "rotation_strategy": self.fleet_config.rotation.strategy,
            "running": self._running,
            "devices": {},
        }
        
        for device_id, state in self._device_states.items():
            status["devices"][device_id] = {
                "device_ip": state.device_ip,
                "is_in_session": state.is_in_session,
                "sessions_today": state.sessions_today,
                "current_account": (
                    state.current_account.username if state.current_account else None
                ),
                "session_elapsed_minutes": state.session_elapsed_minutes,
                "rotation_status": state.rotator.get_rotation_status(),
            }
        
        return status


async def create_multi_account_scheduler(
    fleet_config: FleetConfig | None = None,
) -> MultiAccountScheduler:
    """
    Create and initialize a MultiAccountScheduler.
    
    Args:
        fleet_config: Optional fleet configuration
        
    Returns:
        Initialized MultiAccountScheduler
    """
    scheduler = MultiAccountScheduler(fleet_config=fleet_config)
    return scheduler
