"""Timeout and retry configurations for navigation stages.

Each stage has specific timeout, retry, and backoff settings
based on its expected behavior and failure modes.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class Stage(str, Enum):
    """Navigation stages with timeout configs."""
    
    GET_SCREEN_XML = "get_screen_xml"
    SCREENSHOT = "screenshot"
    FIND_ELEMENT = "find_element"
    AI_ANALYSIS = "ai_analysis"
    ACTION_EXECUTE = "action_execute"
    VERIFY_ACTION = "verify_action"
    DIALOG_HANDLE = "dialog_handle"
    ESCAPE_WORKFLOW = "escape_workflow"


@dataclass
class StageConfig:
    """Configuration for a navigation stage."""
    
    timeout_ms: int
    """Maximum time to wait for this stage (milliseconds)."""
    
    max_retries: int
    """Maximum number of retry attempts."""
    
    backoff_multiplier: float = 1.5
    """Multiplier for exponential backoff between retries."""
    
    base_delay_ms: int = 300
    """Base delay between retries (milliseconds)."""
    
    max_delay_ms: int = 5000
    """Maximum delay between retries (milliseconds)."""
    
    def get_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number (0-indexed).
        
        Args:
            attempt: Current attempt number (0 for first retry)
            
        Returns:
            Delay in seconds
        """
        delay = self.base_delay_ms * (self.backoff_multiplier ** attempt)
        delay = min(delay, self.max_delay_ms)
        return delay / 1000.0  # Convert to seconds


# Default configurations for each stage
STAGE_CONFIGS: dict[Stage, StageConfig] = {
    # XML operations - fast but can hang on ANR
    # OPTIMIZED: Reduced retries from 3 to 1 (was causing 12 XML dumps!)
    Stage.GET_SCREEN_XML: StageConfig(
        timeout_ms=5000,
        max_retries=1,  # Was 3 - too many retries slow everything down
        base_delay_ms=300,
    ),
    
    # Screenshot - depends on quality, can be slow
    Stage.SCREENSHOT: StageConfig(
        timeout_ms=10000,
        max_retries=2,
        base_delay_ms=500,
    ),
    
    # Element finding - pure parsing, very fast
    Stage.FIND_ELEMENT: StageConfig(
        timeout_ms=1000,
        max_retries=0,  # If not found, immediately fallback
        base_delay_ms=0,
    ),
    
    # AI vision analysis - slow and expensive
    Stage.AI_ANALYSIS: StageConfig(
        timeout_ms=30000,
        max_retries=2,
        backoff_multiplier=2.0,
        base_delay_ms=1000,
    ),
    
    # Action execution - fast, but need to wait for UI
    Stage.ACTION_EXECUTE: StageConfig(
        timeout_ms=3000,
        max_retries=2,
        base_delay_ms=200,
    ),
    
    # Post-action verification - UI may be in transition
    Stage.VERIFY_ACTION: StageConfig(
        timeout_ms=5000,
        max_retries=3,
        backoff_multiplier=2.0,
        base_delay_ms=500,
    ),
    
    # Dialog handling - fast dismiss
    Stage.DIALOG_HANDLE: StageConfig(
        timeout_ms=2000,
        max_retries=2,
        base_delay_ms=200,
    ),
    
    # Escape workflow - may need multiple steps
    Stage.ESCAPE_WORKFLOW: StageConfig(
        timeout_ms=15000,
        max_retries=3,
        backoff_multiplier=1.5,
        base_delay_ms=500,
    ),
}


def get_config(stage: Stage | str) -> StageConfig:
    """Get configuration for a stage.
    
    Args:
        stage: Stage enum or string name
        
    Returns:
        StageConfig for the stage
    """
    if isinstance(stage, str):
        stage = Stage(stage)
    return STAGE_CONFIGS.get(stage, StageConfig(timeout_ms=5000, max_retries=1))


@dataclass
class RetryContext:
    """Context for retry operations."""
    
    stage: Stage
    config: StageConfig = field(init=False)
    current_attempt: int = 0
    last_error: Exception | None = None
    
    def __post_init__(self):
        self.config = get_config(self.stage)
    
    @property
    def can_retry(self) -> bool:
        """Check if more retries are available."""
        return self.current_attempt < self.config.max_retries
    
    @property
    def next_delay(self) -> float:
        """Get delay before next retry (seconds)."""
        return self.config.get_delay(self.current_attempt)
    
    def increment(self, error: Exception | None = None):
        """Increment attempt counter and optionally record error."""
        self.current_attempt += 1
        self.last_error = error


# Anti-detection settings
# IMPORTANT: Use these delays in engagement actions to avoid Instagram rate limits!
@dataclass
class ThrottleConfig:
    """Configuration for action throttling (anti-detection).
    
    CRITICAL: Instagram rate-limits aggressive automation.
    Use get_throttle_delay() before engagement actions.
    """
    
    min_delay_between_actions_ms: int = 500
    """Minimum delay between any two actions."""
    
    max_delay_between_actions_ms: int = 2000
    """Maximum delay (for random jitter)."""
    
    min_delay_between_likes_ms: int = 5000
    """Minimum delay between like actions (conservative)."""
    
    max_delay_between_likes_ms: int = 12000
    """Maximum delay between like actions."""
    
    min_delay_between_comments_ms: int = 20000
    """Minimum delay between comment actions (conservative)."""
    
    max_delay_between_comments_ms: int = 45000
    """Maximum delay between comment actions."""
    
    pause_after_scroll_ms: int = 800
    """Pause after scroll to "read" content."""
    
    pause_variation_percent: float = 0.3
    """Variation in pause times (± this percent)."""


DEFAULT_THROTTLE = ThrottleConfig()


import random


def get_throttle_delay(action_type: str, config: ThrottleConfig | None = None) -> float:
    """Get randomized delay for anti-detection.
    
    IMPORTANT: Call this before engagement actions!
    
    Args:
        action_type: "like", "comment", "scroll", or "action"
        config: ThrottleConfig (uses DEFAULT_THROTTLE if not provided)
        
    Returns:
        Delay in seconds (with random jitter)
    """
    cfg = config or DEFAULT_THROTTLE
    
    if action_type == "like":
        min_ms = cfg.min_delay_between_likes_ms
        max_ms = cfg.max_delay_between_likes_ms
    elif action_type == "comment":
        min_ms = cfg.min_delay_between_comments_ms
        max_ms = cfg.max_delay_between_comments_ms
    elif action_type == "scroll":
        base = cfg.pause_after_scroll_ms
        variation = int(base * cfg.pause_variation_percent)
        return random.randint(base - variation, base + variation) / 1000.0
    else:  # generic action
        min_ms = cfg.min_delay_between_actions_ms
        max_ms = cfg.max_delay_between_actions_ms
    
    return random.randint(min_ms, max_ms) / 1000.0


# XML validation settings
MIN_XML_SIZE = 500
"""Minimum XML size in bytes for valid hierarchy."""

MAX_XML_SIZE = 500_000
"""Maximum expected XML size (sanity check)."""

XML_PARSE_TIMEOUT_MS = 2000
"""Timeout for XML parsing operations."""
