"""Escape Workflows for automatic recovery from problematic states.

When the agent gets lost (system UI, other apps, etc.), these workflows
automatically navigate back to Instagram.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from .action_models import ScreenContext
from .screen_detector import ScreenDetectionResult, detect_screen, needs_recovery
from .dialog_handler import DialogHandler

if TYPE_CHECKING:
    pass


class EscapeAction(str, Enum):
    """Actions for escape workflows."""
    
    PRESS_BACK = "press_back"
    PRESS_HOME = "press_home"
    OPEN_INSTAGRAM = "open_instagram"
    RESTART_INSTAGRAM = "restart_instagram"  # Force close + reopen (last resort)
    DISMISS_DIALOG = "dismiss_dialog"
    SWIPE_DOWN = "swipe_down"  # To close notification shade
    WAIT = "wait"


@dataclass
class WorkflowStep:
    """Single step in an escape workflow."""
    
    action: EscapeAction
    """Action to perform."""
    
    max_attempts: int = 1
    """Maximum attempts for this step."""
    
    delay_after_ms: int = 500
    """Delay after action (milliseconds)."""
    
    next_action: EscapeAction | None = None
    """Action to chain if this one succeeds."""


# Escape workflows for different problematic states
STATE_WORKFLOWS: dict[ScreenContext, list[WorkflowStep]] = {
    ScreenContext.SYSTEM_UI: [
        # Notification shade - try swipe down first, then back, then home
        WorkflowStep(
            action=EscapeAction.SWIPE_DOWN,
            max_attempts=1,
            delay_after_ms=300,
        ),
        WorkflowStep(
            action=EscapeAction.PRESS_BACK,
            max_attempts=2,
            delay_after_ms=500,
        ),
        WorkflowStep(
            action=EscapeAction.PRESS_HOME,
            delay_after_ms=500,
            next_action=EscapeAction.OPEN_INSTAGRAM,
        ),
    ],
    ScreenContext.SYSTEM_OVERLAY: [
        # Permission dialog - try to dismiss
        WorkflowStep(
            action=EscapeAction.DISMISS_DIALOG,
            max_attempts=2,
            delay_after_ms=500,
        ),
        WorkflowStep(
            action=EscapeAction.PRESS_BACK,
            max_attempts=1,
            delay_after_ms=500,
        ),
    ],
    ScreenContext.HOME_SCREEN: [
        # On launcher - just open Instagram
        WorkflowStep(
            action=EscapeAction.OPEN_INSTAGRAM,
            max_attempts=2,
            delay_after_ms=1000,
        ),
    ],
    ScreenContext.OTHER_APP: [
        # In another app - go home first, then open Instagram
        WorkflowStep(
            action=EscapeAction.PRESS_BACK,
            max_attempts=2,
            delay_after_ms=300,
        ),
        WorkflowStep(
            action=EscapeAction.PRESS_HOME,
            delay_after_ms=500,
            next_action=EscapeAction.OPEN_INSTAGRAM,
        ),
    ],
    ScreenContext.UNKNOWN: [
        # Unknown state - try everything
        WorkflowStep(
            action=EscapeAction.PRESS_BACK,
            max_attempts=3,
            delay_after_ms=500,
        ),
        WorkflowStep(
            action=EscapeAction.PRESS_HOME,
            delay_after_ms=500,
            next_action=EscapeAction.OPEN_INSTAGRAM,
        ),
    ],
    ScreenContext.KEYBOARD_VISIBLE: [
        # Keyboard open - press back to dismiss
        WorkflowStep(
            action=EscapeAction.PRESS_BACK,
            max_attempts=1,
            delay_after_ms=300,
        ),
    ],
}


@dataclass
class EscapeResult:
    """Result of escape workflow execution."""
    
    success: bool
    """Whether we successfully returned to Instagram."""
    
    final_screen: ScreenContext
    """Final screen context after workflow."""
    
    steps_executed: int
    """Number of steps executed."""
    
    error_message: str | None = None
    """Error message if workflow failed."""


class EscapeWorkflows:
    """Execute escape workflows to return to Instagram from problematic states."""
    
    def __init__(
        self,
        get_xml_func: Callable[[], str],
        press_back_func: Callable[[], bool],
        press_home_func: Callable[[], bool],
        open_instagram_func: Callable[[], bool],
        swipe_down_func: Callable[[], bool] | None = None,
        dialog_handler: DialogHandler | None = None,
        restart_instagram_func: Callable[[], bool] | None = None,
    ):
        """Initialize escape workflows.
        
        Args:
            get_xml_func: Function to get current XML hierarchy
            press_back_func: Function to press back button
            press_home_func: Function to press home button
            open_instagram_func: Function to launch Instagram app
            swipe_down_func: Function to swipe down (close notification shade)
            dialog_handler: DialogHandler for dismissing dialogs
            restart_instagram_func: Function to force close and reopen Instagram (last resort)
        """
        self.get_xml = get_xml_func
        self.press_back = press_back_func
        self.press_home = press_home_func
        self.open_instagram = open_instagram_func
        self.swipe_down = swipe_down_func
        self.dialog_handler = dialog_handler
        self.restart_instagram = restart_instagram_func
    
    def escape_to_instagram(
        self,
        current_screen: ScreenContext | None = None,
        max_total_attempts: int = 10,
    ) -> EscapeResult:
        """Execute escape workflow to return to Instagram.
        
        ADAPTIVE APPROACH: Re-evaluates state after each action and selects
        the most appropriate next action based on current state.
        
        Args:
            current_screen: Current screen context (will detect if not provided)
            max_total_attempts: Maximum total actions to attempt
            
        Returns:
            EscapeResult with success status and final screen
        """
        total_steps = 0
        last_screen = None  # Track screen changes to detect loops
        same_screen_count = 0
        
        while total_steps < max_total_attempts:
            # ALWAYS re-detect current screen state
            xml_str = self.get_xml()
            result = detect_screen(xml_str)
            current_screen = result.context
            
            # Check if we're already in Instagram
            if self._is_instagram(current_screen):
                return EscapeResult(
                    success=True,
                    final_screen=current_screen,
                    steps_executed=total_steps,
                )
            
            # Detect if we're stuck (same screen multiple times)
            if current_screen == last_screen:
                same_screen_count += 1
            else:
                same_screen_count = 0
                last_screen = current_screen
            
            # If stuck on same screen for 5+ attempts and restart available, FORCE RESTART
            if same_screen_count >= 5 and self.restart_instagram:
                # Last resort: force close and reopen Instagram
                self.restart_instagram()
                total_steps += 1
                same_screen_count = 0
                time.sleep(0.5)  # Wait for app to fully restart
                continue
            
            # If stuck on same screen for 3+ attempts, escalate to more aggressive action
            if same_screen_count >= 3:
                # Force HOME + OPEN_INSTAGRAM as escape hatch
                self.press_home()
                time.sleep(0.25)
                self.open_instagram()
                total_steps += 2
                same_screen_count = 0
                continue
            
            # Get workflow for CURRENT state (re-selected each iteration)
            workflow = STATE_WORKFLOWS.get(current_screen)
            if not workflow:
                workflow = STATE_WORKFLOWS[ScreenContext.UNKNOWN]
            
            # Execute ONLY THE FIRST appropriate step, then re-evaluate
            step = workflow[0]
            
            # Try the step
            success = self._execute_step(step.action)
            total_steps += 1
            
            if success:
                time.sleep(step.delay_after_ms / 1000.0)
                
                # Execute chained action if any
                if step.next_action:
                    self._execute_step(step.next_action)
                    total_steps += 1
                    time.sleep(step.delay_after_ms / 1000.0)
            else:
                # If first step failed, try next step in workflow
                if len(workflow) > 1:
                    step = workflow[1]
                    self._execute_step(step.action)
                    total_steps += 1
                    time.sleep(step.delay_after_ms / 1000.0)
        
        # Failed to escape - final state check
        xml_str = self.get_xml()
        result = detect_screen(xml_str)
        
        return EscapeResult(
            success=False,
            final_screen=result.context,
            steps_executed=total_steps,
            error_message=f"Failed to return to Instagram after {total_steps} steps",
        )
    
    def _is_instagram(self, context: ScreenContext) -> bool:
        """Check if context is an Instagram screen."""
        instagram_contexts = {
            ScreenContext.INSTAGRAM_FEED,
            ScreenContext.INSTAGRAM_PROFILE,
            ScreenContext.INSTAGRAM_SEARCH,
            ScreenContext.INSTAGRAM_REELS,
            ScreenContext.INSTAGRAM_NOTIFICATIONS,
            ScreenContext.INSTAGRAM_DM,
            ScreenContext.INSTAGRAM_POST_DETAIL,
            ScreenContext.INSTAGRAM_COMMENTS,
            ScreenContext.INSTAGRAM_STORIES,
            ScreenContext.INSTAGRAM_DIALOG,
            ScreenContext.INSTAGRAM_OTHER,
        }
        return context in instagram_contexts
    
    def _execute_step(self, action: EscapeAction) -> bool:
        """Execute a single escape action.
        
        Args:
            action: Action to execute
            
        Returns:
            True if action executed successfully
        """
        try:
            if action == EscapeAction.PRESS_BACK:
                return self.press_back()
            
            elif action == EscapeAction.PRESS_HOME:
                return self.press_home()
            
            elif action == EscapeAction.OPEN_INSTAGRAM:
                return self.open_instagram()
            
            elif action == EscapeAction.RESTART_INSTAGRAM:
                if self.restart_instagram:
                    return self.restart_instagram()
                # Fallback: just try opening Instagram
                return self.open_instagram()
            
            elif action == EscapeAction.SWIPE_DOWN:
                if self.swipe_down:
                    return self.swipe_down()
                # Fallback: press back
                return self.press_back()
            
            elif action == EscapeAction.DISMISS_DIALOG:
                if self.dialog_handler:
                    xml_str = self.get_xml()
                    handled, _ = self.dialog_handler.handle(xml_str)
                    return handled
                # Fallback: press back
                return self.press_back()
            
            elif action == EscapeAction.WAIT:
                time.sleep(0.2)
                return True
            
            return False
            
        except Exception:
            return False
    
    def quick_check_and_escape(self) -> bool:
        """Quick check if recovery needed and escape if so.
        
        Convenience method for inline recovery checks.
        
        Returns:
            True if we're in Instagram (either already were or successfully escaped)
        """
        xml_str = self.get_xml()
        result = detect_screen(xml_str)
        
        if not needs_recovery(result):
            return True
        
        escape_result = self.escape_to_instagram(result.context)
        return escape_result.success


def create_escape_workflows(
    get_xml_func: Callable[[], str],
    press_back_func: Callable[[], bool],
    press_home_func: Callable[[], bool],
    open_instagram_func: Callable[[], bool],
    swipe_down_func: Callable[[], bool] | None = None,
    dialog_handler: DialogHandler | None = None,
    restart_instagram_func: Callable[[], bool] | None = None,
) -> EscapeWorkflows:
    """Create an EscapeWorkflows instance.
    
    Args:
        get_xml_func: Function to get current XML hierarchy
        press_back_func: Function to press back button
        press_home_func: Function to press home button
        open_instagram_func: Function to launch Instagram app
        swipe_down_func: Function to swipe down
        dialog_handler: DialogHandler for dismissing dialogs
        restart_instagram_func: Function to force close and reopen Instagram (last resort)
        
    Returns:
        Configured EscapeWorkflows instance
    """
    return EscapeWorkflows(
        get_xml_func=get_xml_func,
        press_back_func=press_back_func,
        press_home_func=press_home_func,
        open_instagram_func=open_instagram_func,
        swipe_down_func=swipe_down_func,
        dialog_handler=dialog_handler,
        restart_instagram_func=restart_instagram_func,
    )
