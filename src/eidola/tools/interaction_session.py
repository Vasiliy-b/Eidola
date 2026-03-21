"""Interaction Session Manager for XML caching and state management.

Caches XML within a logical "interaction session" (e.g., engaging with one post)
to avoid redundant device calls.

Cache is automatically invalidated after:
- Any scroll operation
- Any tap/click operation  
- Cache timeout (default 2 seconds)
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Any

logger = logging.getLogger("eidola.tools.session")


@dataclass
class CachedXML:
    """Cached XML with timestamp and parsed tree."""
    xml_str: str
    parsed_root: ET.Element
    timestamp: float
    screen_context: str
    
    @property
    def age_ms(self) -> float:
        """Cache age in milliseconds."""
        return (time.time() - self.timestamp) * 1000


class InteractionSession:
    """Manages cached state for an interaction session.
    
    An interaction session is a logical unit of work, e.g.:
    - Engaging with a single post (check state, like, comment, save)
    - Browsing carousel pages
    - Navigating to a profile
    
    Within a session, XML is cached to avoid redundant fetches.
    """
    
    DEFAULT_CACHE_TTL_MS = 2000  # 2 seconds - UI can change
    
    def __init__(self, fetch_xml_func: Callable[[], dict]):
        """Initialize session.
        
        Args:
            fetch_xml_func: Function that returns dict with "xml" and "valid" keys
        """
        self._fetch_xml = fetch_xml_func
        self._cached: CachedXML | None = None
        self._cache_ttl_ms = self.DEFAULT_CACHE_TTL_MS
        self._interaction_count = 0
        self._cache_hits = 0
        self._cache_misses = 0
    
    def get_xml(self, force_refresh: bool = False) -> dict:
        """Get XML, using cache if valid.
        
        Args:
            force_refresh: If True, bypass cache and fetch fresh
            
        Returns:
            dict with "xml", "valid", "cached" keys
        """
        # Check cache validity
        if not force_refresh and self._cached and self._cached.age_ms < self._cache_ttl_ms:
            self._cache_hits += 1
            logger.debug(f"XML cache HIT (age: {int(self._cached.age_ms)}ms)")
            return {
                "xml": self._cached.xml_str,
                "valid": True,
                "screen_context": self._cached.screen_context,
                "cached": True,
                "cache_age_ms": int(self._cached.age_ms),
            }
        
        # Fetch fresh
        self._cache_misses += 1
        logger.debug("XML cache MISS - fetching fresh")
        result = self._fetch_xml()
        
        if result.get("valid"):
            try:
                self._cached = CachedXML(
                    xml_str=result["xml"],
                    parsed_root=ET.fromstring(result["xml"]),
                    timestamp=time.time(),
                    screen_context=result.get("screen_context", "unknown"),
                )
            except ET.ParseError as e:
                logger.warning(f"Failed to parse XML for cache: {e}")
        
        result["cached"] = False
        return result
    
    def get_parsed_root(self, force_refresh: bool = False) -> ET.Element | None:
        """Get pre-parsed XML root (avoids re-parsing).
        
        Args:
            force_refresh: If True, bypass cache
            
        Returns:
            Parsed ET.Element root or None if invalid
        """
        if not force_refresh and self._cached and self._cached.age_ms < self._cache_ttl_ms:
            return self._cached.parsed_root
        
        # Fetch and parse
        result = self.get_xml(force_refresh=force_refresh)
        return self._cached.parsed_root if self._cached else None
    
    def invalidate(self, reason: str = "unknown") -> None:
        """Invalidate cache (call after UI-changing actions).
        
        Args:
            reason: Why cache was invalidated (for logging)
        """
        if self._cached:
            logger.debug(f"XML cache invalidated: {reason}")
        self._cached = None
    
    def action_performed(self, action_type: str = "unknown") -> None:
        """Called after any UI action - invalidates cache and tracks count.
        
        Args:
            action_type: Type of action (tap, scroll, swipe, etc.)
        """
        self.invalidate(reason=action_type)
        self._interaction_count += 1
    
    @property
    def stats(self) -> dict:
        """Get session statistics."""
        total_requests = self._cache_hits + self._cache_misses
        hit_rate = (self._cache_hits / total_requests * 100) if total_requests > 0 else 0
        
        return {
            "interaction_count": self._interaction_count,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate_percent": round(hit_rate, 1),
            "cache_valid": self._cached is not None and self._cached.age_ms < self._cache_ttl_ms,
            "cache_age_ms": int(self._cached.age_ms) if self._cached else None,
        }
    
    def set_ttl(self, ttl_ms: int) -> None:
        """Set cache TTL in milliseconds.
        
        Args:
            ttl_ms: Time-to-live in milliseconds
        """
        self._cache_ttl_ms = ttl_ms


# =============================================================================
# Global Session Registry (per device)
# =============================================================================

_sessions: dict[str, InteractionSession] = {}


def get_session(device_ip: str, fetch_xml_func: Callable[[], dict]) -> InteractionSession:
    """Get or create interaction session for device.
    
    Args:
        device_ip: Device IP address
        fetch_xml_func: Function to fetch XML from device
        
    Returns:
        InteractionSession instance
    """
    if device_ip not in _sessions:
        logger.info(f"Creating new InteractionSession for {device_ip}")
        _sessions[device_ip] = InteractionSession(fetch_xml_func)
    return _sessions[device_ip]


def clear_session(device_ip: str) -> None:
    """Clear session for device.
    
    Args:
        device_ip: Device IP address
    """
    if device_ip in _sessions:
        stats = _sessions[device_ip].stats
        logger.info(f"Clearing session for {device_ip}: {stats}")
        del _sessions[device_ip]


def get_all_stats() -> dict[str, dict]:
    """Get statistics for all sessions.
    
    Returns:
        Dict mapping device_ip to session stats
    """
    return {ip: session.stats for ip, session in _sessions.items()}
