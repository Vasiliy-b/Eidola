"""Pydantic models for structured output in navigation actions.

These models define the contract between AI decisions and action execution.
Used with ADK's output_schema for type-safe structured responses.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    """Types of actions the agent can perform."""
    
    TAP = "tap"
    LONG_PRESS = "long_press"
    TYPE_TEXT = "type_text"
    SCROLL_DOWN = "scroll_down"
    SCROLL_UP = "scroll_up"
    SCROLL_FEED = "scroll_feed"
    PRESS_BACK = "press_back"
    PRESS_HOME = "press_home"
    WAIT = "wait"
    NEED_SCREENSHOT = "need_screenshot"  # Visual analysis required
    DONE = "done"  # Task completed
    FAILED = "failed"  # Task cannot be completed


class ScrollDirection(str, Enum):
    """Scroll directions."""
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


class ScreenContext(str, Enum):
    """Detected screen context."""
    
    INSTAGRAM_FEED = "instagram_feed"
    INSTAGRAM_PROFILE = "instagram_profile"
    INSTAGRAM_SEARCH = "instagram_search"
    INSTAGRAM_REELS = "instagram_reels"
    INSTAGRAM_NOTIFICATIONS = "instagram_notifications"
    INSTAGRAM_DM = "instagram_dm"
    INSTAGRAM_POST_DETAIL = "instagram_post_detail"
    INSTAGRAM_COMMENTS = "instagram_comments"
    INSTAGRAM_STORIES = "instagram_stories"
    INSTAGRAM_DIALOG = "instagram_dialog"  # Instagram popups
    INSTAGRAM_ACCOUNT_SWITCHER = "instagram_account_switcher"
    INSTAGRAM_ADD_ACCOUNT = "instagram_add_account"
    INSTAGRAM_LOGIN = "instagram_login"
    INSTAGRAM_OTHER = "instagram_other"
    
    SYSTEM_UI = "system_ui"  # Notification shade
    SYSTEM_OVERLAY = "system_overlay"  # Permission dialogs
    KEYBOARD_VISIBLE = "keyboard_visible"  # IME active
    HOME_SCREEN = "home_screen"
    OTHER_APP = "other_app"
    UNKNOWN = "unknown"


class ElementBasedAction(BaseModel):
    """Structured output for element-based navigation decisions.
    
    This model is used with ADK's output_schema to ensure type-safe
    structured responses from the navigation agent.
    """
    
    # What we detected
    current_screen: ScreenContext = Field(
        description="Detected screen context based on XML analysis"
    )
    
    # What to do
    action: ActionType = Field(
        description="Type of action to perform"
    )
    
    # Target element (for tap/type actions)
    target_text: str | None = Field(
        default=None,
        description="Text of the element to interact with"
    )
    target_resource_id: str | None = Field(
        default=None,
        description="resource-id of the element (if text not available)"
    )
    target_content_desc: str | None = Field(
        default=None,
        description="content-desc of the element (for accessibility)"
    )
    
    # Coordinates (optional, for precise taps)
    target_x: int | None = Field(
        default=None,
        description="X coordinate for tap (center of element)"
    )
    target_y: int | None = Field(
        default=None,
        description="Y coordinate for tap (center of element)"
    )
    
    # For type actions
    type_value: str | None = Field(
        default=None,
        description="Text to type if action is TYPE_TEXT"
    )
    
    # Scroll parameters
    scroll_direction: ScrollDirection | None = Field(
        default=None,
        description="Direction for scroll actions"
    )
    scroll_distance: int | None = Field(
        default=None,
        description="Approximate distance to scroll in pixels"
    )
    
    # Wait parameters
    wait_ms: int | None = Field(
        default=None,
        description="Milliseconds to wait if action is WAIT"
    )
    
    # Confidence and reasoning
    confidence: float = Field(
        ge=0.0, le=1.0,
        default=0.8,
        description="Confidence in element identification (0-1)"
    )
    reasoning: str = Field(
        description="Brief explanation of why this action was chosen"
    )
    
    # Recovery hints
    fallback_action: ActionType | None = Field(
        default=None,
        description="Alternative action if primary fails"
    )
    
    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "current_screen": "instagram_feed",
                    "action": "tap",
                    "target_text": None,
                    "target_resource_id": "com.instagram.android:id/row_feed_button_like",
                    "confidence": 0.95,
                    "reasoning": "Found like button by resource_id",
                    "fallback_action": "scroll_down",
                },
                {
                    "current_screen": "instagram_feed",
                    "action": "scroll_feed",
                    "scroll_direction": "down",
                    "confidence": 1.0,
                    "reasoning": "Need to see more posts in feed",
                },
                {
                    "current_screen": "instagram_comments",
                    "action": "type_text",
                    "target_resource_id": "com.instagram.android:id/layout_comment_thread_edittext",
                    "type_value": "Great photo!",
                    "confidence": 0.9,
                    "reasoning": "Typing comment in comment input field",
                },
            ]
        }


class VerificationResult(BaseModel):
    """Result of action verification."""
    
    success: bool = Field(description="Whether the action succeeded")
    
    screen_changed: bool = Field(
        default=False,
        description="Whether the screen changed after action"
    )
    element_disappeared: bool = Field(
        default=False,
        description="Whether the target element disappeared (expected for taps)"
    )
    
    new_screen: ScreenContext | None = Field(
        default=None,
        description="New screen context if changed"
    )
    
    error_message: str | None = Field(
        default=None,
        description="Error message if action failed"
    )
    
    retry_recommended: bool = Field(
        default=False,
        description="Whether retrying the action is recommended"
    )


class NavigationGoal(BaseModel):
    """Goal for the navigation agent."""
    
    destination: ScreenContext = Field(
        description="Target screen to navigate to"
    )
    
    target_username: str | None = Field(
        default=None,
        description="Username to navigate to (for profile navigation)"
    )
    
    target_hashtag: str | None = Field(
        default=None,
        description="Hashtag to search for (for search navigation)"
    )
    
    max_steps: int = Field(
        default=10,
        description="Maximum navigation steps before failing"
    )


class DialogAction(BaseModel):
    """Action for handling dialogs."""
    
    dialog_type: str = Field(description="Type of dialog detected")
    
    action: Literal["accept", "dismiss", "wait", "alert"] = Field(
        description="How to handle the dialog"
    )
    
    button_text: str | None = Field(
        default=None,
        description="Text of button to click"
    )
    
    reasoning: str = Field(description="Why this action was chosen")
