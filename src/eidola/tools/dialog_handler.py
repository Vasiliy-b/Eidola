"""Dialog Handler for system and Instagram dialogs.

Detects and handles common dialogs/popups:
- System permission requests
- App not responding dialogs
- Instagram-specific dialogs (rate limits, verification, etc.)
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from .action_models import DialogAction
from .element_finder import FoundElement, SmartElementFinder

if TYPE_CHECKING:
    pass


class DialogType(str, Enum):
    """Types of dialogs that can be handled."""
    
    PERMISSION_NOTIFICATION = "permission_notification"
    PERMISSION_STORAGE = "permission_storage"
    PERMISSION_LOCATION = "permission_location"
    PERMISSION_CAMERA = "permission_camera"
    PERMISSION_MICROPHONE = "permission_microphone"
    PERMISSION_OTHER = "permission_other"
    
    APP_NOT_RESPONDING = "app_not_responding"
    APP_CRASHED = "app_crashed"
    
    INSTAGRAM_RATE_LIMIT = "instagram_rate_limit"
    INSTAGRAM_VERIFICATION = "instagram_verification"
    INSTAGRAM_LOGIN_REQUIRED = "instagram_login_required"
    INSTAGRAM_UPDATE_REQUIRED = "instagram_update_required"
    INSTAGRAM_ERROR = "instagram_error"
    INSTAGRAM_POPUP = "instagram_popup"
    INSTAGRAM_SAVE_LOGIN = "instagram_save_login"
    INSTAGRAM_SAVE_COLLECTION = "instagram_save_collection"
    
    SYSTEM_ALERT = "system_alert"
    UNKNOWN = "unknown"


class DialogActionType(str, Enum):
    """How to handle a dialog."""
    
    ACCEPT = "accept"
    """Accept/allow the dialog (e.g., grant permission)."""
    
    DISMISS = "dismiss"
    """Dismiss/deny the dialog (e.g., deny permission)."""
    
    WAIT = "wait"
    """Wait for dialog to resolve (e.g., ANR)."""
    
    ALERT = "alert"
    """Alert user - requires manual intervention."""
    
    IGNORE = "ignore"
    """Ignore the dialog - continue automation."""


@dataclass
class DialogConfig:
    """Configuration for handling a specific dialog type."""
    
    dialog_type: DialogType
    """Type of dialog."""
    
    indicators: list[str]
    """Text patterns that indicate this dialog."""
    
    resource_id_patterns: list[str] | None = None
    """Resource ID patterns for this dialog."""
    
    action: DialogActionType = DialogActionType.DISMISS
    """Default action for this dialog."""
    
    button_patterns: list[str] | None = None
    """Text patterns for the action button."""
    
    requires_alert: bool = False
    """Whether to alert user even after handling."""


# Known dialog configurations
KNOWN_DIALOGS: dict[DialogType, DialogConfig] = {
    DialogType.PERMISSION_NOTIFICATION: DialogConfig(
        dialog_type=DialogType.PERMISSION_NOTIFICATION,
        indicators=["Allow notifications", "send you notifications", "notification access"],
        action=DialogActionType.DISMISS,
        button_patterns=["Don't allow", "Deny", "Not now", "Cancel"],
    ),
    DialogType.PERMISSION_STORAGE: DialogConfig(
        dialog_type=DialogType.PERMISSION_STORAGE,
        indicators=["access to photos", "access to media", "storage", "Select photos"],
        action=DialogActionType.ACCEPT,
        button_patterns=["Allow", "Allow access", "Select all", "Grant"],
    ),
    DialogType.PERMISSION_LOCATION: DialogConfig(
        dialog_type=DialogType.PERMISSION_LOCATION,
        indicators=["access your location", "location permission"],
        action=DialogActionType.DISMISS,
        button_patterns=["Don't allow", "Deny", "Not now"],
    ),
    DialogType.PERMISSION_CAMERA: DialogConfig(
        dialog_type=DialogType.PERMISSION_CAMERA,
        indicators=["access to camera", "camera permission", "take pictures"],
        action=DialogActionType.ACCEPT,
        button_patterns=["Allow", "While using the app"],
    ),
    DialogType.PERMISSION_MICROPHONE: DialogConfig(
        dialog_type=DialogType.PERMISSION_MICROPHONE,
        indicators=["access to microphone", "record audio"],
        action=DialogActionType.DISMISS,
        button_patterns=["Don't allow", "Deny"],
    ),
    DialogType.APP_NOT_RESPONDING: DialogConfig(
        dialog_type=DialogType.APP_NOT_RESPONDING,
        indicators=["isn't responding", "not responding", "Wait or close"],
        resource_id_patterns=["aerr"],
        action=DialogActionType.WAIT,
        button_patterns=["Wait", "OK"],
    ),
    DialogType.APP_CRASHED: DialogConfig(
        dialog_type=DialogType.APP_CRASHED,
        indicators=["has stopped", "keeps stopping", "crashed"],
        action=DialogActionType.ACCEPT,
        button_patterns=["OK", "Close app", "Close"],
    ),
    DialogType.INSTAGRAM_RATE_LIMIT: DialogConfig(
        dialog_type=DialogType.INSTAGRAM_RATE_LIMIT,
        indicators=["Try Again Later", "Action Blocked", "too fast", "limit reached"],
        action=DialogActionType.ALERT,
        button_patterns=["OK", "Tell Us", "Report a Problem"],
        requires_alert=True,
    ),
    DialogType.INSTAGRAM_VERIFICATION: DialogConfig(
        dialog_type=DialogType.INSTAGRAM_VERIFICATION,
        indicators=["Verify it's you", "Suspicious login", "security check", "confirm your identity"],
        action=DialogActionType.ALERT,
        requires_alert=True,
    ),
    DialogType.INSTAGRAM_LOGIN_REQUIRED: DialogConfig(
        dialog_type=DialogType.INSTAGRAM_LOGIN_REQUIRED,
        indicators=["Log in", "Log In to Continue", "session expired"],
        action=DialogActionType.ALERT,
        requires_alert=True,
    ),
    DialogType.INSTAGRAM_UPDATE_REQUIRED: DialogConfig(
        dialog_type=DialogType.INSTAGRAM_UPDATE_REQUIRED,
        indicators=["Update Required", "update Instagram", "new version"],
        action=DialogActionType.ALERT,
        requires_alert=True,
    ),
    DialogType.INSTAGRAM_POPUP: DialogConfig(
        dialog_type=DialogType.INSTAGRAM_POPUP,
        indicators=["Turn on Notifications", "Add account", "Add your phone",
                     "Find friends", "Follow people", "Complete your profile",
                     "Switch to professional"],
        action=DialogActionType.DISMISS,
        button_patterns=["Not Now", "Skip", "Later", "Cancel", "No Thanks"],
    ),
    # Save login info — ACCEPT (keeps session alive)
    DialogType.INSTAGRAM_SAVE_LOGIN: DialogConfig(
        dialog_type=DialogType.INSTAGRAM_SAVE_LOGIN,
        indicators=["Save your login", "Save login info", "save your login information"],
        action=DialogActionType.ACCEPT,
        button_patterns=["Save", "Yes", "OK"],
    ),
    DialogType.INSTAGRAM_SAVE_COLLECTION: DialogConfig(
        dialog_type=DialogType.INSTAGRAM_SAVE_COLLECTION,
        indicators=["Collect the posts you love", "Start a collection", "Save posts in collections"],
        resource_id_patterns=["pinned_save_row", "bottom_sheet_container"],
        action=DialogActionType.DISMISS,
        button_patterns=[],  # No text-based button; press_back dismisses bottom sheet
    ),
}


@dataclass
class DetectedDialog:
    """Result of dialog detection."""
    
    detected: bool
    """Whether a dialog was detected."""
    
    dialog_type: DialogType
    """Type of detected dialog."""
    
    config: DialogConfig | None
    """Configuration for handling this dialog."""
    
    matched_text: str | None = None
    """Text that matched the dialog pattern."""
    
    action_button: FoundElement | None = None
    """Button element to click for action."""
    
    dismiss_button: FoundElement | None = None
    """Button element to dismiss dialog."""


class DialogHandler:
    """Detect and handle system and app dialogs."""
    
    def __init__(
        self,
        tap_func: Callable[[int, int], bool],
        press_back_func: Callable[[], bool] | None = None,
    ):
        """Initialize dialog handler.
        
        Args:
            tap_func: Function to tap at coordinates (x, y) -> success
            press_back_func: Function to press back button -> success
        """
        self.tap = tap_func
        self.press_back = press_back_func
    
    def detect(self, xml_str: str) -> DetectedDialog:
        """Detect if a dialog is present.
        
        Args:
            xml_str: XML hierarchy dump
            
        Returns:
            DetectedDialog with detection result
        """
        try:
            finder = SmartElementFinder(xml_str)
        except ET.ParseError:
            return DetectedDialog(
                detected=False,
                dialog_type=DialogType.UNKNOWN,
                config=None,
            )
        
        # Check each known dialog type
        for dialog_type, config in KNOWN_DIALOGS.items():
            result = self._check_dialog(finder, config)
            if result.detected:
                return result
        
        return DetectedDialog(
            detected=False,
            dialog_type=DialogType.UNKNOWN,
            config=None,
        )
    
    def handle(self, xml_str: str) -> tuple[bool, DialogType | None]:
        """Detect and handle dialog if present.
        
        Args:
            xml_str: XML hierarchy dump
            
        Returns:
            Tuple of (handled, dialog_type)
            - handled: True if dialog was handled
            - dialog_type: Type of dialog that was handled (or None)
        """
        detected = self.detect(xml_str)
        
        if not detected.detected:
            return False, None
        
        if detected.config is None:
            return False, detected.dialog_type
        
        # Handle based on action type
        handled = self._execute_action(detected)
        
        return handled, detected.dialog_type
    
    def _check_dialog(self, finder: SmartElementFinder, config: DialogConfig) -> DetectedDialog:
        """Check if specific dialog type is present.
        
        Args:
            finder: Element finder for current screen
            config: Dialog configuration to check
            
        Returns:
            DetectedDialog result
        """
        elements = finder.get_elements_for_ai(max_elements=50)
        
        # Check text indicators
        matched_text = None
        for elem in elements:
            display = elem.get("display", "").lower()
            for indicator in config.indicators:
                if indicator.lower() in display:
                    matched_text = elem.get("display")
                    break
            if matched_text:
                break
        
        if not matched_text:
            return DetectedDialog(
                detected=False,
                dialog_type=config.dialog_type,
                config=config,
            )
        
        # Find action button
        action_button = None
        dismiss_button = None
        
        if config.button_patterns:
            for pattern in config.button_patterns:
                element = finder.find_by_text(pattern, partial=True)
                if element and element.clickable:
                    # ACCEPT and WAIT need action_button (to click)
                    # DISMISS needs dismiss_button
                    if config.action in (DialogActionType.ACCEPT, DialogActionType.WAIT):
                        action_button = element
                    else:
                        dismiss_button = element
                    break
        
        return DetectedDialog(
            detected=True,
            dialog_type=config.dialog_type,
            config=config,
            matched_text=matched_text,
            action_button=action_button,
            dismiss_button=dismiss_button,
        )
    
    def _execute_action(self, detected: DetectedDialog) -> bool:
        """Execute the appropriate action for detected dialog.
        
        Args:
            detected: Detected dialog information
            
        Returns:
            True if action was executed successfully
        """
        if detected.config is None:
            return False
        
        action = detected.config.action
        
        if action == DialogActionType.ALERT:
            # Don't auto-handle, requires user attention
            return False
        
        if action == DialogActionType.IGNORE:
            return True
        
        if action == DialogActionType.WAIT:
            # Click wait button if available
            if detected.action_button:
                return self.tap(
                    detected.action_button.center[0],
                    detected.action_button.center[1],
                )
            return False
        
        if action == DialogActionType.ACCEPT:
            if detected.action_button:
                return self.tap(
                    detected.action_button.center[0],
                    detected.action_button.center[1],
                )
            return False
        
        if action == DialogActionType.DISMISS:
            if detected.dismiss_button:
                return self.tap(
                    detected.dismiss_button.center[0],
                    detected.dismiss_button.center[1],
                )
            # Try press_back as fallback
            if self.press_back:
                return self.press_back()
            return False
        
        return False


def create_dialog_handler(
    tap_func: Callable[[int, int], bool],
    press_back_func: Callable[[], bool] | None = None,
) -> DialogHandler:
    """Create a DialogHandler instance.
    
    Args:
        tap_func: Function to tap at coordinates
        press_back_func: Function to press back button
        
    Returns:
        Configured DialogHandler instance
    """
    return DialogHandler(tap_func, press_back_func)
