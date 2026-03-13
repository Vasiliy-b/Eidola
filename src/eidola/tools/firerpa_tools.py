"""FIRERPA SDK toolset for ADK agents.

Uses direct SDK connection (single connection, no MCP overhead).
Integrates SimpleGestures for human-like scrolling (tested and working).

XML-FIRST NAVIGATION:
- Use get_screen_xml() for navigation decisions
- Use screenshot() only when visual analysis is needed
- screenshot() returns types.Part for direct Gemini multimodal input
"""

from __future__ import annotations

import logging
import os
import random
import time
import io
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from contextvars import ContextVar

logger = logging.getLogger("eidola.tools")

# =============================================================================
# WARMUP MODE FLAG (thread-safe, async-safe via ContextVar)
# =============================================================================
# When True, analyze_feed_posts() skips non-nurtured posts (no RANDOM_25%).
# Set from main.py via set_warmup_mode() before starting the session.
_warmup_mode: ContextVar[bool] = ContextVar("warmup_mode", default=False)

# Feed dedup: track (username, timestamp) pairs already analyzed this session.
# Prevents re-analyzing the same post when scroll doesn't move far enough.
_analyzed_feed_posts: ContextVar[set] = ContextVar("analyzed_feed_posts", default=None)


def _get_analyzed_set() -> set:
    """Get or create the per-session set of analyzed feed posts."""
    s = _analyzed_feed_posts.get(None)
    if s is None:
        s = set()
        _analyzed_feed_posts.set(s)
    return s


# =============================================================================
# NAVIGATION CONTEXT (thread-safe, async-safe via ContextVar)
# =============================================================================
# Tracks current screen and navigation depth so every tool response includes
# a _nav_hint — the agent always knows where it is even after history compaction.
_nav_screen: ContextVar[str] = ContextVar("nav_screen", default="unknown")
_nav_depth: ContextVar[int] = ContextVar("nav_depth", default=0)


def _get_nav_hint() -> dict:
    """Build a navigation hint dict for tool responses."""
    return {"screen": _nav_screen.get(), "depth": _nav_depth.get()}


def _set_nav(screen: str, depth: int | None = None) -> None:
    """Update navigation context."""
    _nav_screen.set(screen)
    if depth is not None:
        _nav_depth.set(depth)


def _nav_deeper() -> None:
    """Increase navigation depth (entering a sub-screen)."""
    _nav_depth.set(_nav_depth.get() + 1)


def _nav_shallower() -> None:
    """Decrease navigation depth (going back)."""
    _nav_depth.set(max(0, _nav_depth.get() - 1))


def set_warmup_mode(enabled: bool) -> None:
    """Set warmup mode flag. In warmup, only nurtured posts get engagement.

    Call from main.py when mode == "warmup" BEFORE running the session.
    Uses ContextVar for thread-safety (multiple agents in parallel).
    """
    _warmup_mode.set(enabled)
    # Reset feed dedup set on mode change (new session context)
    _analyzed_feed_posts.set(set())
    logger.info(f"Warmup mode {'ENABLED' if enabled else 'DISABLED'} (non-nurtured posts will be {'SKIPPED' if enabled else 'RANDOM_25%'})")


# =============================================================================
# DEBUG CONFIGURATION
# =============================================================================
# These can be set from main.py via set_debug_config()

_debug_config = {
    "save_xml": False,         # Save XML dumps to files
    "xml_dir": "./debug_xml",  # Directory for XML dumps
    "verbose": False,          # Extra verbose logging
    "raw_xml": False,          # Disable compression (return raw XML)
}

def set_debug_config(
    save_xml: bool = False,
    verbose: bool = False,
    xml_dir: str = "./debug_xml",
    raw_xml: bool = False,
):
    """Configure debug settings for FIRERPA tools.
    
    Args:
        save_xml: If True, save XML dumps to files for analysis
        verbose: If True, enable extra verbose logging
        xml_dir: Directory to save XML dumps (created if not exists)
        raw_xml: If True, disable XML compression (return full XML)
    """
    _debug_config["save_xml"] = save_xml
    _debug_config["verbose"] = verbose
    _debug_config["xml_dir"] = xml_dir
    _debug_config["raw_xml"] = raw_xml
    
    if save_xml:
        Path(xml_dir).mkdir(parents=True, exist_ok=True)
        logger.info(f"XML dumps will be saved to {xml_dir}")
    
    if raw_xml:
        logger.info("Raw XML mode enabled - compression disabled")


# =============================================================================
# SCROLL LOOP DETECTION
# =============================================================================
# Now managed per-device in DeviceManager._scroll_tracker
# These functions use DeviceManager.get_current() for backward compatibility


def reset_scroll_tracker():
    """Reset scroll counter (call after any engagement).
    
    Uses DeviceManager.get_current() to access per-device scroll tracker.
    """
    dm = DeviceManager.get_current()
    if dm:
        dm._scroll_tracker["consecutive_scrolls"] = 0


def _increment_scroll_tracker() -> dict:
    """Increment scroll counter and return warning if needed.
    
    Uses DeviceManager.get_current() to access per-device scroll tracker.
    """
    dm = DeviceManager.get_current()
    if not dm:
        return {"scroll_streak": 0, "warning": "No device manager"}
    
    tracker = dm._scroll_tracker
    tracker["consecutive_scrolls"] += 1
    count = tracker["consecutive_scrolls"]
    
    if count >= tracker["max_before_escape"]:
        return {
            "scroll_streak": count,
            "warning": f"⚠️ {count} scrolls without engagement - STOP and reassess",
            "should_escape": True,
        }
    elif count >= tracker["max_before_warning"]:
        return {
            "scroll_streak": count,
            "warning": f"⚠️ {count} scrolls without engagement - consider engaging or stopping",
            "should_escape": False,
        }
    return {"scroll_streak": count}


def _save_xml_dump(xml_str: str, screen_context: str, suffix: str = "") -> str | None:
    """Save XML dump to file for debugging.
    
    Returns the filepath if saved, None otherwise.
    """
    if not _debug_config["save_xml"]:
        return None
    
    try:
        timestamp = datetime.now().strftime("%H%M%S_%f")[:-3]  # HH:MM:SS_mmm
        filename = f"{timestamp}_{screen_context}{suffix}.xml"
        filepath = Path(_debug_config["xml_dir"]) / filename
        filepath.write_text(xml_str, encoding="utf-8")
        logger.debug(f"Saved XML dump: {filepath}")
        return str(filepath)
    except Exception as e:
        logger.warning(f"Failed to save XML dump: {e}")
        return None


# =============================================================================
# XML Compression for LLM
# =============================================================================

def compress_xml_for_llm(xml_str: str, max_elements: int = 80) -> list[dict]:
    """Compress XML dump to minimal JSON format optimized for LLM consumption.
    
    Reduces ~350 line XML to ~60-80 element list, saving ~80% tokens.
    
    Keeps elements that have:
    - resource-id (Instagram elements)
    - content-desc (accessibility info)
    - text content
    - clickable=true
    - scrollable=true
    - selected=true
    
    Also keeps SystemUI Back/Home buttons for recovery.
    
    Args:
        xml_str: Raw XML dump from device
        max_elements: Maximum elements to return
        
    Returns:
        List of compressed element dicts with keys:
        - id: Short resource-id (without package prefix)
        - d: content-desc
        - t: text
        - b: bounds as "[x1,y1][x2,y2]"
        - c: class name (short)
        - tap: true if clickable
        - scr: true if scrollable
        - sel: true if selected
        - foc: true if focusable
        - vis: true if visible-to-user
        - p: parent id (for hierarchy context)
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return []
    
    elements = []
    
    # Track parent resource-ids for hierarchy context
    def get_parent_id(node: ET.Element, parent_map: dict) -> str | None:
        """Get the nearest parent's resource-id."""
        parent = parent_map.get(node)
        while parent is not None:
            parent_res_id = parent.get("resource-id", "")
            if parent_res_id:
                # Return short id
                return parent_res_id.split("/")[-1] if "/" in parent_res_id else parent_res_id
            parent = parent_map.get(parent)
        return None
    
    # Build parent map for hierarchy tracking
    parent_map = {child: parent for parent in root.iter() for child in parent}
    
    for node in root.iter("node"):
        # Get attributes
        res_id = node.get("resource-id", "")
        content_desc = node.get("content-desc", "")
        text = node.get("text", "")
        bounds = node.get("bounds", "")
        class_name = node.get("class", "")
        package = node.get("package", "")
        
        clickable = node.get("clickable") == "true"
        scrollable = node.get("scrollable") == "true"
        selected = node.get("selected") == "true"
        focusable = node.get("focusable") == "true"
        enabled = node.get("enabled", "true") == "true"
        visible = node.get("visible-to-user", "true") == "true"
        
        # Skip disabled or invisible elements
        if not enabled or not visible:
            continue
        
        # Decide if we should keep this element
        # Keep if: has resource-id OR content-desc OR text OR clickable OR scrollable OR selected
        is_instagram = "instagram" in package.lower()
        is_systemui = "systemui" in package.lower()
        
        # For SystemUI, only keep navigation buttons (Back, Home, Recent)
        if is_systemui:
            if res_id and any(nav in res_id.lower() for nav in ["back", "home", "recent"]):
                pass  # Keep navigation buttons
            else:
                continue  # Skip other SystemUI elements
        
        # For Instagram elements, apply filtering rules
        has_useful_info = bool(res_id or content_desc or text)
        is_interactive = clickable or scrollable or selected
        
        if not (has_useful_info or is_interactive):
            continue
        
        # Build compressed element
        elem = {}
        
        # Resource ID (short form)
        if res_id:
            short_id = res_id.split("/")[-1] if "/" in res_id else res_id
            elem["id"] = short_id
        
        # Content description
        if content_desc:
            elem["d"] = content_desc
        
        # Text
        if text:
            elem["t"] = text
        
        # Bounds (always include for tap targets)
        if bounds:
            elem["b"] = bounds
        
        # Class name (short form)
        if class_name:
            short_class = class_name.split(".")[-1] if "." in class_name else class_name
            elem["c"] = short_class
        
        # Boolean flags (only include if true)
        if clickable:
            elem["tap"] = True
        if scrollable:
            elem["scr"] = True
        if selected:
            elem["sel"] = True
        if focusable:  # Keep focusable for text input detection (EditText is both clickable AND focusable)
            elem["foc"] = True
        
        # Parent ID for hierarchy context (useful for dialogs)
        parent_id = get_parent_id(node, parent_map)
        if parent_id:
            elem["p"] = parent_id
        
        elements.append(elem)
        
        if len(elements) >= max_elements:
            break
    
    # Log compression stats in debug mode
    if _debug_config["verbose"]:
        raw_size = len(xml_str)
        import json
        compressed_size = len(json.dumps(elements))
        reduction = (1 - compressed_size / raw_size) * 100
        logger.debug(f"XML compression: {raw_size} -> {compressed_size} chars ({reduction:.0f}% reduction), {len(elements)} elements")
    
    return elements

from google.adk.tools import FunctionTool
from google.genai import types

# Use SimpleGestures - the tested and working gesture system
# Wrapped in try/except for graceful handling when lamda is not installed
try:
    from .simple_gestures import SimpleGestures, create_simple_gestures
    GESTURES_AVAILABLE = True
except ImportError:
    SimpleGestures = None  # type: ignore
    create_simple_gestures = None  # type: ignore
    GESTURES_AVAILABLE = False

# XML-first navigation components
from .screen_detector import (
    ScreenDetectionResult,
    detect_screen as _detect_screen,
    is_in_instagram,
    needs_recovery,
)
from .element_finder import SmartElementFinder, FoundElement, create_finder
from .selectors import get_selector, get_all_selectors, INSTAGRAM_SELECTORS
from .action_models import ScreenContext, ActionType, ElementBasedAction
from .timeouts import MIN_XML_SIZE, Stage, get_config
from .dialog_handler import DialogHandler, create_dialog_handler, DialogType
from .escape_workflows import EscapeWorkflows, create_escape_workflows, EscapeResult
from .state_verifier import StateVerifier, create_verifier, VerificationConfig
from .interaction_session import InteractionSession, get_session, clear_session

if TYPE_CHECKING:
    from lamda.client import Device


# =============================================================================
# gRPC keepalive patch (channel-level, version-agnostic)
# =============================================================================
# lamda creates gRPC channels with no options at all, inheriting the C-core
# default keepalive (30 s ping interval).  When multiple channels exist to the
# same FIRERPA server (e.g. scheduler + subprocess), the server replies with
# GOAWAY / ENHANCE_YOUR_CALM / too_many_pings.
#
# Instead of patching lamda's Device.__init__ (which varies across versions),
# we patch grpc.insecure_channel / grpc.secure_channel to merge in keepalive
# options.  This is version-agnostic and doesn't touch lamda internals.

_GRPC_CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms", 120_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 0),
    ("grpc.http2.min_time_between_pings_ms", 120_000),
    ("grpc.http2.min_ping_interval_without_data_ms", 300_000),
]

_grpc_patched = False


def _patch_grpc_channel_keepalive() -> None:
    """Patch grpc channel constructors to always include keepalive options."""
    global _grpc_patched
    if _grpc_patched:
        return
    try:
        import grpc as _grpc

        _orig_insecure = _grpc.insecure_channel
        _orig_secure = _grpc.secure_channel

        def _insecure_with_keepalive(target, options=None, **kwargs):
            merged = list(options or []) + _GRPC_CHANNEL_OPTIONS
            return _orig_insecure(target, options=merged, **kwargs)

        def _secure_with_keepalive(target, credentials, options=None, **kwargs):
            merged = list(options or []) + _GRPC_CHANNEL_OPTIONS
            return _orig_secure(target, credentials, options=merged, **kwargs)

        _grpc.insecure_channel = _insecure_with_keepalive
        _grpc.secure_channel = _secure_with_keepalive
        _grpc_patched = True
        logger.info("Patched grpc.insecure_channel/secure_channel with keepalive options")
    except Exception as exc:
        logger.warning(f"Could not patch gRPC keepalive options: {exc}")


_patch_grpc_channel_keepalive()


# =============================================================================
# Device Connection Manager
# =============================================================================

class DeviceManager:
    """Manages FIRERPA device connections with health checks and XML caching."""
    
    _instances: dict[str, "DeviceManager"] = {}
    
    # XML cache settings
    XML_CACHE_TTL_MS = 5000  # Cache XML for 5 seconds to reduce device round-trips
    
    # Screenshot cache settings
    SCREENSHOT_CACHE_TTL_MS = 10000  # Cache screenshot for 10 seconds
    
    # Loop prevention settings
    MAX_TOOL_FAILURES = 3  # Max consecutive failures per tool+args
    FAILURE_RESET_SECONDS = 300  # Reset failure count after 5 minutes
    
    # gRPC connection health: reconnect if connection older than this
    # Prevents stale gRPC channels and "too_many_pings" GOAWAY errors
    MAX_CONNECTION_AGE_SECONDS = 600  # 10 minutes
    
    def __init__(self, device_ip: str):
        self.device_ip = device_ip
        self._device: Device | None = None
        self._screen_width: int = 1080
        self._screen_height: int = 2400
        self._gestures: SimpleGestures | None = None
        # XML cache
        self._xml_cache: str | None = None
        self._xml_cache_time: float = 0
        # Screenshot cache
        self._screenshot_cache: bytes | None = None
        self._screenshot_cache_time: float = 0
        self._screenshot_post_id: str | None = None
        # Elements cache (lazy-computed from XML)
        self._elements_cache: list[dict] | None = None
        # Loop prevention: track consecutive failures per tool+args
        self._tool_failures: dict[str, int] = {}  # "tool:args" -> failure count
        self._tool_last_attempt: dict[str, float] = {}  # "tool:args" -> timestamp
        self._connection_time: float = 0
        # Scroll loop detection (per-device, not global)
        self._scroll_tracker = {
            "consecutive_scrolls": 0,
            "max_before_warning": 10,
            "max_before_escape": 20,
        }
        # Navigation-lost detection: consecutive analyze_feed_posts calls with 0 posts
        self._zero_post_streak: int = 0
        self.ZERO_POST_RESTART_THRESHOLD: int = 3
    
    def track_tool_failure(self, tool_name: str, args_key: str) -> int:
        """Track a tool failure and return current failure count.
        
        Args:
            tool_name: Name of the tool
            args_key: Unique key for the arguments (e.g., username)
            
        Returns:
            Current consecutive failure count
        """
        key = f"{tool_name}:{args_key}"
        now = time.time()
        
        # Reset if enough time passed
        last_attempt = self._tool_last_attempt.get(key, 0)
        if now - last_attempt > self.FAILURE_RESET_SECONDS:
            self._tool_failures[key] = 0
        
        self._tool_failures[key] = self._tool_failures.get(key, 0) + 1
        self._tool_last_attempt[key] = now
        return self._tool_failures[key]
    
    def reset_tool_failures(self, tool_name: str, args_key: str) -> None:
        """Reset failure count for a tool (called on success)."""
        key = f"{tool_name}:{args_key}"
        self._tool_failures[key] = 0
    
    def check_tool_blocked(self, tool_name: str, args_key: str) -> tuple[bool, int]:
        """Check if tool is blocked due to too many failures.
        
        Returns:
            (is_blocked, failure_count)
        """
        key = f"{tool_name}:{args_key}"
        now = time.time()
        
        # Reset if enough time passed
        last_attempt = self._tool_last_attempt.get(key, 0)
        if now - last_attempt > self.FAILURE_RESET_SECONDS:
            self._tool_failures[key] = 0
            return False, 0
        
        count = self._tool_failures.get(key, 0)
        return count >= self.MAX_TOOL_FAILURES, count
    
    @classmethod
    def get(cls, device_ip: str) -> "DeviceManager":
        """Get or create DeviceManager for IP."""
        if device_ip not in cls._instances:
            cls._instances[device_ip] = cls(device_ip)
        return cls._instances[device_ip]
    
    # Thread-local storage for current device IP.
    # Prevents race condition when multiple threads (run_in_executor) call
    # set_current() simultaneously during fleet scheduler parallel startup.
    import threading
    _current_local = threading.local()
    
    @classmethod
    def set_current(cls, device_ip: str) -> None:
        """Set which device is the 'current' one for get_current() (thread-safe)."""
        cls._current_local.ip = device_ip
    
    @classmethod
    def get_current(cls) -> "DeviceManager | None":
        """Get the current DeviceManager (thread-safe).
        
        Returns the device set via set_current(), or falls back to the
        first (and usually only) instance.
        """
        current_ip = getattr(cls._current_local, 'ip', None)
        if current_ip and current_ip in cls._instances:
            return cls._instances[current_ip]
        if not cls._instances:
            return None
        return next(iter(cls._instances.values()))
    
    @property
    def device(self) -> "Device":
        """Get connected device, reconnect if stale or missing."""
        if self._device is None:
            self._connect()
        elif self._connection_time and (time.time() - self._connection_time) > self.MAX_CONNECTION_AGE_SECONDS:
            logger.info(f"gRPC connection age > {self.MAX_CONNECTION_AGE_SECONDS}s, proactive reconnect")
            self._connect()
        return self._device
    
    def _connect(self) -> None:
        """Establish connection to device, closing any previous channel first."""
        if self._device is not None:
            self._close_device_channel(self._device)
            self._device = None

        from lamda.client import Device
        
        self._device = Device(self.device_ip)
        self._connection_time = time.time()
        
        # Get actual screen dimensions
        try:
            info = self._device.device_info()
            self._screen_width = getattr(info, "displayWidth", 1080)
            self._screen_height = getattr(info, "displayHeight", 2400)
        except Exception:
            # Fallback to defaults
            pass
        
        # Initialize SimpleGestures (tested and working!)
        if GESTURES_AVAILABLE and SimpleGestures is not None:
            self._gestures = SimpleGestures(
                device=self._device,
                screen_width=self._screen_width,
                screen_height=self._screen_height,
            )
    
    @property
    def gestures(self) -> "SimpleGestures":
        """Get SimpleGestures instance (connects if needed).
        
        Raises:
            RuntimeError: If lamda package is not installed
        """
        if not GESTURES_AVAILABLE:
            raise RuntimeError("SimpleGestures not available - install lamda package")
        if self._gestures is None:
            self._connect()
        if self._gestures is None:
            raise RuntimeError("Failed to initialize SimpleGestures")
        return self._gestures
    
    @property
    def screen_width(self) -> int:
        if self._device is None:
            self._connect()
        return self._screen_width
    
    @property
    def screen_height(self) -> int:
        if self._device is None:
            self._connect()
        return self._screen_height
    
    def get_cached_xml(self) -> str | None:
        """Get cached XML if still valid (within TTL).
        
        Returns:
            Cached XML string, or None if cache expired/empty
        """
        if self._xml_cache is None:
            return None
        
        elapsed_ms = (time.time() - self._xml_cache_time) * 1000
        if elapsed_ms > self.XML_CACHE_TTL_MS:
            # Cache expired - must clear BOTH caches to stay in sync
            self._xml_cache = None
            self._elements_cache = None
            return None
        
        return self._xml_cache
    
    def set_xml_cache(self, xml_str: str) -> None:
        """Store XML in cache with current timestamp."""
        self._xml_cache = xml_str
        self._xml_cache_time = time.time()
    
    def invalidate_xml_cache(self) -> None:
        """Invalidate XML cache (call after any action that changes screen)."""
        self._xml_cache = None
        self._elements_cache = None  # Also invalidate elements cache
    
    def get_cached_screenshot(self, post_id: str | None = None) -> bytes | None:
        """Get cached screenshot if still valid and for same post.
        
        Args:
            post_id: Optional post identifier to check cache validity
            
        Returns:
            Cached screenshot bytes or None if cache invalid/expired
        """
        if self._screenshot_cache is None:
            return None
        
        elapsed_ms = (time.time() - self._screenshot_cache_time) * 1000
        if elapsed_ms > self.SCREENSHOT_CACHE_TTL_MS:
            self._screenshot_cache = None
            self._screenshot_post_id = None
            return None
        
        # If post_id provided, check it matches
        if post_id and self._screenshot_post_id != post_id:
            return None
        
        return self._screenshot_cache
    
    def set_screenshot_cache(self, data: bytes, post_id: str | None = None) -> None:
        """Store screenshot in cache.
        
        Args:
            data: Screenshot bytes
            post_id: Optional post identifier for cache key
        """
        self._screenshot_cache = data
        self._screenshot_cache_time = time.time()
        self._screenshot_post_id = post_id
    
    def invalidate_screenshot_cache(self) -> None:
        """Invalidate screenshot cache (call when screen changes significantly)."""
        self._screenshot_cache = None
        self._screenshot_post_id = None
    
    def get_cached_elements(self) -> list[dict] | None:
        """Get compressed elements from cache, or compute from raw XML if needed.
        
        Returns:
            List of compressed element dicts, or None if no XML available
        """
        # If elements already cached, return them
        if self._elements_cache is not None:
            return self._elements_cache
        
        # Try to compute from cached XML
        xml = self.get_cached_xml()
        if xml is None:
            return None
        
        # Lazy compute and cache
        self._elements_cache = compress_xml_for_llm(xml)
        return self._elements_cache
    
    def set_elements_cache(self, elements: list[dict]) -> None:
        """Store computed elements in cache."""
        self._elements_cache = elements
    
    def health_check(self) -> bool:
        """Check if device connection is healthy."""
        try:
            self.device.device_info()
            return True
        except Exception:
            self._device = None
            return False
    
    @staticmethod
    def _close_device_channel(device) -> None:
        """Close the gRPC channel on a lamda Device object.
        
        The lamda library stores its intercepted gRPC channel as ``device.chann``.
        We also attempt to reach the underlying raw channel (``ch._channel``)
        which is kept by ``grpc.intercept_channel`` wrappers.
        """
        for attr in ("chann", "_channel", "channel", "_conn", "conn"):
            ch = getattr(device, attr, None)
            if ch is None:
                continue
            try:
                raw = getattr(ch, "_channel", None)
                if raw is not None and hasattr(raw, "close"):
                    raw.close()
                if hasattr(ch, "close"):
                    ch.close()
            except Exception:
                pass
            break

    def disconnect(self) -> None:
        """Close gRPC connection and release resources.
        
        Call before spawning a subprocess that connects to the same device
        to avoid two simultaneous gRPC channels (causes too_many_pings GOAWAY).
        """
        if self._device is not None:
            try:
                self._close_device_channel(self._device)
                del self._device
            except Exception:
                pass
            self._device = None
        self._gestures = None
        self._connection_time = 0
        import gc
        gc.collect()
        logger.debug(f"Disconnected from {self.device_ip}")
    
    @classmethod
    def disconnect_all(cls) -> None:
        """Disconnect all device connections. Call before subprocess launch."""
        for ip, dm in cls._instances.items():
            dm.disconnect()
        logger.debug(f"Disconnected all {len(cls._instances)} device connections")
    
    def reconnect(self) -> bool:
        """Force reconnection, properly closing the old channel first."""
        if self._device is not None:
            self._close_device_channel(self._device)
        self._device = None
        self._gestures = None
        try:
            self._connect()
            return True
        except Exception:
            return False
    
    def with_reconnect(self, func, *args, max_retries: int = 2, **kwargs):
        """Execute function with automatic reconnect on connection errors.
        
        Wraps function calls to automatically retry with reconnection
        if a gRPC/connection error occurs.
        
        Args:
            func: Function to execute (should use self.device internally)
            *args: Arguments for func
            max_retries: Maximum retry attempts
            **kwargs: Keyword arguments for func
            
        Returns:
            Function result
            
        Raises:
            Last exception if all retries fail
        """
        last_error = None
        
        for attempt in range(max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                error_str = str(e).lower()
                # Check if this is a connection error
                is_connection_error = any(x in error_str for x in [
                    "inactiverpcerror",
                    "grpc",
                    "connection",
                    "unavailable",
                    "deadline",
                    "timeout",
                    "reset",
                    "broken pipe",
                    "protocolerror",
                    "not json serializable",
                    "too_many_pings",
                    "goaway",
                    "stream removed",
                    "channel closed",
                ])
                
                if not is_connection_error or attempt >= max_retries:
                    raise
                
                last_error = e
                # Try to reconnect (use module-level logger)
                logger.warning(f"Connection error (attempt {attempt + 1}/{max_retries + 1}): {e}")
                logger.info("Attempting reconnect...")
                
                if self.reconnect():
                    logger.info("Reconnected successfully, retrying...")
                    time.sleep(0.1)  # Brief delay to ensure gRPC connection is stable
                else:
                    logger.error("Reconnect failed")
                    raise
        
        # Should not reach here, but just in case
        if last_error:
            raise last_error


# =============================================================================
# Public API for accessing DeviceManager
# =============================================================================

def get_device_manager() -> DeviceManager | None:
    """Get the current DeviceManager instance.
    
    Used by callbacks and other code that needs access to device state
    without knowing the device IP.
    
    Returns:
        Current DeviceManager or None if no device is connected.
    """
    return DeviceManager.get_current()


# Stored reference to the type_text closure from create_firerpa_tools.
# Used by posting_tools.type_posting_caption() to type captions reliably.
_type_text_ref: Callable | None = None


def get_type_text_fn():
    """Get the type_text function created by create_firerpa_tools."""
    return _type_text_ref


# =============================================================================
# Tool Factory
# =============================================================================

def create_firerpa_tools(device_ip: str) -> list[FunctionTool]:
    """Create all FIRERPA tools for an agent.
    
    Args:
        device_ip: IP address of FIRERPA device
        
    Returns:
        List of FunctionTools for ADK agent
    """
    dm = DeviceManager.get(device_ip)
    
    # -------------------------------------------------------------------------
    # Screen Tools (XML-FIRST approach)
    # -------------------------------------------------------------------------
    
    def screenshot(quality: int = 60, post_username: str = "", post_timestamp: str = "") -> dict:
        """Take screenshot for visual analysis of post content.
        
        Use this when you need to SEE what's in a post:
        - Analyzing photos/videos for contextual comments
        - Detecting CTA text on images  
        - Understanding post content visually
        
        For NAVIGATION, use get_screen_xml() instead!
        
        Args:
            quality: JPEG quality (1-100, default 60 for balance)
            post_username: Post author username (for caching)
            post_timestamp: Post timestamp (for caching)
            
        Returns:
            dict with screenshot info. Image added to context automatically.
        """
        # Generate post_id for caching if both params provided
        post_id = None
        if post_username and post_timestamp:
            post_id = f"{post_username}_{post_timestamp}"
            
            # Check cache first
            cached = dm.get_cached_screenshot(post_id)
            if cached:
                dm.last_screenshot = cached
                dm.last_screenshot_id = f"screenshot_cached_{post_id}"
                size_kb = len(cached) // 1024
                logger.info(f"📸 Screenshot from cache: {size_kb}KB (post: {post_username})")
                return {
                    "status": "success",
                    "captured": True,
                    "cached": True,
                    "size_kb": size_kb,
                    "screenshot_id": dm.last_screenshot_id,
                    "note": "Screenshot from cache. Look at the image to understand post content.",
                }
        
        # Take fresh screenshot
        img_bytes = dm.device.screenshot(quality)
        img_data = img_bytes.getvalue()
        size_kb = len(img_data) // 1024
        
        # Cache if post_id available
        if post_id:
            dm.set_screenshot_cache(img_data, post_id)
        
        # Store screenshot for callback to add to LLM context
        # This is picked up by before_model_callback in instagram_agent.py
        dm.last_screenshot = img_data
        dm.last_screenshot_id = f"screenshot_{id(img_data)}"
        
        # Log that screenshot was taken
        logger.info(f"📸 Screenshot captured: {size_kb}KB")
        
        return {
            "status": "success",
            "captured": True,
            "cached": False,
            "size_kb": size_kb,
            "screenshot_id": dm.last_screenshot_id,
            "note": "Screenshot captured and ready for analysis. Look at the image to understand post content.",
        }
    
    def get_screen_xml() -> dict:
        """Get XML dump of current screen hierarchy with validation.
        
        PRIMARY tool for navigation decisions. Use this to:
        - Understand current screen (Instagram feed, profile, etc.)
        - Find elements by resourceId, text, content-desc
        - Determine what actions are available
        
        Includes automatic reconnect on connection errors.
        
        Returns:
            dict with:
            - xml: XML content as string
            - valid: Whether XML is valid and complete
            - screen_context: Detected screen type (if valid)
            - in_instagram: Whether we're in Instagram app
        """
        # Local logger ref to avoid closure scoping issues with stale bytecode
        _log = logging.getLogger("eidola.tools")
        config = get_config(Stage.GET_SCREEN_XML)
        reconnect_attempted = False
        
        # Check cache first (avoids redundant XML dumps when multiple tools called together)
        cached_xml = dm.get_cached_xml()
        if cached_xml:
            # Use cached XML - still need to detect screen
            screen_result = _detect_screen(cached_xml)
            context_str = screen_result.context.value
            in_insta = is_in_instagram(cached_xml)
            if _debug_config["verbose"]:
                _log.debug(f"Using cached XML: {context_str}")
            return {
                "xml": cached_xml,
                "valid": True,
                "screen_context": context_str,
                "in_instagram": in_insta,
                "has_dialog": screen_result.has_dialog,
                "has_keyboard": screen_result.has_keyboard,
                "cached": True,
            }
        
        for attempt in range(config.max_retries + 1):
            try:
                xml_bytes = dm.device.dump_window_hierarchy()
                xml_str = xml_bytes.getvalue().decode("utf-8")
                
                # Validate size
                if len(xml_str) < MIN_XML_SIZE:
                    if attempt < config.max_retries:
                        time.sleep(config.get_delay(attempt))
                        continue
                    return {
                        "xml": xml_str,
                        "valid": False,
                        "error": "xml_too_small",
                        "in_instagram": False,
                    }
                
                # Validate parse
                ET.fromstring(xml_str)  # Will raise if invalid
                
                # Detect screen context
                screen_result = _detect_screen(xml_str)
                context_str = screen_result.context.value
                in_insta = is_in_instagram(xml_str)
                
                # Cache the fresh XML for other tools
                dm.set_xml_cache(xml_str)
                
                # Debug: save XML dump
                saved_path = _save_xml_dump(xml_str, context_str)
                if _debug_config["verbose"]:
                    _log.debug(f"Screen: {context_str}, in_instagram={in_insta}, "
                               f"dialog={screen_result.has_dialog}, keyboard={screen_result.has_keyboard}")
                
                result = {
                    "xml": xml_str,
                    "valid": True,
                    "screen_context": context_str,
                    "in_instagram": in_insta,
                    "has_dialog": screen_result.has_dialog,
                    "has_keyboard": screen_result.has_keyboard,
                }
                if saved_path:
                    result["debug_xml_path"] = saved_path
                return result
                
            except ET.ParseError:
                if attempt < config.max_retries:
                    time.sleep(config.get_delay(attempt))
                    continue
                return {
                    "xml": "",
                    "valid": False,
                    "error": "xml_parse_error",
                    "in_instagram": False,
                }
            except Exception as e:
                error_str = str(e).lower()
                # Check if this is a connection error
                is_connection_error = any(x in error_str for x in [
                    "inactiverpcerror", "grpc", "connection", "unavailable",
                    "deadline", "timeout", "reset", "broken pipe",
                    "too_many_pings", "goaway", "stream removed", "channel closed",
                ])
                
                # Try reconnect once on connection error
                if is_connection_error and not reconnect_attempted:
                    reconnect_attempted = True
                    _log.warning(f"Connection error in get_screen_xml: {e}")
                    _log.info("Attempting reconnect...")
                    if dm.reconnect():
                        _log.info("Reconnected, retrying...")
                        time.sleep(0.1)  # Brief delay to ensure gRPC connection is stable
                        continue
                
                if attempt < config.max_retries:
                    time.sleep(config.get_delay(attempt))
                    continue
                return {
                    "xml": "",
                    "valid": False,
                    "error": str(e),
                    "in_instagram": False,
                }
        
        return {
            "xml": "",
            "valid": False,
            "error": "max_retries_exceeded",
            "in_instagram": False,
        }
    
    def get_screen_elements() -> dict:
        """Get compressed UI elements optimized for AI navigation decisions.
        
        PRIMARY tool for fast navigation. Returns ~60-80 key elements instead of
        full XML (~350 lines), reducing token usage by ~80%.
        
        Use this for:
        - Finding navigation targets (buttons, tabs)
        - Detecting interactive elements
        - Quick screen understanding
        
        Use get_screen_xml() only when you need:
        - Full XML structure for debugging
        - Detailed element attributes
        
        Returns:
            dict with:
            - elements: List of compressed element dicts
            - count: Number of elements
            - screen_context: Detected screen type
            - in_instagram: Whether in Instagram app
            
        Element format:
            - id: resource-id (short, without package)
            - d: content-desc (accessibility text)
            - t: text content
            - b: bounds "[x1,y1][x2,y2]"
            - c: class name (short)
            - tap: true if clickable
            - scr: true if scrollable
            - sel: true if selected
            - foc: true if focusable
            - p: parent id (for hierarchy context)
        """
        _log = logging.getLogger("eidola.tools")
        
        # Try to get from elements cache first
        cached_elements = dm.get_cached_elements()
        if cached_elements is not None:
            # Need to get screen context from XML cache
            cached_xml = dm.get_cached_xml()
            if cached_xml:
                screen_result = _detect_screen(cached_xml)
                if _debug_config["verbose"]:
                    _log.debug(f"Using cached elements: {len(cached_elements)} elements")
                return {
                    "elements": cached_elements,
                    "count": len(cached_elements),
                    "screen_context": screen_result.context.value,
                    "in_instagram": is_in_instagram(cached_xml),
                    "has_dialog": screen_result.has_dialog,
                    "has_keyboard": screen_result.has_keyboard,
                    "cached": True,
                }
        
        # Get fresh XML and compress
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {
                "elements": [],
                "count": 0,
                "screen_context": "unknown",
                "in_instagram": False,
                "error": xml_result.get("error", "xml_invalid"),
            }
        
        # Compress XML to elements
        elements = compress_xml_for_llm(xml_result["xml"])
        
        # Cache the computed elements
        dm.set_elements_cache(elements)
        
        if _debug_config["verbose"]:
            _log.debug(f"Compressed XML to {len(elements)} elements")
        
        return {
            "elements": elements,
            "count": len(elements),
            "screen_context": xml_result.get("screen_context", "unknown"),
            "in_instagram": xml_result.get("in_instagram", False),
            "has_dialog": xml_result.get("has_dialog", False),
            "has_keyboard": xml_result.get("has_keyboard", False),
        }
    
    # -------------------------------------------------------------------------
    # XML-based Navigation Tools (NEW)
    # -------------------------------------------------------------------------
    
    def detect_screen() -> dict:
        """Detect current screen context from XML.
        
        Use this to understand WHERE you are:
        - Instagram feed, profile, search, reels, etc.
        - System UI (notification shade)
        - Other apps
        
        Returns:
            dict with screen context and confidence
        """
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {
                "screen_context": ScreenContext.UNKNOWN.value,
                "confidence": 0.0,
                "in_instagram": False,
                "needs_recovery": True,
            }
        
        screen_result = _detect_screen(xml_result["xml"])
        
        return {
            "screen_context": screen_result.context.value,
            "confidence": screen_result.confidence,
            "package": screen_result.package,
            "in_instagram": is_in_instagram(xml_result["xml"]),
            "has_dialog": screen_result.has_dialog,
            "has_keyboard": screen_result.has_keyboard,
            "needs_recovery": needs_recovery(screen_result),
        }
    
    def find_element(selector_name: str) -> dict:
        """Find Instagram UI element by selector name.
        
        Uses predefined selectors with fallbacks for common Instagram elements.
        Available selectors: like_button, comment_button, share_button,
        feed_tab, search_tab, reels_tab, profile_tab, etc.
        
        Args:
            selector_name: Name from INSTAGRAM_SELECTORS registry
            
        Returns:
            dict with element info if found, or error if not found
        """
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {"found": False, "error": "invalid_xml"}
        
        finder = create_finder(xml_result["xml"])
        element = finder.find(selector_name)
        
        if element:
            return {
                "found": True,
                "selector_name": selector_name,
                "text": element.text,
                "content_desc": element.content_desc,
                "resource_id": element.resource_id,
                "center_x": element.center[0],
                "center_y": element.center[1],
                "clickable": element.clickable,
            }
        else:
            return {
                "found": False,
                "selector_name": selector_name,
                "error": "element_not_found",
                "available_selectors": list(INSTAGRAM_SELECTORS.keys())[:10],  # Sample
            }
    
    def is_post_liked(target_username: str) -> dict:
        """Check if a specific post is already liked.
        
        Looks for like button state via content-desc and selected attributes:
        - "Like" + selected=false → NOT liked
        - "Liked" + selected=true → ALREADY liked
        
        Use this BEFORE liking to avoid double-liking.
        
        Args:
            target_username: Username of the post author (REQUIRED).
                            Validates that the Like button belongs to this user's post
                            by checking spatial relationships in the XML hierarchy.
        
        Returns:
            dict with:
            - is_liked: True if already liked, False if not
            - like_button_found: True if like button is visible
            - can_like: True if post can be liked (button found + not liked)
            - verified_for_user: Username if spatial verification passed
        """
        if not target_username:
            return {
                "is_liked": False,
                "like_button_found": False,
                "can_like": False,
                "error": "target_username is required to avoid liking wrong post"
            }
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {"is_liked": False, "like_button_found": False, "can_like": False, "error": "invalid_xml"}
        
        xml_str = xml_result["xml"]
        
        import xml.etree.ElementTree as ET
        import re
        
        def parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
            """Parse bounds string to (x1, y1, x2, y2)."""
            match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
            if match:
                return tuple(int(x) for x in match.groups())
            return None
        
        try:
            root = ET.fromstring(xml_str)
            
            # Collect all post headers and like buttons with their Y positions
            post_headers = []  # [(username, y_top, y_bottom), ...]
            like_buttons = []  # [(node, content_desc, selected, y_center), ...]
            
            for node in root.iter("node"):
                res_id = node.get("resource-id", "")
                content_desc = node.get("content-desc", "")
                
                # Collect post headers (row_feed_profile_header has content-desc like "username posted...")
                if "row_feed_profile_header" in res_id or (
                    "row_feed_photo_profile_name" in res_id
                ):
                    bounds = parse_bounds(node.get("bounds", ""))
                    if bounds:
                        # Extract username from text or content-desc
                        text = node.get("text", "").strip()
                        if text:
                            # row_feed_photo_profile_name has text="username"
                            username = text.split()[0].rstrip("  ")  # Remove trailing spaces
                        elif content_desc:
                            # row_feed_profile_header has content-desc="username posted..."
                            username = content_desc.split()[0] if content_desc else ""
                        else:
                            continue
                        
                        if username:
                            y_center = (bounds[1] + bounds[3]) // 2
                            post_headers.append((username, bounds[1], bounds[3], y_center))
                
                # Collect like buttons
                if "row_feed_button_like" in res_id:
                    bounds = parse_bounds(node.get("bounds", ""))
                    if bounds:
                        y_center = (bounds[1] + bounds[3]) // 2
                        selected = node.get("selected", "false") == "true"
                        like_buttons.append((node, content_desc, selected, y_center, bounds))
            
            if not like_buttons:
                return {
                    "is_liked": False,
                    "like_button_found": False,
                    "can_like": False,
                    "note": "Like button not visible - scroll to show post actions",
                }
            
            # Find the matching post and its Like button
            target_username_clean = target_username.lower().strip()
            
            # Find the post header for this username
            target_header = None
            for username, y_top, y_bottom, y_center in post_headers:
                if username.lower().strip() == target_username_clean:
                    target_header = (username, y_top, y_bottom, y_center)
                    break
            
            if not target_header:
                return {
                    "is_liked": False,
                    "like_button_found": False,
                    "can_like": False,
                    "error": f"Post header for '{target_username}' not found on screen",
                    "visible_users": [h[0] for h in post_headers],
                }
            
            # Find the Like button that is:
            # 1. Below the target post header (higher Y)
            # 2. Above any OTHER post header below the target
            target_y = target_header[3]  # Y center of target header
            
            # Find next post header below target (if any)
            next_header_y = float('inf')
            for username, y_top, y_bottom, y_center in post_headers:
                if y_center > target_y + 50:  # 50px tolerance
                    next_header_y = min(next_header_y, y_top)
            
            # Find the Like button that belongs to target post
            valid_like_button = None
            for node, desc, selected, y_center, bounds in like_buttons:
                # Like button must be below target header and above next header
                if y_center > target_y and y_center < next_header_y:
                    valid_like_button = (node, desc, selected, y_center, bounds)
                    break
            
            if not valid_like_button:
                return {
                    "is_liked": False,
                    "like_button_found": False,
                    "can_like": False,
                    "error": f"Like button for '{target_username}' not visible",
                    "note": "Scroll to show the post's engagement buttons",
                    "target_header_y": target_y,
                }
            
            # Found the correct Like button
            node, content_desc, selected, y_center, bounds = valid_like_button
            is_liked = content_desc == "Liked" or selected
            
            return {
                "is_liked": is_liked,
                "like_button_found": True,
                "can_like": not is_liked,
                "content_desc": content_desc,
                "selected": selected,
                "verified_for_user": target_username,
                "like_button_y": y_center,
                "like_button_bounds": bounds,
            }
            
        except ET.ParseError:
            return {"is_liked": False, "like_button_found": False, "can_like": False, "error": "xml_parse_error"}
    
    def is_post_saved(target_username: str) -> dict:
        """Check if a specific post is already saved/bookmarked.
        
        Looks for save button state via content-desc and selected attributes:
        - "Add to Saved" + selected=false → NOT saved
        - "Remove from saved" + selected=true → ALREADY saved
        
        Use this BEFORE saving to avoid redundant saves.
        
        Args:
            target_username: Username of the post author (REQUIRED).
                            Validates that the Save button belongs to this user's post
                            by checking spatial relationships in the XML hierarchy.
        
        Returns:
            dict with:
            - is_saved: True if already saved, False if not
            - save_button_found: True if save button is visible
            - can_save: True if post can be saved (button found + not saved)
            - verified_for_user: Username if spatial verification passed
        """
        if not target_username:
            return {
                "is_saved": False,
                "save_button_found": False,
                "can_save": False,
                "error": "target_username is required to avoid saving wrong post"
            }
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {"is_saved": False, "save_button_found": False, "can_save": False, "error": "invalid_xml"}
        
        xml_str = xml_result["xml"]
        
        import xml.etree.ElementTree as ET
        import re
        
        def parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
            """Parse bounds string to (x1, y1, x2, y2)."""
            match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
            if match:
                return tuple(int(x) for x in match.groups())
            return None
        
        try:
            root = ET.fromstring(xml_str)
            
            # Collect all post headers and save buttons with their Y positions
            post_headers = []  # [(username, y_top, y_bottom, y_center), ...]
            save_buttons = []  # [(node, content_desc, selected, y_center, bounds), ...]
            
            for node in root.iter("node"):
                res_id = node.get("resource-id", "")
                content_desc = node.get("content-desc", "")
                
                # Collect post headers
                if "row_feed_profile_header" in res_id or (
                    "row_feed_photo_profile_name" in res_id
                ):
                    bounds = parse_bounds(node.get("bounds", ""))
                    if bounds:
                        text = node.get("text", "").strip()
                        if text:
                            username = text.split()[0].rstrip("  ")
                        elif content_desc:
                            username = content_desc.split()[0] if content_desc else ""
                        else:
                            continue
                        
                        if username:
                            y_center = (bounds[1] + bounds[3]) // 2
                            post_headers.append((username, bounds[1], bounds[3], y_center))
                
                # Collect save buttons
                if "row_feed_button_save" in res_id:
                    bounds = parse_bounds(node.get("bounds", ""))
                    if bounds:
                        y_center = (bounds[1] + bounds[3]) // 2
                        selected = node.get("selected", "false") == "true"
                        save_buttons.append((node, content_desc, selected, y_center, bounds))
            
            if not save_buttons:
                return {
                    "is_saved": False,
                    "save_button_found": False,
                    "can_save": False,
                    "note": "Save button not visible - scroll to show post actions",
                }
            
            # Find the matching post and its Save button
            target_username_clean = target_username.lower().strip()
            
            # Find the post header for this username
            target_header = None
            for username, y_top, y_bottom, y_center in post_headers:
                if username.lower().strip() == target_username_clean:
                    target_header = (username, y_top, y_bottom, y_center)
                    break
            
            if not target_header:
                return {
                    "is_saved": False,
                    "save_button_found": False,
                    "can_save": False,
                    "error": f"Post header for '{target_username}' not found on screen",
                    "visible_users": [h[0] for h in post_headers],
                }
            
            target_y = target_header[3]
            
            # Find next post header below target (if any)
            next_header_y = float('inf')
            for username, y_top, y_bottom, y_center in post_headers:
                if y_center > target_y + 50:
                    next_header_y = min(next_header_y, y_top)
            
            # Find the Save button that belongs to target post
            valid_save_button = None
            for node, desc, selected, y_center, bounds in save_buttons:
                if y_center > target_y and y_center < next_header_y:
                    valid_save_button = (node, desc, selected, y_center, bounds)
                    break
            
            if not valid_save_button:
                return {
                    "is_saved": False,
                    "save_button_found": False,
                    "can_save": False,
                    "error": f"Save button for '{target_username}' not visible",
                    "note": "Scroll to show the post's engagement buttons",
                    "target_header_y": target_y,
                }
            
            # Found the correct Save button
            node, content_desc, selected, y_center, bounds = valid_save_button
            is_saved = "remove" in content_desc.lower() or selected
            
            return {
                "is_saved": is_saved,
                "save_button_found": True,
                "can_save": not is_saved,
                "content_desc": content_desc,
                "selected": selected,
                "verified_for_user": target_username,
                "save_button_y": y_center,
                "save_button_bounds": bounds,
            }
            
        except ET.ParseError:
            return {"is_saved": False, "save_button_found": False, "can_save": False, "error": "xml_parse_error"}
    
    def get_post_engagement_buttons(target_username: str) -> dict:
        """Get engagement button coordinates for a specific post.
        
        CRITICAL: Use this to get the CORRECT button coordinates before tapping.
        When multiple posts are visible, this ensures you interact with the right post.
        
        AUTO-ENRICHED: This tool automatically checks nurtured status and includes
        it in the response. If `is_nurtured: true`, you MUST engage (no skipping)!
        
        AUTO-SCROLL: If the post header is visible but buttons are NOT, this function
        automatically performs 1-2 small scrolls to bring buttons into view. No need
        to manually call scroll_to_post_buttons() first!
        
        Args:
            target_username: Username of the post author (required)
        
        Returns:
            dict with:
            - found: True if post and buttons found
            - like_button: {x, y, is_liked, content_desc} or None
            - comment_button: {x, y} or None
            - share_button: {x, y} or None
            - save_button: {x, y, is_saved, content_desc} or None
            - post_header_y: Y coordinate of post header
            - is_nurtured: True if this is a VIP account (AUTO-CHECKED!)
            - nurtured_priority: "vip"/"high"/"medium" if nurtured
            - auto_scrolled: True if auto-scroll was performed
            - scrolls_done: Number of auto-scrolls performed (if any)
        """
        if not target_username:
            return {"found": False, "error": "target_username is required"}
        
        # AUTO-CHECK NURTURED STATUS (so agent can't skip this!)
        nurtured_info = {"is_nurtured": False}
        try:
            from .memory_tools import is_nurtured_account
            nurtured_result = is_nurtured_account(target_username)
            nurtured_info = {
                "is_nurtured": nurtured_result.get("is_nurtured", False),
                "nurtured_priority": nurtured_result.get("priority"),
            }
        except Exception as e:
            logger.warning(f"Could not check nurtured status: {e}")
        
        import xml.etree.ElementTree as ET
        import re
        
        def parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
            match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
            if match:
                return tuple(int(x) for x in match.groups())
            return None
        
        def bounds_to_center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
            return ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
        
        def find_buttons_in_xml(xml_str: str) -> dict:
            """Inner function to find buttons. Returns result dict."""
            try:
                root = ET.fromstring(xml_str)
                
                # Collect post headers
                post_headers = []
                for node in root.iter("node"):
                    res_id = node.get("resource-id", "")
                    content_desc = node.get("content-desc", "")
                    
                    if "row_feed_profile_header" in res_id or "row_feed_photo_profile_name" in res_id:
                        bounds = parse_bounds(node.get("bounds", ""))
                        if bounds:
                            text = node.get("text", "").strip()
                            if text:
                                username = text.split()[0].rstrip("  ")
                            elif content_desc:
                                username = content_desc.split()[0] if content_desc else ""
                            else:
                                continue
                            
                            if username:
                                y_center = (bounds[1] + bounds[3]) // 2
                                post_headers.append((username, bounds[1], bounds[3], y_center))
                
                # Find target post header
                target_username_clean = target_username.lower().strip()
                target_header = None
                for username, y_top, y_bottom, y_center in post_headers:
                    if username.lower().strip() == target_username_clean:
                        target_header = (username, y_top, y_bottom, y_center)
                        break
                
                if not target_header:
                    return {
                        "found": False,
                        "error": f"Post header for '{target_username}' not found",
                        "visible_users": [h[0] for h in post_headers],
                    }
                
                target_y = target_header[3]
                
                # Find next post header below target
                next_header_y = float('inf')
                for username, y_top, y_bottom, y_center in post_headers:
                    if y_center > target_y + 50:
                        next_header_y = min(next_header_y, y_top)
                
                # Collect all engagement buttons
                buttons = {
                    "like": None,
                    "comment": None,
                    "share": None,
                    "save": None,
                }
                
                for node in root.iter("node"):
                    res_id = node.get("resource-id", "")
                    content_desc = node.get("content-desc", "")
                    bounds = parse_bounds(node.get("bounds", ""))
                    
                    if not bounds:
                        continue
                    
                    y_center = (bounds[1] + bounds[3]) // 2
                    
                    # Check if this button is in the target post's region
                    if not (y_center > target_y and y_center < next_header_y):
                        continue
                    
                    center = bounds_to_center(bounds)
                    
                    # Check by resource-id (old Instagram) OR content-desc (new Instagram)
                    # Like button
                    is_like_by_id = "row_feed_button_like" in res_id
                    is_like_by_desc = content_desc in ("Like", "Liked")
                    if (is_like_by_id or is_like_by_desc) and buttons["like"] is None:
                        selected = node.get("selected", "false") == "true"
                        is_liked = content_desc == "Liked" or selected
                        buttons["like"] = {
                            "x": center[0],
                            "y": center[1],
                            "is_liked": is_liked,
                            "content_desc": content_desc,
                        }
                    # Comment button
                    elif ("row_feed_button_comment" in res_id or content_desc == "Comment") and buttons["comment"] is None:
                        buttons["comment"] = {"x": center[0], "y": center[1]}
                    # Share button
                    elif ("row_feed_button_share" in res_id or content_desc == "Send Post") and buttons["share"] is None:
                        buttons["share"] = {"x": center[0], "y": center[1]}
                    # Save button
                    elif ("row_feed_button_save" in res_id or content_desc in ("Add to Saved", "Remove from Saved")) and buttons["save"] is None:
                        selected = node.get("selected", "false") == "true"
                        is_saved = "remove" in content_desc.lower() or selected
                        buttons["save"] = {
                            "x": center[0],
                            "y": center[1],
                            "is_saved": is_saved,
                            "content_desc": content_desc,
                        }
                
                # Check if we found at least one button
                has_buttons = any(buttons.values())
                
                if not has_buttons:
                    return {
                        "found": False,
                        "error": f"Engagement buttons for '{target_username}' not visible",
                        "note": "Scroll to show the post's Like/Comment/Share/Save buttons",
                        "post_header_y": target_y,
                        "verified_user": target_username,
                    }
                
                return {
                    "found": True,
                    "verified_user": target_username,
                    "post_header_y": target_y,
                    "like_button": buttons["like"],
                    "comment_button": buttons["comment"],
                    "share_button": buttons["share"],
                    "save_button": buttons["save"],
                }
                
            except ET.ParseError:
                return {"found": False, "error": "xml_parse_error"}
        
        # First attempt: Try to find buttons without scrolling
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {"found": False, "error": "invalid_xml", **nurtured_info}
        
        result = find_buttons_in_xml(xml_result["xml"])
        
        # If buttons found on first try, return success
        if result.get("found"):
            result.update(nurtured_info)
            return result
        
        # If post header not found, user is not on screen - no point in scrolling
        if "Post header" in result.get("error", ""):
            result.update(nurtured_info)
            return result
        
        # =====================================================================
        # AUTO-SCROLL LOGIC: Header found but buttons NOT visible
        # Use tiny scrolls (150-250px) to nudge buttons into view without
        # pushing the post header off screen. 3 attempts max.
        # =====================================================================
        header_y = result.get("post_header_y")  # We have header position
        max_scroll_attempts = 3
        scrolls_done = 0
        
        logger.info(f"🔄 Auto-scroll: Header for '{target_username}' found at y={header_y}, but buttons not visible. Starting gentle auto-scroll...")
        
        for scroll_attempt in range(max_scroll_attempts):
            # Scroll enough to actually reveal buttons below the post image
            scroll_distance = random.randint(300, 600)
            
            try:
                dm.gestures.scroll_precise(scroll_distance, variability_percent=10)
            except Exception as scroll_err:
                logger.warning(f"Auto-scroll attempt {scroll_attempt + 1} failed: {scroll_err}")
                continue
            dm.invalidate_xml_cache()
            scrolls_done += 1
            
            # Check for buttons again
            xml_result = get_screen_xml()
            if not xml_result.get("valid"):
                continue
            
            result = find_buttons_in_xml(xml_result["xml"])
            
            # Found buttons after scroll!
            if result.get("found"):
                result.update(nurtured_info)
                result["auto_scrolled"] = True
                result["scrolls_done"] = scrolls_done
                logger.info(f"✅ Auto-scroll SUCCESS: Found buttons for '{target_username}' after {scrolls_done} scroll(s)")
                return result
            
            # If header disappeared (scrolled off), we've gone too far - stop
            if "Post header" in result.get("error", ""):
                logger.warning(f"⚠️ Auto-scroll: Header for '{target_username}' scrolled off after {scrolls_done} scroll(s)")
                return {
                    "found": False,
                    "error": f"Post '{target_username}' scrolled off screen during auto-scroll",
                    "note": "Post lost - continue with scroll_feed() to next post. DO NOT scroll_back().",
                    "scrolls_done": scrolls_done,
                    "visible_users": result.get("visible_users", []),
                    **nurtured_info,
                }
        
        # After max scrolls, buttons still not visible but header still there
        # This means the post is very tall (large image/video/reel)
        logger.warning(f"⚠️ Auto-scroll: Buttons for '{target_username}' still not visible after {scrolls_done} scrolls")
        result.update(nurtured_info)
        result["auto_scrolled"] = True
        result["scrolls_done"] = scrolls_done
        result["note"] = f"Auto-scrolled {scrolls_done}x but buttons still not visible. Post may be very tall. Try scroll_to_post_buttons(max_scrolls=3) or skip."
        return result
    
    def check_post_liked(target_username: str) -> dict:
        """Check if a post is already liked by looking at the screen.
        
        SIMPLE AND RELIABLE: Reads the Like button status directly from XML.
        - "Like" = NOT liked
        - "Liked" = ALREADY liked
        
        USE THIS instead of MongoDB memory checks!
        
        Args:
            target_username: Username of the post author
        
        Returns:
            dict with:
            - is_liked: True if already liked, False if not
            - can_like: True if Like button is visible and post NOT liked
            - like_button: {x, y} coordinates for tapping (if visible)
            - note: Human-readable status
        """
        buttons = get_post_engagement_buttons(target_username)
        
        if not buttons.get("found"):
            return {
                "is_liked": None,
                "can_like": False,
                "error": buttons.get("error", "Post not found"),
                "note": "Cannot determine like status - buttons not visible",
            }
        
        like_btn = buttons.get("like_button")
        if not like_btn:
            return {
                "is_liked": None,
                "can_like": False,
                "note": "Like button not visible - scroll to show buttons",
            }
        
        is_liked = like_btn.get("is_liked", False)
        
        return {
            "is_liked": is_liked,
            "can_like": not is_liked,
            "like_button": {"x": like_btn["x"], "y": like_btn["y"]} if not is_liked else None,
            "note": f"Post is {'ALREADY LIKED ✓' if is_liked else 'NOT liked - can tap Like'}",
            "is_nurtured": buttons.get("is_nurtured", False),
        }
    
    def get_caption_info(target_username: str, expand_if_truncated: bool = True) -> dict:
        """Get caption text for a post, optionally expanding if truncated.
        
        Instagram truncates long captions with a "more" button. This function
        detects truncation and can expand the caption to get the full text.
        
        IMPORTANT: Use this for nurtured accounts to read full CTA (call-to-action)
        before generating comments. For example, post might say "Comment TYPE" 
        but only "Comment TY..." is visible without expansion.
        
        AUTO-ENRICHED: This tool automatically checks nurtured status and includes
        it in the response. If `is_nurtured: true`, you MUST engage and follow CTA!
        
        NOTE: One user can have multiple posts in feed. This function finds
        the caption for the FIRST visible post by this username.
        
        Args:
            target_username: Post author username (required)
            expand_if_truncated: If True and caption has "more" button, tap to expand
        
        Returns:
            dict with:
            - found: True if caption found
            - is_truncated: True if "more" button present (before expansion)
            - caption_text: Full or truncated caption text
            - expanded: True if expansion was performed
            - cta_detected: Possible CTA keywords found (TYPE, DM, LINK, etc.)
            - visible_users: List of usernames whose posts are currently visible
            - is_nurtured: True if this is a VIP account (AUTO-CHECKED!)
            - nurtured_priority: "vip"/"high"/"medium" if nurtured
        """
        if not target_username:
            return {"found": False, "error": "target_username is required"}
        
        # AUTO-CHECK NURTURED STATUS (so agent can't skip this!)
        nurtured_info = {"is_nurtured": False}
        try:
            from .memory_tools import is_nurtured_account
            nurtured_result = is_nurtured_account(target_username)
            nurtured_info = {
                "is_nurtured": nurtured_result.get("is_nurtured", False),
                "nurtured_priority": nurtured_result.get("priority"),
            }
        except Exception as e:
            logger.warning(f"Could not check nurtured status: {e}")
        
        import xml.etree.ElementTree as ET
        import re
        
        def parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
            match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
            if match:
                return tuple(int(x) for x in match.groups())
            return None
        
        def extract_caption_from_xml(xml_str: str, username: str) -> dict:
            """Extract caption info from XML for a specific user's post."""
            try:
                root = ET.fromstring(xml_str)
                
                # Find post headers to identify post regions
                target_username_clean = username.lower().strip()
                target_y_top = None
                next_post_y = float('inf')
                
                post_headers = []
                for node in root.iter("node"):
                    res_id = node.get("resource-id", "")
                    if "row_feed_photo_profile_name" in res_id or "row_feed_profile_header" in res_id:
                        bounds = parse_bounds(node.get("bounds", ""))
                        text = node.get("text", "").strip()
                        content_desc = node.get("content-desc", "")
                        
                        if bounds:
                            node_username = text.split()[0] if text else content_desc.split()[0] if content_desc else ""
                            if node_username:
                                post_headers.append((node_username.lower().strip(), bounds[1], bounds[3]))
                                if node_username.lower().strip() == target_username_clean:
                                    target_y_top = bounds[1]
                
                if target_y_top is None:
                    return {"found": False, "visible_users": [h[0] for h in post_headers]}
                
                # Find next post below target
                for u, y_top, y_bottom in post_headers:
                    if y_top > target_y_top + 100:
                        next_post_y = min(next_post_y, y_top)
                
                # Look for caption elements in target post region
                caption_text = ""
                more_button = None
                is_truncated = False
                
                for node in root.iter("node"):
                    bounds = parse_bounds(node.get("bounds", ""))
                    if not bounds:
                        continue
                    
                    y_center = (bounds[1] + bounds[3]) // 2
                    
                    # Only look in target post's region
                    if not (y_center > target_y_top and y_center < next_post_y):
                        continue
                    
                    res_id = node.get("resource-id", "")
                    text = node.get("text", "")
                    content_desc = node.get("content-desc", "")
                    
                    # Look for caption container (IgTextLayoutView with post text)
                    node_class = node.get("class", "")
                    
                    # Check for "more" button - multiple detection methods
                    # Method 1: Separate "more" element
                    if content_desc == "more" or text == "more":
                        more_button = {
                            "x": (bounds[0] + bounds[2]) // 2,
                            "y": (bounds[1] + bounds[3]) // 2,
                        }
                        is_truncated = True
                    
                    # Look for caption text (contains username followed by caption)
                    if "row_feed_comment_textview" in res_id or "IgTextLayoutView" in node_class:
                        if text and username.lower() in text.lower()[:50]:
                            # This is the caption container
                            caption_text = text
                            # Method 2: Caption ends with "more" or "… more"
                            if text.rstrip().endswith("more") or "… more" in text or "... more" in text:
                                is_truncated = True
                                # The "more" text position - tap at the end of the caption area
                                if not more_button:
                                    more_button = {
                                        "x": bounds[2] - 50,  # Right side where "more" appears
                                        "y": (bounds[1] + bounds[3]) // 2,
                                    }
                    
                    # Also check content-desc for caption fragments
                    if content_desc and len(content_desc) > 20:
                        if not content_desc.startswith("Profile picture") and not "button" in content_desc.lower():
                            if not caption_text:
                                caption_text = content_desc
                            # Also check content_desc for truncation
                            if content_desc.rstrip().endswith("more") or "… more" in content_desc:
                                is_truncated = True
                
                return {
                    "found": bool(caption_text),
                    "is_truncated": is_truncated,
                    "caption_text": caption_text,
                    "more_button": more_button,
                }
                
            except ET.ParseError:
                return {"found": False, "error": "xml_parse_error"}
        
        # Get initial XML
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {"found": False, "error": "invalid_xml"}
        
        initial_info = extract_caption_from_xml(xml_result["xml"], target_username)
        
        if not initial_info.get("found"):
            visible_users = initial_info.get("visible_users", [])
            return {
                "found": False,
                "visible_users": visible_users,
                "note": (
                    f"Caption for '{target_username}' not found on screen. "
                    f"Visible posts are by: {', '.join(visible_users[:3]) if visible_users else 'unknown'}. "
                    f"Scroll to find the post first."
                )
            }
        
        is_truncated = initial_info.get("is_truncated", False)
        more_button = initial_info.get("more_button")
        caption_text = initial_info.get("caption_text", "")
        expanded = False
        
        # Expand if requested and truncated
        if expand_if_truncated and is_truncated:
            if more_button:
                try:
                    # Tap "more" button
                    from lamda.client import Point as FPoint
                    logger.debug(f"Tapping 'more' button at ({more_button['x']}, {more_button['y']})")
                    dm.device.click(FPoint(x=more_button["x"], y=more_button["y"]))
                    dm.invalidate_xml_cache()  # Important! Screen changed
                    time.sleep(0.1)  # Wait for expansion animation
                    
                    # Re-read XML to get expanded caption
                    xml_result2 = get_screen_xml()
                    if xml_result2.get("valid"):
                        expanded_info = extract_caption_from_xml(xml_result2["xml"], target_username)
                        if expanded_info.get("found"):
                            caption_text = expanded_info.get("caption_text", caption_text)
                            expanded = True
                            logger.debug(f"Caption expanded successfully, new length: {len(caption_text)}")
                            # Check if still truncated (very long captions)
                            is_truncated = expanded_info.get("is_truncated", False)
                        else:
                            logger.warning(f"After expansion, caption not found for {target_username}")
                    else:
                        logger.warning("XML invalid after expansion attempt")
                except Exception as e:
                    # If expansion fails, log it and return what we have
                    logger.warning(f"Caption expansion failed: {e}")
            else:
                logger.warning(f"Caption truncated but no 'more' button found for {target_username}")
        
        # Detect CTAs and extract the exact keyword to comment
        cta_detected = False
        cta_keyword = None
        cta_instruction = None
        cta_type = None
        
        # Pattern 1: "Comment [WORD]" or "Type [WORD]" patterns
        # Matches: "comment TYPE", "Comment LOVE below", "type YES if you agree"
        import re
        comment_patterns = [
            r'(?:comment|type|write|say|drop|leave)\s+["\']?([A-Z]{2,}|[a-z]{2,}|\w+)["\']?(?:\s|$|!|\.)',
            r'(?:comment|type|write)\s+(?:a\s+)?["\']([^"\']+)["\']',  # "comment 'word'" format
            r'(?:comment|type)\s+(\S+)\s+(?:if|below|for)',  # "comment WORD if you..."
        ]
        
        for pattern in comment_patterns:
            match = re.search(pattern, caption_text, re.IGNORECASE)
            if match:
                keyword = match.group(1).strip()
                # Filter out common non-CTA words
                if keyword.lower() not in ['a', 'an', 'the', 'your', 'my', 'this', 'that', 'below', 'if', 'and', 'or']:
                    cta_detected = True
                    cta_keyword = keyword.upper() if keyword.isupper() or len(keyword) <= 4 else keyword
                    cta_instruction = match.group(0).strip()
                    cta_type = "COMMENT_WORD"
                    break
        
        # Pattern 2: "Type [EMOJI]" or emoji-based CTAs
        if not cta_detected:
            emoji_pattern = r'(?:comment|type|drop)\s+([🔥❤️💯✨👏💪🙏💕😍😎]+)'
            match = re.search(emoji_pattern, caption_text)
            if match:
                cta_detected = True
                cta_keyword = match.group(1)
                cta_instruction = match.group(0).strip()
                cta_type = "COMMENT_EMOJI"
        
        # Pattern 3: Other CTA types (DM, link, etc.)
        other_ctas = []
        caption_lower = caption_text.lower()
        if "dm " in caption_lower or "dm me" in caption_lower:
            other_ctas.append("DM")
        if "link in bio" in caption_lower or "link in profile" in caption_lower:
            other_ctas.append("LINK_IN_BIO")
        if "tag someone" in caption_lower or "tag a friend" in caption_lower:
            other_ctas.append("TAG_FRIEND")
        if "save this" in caption_lower or "save for later" in caption_lower:
            other_ctas.append("SAVE")
        
        result = {
            "found": True,
            "is_truncated": is_truncated,
            "was_truncated": initial_info.get("is_truncated", False),
            "caption_text": caption_text,
            "expanded": expanded,
            # CTA detection from text
            "cta_detected": cta_detected,
            "cta_keyword": cta_keyword,  # Exact word to comment (if applicable)
            "cta_instruction": cta_instruction,  # Original phrase from post
            "cta_type": cta_type,  # COMMENT_WORD, COMMENT_EMOJI, etc.
            "other_ctas": other_ctas,  # Non-comment CTAs (DM, LINK, etc.)
            "verified_user": target_username,
            **nurtured_info,  # Include nurtured status!
        }
        return result
    
    def get_visible_comments(target_username: str, max_comments: int = 7) -> dict:
        """Get visible comments from the Instagram comments screen.
        
        Call this AFTER opening the comments section (tap comment button).
        Extracts comment authors from the comments screen XML using
        profile image content-desc pattern: "Go to {username}'s profile".
        
        Args:
            target_username: Post author username (for filtering)
            max_comments: Maximum comments to return (default 7)
        
        Returns:
            dict with:
            - found: True if at least one comment was extracted
            - comments: list of {"username": str, "text": str}
            - count: number of comments returned
            - on_comments_screen: True if Comments header detected
            - verified_user: the target_username that was searched
        """
        if not target_username:
            return {"found": False, "error": "target_username is required", "comments": [], "count": 0}
        
        max_comments = min(max_comments, 20)
        
        import xml.etree.ElementTree as ET
        import re
        
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {
                "found": False, "comments": [], "count": 0,
                "error": "invalid_xml", "verified_user": target_username,
            }
        
        try:
            root = ET.fromstring(xml_result["xml"])
        except ET.ParseError:
            return {
                "found": False, "comments": [], "count": 0,
                "error": "xml_parse_error", "verified_user": target_username,
            }
        
        target_clean = target_username.lower().strip().lstrip("@")
        
        # Detect if we're on the comments screen
        on_comments_screen = False
        for node in root.iter("node"):
            if node.get("text", "").strip() == "Comments":
                rid = node.get("resource-id", "")
                if "title_text_view" in rid or node.get("class", "") == "android.widget.TextView":
                    on_comments_screen = True
                    break
        
        comments: list[dict[str, str]] = []
        seen_usernames: set[str] = set()
        
        # Primary method: extract from "Go to {username}'s profile" ImageView pattern
        profile_pattern = re.compile(r"Go to (.+?)(?:'s|'s) profile", re.IGNORECASE)
        
        for node in root.iter("node"):
            desc = node.get("content-desc", "")
            m = profile_pattern.search(desc)
            if not m:
                continue
            
            comment_username = m.group(1).strip()
            comment_clean = comment_username.lower().strip()
            
            # Skip post author (appears as first profile image)
            if comment_clean == target_clean:
                continue
            
            # Skip duplicates (same user may appear multiple times)
            if comment_clean in seen_usernames:
                continue
            seen_usernames.add(comment_clean)
            
            # Try to extract comment text from sibling ViewGroup
            comment_text = ""
            parent = None
            for potential_parent in root.iter("node"):
                for child in potential_parent:
                    if child is node:
                        parent = potential_parent
                        break
                if parent:
                    break
            
            if parent is not None:
                for sibling in parent:
                    sib_text = sibling.get("text", "").strip()
                    if sib_text and sib_text.lower().startswith(comment_clean):
                        # Format: "username comment_text" — extract text after username
                        remainder = sib_text[len(comment_username):].strip()
                        if remainder:
                            comment_text = remainder
                        break
            
            comments.append({
                "username": comment_username,
                "text": comment_text,
            })
            
            if len(comments) >= max_comments:
                break
        
        # Fallback: if no profile images found, try Button elements with Reply sibling
        if not comments:
            for node in root.iter("node"):
                if node.get("text", "").strip() == "Reply" and node.get("class", "") == "android.widget.Button":
                    parent = None
                    for p in root.iter("node"):
                        if node in list(p):
                            parent = p
                            break
                    if parent is None:
                        continue
                    # Look for username Button in same parent
                    for sibling in parent:
                        sib_text = sibling.get("text", "").strip()
                        if (sib_text and sib_text != "Reply" and sib_text != "more"
                                and sibling.get("class", "") == "android.widget.Button"
                                and sib_text.lower() != target_clean):
                            if sib_text.lower() not in seen_usernames:
                                seen_usernames.add(sib_text.lower())
                                comments.append({"username": sib_text, "text": ""})
                                if len(comments) >= max_comments:
                                    break
        
        result = {
            "found": len(comments) > 0,
            "comments": comments,
            "count": len(comments),
            "on_comments_screen": on_comments_screen,
            "verified_user": target_username,
        }
        
        logger.debug(
            f"get_visible_comments('{target_username}'): "
            f"found={result['found']}, count={result['count']}, "
            f"on_comments_screen={on_comments_screen}"
        )
        
        return result
    
    def get_elements_for_ai(max_elements: int = 40) -> dict:
        """Get UI elements formatted for AI decision making.
        
        Use when find_element() doesn't work - provides list of all
        visible elements for AI to choose from.
        
        Args:
            max_elements: Maximum elements to return (default 40)
            
        Returns:
            dict with list of elements and their properties
        """
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {"elements": [], "error": "invalid_xml"}
        
        finder = create_finder(xml_result["xml"])
        elements = finder.get_elements_for_ai(max_elements=max_elements)
        
        return {
            "elements": elements,
            "count": len(elements),
            "screen_context": xml_result.get("screen_context", "unknown"),
        }
    
    def open_instagram() -> dict:
        """Launch Instagram app.
        
        Use when need to return to Instagram from home screen or other app.
        Uses FIRERPA application API for reliable app launch.
        Includes automatic reconnect on connection errors.
        
        Returns:
            dict with success status
        """
        from lamda.const import FLAG_ACTIVITY_NEW_TASK, FLAG_ACTIVITY_CLEAR_TOP
        
        def _do_launch():
            # Use application API (correct FIRERPA method)
            app = dm.device.application("com.instagram.android")
            
            # Check if already in foreground
            if app.is_foreground():
                # ALWAYS check actual screen context, don't assume feed!
                xml_result = get_screen_xml()
                return {
                    "launched": True,
                    "in_instagram": True,
                    "screen_context": xml_result.get("screen_context", "instagram_unknown"),
                    "note": "Already in foreground",
                }
            
            # Method 1: Try start_activity with launcher intent (most reliable)
            try:
                dm.device.start_activity(
                    action="android.intent.action.MAIN",
                    category="android.intent.category.LAUNCHER",
                    component="com.instagram.android/com.instagram.mainactivity.LauncherActivity",
                    flags=FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_CLEAR_TOP,
                )
            except Exception:
                # Fallback: use app.start() if activity launch fails
                app.start()
            
            # Wait for app to be ready
            dm.device.wait_for_idle(timeout=800)
            
            # OPTIMIZED: First check foreground (fast, no XML), then ONE XML check
            # This reduces 12 XML dumps to 1-2
            if app.is_foreground():
                # App is definitely running - do ONE XML check for screen context
                xml_result = get_screen_xml()
                return {
                    "launched": True,
                    "in_instagram": True,
                    "screen_context": xml_result.get("screen_context", "instagram_unknown"),
                }
            
            # App not in foreground yet - wait a bit and retry foreground check
            time.sleep(0.25)
            if app.is_foreground():
                xml_result = get_screen_xml()
                return {
                    "launched": True,
                    "in_instagram": True,
                    "screen_context": xml_result.get("screen_context", "instagram_unknown"),
                }
            
            return {
                "launched": False,
                "in_instagram": False,
                "screen_context": xml_result.get("screen_context", "unknown"),
            }
        
        try:
            result = dm.with_reconnect(_do_launch)
            ctx = result.get("screen_context", "instagram_unknown")
            _set_nav(ctx, 0)
            result["_nav_hint"] = _get_nav_hint()
            return result
        except Exception as e:
            return {
                "launched": False,
                "error": str(e),
                "_nav_hint": _get_nav_hint(),
            }
    
    def force_close_instagram() -> dict:
        """Force close (kill) Instagram app.
        
        Use as last resort recovery when app is stuck/frozen.
        This completely terminates the app process, not just minimizes it.
        
        After calling this, you MUST call open_instagram() to restart.
        
        Returns:
            dict with success status
        """
        try:
            app = dm.device.application("com.instagram.android")
            app.stop()  # Force kill the app
            dm.invalidate_xml_cache()  # App killed - screen completely changed
            time.sleep(0.1)  # Brief pause for process to terminate
            return {
                "closed": True,
                "note": "Instagram force closed. Call open_instagram() to restart.",
            }
        except Exception as e:
            return {
                "closed": False,
                "error": str(e),
            }
    
    def restart_instagram() -> dict:
        """Force close and reopen Instagram app.
        
        Use as last resort recovery when app is completely stuck.
        This kills the app and restarts it fresh.
        
        Returns:
            dict with success status and screen context
        """
        try:
            app = dm.device.application("com.instagram.android")
            
            # Force close
            app.stop()
            time.sleep(0.3)  # Wait for process to fully terminate
            
            # Reopen
            app.start()
            dm.device.wait_for_idle(timeout=800)
            
            # Verify we're back in Instagram
            xml_result = get_screen_xml()
            in_instagram = xml_result.get("in_instagram", False)
            
            return {
                "restarted": True,
                "in_instagram": in_instagram,
                "screen_context": xml_result.get("screen_context", "unknown"),
            }
        except Exception as e:
            return {
                "restarted": False,
                "error": str(e),
            }
    
    def handle_dialog() -> dict:
        """Detect and handle system/app dialogs.
        
        Automatically handles common dialogs:
        - Permission requests (notifications, storage)
        - App not responding
        - Instagram popups (turn on notifications, save login, etc.)
        
        Returns:
            dict with handling result
        """
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {"handled": False, "error": "invalid_xml"}
        
        def tap_at(x: int, y: int) -> bool:
            tap(x, y)
            return True
        
        def press_back_func() -> bool:
            press_back()
            return True
        
        handler = create_dialog_handler(tap_at, press_back_func)
        handled, dialog_type = handler.handle(xml_result["xml"])
        
        return {
            "handled": handled,
            "dialog_type": dialog_type.value if dialog_type else None,
        }
    
    def escape_to_instagram() -> dict:
        """Execute escape workflow to return to Instagram.
        
        Use when agent is lost (in system UI, other app, etc.).
        Automatically executes recovery steps.
        
        Returns:
            dict with escape result
        """
        def get_xml_func() -> str:
            result = get_screen_xml()
            return result.get("xml", "")
        
        def press_back_func() -> bool:
            press_back()
            return True
        
        def press_home_func() -> bool:
            press_home()
            return True
        
        def open_instagram_func() -> bool:
            result = open_instagram()
            return result.get("launched", False)
        
        def swipe_up_to_close_shade() -> bool:
            # Swipe UP to close notification shade (finger moves from middle/bottom to top)
            from lamda.client import Point as FPoint
            start = FPoint(x=dm.screen_width // 2, y=dm.screen_height // 2)
            end = FPoint(x=dm.screen_width // 2, y=100)
            dm.device.swipe(start, end, step=20)
            dm.invalidate_xml_cache()  # Screen changed after swipe
            return True
        
        def restart_instagram_func() -> bool:
            # Force close and reopen Instagram (last resort recovery)
            result = restart_instagram()
            return result.get("restarted", False)
        
        workflows = create_escape_workflows(
            get_xml_func=get_xml_func,
            press_back_func=press_back_func,
            press_home_func=press_home_func,
            open_instagram_func=open_instagram_func,
            swipe_down_func=swipe_up_to_close_shade,
            restart_instagram_func=restart_instagram_func,
        )
        
        result = workflows.escape_to_instagram()
        
        return {
            "success": result.success,
            "final_screen": result.final_screen.value,
            "steps_executed": result.steps_executed,
            "error": result.error_message,
        }
    
    # -------------------------------------------------------------------------
    # Basic Touch Tools
    # -------------------------------------------------------------------------
    
    def tap(x: int, y: int) -> dict:
        """Tap at specific screen coordinates.
        
        Args:
            x: X coordinate (0 = left edge)
            y: Y coordinate (0 = top edge)
            
        Returns:
            dict confirming tap location
        """
        from lamda.client import Point as FPoint
        
        dm.device.click(FPoint(x=x, y=y))
        dm.invalidate_xml_cache()  # Screen may have changed
        return {"tapped": True, "x": x, "y": y}
    
    def long_press(x: int, y: int, duration_ms: int = 1000) -> dict:
        """Long press at coordinates.
        
        Args:
            x: X coordinate
            y: Y coordinate
            duration_ms: Hold duration in milliseconds (default 1000ms)
            
        Returns:
            dict confirming action
        """
        # For direct coordinate long press, we use drag from point to same point
        from lamda.client import Point as FPoint
        
        point = FPoint(x=x, y=y)
        # Drag to same point with high step = slow = long press effect
        step = max(50, duration_ms // 20)  # Higher step = slower = longer
        dm.device.drag(point, point, step=step)
        dm.invalidate_xml_cache()  # Screen may have changed
        
        return {"long_pressed": True, "x": x, "y": y, "duration_ms": duration_ms}
    
    def double_tap_like() -> dict:
        """Double-tap to like current post (human-like gesture).
        
        Auto-detects post image container and taps in center with 30% padding.
        Uses shell command for precise 33-78ms timing between taps.
        
        IMPORTANT: Must be on Feed screen with a post visible!
        Does NOT work in Reels mode - use press_back() first if in Reels.
        
        Returns:
            dict with double_tapped, position, delay_ms, method
            If post container not found: error with visible_containers list
        """
        import re
        
        # ALWAYS auto-detect bounds from XML (ignore any user-provided bounds)
        # LLM sometimes hallucinates wrong bounds, so we force auto-detect
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {"error": "Cannot get XML to detect post image bounds"}
        
        xml_str = xml_result["xml"]
        
        # Look for post image containers (priority order)
        # Updated based on XML dump analysis
        image_ids = [
            "com.instagram.android:id/carousel_media_group",       # Carousel (outer)
            "com.instagram.android:id/carousel_video_media_group", # Video in carousel
            "com.instagram.android:id/zoomable_view_container",    # Single image
            "com.instagram.android:id/media_group",                # Generic media (fallback)
        ]
        
        bounds = None
        found_container = None
        for img_id in image_ids:
            pattern = rf'resource-id="{img_id}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
            match = re.search(pattern, xml_str)
            if match:
                bounds = tuple(int(g) for g in match.groups())
                found_container = img_id.split("/")[-1]  # Just the ID part
                break
        
        if not bounds:
            # Return error with debug info instead of using fallback
            # Fallback was causing issues - better to fail explicitly
            return {
                "error": "No post image container found on screen",
                "hint": "Make sure a post is visible in Feed (not Reels/Stories)",
                "searched_ids": [i.split("/")[-1] for i in image_ids],
            }
        
        # Execute double-tap via SimpleGestures
        result = dm.gestures.double_tap_like(bounds)
        dm.invalidate_xml_cache()  # Like status may have changed
        
        # Reset scroll tracker (engagement happened)
        reset_scroll_tracker()
        
        # Add debug info
        result["container"] = found_container
        result["bounds"] = bounds
        
        return result
    
    # -------------------------------------------------------------------------
    # Human-like Scroll Tools (using SimpleGestures - tested and working!)
    # -------------------------------------------------------------------------
    
    def scroll_feed(mode: str = "normal") -> dict:
        """Execute single human-like scroll gesture.
        
        Use for Instagram feed browsing when you want to see next post.
        Uses SimpleGestures with real human gesture statistics.
        
        Args:
            mode: "normal", "fast", or "slow"
            
        Returns:
            dict with scroll direction and status
        """
        scrolled_down = dm.gestures.scroll_feed(mode=mode)
        dm.invalidate_xml_cache()  # Screen content changed
        
        # Track consecutive scrolls
        scroll_info = _increment_scroll_tracker()
        
        result = {
            "executed": True,
            "direction": "down" if scrolled_down else "up (scroll back)",
            "mode": mode,
            "_nav_hint": _get_nav_hint(),
        }
        
        # Add warning if too many scrolls without engagement
        if "warning" in scroll_info:
            result["warning"] = scroll_info["warning"]
            result["scroll_streak"] = scroll_info["scroll_streak"]
            if scroll_info.get("should_escape"):
                logger.warning(
                    f"🔄 {scroll_info['scroll_streak']} scrolls without engagement — "
                    "force-restarting Instagram to reset state"
                )
                reset_scroll_tracker()
                try:
                    app = dm.device.application("com.instagram.android")
                    app.stop()
                    time.sleep(0.3)
                    app.start()
                    dm.device.wait_for_idle(timeout=2000)
                    dm.invalidate_xml_cache()
                except Exception as e:
                    logger.error(f"Scroll-escape restart failed: {e}")
                result["auto_restarted"] = True
                result["reason"] = (
                    "Too many scrolls without engagement. Instagram was RESTARTED. "
                    "Call analyze_feed_posts() to continue from fresh feed."
                )
        
        return result
    
    def scroll_fast() -> dict:
        """Execute fast fling gesture (human-like quick flick).
        
        Use when you want to quickly scroll:
        - Skipping ads or sponsored posts
        - Fast browsing
        - When instructed to "scroll fast"
        
        Returns:
            dict with execution status
        """
        result = dm.gestures.scroll_fast()
        dm.invalidate_xml_cache()  # Screen content changed
        
        # Track consecutive scrolls
        scroll_info = _increment_scroll_tracker()
        
        response = {
            "executed": True,
            "mode": "fast_fling",
            "success": result,
        }
        
        if "warning" in scroll_info:
            response["warning"] = scroll_info["warning"]
            response["scroll_streak"] = scroll_info["scroll_streak"]
        
        return response
    
    def scroll_burst(count: int = 3) -> dict:
        """Execute multiple fast scroll gestures WITHOUT analyzing content between.
        
        Use when you want to quickly skip through multiple posts:
        - Skipping ads or sponsored posts
        - Quickly scrolling past uninteresting content
        - When instructed to "skip ahead"
        
        This executes all gestures in sequence without LLM processing between.
        
        Args:
            count: Number of scrolls (2-5 recommended)
            
        Returns:
            dict with execution summary
        """
        count = max(2, min(5, count))  # clamp to safe range
        actual_count = dm.gestures.scroll_burst(count=count)
        dm.invalidate_xml_cache()  # Screen content changed
        
        # Track consecutive scrolls (increment by actual count)
        scroll_info = {"scroll_streak": 0}
        for _ in range(actual_count):
            scroll_info = _increment_scroll_tracker()
        
        response = {
            "executed": True,
            "mode": "burst",
            "gestures_count": actual_count,
        }
        
        if "warning" in scroll_info:
            response["warning"] = scroll_info["warning"]
            response["scroll_streak"] = scroll_info["scroll_streak"]
        
        return response
    
    def scroll_slow_browse() -> dict:
        """Execute slow scroll gesture for reading mode.
        
        Use when carefully browsing content:
        - Reading captions or comments
        - Looking for specific content
        - "Engage mode" or "browse slowly" instructions
        
        Returns:
            dict with execution status
        """
        dm.gestures.scroll_slow_browse()
        dm.invalidate_xml_cache()  # Screen content changed
        
        # Track consecutive scrolls
        scroll_info = _increment_scroll_tracker()
        
        response = {
            "executed": True,
            "mode": "slow_browse",
        }
        
        if "warning" in scroll_info:
            response["warning"] = scroll_info["warning"]
            response["scroll_streak"] = scroll_info["scroll_streak"]
        
        return response
    
    def watch_media(media_type: str = "photo") -> dict:
        """Wait appropriate duration for media type (simulates viewing content).
        
        Watch times minimal - agent processing (3-8s) provides natural delay:
        - photo: 0.5-1.5 seconds (agent adds 3-8s on top)
        - video: 5-13 seconds (view counts after 5s, agent adds overhead)
        - carousel: 0.5-1.5 seconds (agent adds 3-8s on top)
        
        Args:
            media_type: "photo", "video", or "carousel"
            
        Returns:
            dict with duration watched in seconds
        """
        result = dm.gestures.watch_media(media_type)
        # No cache invalidation needed - just waiting
        return result
    
    def scroll_back(slow: bool = False) -> dict:
        """Scroll back up to see previous content.
        
        ⚠️ DANGER: This can trigger pull-to-refresh if already near top!
        
        Use ONLY when:
        - User explicitly asked to go back
        - You just scrolled and want to undo ONE scroll
        
        ⛔ NEVER use when:
        - scroll_to_post_buttons() returned header_visible=false (post is LOST)
        - Trying to find a post you scrolled past (it's GONE)
        - You've already called scroll_back and it returned at_top_of_feed=true
        
        Args:
            slow: If True, use normal-intensity swipe instead of fast fling
            
        Returns:
            dict with:
            - executed: True
            - visible_posts: List of usernames whose posts are now visible
            - at_top_of_feed: True if we're at the very top (DANGER: refresh triggered!)
            - feed_may_have_refreshed: True if refresh likely happened
            - previous_posts_lost: True if any earlier posts are now gone
        """
        import xml.etree.ElementTree as ET
        import re
        
        result = dm.gestures.scroll_back(slow=slow)
        dm.invalidate_xml_cache()  # Screen content changed
        time.sleep(0.15)  # Wait for scroll animation
        
        # Check what posts are now visible
        visible_posts = []
        at_top = False
        
        try:
            xml_result = get_screen_xml()
            if xml_result.get("valid"):
                root = ET.fromstring(xml_result["xml"])
                
                # Find post headers
                for node in root.iter("node"):
                    res_id = node.get("resource-id", "")
                    if "row_feed_photo_profile_name" in res_id or "row_feed_profile_header" in res_id:
                        text = node.get("text", "").strip()
                        content_desc = node.get("content-desc", "")
                        username = text.split()[0] if text else content_desc.split()[0] if content_desc else ""
                        if username and username not in visible_posts:
                            visible_posts.append(username)
                
                # Check for "New Posts" indicator or top-of-feed elements
                for node in root.iter("node"):
                    text = node.get("text", "").lower()
                    content_desc = node.get("content-desc", "").lower()
                    if "new posts" in text or "new posts" in content_desc:
                        at_top = True
                        break
        except Exception:
            pass  # Don't fail if XML parsing fails
        
        # Track consecutive scrolls (scroll_back counts too)
        scroll_info = _increment_scroll_tracker()
        
        # Build response with strong warnings if at top
        if at_top:
            response = {
                "executed": True,
                "mode": "slow" if slow else "fast",
                "direction": "up",
                "success": result,
                "visible_posts": visible_posts[:5],
                "at_top_of_feed": True,
                "feed_may_have_refreshed": True,  # Explicit flag
                "previous_posts_lost": True,  # Explicit flag
                "do_not_scroll_back_again": True,  # Prevent loop
                "action_required": "CONTINUE_SCROLLING_DOWN",
                "note": (
                    "⚠️ REACHED TOP OF FEED - Pull-to-refresh may have triggered! "
                    "⛔ DO NOT scroll_back() again - you'll just refresh more. "
                    "⛔ DO NOT try to find previous posts - they are GONE. "
                    "✅ Continue scrolling DOWN with scroll_feed() to see new content."
                ),
            }
            if "warning" in scroll_info:
                response["warning"] = scroll_info["warning"]
                response["scroll_streak"] = scroll_info["scroll_streak"]
            return response
        
        response = {
            "executed": True,
            "mode": "slow" if slow else "fast",
            "direction": "up",
            "success": result,
            "visible_posts": visible_posts[:5],  # First 5 visible
            "at_top_of_feed": False,
            "feed_may_have_refreshed": False,
            "previous_posts_lost": False,
        }
        
        if "warning" in scroll_info:
            response["warning"] = scroll_info["warning"]
            response["scroll_streak"] = scroll_info["scroll_streak"]
        
        return response
    
    # -------------------------------------------------------------------------
    # Story Watching (GramAddict pattern)
    # -------------------------------------------------------------------------
    
    def watch_stories(
        username: str = "",
        max_stories: int = 2,
        like_probability: float = 0.0,
    ) -> dict:
        """Watch user's stories with human-like timing.
        
        GramAddict approach for natural story viewing:
        - 3.5-7 seconds per story (7 iterations x 0.5-1s)
        - Tap right edge to advance
        - Optional like with configurable probability
        
        WORKFLOW:
        1. Must already be viewing stories (tap story ring first)
        2. Watches up to max_stories
        3. Tap right to advance or press back to exit
        
        Args:
            username: Expected story author (for verification, optional)
            max_stories: Max stories to watch (default 2)
            like_probability: Chance to like each story (0.0-1.0, default 0)
            
        Returns:
            dict with stories_watched count and liked count
        """
        import re
        import xml.etree.ElementTree as ET
        stories_watched = 0
        stories_liked = 0
        
        for story_num in range(max_stories):
            # Check we're still in story viewer
            xml_result = get_screen_xml()
            if not xml_result.get("valid"):
                break
            
            xml_str = xml_result["xml"]
            
            # Look for story viewer indicators (NOT stories_tray - that's on home feed)
            in_story_viewer = any(indicator in xml_str for indicator in [
                "reel_viewer_container",
                "reel_viewer_media_container",
                "story_viewer_container",
                "reel_viewer_progress",  # Progress bar at top of stories
            ])
            
            if not in_story_viewer:
                # Exited stories, stop watching
                break
            
            # Verify username if provided
            if username:
                username_match = re.search(
                    r'resource-id="[^"]*username[^"]*"[^>]*text="([^"]+)"',
                    xml_str,
                    re.IGNORECASE
                )
                if username_match and username.lower() not in username_match.group(1).lower():
                    # Different user's story, stop
                    break
            
            # Watch story (GramAddict: 7 iterations x 0.5-1s = 3.5-7s)
            for _ in range(7):
                time.sleep(random.uniform(0.5, 1.0))
            
            stories_watched += 1
            
            # Maybe like the story
            if random.random() < like_probability:
                # Find and tap like button
                like_match = re.search(
                    r'resource-id="[^"]*like[^"]*button[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    xml_str,
                    re.IGNORECASE
                )
                if like_match:
                    x = (int(like_match.group(1)) + int(like_match.group(3))) // 2
                    y = (int(like_match.group(2)) + int(like_match.group(4))) // 2
                    from lamda.client import Point as FPoint
                    dm.device.click(FPoint(x=x, y=y))
                    stories_liked += 1
                    # No artificial delay - agent processing provides natural pause
            
            # Advance to next story (tap right edge)
            if story_num < max_stories - 1:
                dm.gestures.tap_right_edge(y_fraction=0.5)
                # No artificial delay - agent processing provides natural pause
        
        dm.invalidate_xml_cache()
        
        # Reset scroll tracker (engagement happened)
        reset_scroll_tracker()
        
        return {
            "stories_watched": stories_watched,
            "stories_liked": stories_liked,
            "username": username or "unknown",
        }
    
    def refresh_feed() -> dict:
        """Pull-to-refresh Instagram feed.
        
        Use ONLY when:
        - You are at the TOP of the feed
        - User explicitly asks to refresh
        - Need to load new content
        
        This is a slow downward pull gesture that triggers refresh.
        DO NOT use for normal scrolling!
        
        Returns:
            dict with execution status
        """
        dm.gestures.pull_to_refresh()
        dm.invalidate_xml_cache()  # Screen content changed after refresh
        
        return {
            "executed": True,
            "action": "pull_to_refresh",
            "note": "Wait for content to load after refresh",
        }
    
    def scroll_to_post_buttons(target_username: str, max_scrolls: int = 2) -> dict:
        """Scroll incrementally to bring engagement buttons into view.
        
        Uses 1-2 small scrolls to reveal engagement buttons (like, comment, share, save).
        Each scroll is ~400-600px with natural human-like variability.
        
        NOTE: One account can have MULTIPLE posts in the feed. This function finds
        buttons for the FIRST visible post by this username. If you need buttons
        for a different post by the same user, scroll first to bring that post
        into view, then call this function.
        
        Args:
            target_username: Post author username (case insensitive)
            max_scrolls: Maximum scroll attempts (default 2)
            
        Returns:
            dict with:
            - success: True if buttons now visible
            - scrolls_done: Number of scrolls executed  
            - buttons_visible: True if buttons are visible
            - buttons_y: Y coordinate of buttons (for tap targeting)
            - header_visible: True if post header still on screen
        """
        import re
        
        target_username_lower = target_username.lower().strip()
        
        def parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
            match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
            if match:
                return tuple(int(x) for x in match.groups())
            return None
        
        def check_buttons_visible(username_lower: str, last_header_y: int | None = None) -> tuple[bool, int | None, bool, list, int | None]:
            """Check if engagement buttons for target post are visible.
            
            Uses multiple anchors to identify post ownership:
            1. Header (row_feed_photo_profile_name) - username at top
            2. Caption below buttons - always starts with "username текст..."
            3. Caption has Button with content-desc="username"
            
            Args:
                username_lower: Target username in lowercase
                last_header_y: Header Y position from previous check (fallback if header scrolled off)
            
            Returns: (buttons_visible, buttons_y, header_found, visible_posts, header_y)
            """
            xml_result = get_screen_xml()
            if not xml_result.get("valid"):
                return False, None, False, [], None
            
            try:
                root = ET.fromstring(xml_result["xml"])
            except ET.ParseError:
                return False, None, False, [], None
            
            VISIBLE_TOP = 77
            VISIBLE_BOTTOM = dm.screen_height - 260
            
            # Collect ALL visible post headers
            visible_posts = []
            header_y = None
            header_found = False
            
            # Collect captions (text starting with username)
            # Format: "username caption text..." or Button content-desc="username"
            caption_authors = []  # list of (username, y_position)
            
            for node in root.iter("node"):
                res_id = node.get("resource-id", "")
                text = node.get("text", "").strip()
                content_desc = node.get("content-desc", "")
                node_class = node.get("class", "")
                
                # 1. Check headers
                if "row_feed_photo_profile_name" in res_id or "row_feed_profile_header" in res_id:
                    node_text = text or content_desc
                    username = node_text.split()[0] if node_text else ""
                    if username:
                        visible_posts.append(username.lower().strip())
                        if username_lower in node_text.lower():
                            bounds = parse_bounds(node.get("bounds", ""))
                            if bounds:
                                header_y = bounds[1]
                                header_found = True
                
                # 2. Check captions - text starts with "username ..."
                # Caption is IgTextLayoutView or TextView with text like "username caption..."
                if "IgTextLayoutView" in node_class or (node_class == "android.widget.TextView" and text):
                    if text and " " in text:
                        caption_user = text.split()[0].lower().strip()
                        # Remove trailing punctuation
                        caption_user = caption_user.rstrip(".,!?:;")
                        bounds = parse_bounds(node.get("bounds", ""))
                        if bounds and VISIBLE_TOP < bounds[1] < VISIBLE_BOTTOM:
                            caption_authors.append((caption_user, bounds[1]))
                
                # 3. Check caption Buttons with content-desc="username"
                # These are inside caption and have exact username
                if node_class == "android.widget.Button" and content_desc:
                    # Caption button has just username as content-desc
                    btn_user = content_desc.lower().strip()
                    # Should be simple username (no spaces, not a description)
                    if btn_user and " " not in btn_user and len(btn_user) < 50:
                        bounds = parse_bounds(node.get("bounds", ""))
                        if bounds and VISIBLE_TOP < bounds[1] < VISIBLE_BOTTOM:
                            caption_authors.append((btn_user, bounds[1]))
            
            # Find engagement buttons in visible area
            button_positions = []
            
            for node in root.iter("node"):
                res_id = node.get("resource-id", "")
                content_desc = node.get("content-desc", "")
                
                # Check by resource-id (old Instagram) OR content-desc (new Instagram)
                is_button_by_id = "row_feed_view_group_buttons" in res_id or "row_feed_button_like" in res_id
                is_button_by_desc = content_desc in ("Like", "Liked", "Comment", "Send Post", "Add to Saved", "Remove from Saved")
                
                if is_button_by_id or is_button_by_desc:
                    bounds = parse_bounds(node.get("bounds", ""))
                    if bounds:
                        y_center = (bounds[1] + bounds[3]) // 2
                        if VISIBLE_TOP < y_center < VISIBLE_BOTTOM:
                            button_positions.append(y_center)
            
            if not button_positions:
                return False, None, header_found, visible_posts, header_y
            
            # Strategy 1: Header visible - find buttons below it
            if header_y is not None:
                buttons_below = [y for y in button_positions if y > header_y]
                if buttons_below:
                    return True, min(buttons_below), True, visible_posts, header_y
            
            # Strategy 2: Header scrolled off - use caption to verify post ownership
            # Caption is BELOW buttons, so find buttons with matching caption below them
            if caption_authors:
                for btn_y in sorted(button_positions):
                    # Find captions below this button position
                    captions_below = [(u, y) for u, y in caption_authors if y > btn_y]
                    for caption_user, caption_y in captions_below:
                        # Check if caption belongs to target user
                        if username_lower in caption_user or caption_user in username_lower:
                            # Found our post! Buttons are at btn_y, caption confirms ownership
                            logger.info(f"📍 Found buttons for @{username_lower} via caption anchor (y={btn_y})")
                            return True, btn_y, False, visible_posts, None
            
            # Strategy 3: Fallback - if we had header before and buttons exist, assume they're ours
            if last_header_y is not None:
                # Take topmost buttons (most likely our post that scrolled up)
                return True, min(button_positions), False, visible_posts, None
            
            return False, None, header_found, visible_posts, header_y
        
        # First check - maybe buttons already visible
        visible, buttons_y, header_found, visible_posts, last_header_y = check_buttons_visible(target_username_lower)
        if visible:
            return {
                "success": True,
                "buttons_visible": True,
                "buttons_y": buttons_y,
                "header_visible": header_found,
                "scrolls_done": 0,
                "note": "Buttons already visible - no scroll needed"
            }
        
        # If post header not even found, post is not on screen
        if not header_found:
            return {
                "success": False,
                "error": "POST_NOT_FOUND",  # Machine-readable error type
                "error_is_permanent": True,  # Cannot be recovered without scrolling
                "buttons_visible": False,
                "header_visible": False,
                "scrolls_done": 0,
                "visible_posts": visible_posts[:5],
                "action_required": "SKIP_TO_NEXT_POST",
                "do_not_scroll_back": True,  # Explicit flag
                "note": (
                    f"⚠️ POST NOT FOUND: '{target_username}' is NOT on screen. "
                    f"Currently visible: {', '.join(visible_posts[:3]) if visible_posts else 'none'}. "
                    f"⛔ DO NOT scroll_back() - it won't help and may trigger refresh. "
                    f"✅ SKIP this post and continue with scroll_feed()."
                )
            }
        
        # Do incremental scrolls (max 2)
        scrolls_done = 0
        
        for i in range(max_scrolls):
            # Smaller scroll for first attempt, then normal
            # This reduces chance of scrolling past the post
            if i == 0:
                scroll_distance = random.randint(200, 350)  # Gentle first scroll
            else:
                scroll_distance = random.randint(350, 500)  # Normal subsequent
            dm.gestures.scroll_precise(scroll_distance, variability_percent=15)
            dm.invalidate_xml_cache()  # Screen content changed
            scrolls_done += 1
            
            # No artificial delay - agent processing provides natural pause
            
            # Check if buttons now visible - pass last_header_y for fallback
            visible, buttons_y, header_found, visible_posts, new_header_y = check_buttons_visible(
                target_username_lower, last_header_y=last_header_y
            )
            
            # Update last_header_y if we got a new position
            if new_header_y is not None:
                last_header_y = new_header_y
            
            if visible:
                return {
                    "success": True,
                    "buttons_visible": True,
                    "buttons_y": buttons_y,
                    "header_visible": header_found,
                    "scrolls_done": scrolls_done,
                    "note": f"Buttons visible after {scrolls_done} scroll(s)" + (" (header scrolled off)" if not header_found else "")
                }
            
            # If header disappeared AND no buttons found, we scrolled past the post
            if not header_found and not visible:
                return {
                    "success": False,
                    "error": "POST_LOST",  # Machine-readable error type
                    "error_is_permanent": True,  # Cannot be recovered
                    "buttons_visible": False,
                    "header_visible": False,
                    "scrolls_done": scrolls_done,
                    "visible_posts": visible_posts[:5],
                    "action_required": "SKIP_TO_NEXT_POST",
                    "do_not_scroll_back": True,  # Explicit flag
                    "note": (
                        f"⚠️ POST LOST: '{target_username}' scrolled off screen. "
                        f"Now visible: {', '.join(visible_posts[:3]) if visible_posts else 'none'}. "
                        f"⛔ DO NOT scroll_back() - it triggers pull-to-refresh and loses MORE posts. "
                        f"✅ Just call scroll_feed() to continue to the next post."
                    )
                }
        
        # After max_scrolls, buttons still not visible but header still there
        # Post might be very tall (large image/video) - one more small scroll usually helps
        return {
            "success": False,
            "buttons_visible": False,
            "header_visible": header_found,
            "scrolls_done": scrolls_done,
            "visible_posts": visible_posts[:5],
            "note": (
                f"Buttons not visible after {scrolls_done} scroll(s) but '{target_username}' header still there. "
                f"Post is tall - try get_post_engagement_buttons() directly or scroll_feed('slow')."
            )
        }
    
    # -------------------------------------------------------------------------
    # Post Type Detection
    # -------------------------------------------------------------------------
    
    def detect_post_type(target_username: str) -> dict:
        """Detect the type of post (photo, video, or carousel).
        
        Use this to determine how to interact with a post:
        - photo: Quick view, double-tap to like
        - video: Watch 5-13 seconds, then like (for nurtured)
        - carousel: Swipe through 2-5 pages, then like (for nurtured)
        
        AUTO-ENRICHED: Automatically checks nurtured status.
        
        Args:
            target_username: Post author username
            
        Returns:
            dict with:
            - post_type: "video", "carousel", or "photo"
            - is_nurtured: True if VIP account
            - nurtured_priority: Priority level if nurtured
            - indicators: List of detected indicators
        """
        import re
        
        # Check nurtured status first
        nurtured_info = {}
        try:
            nurtured_result = is_nurtured_account(target_username)
            nurtured_info = {
                "is_nurtured": nurtured_result.get("is_nurtured", False),
                "nurtured_priority": nurtured_result.get("priority"),
            }
        except Exception:
            nurtured_info = {"is_nurtured": False}
        
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {"post_type": "photo", "error": "invalid_xml", **nurtured_info}
        
        try:
            root = ET.fromstring(xml_result["xml"])
        except ET.ParseError:
            return {"post_type": "photo", "error": "xml_parse_error", **nurtured_info}
        
        video_indicators = []
        carousel_indicators = []
        
        for node in root.iter("node"):
            res_id = node.get("resource-id", "")
            content_desc = node.get("content-desc", "")
            
            # Video indicators
            if "video_container" in res_id:
                video_indicators.append("video_container")
            if "video_states" in res_id:
                video_indicators.append("video_states")
            if "Turn sound" in content_desc:
                video_indicators.append("sound_button")
            if "posted a video" in content_desc.lower():
                video_indicators.append("header_video")
            
            # Carousel indicators (HIGHER PRIORITY than video!)
            if "carousel" in res_id and "media_group" in res_id:
                carousel_indicators.append("carousel_media_group")
            if "carousel_page_indicator" in res_id or "page_indicator" in res_id:
                carousel_indicators.append("page_indicator")
            if re.search(r"\d+\s+of\s+\d+", content_desc):
                carousel_indicators.append("x_of_y_pattern")
            # Header text like "posted a carousel"
            if "posted a carousel" in content_desc.lower():
                carousel_indicators.append("header_carousel")
        
        # Determine type by PRIORITY: carousel > video > photo
        indicators = carousel_indicators + video_indicators
        if carousel_indicators:
            post_type = "carousel"
        elif video_indicators:
            post_type = "video"
        else:
            post_type = "photo"
        
        return {
            "post_type": post_type,
            "indicators": indicators,
            **nurtured_info,
        }
    
    # -------------------------------------------------------------------------
    # CONSOLIDATED OBSERVATION TOOL (Super-Tool)
    # -------------------------------------------------------------------------
    
    def analyze_feed_posts(max_posts: int = 3) -> dict:
        """
        🚀 SUPER-TOOL: Analyze all visible posts in ONE call.
        
        Combines: detect_screen + get_elements + is_nurtured + check_liked + detect_post_type
        
        This tool reduces 5 LLM round-trips to 1, saving ~10 seconds per post.
        
        USE THIS instead of calling observation tools separately!
        
        Args:
            max_posts: Maximum posts to analyze (default 3)
            
        Returns:
            dict with:
            - screen: Screen context info
            - posts: List of analyzed posts with all info
            - recommended_action: What to do next (Python decision)
            - target_post: Post to engage with (if any)
        """
        import re
        from .memory_tools import is_nurtured_account
        
        # 1. Get XML once (uses cache if available)
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {
                "screen": {"needs_recovery": True, "context": "unknown"},
                "posts": [],
                "recommended_action": "recover",
                "reason": "Invalid XML - screen not readable",
            }
        
        xml_str = xml_result["xml"]
        
        # 2. Detect screen context
        screen_result = _detect_screen(xml_str)
        screen_info = {
            "context": screen_result.context.value,
            "in_instagram": is_in_instagram(xml_str),
            "needs_recovery": needs_recovery(screen_result),
            "has_dialog": screen_result.has_dialog,
            "has_keyboard": screen_result.has_keyboard,
        }
        
        if screen_info["needs_recovery"]:
            # Auto-dismiss account switcher/login screens
            if "account_switcher" in screen_info["context"] or "add_account" in screen_info["context"] or "login" in screen_info["context"]:
                from lamda.client import Keys
                dismissed = False
                for _ in range(3):
                    dm.device.press_key(Keys.KEY_BACK)
                    time.sleep(0.6)
                    dm.invalidate_xml_cache()
                    recheck_xml = get_screen_xml()
                    recheck_ctx = _detect_screen(recheck_xml.get("xml", "")).context.value
                    if "account_switcher" not in recheck_ctx and "add_account" not in recheck_ctx and "login" not in recheck_ctx:
                        dismissed = True
                        break
                screen_info["auto_dismissed"] = dismissed
                logger.info(
                    f"Auto-dismissed {screen_info['context']} (dismissed={dismissed})"
                )
                return {
                    "screen": screen_info,
                    "posts": [],
                    "recommended_action": "continue_scrolling",
                    "reason": f"Auto-dismissed {screen_info['context']}. Call analyze_feed_posts() again.",
                    "_nav_hint": _get_nav_hint(),
                }
            return {
                "screen": screen_info,
                "posts": [],
                "recommended_action": "recover",
                "reason": f"Not in feed - screen is {screen_info['context']}",
            }

        # Search is a common trap after hashtag/profile exploration. Escape it
        # proactively so the agent returns to a stable feed loop.
        if screen_info["context"] == "instagram_search":
            from lamda.client import Keys
            dm.device.press_key(Keys.KEY_BACK)
            time.sleep(0.6)
            dm.invalidate_xml_cache()
            return {
                "screen": screen_info,
                "posts": [],
                "recommended_action": "continue_scrolling",
                "reason": "Was on search screen — pressed Back to return to feed. Call analyze_feed_posts() again.",
                "auto_escaped_search": True,
                "_nav_hint": _get_nav_hint(),
            }
        
        # 3. Parse XML for posts
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError:
            return {
                "screen": screen_info,
                "posts": [],
                "recommended_action": "recover",
                "reason": "XML parse error",
            }
        
        def parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
            match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
            if match:
                return tuple(int(x) for x in match.groups())
            return None
        
        # Collect post headers, like buttons, and type indicators
        post_headers = []  # [(username, y_top, y_bottom, y_center, timestamp, is_sponsored), ...]
        like_buttons = []  # [(content_desc, selected, y_center, bounds), ...]
        video_indicators_by_y = {}  # {y_range: [indicators]}
        carousel_indicators_by_y = {}  # {y_range: [indicators]}
        ad_label_positions = []  # [y_center, ...] for "Ad", "Sponsored", "Paid partnership" labels
        
        # First pass: collect Ad/Sponsored labels
        for node in root.iter("node"):
            text = node.get("text", "").strip().lower()
            content_desc = node.get("content-desc", "").lower()
            bounds = parse_bounds(node.get("bounds", ""))
            
            if bounds:
                y_center = (bounds[1] + bounds[3]) // 2
                
                # CRITICAL: Detect Ad/Sponsored labels
                # Instagram shows "Ad", "Sponsored", or "Paid partnership" near
                # the username.  Use substring match — some locales/layouts
                # render "Sponsored · Brand" or "Paid partnership with @brand".
                if text in ("ad", "sponsored") or \
                   "sponsored" in text or "paid partnership" in text or \
                   "sponsored" in content_desc or "paid partnership" in content_desc:
                    ad_label_positions.append(y_center)
        
        # Second pass: collect all elements
        for node in root.iter("node"):
            res_id = node.get("resource-id", "")
            content_desc = node.get("content-desc", "")
            text = node.get("text", "").strip()
            bounds = parse_bounds(node.get("bounds", ""))
            
            if not bounds:
                continue
                
            y_center = (bounds[1] + bounds[3]) // 2
            
            # Post headers
            if "row_feed_profile_header" in res_id or "row_feed_photo_profile_name" in res_id:
                if text:
                    username = text.split()[0].rstrip("  ")
                elif content_desc:
                    username = content_desc.split()[0]
                else:
                    continue
                
                # Check for sponsored in header text (substring match)
                _cd_lower = content_desc.lower()
                _tx_lower = text.lower()
                is_sponsored = (
                    "sponsored" in _cd_lower or "sponsored" in _tx_lower
                    or "paid partnership" in _cd_lower or "paid partnership" in _tx_lower
                )
                
                # Also check if there's an "Ad"/"Sponsored" label near this
                # header (within 250px vertically)
                if not is_sponsored:
                    for ad_y in ad_label_positions:
                        if abs(y_center - ad_y) < 250:
                            is_sponsored = True
                            break
                
                # Extract timestamp if in content_desc (e.g., "user posted 2h")
                timestamp = ""
                if content_desc:
                    parts = content_desc.split()
                    if len(parts) >= 3:
                        timestamp = " ".join(parts[2:])  # "2h" or "January 25"
                
                # Heuristic: ads often have no timestamp in header
                if not is_sponsored and not timestamp:
                    for ad_y in ad_label_positions:
                        if abs(y_center - ad_y) < 400:
                            is_sponsored = True
                            break
                
                if username:
                    post_headers.append((username, bounds[1], bounds[3], y_center, timestamp, is_sponsored))
            
            # Like buttons
            if "row_feed_button_like" in res_id:
                selected = node.get("selected", "false") == "true"
                like_buttons.append((content_desc, selected, y_center, bounds))
            
            # Video indicators
            if "video_container" in res_id or "video_states" in res_id:
                y_key = y_center // 500  # Group by ~500px ranges
                video_indicators_by_y.setdefault(y_key, []).append("video")
            if "Turn sound" in content_desc or "posted a video" in content_desc.lower():
                y_key = y_center // 500
                video_indicators_by_y.setdefault(y_key, []).append("video")
            
            # Carousel indicators
            if "carousel" in res_id and "media_group" in res_id:
                y_key = y_center // 500
                carousel_indicators_by_y.setdefault(y_key, []).append("carousel")
            if "page_indicator" in res_id or re.search(r"\d+\s+of\s+\d+", content_desc):
                y_key = y_center // 500
                carousel_indicators_by_y.setdefault(y_key, []).append("carousel")
            if "posted a carousel" in content_desc.lower():
                y_key = y_center // 500
                carousel_indicators_by_y.setdefault(y_key, []).append("carousel")
        
        # 3b. PROFILE TRAP DETECTION: if ALL visible posts are from ONE non-nurtured
        # user, we're stuck on their profile page. Press back to return to feed.
        # IMPORTANT: Only trigger when NOT on the main feed — on the feed it's
        # normal for the same user (especially active accounts) to have 2+ posts
        # in a row. We only escape from profile/other screens.
        _on_feed = screen_info.get("context") == "instagram_feed"
        if not _on_feed and len(post_headers) >= 2:
            unique_authors = {h[0].lower() for h in post_headers}
            if len(unique_authors) == 1:
                profile_username = post_headers[0][0]
                try:
                    _nurtured_check = is_nurtured_account(profile_username)
                    _is_nurtured = _nurtured_check.get("is_nurtured", False)
                except Exception as e:
                    logger.warning(f"Profile trap: nurtured check failed for @{profile_username}: {e}")
                    _is_nurtured = False
                
                if not _is_nurtured:
                    logger.info(
                        f"🔙 Profile trap: all {len(post_headers)} posts from "
                        f"non-nurtured @{profile_username} — pressing back to feed"
                    )
                    try:
                        from lamda.client import Keys
                        dm.device.press_key(Keys.KEY_BACK)
                        dm.invalidate_xml_cache()
                        time.sleep(0.5)
                    except Exception as e:
                        logger.warning(f"Failed to press back from profile: {e}")
                    return {
                        "screen": screen_info,
                        "posts": [],
                        "recommended_action": "continue_scrolling",
                        "reason": (
                            f"Was on @{profile_username}'s profile (not nurtured) — "
                            "pressed back to feed. Call analyze_feed_posts() again."
                        ),
                        "auto_escaped_profile": True,
                    }
                else:
                    logger.debug(
                        f"📌 On nurtured @{profile_username}'s profile — "
                        "allowing intentional visit (no escape)"
                    )
        
        # 4. Build enriched posts list (with session-level dedup)
        analyzed_set = _get_analyzed_set()
        enriched_posts = []
        skipped_seen = 0
        
        for i, (username, y_top, y_bottom, y_center, timestamp, is_sponsored) in enumerate(post_headers[:max_posts]):
            # Feed dedup: skip posts we already analyzed this session.
            # Only dedup when timestamp is non-empty (otherwise different posts by
            # the same user with empty timestamps would collide).
            ts_clean = timestamp.strip() if timestamp else ""
            if ts_clean:
                dedup_key = (username.lower(), ts_clean)
                if dedup_key in analyzed_set:
                    skipped_seen += 1
                    logger.debug(f"⏭️ Feed dedup: skipping already-analyzed post by @{username} ({ts_clean})")
                    continue
                analyzed_set.add(dedup_key)
            
            # Find like button for this post
            # Like button should be below header and above next header
            next_header_y = float('inf')
            if i + 1 < len(post_headers):
                next_header_y = post_headers[i + 1][1]
            
            like_info = {"found": False, "is_liked": False, "can_like": False, "button": None}
            for desc, selected, btn_y, btn_bounds in like_buttons:
                if btn_y > y_center and btn_y < next_header_y:
                    is_liked = desc == "Liked" or selected
                    like_info = {
                        "found": True,
                        "is_liked": is_liked,
                        "can_like": not is_liked,
                        "button": {"x": (btn_bounds[0] + btn_bounds[2]) // 2, "y": (btn_bounds[1] + btn_bounds[3]) // 2},
                    }
                    break
            
            # Determine post type
            y_key = y_center // 500
            has_carousel = y_key in carousel_indicators_by_y
            has_video = y_key in video_indicators_by_y
            
            if has_carousel:
                post_type = "carousel"
            elif has_video:
                post_type = "video"
            else:
                post_type = "photo"
            
            # Check nurtured status (MongoDB lookup — import at function top)
            nurtured_info = {"is_nurtured": False, "priority": None}
            try:
                nurtured_result = is_nurtured_account(username)
                nurtured_info = {
                    "is_nurtured": nurtured_result.get("is_nurtured", False),
                    "priority": nurtured_result.get("priority"),
                }
            except Exception as e:
                logger.warning(f"Nurtured check failed for @{username}: {e}")
            
            # Check if already commented on this post (MongoDB + session dedup)
            already_commented = False
            if timestamp:
                try:
                    from .memory_tools import check_post_interaction as _check_post
                    comment_check = _check_post(username, timestamp, action="comment")
                    already_commented = comment_check.get("interacted", False) or False
                except Exception:
                    pass  # If check fails, assume not commented (fail-open for analysis)
            
            # CRITICAL LOGIC: Can we like this post?
            # - Photo/Carousel: double_tap_like() works WITHOUT visible buttons!
            # - Video: NEEDS tap(x,y) on Like button → requires visible button
            
            if post_type == "video":
                # Video needs visible Like button for tap()
                can_like = like_info["found"] and not like_info["is_liked"]
                needs_buttons = not like_info["found"]
            else:
                # Photo/Carousel: double_tap works without buttons
                # Only thing that blocks is if already liked (but we can't know without button)
                can_like = not like_info["is_liked"] if like_info["found"] else True  # Assume can like if unknown
                needs_buttons = False
            
            # Python decision: engagement recommendation
            if is_sponsored:
                recommendation = "SKIP_SPONSORED"
            elif like_info["is_liked"] and already_commented:
                recommendation = "SKIP_FULLY_ENGAGED"
            elif like_info["is_liked"]:
                recommendation = "SKIP_ALREADY_LIKED"
            elif post_type == "video" and needs_buttons:
                # Only video needs buttons - photo/carousel use double_tap
                recommendation = "SCROLL_BUTTONS_NOT_VISIBLE"
            elif nurtured_info["is_nurtured"]:
                recommendation = "MUST_ENGAGE_VIP"
            elif _warmup_mode.get():
                recommendation = "SKIP_NOT_NURTURED"
            else:
                recommendation = "RANDOM_25%"
            
            enriched_posts.append({
                "username": username,
                "timestamp": timestamp,
                "is_sponsored": is_sponsored,
                "is_nurtured": nurtured_info["is_nurtured"],
                "nurtured_priority": nurtured_info["priority"],
                "is_liked": like_info["is_liked"],
                "already_commented": already_commented,
                "can_like": can_like,
                "like_button": like_info["button"],
                "buttons_visible": like_info["found"],
                "post_type": post_type,
                "recommendation": recommendation,
            })
        
        # 5. Determine recommended action
        # Priority: VIP > Random 25%
        # Decision is made HERE - agent just follows the recommendation!
        
        # 5a. Starvation guard: if ALL visible posts were deduped (already seen),
        # the agent hasn't scrolled far enough. Signal to scroll harder.
        if skipped_seen > 0 and len(enriched_posts) == 0:
            logger.info(f"⏭️ All {skipped_seen} visible posts already seen — need bigger scroll")
            return {
                "screen": screen_info,
                "posts": [],
                "post_count": 0,
                "skipped_already_seen": skipped_seen,
                "recommended_action": "scroll",
                "reason": f"All {skipped_seen} visible posts already analyzed. Use scroll_feed('fast') to move further.",
                "target_post": None,
                "note": "All posts on screen were already seen. Scroll further to find new posts.",
            }
        
        target_post = None
        recommended_action = "scroll"
        reason = "No actionable posts"
        
        for post in enriched_posts:
            rec = post["recommendation"]
            
            # Skip non-actionable
            if rec in ("SKIP_SPONSORED", "SKIP_ALREADY_LIKED", "SKIP_NOT_NURTURED", "SKIP_FULLY_ENGAGED"):
                continue
            
            # VIP video needs buttons → scroll to find them
            if rec == "SCROLL_BUTTONS_NOT_VISIBLE" and post["is_nurtured"]:
                target_post = post
                recommended_action = "scroll_to_buttons"
                reason = f"VIP video @{post['username']} - need buttons for tap()"
                break
            
            # VIP must engage
            if rec == "MUST_ENGAGE_VIP":
                target_post = post
                recommended_action = "engage"
                reason = f"VIP @{post['username']} ({post['post_type']}) - MUST engage!"
                break
            
            # Random 25% for regular posts (decision made HERE, not by agent!)
            if rec == "RANDOM_25%":
                if random.random() < 0.25:
                    target_post = post
                    recommended_action = "engage"
                    reason = f"Random 25% → engage @{post['username']} ({post['post_type']})"
                    break
                # else: continue to next post (this one was not selected)
        
        _set_nav(screen_info.get("context", "unknown"), 0 if "feed" in screen_info.get("context", "") else _nav_depth.get())
        
        # ── Navigation-lost detection ──
        # If we keep returning 0 posts, the agent is probably lost (not on feed).
        # After ZERO_POST_RESTART_THRESHOLD consecutive 0-post calls, force-restart
        # Instagram to guarantee a clean feed state.
        if len(enriched_posts) > 0:
            dm._zero_post_streak = 0
        else:
            dm._zero_post_streak += 1
            if dm._zero_post_streak >= dm.ZERO_POST_RESTART_THRESHOLD:
                logger.warning(
                    f"🔄 {dm._zero_post_streak} consecutive analyze calls with 0 posts — "
                    "agent is lost, force-restarting Instagram"
                )
                dm._zero_post_streak = 0
                try:
                    app = dm.device.application("com.instagram.android")
                    app.stop()
                    time.sleep(0.3)
                    app.start()
                    dm.device.wait_for_idle(timeout=2000)
                    dm.invalidate_xml_cache()
                except Exception as e:
                    logger.error(f"Auto-restart failed: {e}")
                return {
                    "screen": screen_info,
                    "posts": [],
                    "recommended_action": "continue_scrolling",
                    "reason": (
                        f"Agent was lost ({dm.ZERO_POST_RESTART_THRESHOLD} calls with 0 posts). "
                        "Instagram was RESTARTED — you are now on the feed. "
                        "Call analyze_feed_posts() to continue."
                    ),
                    "auto_restarted": True,
                    "_nav_hint": _get_nav_hint(),
                }
        
        return {
            "screen": screen_info,
            "posts": enriched_posts,
            "post_count": len(enriched_posts),
            "skipped_already_seen": skipped_seen,
            "recommended_action": recommended_action,
            "reason": reason,
            "target_post": target_post,
            "_nav_hint": _get_nav_hint(),
            "note": (
                "WARMUP MODE: Only nurtured accounts get engagement. Non-nurtured posts are SKIPPED. "
                "Just follow recommended_action. video=tap(like_button), photo/carousel=double_tap_like()"
            ) if _warmup_mode.get() else (
                "Decision already made! Just follow recommended_action. video=tap(like_button), photo/carousel=double_tap_like()"
            ),
        }
    
    # -------------------------------------------------------------------------
    # Carousel Tools
    # -------------------------------------------------------------------------
    
    def detect_carousel(target_username: str) -> dict:
        """Detect if current post is a carousel (multiple images/videos).
        
        Use this to check if a post has multiple pages before deciding
        whether to swipe through it.
        
        AUTO-ENRICHED: This tool automatically checks nurtured status and includes
        it in the response. If `is_nurtured: true`, you MUST swipe through carousel!
        
        Args:
            target_username: Post author username
            
        Returns:
            dict with:
            - is_carousel: True if post has multiple pages
            - page_count: Number of pages (if detectable), or "unknown"
            - current_page: Current page number (if detectable)
            - swipe_bounds: (x1, y1, x2, y2) area to swipe on
            - is_nurtured: True if this is a VIP account (AUTO-CHECKED!)
            - nurtured_priority: "vip"/"high"/"medium" if nurtured
        """
        import re
        
        # AUTO-CHECK NURTURED STATUS (so agent can't skip this!)
        nurtured_info = {"is_nurtured": False}
        try:
            from .memory_tools import is_nurtured_account
            nurtured_result = is_nurtured_account(target_username)
            nurtured_info = {
                "is_nurtured": nurtured_result.get("is_nurtured", False),
                "nurtured_priority": nurtured_result.get("priority"),
            }
        except Exception as e:
            logger.warning(f"Could not check nurtured status: {e}")
        
        def parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
            match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
            if match:
                return tuple(int(x) for x in match.groups())
            return None
        
        xml_result = get_screen_xml()
        if not xml_result.get("valid"):
            return {"is_carousel": False, "error": "invalid_xml", **nurtured_info}
        
        try:
            root = ET.fromstring(xml_result["xml"])
        except ET.ParseError:
            return {"is_carousel": False, "error": "xml_parse_error", **nurtured_info}
        
        target_username_lower = target_username.lower().strip()
        
        # Find post region for this username - MULTIPLE strategies
        header_y = None
        
        # Strategy 1: Look in profile name elements (preferred - most reliable)
        for node in root.iter("node"):
            res_id = node.get("resource-id", "")
            text = node.get("text", "").strip()
            content_desc = node.get("content-desc", "")
            
            if "row_feed_photo_profile_name" in res_id or "row_feed_profile_header" in res_id:
                node_text = text or content_desc
                if target_username_lower in node_text.lower():
                    bounds = parse_bounds(node.get("bounds", ""))
                    if bounds:
                        header_y = bounds[1]
                        break
        
        # Strategy 2: Look in content descriptions (handles "Photo 1 of 7 by Username" format)
        if header_y is None:
            for node in root.iter("node"):
                content_desc = node.get("content-desc", "")
                if target_username_lower in content_desc.lower():
                    bounds = parse_bounds(node.get("bounds", ""))
                    if bounds and bounds[1] > 200:  # Below status bar
                        header_y = bounds[1]
                        break
        
        # Strategy 3: Look in any text element
        if header_y is None:
            for node in root.iter("node"):
                text = node.get("text", "").strip()
                if text and target_username_lower in text.lower():
                    bounds = parse_bounds(node.get("bounds", ""))
                    if bounds and bounds[1] > 200:  # Below status bar
                        header_y = bounds[1]
                        break
        
        # Strategy 4: If username not found but carousel indicators exist, use them directly
        if header_y is None:
            for node in root.iter("node"):
                res_id = node.get("resource-id", "")
                if "carousel" in res_id and "media_group" in res_id:
                    bounds = parse_bounds(node.get("bounds", ""))
                    if bounds:
                        # Use carousel's Y as reference (post contains this carousel)
                        header_y = max(0, bounds[1] - 200)  # Estimate header above carousel
                        break
        
        if header_y is None:
            return {
                "is_carousel": False,
                "error": f"Post by '{target_username}' not found in screen",
                **nurtured_info,
            }
        
        # Find next post header to determine bounds of this post
        next_header_y = dm.screen_height
        for node in root.iter("node"):
            res_id = node.get("resource-id", "")
            if "row_feed_photo_profile_name" in res_id or "row_feed_profile_header" in res_id:
                bounds = parse_bounds(node.get("bounds", ""))
                if bounds and bounds[1] > header_y + 200:
                    next_header_y = min(next_header_y, bounds[1])
        
        # Look for carousel indicators
        carousel_info = {"is_carousel": False}
        
        for node in root.iter("node"):
            res_id = node.get("resource-id", "")
            text = node.get("text", "").strip()
            bounds = parse_bounds(node.get("bounds", ""))
            
            if not bounds:
                continue
            
            # Check if element is within this post's region
            y_center = (bounds[1] + bounds[3]) // 2
            if not (header_y < y_center < next_header_y):
                continue
            
            # Check for carousel media group (main carousel container)
            # Includes: carousel_media_group, carousel_video_media_group, carousel_image_media_group
            if "carousel" in res_id and "media_group" in res_id:
                carousel_info = {
                    "is_carousel": True,
                    "swipe_bounds": bounds,
                    "page_count": "unknown",
                }
            
            # Check for carousel page indicator dots
            if "carousel_page_indicator" in res_id or "page_indicator" in res_id:
                carousel_info["is_carousel"] = True
                carousel_info["has_page_indicator"] = True
            
            # Check content_desc for "Photo X of Y" or "Video X of Y" pattern
            content_desc = node.get("content-desc", "")
            page_match = re.search(r"(\d+)\s+of\s+(\d+)", content_desc)
            if page_match:
                carousel_info["is_carousel"] = True
                carousel_info["current_page"] = int(page_match.group(1))
                carousel_info["page_count"] = int(page_match.group(2))
            
            # Check for position text like "1/5" or "2/10"
            if re.match(r"^\d+/\d+$", text):
                parts = text.split("/")
                carousel_info["is_carousel"] = True
                carousel_info["current_page"] = int(parts[0])
                carousel_info["page_count"] = int(parts[1])
        
        # If no swipe_bounds found but is carousel, use media group bounds
        if carousel_info.get("is_carousel") and "swipe_bounds" not in carousel_info:
            # Default to center area of post
            carousel_info["swipe_bounds"] = (0, header_y + 100, dm.screen_width, next_header_y - 200)
        
        # Add nurtured info to result
        carousel_info.update(nurtured_info)
        
        return carousel_info
    
    def swipe_carousel(target_username: str, capture_screenshots: bool = False) -> dict:
        """Swipe through carousel post with human-like behavior.
        
        Randomly swipes 2-5 pages through the carousel for optimal balance
        between content viewing and speed.
        
        IMPORTANT: Use detect_carousel() first to confirm post is a carousel.
        
        Args:
            target_username: Post author username (for logging)
            capture_screenshots: If True, captures screenshot of the LAST page
                               and stores it for the LLM via before_model_callback
                               (injected as native image Part = ~258 tokens)
            
        Returns:
            dict with:
            - is_carousel: True if carousel detected
            - pages_viewed: Number of pages swiped through
            - page_count: Total pages in carousel (if detected)
            - current_page: Which page we're on now
            - has_screenshot: True if screenshot was captured for LLM
            - note: Human-readable summary
        """
        # First detect carousel
        carousel_info = detect_carousel(target_username)
        
        if not carousel_info.get("is_carousel"):
            return {
                "is_carousel": False,
                "note": "Not a carousel post - no swiping needed"
            }
        
        swipe_bounds = carousel_info.get("swipe_bounds")
        page_count = carousel_info.get("page_count", "unknown")
        current_page = carousel_info.get("current_page", 1)
        
        if not swipe_bounds:
            return {
                "is_carousel": True,
                "error": "Could not determine swipe area"
            }
        
        # Calculate how many swipes (2-5 for optimal balance)
        if isinstance(page_count, int):
            remaining_pages = page_count - current_page
            if remaining_pages <= 0:
                return {
                    "is_carousel": True,
                    "page_count": page_count,
                    "pages_viewed": 1,
                    "current_page": current_page,
                    "note": f"Already on page {current_page} of {page_count}, no more pages to view.",
                }
            max_swipes = min(5, remaining_pages)
        else:
            max_swipes = random.randint(2, 4)
        
        target_swipes = random.randint(2, max(2, min(10, max_swipes)))
        
        pages_viewed = 1
        
        # Swipe through carousel
        for i in range(target_swipes):
            swipe_result = dm.gestures.swipe_carousel(swipe_bounds, direction="left")
            pages_viewed += 1
        
        # Calculate what page we ended on
        final_page = current_page + target_swipes
        if isinstance(page_count, int):
            final_page = min(final_page, page_count)
        
        dm.invalidate_xml_cache()
        
        result = {
            "is_carousel": True,
            "page_count": page_count,
            "pages_viewed": pages_viewed,
            "current_page": final_page,
            "has_screenshot": False,
            "note": (
                f"Viewed {pages_viewed} carousel pages for @{target_username}. "
                f"Now on page {final_page}."
            ),
        }
        
        # Capture ONLY the last page screenshot — stored in dm for injection
        # by before_model_callback as native image Part (~258 tokens, not ~33K)
        if capture_screenshots:
            try:
                last_page_bytes = dm.device.screenshot()
                if last_page_bytes:
                    import uuid as _uuid
                    dm.last_screenshot = last_page_bytes
                    dm.last_screenshot_id = f"carousel_{target_username}_{_uuid.uuid4().hex[:6]}"
                    result["has_screenshot"] = True
                    result["note"] += " Screenshot of last page captured for analysis."
            except Exception as e:
                logger.warning(f"Carousel screenshot capture failed: {e}")
                result["note"] += " Use screenshot() to see current content."
        else:
            result["note"] += " Use screenshot() to see current content."
        
        return result
    
    # -------------------------------------------------------------------------
    # Navigation Tools
    # -------------------------------------------------------------------------
    
    def press_back() -> dict:
        """Press Android back button.
        
        Returns:
            dict confirming action with _nav_hint
        """
        from lamda.client import Keys
        
        dm.device.press_key(Keys.KEY_BACK)
        dm.invalidate_xml_cache()
        _nav_shallower()
        return {"pressed": "back", "_nav_hint": _get_nav_hint()}
    
    def press_home() -> dict:
        """Press Android home button.
        
        Returns:
            dict confirming action with _nav_hint
        """
        from lamda.client import Keys
        
        dm.device.press_key(Keys.KEY_HOME)
        dm.invalidate_xml_cache()
        _set_nav("home_screen", 0)
        return {"pressed": "home", "_nav_hint": _get_nav_hint()}
    
    def press_recent() -> dict:
        """Press Android recent apps button.
        
        Returns:
            dict confirming action
        """
        from lamda.client import Keys
        
        dm.device.press_key(Keys.KEY_RECENT)
        dm.invalidate_xml_cache()  # Screen changed
        return {"pressed": "recent"}
    
    def open_notification_panel() -> dict:
        """Pull down notification panel.
        
        Returns:
            dict confirming action
        """
        dm.device.open_notification()
        dm.invalidate_xml_cache()  # Screen changed
        return {"opened": "notification_panel"}
    
    # -------------------------------------------------------------------------
    # Text Input
    # -------------------------------------------------------------------------
    
    def type_text(
        text: str,
        human_like: bool = True,
        simulate_suggestions: bool = True,  # Changed default to True (GramAddict style)
        suggestion_probability: float = 0.7,
        gramaddict_style: bool = True,  # New: minimal delays like GramAddict
    ) -> dict:
        """Type text with human-like timing.
        
        IMPORTANT: An input field must be focused (keyboard visible) before calling.
        
        Three typing modes:
        
        1. GRAMADDICT MODE (default, gramaddict_style=True): Fastest human-like typing
           Based on GramAddict pattern - minimal delays, relies on natural API latency:
           - Type 1-3 letters of each word
           - Then "select" autocomplete suggestion (types rest of word at once)
           - No artificial delays between characters
           - Target speed: natural API latency (~50-100ms per call)
        
        2. SUGGESTION MODE (simulate_suggestions=True, gramaddict_style=False): 
           Same pattern but with small delays for slower appearance
        
        3. CHUNK MODE (simulate_suggestions=False): Types 2-6 characters at a time
           - Variable chunk sizes for natural rhythm
           - Occasional typos with quick correction
           - Natural pauses at word boundaries
           - Target speed: 4-7 chars/sec
        
        Args:
            text: Text to type
            human_like: If True, use typing simulation. If False, instant set_text.
            simulate_suggestions: If True, use word-by-word suggestion pattern.
            suggestion_probability: Probability of using suggestion per word (default 0.7).
            gramaddict_style: If True, minimal delays (relies on API latency). Default True.
            
        Returns:
            dict confirming action with timing info
        """
        # Local logger ref to avoid closure scoping issues
        _log = logging.getLogger("eidola.tools")

        # --- Posting safety guard ---
        from ..tools.posting_tools import _last_manifest, _caption_looks_suspicious
        if _last_manifest is not None:
            manifest_caption = _last_manifest.get("caption", "")
            if not manifest_caption or not manifest_caption.strip():
                _log.warning("type_text BLOCKED during posting: manifest caption is empty, skip caption step")
                return {"typed": False, "blocked": True, "reason": "Manifest caption is empty — do not type a caption. Skip to Share."}
            if _caption_looks_suspicious(text):
                _log.critical("type_text BLOCKED suspicious caption during posting: %r", text)
                return {"typed": False, "blocked": True, "reason": "Text blocked by safety filter. Interpret the manifest caption creatively instead."}
            # Detect repetition loops (e.g. "🏰🏰🏰🏰🏰..." or "hahahahaha...")
            import re as _re
            if _re.search(r'(.)\1{4,}', text) or _re.search(r'(.{1,4})\1{3,}', text):
                _log.warning("type_text BLOCKED: repetition loop detected in caption: %r", text[:80])
                return {"typed": False, "blocked": True, "reason": "Repetition loop detected in caption. Write a natural caption without repeating characters or emojis."}

        from lamda.client import Keys
        from lamda.exceptions import UiObjectNotFoundException
        
        # Known Instagram input field resource IDs (priority order)
        KNOWN_EDIT_TEXT_IDS = [
            "com.instagram.android:id/action_bar_search_edit_text",  # Search bar
            "com.instagram.android:id/layout_comment_thread_edittext",  # Comment field
            "com.instagram.android:id/row_thread_composer_edittext",  # DM field
        ]
        
        def _get_edit_text():
            """Get EditText element using multiple strategies.
            
            Priority:
            1. Known Instagram input field IDs (search, comment, DM)
            2. Currently FOCUSED EditText (critical for login forms with multiple fields!)
            3. Any EditText as fallback
            """
            # First: check known Instagram-specific input fields
            for res_id in KNOWN_EDIT_TEXT_IDS:
                et = dm.device(resourceId=res_id)
                if et.exists():
                    return et, res_id
            
            # Second: find the FOCUSED EditText (critical for multi-field forms like login)
            et = dm.device(className="android.widget.EditText", focused=True)
            if et.exists():
                return et, "android.widget.EditText[focused=True]"
            
            # Fallback: any EditText (single field scenario)
            et = dm.device(className="android.widget.EditText")
            if et.exists():
                return et, "android.widget.EditText"
            
            return None, None
        
        def _safe_set_text(element, new_text: str, selector_info: str, max_retries: int = 4) -> bool:
            """Set text with retry logic and progressive backoff.
            
            Instagram can temporarily remove EditText from the UI tree when
            refreshing search suggestions or comment overlays (~300-1000ms).
            Progressive backoff: 0.1s → 0.2s → 0.4s → 0.6s gives ~1.3s total.
            """
            backoff_delays = [0.1, 0.2, 0.4, 0.6]
            for attempt in range(max_retries):
                try:
                    element.set_text(new_text)
                    return True
                except UiObjectNotFoundException:
                    delay = backoff_delays[min(attempt, len(backoff_delays) - 1)]
                    if attempt < max_retries - 1:
                        time.sleep(delay)
                        new_element, new_info = _get_edit_text()
                        if new_element:
                            element = new_element
                            _log.debug(f"_safe_set_text: re-found element via {new_info} (attempt {attempt + 1})")
                        else:
                            _log.debug(f"_safe_set_text: element gone, waiting {delay}s (attempt {attempt + 1})")
                    else:
                        return False
                except Exception as e:
                    delay = backoff_delays[min(attempt, len(backoff_delays) - 1)]
                    _log.warning(f"set_text failed (attempt {attempt + 1}): {e}")
                    if attempt < max_retries - 1:
                        time.sleep(delay)
                    else:
                        return False
            return False
        
        def _type_with_recovery(current_text: str, et, sel_info: str):
            """Try _safe_set_text, with an extra cache-invalidate recovery if it fails.
            
            Returns (element, selector_info, success_bool).
            When Instagram refreshes suggestions/overlays, EditText can vanish
            for up to ~1s. _safe_set_text handles short gaps; this adds a final
            cache-invalidate + re-find attempt for longer UI refreshes.
            """
            if _safe_set_text(et, current_text, sel_info):
                return et, sel_info, True
            # _safe_set_text already retried 4x with ~1.3s backoff — still failed.
            # Final recovery: invalidate XML cache and re-find fresh element
            _log.warning(f"Element lost after {len(current_text)} chars — attempting recovery")
            time.sleep(0.5)
            dm.invalidate_xml_cache()
            new_et, new_info = _get_edit_text()
            if new_et and _safe_set_text(new_et, current_text, new_info):
                _log.info(f"✅ Element recovered via {new_info} — continuing typing")
                return new_et, new_info, True
            return et, sel_info, False
        
        # =================================================================
        # PRE-FLIGHT: Dismiss auto-fill overlay if present
        # =================================================================
        try:
            autofill_el = dm.device(resourceId="android:id/autofill_dataset_picker")
            if autofill_el.exists():
                _log.info("Auto-fill overlay detected — dismissing with BACK")
                from lamda.client import Keys
                dm.device.press_key(Keys.KEY_BACK)
                time.sleep(0.3)
                dm.invalidate_xml_cache()
            else:
                autofill_save = dm.device(resourceId="android:id/autofill_save")
                if autofill_save.exists():
                    _log.info("Auto-fill save prompt detected — dismissing with BACK")
                    from lamda.client import Keys
                    dm.device.press_key(Keys.KEY_BACK)
                    time.sleep(0.3)
                    dm.invalidate_xml_cache()
        except Exception:
            pass
        
        # Ensure auto-fill service stays disabled
        try:
            dm.device.execute_script("settings put secure autofill_service null 2>/dev/null || true")
        except Exception:
            pass
        
        edit_text, selector_info = _get_edit_text()
        
        if not edit_text:
            return {
                "typed": False,
                "error": "No EditText element found. Make sure input field is focused.",
                "text": text,
            }
        
        # =================================================================
        # PRE-FLIGHT GUARD: Block comment typing when 24h limit reached
        # =================================================================
        # This prevents the comment from reaching Instagram at all.
        # Uses MongoDB 24h rolling window — works across ALL sessions.
        _sel_lower = (selector_info or "").lower()
        _is_comment = "comment" in _sel_lower
        
        if _is_comment:
            try:
                from .memory_tools import get_24h_comment_count
                limit_result = get_24h_comment_count()
                if not limit_result.get("can_comment", False):
                    _log.warning(
                        f"24H COMMENT GUARD: Blocked type_text() — "
                        f"{limit_result.get('count', '?')}/{limit_result.get('limit', '?')} "
                        f"comments in last 24h ({selector_info!r})"
                    )
                    return {
                        "typed": False,
                        "error": (
                            f"24H COMMENT LIMIT REACHED: {limit_result.get('count', '?')}/{limit_result.get('limit', '?')} "
                            f"comments in last 24 hours. No more comments allowed! "
                            f"Remaining: {limit_result.get('remaining', 0)}"
                        ),
                    }
            except Exception as e:
                _log.warning(f"24h comment check failed ({e}) — blocking comment for safety")
                return {
                    "typed": False,
                    "error": f"Cannot verify comment limit ({e}) — blocked for safety",
                }
        
        if not human_like or len(text) == 0:
            if _safe_set_text(edit_text, text, selector_info):
                return {"typed": True, "text": text, "mode": "instant"}
            else:
                return {"typed": False, "error": "Failed to set text", "text": text}
        
        # PRE-TYPING PAUSE: Removed — Gemini API latency (2-5s) provides natural thinking delay
        
        start_time = time.time()
        
        # =====================================================================
        # SUGGESTION MODE: GramAddict-style autocomplete simulation
        # =====================================================================
        if simulate_suggestions:
            current_text = ""
            words_typed = 0
            suggestions_used = 0
            
            # Split into words, preserving spaces and punctuation
            import re
            tokens = re.findall(r'\S+|\s+', text)  # Split keeping whitespace as tokens
            
            for token in tokens:
                if not token.strip():
                    # It's whitespace - just append
                    current_text += token
                    edit_text, selector_info, ok = _type_with_recovery(current_text, edit_text, selector_info)
                    if not ok:
                        return {"typed": False, "error": "Element lost", "partial": current_text}
                    # GramAddict style: no delay, API latency is enough
                    if not gramaddict_style:
                        time.sleep(random.uniform(0.03, 0.08))
                    continue
                
                # It's a word - decide if using "suggestion"
                use_suggestion = random.random() < suggestion_probability and len(token) > 2
                
                if use_suggestion:
                    # Type 1-3 letters first (GramAddict pattern)
                    n_typed_first = random.randint(1, min(3, len(token) - 1))
                    
                    # Type letters one by one
                    for i, char in enumerate(token[:n_typed_first]):
                        current_text += char
                        edit_text, selector_info, ok = _type_with_recovery(current_text, edit_text, selector_info)
                        if not ok:
                            return {"typed": False, "error": "Element lost", "partial": current_text}
                        # GramAddict style: no artificial delay, API latency is enough
                        if not gramaddict_style:
                            time.sleep(random.uniform(0.08, 0.18))
                    
                    # "Select" suggestion = type rest of word at once
                    rest_of_word = token[n_typed_first:]
                    current_text += rest_of_word
                    edit_text, selector_info, ok = _type_with_recovery(current_text, edit_text, selector_info)
                    if not ok:
                        return {"typed": False, "error": "Element lost", "partial": current_text}
                    
                    suggestions_used += 1
                else:
                    # No suggestion - type word normally (in chunks)
                    for char in token:
                        current_text += char
                        edit_text, selector_info, ok = _type_with_recovery(current_text, edit_text, selector_info)
                        if not ok:
                            return {"typed": False, "error": "Element lost", "partial": current_text}
                        # GramAddict style: no artificial delay
                        if not gramaddict_style:
                            time.sleep(random.uniform(0.04, 0.12))
                
                words_typed += 1
                # GramAddict style: no pause between words
                if not gramaddict_style:
                    time.sleep(random.uniform(0.05, 0.15))
            
            elapsed = time.time() - start_time
            
            # POST-TYPING PAUSE: Removed — Gemini API latency provides natural delay before next action
            
            dm.invalidate_xml_cache()
            
            return {
                "typed": True,
                "text": text,
                "mode": "gramaddict" if gramaddict_style else "suggestions",
                "chars": len(text),
                "words": words_typed,
                "suggestions_used": suggestions_used,
                "elapsed_ms": int(elapsed * 1000),
                "chars_per_sec": round(len(text) / elapsed, 1) if elapsed > 0 else 0,
            }
        
        # =====================================================================
        # CHUNK MODE: Original chunk-based typing
        # =====================================================================
        typos_made = 0
        chunks_typed = 0
        
        # Adjacent keys for typo simulation
        adjacent_keys = {
            'a': 'sqwz', 'b': 'vghn', 'c': 'xdfv', 'd': 'erfcxs', 'e': 'rwsd',
            'f': 'rtgvcd', 'g': 'tyhbvf', 'h': 'yujnbg', 'i': 'uojk', 'j': 'uikmnh',
            'k': 'iolmj', 'l': 'opk', 'm': 'njk', 'n': 'bhjm', 'o': 'iplk',
            'p': 'ol', 'q': 'wa', 'r': 'etdf', 's': 'awedxz', 't': 'ryfg',
            'u': 'yihj', 'v': 'cfgb', 'w': 'qeas', 'x': 'zsdc', 'y': 'tugh', 'z': 'asx',
        }
        
        current_text = ""
        pos = 0
        
        while pos < len(text):
            # === DETERMINE CHUNK SIZE ===
            # Variable chunk sizes: 2-6 chars typically, occasional bursts up to 8
            remaining = len(text) - pos
            
            # Varied chunk sizes for natural rhythm
            # Ensure min doesn't exceed remaining chars
            if remaining <= 2:
                # Very few chars left - just type them
                chunk_size = remaining
            elif random.random() < 0.25 and remaining >= 4:
                # Fast burst: type more at once (4-8 chars)
                chunk_size = random.randint(4, min(8, remaining))
            elif random.random() < 0.15:
                # Careful/slow: smaller chunks (1-2 chars)
                chunk_size = random.randint(1, min(2, remaining))
            else:
                # Normal: 2-5 chars
                chunk_size = random.randint(2, min(5, remaining))
            
            # Try to end at word boundary if close (looks more natural)
            end_pos = pos + chunk_size
            if end_pos < len(text):
                for offset in range(min(3, len(text) - end_pos)):
                    if text[end_pos + offset] == ' ':
                        chunk_size += offset + 1
                        break
            
            chunk_size = min(chunk_size, remaining)
            chunk = text[pos:pos + chunk_size]
            
            # === TYPO SIMULATION (3% chance per chunk) ===
            if pos > 0 and random.random() < 0.03 and len(chunk) > 1:
                typo_pos = random.randint(0, len(chunk) - 1)
                typo_char = chunk[typo_pos].lower()
                
                if typo_char in adjacent_keys:
                    wrong_char = random.choice(adjacent_keys[typo_char])
                    if chunk[typo_pos].isupper():
                        wrong_char = wrong_char.upper()
                    
                    # Type chunk with typo
                    typo_chunk = chunk[:typo_pos] + wrong_char + chunk[typo_pos + 1:]
                    current_text += typo_chunk
                    
                    if _safe_set_text(edit_text, current_text, selector_info):
                        typos_made += 1
                        
                        # Notice typo (80-180ms)
                        time.sleep(random.uniform(0.08, 0.18))
                        
                        # Backspace to fix
                        chars_to_delete = len(chunk) - typo_pos
                        for _ in range(chars_to_delete):
                            dm.device.press_key(Keys.KEY_DELETE)
                            time.sleep(random.uniform(0.02, 0.04))
                        
                        current_text = current_text[:-(chars_to_delete)]
                        
                        # Retype correct portion
                        current_text += chunk[typo_pos:]
                        _safe_set_text(edit_text, current_text, selector_info)
                        chunks_typed += 1
                        pos += chunk_size
                        
                        # Post-correction delay
                        time.sleep(random.uniform(0.05, 0.12))
                        continue
                    else:
                        current_text = current_text[:-len(typo_chunk)]
            
            # === TYPE CHUNK ===
            current_text += chunk
            edit_text, selector_info, ok = _type_with_recovery(current_text, edit_text, selector_info)
            if not ok:
                return {
                    "typed": False,
                    "error": "Element lost during typing",
                    "partial_text": current_text[:-len(chunk)],
                    "chars_typed": pos,
                }
            
            chunks_typed += 1
            pos += chunk_size
            
            # === INTER-CHUNK DELAY ===
            if pos < len(text):
                last_char = chunk[-1] if chunk else ''
                next_char = text[pos] if pos < len(text) else ''
                
                # Base delay: 70-160ms per chunk → ~4-7 chars/sec
                delay = random.uniform(0.07, 0.16)
                
                # Context-based adjustments
                if last_char in '.!?':
                    delay += random.uniform(0.12, 0.35)  # Sentence end
                elif last_char in ',;:':
                    delay += random.uniform(0.06, 0.18)  # Clause break
                
                if last_char == ' ' and random.random() < 0.2:
                    delay += random.uniform(0.04, 0.12)  # Word boundary
                
                if next_char.isupper():
                    delay += random.uniform(0.02, 0.08)  # Before capital
                
                # Occasional longer "thinking" pause (4%)
                if random.random() < 0.04:
                    delay = random.uniform(0.20, 0.50)
                
                # Occasional very fast burst (12%)
                if random.random() < 0.12:
                    delay = random.uniform(0.02, 0.06)
                
                time.sleep(delay)
        
        elapsed = time.time() - start_time
        chars_per_sec = len(text) / elapsed if elapsed > 0 else 0
        
        # POST-TYPING PAUSE: Removed — Gemini API latency provides natural delay
        
        # Invalidate cache - screen content changed
        dm.invalidate_xml_cache()
        
        return {
            "typed": True,
            "text": text,
            "mode": "chunked",
            "chars": len(text),
            "chunks": chunks_typed,
            "typos_simulated": typos_made,
            "elapsed_ms": int(elapsed * 1000),
            "chars_per_sec": round(chars_per_sec, 1),
        }
    
    def clear_text() -> dict:
        """Clear text in currently focused input field.
        
        Returns:
            dict confirming action
        """
        # Find EditText element - clear_text_field() must be called on element
        edit_text = dm.device(className="android.widget.EditText", focused=True)
        if not edit_text.exists():
            edit_text = dm.device(className="android.widget.EditText")
        
        if edit_text.exists():
            edit_text.clear_text_field()
            dm.invalidate_xml_cache()  # Screen content changed
            return {"cleared": True}
        
        return {"cleared": False, "error": "No EditText element found"}
    
    # -------------------------------------------------------------------------
    # Comment Super-Tool (atomic comment workflow)
    # -------------------------------------------------------------------------

    def post_comment(
        comment_text: str,
        max_retries: int = 2,
        _skip_guards: bool = False,
    ) -> dict:
        """UI automation for posting a comment. Opens comment section → types → posts → verifies.
        
        ⚠️ INTERNAL: Prefer `comment_on_post(author, timestamp)` which handles the full pipeline
        (guard checks, screenshot, caption, AI generation, validation, posting, recording).
        
        This function handles only the UI automation part:
        1. Open comment section (tap comment button if on feed)
        2. Wait for comment input field
        3. Type the comment text
        4. Tap Post/Send button (tries ALL known selectors)
        5. Verify comment was posted
        
        Args:
            comment_text: The comment to post (1-10 words typically)
            max_retries: Max retries for typing/posting (default 2)
            _skip_guards: If True, skip 24h limit and dedup checks (used by comment_on_post)
            
        Returns:
            dict with:
            - posted: True if comment was successfully posted
            - comment: The comment text
            - elapsed_ms: Total time taken
            - steps: List of steps completed
            - error: Error message if failed
        """
        from lamda.exceptions import UiObjectNotFoundException
        
        _log = logging.getLogger("eidola.tools")
        steps = []
        start_time = time.time()
        opened_comments = False
        
        # --- Known Instagram element IDs ---
        COMMENT_BUTTON_ID = "com.instagram.android:id/row_feed_button_comment"
        COMMENT_INPUT_ID = "com.instagram.android:id/layout_comment_thread_edittext"
        COMMENT_INPUT_MULTILINE_ID = "com.instagram.android:id/layout_comment_thread_edittext_multiline"
        POST_BUTTON_SELECTORS = [
            {"resourceId": "com.instagram.android:id/layout_comment_thread_post_button_click_area"},
            {"resourceId": "com.instagram.android:id/layout_comment_thread_post_button_icon"},
            {"description": "Post"},
            {"description": "Post comment"},
            {"text": "Post"},
        ]
        
        _COMMENT_INPUT_IDS = [COMMENT_INPUT_ID, COMMENT_INPUT_MULTILINE_ID]
        
        def _find_comment_input():
            """Find comment input — tries both single-line and multiline variants."""
            for rid in _COMMENT_INPUT_IDS:
                el = dm.device(resourceId=rid)
                if el.exists():
                    return el
            return dm.device(resourceId=COMMENT_INPUT_ID)  # return non-existing for .exists() check
        
        def _elapsed():
            return int((time.time() - start_time) * 1000)
        
        # =================================================================
        # PRE-FLIGHT: Guard checks (skipped when called from comment_on_post)
        # =================================================================
        if not _skip_guards:
            try:
                from .memory_tools import get_24h_comment_count
                limit_result = get_24h_comment_count()
                if not limit_result.get("can_comment", False):
                    return {
                        "posted": False,
                        "error": f"24H LIMIT: {limit_result.get('count', '?')}/{limit_result.get('limit', '?')} comments. Stop!",
                        "elapsed_ms": _elapsed(),
                    }
            except Exception as e:
                _log.warning(f"24h comment check failed ({e}) — blocking for safety")
                return {"posted": False, "error": f"Cannot verify comment limit: {e}", "elapsed_ms": _elapsed()}
            
            try:
                from .memory_tools import (
                    check_comment_text_duplicate,
                    get_recent_comments,
                    get_session_comment_texts,
                )
                
                if check_comment_text_duplicate(comment_text):
                    return {
                        "posted": False,
                        "error": f"DUPLICATE BLOCKED: You already used '{comment_text}' this session! Write a DIFFERENT comment.",
                        "already_used": get_session_comment_texts(),
                        "elapsed_ms": _elapsed(),
                    }
                
                try:
                    recent = get_recent_comments()
                    recent_texts = [c.get("comment_text", "") for c in recent.get("comments", [])]
                    normalized_new = comment_text.strip().lower()
                    for rt in recent_texts:
                        if rt.strip().lower() == normalized_new:
                            return {
                                "posted": False,
                                "error": f"DUPLICATE BLOCKED: '{comment_text}' matches a recent comment in history! Write something UNIQUE.",
                                "recent_comments": recent_texts[:5],
                                "elapsed_ms": _elapsed(),
                            }
                except Exception as db_err:
                    _log.debug(f"DB dedup check failed (non-fatal): {db_err}")
                    
            except ImportError:
                _log.debug("memory_tools not available for dedup — skipping")
        else:
            _log.debug("post_comment: Guards skipped (called from comment_on_post)")
        
        # =================================================================
        # STEP 1: Ensure we're in comment view (open if needed)
        # =================================================================
        comment_input = _find_comment_input()
        
        if not comment_input.exists():
            # Not in comment view — try to open it
            comment_btn = dm.device(resourceId=COMMENT_BUTTON_ID)
            
            # If comment button not visible, try a small scroll to reveal buttons
            if not comment_btn.exists():
                steps.append("comment_btn_not_visible")
                _log.debug("post_comment: Comment button not visible, scrolling to reveal")
                for scroll_attempt in range(2):
                    from lamda.client import Point as _Point
                    _sx = dm.screen_width // 2
                    _sy = int(dm.screen_height * 0.65)
                    _ey = _sy - random.randint(250, 400)
                    dm.device.swipe(_Point(x=_sx, y=_sy), _Point(x=_sx, y=max(100, _ey)), step=12)
                    dm.invalidate_xml_cache()
                    time.sleep(0.3)
                    comment_btn = dm.device(resourceId=COMMENT_BUTTON_ID)
                    if comment_btn.exists():
                        steps.append(f"revealed_after_{scroll_attempt + 1}_scroll")
                        _log.info(f"post_comment: Comment button found after {scroll_attempt + 1} scroll(s)")
                        break
            
            if comment_btn.exists():
                comment_btn.click()
                steps.append("opened_comments")
                opened_comments = True
                _nav_deeper()
                _log.info(f"post_comment: Opened comment section")
                
                # Wait for comment input to appear (up to 3 seconds)
                for wait_i in range(6):
                    time.sleep(0.5)
                    comment_input = _find_comment_input()
                    if comment_input.exists():
                        break
                else:
                    # Last resort: tap the input area at bottom of screen
                    # Instagram comments input is typically at very bottom
                    from lamda.client import Point as _ClickPoint
                    dm.device.click(_ClickPoint(x=dm.screen_width // 2, y=dm.screen_height - 150))
                    time.sleep(0.5)
                    comment_input = _find_comment_input()
            else:
                # Maybe we're already viewing the post detail?
                # Try clicking on "Add a comment..." text
                add_comment = dm.device(text="Add a comment…")
                if not add_comment.exists():
                    add_comment = dm.device(textContains="Add a comment")
                if add_comment.exists():
                    add_comment.click()
                    time.sleep(0.5)
                    steps.append("tapped_add_comment_text")
                    opened_comments = True
                    _nav_deeper()
                    comment_input = _find_comment_input()
        else:
            steps.append("already_in_comments")
            opened_comments = True
        
        # Check if comment input is ready
        if not comment_input or not comment_input.exists():
            dm.invalidate_xml_cache()
            return {
                "posted": False,
                "error": "Comment input field not found. Are you on a post with visible comment button?",
                "steps": steps,
                "elapsed_ms": _elapsed(),
            }
        
        # Focus the input field
        try:
            comment_input.click()
            time.sleep(0.3)
            steps.append("focused_input")
        except Exception:
            pass  # May already be focused
        
        # =================================================================
        # STEP 2: Type the comment (try set_text, fallback to type_text)
        # =================================================================
        typed_ok = False
        for attempt in range(max_retries + 1):
            try:
                # Re-find element on retry
                if attempt > 0:
                    time.sleep(0.5)
                    dm.invalidate_xml_cache()
                    comment_input = _find_comment_input()
                    if not comment_input.exists():
                        comment_input = dm.device(className="android.widget.EditText", focused=True)
                    if not comment_input or not comment_input.exists():
                        continue
                    comment_input.click()
                    time.sleep(0.3)
                    try:
                        comment_input.clear_text_field()
                        time.sleep(0.2)
                    except Exception:
                        pass
                
                # Strategy A: Human-like typing (consistent with search input)
                type_result = type_text(comment_text, human_like=True, gramaddict_style=True)
                if type_result.get("typed"):
                    typed_ok = True
                    steps.append(f"typed_human_like(attempt={attempt + 1})")
                    _log.info(f"post_comment: Typed '{comment_text}' via type_text (attempt {attempt + 1})")
                    break
                
                # Strategy B: Fallback to set_text on last attempt only
                if attempt >= max_retries:
                    _log.warning(f"post_comment: type_text failed — last resort set_text")
                    dm.invalidate_xml_cache()
                    comment_input = _find_comment_input()
                    if not comment_input.exists():
                        comment_input = dm.device(className="android.widget.EditText", focused=True)
                    if comment_input and comment_input.exists():
                        try:
                            comment_input.set_text(comment_text)
                            typed_ok = True
                            steps.append(f"typed_set_text_fallback(attempt={attempt + 1})")
                            _log.info(f"post_comment: Typed '{comment_text}' via set_text fallback")
                            break
                        except Exception as set_err:
                            raise Exception(f"Both type_text and set_text failed: {set_err}")
                    else:
                        raise Exception(f"type_text failed and comment input not found for set_text")
                else:
                    _log.warning(f"post_comment: type_text failed attempt {attempt + 1}: {type_result.get('error')}")
                
            except (UiObjectNotFoundException, Exception) as e:
                _log.warning(f"post_comment: Typing failed attempt {attempt + 1}: {e}")
                steps.append(f"type_failed(attempt={attempt + 1}, error={str(e)[:50]})")
                if attempt >= max_retries:
                    dm.invalidate_xml_cache()
                    return {
                        "posted": False,
                        "error": f"Failed to type comment after {max_retries + 1} attempts: {e}",
                        "steps": steps,
                        "elapsed_ms": _elapsed(),
                    }
        
        if not typed_ok:
            dm.invalidate_xml_cache()
            return {
                "posted": False,
                "error": "Failed to type comment text",
                "steps": steps,
                "elapsed_ms": _elapsed(),
            }
        
        # =================================================================
        # STEP 3: Tap the Post button (try ALL known selectors)
        # =================================================================
        # Small wait for UI to settle after typing
        time.sleep(0.3)
        dm.invalidate_xml_cache()
        
        post_tapped = False
        for selector in POST_BUTTON_SELECTORS:
            try:
                btn = dm.device(**selector)
                if btn.exists():
                    btn.click()
                    post_tapped = True
                    steps.append(f"tapped_post_button({selector})")
                    _log.info(f"post_comment: Tapped Post button via {selector}")
                    break
            except Exception as e:
                _log.debug(f"post_comment: Selector {selector} failed: {e}")
                continue
        
        if not post_tapped:
            # Fallback: tap the right side of the comment input area
            # The Post button is always to the right of the input field
            try:
                input_el = _find_comment_input()
                if input_el.exists():
                    info = input_el.info()
                    bounds = info.get("visibleBounds", info.get("bounds", {}))
                    right_x = bounds.get("right", dm.screen_width - 50)
                    center_y = (bounds.get("top", 0) + bounds.get("bottom", dm.screen_height)) // 2
                    # Tap to the right of the input field (where Post button is)
                    tap_x = min(right_x + 60, dm.screen_width - 20)
                    from lamda.client import Point as _P; dm.device.click(_P(x=tap_x, y=center_y))
                    post_tapped = True
                    steps.append(f"tapped_post_fallback(x={tap_x}, y={center_y})")
                    _log.info(f"post_comment: Tapped Post button via coordinate fallback ({tap_x}, {center_y})")
            except Exception as e:
                _log.warning(f"post_comment: Fallback tap failed: {e}")
        
        if not post_tapped:
            dm.invalidate_xml_cache()
            return {
                "posted": False,
                "error": "Could not find or tap the Post button after trying all selectors",
                "comment": comment_text,
                "steps": steps,
                "elapsed_ms": _elapsed(),
            }
        
        # =================================================================
        # STEP 4: Verify comment was posted
        # =================================================================
        time.sleep(1.0)
        dm.invalidate_xml_cache()
        
        # Check: comment input should be cleared (empty) after successful post
        comment_input = _find_comment_input()
        verified = False
        if comment_input.exists():
            try:
                info = comment_input.info()
                current_text = info.get("text", "")
                if not current_text or current_text.strip() == "" or "Add a comment" in current_text:
                    verified = True
                    steps.append("verified_posted")
                else:
                    # Text still there — comment might not have been posted
                    steps.append(f"text_still_present: {current_text[:30]}")
                    # Try tapping Post button one more time
                    for selector in POST_BUTTON_SELECTORS[:2]:
                        try:
                            btn = dm.device(**selector)
                            if btn.exists():
                                btn.click()
                                time.sleep(0.8)
                                verified = True
                                steps.append("retried_post_button")
                                break
                        except Exception:
                            continue
            except Exception:
                # Can't read text — assume posted
                verified = True
                steps.append("assumed_posted")
        else:
            # Comment input gone — probably navigated away, comment was posted
            verified = True
            steps.append("input_gone_assumed_posted")
        
        dm.invalidate_xml_cache()
        elapsed = _elapsed()
        
        _log.info(f"post_comment: {'SUCCESS' if verified else 'UNCERTAIN'} — '{comment_text}' in {elapsed}ms")
        
        # Record comment text for session-level dedup (prevents re-use on next post)
        if verified:
            try:
                from .memory_tools import record_comment_text
                record_comment_text(comment_text)
            except Exception:
                pass  # Non-fatal

            # Leave comments layer to avoid getting stuck after hashtag/profile paths.
            try:
                xml_after_post = get_screen_xml()
                ctx_after_post = _detect_screen(xml_after_post.get("xml", "")).context.value
                should_go_back = opened_comments or ctx_after_post in {
                    "instagram_comments",
                    "instagram_search",
                }
                if should_go_back:
                    from lamda.client import Keys
                    dm.device.press_key(Keys.KEY_BACK)
                    _nav_shallower()
                    dm.invalidate_xml_cache()
                    time.sleep(0.4)
                    steps.append("closed_comments_layer")
            except Exception as nav_err:
                _log.debug(f"post_comment: Navigation restore skipped: {nav_err}")
        
        return {
            "posted": verified,
            "comment": comment_text,
            "steps": steps,
            "elapsed_ms": elapsed,
        }
    
    # -------------------------------------------------------------------------
    # Comment Orchestrator (atomic comment pipeline)
    # -------------------------------------------------------------------------
    
    # --- Banned phrases / emojis for validation ---
    _BANNED_PHRASES = {
        "love this", "so true", "gorgeous", "amazing", "beautiful",
        "awesome", "stunning", "obsessed", "queen", "king", "goals",
        "slay", "periodt", "facts", "mood", "vibes", "inspo",
        "needed this", "this >>>", "dead", "i can't", "iconic",
        "great post", "nice pic", "looks good", "so cool", "fire",
        "perfect", "incredible", "wonderful",
    }
    _BANNED_SOLO_EMOJIS = {"💯", "🔥", "😍", "❤️", "👏", "🙌", "✨", "💀", "😻", "💕"}
    
    def _generate_comment_via_gemini(
        screenshot_bytes: bytes | None,
        caption_text: str,
        author_username: str,
        visible_comments: list[str],
        recent_own_comments: list[str],
        is_nurtured: bool = False,
        extra_instruction: str = "",
    ) -> str | None:
        """Internal: Generate comment text via focused Gemini API call.
        
        Uses a minimal prompt (~200 tokens) + optional image for fast,
        specific comment generation. Not exposed as a tool.
        """
        _log = logging.getLogger("eidola.tools.comment_gen")
        
        try:
            import google.genai as genai
            
            parts = []
            
            if screenshot_bytes:
                parts.append(types.Part.from_bytes(
                    data=screenshot_bytes,
                    mime_type="image/jpeg",
                ))
            
            visible_str = "\n".join(f"- {c}" for c in visible_comments[:5]) if visible_comments else "(none visible)"
            recent_str = "\n".join(f"- {c}" for c in recent_own_comments[:7]) if recent_own_comments else "(none yet)"

            prompt = f"""Caption: {caption_text or '(no caption)'}

Comments on this post:
{visible_str}

My recent comments (don't repeat):
{recent_str}

Write ONE Instagram comment. React to the specific thing in the image or caption that caught your eye. Your comment should only make sense on THIS post — if it could work on any photo, discard it.

Sound like a real person texting a friend about what they just saw. Keep it short. Max 15 words. 0-2 emoji.
{extra_instruction}

BANNED (instant bot flag):
love this, amazing, beautiful, gorgeous, stunning, goals, vibes, queen, king, slay, obsessed, iconic, fire, perfect, incredible, wonderful, great post, so true

If the screenshot shows a different user than @{author_username} → WRONG_POST
If no image and no caption → SKIP

Output ONLY the comment. No quotes, no labels."""

            parts.append(types.Part(text=prompt))
            
            # Create client — auto-detects Vertex AI from env vars
            client_kwargs = {}
            if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE":
                client_kwargs["vertexai"] = True
                project = os.environ.get("GOOGLE_CLOUD_PROJECT")
                location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
                if project:
                    client_kwargs["project"] = project
                    client_kwargs["location"] = location
            
            from ..config import settings as _settings
            _comment_model = _settings.comment_model or _settings.default_model
            
            client = genai.Client(**client_kwargs)
            response = client.models.generate_content(
                model=_comment_model,
                contents=[types.Content(role="user", parts=parts)],
                config=types.GenerateContentConfig(
                    temperature=1.0,
                ),
            )
            
            # Clean up model output: strip quotes, markdown, whitespace
            result = response.text.strip() if response.text else None
            if result:
                result = result.strip('"').strip("'").strip("*").strip("`").strip()
            _log.info(f"comment_gen: Gemini → '{result}'")
            return result
            
        except Exception as e:
            _log.error(f"comment_gen: Gemini call failed: {e}")
            return None
    
    def comment_on_post(
        author_username: str,
        timestamp_text: str,
    ) -> dict:
        """🎯 ORCHESTRATOR: Analyze post + generate + post comment in ONE call.
        
        This tool handles the ENTIRE comment pipeline atomically:
        
        Stage 0: Guard — already commented? 24h limit?
        Stage 1: Gather — screenshot + caption + visible comments + recent comments
        Stage 2: Generate — CTA → exact keyword | else → focused AI call with image
        Stage 3: Validate — dedup, banned phrases, specificity
        Stage 4: Post — UI automation (open comments → type → tap Post → verify)
        Stage 5: Record — MongoDB + session memory
        
        The agent decides IF and WHEN to comment. This tool handles HOW.
        
        Args:
            author_username: Post author's @username (e.g., "example_user")
            timestamp_text: Timestamp shown on post (e.g., "3h", "2 days ago")
            
        Returns:
            dict with:
            - posted: True if comment was successfully posted
            - comment_text: The generated/posted comment
            - stages: List of completed stages for debugging
            - skipped: True if post was skipped (already commented, limit, etc.)
            - skip_reason: Why it was skipped (if skipped)
            - elapsed_ms: Total time taken in milliseconds
        """
        _log = logging.getLogger("eidola.tools.comment_orchestrator")
        start_time = time.time()
        stages = []
        
        def _elapsed():
            return int((time.time() - start_time) * 1000)
        
        # =============================================================
        # STAGE 0: GUARD CHECKS
        # =============================================================
        _log.info(f"comment_on_post: START @{author_username} ({timestamp_text})")
        
        # 0-pre: Self-comment guard — never comment on our own posts
        try:
            from .memory_tools import _memory_context
            _ctx = _memory_context.get()
            if _ctx:
                own_username = _ctx.instagram_username.lower().strip().lstrip("@")
                target = author_username.lower().strip().lstrip("@")
                if own_username == target:
                    stages.append("guard:self_post")
                    _log.info(f"comment_on_post: SKIP — @{author_username} is our own account")
                    return {
                        "posted": False, "skipped": True,
                        "skip_reason": f"Cannot comment on your own post (@{author_username})",
                        "stages": stages, "elapsed_ms": _elapsed(),
                    }
        except Exception:
            pass

        # 0a: Nurtured account check (fail-safe — agent should only call for VIP)
        try:
            from .memory_tools import is_nurtured_account
            nurtured_check = is_nurtured_account(author_username)
            if not nurtured_check.get("is_nurtured", False):
                stages.append("guard:not_nurtured")
                _log.warning(f"comment_on_post: @{author_username} is NOT nurtured — skipping")
                return {
                    "posted": False, "skipped": True,
                    "skip_reason": f"@{author_username} is not a nurtured/VIP account",
                    "stages": stages, "elapsed_ms": _elapsed(),
                }
        except Exception as e:
            _log.warning(f"comment_on_post: Nurtured check failed: {e} — BLOCKING comment (safety)")
            return {
                "posted": False, "skipped": True,
                "skip_reason": f"Nurtured check error — cannot verify @{author_username} is VIP",
                "stages": stages, "elapsed_ms": _elapsed(),
            }

        # 0b: Already commented on this post?
        try:
            from .memory_tools import check_post_interaction, get_24h_comment_count
            
            dedup = check_post_interaction(author_username, timestamp_text, action="comment")
            if dedup.get("interacted") or dedup.get("skip"):
                stages.append("guard:already_commented")
                _log.info(f"comment_on_post: SKIP — already commented on @{author_username}")
                return {
                    "posted": False, "skipped": True,
                    "skip_reason": "Already commented on this post",
                    "stages": stages, "elapsed_ms": _elapsed(),
                }
        except Exception as e:
            _log.warning(f"comment_on_post: Dedup check failed: {e}")
            return {
                "posted": False, "skipped": True,
                "skip_reason": f"Dedup check error: {e}",
                "stages": stages, "elapsed_ms": _elapsed(),
            }
        
        # 0b: 24h comment budget
        try:
            budget = get_24h_comment_count()
            if not budget.get("can_comment", False):
                stages.append("guard:24h_limit")
                _log.info(f"comment_on_post: SKIP — 24h limit ({budget.get('count')}/{budget.get('limit')})")
                return {
                    "posted": False, "skipped": True,
                    "skip_reason": f"24h limit: {budget.get('count')}/{budget.get('limit')}",
                    "stages": stages, "elapsed_ms": _elapsed(),
                }
        except Exception as e:
            return {
                "posted": False, "skipped": True,
                "skip_reason": f"Budget check error: {e}",
                "stages": stages, "elapsed_ms": _elapsed(),
            }
        
        # 0c: Session comment budget (MongoDB-backed, survives ContextVar loss)
        try:
            from .memory_tools import get_memory_context
            _ctx = get_memory_context()
            if _ctx and _ctx.session_id:
                _sess_budget = _ctx.memory.get_session_comment_budget(
                    _ctx.account_id, _ctx.session_id
                )
                if not _sess_budget.get("can_comment", True):
                    stages.append("guard:session_limit")
                    _log.info(
                        f"comment_on_post: SKIP — session limit "
                        f"({_sess_budget['comments_done']}/{_sess_budget['comments_limit']})"
                    )
                    return {
                        "posted": False, "skipped": True,
                        "skip_reason": (
                            f"Session comment limit reached: "
                            f"{_sess_budget['comments_done']}/{_sess_budget['comments_limit']}"
                        ),
                        "stages": stages, "elapsed_ms": _elapsed(),
                    }
        except Exception as e:
            _log.warning(f"comment_on_post: Session budget check failed: {e}")
        
        stages.append("guard:passed")
        
        # =============================================================
        # STAGE 1: GATHER CONTEXT (deterministic, no LLM)
        # =============================================================
        
        # 1a: Screenshot
        screenshot_bytes = None
        try:
            img_io = dm.device.screenshot(60)
            screenshot_bytes = img_io.getvalue()
            stages.append(f"gather:screenshot({len(screenshot_bytes) // 1024}KB)")
            _log.info(f"comment_on_post: Screenshot {len(screenshot_bytes) // 1024}KB")
        except Exception as e:
            _log.warning(f"comment_on_post: Screenshot failed: {e}")
            stages.append("gather:screenshot_failed")
        
        # 1b: Caption + CTA detection
        caption_text = ""
        cta_keyword = None
        is_nurtured = False
        try:
            cap = get_caption_info(author_username, expand_if_truncated=True)
            caption_text = cap.get("caption_text", "")
            is_nurtured = cap.get("is_nurtured", False)
            cta_keyword = cap.get("cta_keyword")
            stages.append(f"gather:caption({len(caption_text)}ch,CTA={cta_keyword or 'none'})")
            _log.info(f"comment_on_post: Caption={caption_text[:60]}… CTA={cta_keyword} nurtured={is_nurtured}")
        except Exception as e:
            _log.warning(f"comment_on_post: Caption failed: {e}")
            stages.append("gather:caption_failed")
        
        # 1c: Open comments section so we can see existing comments (SPATIAL VERIFICATION)
        comments_opened = False
        try:
            buttons = get_post_engagement_buttons(author_username)
            if not buttons.get("found"):
                stages.append("gather:post_not_visible")
                _log.warning(
                    f"comment_on_post: Post @{author_username} not visible — {buttons.get('error', 'unknown')}. "
                    "Agent should scroll_feed() to bring post into view."
                )
                return {
                    "posted": False,
                    "skipped": True,
                    "skip_reason": buttons.get("error", "Post not visible on screen"),
                    "note": "Scroll feed to bring target post into view, then retry",
                    "visible_users": buttons.get("visible_users", []),
                    "stages": stages,
                    "elapsed_ms": _elapsed(),
                }
            comment_btn_coords = buttons.get("comment_button")
            if comment_btn_coords:
                from lamda.client import Point as _P
                dm.device.click(_P(x=comment_btn_coords["x"], y=comment_btn_coords["y"]))
                dm.invalidate_xml_cache()
                time.sleep(2.5)
                comments_opened = True
                stages.append("opened_comments_for_dedup")
                _log.info("comment_on_post: Opened comments section for visual dedup (spatial-verified)")
            else:
                stages.append("comment_btn_not_found")
                _log.debug("comment_on_post: Comment button not found — visual dedup will have limited data")
        except Exception as e:
            _log.debug(f"comment_on_post: Failed to open comments: {e}")

        # 1d: Read visible comments (now from comments screen)
        visible_comments: list[str] = []
        visible_comments_raw: list[dict] = []
        try:
            vc = get_visible_comments(author_username, max_comments=7)
            if vc.get("found"):
                visible_comments_raw = vc.get("comments", [])
                visible_comments = [c.get("text", "") for c in visible_comments_raw]
            stages.append(f"gather:visible({len(visible_comments)})")
            if comments_opened and not visible_comments_raw:
                _log.warning("comment_on_post: comments_opened=True but 0 visible comments — screen may not have loaded")
        except Exception as e:
            _log.debug(f"comment_on_post: Visible comments failed: {e}")
            stages.append("gather:visible_failed")

        # 1e-visual: Check if our own comment is already visible
        try:
            from .memory_tools import get_instagram_username
            our_username = get_instagram_username()
            if not our_username:
                _log.warning("comment_on_post: VISUAL DEDUP — get_instagram_username() returned None, trying module fallback")
                from .memory_tools import _username_fallback
                our_username = _username_fallback.get("username")
            if our_username and visible_comments_raw:
                our_clean = our_username.lower().strip().lstrip("@")
                for c in visible_comments_raw:
                    comment_author = c.get("username", "").lower().strip().lstrip("@")
                    if comment_author == our_clean:
                        stages.append(f"guard:visual_dedup({our_username})")
                        _log.info(
                            f"comment_on_post: VISUAL DEDUP — "
                            f"our comment already visible by @{our_username}: '{c.get('text', '')[:50]}'"
                        )
                        return {
                            "posted": False, "skipped": True,
                            "skip_reason": f"Our comment already visible on screen (by @{our_username})",
                            "our_visible_comment": c.get("text", ""),
                            "stages": stages, "elapsed_ms": _elapsed(),
                        }
            elif not our_username:
                _log.warning("comment_on_post: VISUAL DEDUP FULLY SKIPPED — no username available")
        except Exception as e:
            _log.warning(f"comment_on_post: Visual dedup check failed: {e}")
        
        # 1f: Recent own comments (DB + session)
        recent_own: list[str] = []
        try:
            from .memory_tools import get_recent_comments, get_session_comment_texts
            rc = get_recent_comments(limit=5)
            for c in rc.get("comments", []):
                txt = c.get("comment_text", str(c)) if isinstance(c, dict) else str(c)
                if txt:
                    recent_own.append(txt)
            for st in get_session_comment_texts():
                if st and st not in recent_own:
                    recent_own.append(st)
            stages.append(f"gather:recent_own({len(recent_own)})")
        except Exception as e:
            _log.debug(f"comment_on_post: Recent comments failed: {e}")
            stages.append("gather:recent_failed")
        
        # =============================================================
        # STAGE 2: GENERATE COMMENT TEXT
        # =============================================================
        
        # 2pre: Filter out Instagram UI text masquerading as captions
        _UI_JUNK = {
            "tap to watch more reels", "posted a photo", "posted a reel",
            "suggested for you", "sponsored", "see translation",
            "view all comments", "add a comment",
        }
        if caption_text and caption_text.strip().lower() in _UI_JUNK:
            _log.debug(f"comment_on_post: Filtered UI junk caption: '{caption_text}'")
            caption_text = ""
        
        # 2a: CTA shortcut (no LLM needed)
        if cta_keyword and is_nurtured:
            comment_text = cta_keyword
            stages.append(f"generate:cta({cta_keyword})")
            _log.info(f"comment_on_post: CTA → '{cta_keyword}'")
        
        # 2b: Insufficient context check
        elif not screenshot_bytes and (not caption_text or caption_text.strip().lower() in ("", "posted a photo", "posted a reel")):
            stages.append("generate:insufficient_context")
            _log.warning("comment_on_post: No screenshot + no real caption → skip")
            return {
                "posted": False, "skipped": True,
                "skip_reason": "Insufficient context (no screenshot, no caption)",
                "stages": stages, "elapsed_ms": _elapsed(),
            }
        
        # 2c: Focused Gemini call
        else:
            comment_text = _generate_comment_via_gemini(
                screenshot_bytes=screenshot_bytes,
                caption_text=caption_text,
                author_username=author_username,
                visible_comments=visible_comments,
                recent_own_comments=recent_own,
                is_nurtured=is_nurtured,
            )
            
            if comment_text and comment_text.strip().upper() == "WRONG_POST":
                stages.append("generate:wrong_post")
                _log.warning(f"comment_on_post: WRONG_POST — screenshot shows different post than @{author_username}")
                return {
                    "posted": False, "skipped": True,
                    "skip_reason": f"Visual verification: screenshot shows different post than @{author_username}",
                    "stages": stages, "elapsed_ms": _elapsed(),
                }
            
            if not comment_text or comment_text.strip().upper() == "SKIP":
                stages.append("generate:ai_skip")
                return {
                    "posted": False, "skipped": True,
                    "skip_reason": "AI decided to skip (low-signal post)",
                    "stages": stages, "elapsed_ms": _elapsed(),
                }
            
            stages.append(f"generate:ai('{comment_text[:40]}')")
            _log.info(f"comment_on_post: AI → '{comment_text}'")
        
        # =============================================================
        # STAGE 3: VALIDATE (programmatic, not LLM)
        # =============================================================
        MAX_RETRIES = 2
        
        for validation_round in range(MAX_RETRIES + 1):
            normalized = comment_text.strip().lower()
            # Strip markdown/quote decorators that Gemini might add
            comment_text = comment_text.strip().strip('"').strip("'").strip("*").strip("`")
            normalized = comment_text.strip().lower()
            reject_reason = None
            
            # 3a: Banned phrases (substring match — catches "love this post", "so true!!", etc.)
            if any(bp in normalized for bp in _BANNED_PHRASES):
                matched = next(bp for bp in _BANNED_PHRASES if bp in normalized)
                reject_reason = f"contains banned phrase '{matched}' in: '{comment_text}'"
            
            # 3b: Banned solo emojis
            elif comment_text.strip() in _BANNED_SOLO_EMOJIS:
                reject_reason = f"banned solo emoji: '{comment_text}'"
            
            # 3c: Session dedup
            else:
                try:
                    from .memory_tools import check_comment_text_duplicate
                    if check_comment_text_duplicate(comment_text):
                        reject_reason = f"duplicate in session: '{comment_text}'"
                except Exception:
                    pass
            
            # 3d: Matches recent own comment
            if not reject_reason:
                for rc in recent_own:
                    if rc.strip().lower() == normalized:
                        reject_reason = f"matches recent: '{rc}'"
                        break
            
            # 3e: Matches visible comment
            if not reject_reason:
                for vc in visible_comments:
                    if vc.strip().lower() == normalized:
                        reject_reason = f"matches visible: '{vc}'"
                        break
            
            if not reject_reason:
                stages.append("validate:passed")
                break
            
            # Retry generation
            if validation_round < MAX_RETRIES:
                _log.warning(f"comment_on_post: Validation failed ({reject_reason}), retry {validation_round + 1}")
                stages.append(f"validate:retry({reject_reason[:30]})")
                comment_text = _generate_comment_via_gemini(
                    screenshot_bytes=screenshot_bytes,
                    caption_text=caption_text,
                    author_username=author_username,
                    visible_comments=visible_comments,
                    recent_own_comments=recent_own + [comment_text],
                    is_nurtured=is_nurtured,
                    extra_instruction=f"AVOID: '{comment_text}' — rejected ({reject_reason}). Write completely different.",
                )
                if comment_text and comment_text.strip().upper() == "WRONG_POST":
                    stages.append("validate:wrong_post_on_retry")
                    return {
                        "posted": False, "skipped": True,
                        "skip_reason": f"Visual verification: screenshot shows different post than @{author_username}",
                        "stages": stages, "elapsed_ms": _elapsed(),
                    }
                if not comment_text or comment_text.strip().upper() == "SKIP":
                    return {
                        "posted": False, "skipped": True,
                        "skip_reason": f"Retry produced no valid comment ({reject_reason})",
                        "stages": stages, "elapsed_ms": _elapsed(),
                    }
            else:
                _log.warning(f"comment_on_post: Validation failed after {MAX_RETRIES} retries")
                return {
                    "posted": False, "skipped": True,
                    "skip_reason": f"Cannot generate valid comment after {MAX_RETRIES} retries: {reject_reason}",
                    "stages": stages, "elapsed_ms": _elapsed(),
                }
        
        # =============================================================
        # STAGE 4: POST via UI automation (reuse post_comment, skip redundant guards)
        # =============================================================
        post_result = post_comment(comment_text, max_retries=2, _skip_guards=True)
        
        if not post_result.get("posted"):
            stages.append(f"post:fail({post_result.get('error', '?')[:40]})")
            _log.warning(f"comment_on_post: UI posting failed: {post_result.get('error')}")
            return {
                "posted": False,
                "comment_text": comment_text,
                "post_error": post_result.get("error"),
                "stages": stages, "elapsed_ms": _elapsed(),
            }
        
        stages.append("post:ok")

        # Safety net: if UI still sits in comments/search, step back once.
        try:
            xml_after_post = get_screen_xml()
            ctx_after_post = _detect_screen(xml_after_post.get("xml", "")).context.value
            if ctx_after_post in {"instagram_comments", "instagram_search"}:
                from lamda.client import Keys
                dm.device.press_key(Keys.KEY_BACK)
                _nav_shallower()
                dm.invalidate_xml_cache()
                time.sleep(0.4)
                stages.append(f"post_cleanup:back_from_{ctx_after_post}")
        except Exception as e:
            _log.debug(f"comment_on_post: post-cleanup check skipped: {e}")
        
        # =============================================================
        # STAGE 5: RECORD to MongoDB + session memory
        # =============================================================
        try:
            from .memory_tools import record_post_interaction
            rec = record_post_interaction(
                author_username=author_username,
                timestamp_text=timestamp_text,
                action="comment",
                comment_text=comment_text,
            )
            stages.append(f"record:{'ok' if rec.get('recorded') else 'fail'}")
        except Exception as e:
            _log.warning(f"comment_on_post: Recording failed: {e}")
            stages.append("record:error")
        
        # Increment session comment counter (MongoDB-backed)
        try:
            from .memory_tools import get_memory_context
            _ctx = get_memory_context()
            if _ctx and _ctx.session_id:
                _ctx.memory.increment_session_comments(_ctx.account_id, _ctx.session_id)
        except Exception as e:
            _log.warning(f"comment_on_post: Session counter increment failed: {e}")
        
        elapsed = _elapsed()
        _log.info(f"comment_on_post: ✅ SUCCESS '{comment_text}' on @{author_username} in {elapsed}ms")
        
        return {
            "posted": True,
            "comment_text": comment_text,
            "author": author_username,
            "stages": stages,
            "elapsed_ms": elapsed,
        }
    
    # -------------------------------------------------------------------------
    # Save & Share
    # -------------------------------------------------------------------------
    
    def save_post(target_username: str) -> dict:
        """Save (bookmark) a post by the given user.
        
        Finds the correct save button for this user's post via spatial matching,
        checks if already saved, and taps if not.
        
        Args:
            target_username: Post author username (required for spatial verification)
            
        Returns:
            dict with saved status
        """
        if not target_username:
            return {"saved": False, "error": "target_username is required"}
        
        saved_check = is_post_saved(target_username)
        if saved_check.get("is_saved"):
            return {
                "saved": False,
                "already_saved": True,
                "note": f"Post by @{target_username} is already saved",
            }
        
        if not saved_check.get("save_button_found"):
            buttons = get_post_engagement_buttons(target_username)
            if not buttons.get("found") or not buttons.get("save_button"):
                return {
                    "saved": False,
                    "error": f"Save button for @{target_username} not found",
                    "note": "Scroll to reveal buttons first",
                }
            btn = buttons["save_button"]
            if btn.get("is_saved"):
                return {"saved": False, "already_saved": True}
            from lamda.client import Point as _P; dm.device.click(_P(x=btn["x"], y=btn["y"]))
        else:
            bounds = saved_check.get("save_button_bounds")
            if bounds:
                cx = (bounds[0] + bounds[2]) // 2
                cy = (bounds[1] + bounds[3]) // 2
                from lamda.client import Point as _P; dm.device.click(_P(x=cx, y=cy))
            else:
                return {"saved": False, "error": "Save button bounds missing"}
        
        dm.invalidate_xml_cache()
        time.sleep(random.uniform(0.8, 1.4))

        # Dismiss "Save to collection" bottom sheet that Instagram shows after tapping save.
        from lamda.client import Keys as _Keys
        dm.device.press_key(_Keys.KEY_BACK)
        dm.invalidate_xml_cache()
        time.sleep(random.uniform(0.2, 0.5))

        verify = is_post_saved(target_username)
        return {
            "saved": verify.get("is_saved", False),
            "username": target_username,
            "_nav_hint": _get_nav_hint(),
        }
    
    def share_post(target_username: str) -> dict:
        """Share a post by tapping the share/send button.
        
        Opens the Instagram share sheet for the post. After this, you can:
        - Tap "Add post to your story" to repost to story
        - Tap a DM contact to share via DM
        - Press back to cancel
        
        Args:
            target_username: Post author username (required for spatial verification)
            
        Returns:
            dict with share sheet status
        """
        if not target_username:
            return {"shared": False, "error": "target_username is required"}
        
        buttons = get_post_engagement_buttons(target_username)
        if not buttons.get("found"):
            return {
                "shared": False,
                "error": f"Buttons for @{target_username} not found",
                "note": "Scroll to reveal buttons first",
            }
        
        share_btn = buttons.get("share_button")
        if not share_btn:
            return {
                "shared": False,
                "error": f"Share button for @{target_username} not visible",
            }
        
        from lamda.client import Point as _P; dm.device.click(_P(x=share_btn["x"], y=share_btn["y"]))
        dm.invalidate_xml_cache()
        time.sleep(0.5)
        
        # Check if share sheet opened — look for "Add post to your story" or share options
        add_to_story = dm.device(textContains="Add post to your story")
        share_sheet_open = add_to_story.exists()
        
        if not share_sheet_open:
            # Try alternative: "Share to..." or send_bar
            share_sheet_open = dm.device(resourceId="com.instagram.android:id/action_bar_title").exists()
        
        _nav_deeper()
        return {
            "shared": True,
            "share_sheet_open": share_sheet_open,
            "username": target_username,
            "note": "Share sheet open. Tap 'Add post to your story' to repost, or press_back() to cancel.",
            "_nav_hint": _get_nav_hint(),
        }
    
    # -------------------------------------------------------------------------
    # Follow / Unfollow (nurtured only)
    # -------------------------------------------------------------------------
    
    def follow_nurtured_account(target_username: str) -> dict:
        """Follow a nurtured account if not already following.
        
        SAFETY: Only works for accounts in the nurtured list.
        Call this when visiting a nurtured profile — it checks if you're
        already following and taps Follow if not.
        
        Must be on the target user's profile page when calling this.
        
        Args:
            target_username: Instagram username of the nurtured account
            
        Returns:
            dict with followed (bool), already_following (bool), or error
        """
        from .memory_tools import is_nurtured_account
        
        try:
            check = is_nurtured_account(target_username)
            if not check.get("is_nurtured", False):
                logger.warning(f"follow_nurtured_account: @{target_username} is NOT nurtured — blocked")
                return {
                    "followed": False,
                    "error": f"@{target_username} is not a nurtured account — follow blocked",
                    "blocked": True,
                }
        except Exception as e:
            logger.warning(f"follow_nurtured_account: nurtured check failed: {e} — blocked for safety")
            return {"followed": False, "error": f"Nurtured check failed: {e}", "blocked": True}
        
        FOLLOW_BTN_ID = "com.instagram.android:id/profile_header_follow_button"
        
        btn = dm.device(resourceId=FOLLOW_BTN_ID)
        if not btn.exists():
            return {
                "followed": False,
                "already_following": None,
                "note": "Follow button not found — may not be on a profile page",
            }
        
        btn_text = btn.get_text() or ""
        
        if btn_text.strip().lower() in ("following", "requested"):
            return {
                "followed": False,
                "already_following": True,
                "note": f"Already following @{target_username}" if btn_text.strip().lower() == "following" else f"Follow request pending for @{target_username}",
            }
        
        if btn_text.strip().lower() in ("follow", "follow back"):
            btn.click()
            dm.invalidate_xml_cache()
            time.sleep(0.8)
            
            # Check if follow went through immediately
            verify_btn = dm.device(resourceId=FOLLOW_BTN_ID)
            if verify_btn.exists() and (verify_btn.get_text() or "").strip().lower() == "following":
                logger.info(f"✅ Followed nurtured @{target_username} (direct)")
                return {
                    "followed": True, "already_following": False,
                    "verified": True, "note": f"Now following @{target_username}",
                }
            
            # Instagram may show "Review this account before following" overlay.
            # Try multiple strategies to find and tap the confirmation Follow button.
            logger.info("Follow not verified — checking for review overlay")
            
            # Save XML dump for debugging
            try:
                xml_bytes = dm.device.dump_window_hierarchy()
                overlay_xml = xml_bytes.getvalue().decode("utf-8")
                _save_xml_dump(overlay_xml, "follow_overlay")
            except Exception:
                overlay_xml = ""
            
            overlay_confirmed = False
            from lamda.client import Keys
            for _confirm_try in range(5):
                dm.invalidate_xml_cache()
                
                # Check if follow already went through
                verify_btn = dm.device(resourceId=FOLLOW_BTN_ID)
                if verify_btn.exists():
                    btn_state = (verify_btn.get_text() or "").strip().lower()
                    if btn_state in ("following", "requested"):
                        overlay_confirmed = True
                        break
                
                # Overlay buttons have text="" but content-desc="Follow"/"Cancel"
                cancel_btn = dm.device(description="Cancel")
                if cancel_btn.exists():
                    logger.info(f"📋 Review overlay detected (try {_confirm_try + 1})")
                    confirm_btn = dm.device(description="Follow", className="android.widget.Button")
                    if confirm_btn.exists():
                        confirm_btn.click()
                        dm.invalidate_xml_cache()
                        time.sleep(0.8)
                        overlay_confirmed = True
                        logger.info("📋 Tapped overlay Follow confirmation")
                        break
                    # Follow button not rendered yet — wait and retry (do NOT press Back)
                    logger.info("📋 Cancel visible but Follow button not found yet — waiting")
                
                time.sleep(0.5)
            
            verify_btn = dm.device(resourceId=FOLLOW_BTN_ID)
            btn_state = (verify_btn.get_text() or "").strip().lower() if verify_btn.exists() else ""
            verified = btn_state in ("following", "requested")
            
            logger.info(f"{'✅' if verified else '⚠️'} Follow @{target_username}: verified={verified}, state='{btn_state}'")
            return {
                "followed": verified,
                "already_following": False,
                "verified": verified,
                "note": f"{'Now following' if verified else 'Follow attempted but not verified for'} @{target_username}",
            }
        
        return {
            "followed": False,
            "already_following": None,
            "note": f"Button text is '{btn_text}' — not a standard Follow state",
            "button_text": btn_text,
        }
    
    # -------------------------------------------------------------------------
    # Element Interaction (using selectors)
    # -------------------------------------------------------------------------
    
    def tap_element(
        text: str | None = None,
        resource_id: str | None = None,
        content_desc: str | None = None,
    ) -> dict:
        """Tap element by selector.
        
        Provide at least one selector:
        - text: Element's visible text
        - resource_id: Element's resourceId (e.g., "com.instagram.android:id/feed_image")
        - content_desc: Element's content description (accessibility)
        
        Args:
            text: Match by text
            resource_id: Match by resourceId
            content_desc: Match by contentDescription
            
        Returns:
            dict with success status
        """
        # Block dangerous actions that the agent should NEVER perform
        _blocked_texts = {"follow", "unfollow", "remove", "block", "restrict", "report", "delete"}
        _blocked_ids = {"profile_header_follow_button", "follow_button"}
        for val in [text, content_desc]:
            if val and val.strip().lower() in _blocked_texts:
                logger.warning(f"tap_element BLOCKED: attempted to tap '{val}' — forbidden action")
                return {"tapped": False, "error": f"Action '{val}' is blocked for safety", "blocked": True}
        if resource_id:
            for bid in _blocked_ids:
                if bid in resource_id.lower():
                    logger.warning(f"tap_element BLOCKED: attempted to tap resourceId containing '{bid}'")
                    return {"tapped": False, "error": f"Follow/Unfollow actions are blocked", "blocked": True}
        
        # Build selector from provided args
        selector_kwargs = {}
        if text:
            selector_kwargs["text"] = text
        if resource_id:
            selector_kwargs["resourceId"] = resource_id
        if content_desc:
            selector_kwargs["description"] = content_desc
        
        if not selector_kwargs:
            return {"error": "Provide at least one selector (text, resource_id, or content_desc)"}
        
        # FIRERPA uses d(**kwargs) directly, not d(Selector(...))
        element = dm.device(**selector_kwargs)
        
        if element.exists():
            element.click()
            dm.invalidate_xml_cache()
            _nav_deeper()
            return {"tapped": True, "selector": selector_kwargs, "_nav_hint": _get_nav_hint()}
        else:
            return {"tapped": False, "error": "Element not found", "selector": selector_kwargs, "_nav_hint": _get_nav_hint()}
    
    def element_exists(
        text: str | None = None,
        resource_id: str | None = None,
        content_desc: str | None = None,
    ) -> dict:
        """Check if element exists on screen.
        
        Args:
            text: Match by text
            resource_id: Match by resourceId
            content_desc: Match by contentDescription
            
        Returns:
            dict with exists status
        """
        selector_kwargs = {}
        if text:
            selector_kwargs["text"] = text
        if resource_id:
            selector_kwargs["resourceId"] = resource_id
        if content_desc:
            selector_kwargs["description"] = content_desc
        
        if not selector_kwargs:
            return {"error": "Provide at least one selector"}
        
        # FIRERPA uses d(**kwargs) directly
        exists = dm.device(**selector_kwargs).exists()
        
        return {"exists": exists, "selector": selector_kwargs}
    
    # -------------------------------------------------------------------------
    # Utility Tools
    # -------------------------------------------------------------------------
    
    def wait_for_idle(timeout_ms: int = 800) -> dict:
        """Wait for UI to become idle (animations/loading complete).
        
        Args:
            timeout_ms: Maximum wait time in milliseconds (default 800ms for speed)
            
        Returns:
            dict with status
        """
        dm.device.wait_for_idle(timeout=timeout_ms)
        return {"idle": True, "timeout_ms": timeout_ms}
    
    def device_info() -> dict:
        """Get device information.
        
        Returns:
            dict with device model, screen size, Android version, etc.
        """
        info = dm.device.device_info()
        return {
            "model": getattr(info, "productModel", "unknown"),
            "brand": getattr(info, "productBrand", "unknown"),
            "android_version": getattr(info, "androidVersion", "unknown"),
            "screen_width": dm.screen_width,
            "screen_height": dm.screen_height,
        }
    
    def check_connection() -> dict:
        """Check if device connection is healthy.
        
        Returns:
            dict with connection status
        """
        healthy = dm.health_check()
        if not healthy:
            # Try reconnect
            healthy = dm.reconnect()
        
        return {
            "connected": healthy,
            "device_ip": dm.device_ip,
        }
    
    # -------------------------------------------------------------------------
    # Build tool list
    # -------------------------------------------------------------------------
    
    return [
        # Screen (XML-FIRST!)
        FunctionTool(get_screen_elements),  # PRIMARY: Compressed format for fast navigation
        FunctionTool(get_screen_xml),       # Fallback: Full XML for debugging/detailed analysis
        FunctionTool(screenshot),           # Only for visual analysis
        
        # XML-based Navigation (NEW)
        FunctionTool(detect_screen),
        FunctionTool(analyze_feed_posts),  # 🚀 SUPER-TOOL: Replaces 5 observation tools in 1 call
        FunctionTool(find_element),
        FunctionTool(is_post_liked),
        FunctionTool(is_post_saved),
        FunctionTool(check_post_liked),  # Simple: check if post liked from XML
        FunctionTool(get_post_engagement_buttons),
        FunctionTool(get_caption_info),  # Get full caption text, expand if truncated
        FunctionTool(get_visible_comments),  # Get visible comments on a post
        FunctionTool(get_elements_for_ai),
        FunctionTool(open_instagram),
        FunctionTool(force_close_instagram),
        FunctionTool(restart_instagram),
        FunctionTool(handle_dialog),
        FunctionTool(escape_to_instagram),
        
        # Touch
        FunctionTool(tap),
        FunctionTool(long_press),
        FunctionTool(double_tap_like),  # GramAddict-style double tap (human-like)
        FunctionTool(save_post),        # Bookmark a post
        FunctionTool(share_post),       # Open share sheet (repost to story / DM)
        FunctionTool(follow_nurtured_account),  # Auto-follow nurtured accounts

        # Human-like scrolling (SimpleGestures - tested!)
        FunctionTool(scroll_feed),
        FunctionTool(scroll_fast),
        # scroll_burst removed - agent was overusing it, skipping VIP posts
        FunctionTool(scroll_slow_browse),
        FunctionTool(watch_media),  # GramAddict-style view duration
        FunctionTool(scroll_back),
        FunctionTool(refresh_feed),
        FunctionTool(scroll_to_post_buttons),  # Smart scroll with ±15% variability
        
        # Story watching (GramAddict pattern)
        FunctionTool(watch_stories),  # Human-like story viewing
        
        # Post type detection and carousel handling
        FunctionTool(detect_post_type),  # Detect post type (video/carousel/photo) + nurtured status
        FunctionTool(detect_carousel),   # Check if post is carousel
        FunctionTool(swipe_carousel),    # Swipe through carousel + capture screenshots
        
        # Navigation
        FunctionTool(press_back),
        FunctionTool(press_home),
        FunctionTool(press_recent),
        FunctionTool(open_notification_panel),
        
        # Text & Comments
        FunctionTool(type_text),
        FunctionTool(clear_text),
        # post_comment is INTERNAL ONLY — called by comment_on_post with _skip_guards=True
        FunctionTool(comment_on_post),  # 🎯 ORCHESTRATOR: analyze + generate + post + record in 1 call
        
        # Element interaction
        FunctionTool(tap_element),
        FunctionTool(element_exists),
        
        # Utility
        FunctionTool(wait_for_idle),
        FunctionTool(device_info),
        FunctionTool(check_connection),
    ]

    global _type_text_ref
    _type_text_ref = type_text


# =============================================================================
# Role-specific tool subsets
# =============================================================================

def create_navigator_tools(device_ip: str) -> list[FunctionTool]:
    """Tools for Navigator agent (XML-first navigation + scrolling).
    
    Navigator uses XML ONLY - NO screenshots!
    - get_screen_xml() for understanding current state
    - detect_screen() for quick context check
    - find_element() for known Instagram elements
    """
    all_tools = create_firerpa_tools(device_ip)
    
    navigator_names = {
        # XML-first (primary) - NO SCREENSHOTS for Navigator!
        "get_screen_elements",  # PRIMARY: Compressed format (~80% fewer tokens)
        "get_screen_xml",       # Fallback: Full XML if compressed not enough
        "detect_screen", "find_element", "get_elements_for_ai",
        
        # Recovery (full set - Navigator is primary recovery agent)
        "open_instagram", "force_close_instagram", "restart_instagram",
        "handle_dialog", "escape_to_instagram",
        
        # NOTE: screenshot REMOVED - Navigator uses XML only!
        
        # Actions
        "tap", "tap_element", "element_exists",
        "scroll_feed", "scroll_fast", "scroll_slow_browse", "scroll_back", "refresh_feed",
        "scroll_to_post_buttons",  # Smart scroll to center buttons
        "press_back", "press_home", "press_recent",
        
        # Text input (needed for search)
        "type_text", "clear_text",
        
        # Utility
        "wait_for_idle", "check_connection",
        
        # NOTE: NO Action Budget tools! Navigator should NOT manage budget.
        # Orchestrator manages budget. Navigator just navigates and transfers back.
    }
    
    return [t for t in all_tools if t.name in navigator_names]


def create_observer_tools(device_ip: str) -> list[FunctionTool]:
    """Tools for Observer agent (screen analysis - XML + visual).
    
    Observer analyzes screen content:
    - get_screen_xml() for structure
    - screenshot() for visual content (posts, images)
    
    Also has basic recovery tools for self-recovery from dialogs/system UI.
    """
    all_tools = create_firerpa_tools(device_ip)
    
    observer_names = {
        # Analysis
        "get_screen_elements",  # Compressed format for quick analysis
        "get_screen_xml",       # Full XML for detailed analysis
        "detect_screen", "find_element", "get_elements_for_ai",
        "screenshot",  # For visual content analysis
        "is_post_liked",  # Check if already liked
        "is_post_saved",  # Check if already saved
        "get_post_engagement_buttons",  # Get correct button coords for specific post
        "get_caption_info",  # Get full caption text, expand if truncated

        # Read-only checks
        "element_exists", "device_info",
        
        # Limited interaction (to expand/view more content for analysis)
        "tap_element",  # To tap "more" to see full caption, expand comments
        "scroll_feed",  # To scroll and see more content/comments
        "scroll_back",  # To return to previously viewed content
        "scroll_slow_browse",  # To carefully examine post/carousel
        "scroll_to_post_buttons",  # Smart scroll to position post for analysis
        
        # Post type and carousel detection
        "detect_post_type",  # Detect post type (video/carousel/photo)
        "detect_carousel",   # Check if post is carousel
        
        # Recovery (basic set for self-recovery)
        "handle_dialog", "press_back", "escape_to_instagram",
        
        # Utility
        "wait_for_idle",
    }
    
    return [t for t in all_tools if t.name in observer_names]


def create_engager_tools(device_ip: str) -> list[FunctionTool]:
    """Tools for Engager agent (interactions + text input).
    
    Engager performs actions:
    - Uses XML to find interaction elements
    - Uses screenshot to analyze content for comments
    
    Also has recovery tools for handling dialogs during interaction.
    """
    all_tools = create_firerpa_tools(device_ip)
    
    engager_names = {
        # Analysis
        "get_screen_xml", "detect_screen", "find_element", "get_elements_for_ai",
        "screenshot",  # For content-aware comments
        "is_post_liked",  # Check before liking
        "is_post_saved",  # Check before saving
        "get_post_engagement_buttons",  # Get correct button coords for specific post
        "get_caption_info",  # Get full caption for CTA detection

        # Recovery (for handling interruptions during engagement)
        "handle_dialog", "escape_to_instagram", "press_back",
        
        # Interactions
        "tap", "tap_element", "long_press",
        "type_text", "clear_text",
        "post_comment",  # UI: type + post (used internally)
        "comment_on_post",  # 🎯 ORCHESTRATOR: full comment pipeline in 1 call
        "scroll_feed", "scroll_back", "scroll_slow_browse",
        "scroll_to_post_buttons",  # Smart scroll to center buttons
        
        # Post type and carousel handling
        "detect_post_type",  # Detect post type (video/carousel/photo)
        "detect_carousel",   # Check if post is carousel
        "swipe_carousel",    # Swipe through carousel + batch screenshots
        
        # Utility
        "wait_for_idle", "element_exists",
    }
    
    return [t for t in all_tools if t.name in engager_names]


def create_login_tools(device_ip: str) -> list[FunctionTool]:
    """Minimal tools for login mode only.
    
    Login mode needs UI navigation and text input — no feed analysis,
    engagement, scrolling, or post-type detection tools.
    Reduces token overhead from tool definitions significantly.
    """
    all_tools = create_firerpa_tools(device_ip)
    
    login_names = {
        # Screen observation
        "detect_screen",
        "get_screen_elements",
        "get_screen_xml",
        
        # Basic interaction
        "tap",
        "type_text",
        "wait_for_idle",
        
        # Instagram lifecycle
        "open_instagram",
        "escape_to_instagram",
        "handle_dialog",
        
        # Visual feedback + navigation
        "screenshot",
        "press_back",
        "press_home",
    }
    
    return [t for t in all_tools if t.name in login_names]


def create_unified_tools(device_ip: str) -> list[FunctionTool]:
    """All tools for the unified Instagram agent.
    
    Merges Navigator + Observer + Engager tools into one set.
    Used by the new unified agent architecture.
    """
    all_tools = create_firerpa_tools(device_ip)
    dm = DeviceManager.get(device_ip)

    # =========================================================================
    # Internal helper — fresh XML + screen detection in one call
    # =========================================================================

    def _fresh_xml() -> tuple[str | None, str, bool]:
        """Get fresh XML dump with screen context detection.

        Returns:
            (xml_str, screen_context_value, in_instagram)
            xml_str is None when XML is invalid / unreachable.
        """
        try:
            xml_bytes = dm.device.dump_window_hierarchy()
            xml_str = xml_bytes.getvalue().decode("utf-8")
            if len(xml_str) < MIN_XML_SIZE:
                return None, "unknown", False
            ET.fromstring(xml_str)
            screen_result = _detect_screen(xml_str)
            dm.set_xml_cache(xml_str)
            return xml_str, screen_result.context.value, is_in_instagram(xml_str)
        except Exception:
            return None, "unknown", False

    # =========================================================================
    # COMPOUND TOOL 1: navigate_to_profile
    # =========================================================================

    def navigate_to_profile(username: str) -> dict:
        """Navigate to a user's Instagram profile from anywhere in ~1 call.

        Replaces ~15 individual tool calls. Full workflow:
        1. Ensure Instagram is in foreground (launch if needed)
        2. Press back to reach a screen with bottom navigation (max 3)
        3. Tap the Search tab
        4. Focus search input, clear existing text, type the username
        5. Wait for search results and tap the matching user
        6. Wait for profile to load and return result

        Args:
            username: Instagram username to navigate to (without @)

        Returns:
            dict with success, username, screen_context, profile_info,
            steps (audit trail), and _nav_hint
        """
        _log = logging.getLogger("eidola.tools")
        username = username.lstrip("@").strip()
        steps: list[str] = []

        if not username:
            return {"success": False, "error": "username is required", "_nav_hint": _get_nav_hint()}

        try:
            from lamda.client import Point as FPoint, Keys
            from lamda.const import FLAG_ACTIVITY_NEW_TASK, FLAG_ACTIVITY_CLEAR_TOP
            import re

            # ----- 1. Ensure Instagram foreground -----
            app = dm.device.application("com.instagram.android")
            if not app.is_foreground():
                _log.info("navigate_to_profile: launching Instagram")
                try:
                    dm.device.start_activity(
                        action="android.intent.action.MAIN",
                        category="android.intent.category.LAUNCHER",
                        component="com.instagram.android/com.instagram.mainactivity.LauncherActivity",
                        flags=FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_CLEAR_TOP,
                    )
                except Exception:
                    app.start()
                dm.device.wait_for_idle(timeout=800)
                dm.invalidate_xml_cache()
                time.sleep(1.0)
                steps.append("launched_instagram")
            else:
                steps.append("instagram_already_foreground")

            # ----- 2. Back-navigate until bottom nav (search tab) is visible -----
            SEARCH_TAB_ID = "com.instagram.android:id/search_tab"
            xml_str, screen_ctx, in_insta = _fresh_xml()
            if not in_insta:
                return {
                    "success": False, "error": "Not in Instagram after launch",
                    "screen_context": screen_ctx, "steps": steps,
                    "_nav_hint": _get_nav_hint(),
                }

            for i in range(6):
                if dm.device(resourceId=SEARCH_TAB_ID).exists():
                    break
                _log.info(f"navigate_to_profile: back {i+1}/6 — looking for bottom nav (current: {screen_ctx})")
                dm.device.press_key(Keys.KEY_BACK)
                dm.invalidate_xml_cache()
                time.sleep(0.6)
                xml_str, screen_ctx, in_insta = _fresh_xml()
                steps.append(f"back_{i+1}:{screen_ctx}")
                if not in_insta:
                    _log.info("navigate_to_profile: left Instagram during back-nav, relaunching")
                    try:
                        dm.device.start_activity(
                            action="android.intent.action.MAIN",
                            category="android.intent.category.LAUNCHER",
                            component="com.instagram.android/com.instagram.mainactivity.LauncherActivity",
                            flags=FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_CLEAR_TOP,
                        )
                    except Exception:
                        app.start()
                    dm.device.wait_for_idle(timeout=1500)
                    dm.invalidate_xml_cache()
                    time.sleep(1.0)
                    xml_str, screen_ctx, in_insta = _fresh_xml()
                    steps.append("relaunched_after_exit")
                    break

            # ----- 3. Tap Search tab -----
            search_tab = dm.device(resourceId=SEARCH_TAB_ID)
            if not search_tab.exists():
                search_tab = dm.device(description="Search and explore")
            if not search_tab.exists():
                search_tab = dm.device(descriptionContains="Search")
            if not search_tab.exists():
                return {
                    "success": False, "error": "Search tab not found after 6 back presses",
                    "screen_context": screen_ctx, "steps": steps,
                    "_nav_hint": _get_nav_hint(),
                }

            search_tab.click()
            dm.invalidate_xml_cache()
            time.sleep(0.8)
            steps.append("tapped_search_tab")

            # ----- 4. Focus search input -----
            SEARCH_INPUT = "com.instagram.android:id/action_bar_search_edit_text"
            search_field = dm.device(resourceId=SEARCH_INPUT)
            if not search_field.exists():
                bar = dm.device(descriptionContains="Search")
                if bar.exists():
                    bar.click()
                    dm.invalidate_xml_cache()
                    time.sleep(0.5)
                    search_field = dm.device(resourceId=SEARCH_INPUT)

            if not search_field.exists():
                return {
                    "success": False, "error": "Search input not found",
                    "steps": steps, "_nav_hint": _get_nav_hint(),
                }

            search_field.click()
            dm.invalidate_xml_cache()
            time.sleep(0.4)
            steps.append("focused_search")

            # ----- 5. Clear + type username -----
            search_field = dm.device(resourceId=SEARCH_INPUT)
            if not search_field.exists():
                search_field = dm.device(className="android.widget.EditText", focused=True)
            if not search_field.exists():
                return {
                    "success": False, "error": "Search input lost after focus",
                    "steps": steps, "_nav_hint": _get_nav_hint(),
                }

            try:
                search_field.clear_text_field()
                time.sleep(0.2)
            except Exception:
                pass
            search_field.set_text(username)
            dm.invalidate_xml_cache()
            steps.append("typed_username")
            time.sleep(1.5)

            # ----- 6. Find and tap matching result -----
            RESULT_USERNAME_ID = "com.instagram.android:id/row_search_user_username"
            found = False

            for attempt in range(3):
                dm.invalidate_xml_cache()
                xml_str, _, _ = _fresh_xml()
                if xml_str is None:
                    time.sleep(0.5)
                    continue

                root = ET.fromstring(xml_str)
                for node in root.iter("node"):
                    node_rid = node.get("resource-id", "")
                    node_text = node.get("text", "").strip()
                    if RESULT_USERNAME_ID in node_rid and node_text.lower() == username.lower():
                        bounds_str = node.get("bounds", "")
                        m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
                        if m:
                            x1, y1, x2, y2 = (int(g) for g in m.groups())
                            dm.device.click(FPoint(x=(x1 + x2) // 2, y=(y1 + y2) // 2))
                            dm.invalidate_xml_cache()
                            found = True
                            break
                if found:
                    break

                if attempt == 1:
                    el = dm.device(text=username)
                    if not el.exists():
                        el = dm.device(textContains=username)
                    if el.exists():
                        el.click()
                        dm.invalidate_xml_cache()
                        found = True
                        break
                time.sleep(0.7)

            if not found:
                return {
                    "success": False,
                    "error": f"User '{username}' not found in search results",
                    "steps": steps, "_nav_hint": _get_nav_hint(),
                }
            steps.append("tapped_user_result")

            # ----- 7. Wait for profile to load (retry up to 5s) -----
            PROFILE_SCREENS = {"instagram_profile", "instagram_post_detail"}
            screen_ctx = "unknown"
            on_profile = False
            for wait_i in range(5):
                time.sleep(1.0)
                dm.invalidate_xml_cache()
                xml_str, screen_ctx, _ = _fresh_xml()
                if screen_ctx in PROFILE_SCREENS:
                    on_profile = True
                    break
                # After 2s still on search — retap the result row
                if wait_i == 1 and screen_ctx in ("instagram_search", "unknown"):
                    _log.info(f"navigate_to_profile: still on {screen_ctx}, retapping")
                    # Try row container first, then text match
                    row = dm.device(resourceId="com.instagram.android:id/row_search_user_container")
                    if row.exists():
                        row.click()
                        dm.invalidate_xml_cache()
                        steps.append("retapped_row_container")
                    else:
                        el = dm.device(textContains=username)
                        if el.exists():
                            el.click()
                            dm.invalidate_xml_cache()
                            steps.append("retapped_text_match")

            # Fallback: check if username appears in XML (profile loaded but screen_ctx wrong)
            if not on_profile and xml_str:
                if username.lower() in xml_str.lower():
                    _log.info(f"navigate_to_profile: username found in XML despite screen_ctx={screen_ctx}")
                    on_profile = True

            steps.append(f"profile_screen:{screen_ctx}:on_profile={on_profile}")
            _set_nav(screen_ctx, 2 if on_profile else 1)

            return {
                "success": True,
                "username": username,
                "screen_context": screen_ctx,
                "on_profile": on_profile,
                "steps": steps,
                "_nav_hint": _get_nav_hint(),
            }

        except Exception as e:
            _log.error(f"navigate_to_profile failed: {e}", exc_info=True)
            return {
                "success": False, "error": str(e),
                "steps": steps, "_nav_hint": _get_nav_hint(),
            }

    # =========================================================================
    # COMPOUND TOOL 2: return_to_feed
    # =========================================================================

    def return_to_feed() -> dict:
        """Return to the Instagram home feed from anywhere in ~1 call.

        Replaces ~13 individual tool calls. Workflow:
        1. Check current screen — if already on feed, return immediately
        2. Try pressing back (max 5 times), checking screen each time
        3. Try tapping the Home/Feed tab if bottom nav is visible
        4. Last resort: force-restart Instagram
        5. Return final screen state

        Returns:
            dict with success, screen_context, method, steps, and _nav_hint
        """
        _log = logging.getLogger("eidola.tools")
        steps: list[str] = []

        try:
            from lamda.client import Keys
            from lamda.const import FLAG_ACTIVITY_NEW_TASK, FLAG_ACTIVITY_CLEAR_TOP

            # ----- 1. Quick check — already on feed? -----
            xml_str, screen_ctx, in_insta = _fresh_xml()
            if screen_ctx == "instagram_feed":
                _set_nav("instagram_feed", 0)
                return {
                    "success": True, "screen_context": "instagram_feed",
                    "method": "already_on_feed", "steps": ["already_on_feed"],
                    "_nav_hint": _get_nav_hint(),
                }

            # ----- 2. Back-press loop (up to 5 times) -----
            if in_insta:
                for i in range(5):
                    dm.device.press_key(Keys.KEY_BACK)
                    dm.invalidate_xml_cache()
                    time.sleep(0.6)
                    xml_str, screen_ctx, in_insta = _fresh_xml()
                    steps.append(f"back_{i+1}:{screen_ctx}")

                    if screen_ctx == "instagram_feed":
                        _set_nav("instagram_feed", 0)
                        return {
                            "success": True, "screen_context": "instagram_feed",
                            "method": "back_navigation", "back_presses": i + 1,
                            "steps": steps, "_nav_hint": _get_nav_hint(),
                        }
                    if not in_insta:
                        break

            # ----- 3. Try tapping Feed/Home tab -----
            feed_tab = dm.device(resourceId="com.instagram.android:id/feed_tab")
            if not feed_tab.exists():
                feed_tab = dm.device(description="Home")
            if not feed_tab.exists():
                feed_tab = dm.device(descriptionContains="Home")
            if feed_tab.exists():
                feed_tab.click()
                dm.invalidate_xml_cache()
                time.sleep(0.8)
                xml_str, screen_ctx, in_insta = _fresh_xml()
                steps.append("tapped_feed_tab")
                if screen_ctx == "instagram_feed":
                    _set_nav("instagram_feed", 0)
                    return {
                        "success": True, "screen_context": "instagram_feed",
                        "method": "feed_tab", "steps": steps,
                        "_nav_hint": _get_nav_hint(),
                    }

            # ----- 4. Force restart Instagram -----
            _log.info("return_to_feed: force restarting Instagram")
            app = dm.device.application("com.instagram.android")
            app.stop()
            dm.invalidate_xml_cache()
            time.sleep(0.5)
            steps.append("force_closed")

            try:
                dm.device.start_activity(
                    action="android.intent.action.MAIN",
                    category="android.intent.category.LAUNCHER",
                    component="com.instagram.android/com.instagram.mainactivity.LauncherActivity",
                    flags=FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_CLEAR_TOP,
                )
            except Exception:
                app.start()

            dm.device.wait_for_idle(timeout=2000)
            dm.invalidate_xml_cache()
            time.sleep(1.5)
            steps.append("relaunched")

            xml_str, screen_ctx, in_insta = _fresh_xml()
            _set_nav(screen_ctx, 0)
            return {
                "success": screen_ctx == "instagram_feed",
                "screen_context": screen_ctx, "method": "force_restart",
                "in_instagram": in_insta, "steps": steps,
                "_nav_hint": _get_nav_hint(),
            }

        except Exception as e:
            _log.error(f"return_to_feed failed: {e}", exc_info=True)
            return {
                "success": False, "error": str(e),
                "steps": steps, "_nav_hint": _get_nav_hint(),
            }

    # =========================================================================
    # COMPOUND TOOL 3: open_post_and_engage
    # =========================================================================

    def open_post_and_engage(target_username: str, action: str = "like") -> dict:
        """Open the first post on a profile and engage with it in ~1 call.

        Must be on the target user's profile page when calling.
        Replaces ~10 individual tool calls.

        Workflow:
        1. Verify current screen is a profile page
        2. Find post thumbnails in the grid (scroll once if grid not visible)
        3. Tap the first/most-recent post
        4. Wait for post detail to load
        5. Perform engagement action (currently supports 'like')
        6. Return result with verification

        Args:
            target_username: Username of the profile (for audit / validation)
            action: Engagement action — "like" (default). Extensible for
                    "save", "comment", etc. in future.

        Returns:
            dict with success, action, result, post_type, verified,
            steps (audit trail), and _nav_hint
        """
        _log = logging.getLogger("eidola.tools")
        import re
        steps: list[str] = []

        try:
            from lamda.client import Point as FPoint

            # ----- 1. Verify we're on a profile page -----
            xml_str, screen_ctx, in_insta = _fresh_xml()
            PROFILE_SCREENS = {"instagram_profile", "instagram_post_detail"}
            if screen_ctx not in PROFILE_SCREENS:
                # Fallback: check if target_username appears in the XML
                if xml_str and target_username.lower() in xml_str.lower():
                    _log.info(f"open_post_and_engage: screen={screen_ctx} but username found in XML, proceeding")
                else:
                    return {
                        "success": False,
                        "error": f"Not on a profile page (current: {screen_ctx}). Call navigate_to_profile() first.",
                    "screen_context": screen_ctx, "steps": steps,
                    "_nav_hint": _get_nav_hint(),
                }
            steps.append("verified_profile")

            # ----- 2. Find post thumbnails in the grid -----
            POST_DESC_KEYWORDS = [
                "photo by", "video by", "reel by", "photo shared",
                "at row 1, column 1", "at row 1, column 2", "at row 1, column 3",
                "at row 2, column 1",
            ]

            def _find_post_candidates(xml: str) -> list[dict]:
                candidates = []
                for node in ET.fromstring(xml).iter("node"):
                    desc = node.get("content-desc", "")
                    bounds_str = node.get("bounds", "")
                    if not (desc and bounds_str):
                        continue
                    if not any(kw in desc.lower() for kw in POST_DESC_KEYWORDS):
                        continue
                    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
                    if not m:
                        continue
                    x1, y1, x2, y2 = (int(g) for g in m.groups())
                    if y1 < 300:
                        continue
                    candidates.append({
                        "desc": desc, "bounds": (x1, y1, x2, y2),
                        "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
                    })
                candidates.sort(key=lambda p: (p["cy"], p["cx"]))
                return candidates

            posts = _find_post_candidates(xml_str) if xml_str else []

            if not posts and dm._gestures:
                dm._gestures.scroll_feed(mode="normal")
                dm.invalidate_xml_cache()
                time.sleep(0.8)
                steps.append("scrolled_for_grid")
                xml_str, screen_ctx, _ = _fresh_xml()
                if xml_str:
                    posts = _find_post_candidates(xml_str)

            if not posts:
                return {
                    "success": False,
                    "error": "No post thumbnails found on profile grid",
                    "steps": steps, "_nav_hint": _get_nav_hint(),
                }

            # ----- 3. Tap first (most recent) post -----
            target = posts[0]
            _log.info(f"open_post_and_engage: tapping post at ({target['cx']}, {target['cy']})")
            dm.device.click(FPoint(x=target["cx"], y=target["cy"]))
            dm.invalidate_xml_cache()
            time.sleep(1.5)
            steps.append("tapped_first_post")

            # ----- 4. Read post detail screen -----
            xml_str, screen_ctx, _ = _fresh_xml()
            steps.append(f"post_screen:{screen_ctx}")

            # ----- 5. Engagement -----
            if action != "like":
                _nav_deeper()
                return {
                    "success": False,
                    "error": f"Unsupported action '{action}'. Currently supported: 'like'",
                    "screen_context": screen_ctx, "steps": steps,
                    "_nav_hint": _get_nav_hint(),
                }

            LIKE_BTN_ID = "com.instagram.android:id/row_feed_button_like"

            # Check if already liked
            already_liked = False
            if xml_str:
                for node in ET.fromstring(xml_str).iter("node"):
                    if LIKE_BTN_ID not in node.get("resource-id", ""):
                        continue
                    if node.get("selected", "false") == "true":
                        already_liked = True
                    desc = node.get("content-desc", "").lower()
                    if desc == "liked":
                        already_liked = True

            if already_liked:
                steps.append("already_liked")
                _nav_deeper()
                return {
                    "success": True, "action": "like", "result": "already_liked",
                    "target_username": target_username,
                    "screen_context": screen_ctx, "steps": steps,
                    "_nav_hint": _get_nav_hint(),
                }

            # Detect media bounds for double-tap
            IMAGE_IDS = [
                "com.instagram.android:id/carousel_media_group",
                "com.instagram.android:id/carousel_video_media_group",
                "com.instagram.android:id/zoomable_view_container",
                "com.instagram.android:id/media_group",
            ]
            media_bounds = None
            post_type = "unknown"
            if xml_str:
                for img_id in IMAGE_IDS:
                    pattern = rf'resource-id="{img_id}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
                    m = re.search(pattern, xml_str)
                    if m:
                        media_bounds = tuple(int(g) for g in m.groups())
                        if "carousel" in img_id:
                            post_type = "carousel"
                        elif "zoomable" in img_id:
                            post_type = "photo"
                        else:
                            post_type = "media"
                        break
                if "player_overlay_primary" in xml_str or "clips_viewer" in xml_str:
                    post_type = "video"

            like_method = "none"
            if media_bounds and post_type != "video" and dm._gestures:
                result = dm._gestures.double_tap_like(media_bounds)
                dm.invalidate_xml_cache()
                like_method = "double_tap"
                steps.append(f"double_tapped:{result.get('tap1')}")
            else:
                like_btn = dm.device(resourceId=LIKE_BTN_ID)
                if like_btn.exists():
                    like_btn.click()
                    dm.invalidate_xml_cache()
                    like_method = "like_button"
                    steps.append("tapped_like_button")
                else:
                    _nav_deeper()
                    return {
                        "success": False,
                        "error": "Cannot like: no media bounds for double-tap and like button not visible",
                        "post_type": post_type, "steps": steps,
                        "_nav_hint": _get_nav_hint(),
                    }

            time.sleep(0.8)
            reset_scroll_tracker()

            # ----- 6. Verify like -----
            dm.invalidate_xml_cache()
            xml_str, screen_ctx, _ = _fresh_xml()
            verified = False
            if xml_str:
                for node in ET.fromstring(xml_str).iter("node"):
                    if LIKE_BTN_ID in node.get("resource-id", ""):
                        if node.get("selected", "false") == "true":
                            verified = True
                            break
            steps.append(f"verified:{verified}")
            _set_nav(screen_ctx)
            _nav_deeper()

            return {
                "success": True, "action": "like",
                "result": "liked" if verified else "like_attempted",
                "like_method": like_method, "post_type": post_type,
                "verified": verified, "target_username": target_username,
                "screen_context": screen_ctx, "steps": steps,
                "_nav_hint": _get_nav_hint(),
            }

        except Exception as e:
            _log.error(f"open_post_and_engage failed: {e}", exc_info=True)
            return {
                "success": False, "error": str(e),
                "steps": steps, "_nav_hint": _get_nav_hint(),
            }

    # =========================================================================
    # Assemble unified tool list
    # =========================================================================

    # Include all tools from all roles
    unified_names = {
        # SUPER-TOOL (use this instead of separate observation tools!)
        "analyze_feed_posts",  # Combines: detect_screen + get_elements + is_nurtured + check_liked + detect_post_type
        
        # Navigation (from Navigator)
        "detect_screen",
        "get_screen_elements",
        "get_screen_xml",
        "find_element",
        "get_elements_for_ai",
        "open_instagram",
        "force_close_instagram",
        "restart_instagram",
        "handle_dialog",
        "escape_to_instagram",
        "tap",
        "tap_element",
        "scroll_feed",
        "scroll_fast",
        "scroll_slow_browse",
        "watch_media",  # GramAddict-style view duration
        "scroll_back",
        "refresh_feed",
        "scroll_to_post_buttons",
        "watch_stories",  # GramAddict-style story watching
        "press_back",
        "press_home",
        "press_recent",
        "type_text",
        "clear_text",
        "wait_for_idle",
        "check_connection",
        
        # Analysis (from Observer)
        "screenshot",
        "is_post_liked",
        "check_post_liked",  # Alias for is_post_liked - LLM sometimes hallucinates this name
        "is_post_saved",
        "get_post_engagement_buttons",
        "get_caption_info",
        "get_visible_comments",  # Read existing comments before commenting
        "detect_post_type",
        "detect_carousel",
        "element_exists",
        
        # Engagement (from Engager)
        "long_press",
        "double_tap_like",  # Human-like double tap to like (GramAddict pattern)
        "swipe_carousel",
        "save_post",
        "share_post",
        "follow_nurtured_account",  # Auto-follow nurtured accounts on profile visit
        
        # Comment Orchestrator (replaces manual screenshot -> caption -> generate -> post workflow)
        "comment_on_post",  # Full pipeline: guard -> gather -> generate -> validate -> post -> record
    }
    
    filtered = [t for t in all_tools if t.name in unified_names]

    # Compound tools (replace 15-30 individual tool calls each)
    filtered.extend([
        FunctionTool(navigate_to_profile),
        FunctionTool(return_to_feed),
        FunctionTool(open_post_and_engage),
    ])

    return filtered
