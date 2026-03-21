"""
Authentication tools for Instagram login and 2FA.

Provides TOTP code generation and login flow helpers.
"""

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pyotp

from ..config import (
    AccountConfig,
    load_account_config,
    get_account_password,
    get_account_totp_secret,
    # Gmail configs
    GmailConfig,
    load_gmail_config,
    load_gmail_config_by_account_id,
    get_gmail_password,
    get_gmail_totp_secret,
)

logger = logging.getLogger("eidola.tools.auth")


# =============================================================================
# TOTP 2FA TOOLS
# =============================================================================

def generate_2fa_code(account_id: str) -> dict[str, Any]:
    """
    Generate current TOTP 2FA code for an Instagram account.
    
    Uses pyotp to generate time-based one-time passwords compatible
    with Instagram's authentication app 2FA.
    
    Args:
        account_id: Account identifier (matches config/accounts/{account_id}.yaml)
        
    Returns:
        dict with:
            - success: bool
            - code: str (6-digit code) if success
            - valid_for_seconds: int (seconds until code expires) if success
            - error: str if not success
            
    Example:
        >>> result = generate_2fa_code("example_account")
        >>> if result["success"]:
        ...     print(f"Code: {result['code']}, valid for {result['valid_for_seconds']}s")
    """
    # Load account config
    account_config = load_account_config(account_id)
    if not account_config:
        logger.error(f"Account config not found: {account_id}")
        return {
            "success": False,
            "error": f"Account config not found: {account_id}",
        }
    
    # Get TOTP secret from environment
    totp_secret = get_account_totp_secret(account_config)
    if not totp_secret:
        logger.error(f"TOTP secret not configured for account: {account_id}")
        return {
            "success": False,
            "error": f"TOTP secret not configured. Set {account_config.instagram.totp_secret_env} in .env",
        }
    
    try:
        # Generate TOTP code
        totp = pyotp.TOTP(totp_secret)
        code = totp.now()
        
        # Calculate time remaining until code expires
        # TOTP codes are valid for 30 seconds by default
        current_time = time.time()
        time_remaining = totp.interval - (int(current_time) % totp.interval)
        
        logger.info(f"Generated 2FA code for {account_id}: {code} (valid for {time_remaining}s)")
        
        return {
            "success": True,
            "code": code,
            "valid_for_seconds": time_remaining,
            "account_id": account_id,
        }
        
    except Exception as e:
        logger.error(f"Failed to generate 2FA code for {account_id}: {e}")
        return {
            "success": False,
            "error": f"Failed to generate 2FA code: {str(e)}",
        }


def get_account_credentials(account_id: str) -> dict[str, Any]:
    """
    Get Instagram credentials for an account.
    
    Retrieves username and password from account config and environment.
    Does NOT include TOTP secret for security.
    
    Args:
        account_id: Account identifier
        
    Returns:
        dict with:
            - success: bool
            - username: str if success
            - password: str if success
            - has_2fa: bool (whether TOTP is configured)
            - error: str if not success
    """
    account_config = load_account_config(account_id)
    if not account_config:
        return {
            "success": False,
            "error": f"Account config not found: {account_id}",
        }
    
    password = get_account_password(account_config)
    if not password:
        return {
            "success": False,
            "error": f"Password not found. Set {account_config.instagram.password_env} in .env",
        }
    
    has_2fa = bool(account_config.instagram.totp_secret_env and 
                   get_account_totp_secret(account_config))
    
    return {
        "success": True,
        "username": account_config.instagram.username,
        "password": password,
        "has_2fa": has_2fa,
        "account_id": account_id,
    }


def verify_2fa_setup(account_id: str) -> dict[str, Any]:
    """
    Verify that 2FA is properly configured for an account.
    
    Generates a test code to ensure the TOTP secret is valid.
    
    Args:
        account_id: Account identifier
        
    Returns:
        dict with verification status and details
    """
    account_config = load_account_config(account_id)
    if not account_config:
        return {
            "valid": False,
            "error": f"Account config not found: {account_id}",
        }
    
    totp_secret = get_account_totp_secret(account_config)
    if not totp_secret:
        return {
            "valid": False,
            "error": "TOTP secret not configured",
            "env_var": account_config.instagram.totp_secret_env,
        }
    
    try:
        # Try to create TOTP and generate code
        totp = pyotp.TOTP(totp_secret)
        code = totp.now()
        
        # Verify the secret is valid base32
        if len(code) != 6 or not code.isdigit():
            return {
                "valid": False,
                "error": "Generated code is invalid - check TOTP secret format",
            }
        
        return {
            "valid": True,
            "account_id": account_id,
            "username": account_config.instagram.username,
            "test_code": code,
            "message": "2FA is properly configured",
        }
        
    except Exception as e:
        return {
            "valid": False,
            "error": f"Invalid TOTP secret: {str(e)}",
            "hint": "TOTP secret must be a valid base32 string",
        }


# =============================================================================
# GMAIL / PLAY STORE AUTHENTICATION
# =============================================================================

def get_gmail_credentials(device_id: str) -> dict[str, Any]:
    """
    Get Gmail credentials for a device (for Play Store login).
    
    Loads Gmail config from config/gmail/{device_id}.yaml and retrieves
    credentials from environment variables.
    
    Args:
        device_id: Device identifier (e.g., 'phone_01')
        
    Returns:
        dict with:
            - success: bool
            - email: str if success
            - password: str if success
            - has_2fa: bool
            - account_id: str (gmail account id)
            - error: str if not success
            
    Example:
        >>> result = get_gmail_credentials("phone_01")
        >>> if result["success"]:
        ...     print(f"Email: {result['email']}")
    """
    gmail_config = load_gmail_config(device_id)
    if not gmail_config:
        return {
            "success": False,
            "error": f"Gmail config not found for device: {device_id}",
            "hint": f"Create config/gmail/{device_id}.yaml",
        }
    
    password = get_gmail_password(gmail_config)
    if not password:
        return {
            "success": False,
            "error": f"Gmail password not found. Set {gmail_config.gmail.password_env} in .env",
        }
    
    has_2fa = bool(gmail_config.gmail.totp_secret_env and 
                   get_gmail_totp_secret(gmail_config))
    
    return {
        "success": True,
        "email": gmail_config.gmail.email,
        "password": password,
        "has_2fa": has_2fa,
        "account_id": gmail_config.account_id,
        "device_id": device_id,
    }


def generate_gmail_2fa_code(device_id: str) -> dict[str, Any]:
    """
    Generate TOTP 2FA code for Gmail/Play Store login.
    
    Uses the Gmail config for the specified device.
    
    Args:
        device_id: Device identifier (e.g., 'phone_01')
        
    Returns:
        dict with:
            - success: bool
            - code: str (6-digit code) if success
            - valid_for_seconds: int if success
            - email: str (Gmail address)
            - error: str if not success
    """
    gmail_config = load_gmail_config(device_id)
    if not gmail_config:
        return {
            "success": False,
            "error": f"Gmail config not found for device: {device_id}",
        }
    
    totp_secret = get_gmail_totp_secret(gmail_config)
    if not totp_secret:
        return {
            "success": False,
            "error": f"Gmail TOTP secret not configured. Set {gmail_config.gmail.totp_secret_env} in .env",
        }
    
    try:
        totp = pyotp.TOTP(totp_secret)
        code = totp.now()
        
        current_time = time.time()
        time_remaining = totp.interval - (int(current_time) % totp.interval)
        
        logger.info(f"Generated Gmail 2FA code for {gmail_config.gmail.email}: {code}")
        
        return {
            "success": True,
            "code": code,
            "valid_for_seconds": time_remaining,
            "email": gmail_config.gmail.email,
            "device_id": device_id,
        }
        
    except Exception as e:
        logger.error(f"Failed to generate Gmail 2FA code for {device_id}: {e}")
        return {
            "success": False,
            "error": f"Failed to generate 2FA code: {str(e)}",
        }


def generate_2fa_code_raw(totp_secret: str) -> dict[str, Any]:
    """
    Generate TOTP 2FA code from a raw secret.
    
    Use this when you have the TOTP secret directly (not from config).
    Useful for one-off operations or testing.
    
    Args:
        totp_secret: Base32-encoded TOTP secret
        
    Returns:
        dict with:
            - success: bool
            - code: str (6-digit code) if success
            - valid_for_seconds: int if success
            - error: str if not success
            
    Example:
        >>> result = generate_2fa_code_raw("JBSWY3DPEHPK3PXP")
        >>> print(result["code"])
    """
    try:
        totp = pyotp.TOTP(totp_secret)
        code = totp.now()
        
        current_time = time.time()
        time_remaining = totp.interval - (int(current_time) % totp.interval)
        
        return {
            "success": True,
            "code": code,
            "valid_for_seconds": time_remaining,
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": f"Invalid TOTP secret: {str(e)}",
            "hint": "Secret must be a valid base32 string (uppercase letters A-Z and digits 2-7)",
        }


# =============================================================================
# LOGIN DETECTION HELPERS
# =============================================================================

def detect_login_screen(xml_content: str) -> dict[str, Any]:
    """
    Detect if current screen is Instagram login screen.
    
    Analyzes XML dump to identify login-related elements.
    
    Args:
        xml_content: XML dump from device
        
    Returns:
        dict with:
            - is_login_screen: bool
            - screen_type: str (login | 2fa_method_select | 2fa_code_input | unknown)
            - elements: dict of found elements with their bounds
    """
    import xml.etree.ElementTree as ET
    
    result = {
        "is_login_screen": False,
        "screen_type": "unknown",
        "elements": {},
    }
    
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return result
    
    # Login screen indicators
    login_indicators = {
        "username_field": False,
        "password_field": False,
        "login_button": False,
        "2fa_code_field": False,
        "2fa_method_select": False,
        "auth_app_option": False,
    }
    
    for node in root.iter("node"):
        text = node.get("text", "").lower()
        content_desc = node.get("content-desc", "").lower()
        resource_id = node.get("resource-id", "").lower()
        bounds = node.get("bounds", "")
        
        # Username field detection
        if "username" in text or "phone number" in text or "email" in text:
            if node.get("class", "").endswith("EditText"):
                login_indicators["username_field"] = True
                result["elements"]["username_field"] = bounds
        
        # Password field detection
        if "password" in text or "password" in resource_id:
            if node.get("class", "").endswith("EditText"):
                login_indicators["password_field"] = True
                result["elements"]["password_field"] = bounds
        
        # Login button detection
        if text == "log in" or "login" in resource_id:
            login_indicators["login_button"] = True
            result["elements"]["login_button"] = bounds
        
        # 2FA code input detection
        if content_desc == "code," or "code" in text and "6-digit" in text.lower():
            login_indicators["2fa_code_field"] = True
            result["elements"]["2fa_code_field"] = bounds
        
        # 2FA method selection screen
        if "choose a way" in text or "select" in text and "confirm" in text:
            login_indicators["2fa_method_select"] = True
        
        # Authentication app option
        if "authentication app" in text or "authenticator" in text:
            login_indicators["auth_app_option"] = True
            result["elements"]["auth_app_option"] = bounds
        
        # Continue button (for 2FA screens)
        if text == "continue" or text == "next":
            result["elements"]["continue_button"] = bounds
    
    # Determine screen type
    if login_indicators["2fa_code_field"]:
        result["is_login_screen"] = True
        result["screen_type"] = "2fa_code_input"
    elif login_indicators["2fa_method_select"] or login_indicators["auth_app_option"]:
        result["is_login_screen"] = True
        result["screen_type"] = "2fa_method_select"
    elif login_indicators["username_field"] and login_indicators["password_field"]:
        result["is_login_screen"] = True
        result["screen_type"] = "login"
    elif login_indicators["login_button"]:
        result["is_login_screen"] = True
        result["screen_type"] = "login"
    
    return result


def detect_feed_screen(xml_content: str) -> bool:
    """
    Detect if current screen is Instagram feed (login successful).
    
    Looks for bottom navigation tabs as indicator of successful login.
    
    Args:
        xml_content: XML dump from device
        
    Returns:
        True if feed screen detected, False otherwise
    """
    import xml.etree.ElementTree as ET
    
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return False
    
    # Feed indicators - bottom nav tabs
    feed_indicators = {
        "home_tab": False,
        "search_tab": False,
        "reels_tab": False,
        "profile_tab": False,
    }
    
    for node in root.iter("node"):
        content_desc = node.get("content-desc", "").lower()
        
        if "home" in content_desc:
            feed_indicators["home_tab"] = True
        if "search" in content_desc:
            feed_indicators["search_tab"] = True
        if "reels" in content_desc:
            feed_indicators["reels_tab"] = True
        if "profile" in content_desc:
            feed_indicators["profile_tab"] = True
    
    # Need at least 2 nav tabs to confirm feed screen
    matches = sum(feed_indicators.values())
    return matches >= 2


# =============================================================================
# DEVICE ACCOUNT TRACKING (MongoDB)
# =============================================================================

def _get_device_accounts_collection():
    """Get the device_accounts MongoDB collection."""
    try:
        from ..memory.sync_memory import SyncAgentMemory
        mem = SyncAgentMemory()
        if mem.is_connected():
            return mem.db["device_accounts"]
    except Exception:
        pass
    return None


def mark_account_on_device(device_id: str, account_id: str, username: str) -> bool:
    """Record that an account is logged into a device.
    
    Upserts a document in device_accounts collection.
    
    Args:
        device_id: Device identifier
        account_id: Account identifier
        username: Instagram username
        
    Returns:
        True if recorded successfully
    """
    from datetime import datetime, timezone
    
    coll = _get_device_accounts_collection()
    if coll is None:
        logger.warning("MongoDB not available for device account tracking")
        return False
    
    try:
        coll.update_one(
            {"device_id": device_id, "account_id": account_id},
            {
                "$set": {
                    "username": username,
                    "logged_in": True,
                    "last_seen": datetime.now(timezone.utc),
                },
                "$setOnInsert": {
                    "first_login": datetime.now(timezone.utc),
                },
            },
            upsert=True,
        )
        logger.info(f"Tracked: {account_id} logged in on {device_id}")
        return True
    except Exception as e:
        logger.warning(f"Failed to track account on device: {e}")
        return False


def get_accounts_on_device(device_id: str) -> list[dict]:
    """Get all accounts known to be logged into a device.
    
    Args:
        device_id: Device identifier
        
    Returns:
        List of dicts with account_id, username, logged_in, last_seen
    """
    coll = _get_device_accounts_collection()
    if coll is None:
        return []
    
    try:
        docs = coll.find(
            {"device_id": device_id, "logged_in": True},
            {"_id": 0, "account_id": 1, "username": 1, "logged_in": 1, "last_seen": 1},
        )
        return list(docs)
    except Exception as e:
        logger.warning(f"Failed to query device accounts: {e}")
        return []


def mark_account_logged_out(device_id: str, account_id: str) -> bool:
    """Mark an account as logged out from a device.
    
    Args:
        device_id: Device identifier
        account_id: Account identifier
        
    Returns:
        True if updated successfully
    """
    coll = _get_device_accounts_collection()
    if coll is None:
        return False
    
    try:
        coll.update_one(
            {"device_id": device_id, "account_id": account_id},
            {"$set": {"logged_in": False}},
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to mark account logged out: {e}")
        return False


# =============================================================================
# ACCOUNT SWITCHING
# =============================================================================

def _dump_and_find():
    """Dump device XML hierarchy and return SmartElementFinder."""
    from .firerpa_tools import get_device_manager
    from .element_finder import SmartElementFinder
    
    dm = get_device_manager()
    xml_bytes = dm.device.dump_window_hierarchy()
    xml_str = xml_bytes.getvalue().decode("utf-8")
    return SmartElementFinder(xml_str), xml_str


def _tap_element(element, label: str = "") -> bool:
    """Tap the center of a FoundElement. Returns True on success."""
    from .firerpa_tools import get_device_manager
    from lamda.client import Point
    
    dm = get_device_manager()
    x, y = element.center
    dm.device.click(Point(x=x, y=y))
    if label:
        logger.info(f"Tapped: {label} at ({x}, {y})")
    return True


def _type_into_focused_field(dm, text: str) -> None:
    """Type text into the currently focused EditText field.
    
    Types character-by-character with small delays for human-like behavior.
    Falls back to set_text if char-by-char fails.
    """
    import time
    import random
    
    et = dm.device(className="android.widget.EditText", focused=True)
    if not et.exists():
        et = dm.device(className="android.widget.EditText")
    
    if et.exists():
        et.clear_text_field()
        time.sleep(0.2)
        
        # Type character by character with random delays (human-like)
        try:
            current = ""
            for char in text:
                current += char
                et.set_text(current)
                time.sleep(random.uniform(0.05, 0.15))
            logger.debug(f"Typed {len(text)} chars (char-by-char)")
        except Exception:
            et.set_text(text)
            logger.debug(f"Typed {len(text)} chars (set_text fallback)")
    else:
        logger.warning("No EditText found for typing, falling back to input_text")
        dm.device.input_text(text)


def _find_with_retry(selector_name: str, retries: int = 2, delay: float = 0.8):
    """Dump XML and find element, retrying if not found."""
    import time
    for attempt in range(retries + 1):
        finder, xml_str = _dump_and_find()
        element = finder.find(selector_name)
        if element:
            return element, finder, xml_str
        if attempt < retries:
            time.sleep(delay)
    return None, finder, xml_str


def _ensure_profile_header_ready(max_attempts: int = 4) -> tuple[Any | None, Any, str]:
    """Ensure profile page header is fully loaded before switcher actions.

    Returns:
        (username_elem_or_none, finder, xml_str)
    """
    from .screen_detector import detect_screen

    finder = None
    xml_str = ""
    username_elem = None

    for attempt in range(max_attempts):
        finder, xml_str = _dump_and_find()
        username_elem = finder.find("profile_username_text")
        dropdown_elem = finder.find("profile_username_dropdown")
        if username_elem or dropdown_elem:
            return username_elem, finder, xml_str

        screen_ctx = detect_screen(xml_str).context.value
        logger.info(
            f"Profile header not ready (attempt {attempt + 1}/{max_attempts}, screen={screen_ctx})"
        )

        feed_tab = finder.find("feed_tab")
        profile_tab = finder.find("profile_tab")
        if feed_tab and profile_tab:
            _tap_element(feed_tab, "Feed tab (profile refresh)")
            time.sleep(0.3)
            _tap_element(profile_tab, "Profile tab (profile refresh)")
            time.sleep(0.5)
        elif profile_tab:
            _tap_element(profile_tab, "Profile tab (retry)")
            time.sleep(0.5)
        else:
            time.sleep(0.3)

    return username_elem, finder, xml_str


def _save_switch_failure_snapshot(xml_str: str, reason: str) -> str | None:
    """Persist XML snapshot for switch failures to simplify debugging."""
    try:
        project_root = Path(__file__).resolve().parents[3]
        debug_dir = project_root / "debug_xml"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_reason = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in reason)[:40]
        path = debug_dir / f"switch_failure_{safe_reason}_{ts}.xml"
        path.write_text(xml_str or "", encoding="utf-8")
        return str(path)
    except Exception as e:
        logger.warning(f"Failed to save switch failure snapshot: {e}")
        return None


def _navigate_to_login_screen() -> dict:
    """Navigate from open account switcher to the login form.
    
    Taps "Add Instagram account" → "Log into existing account".
    After this, the device shows the login screen ready for credentials.
    The LLM agent in login mode can take over from here.
    
    Returns:
        dict with success and steps performed
    """
    import time
    steps = []
    
    try:
        # Tap "Add Instagram account"
        add_btn, _, _ = _find_with_retry("switcher_add_account")
        if not add_btn:
            return {"success": False, "error": "Could not find 'Add Instagram account' button", "steps": steps}
        
        _tap_element(add_btn, "Add Instagram account")
        steps.append("Tapped 'Add Instagram account'")
        time.sleep(1.5)
        
        # Check: choice screen or direct login?
        finder, _ = _dump_and_find()
        login_existing = finder.find("add_account_login_existing")
        
        if login_existing:
            _tap_element(login_existing, "Log into existing account")
            steps.append("Tapped 'Log into existing account'")
            time.sleep(1.5)
        else:
            # May have gone directly to login form
            steps.append("Choice screen not shown (direct login form)")
        
        return {"success": True, "steps": steps}
    except Exception as e:
        return {"success": False, "error": str(e), "steps": steps}


def _recover_from_login_screen(dm) -> None:
    """Press Back multiple times to escape login/2FA screen back to main Instagram."""
    import time
    from lamda.client import Keys
    
    logger.info("Recovering from login screen — pressing Back 3x...")
    for i in range(3):
        dm.device.press_key(Keys.KEY_BACK)
        time.sleep(1.0)
    
    # Try to open Instagram fresh
    try:
        app = dm.device.application("com.instagram.android")
        if not app.is_foreground():
            app.start()
            time.sleep(3.0)
    except Exception:
        pass
    logger.info("Recovery complete")


def _dismiss_switcher() -> None:
    """Dismiss the account switcher bottom sheet if open."""
    import time
    try:
        finder, _ = _dump_and_find()
        cancel = finder.find("switcher_cancel")
        if cancel:
            _tap_element(cancel, "Cancel switcher")
            time.sleep(0.5)
            return
        # Fallback: press back
        from .firerpa_tools import get_device_manager
        dm = get_device_manager()
        if dm:
            dm.device.press("back")
            time.sleep(0.5)
    except Exception:
        pass


# Navigation/system elements that appear in the XML but are NOT accounts
_SWITCHER_IGNORE_DESCS = {
    "cancel", "add instagram account", "go to accounts center", "meta logo",
    "profile", "home", "search and explore", "reels", "activity",
    "back", "overview", "search", "notifications", "direct",
    "share profile", "edit profile",
}


def _parse_switcher_accounts(finder) -> list[dict]:
    """Parse available accounts from the account switcher bottom sheet.
    
    Accounts in the switcher have content-desc set to the username,
    are inside recycler_view_container_id, and have selected attribute.
    We filter out navigation/system elements by checking against a blocklist
    and requiring the node to be inside the recycler container.
    """
    import xml.etree.ElementTree as ET
    
    # Find the recycler container bounds to scope our search
    recycler = finder.find("switcher_recycler")
    recycler_bounds = recycler.bounds if recycler else None
    
    accounts = []
    for node in finder._root.iter("node"):
        content_desc = node.get("content-desc", "")
        if not content_desc:
            continue
        
        # Skip known non-account items (case-insensitive)
        if content_desc.lower() in _SWITCHER_IGNORE_DESCS:
            continue
        
        selected = node.get("selected", "false") == "true"
        text = node.get("text", "")
        clickable = node.get("clickable", "false") == "true"
        long_clickable = node.get("long-clickable", "false") == "true"
        
        # Account rows are clickable + long-clickable ViewGroups
        # with content-desc = username and a selected attribute
        if not (clickable and long_clickable):
            continue
        
        # Extract username from content-desc.
        # Instagram appends notifications: "ooo.darlinggg, 2 chats and 42 more"
        # Extract just the username (before first comma).
        username = content_desc.split(",")[0].strip()
        
        # Username sanity: no spaces, reasonable length
        if " " in username or len(username) > 30 or len(username) < 2:
            continue
        
        # If we found the recycler, check bounds overlap
        if recycler_bounds:
            elem = finder._node_to_element(node)
            if elem.bounds[1] < recycler_bounds[1] or elem.bounds[3] > recycler_bounds[3]:
                continue
        else:
            elem = finder._node_to_element(node)
        
        accounts.append({
            "username": username,
            "text": text or username,
            "selected": selected,
            "element": elem,
        })
    
    return accounts


def switch_instagram_account(target_username: str) -> dict[str, Any]:
    """
    Switch to a different Instagram account on the device.
    
    Navigates through Instagram's in-app account switcher UI.
    Uses SmartElementFinder with registered selectors from selectors.py.
    
    Flow:
    1. Tap Profile tab
    2. Read current username from action_bar_title
    3. If already on target -> return success
    4. Tap username dropdown -> opens account switcher
    5. If target in list -> tap it -> verify
    6. If target not in list -> return need_login=True
    
    Args:
        target_username: Instagram username to switch to
        
    Returns:
        dict with:
            - success: bool
            - current_account: str (username after switch)
            - need_login: bool (if account needs add_account_login_flow)
            - available_accounts: list of logged-in usernames in switcher
            - steps_performed: list of actions taken
            - error: str if failed
    """
    
    steps = []
    target_lower = target_username.lower().replace("@", "")
    
    try:
        # Step 1: Navigate to Profile tab
        logger.info(f"Switching to account: {target_username}")
        
        profile_tab, _, _ = _find_with_retry("profile_tab")
        if not profile_tab:
            return {
                "success": False,
                "error": "Could not find Profile tab",
                "need_login": False,
                "steps_performed": steps,
            }
        
        _tap_element(profile_tab, "Profile tab")
        steps.append("Tapped Profile tab")
        time.sleep(0.5)
        
        # Step 2: Read current username only after profile header is truly ready.
        username_elem, finder, xml_str = _ensure_profile_header_ready(max_attempts=5)
        
        current_username = None
        if username_elem and username_elem.text:
            current_username = username_elem.text
        
        if current_username and current_username.lower() == target_lower:
            logger.info(f"Already on target account: {current_username}")
            return {
                "success": True,
                "current_account": current_username,
                "need_login": False,
                "steps_performed": steps,
                "message": "Already on target account",
            }
        
        # Step 3: Open account switcher
        dropdown = finder.find("profile_username_dropdown") if finder else None
        if not dropdown:
            dropdown = username_elem
        
        if not dropdown:
            # Retry: force profile refresh and re-check.
            logger.info("Username dropdown not found, forcing profile header refresh")
            feed_tab = finder.find("feed_tab") if finder else None
            if feed_tab:
                _tap_element(feed_tab, "Feed tab (bounce)")
                time.sleep(0.3)
                profile_tab2, _, _ = _find_with_retry("profile_tab", retries=3, delay=0.5)
                if profile_tab2:
                    _tap_element(profile_tab2, "Profile tab (retry)")
                    time.sleep(0.5)
                    username_elem, finder, xml_str = _ensure_profile_header_ready(max_attempts=4)
                    if username_elem and username_elem.text:
                        current_username = username_elem.text
                    dropdown = finder.find("profile_username_dropdown") or username_elem
        
        if not dropdown:
            snap_path = _save_switch_failure_snapshot(
                xml_str,
                reason="dropdown_missing_after_refresh",
            )
            if snap_path:
                logger.warning(f"Saved switch failure XML snapshot: {snap_path}")
            return {
                "success": False,
                "error": "Could not find username dropdown after profile refresh",
                "current_account": current_username,
                "need_login": False,
                "steps_performed": steps,
                "debug_xml_path": snap_path,
            }
        
        _tap_element(dropdown, "Username dropdown")
        steps.append("Tapped username dropdown")
        time.sleep(0.5)
        
        # Step 4: Parse account switcher
        switcher_finder, switcher_xml = _dump_and_find()[:2]
        
        # Verify we're on the switcher screen
        add_btn = switcher_finder.find("switcher_add_account")
        if not add_btn:
            # Retry once — animation may still be playing
            time.sleep(0.5)
            switcher_finder, switcher_xml = _dump_and_find()[:2]
            add_btn = switcher_finder.find("switcher_add_account")
        
        available = _parse_switcher_accounts(switcher_finder)
        available_usernames = [a["username"] for a in available]
        logger.info(f"Accounts in switcher: {available_usernames}")
        
        # Step 5: Find target in list
        target_entry = None
        for acc in available:
            if acc["username"].lower() == target_lower:
                target_entry = acc
                break
        
        if target_entry:
            if target_entry["selected"]:
                # Already selected but we didn't detect it earlier — dismiss and return
                cancel = switcher_finder.find("switcher_cancel")
                if cancel:
                    _tap_element(cancel, "Cancel switcher")
                time.sleep(0.5)
                return {
                    "success": True,
                    "current_account": target_entry["username"],
                    "need_login": False,
                    "available_accounts": available_usernames,
                    "steps_performed": steps,
                    "message": "Already on target account (detected in switcher)",
                }
            
            _tap_element(target_entry["element"], f"Account: {target_entry['username']}")
            steps.append(f"Tapped account: {target_entry['username']}")
            time.sleep(1.0)

            from .firerpa_tools import get_device_manager as _get_dm
            _dm = _get_dm()
            if _dm:
                from lamda.client import Keys
                for close_try in range(3):
                    _f, _ = _dump_and_find()
                    if not _f.find("switcher_add_account"):
                        break
                    _dm.device.press_key(Keys.KEY_BACK)
                    time.sleep(0.3)
                    steps.append(f"Dismissed lingering switcher (attempt {close_try + 1})")
            
            # Step 6: Verify switch success
            verify_elem, _, _ = _find_with_retry("profile_username_text", retries=3, delay=1.0)
            verified_username = verify_elem.text if verify_elem and verify_elem.text else None
            
            if verified_username and verified_username.lower() == target_lower:
                logger.info(f"Switch verified: {verified_username}")
                return {
                    "success": True,
                    "current_account": verified_username,
                    "need_login": False,
                    "available_accounts": available_usernames,
                    "steps_performed": steps,
                }
            
            _dismiss_switcher()
            time.sleep(0.3)
            profile_tab_retry, _, _ = _find_with_retry("profile_tab")
            if profile_tab_retry:
                _tap_element(profile_tab_retry, "Profile tab (verify retry)")
                time.sleep(0.5)
                verify2, _, _ = _find_with_retry("profile_username_text", retries=2, delay=1.0)
                if verify2 and verify2.text and verify2.text.lower() == target_lower:
                    logger.info(f"Switch verified on retry: {verify2.text}")
                    return {
                        "success": True,
                        "current_account": verify2.text,
                        "need_login": False,
                        "available_accounts": available_usernames,
                        "steps_performed": steps,
                    }
            
            logger.error(
                f"Switch verification FAILED: expected @{target_username}, "
                f"got @{verified_username}"
            )
            return {
                "success": False,
                "current_account": verified_username,
                "need_login": False,
                "available_accounts": available_usernames,
                "steps_performed": steps,
                "error": f"Verification failed: action_bar shows '{verified_username}' not '{target_username}'",
            }
        
        else:
            # Target not in switcher — need login flow
            logger.info(
                f"Account {target_username} not in switcher. "
                f"Available: {available_usernames}"
            )
            return {
                "success": False,
                "current_account": current_username,
                "need_login": True,
                "available_accounts": available_usernames,
                "steps_performed": steps,
                "message": f"Account {target_username} not logged in. Use add_account_login_flow().",
            }
    
    except Exception as e:
        logger.error(f"Failed to switch account: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "need_login": False,
            "steps_performed": steps,
        }


def handle_post_login_dialogs(max_rounds: int = 5, delay: float = 1.5) -> list[str]:
    """
    Handle sequential post-login prompts from Instagram.
    
    After a fresh login, Instagram may show multiple prompts in sequence:
    - "Save your login info?" -> Save (ACCEPT)
    - "Turn on Notifications?" -> Not Now (DISMISS)
    - Camera/Storage permissions -> Allow (ACCEPT)
    - "Add your phone number" -> Not Now (DISMISS)
    - "Find friends" -> Skip (DISMISS)
    
    Loops up to max_rounds times, checking and handling dialogs.
    Stops early if no dialog found (reached main screen).
    
    Args:
        max_rounds: Maximum number of dialog check rounds
        delay: Seconds to wait between rounds
        
    Returns:
        List of dialog types that were handled
    """
    import time
    from .dialog_handler import DialogHandler
    from .firerpa_tools import get_device_manager
    from lamda.client import Point
    
    dm = get_device_manager()
    handled_dialogs = []
    
    def _tap(x: int, y: int) -> bool:
        dm.device.click(Point(x=x, y=y))
        return True
    
    def _press_back() -> bool:
        dm.device.press("back")
        return True
    
    handler = DialogHandler(tap_func=_tap, press_back_func=_press_back)
    
    for round_num in range(max_rounds):
        time.sleep(delay)
        
        _, xml_str = _dump_and_find()
        handled, dialog_type = handler.handle(xml_str)
        
        if handled and dialog_type:
            logger.info(f"Post-login dialog handled (round {round_num + 1}): {dialog_type.value}")
            handled_dialogs.append(dialog_type.value)
        else:
            # No dialog found — we've reached the main screen
            logger.info(f"No more post-login dialogs after {round_num + 1} rounds")
            break
    
    return handled_dialogs


def add_account_login_flow(
    account_id: str,
    from_switcher: bool = True,
) -> dict[str, Any]:
    """
    Full login flow for adding an account not yet on the device.
    
    Called when switch_instagram_account() returns need_login=True.
    Assumes the account switcher is currently open (from_switcher=True).
    
    Flow:
    1. Tap "Add Instagram account" in switcher
    2. Tap "Log into existing account"
    3. Type username
    4. Type password
    5. Tap "Log in"
    6. Handle 2FA if prompted
    7. Handle post-login dialogs (Save info, Notifications, etc.)
    8. Verify landing on feed/profile
    
    Args:
        account_id: Account identifier (matches config/accounts/{id}.yaml)
        from_switcher: If True, assumes account switcher is already open.
                       If False, opens switcher first.
        
    Returns:
        dict with:
            - success: bool
            - account_id: str
            - username: str
            - steps_performed: list
            - handled_dialogs: list of post-login dialogs handled
            - error: str if failed
    """
    import time
    from .firerpa_tools import get_device_manager
    from lamda.client import Point
    
    dm = get_device_manager()
    steps = []
    
    # Load credentials
    creds = get_account_credentials(account_id)
    if not creds.get("success"):
        _dismiss_switcher()
        return {
            "success": False,
            "account_id": account_id,
            "error": f"Failed to load credentials: {creds.get('error', 'unknown')}",
            "steps_performed": steps,
        }
    
    username = creds["username"]
    password = creds["password"]
    
    try:
        # Step 1: If not from switcher, open it
        if not from_switcher:
            profile_tab, _, _ = _find_with_retry("profile_tab")
            if profile_tab:
                _tap_element(profile_tab, "Profile tab")
                time.sleep(0.8)
                dropdown, _, _ = _find_with_retry("profile_username_dropdown")
                if dropdown:
                    _tap_element(dropdown, "Username dropdown")
                    time.sleep(1.0)
                    steps.append("Opened account switcher")
        
        # Step 2: Tap "Add Instagram account"
        add_btn, finder, _ = _find_with_retry("switcher_add_account")
        if not add_btn:
            return {
                "success": False,
                "account_id": account_id,
                "username": username,
                "error": "Could not find 'Add Instagram account' button",
                "steps_performed": steps,
            }
        
        _tap_element(add_btn, "Add Instagram account")
        steps.append("Tapped 'Add Instagram account'")
        time.sleep(1.5)
        
        # Step 3: Detect which screen appeared
        # Instagram may show either:
        #   a) Choice screen: "Log into existing account" / "Create new account"
        #   b) Login form directly (common when only 1 account on device)
        finder_after_add, _ = _dump_and_find()
        login_existing_btn = finder_after_add.find("add_account_login_existing")
        username_field_direct = finder_after_add.find("login_username_field")
        
        if login_existing_btn:
            # Path A: choice screen shown — tap "Log into existing account"
            _tap_element(login_existing_btn, "Log into existing account")
            steps.append("Tapped 'Log into existing account'")
            time.sleep(1.5)
        elif username_field_direct:
            # Path B: Instagram went straight to login form — skip choice screen
            logger.info("Instagram skipped choice screen, already on login form")
            steps.append("Login form shown directly (choice screen skipped)")
        else:
            # Unknown state — try waiting and checking again
            time.sleep(1.5)
            login_existing_btn, _, _ = _find_with_retry("add_account_login_existing")
            if login_existing_btn:
                _tap_element(login_existing_btn, "Log into existing account")
                steps.append("Tapped 'Log into existing account' (retry)")
                time.sleep(1.5)
            else:
                _dismiss_switcher()
                return {
                    "success": False,
                    "account_id": account_id,
                    "username": username,
                    "error": "Could not find login form or choice screen after 'Add account'",
                    "steps_performed": steps,
                }
        
        # Step 4: Type username
        username_field, _, _ = _find_with_retry("login_username_field", retries=3, delay=1.0)
        if not username_field:
            return {
                "success": False,
                "account_id": account_id,
                "username": username,
                "error": "Could not find username field on login screen",
                "steps_performed": steps,
            }
        
        _tap_element(username_field, "Username field")
        time.sleep(0.5)
        
        # Type username via FIRERPA set_text on focused EditText
        _type_into_focused_field(dm, username)
        steps.append(f"Typed username: {username}")
        time.sleep(0.3)
        
        # Step 5: Type password
        password_field, _, _ = _find_with_retry("login_password_field")
        if not password_field:
            return {
                "success": False,
                "account_id": account_id,
                "username": username,
                "error": "Could not find password field",
                "steps_performed": steps,
            }
        
        _tap_element(password_field, "Password field")
        time.sleep(0.5)
        
        _type_into_focused_field(dm, password)
        steps.append("Typed password")
        time.sleep(0.3)
        
        # Step 6: Tap "Log in"
        login_submit, _, _ = _find_with_retry("login_button")
        if not login_submit:
            return {
                "success": False,
                "account_id": account_id,
                "username": username,
                "error": "Could not find 'Log in' button",
                "steps_performed": steps,
            }
        
        _tap_element(login_submit, "Log in")
        steps.append("Tapped 'Log in'")
        
        # Step 7: Check for 2FA (with retry — screen may take time to load)
        # From XML dump 05: "Go to your authentication app" text + EditText with
        # content-desc="Code," + Button content-desc="Continue" (initially disabled)
        twofa_detected = False
        code_input = None
        finder = None
        
        for twofa_attempt in range(5):
            time.sleep(2.0)
            finder, xml_str = _dump_and_find()
            
            # Detect 2FA: look for the Code input field (most reliable signal)
            for node in finder._root.iter("node"):
                desc = node.get("content-desc", "")
                if desc == "Code," and node.get("class", "") == "android.widget.EditText":
                    code_input = finder._node_to_element(node)
                    twofa_detected = True
                    break
            
            if twofa_detected:
                break
            
            # Fallback: check for text indicators
            for indicator in ["authentication app", "6-digit code", "two-factor"]:
                if finder.find_by_text(indicator, partial=True):
                    twofa_detected = True
                    break
            
            if twofa_detected:
                break
            
            # Check if we already landed on feed/profile (no 2FA needed)
            from .screen_detector import detect_screen as _detect
            from .action_models import ScreenContext as _SC
            screen_check = _detect(xml_str)
            if screen_check.context in {_SC.INSTAGRAM_FEED, _SC.INSTAGRAM_PROFILE}:
                logger.info("No 2FA — landed directly on feed/profile")
                break
            
            logger.info(f"2FA check attempt {twofa_attempt + 1}/5 — screen: {screen_check.context.value}")
        
        if twofa_detected:
            logger.info("2FA screen detected, generating code...")
            twofa_result = generate_2fa_code(account_id)
            
            if not twofa_result.get("success"):
                return {
                    "success": False,
                    "account_id": account_id,
                    "username": username,
                    "error": f"2FA code generation failed: {twofa_result.get('error')}",
                    "steps_performed": steps,
                }
            
            code = twofa_result["code"]
            
            # Find code field if not already found
            if not code_input:
                for node in finder._root.iter("node"):
                    if node.get("class", "") == "android.widget.EditText":
                        code_input = finder._node_to_element(node)
                        break
            
            if not code_input:
                return {
                    "success": False,
                    "account_id": account_id,
                    "username": username,
                    "error": "2FA screen detected but could not find code input field",
                    "steps_performed": steps,
                }
            
            # Tap code field and type code
            _tap_element(code_input, "2FA code field (content-desc=Code)")
            time.sleep(0.5)
            _type_into_focused_field(dm, code)
            steps.append(f"Typed 2FA code: {code}")
            logger.info(f"Typed 2FA code: {code}")
            time.sleep(1.5)
            
            # Find Continue button (it becomes enabled after code is entered)
            # From dump 05: content-desc="Continue"
            finder2, _ = _dump_and_find()
            confirm = None
            for node in finder2._root.iter("node"):
                desc = node.get("content-desc", "")
                if desc == "Continue" and node.get("clickable", "") == "true":
                    confirm = finder2._node_to_element(node)
                    break
            
            if not confirm:
                confirm = finder2.find_by_text("Continue", partial=False)
            if not confirm:
                confirm = finder2.find_by_text("Confirm", partial=False)
            if not confirm:
                confirm = finder2.find_by_text("Next", partial=False)
            
            if confirm:
                _tap_element(confirm, "Continue (2FA)")
                steps.append("Tapped Continue for 2FA")
                time.sleep(4.0)
            else:
                logger.warning("Could not find Continue button after 2FA — trying press BACK to re-enter")
                steps.append("Continue button not found after 2FA")
        
        # Step 8: Handle post-login dialogs
        handled_dialogs = handle_post_login_dialogs(max_rounds=5, delay=1.5)
        steps.append(f"Handled {len(handled_dialogs)} post-login dialogs")
        
        # Step 9: Verify we landed on feed or profile
        from .screen_detector import detect_screen
        from .action_models import ScreenContext
        
        _, final_xml = _dump_and_find()
        screen = detect_screen(final_xml)
        
        success_screens = {
            ScreenContext.INSTAGRAM_FEED,
            ScreenContext.INSTAGRAM_PROFILE,
            ScreenContext.INSTAGRAM_OTHER,
        }
        
        if screen.context in success_screens:
            logger.info(f"Login successful for {username}, landed on {screen.context.value}")
            return {
                "success": True,
                "account_id": account_id,
                "username": username,
                "screen": screen.context.value,
                "handled_dialogs": handled_dialogs,
                "steps_performed": steps,
            }
        else:
            logger.error(
                f"Login FAILED — landed on unexpected screen: {screen.context.value}"
            )
            _recover_from_login_screen(dm)
            return {
                "success": False,
                "account_id": account_id,
                "username": username,
                "screen": screen.context.value,
                "handled_dialogs": handled_dialogs,
                "steps_performed": steps,
                "error": f"Login incomplete — landed on {screen.context.value} instead of feed/profile",
            }
    
    except Exception as e:
        logger.error(f"add_account_login_flow failed for {account_id}: {e}", exc_info=True)
        try:
            _recover_from_login_screen(dm)
        except Exception:
            pass
        return {
            "success": False,
            "account_id": account_id,
            "username": username if 'username' in dir() else account_id,
            "error": str(e),
            "steps_performed": steps,
        }


def get_logged_in_accounts() -> dict[str, Any]:
    """
    Check which Instagram account is currently active WITHOUT navigating.
    
    NON-DESTRUCTIVE: Only reads the current screen XML.
    Does NOT tap profile tab, does NOT open account switcher.
    
    Detects the active account by checking:
    1. Story tray on feed (shows "Your story" with username)
    2. Profile page action bar title
    3. Any visible username indicators
    
    Returns:
        dict with:
            - success: bool
            - current_account: str (currently active account, if detectable)
            - is_logged_in: bool (whether Instagram UI is visible)
            - screen_type: str (current screen detected)
            - error: str if failed
    """
    from .firerpa_tools import get_device_manager
    from .screen_detector import detect_screen
    import xml.etree.ElementTree as ET
    
    dm = get_device_manager()
    
    try:
        # Get current screen XML (read-only, no navigation!)
        xml_bytes = dm.device.dump_window_hierarchy()
        xml_str = xml_bytes.getvalue().decode("utf-8")
        root = ET.fromstring(xml_str)
        
        # Detect screen type
        screen = detect_screen(xml_str)
        screen_type = screen.context.value if hasattr(screen.context, 'value') else str(screen.context)
        
        # Check if we're in Instagram at all
        is_instagram = "instagram" in screen_type.lower() or screen.package == "com.instagram.android"
        
        # If not in Instagram, can't determine account
        if not is_instagram:
            return {
                "success": True,
                "current_account": None,
                "is_logged_in": False,
                "screen_type": screen_type,
                "message": "Not in Instagram. App may need to be opened.",
            }
        
        # Look for username indicators (non-destructive scan)
        current_account = None
        
        # Method 1: Check action_bar_title (visible on profile page)
        for node in root.iter("node"):
            resource_id = node.get("resource-id", "").lower()
            if "action_bar_title" in resource_id:
                text = node.get("text", "")
                if text and len(text) < 30 and ("." in text or "_" in text):
                    current_account = text
                    break
        
        # Method 2: Check for username in profile header area
        if not current_account:
            for node in root.iter("node"):
                content_desc = (node.get("content-desc", "") or "").lower()
                text = node.get("text", "")
                # Profile tab shows username in content-desc
                if "profile" in content_desc and "tab" in content_desc:
                    # Extract username from content-desc like "sandraaaa.cook's profile"
                    if "'s profile" in content_desc:
                        current_account = content_desc.split("'s profile")[0].strip()
                        break
        
        # Any Instagram authenticated screen = logged in
        # Match actual ScreenContext enum values from screen_detector.py
        is_logged_in = screen_type in [
            "instagram_feed", "instagram_profile", "instagram_reels",
            "instagram_search", "instagram_dm", "instagram_stories",
            "instagram_notifications", "instagram_comments",
            "instagram_post_detail", "instagram_other",
        ]
        
        return {
            "success": True,
            "current_account": current_account,
            "is_logged_in": is_logged_in,
            "screen_type": screen_type,
        }
        
    except Exception as e:
        logger.error(f"Failed to check logged in accounts: {e}")
        return {
            "success": False,
            "error": str(e),
        }


# =============================================================================
# AGENT TOOL WRAPPERS
# =============================================================================
# These are the tools exposed to the LLM agent

def create_auth_tools() -> list:
    """
    Create authentication tools for agent use.
    
    Returns:
        List of FunctionTool objects for auth operations
    """
    from google.adk.tools import FunctionTool
    
    return [
        # Instagram auth
        FunctionTool(generate_2fa_code),
        FunctionTool(get_account_credentials),
        FunctionTool(verify_2fa_setup),
        FunctionTool(switch_instagram_account),
        FunctionTool(add_account_login_flow),
        FunctionTool(get_logged_in_accounts),
        # Gmail / Play Store auth
        FunctionTool(get_gmail_credentials),
        FunctionTool(generate_gmail_2fa_code),
        FunctionTool(generate_2fa_code_raw),
    ]
