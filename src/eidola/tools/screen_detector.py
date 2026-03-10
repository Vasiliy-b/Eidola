"""Screen detection based on XML hierarchy analysis.

Determines the current screen context by analyzing XML dump:
- Package name detection (Instagram vs SystemUI vs other apps)
- Screen type detection within Instagram (feed, profile, search, etc.)
- Overlay/dialog detection
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .action_models import ScreenContext
from .selectors import SCREEN_SIGNATURES, MIN_CONFIDENCE

if TYPE_CHECKING:
    pass


@dataclass
class ScreenDetectionResult:
    """Result of screen detection."""
    
    context: ScreenContext
    """Detected screen context."""
    
    confidence: float
    """Confidence level (0-1)."""
    
    package: str
    """Package name of the foreground app."""
    
    has_dialog: bool = False
    """Whether a dialog/overlay is present."""
    
    has_keyboard: bool = False
    """Whether keyboard is visible."""
    
    matched_resource_ids: list[str] | None = None
    """Resource IDs that matched for this screen."""


# Known system UI packages
SYSTEM_PACKAGES = {
    "com.android.systemui",
    "com.android.settings",
    "com.android.packageinstaller",
    "com.google.android.permissioncontroller",
}

# Known launcher packages
LAUNCHER_PACKAGES_PATTERNS = [
    "launcher",
    "trebuchet",
    "home",
    "desktop",
]

# Dialog indicators in resource-ids
DIALOG_INDICATORS = [
    "dialog",
    "popup",
    "modal",
    "alert",
    "sheet",
    "bottom_sheet",
    "action_sheet",
]

# Keyboard indicators
KEYBOARD_INDICATORS = [
    "com.google.android.inputmethod",
    "android.inputmethodservice",
    "keyboard",
    "ime",
]


def detect_screen(xml_str: str) -> ScreenDetectionResult:
    """Detect current screen context from XML hierarchy.
    
    Args:
        xml_str: XML dump of the screen hierarchy
        
    Returns:
        ScreenDetectionResult with context and confidence
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return ScreenDetectionResult(
            context=ScreenContext.UNKNOWN,
            confidence=0.0,
            package="",
        )
    
    # Extract package from first nodes
    package = _extract_package(root)
    
    # Check for keyboard first (can overlay any app)
    has_keyboard = _check_keyboard(root)
    
    # Check for system overlay/dialog
    has_dialog = _check_for_dialog(root)
    
    # ==========================================================================
    # PRIORITY 1: Check Instagram FIRST - most common case, avoid false positives
    # ==========================================================================
    if package == "com.instagram.android":
        return _detect_instagram_screen(root, package, has_dialog, has_keyboard)
    
    # ==========================================================================
    # PRIORITY 2: System UI (only if NOT in Instagram)
    # ==========================================================================
    if package in SYSTEM_PACKAGES:
        return ScreenDetectionResult(
            context=ScreenContext.SYSTEM_UI if not has_dialog else ScreenContext.SYSTEM_OVERLAY,
            confidence=1.0,
            package=package,
            has_dialog=has_dialog,
            has_keyboard=has_keyboard,
        )
    
    # Check for system overlay ONLY for non-Instagram apps
    # (Instagram has its own "Allow notifications" dialogs that shouldn't trigger this)
    if _is_system_overlay(root, package):
        return ScreenDetectionResult(
            context=ScreenContext.SYSTEM_OVERLAY,
            confidence=0.9,
            package=package,
            has_dialog=True,
            has_keyboard=has_keyboard,
        )
    
    # Check for launcher/home screen
    if _is_launcher(package, root):
        return ScreenDetectionResult(
            context=ScreenContext.HOME_SCREEN,
            confidence=0.9,
            package=package,
            has_keyboard=has_keyboard,
        )
    
    # Other app
    return ScreenDetectionResult(
        context=ScreenContext.OTHER_APP,
        confidence=0.8,
        package=package,
        has_dialog=has_dialog,
        has_keyboard=has_keyboard,
    )


def _extract_package(root: ET.Element) -> str:
    """Extract MAIN package name from XML hierarchy.
    
    Skips system UI packages (keyboard, status bar, overlays) to find
    the actual foreground app package.
    """
    SYSTEM_PACKAGES = {
        "com.android.systemui",
        "com.android.inputmethod",
        "com.google.android.inputmethod",
        "com.touchtype.swiftkey",  # SwiftKey keyboard
        "com.baidu.input",  # Baidu IME
        "com.samsung.android.honeyboard",  # Samsung keyboard
        "com.sec.android.inputmethod",  # Samsung IME
    }
    
    # First pass: find non-system package
    for node in root.iter("node"):
        pkg = node.get("package", "")
        if pkg and not any(pkg.startswith(sys_pkg) for sys_pkg in SYSTEM_PACKAGES):
            return pkg
    
    # Fallback: return first package if all are system
    for node in root.iter("node"):
        pkg = node.get("package", "")
        if pkg:
            return pkg
    return ""


def _check_keyboard(root: ET.Element) -> bool:
    """Check if keyboard is visible."""
    for node in root.iter("node"):
        pkg = node.get("package", "").lower()
        res_id = node.get("resource-id", "").lower()
        class_name = node.get("class", "").lower()
        
        for indicator in KEYBOARD_INDICATORS:
            if indicator in pkg or indicator in res_id or indicator in class_name:
                return True
    return False


def _check_for_dialog(root: ET.Element) -> bool:
    """Check if a dialog/overlay is present.
    
    IMPORTANT: Filters out Instagram's normal UI containers that contain
    "sheet" or "bottom_sheet" in their resource-ids (like bottom_sheet_camera_container).
    Only returns True for actual dialog/popup overlays.
    """
    # Instagram resource IDs that contain "sheet" but are NOT dialogs
    INSTAGRAM_FALSE_POSITIVES = {
        "bottom_sheet_camera_container",
        "sheet_container",
        "action_sheet_container",  # Normal UI, not a real action sheet
    }
    
    for node in root.iter("node"):
        res_id = node.get("resource-id", "").lower()
        class_name = node.get("class", "").lower()
        pkg = node.get("package", "")
        
        # Skip Instagram's known false positives
        if pkg == "com.instagram.android":
            res_id_short = res_id.split("/")[-1] if "/" in res_id else res_id
            if any(fp in res_id_short for fp in INSTAGRAM_FALSE_POSITIVES):
                continue
        
        # Check for dialog indicators
        for indicator in DIALOG_INDICATORS:
            if indicator in res_id or indicator in class_name:
                # Additional validation: for Instagram, require more specific dialog markers
                if pkg == "com.instagram.android":
                    # Real Instagram dialogs usually have "dialog" in class name
                    if "dialog" in class_name:
                        return True
                    # For resource-id matches, require actual dialog buttons
                    # This catches: action_sheet, popup, modal, alert, bottom_sheet
                    if _has_dialog_buttons(root):
                        return True
                else:
                    # For non-Instagram apps, use original logic
                    return True
        
        # Check for common Android dialog patterns
        if "android:id/alerttitle" in res_id:
            return True
        if "android:id/message" in res_id and "alertdialog" in class_name:
            return True
        
        # Detect auto-fill overlay (blocks input during account search)
        if "autofill" in res_id or "autofill" in class_name:
            return True
    
    return False


def _has_dialog_buttons(root: ET.Element) -> bool:
    """Check if dialog has actual buttons (OK, Cancel, Allow, etc.).
    
    Extended to detect Instagram-specific dialog buttons and various button types.
    """
    # Comprehensive set of dialog button texts
    button_texts = {
        # Standard dialog buttons
        "ok", "cancel", "allow", "deny", "yes", "no", "dismiss", "not now", "close",
        # Instagram-specific
        "block", "mute", "report", "unfollow", "remove", "delete", "confirm",
        "skip", "continue", "turn on", "save", "discard", "leave", "stay",
        "restrict", "unrestrict", "hide", "unhide", "copy link",
    }
    
    for node in root.iter("node"):
        class_name = node.get("class", "")
        text = node.get("text", "").lower().strip()
        clickable = node.get("clickable", "") == "true"
        
        # Check various button classes
        is_button_class = (
            class_name.endswith("Button") or
            class_name.endswith("ImageButton") or
            "Button" in class_name
        )
        
        # Check clickable text elements (Instagram uses these in dialogs)
        is_clickable_text = clickable and class_name.endswith("TextView")
        
        if (is_button_class or is_clickable_text) and text in button_texts:
            return True
    
    return False


def _is_system_overlay(root: ET.Element, package: str = "") -> bool:
    """Check if current view is a system overlay (permissions, etc.).
    
    IMPORTANT: This should only be called for non-Instagram packages.
    Instagram has its own internal dialogs that should NOT trigger overlay detection.
    
    Args:
        root: XML root element
        package: Current package name (used for additional filtering)
        
    Returns:
        True if this appears to be a system overlay
    """
    # Never trigger for Instagram - it has internal dialogs
    if package == "com.instagram.android":
        return False
    
    for node in root.iter("node"):
        res_id = node.get("resource-id", "")
        pkg = node.get("package", "")
        text = node.get("text", "").lower()
        
        # Must be from system/permission controller package
        is_system_pkg = (
            pkg in SYSTEM_PACKAGES or 
            "permissioncontroller" in pkg.lower() or
            pkg.startswith("com.android.")
        )
        
        if not is_system_pkg:
            continue
        
        # Permission dialog indicators - require BOTH system package AND keywords
        permission_keywords = ["allow", "deny", "permission"]
        if any(kw in text for kw in permission_keywords):
            # Additional check: must have system resource ID
            if "permissioncontroller" in res_id or res_id.startswith("com.android."):
                return True
    
    return False


def _is_launcher(package: str, root: ET.Element) -> bool:
    """Check if current view is a launcher/home screen."""
    pkg_lower = package.lower()
    
    for pattern in LAUNCHER_PACKAGES_PATTERNS:
        if pattern in pkg_lower:
            return True
    
    # Check for launcher-specific resource IDs
    for node in root.iter("node"):
        res_id = node.get("resource-id", "").lower()
        if "workspace" in res_id or "hotseat" in res_id or "search_container" in res_id:
            return True
    
    return False


def _detect_instagram_screen(
    root: ET.Element,
    package: str,
    has_dialog: bool,
    has_keyboard: bool,
) -> ScreenDetectionResult:
    """Detect specific Instagram screen type."""
    
    # ==========================================================================
    # PRIORITY CHECK: Explore Grid Detection (before signature matching)
    # Explore grid has unique content-desc pattern: "at row X, column Y"
    # This overrides feed detection since feed_tab is visible on both
    # ==========================================================================
    explore_grid_count = 0
    for node in root.iter("node"):
        content_desc = node.get("content-desc", "")
        # Explore grid items have pattern like "Reel by X at row Y, column Z"
        if " at row " in content_desc and ", column " in content_desc:
            explore_grid_count += 1
    
    # If we found multiple explore grid items, this is definitely Search/Explore
    if explore_grid_count >= 3:
        return ScreenDetectionResult(
            context=ScreenContext.INSTAGRAM_SEARCH,
            confidence=0.95,
            package=package,
            has_dialog=has_dialog,
            has_keyboard=has_keyboard,
            matched_resource_ids=["explore_grid_pattern"],
        )
    
    # ==========================================================================
    # PRIORITY CHECK: Account Switcher / Add Account / Login screens
    # These screens use content-desc rather than resource-ids for key elements
    # ==========================================================================
    content_descs = set()
    texts = set()
    resource_ids = set()
    for node in root.iter("node"):
        res_id = node.get("resource-id", "")
        if res_id:
            resource_ids.add(res_id)
        cd = node.get("content-desc", "")
        if cd:
            content_descs.add(cd)
        txt = node.get("text", "")
        if txt:
            texts.add(txt)
    
    # Login screen: has username field + password field + Log in button
    if ("Username, email or mobile number," in content_descs
            and "Password," in content_descs):
        return ScreenDetectionResult(
            context=ScreenContext.INSTAGRAM_LOGIN,
            confidence=0.98,
            package=package,
            has_dialog=has_dialog,
            has_keyboard=has_keyboard,
            matched_resource_ids=["login_fields"],
        )
    
    # Add Account choice screen: "Log into existing account" + "Create new account"
    if ("Log into existing account" in content_descs
            and "Create new account" in content_descs):
        return ScreenDetectionResult(
            context=ScreenContext.INSTAGRAM_ADD_ACCOUNT,
            confidence=0.98,
            package=package,
            has_dialog=has_dialog,
            has_keyboard=has_keyboard,
            matched_resource_ids=["add_account_choice"],
        )
    
    # Account Switcher bottom sheet: has recycler_view_container_id + "Add Instagram account"
    if ("com.instagram.android:id/recycler_view_container_id" in resource_ids
            and ("Add Instagram account" in content_descs
                 or "Add Instagram account" in texts)):
        return ScreenDetectionResult(
            context=ScreenContext.INSTAGRAM_ACCOUNT_SWITCHER,
            confidence=0.98,
            package=package,
            has_dialog=has_dialog,
            has_keyboard=has_keyboard,
            matched_resource_ids=["recycler_view_container_id", "add_account"],
        )
    
    # Try to match against screen signatures (resource-id based)
    best_match = None
    best_confidence = 0.0
    matched_ids = []
    
    for screen_name, signature in SCREEN_SIGNATURES.items():
        if signature.get("package") and signature["package"] != "com.instagram.android":
            continue
        
        confidence, matched = _match_signature(resource_ids, signature)
        
        if confidence > best_confidence:
            best_confidence = confidence
            best_match = screen_name
            matched_ids = matched
    
    # Map screen name to context
    screen_context_map = {
        "instagram_feed": ScreenContext.INSTAGRAM_FEED,
        "instagram_profile": ScreenContext.INSTAGRAM_PROFILE,
        "instagram_search": ScreenContext.INSTAGRAM_SEARCH,
        "instagram_reels": ScreenContext.INSTAGRAM_REELS,
        "instagram_notifications": ScreenContext.INSTAGRAM_NOTIFICATIONS,
        "instagram_dm": ScreenContext.INSTAGRAM_DM,
        "instagram_post_detail": ScreenContext.INSTAGRAM_POST_DETAIL,
        "instagram_comments": ScreenContext.INSTAGRAM_COMMENTS,
        "instagram_account_switcher": ScreenContext.INSTAGRAM_ACCOUNT_SWITCHER,
        "instagram_add_account": ScreenContext.INSTAGRAM_ADD_ACCOUNT,
        "instagram_login": ScreenContext.INSTAGRAM_LOGIN,
    }
    
    if best_match and best_confidence >= MIN_CONFIDENCE:
        context = screen_context_map.get(best_match, ScreenContext.INSTAGRAM_OTHER)
    else:
        context = ScreenContext.INSTAGRAM_OTHER
        best_confidence = 0.5  # We know it's Instagram, just not sure which screen
    
    return ScreenDetectionResult(
        context=context,
        confidence=best_confidence,
        package=package,
        has_dialog=has_dialog,
        has_keyboard=has_keyboard,
        matched_resource_ids=matched_ids,
    )


def _match_signature(resource_ids: set[str], signature: dict) -> tuple[float, list[str]]:
    """Match resource IDs against a screen signature.
    
    Returns:
        Tuple of (confidence, matched_ids)
    """
    matched = []
    score = 0.0
    max_score = 0.0
    
    # Required resource IDs (must all be present)
    required = signature.get("required_resource_ids", [])
    if required:
        max_score += 1.0
        required_matched = all(
            any(req in rid for rid in resource_ids)
            for req in required
        )
        if required_matched:
            score += 1.0
            matched.extend(required)
        else:
            return 0.0, []  # Required not met, no match
    
    # Any resource IDs (at least one should be present)
    any_ids = signature.get("any_resource_ids", [])
    if any_ids:
        max_score += 0.5
        for any_id in any_ids:
            for rid in resource_ids:
                if any_id in rid:
                    score += 0.5 / len(any_ids)  # Partial credit for each match
                    matched.append(any_id)
                    break
    
    if max_score == 0:
        return 0.5, []  # No specific requirements, assume partial match
    
    confidence = score / max_score
    return confidence, matched


def is_in_instagram(xml_str: str) -> bool:
    """Quick check if we're in Instagram app.
    
    Checks if Instagram is the main foreground app, even if
    there's a keyboard or system overlay on top.
    
    Args:
        xml_str: XML dump of the screen hierarchy
        
    Returns:
        True if in Instagram, False otherwise
    """
    try:
        root = ET.fromstring(xml_str)
        
        # Method 1: Check extracted package (skips system UI)
        package = _extract_package(root)
        if package == "com.instagram.android":
            return True
        
        # Method 2: Search for any Instagram elements in the tree
        # This catches cases where overlays are on top
        for node in root.iter("node"):
            pkg = node.get("package", "")
            res_id = node.get("resource-id", "")
            if pkg == "com.instagram.android" or res_id.startswith("com.instagram.android:"):
                return True
        
        return False
    except ET.ParseError:
        return False


def needs_recovery(result: ScreenDetectionResult) -> bool:
    """Check if the current screen state needs recovery.
    
    Args:
        result: Screen detection result
        
    Returns:
        True if recovery workflow should be triggered
    """
    recovery_contexts = {
        ScreenContext.SYSTEM_UI,
        ScreenContext.SYSTEM_OVERLAY,
        ScreenContext.HOME_SCREEN,
        ScreenContext.OTHER_APP,
        ScreenContext.UNKNOWN,
        ScreenContext.INSTAGRAM_ACCOUNT_SWITCHER,
        ScreenContext.INSTAGRAM_ADD_ACCOUNT,
        ScreenContext.INSTAGRAM_LOGIN,
    }
    return result.context in recovery_contexts
