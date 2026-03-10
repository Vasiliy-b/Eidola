"""Instagram UI element selectors registry.

This module contains known selectors for Instagram UI elements.
Each selector has a primary identifier and fallbacks for when the UI changes.

Version tracking helps identify when selectors need updating after Instagram updates.
"""

from typing import TypedDict

# Version: Update this when Instagram changes UI and selectors need updating
SELECTOR_VERSION = "2026-02-20"

# Minimum confidence threshold for screen detection
MIN_CONFIDENCE = 0.6


class SelectorConfig(TypedDict, total=False):
    """Configuration for a single selector."""
    resourceId: str
    text: str
    textContains: str
    description: str  # content-desc attribute
    descriptionContains: str
    className: str
    clickable: bool
    focusable: bool
    enabled: bool
    instance: int  # For multiple matching elements, 0-indexed


class ElementSelector(TypedDict):
    """Element selector with primary and fallback options."""
    primary: SelectorConfig
    fallbacks: list[SelectorConfig]


# ============================================================================
# INSTAGRAM SELECTORS REGISTRY
# ============================================================================

INSTAGRAM_SELECTORS: dict[str, ElementSelector] = {
    # -------------------------------------------------------------------------
    # Bottom Navigation Bar
    # -------------------------------------------------------------------------
    "feed_tab": {
        "primary": {"resourceId": "com.instagram.android:id/feed_tab"},
        "fallbacks": [
            {"description": "Home"},
            {"descriptionContains": "Home"},
            {"descriptionContains": "Feed"},
        ],
    },
    "search_tab": {
        "primary": {"resourceId": "com.instagram.android:id/search_tab"},
        "fallbacks": [
            {"description": "Search and explore"},
            {"description": "Search"},
            {"descriptionContains": "Search"},
        ],
    },
    "reels_tab": {
        "primary": {"resourceId": "com.instagram.android:id/clips_tab"},
        "fallbacks": [
            {"description": "Reels"},
            {"descriptionContains": "Reels"},
        ],
    },
    "notifications_tab": {
        "primary": {"resourceId": "com.instagram.android:id/activity_tab"},
        "fallbacks": [
            {"description": "Activity"},
            {"description": "Notifications"},
            {"descriptionContains": "Activity"},
        ],
    },
    "profile_tab": {
        "primary": {"resourceId": "com.instagram.android:id/profile_tab"},
        "fallbacks": [
            {"description": "Profile"},
            {"descriptionContains": "Profile"},
        ],
    },
    
    # -------------------------------------------------------------------------
    # Feed Post Interactions
    # -------------------------------------------------------------------------
    "like_button": {
        # Not liked yet - content-desc="Like", selected="false"
        "primary": {"resourceId": "com.instagram.android:id/row_feed_button_like", "description": "Like"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/row_feed_button_like"},
            {"description": "Like"},
            {"descriptionContains": "Like"},
        ],
    },
    "like_button_liked": {
        # Already liked - content-desc="Liked", selected="true"
        "primary": {"resourceId": "com.instagram.android:id/row_feed_button_like", "description": "Liked"},
        "fallbacks": [
            {"description": "Liked"},
        ],
    },
    "unlike_button": {
        # Same as like_button_liked - for removing like
        "primary": {"resourceId": "com.instagram.android:id/row_feed_button_like", "description": "Liked"},
        "fallbacks": [
            {"description": "Liked"},
            {"description": "Unlike"},
        ],
    },
    "comment_button": {
        "primary": {"resourceId": "com.instagram.android:id/row_feed_button_comment"},
        "fallbacks": [
            {"description": "Comment"},
            {"descriptionContains": "Comment"},
        ],
    },
    "share_button": {
        "primary": {"resourceId": "com.instagram.android:id/row_feed_button_share"},
        "fallbacks": [
            {"description": "Share"},
            {"descriptionContains": "Send"},
            {"descriptionContains": "Share"},
        ],
    },
    "save_button": {
        # Not saved - content-desc="Add to Saved", selected="false"
        "primary": {"resourceId": "com.instagram.android:id/row_feed_button_save", "description": "Add to Saved"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/row_feed_button_save"},
            {"description": "Add to Saved"},
            {"descriptionContains": "Save"},
        ],
    },
    "save_button_saved": {
        # Already saved - content-desc="Remove from saved", selected="true"
        "primary": {"resourceId": "com.instagram.android:id/row_feed_button_save", "description": "Remove from saved"},
        "fallbacks": [
            {"description": "Remove from saved"},
            {"descriptionContains": "Remove from saved"},
        ],
    },
    "more_options_button": {
        "primary": {"resourceId": "com.instagram.android:id/row_feed_button_more"},
        "fallbacks": [
            {"description": "More options"},
            {"descriptionContains": "More"},
        ],
    },
    
    # -------------------------------------------------------------------------
    # Feed Content Elements
    # -------------------------------------------------------------------------
    "feed_container": {
        "primary": {"resourceId": "com.instagram.android:id/recycler_view"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/main_feed_container"},
        ],
    },
    "post_image": {
        "primary": {"resourceId": "com.instagram.android:id/media_group"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/image_button"},
            {"className": "android.widget.ImageView", "clickable": True},
        ],
    },
    "post_image_container": {
        # For double-tap to like - larger tappable area
        "primary": {"resourceId": "com.instagram.android:id/carousel_media_group"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/zoomable_view_container"},
            {"resourceId": "com.instagram.android:id/media_group"},
        ],
    },
    "post_caption": {
        "primary": {"resourceId": "com.instagram.android:id/row_feed_comment_textview_layout"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/row_feed_comment"},
        ],
    },
    "post_username": {
        "primary": {"resourceId": "com.instagram.android:id/row_feed_photo_profile_name"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/row_feed_profile_name_textview"},
        ],
    },
    "likes_count": {
        "primary": {"resourceId": "com.instagram.android:id/row_feed_textview_likes"},
        "fallbacks": [
            {"textContains": "likes"},
            {"textContains": "like"},
        ],
    },
    
    # -------------------------------------------------------------------------
    # Stories
    # -------------------------------------------------------------------------
    "stories_tray": {
        # Horizontal list of story avatars at top of feed (NOT the viewer)
        "primary": {"resourceId": "com.instagram.android:id/stories_tray"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/reel_tray"},
            {"resourceId": "com.instagram.android:id/reels_tray_container"},
        ],
    },
    "story_ring": {
        # Colored ring around story avatar (unwatched stories)
        "primary": {"resourceId": "com.instagram.android:id/story_ring_colored"},
        "fallbacks": [
            {"descriptionContains": "story"},
            {"descriptionContains": "Story"},
        ],
    },
    "story_frame": {
        # Container when viewing a story
        "primary": {"resourceId": "com.instagram.android:id/reel_viewer_media_container"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/story_viewer_container"},
            {"resourceId": "com.instagram.android:id/reel_viewer_root"},
        ],
    },
    "story_username": {
        # Username displayed in story viewer
        "primary": {"resourceId": "com.instagram.android:id/reel_viewer_username"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/story_viewer_username"},
            {"resourceId": "com.instagram.android:id/reel_viewer_title"},
        ],
    },
    "story_like_button": {
        # Like button in story viewer (heart icon)
        # NOTE: No generic fallbacks to avoid collision with feed like_button
        "primary": {"resourceId": "com.instagram.android:id/reel_viewer_like_button"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/story_viewer_like_button"},
        ],
    },
    "story_reply_input": {
        # "Send message" input in story viewer
        "primary": {"resourceId": "com.instagram.android:id/reel_viewer_text_composer"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/story_composer_edittext"},
            {"className": "android.widget.EditText", "focusable": True},
        ],
    },
    
    # -------------------------------------------------------------------------
    # Comments Screen
    # -------------------------------------------------------------------------
    "comment_input": {
        "primary": {"resourceId": "com.instagram.android:id/layout_comment_thread_edittext"},
        "fallbacks": [
            {"resourceId": "com.instagram.android:id/comment_text"},
            {"textContains": "Add a comment"},
            {"descriptionContains": "Add a comment"},
        ],
    },
    "post_comment_button": {
        "primary": {"resourceId": "com.instagram.android:id/layout_comment_thread_post_button"},
        "fallbacks": [
            {"text": "Post"},
            {"description": "Post"},
        ],
    },
    
    # -------------------------------------------------------------------------
    # Profile Screen
    # -------------------------------------------------------------------------
    "follow_button": {
        "primary": {"resourceId": "com.instagram.android:id/profile_header_follow_button"},
        "fallbacks": [
            {"text": "Follow"},
            {"textContains": "Follow"},
        ],
    },
    "following_button": {
        "primary": {"resourceId": "com.instagram.android:id/profile_header_follow_button"},
        "fallbacks": [
            {"text": "Following"},
            {"textContains": "Following"},
        ],
    },
    "message_button": {
        "primary": {"resourceId": "com.instagram.android:id/profile_header_actions_container"},
        "fallbacks": [
            {"text": "Message"},
            {"description": "Message"},
        ],
    },
    "profile_grid": {
        "primary": {"resourceId": "com.instagram.android:id/profile_tab_layout"},
        "fallbacks": [
            {"description": "Grid View"},
        ],
    },
    "followers_count": {
        "primary": {"resourceId": "com.instagram.android:id/row_profile_header_textview_followers_count"},
        "fallbacks": [
            {"textContains": "followers"},
        ],
    },
    "following_count": {
        "primary": {"resourceId": "com.instagram.android:id/row_profile_header_textview_following_count"},
        "fallbacks": [
            {"textContains": "following"},
        ],
    },
    
    # -------------------------------------------------------------------------
    # Search Screen
    # -------------------------------------------------------------------------
    "search_input": {
        "primary": {"resourceId": "com.instagram.android:id/action_bar_search_edit_text"},
        "fallbacks": [
            {"text": "Search"},
            {"descriptionContains": "Search"},
        ],
    },
    "search_result_item": {
        "primary": {"resourceId": "com.instagram.android:id/row_search_user_username"},
        "fallbacks": [],
    },
    
    # -------------------------------------------------------------------------
    # Direct Messages
    # -------------------------------------------------------------------------
    "dm_inbox": {
        "primary": {"resourceId": "com.instagram.android:id/action_bar_inbox_button"},
        "fallbacks": [
            {"description": "Direct"},
            {"descriptionContains": "Message"},
        ],
    },
    "dm_compose": {
        "primary": {"resourceId": "com.instagram.android:id/row_inbox_new_message"},
        "fallbacks": [
            {"description": "New message"},
        ],
    },
    "dm_input": {
        "primary": {"resourceId": "com.instagram.android:id/row_thread_composer_edittext"},
        "fallbacks": [
            {"textContains": "Message"},
        ],
    },
    "dm_send_button": {
        "primary": {"resourceId": "com.instagram.android:id/row_thread_composer_send_button"},
        "fallbacks": [
            {"description": "Send"},
        ],
    },
    
    # -------------------------------------------------------------------------
    # Reels
    # -------------------------------------------------------------------------
    "reel_like_button": {
        "primary": {"resourceId": "com.instagram.android:id/like_button"},
        "fallbacks": [
            {"description": "Like"},
            {"descriptionContains": "Like"},
        ],
    },
    "reel_comment_button": {
        "primary": {"resourceId": "com.instagram.android:id/comment_button"},
        "fallbacks": [
            {"description": "Comment"},
        ],
    },
    "reel_share_button": {
        "primary": {"resourceId": "com.instagram.android:id/share_button"},
        "fallbacks": [
            {"description": "Share"},
        ],
    },
    
    # -------------------------------------------------------------------------
    # Account Switcher (from debug_xml/01-04 dumps)
    # -------------------------------------------------------------------------
    "profile_username_dropdown": {
        # Tappable username container at top of profile page — opens account switcher
        "primary": {"resourceId": "com.instagram.android:id/action_bar_username_container"},
        "fallbacks": [
            # Some app variants expose only title/chevron descendants;
            # tapping their coordinates still opens the parent dropdown container.
            {"resourceId": "com.instagram.android:id/action_bar_title"},
            {"resourceId": "com.instagram.android:id/action_bar_title_chevron"},
            {"resourceId": "com.instagram.android:id/action_bar_username_buttons_container"},
        ],
    },
    "profile_username_text": {
        # Username text inside the action bar (read-only, for detecting current account)
        "primary": {"resourceId": "com.instagram.android:id/action_bar_title"},
        "fallbacks": [],
    },
    "switcher_cancel": {
        # Cancel button at top of the account switcher bottom sheet
        "primary": {"description": "Cancel"},
        "fallbacks": [
            {"text": "Cancel"},
        ],
    },
    "switcher_add_account": {
        # "Add Instagram account" button in account switcher
        "primary": {"description": "Add Instagram account"},
        "fallbacks": [
            {"text": "Add Instagram account"},
            {"descriptionContains": "Add Instagram account"},
            {"textContains": "Add Instagram account"},
        ],
    },
    "switcher_accounts_center": {
        # "Go to Accounts Center" link in account switcher
        "primary": {"description": "Go to Accounts Center"},
        "fallbacks": [
            {"text": "Go to Accounts Center"},
        ],
    },
    "switcher_recycler": {
        # RecyclerView container holding account list in switcher
        "primary": {"resourceId": "com.instagram.android:id/recycler_view_container_id"},
        "fallbacks": [],
    },
    "add_account_login_existing": {
        # "Log into existing account" button on the Add Account choice screen
        "primary": {"description": "Log into existing account"},
        "fallbacks": [
            {"text": "Log into existing account"},
        ],
    },
    "add_account_create_new": {
        # "Create new account" button on the Add Account choice screen
        "primary": {"description": "Create new account"},
        "fallbacks": [
            {"text": "Create new account"},
        ],
    },
    "login_username_field": {
        # Username/email input on the login screen
        # Instagram uses different labels depending on context:
        #   - "Username, email or mobile number," (from Add Account → Log into existing)
        #   - "Phone number, email or username" (direct login screen)
        "primary": {"description": "Username, email or mobile number,", "className": "android.widget.EditText"},
        "fallbacks": [
            {"descriptionContains": "Username, email or mobile number"},
            {"descriptionContains": "Phone number, email or username"},
            {"text": "Phone number, email or username"},
            {"text": "Username, email or mobile number"},
            {"className": "android.widget.EditText", "focusable": True, "instance": 0},
        ],
    },
    "login_password_field": {
        # Password input on the login screen
        "primary": {"description": "Password,", "className": "android.widget.EditText"},
        "fallbacks": [
            {"descriptionContains": "Password"},
            {"text": "Password"},
            {"className": "android.widget.EditText", "focusable": True, "instance": 1},
        ],
    },
    "login_button": {
        # "Log in" button on the login screen
        "primary": {"description": "Log in", "className": "android.widget.Button"},
        "fallbacks": [
            {"description": "Log in"},
            {"text": "Log in"},
            {"text": "Log In"},
        ],
    },
    "login_close_button": {
        # Close/X button on the login screen (returns to account switcher)
        "primary": {"description": "Close"},
        "fallbacks": [
            {"text": "Close"},
            {"descriptionContains": "Close"},
        ],
    },
    
    # -------------------------------------------------------------------------
    # Common UI Elements
    # -------------------------------------------------------------------------
    "back_button": {
        "primary": {"resourceId": "com.instagram.android:id/action_bar_button_back"},
        "fallbacks": [
            {"description": "Back"},
            {"descriptionContains": "Back"},
            {"className": "android.widget.ImageButton", "clickable": True},
        ],
    },
    "close_button": {
        "primary": {"resourceId": "com.instagram.android:id/close_button"},
        "fallbacks": [
            {"description": "Close"},
            {"descriptionContains": "Close"},
            {"text": "Close"},
        ],
    },
    "cancel_button": {
        "primary": {"text": "Cancel"},
        "fallbacks": [
            {"description": "Cancel"},
        ],
    },
    "done_button": {
        "primary": {"text": "Done"},
        "fallbacks": [
            {"description": "Done"},
        ],
    },
}


# ============================================================================
# SCREEN SIGNATURE PATTERNS
# ============================================================================

SCREEN_SIGNATURES: dict[str, dict] = {
    "instagram_feed": {
        "package": "com.instagram.android",
        "required_resource_ids": [
            "com.instagram.android:id/feed_tab",
        ],
        "any_resource_ids": [
            # Feed-specific elements (distinguish from profile)
            "com.instagram.android:id/main_feed_action_bar",
            "com.instagram.android:id/title_logo",  # Instagram logo in feed
            "com.instagram.android:id/refreshable_container",
            "com.instagram.android:id/reels_tray_container",  # Stories tray
            "com.instagram.android:id/row_feed_photo_profile_header",
        ],
    },
    "instagram_profile": {
        "package": "com.instagram.android",
        # Profile is uniquely identified by profile_action_bar (present on all profile screens)
        "required_resource_ids": [
            "com.instagram.android:id/profile_action_bar",
        ],
        "any_resource_ids": [
            # Additional profile-specific elements for higher confidence
            "com.instagram.android:id/profile_header_container",
            "com.instagram.android:id/row_profile_header",
            "com.instagram.android:id/profile_header_full_name",
            "com.instagram.android:id/profile_header_follow_button",
            "com.instagram.android:id/profile_tab_layout",  # Profile tabs (Grid, Reels, etc.)
        ],
    },
    "instagram_search": {
        "package": "com.instagram.android",
        # Search requires the search input field to be visible (not just tab)
        # NOTE: Explore grid is now detected separately in screen_detector.py
        # using content-desc pattern "at row X, column Y"
        "required_resource_ids": [
            "com.instagram.android:id/action_bar_search_edit_text",
        ],
        "any_resource_ids": [
            "com.instagram.android:id/search_grid",
            "com.instagram.android:id/explore_grid",
        ],
    },
    "instagram_reels": {
        "package": "com.instagram.android",
        # Reels requires actual reels viewer/player elements (not just tab)
        "required_resource_ids": [
            "com.instagram.android:id/clips_viewer_view_pager",  # Main reels viewer
        ],
        "any_resource_ids": [
            "com.instagram.android:id/clips_viewer_container",
            "com.instagram.android:id/reel_viewer_like_button",
            "com.instagram.android:id/reel_viewer_comment_button",
            "com.instagram.android:id/clips_video_container",
        ],
    },
    "instagram_notifications": {
        "package": "com.instagram.android",
        # Notifications requires actual activity feed elements (not just tab)
        "required_resource_ids": [
            "com.instagram.android:id/activity_feed_container",
        ],
        "any_resource_ids": [
            "com.instagram.android:id/activity_feed_recycler_view",
            "com.instagram.android:id/activity_notification_row",
        ],
    },
    "instagram_dm": {
        "package": "com.instagram.android",
        "any_resource_ids": [
            "com.instagram.android:id/row_inbox",
            "com.instagram.android:id/direct_inbox",
        ],
    },
    "instagram_post_detail": {
        "package": "com.instagram.android",
        "any_resource_ids": [
            "com.instagram.android:id/media_set_row_content_holder",
            "com.instagram.android:id/row_feed_photo_imageview",
        ],
    },
    "instagram_comments": {
        "package": "com.instagram.android",
        "any_resource_ids": [
            "com.instagram.android:id/layout_comment_thread_edittext",
            "com.instagram.android:id/comment_composer",
        ],
    },
    "instagram_account_switcher": {
        "package": "com.instagram.android",
        # Bottom sheet with recycler_view_container_id + "Add Instagram account" button
        "required_resource_ids": [
            "com.instagram.android:id/recycler_view_container_id",
        ],
        "any_content_descs": [
            "Add Instagram account",
            "Cancel",
            "Go to Accounts Center",
        ],
    },
    "instagram_add_account": {
        "package": "com.instagram.android",
        # Choice screen: "Log into existing account" / "Create new account"
        "any_content_descs": [
            "Log into existing account",
            "Create new account",
        ],
        "any_texts": [
            "Add account",
        ],
    },
    "instagram_login": {
        "package": "com.instagram.android",
        # Login form with username/password fields and "Log in" button
        "any_content_descs": [
            "Username, email or mobile number,",
            "Password,",
            "Log in",
        ],
    },
    "system_ui": {
        "package": "com.android.systemui",
    },
    "launcher": {
        "package_contains": "launcher",
    },
}


def get_selector(name: str) -> ElementSelector | None:
    """Get selector by name."""
    return INSTAGRAM_SELECTORS.get(name)


def get_all_selectors(name: str) -> list[SelectorConfig]:
    """Get all selectors (primary + fallbacks) for an element name."""
    selector = INSTAGRAM_SELECTORS.get(name)
    if not selector:
        return []
    
    result = [selector["primary"]]
    result.extend(selector.get("fallbacks", []))
    return result


def get_screen_signature(screen_name: str) -> dict | None:
    """Get screen signature for detection."""
    return SCREEN_SIGNATURES.get(screen_name)
