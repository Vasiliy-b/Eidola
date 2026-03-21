"""
Account rotation logic for multi-account management on single device.

Manages switching between multiple Instagram accounts assigned to a device,
tracking session times, cooldowns, and rotation strategies.
"""

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..config import AccountConfig, RotationConfig, load_account_config

logger = logging.getLogger("eidola.scheduler.rotator")


@dataclass
class AccountSession:
    """Tracks session data for an account."""
    account_id: str
    username: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_minutes: int = 0
    actions_performed: int = 0
    errors: list[str] = field(default_factory=list)
    
    @property
    def is_active(self) -> bool:
        """Check if session is currently active."""
        return self.started_at is not None and self.ended_at is None
    
    def start(self):
        """Mark session as started."""
        self.started_at = datetime.now()
        self.ended_at = None
        logger.info(f"Session started for account: {self.username}")
    
    def end(self):
        """Mark session as ended."""
        if self.started_at:
            self.ended_at = datetime.now()
            self.duration_minutes = int(
                (self.ended_at - self.started_at).total_seconds() / 60
            )
            logger.info(
                f"Session ended for account: {self.username}, "
                f"duration: {self.duration_minutes} min, "
                f"actions: {self.actions_performed}"
            )
    
    def record_action(self):
        """Record an action performed during session."""
        self.actions_performed += 1
    
    def record_error(self, error: str):
        """Record an error during session."""
        self.errors.append(error)
        logger.warning(f"Session error for {self.username}: {error}")


@dataclass
class AccountState:
    """Tracks the state of an account for rotation decisions."""
    account_id: str
    username: str
    config: AccountConfig
    
    # Session history
    sessions_today: int = 0
    total_minutes_today: int = 0
    last_session: AccountSession | None = None
    
    # Status
    is_logged_in: bool = False
    is_current: bool = False
    cooldown_until: datetime | None = None
    
    # Error tracking
    consecutive_errors: int = 0
    last_error: str | None = None
    
    @property
    def is_on_cooldown(self) -> bool:
        """Check if account is on cooldown."""
        if self.cooldown_until is None:
            return False
        return datetime.now() < self.cooldown_until
    
    @property
    def cooldown_remaining_minutes(self) -> int:
        """Get remaining cooldown time in minutes."""
        if not self.is_on_cooldown:
            return 0
        remaining = (self.cooldown_until - datetime.now()).total_seconds() / 60
        return max(0, int(remaining))
    
    @property
    def is_available(self) -> bool:
        """Check if account is available for rotation."""
        if self.is_on_cooldown:
            return False
        if self.consecutive_errors >= 3:
            return False
        if self.config.metadata.status != "active":
            return False
        return True
    
    def start_cooldown(self, minutes: int):
        """Put account on cooldown."""
        self.cooldown_until = datetime.now() + timedelta(minutes=minutes)
        logger.info(f"Account {self.username} on cooldown for {minutes} min")
    
    def reset_daily_stats(self):
        """Reset daily statistics."""
        self.sessions_today = 0
        self.total_minutes_today = 0
    
    def record_session_complete(self, session: AccountSession):
        """Record a completed session."""
        self.sessions_today += 1
        self.total_minutes_today += session.duration_minutes
        self.last_session = session
        self.is_current = False
        
        # Clear errors on successful session
        if not session.errors:
            self.consecutive_errors = 0
    
    def record_error(self, error: str):
        """Record an error for this account."""
        self.consecutive_errors += 1
        self.last_error = error
        
        # Auto-cooldown on repeated errors
        if self.consecutive_errors >= 3:
            self.start_cooldown(30)  # 30 min cooldown on repeated errors


class AccountRotator:
    """
    Manages rotation between multiple Instagram accounts on a device.
    
    Strategies:
    - sequential: Rotate through accounts in order
    - random: Random selection from available accounts
    - random_within_schedule: Random but respects schedule and cooldowns
    
    Usage:
        rotator = AccountRotator(device_id, account_configs, rotation_config)
        
        while running:
            account = rotator.get_next_account()
            if not account:
                break  # No accounts available
            
            session = rotator.start_session(account)
            # ... run agent session ...
            rotator.end_session(session)
            
            if rotator.should_switch_account(session.duration_minutes):
                continue  # Switch to next account
    """
    
    def __init__(
        self,
        device_id: str,
        account_ids: list[str],
        rotation_config: RotationConfig | None = None,
    ):
        """
        Initialize account rotator for a device.
        
        Args:
            device_id: Device identifier
            account_ids: List of account IDs assigned to this device
            rotation_config: Rotation strategy configuration
        """
        self.device_id = device_id
        self.rotation_config = rotation_config or RotationConfig()
        
        # Load account configs and create state tracking
        self.accounts: dict[str, AccountState] = {}
        for account_id in account_ids:
            config = load_account_config(account_id)
            if config:
                self.accounts[account_id] = AccountState(
                    account_id=account_id,
                    username=config.instagram.username,
                    config=config,
                )
                logger.info(f"Loaded account: {config.instagram.username}")
            else:
                logger.warning(f"Account config not found: {account_id}")
        
        # Rotation state
        self._current_index = 0
        self._current_account: AccountState | None = None
        self._current_session: AccountSession | None = None
        self._last_date: datetime.date | None = None
    
    @property
    def current_account(self) -> AccountState | None:
        """Get the currently active account."""
        return self._current_account
    
    @property
    def current_session(self) -> AccountSession | None:
        """Get the current session."""
        return self._current_session
    
    @property
    def available_accounts(self) -> list[AccountState]:
        """Get list of accounts available for rotation."""
        return [acc for acc in self.accounts.values() if acc.is_available]
    
    def _reset_daily_stats_if_needed(self):
        """Reset daily stats at midnight."""
        today = datetime.now().date()
        if self._last_date != today:
            for account in self.accounts.values():
                account.reset_daily_stats()
            self._last_date = today
            logger.info("Daily stats reset for all accounts")
    
    def get_next_account(self) -> AccountState | None:
        """
        Get the next account to use based on rotation strategy.
        
        Returns:
            AccountState for the next account, or None if no accounts available
        """
        self._reset_daily_stats_if_needed()
        
        available = self.available_accounts
        if not available:
            logger.warning("No accounts available for rotation")
            return None
        
        strategy = self.rotation_config.strategy
        
        if strategy == "sequential":
            account = self._select_sequential(available)
        elif strategy == "random":
            account = self._select_random(available)
        elif strategy == "random_within_schedule":
            account = self._select_random_weighted(available)
        else:
            logger.warning(f"Unknown rotation strategy: {strategy}, using sequential")
            account = self._select_sequential(available)
        
        logger.info(f"Selected account for rotation: {account.username}")
        return account
    
    def _select_sequential(self, available: list[AccountState]) -> AccountState:
        """Select next account in sequential order."""
        # Get list of all account IDs in order
        all_ids = list(self.accounts.keys())
        
        # Find next available starting from current index
        for _ in range(len(all_ids)):
            account_id = all_ids[self._current_index]
            self._current_index = (self._current_index + 1) % len(all_ids)
            
            account = self.accounts.get(account_id)
            if account and account.is_available:
                return account
        
        # Fallback to first available
        return available[0]
    
    def _select_random(self, available: list[AccountState]) -> AccountState:
        """Select random account from available."""
        return random.choice(available)
    
    def _select_random_weighted(self, available: list[AccountState]) -> AccountState:
        """
        Select random account with weighting.
        
        Accounts with less activity today get higher weight.
        """
        # Calculate weights based on inverse of sessions today
        max_sessions = max(acc.sessions_today for acc in available) + 1
        weights = [max_sessions - acc.sessions_today + 1 for acc in available]
        
        return random.choices(available, weights=weights, k=1)[0]
    
    def start_session(self, account: AccountState | None = None) -> AccountSession:
        """
        Start a new session for an account.
        
        Args:
            account: Account to start session for (or auto-select if None)
            
        Returns:
            AccountSession for tracking the session
        """
        if account is None:
            account = self.get_next_account()
            if account is None:
                raise ValueError("No accounts available")
        
        # End current session if active
        if self._current_session and self._current_session.is_active:
            self.end_session(self._current_session)
        
        # Mark previous account as not current
        if self._current_account:
            self._current_account.is_current = False
        
        # Start new session
        session = AccountSession(
            account_id=account.account_id,
            username=account.username,
        )
        session.start()
        
        self._current_account = account
        self._current_session = session
        account.is_current = True
        
        return session
    
    def end_session(self, session: AccountSession | None = None):
        """
        End the current or specified session.
        
        Args:
            session: Session to end (uses current if None)
        """
        session = session or self._current_session
        if session is None:
            return
        
        if session.is_active:
            session.end()
        
        # Update account state
        account = self.accounts.get(session.account_id)
        if account:
            account.record_session_complete(session)
            
            # Apply cooldown between accounts
            cooldown = self.rotation_config.break_between_accounts_minutes
            if cooldown > 0:
                account.start_cooldown(cooldown)
        
        if session == self._current_session:
            self._current_session = None
    
    def should_switch_account(self, current_session_minutes: int) -> bool:
        """
        Decide if it's time to switch to a different account.
        
        Based on rotation config min/max session duration.
        
        Args:
            current_session_minutes: Minutes elapsed in current session
            
        Returns:
            True if should switch accounts
        """
        min_minutes = self.rotation_config.min_session_minutes
        max_minutes = self.rotation_config.max_session_minutes
        
        # Must run at least min_minutes
        if current_session_minutes < min_minutes:
            return False
        
        # Must switch after max_minutes
        if current_session_minutes >= max_minutes:
            return True
        
        # Random chance to switch between min and max
        # Higher chance as we approach max
        elapsed_ratio = (current_session_minutes - min_minutes) / (max_minutes - min_minutes)
        return random.random() < elapsed_ratio * 0.5  # 50% chance at max
    
    def get_rotation_status(self) -> dict[str, Any]:
        """
        Get current rotation status for all accounts.
        
        Returns:
            dict with account statuses
        """
        return {
            "device_id": self.device_id,
            "strategy": self.rotation_config.strategy,
            "current_account": (
                self._current_account.username if self._current_account else None
            ),
            "accounts": {
                acc_id: {
                    "username": acc.username,
                    "is_current": acc.is_current,
                    "is_available": acc.is_available,
                    "is_on_cooldown": acc.is_on_cooldown,
                    "cooldown_remaining_minutes": acc.cooldown_remaining_minutes,
                    "sessions_today": acc.sessions_today,
                    "total_minutes_today": acc.total_minutes_today,
                    "consecutive_errors": acc.consecutive_errors,
                    "status": acc.config.metadata.status,
                }
                for acc_id, acc in self.accounts.items()
            },
        }
    
    def mark_account_error(self, account_id: str, error: str):
        """
        Mark an error for an account.
        
        Args:
            account_id: Account that encountered error
            error: Error description
        """
        account = self.accounts.get(account_id)
        if account:
            account.record_error(error)
            
            # Also record on current session if active
            if self._current_session and self._current_session.account_id == account_id:
                self._current_session.record_error(error)
    
    def mark_account_logged_in(self, account_id: str, is_logged_in: bool = True):
        """
        Update login status for an account.
        
        Args:
            account_id: Account to update
            is_logged_in: Whether account is logged in
        """
        account = self.accounts.get(account_id)
        if account:
            account.is_logged_in = is_logged_in
            logger.info(f"Account {account.username} logged_in={is_logged_in}")


def create_rotator_for_device(
    device_id: str,
    account_ids: list[str],
    rotation_config: RotationConfig | None = None,
) -> AccountRotator:
    """
    Create an AccountRotator for a device.
    
    Args:
        device_id: Device identifier
        account_ids: List of account IDs assigned to device
        rotation_config: Optional rotation configuration
        
    Returns:
        AccountRotator instance
    """
    return AccountRotator(device_id, account_ids, rotation_config)
