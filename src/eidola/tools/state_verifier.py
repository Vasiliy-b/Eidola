"""State Verifier for action result validation.

Verifies that actions produced expected results by comparing
screen state before and after action execution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from .action_models import ScreenContext, VerificationResult
from .element_finder import FoundElement, SmartElementFinder
from .screen_detector import ScreenDetectionResult, detect_screen
from .timeouts import Stage, get_config

if TYPE_CHECKING:
    pass


class VerificationStatus(str, Enum):
    """Status of verification check."""
    
    SUCCESS = "success"
    """Action succeeded - expected state change observed."""
    
    UNCHANGED = "unchanged"
    """Action had no effect - state didn't change."""
    
    UNEXPECTED = "unexpected"
    """Action had unexpected effect - different state than expected."""
    
    ERROR = "error"
    """Error during verification."""
    
    TIMEOUT = "timeout"
    """Verification timed out."""


@dataclass
class VerificationConfig:
    """Configuration for state verification."""
    
    max_retries: int = 3
    """Maximum verification attempts."""
    
    retry_delay_ms: int = 200
    """Delay between verification attempts."""
    
    check_element_disappeared: bool = True
    """Check if tapped element disappeared."""
    
    check_screen_changed: bool = True
    """Check if screen context changed."""
    
    check_content_changed: bool = False
    """Check if visible content changed (more expensive)."""
    
    timeout_ms: int = 5000
    """Total timeout for verification."""


@dataclass
class StateSnapshot:
    """Snapshot of screen state for comparison."""
    
    timestamp: float
    """When snapshot was taken."""
    
    screen: ScreenDetectionResult
    """Detected screen context."""
    
    xml_hash: int
    """Hash of XML for quick comparison."""
    
    element_count: int
    """Number of elements in hierarchy."""
    
    target_element_exists: bool = False
    """Whether the target element was found."""
    
    target_element_bounds: tuple[int, int, int, int] | None = None
    """Bounds of target element if found."""


class StateVerifier:
    """Verify that actions produce expected state changes."""
    
    def __init__(
        self,
        get_xml_func: Callable[[], str],
        config: VerificationConfig | None = None,
    ):
        """Initialize state verifier.
        
        Args:
            get_xml_func: Function to get current XML hierarchy
            config: Verification configuration
        """
        self.get_xml = get_xml_func
        self.config = config or VerificationConfig()
        self._before_snapshot: StateSnapshot | None = None
    
    def capture_before(
        self,
        target_element: FoundElement | None = None,
    ) -> StateSnapshot:
        """Capture state before action.
        
        Args:
            target_element: Element that will be interacted with
            
        Returns:
            StateSnapshot for later comparison
        """
        xml_str = self.get_xml()
        screen = detect_screen(xml_str)
        finder = SmartElementFinder(xml_str)
        
        target_exists = False
        target_bounds = None
        
        if target_element:
            # Check if target element still exists
            found = finder.find_by_bounds(target_element.bounds, tolerance=20)
            target_exists = found is not None
            if found:
                target_bounds = found.bounds
        
        snapshot = StateSnapshot(
            timestamp=time.time(),
            screen=screen,
            xml_hash=hash(xml_str),
            element_count=len(finder.get_all_elements()),
            target_element_exists=target_exists,
            target_element_bounds=target_bounds,
        )
        
        self._before_snapshot = snapshot
        return snapshot
    
    def verify_after(
        self,
        expected_screen: ScreenContext | None = None,
        expect_element_gone: bool = True,
    ) -> VerificationResult:
        """Verify state after action.
        
        Args:
            expected_screen: Expected screen context after action
            expect_element_gone: Whether target element should disappear
            
        Returns:
            VerificationResult with success status and details
        """
        if not self._before_snapshot:
            return VerificationResult(
                success=False,
                error_message="No before snapshot captured",
            )
        
        before = self._before_snapshot
        start_time = time.time()
        timeout_sec = self.config.timeout_ms / 1000.0
        
        for attempt in range(self.config.max_retries):
            # Check timeout
            if time.time() - start_time > timeout_sec:
                return VerificationResult(
                    success=False,
                    error_message="Verification timed out",
                    retry_recommended=True,
                )
            
            # Wait before retry (except first attempt)
            if attempt > 0:
                time.sleep(self.config.retry_delay_ms / 1000.0)
            
            # Get current state
            try:
                xml_str = self.get_xml()
                screen = detect_screen(xml_str)
                finder = SmartElementFinder(xml_str)
            except Exception as e:
                continue  # Retry on error
            
            # Check screen change
            screen_changed = screen.context != before.screen.context
            
            # Check element disappeared
            element_gone = False
            if self.config.check_element_disappeared and before.target_element_bounds:
                found = finder.find_by_bounds(before.target_element_bounds, tolerance=20)
                element_gone = found is None
            
            # Check content change (hash comparison)
            content_changed = hash(xml_str) != before.xml_hash
            
            # Determine success based on expectations
            success = False
            
            if expected_screen:
                # Specific screen expected
                if screen.context == expected_screen:
                    success = True
            elif expect_element_gone and before.target_element_exists:
                # Element should have disappeared (e.g., after tap)
                if element_gone or screen_changed:
                    success = True
            elif content_changed or screen_changed:
                # Any change is success
                success = True
            
            if success:
                return VerificationResult(
                    success=True,
                    screen_changed=screen_changed,
                    element_disappeared=element_gone,
                    new_screen=screen.context if screen_changed else None,
                )
        
        # All retries exhausted
        return VerificationResult(
            success=False,
            screen_changed=False,
            element_disappeared=False,
            error_message="State unchanged after action",
            retry_recommended=True,
        )
    
    def verify_tap(
        self,
        tapped_element: FoundElement,
        expected_screen: ScreenContext | None = None,
    ) -> VerificationResult:
        """Verify tap action result.
        
        Convenience method that captures before state if not already done.
        
        Args:
            tapped_element: Element that was tapped
            expected_screen: Expected screen after tap (optional)
            
        Returns:
            VerificationResult
        """
        if not self._before_snapshot:
            self.capture_before(tapped_element)
        
        return self.verify_after(
            expected_screen=expected_screen,
            expect_element_gone=True,
        )
    
    def verify_scroll(self) -> VerificationResult:
        """Verify scroll action result.
        
        After scroll, we expect content to change but screen context
        should remain the same.
        
        Returns:
            VerificationResult
        """
        if not self._before_snapshot:
            self.capture_before()
        
        before = self._before_snapshot
        
        # Get current state
        xml_str = self.get_xml()
        
        # Check that content changed (hash different)
        content_changed = hash(xml_str) != before.xml_hash
        
        # Screen context should be same
        screen = detect_screen(xml_str)
        screen_same = screen.context == before.screen.context
        
        success = content_changed and screen_same
        
        return VerificationResult(
            success=success,
            screen_changed=not screen_same,
            new_screen=screen.context if not screen_same else None,
            error_message="Scroll had no effect" if not content_changed else None,
            retry_recommended=not content_changed,
        )
    
    def verify_type(self, typed_text: str) -> VerificationResult:
        """Verify type action result.
        
        Check that typed text appears in some element.
        
        Args:
            typed_text: Text that was typed
            
        Returns:
            VerificationResult
        """
        xml_str = self.get_xml()
        finder = SmartElementFinder(xml_str)
        
        # Look for typed text in any element
        found = finder.find_by_text(typed_text, partial=True)
        
        return VerificationResult(
            success=found is not None,
            error_message="Typed text not found on screen" if not found else None,
            retry_recommended=found is None,
        )
    
    def reset(self):
        """Reset verifier state for next action."""
        self._before_snapshot = None


def create_verifier(
    get_xml_func: Callable[[], str],
    config: VerificationConfig | None = None,
) -> StateVerifier:
    """Create a StateVerifier instance.
    
    Args:
        get_xml_func: Function to get current XML hierarchy
        config: Optional verification configuration
        
    Returns:
        Configured StateVerifier instance
    """
    return StateVerifier(get_xml_func, config)
