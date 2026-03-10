"""
Shared ADK callbacks for token optimization.

Implements:
1. unified_before_model_callback — context trimming, image stripping, screenshot injection
2. compress_tool_response — after_tool_callback to truncate large tool outputs at source
3. Mode-specific tool filtering
"""

import copy
import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.tools import BaseTool, ToolContext
from google.genai import types

from ..config import settings

logger = logging.getLogger("eidola.agents.callbacks")


# ---------------------------------------------------------------------------
# Tool sets per mode — only these tools are exposed to the LLM
# Reduces ~4,500 tokens of tool schemas to ~1,200–2,500 depending on mode
# ---------------------------------------------------------------------------
CORE_TOOLS = {
    "detect_screen",
    "get_screen_elements",
    "tap",
    "long_press",
    "press_back",
    "press_home",
    "type_text",
    "clear_text",
    "tap_element",
    "element_exists",
    "wait_for_idle",
    "handle_dialog",
    "open_instagram",
    "restart_instagram",
    "force_close_instagram",
    "escape_to_instagram",
    "device_info",
    "check_connection",
    "screenshot",
}

FEED_TOOLS = {
    "analyze_feed_posts",
    "scroll_feed",
    "scroll_fast",
    "scroll_slow_browse",
    "scroll_back",
    "refresh_feed",
    "scroll_to_post_buttons",
    "double_tap_like",
    "save_post",
    "share_post",
    "is_post_liked",
    "is_post_saved",
    "watch_media",
    "watch_stories",
    "detect_post_type",
    "detect_carousel",
    "swipe_carousel",
    "find_element",
    "get_elements_for_ai",
    "open_notification_panel",
}

COMMENT_TOOLS = {
    "comment_on_post",
    "post_comment",
    "get_caption_info",
    "get_visible_comments",
}

PROFILE_TOOLS = {
    "follow_nurtured_account",
    "get_post_engagement_buttons",
}

COMPOUND_TOOLS = {
    "navigate_to_profile",
    "return_to_feed",
    "open_post_and_engage",
}

MEMORY_TOOLS = {
    "is_nurtured_account",
    "check_post_interaction",
    "get_next_nurtured_to_visit",
    "record_profile_visit",
    "get_nurtured_list",
    "record_action",
    "get_session_stats",
    "check_post_liked",
}

POSTING_TOOLS = {
    "get_posting_manifest",
    "report_posting_result",
}

AUTH_TOOLS = {
    "get_account_credentials",
    "generate_2fa_code",
    "switch_instagram_account",
    "verify_logged_in_account",
    "list_available_accounts",
    "get_device_gmail",
}

TOOLS_BY_MODE: dict[str, set[str]] = {
    "warmup": CORE_TOOLS | FEED_TOOLS | PROFILE_TOOLS | MEMORY_TOOLS | POSTING_TOOLS | COMPOUND_TOOLS,
    "feed_scroll": CORE_TOOLS | FEED_TOOLS | COMMENT_TOOLS | PROFILE_TOOLS | MEMORY_TOOLS | POSTING_TOOLS | COMPOUND_TOOLS,
    "active_engage": CORE_TOOLS | FEED_TOOLS | COMMENT_TOOLS | PROFILE_TOOLS | MEMORY_TOOLS | POSTING_TOOLS | COMPOUND_TOOLS,
    "nurture_accounts": CORE_TOOLS | FEED_TOOLS | COMMENT_TOOLS | PROFILE_TOOLS | MEMORY_TOOLS | POSTING_TOOLS | COMPOUND_TOOLS,
    "respond": CORE_TOOLS | FEED_TOOLS | COMMENT_TOOLS | MEMORY_TOOLS | COMPOUND_TOOLS,
    "login": CORE_TOOLS | AUTH_TOOLS,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_inline_image(part: Any) -> bool:
    """Check if a Part contains inline image data."""
    if hasattr(part, "inline_data") and part.inline_data is not None:
        return True
    return False


def _strip_large_values_from_dict(d: dict, max_str_len: int = 1500) -> dict:
    """Strip base64 and oversized strings from a dict (shallow copy)."""
    if not isinstance(d, dict):
        return d
    result = {}
    for key, value in d.items():
        if key in ("screenshots", "screenshot", "image_data", "base64"):
            if isinstance(value, list):
                result[f"_{key}_count"] = len(value)
            else:
                result[f"_{key}_stripped"] = True
            continue
        if key == "xml" and isinstance(value, str) and len(value) > 500:
            result["_xml_stripped"] = True
            result["_xml_size"] = len(value)
            continue
        if isinstance(value, str) and len(value) > max_str_len:
            result[key] = value[:400] + f"...[truncated, {len(value)} chars]"
            continue
        if isinstance(value, dict):
            result[key] = _strip_large_values_from_dict(value, max_str_len)
        elif isinstance(value, list):
            result[key] = [
                _strip_large_values_from_dict(item, max_str_len)
                if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def _make_clean_content(content: types.Content) -> types.Content:
    """Create a copy of Content with image Parts and large data stripped."""
    clean_parts = []
    for part in (content.parts or []):
        if _has_inline_image(part):
            clean_parts.append(types.Part(text="[image removed from history]"))
            continue

        if hasattr(part, "function_response") and part.function_response:
            fr = part.function_response
            if fr.response and isinstance(fr.response, dict):
                cleaned_response = _strip_large_values_from_dict(fr.response)
                new_fr = types.FunctionResponse(
                    name=fr.name,
                    response=cleaned_response,
                )
                clean_parts.append(types.Part(function_response=new_fr))
                continue

        clean_parts.append(part)

    return types.Content(role=content.role, parts=clean_parts)


# ---------------------------------------------------------------------------
# before_model_callback — unified
# ---------------------------------------------------------------------------

async def unified_before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> LlmResponse | None:
    """
    Combined before-model callback that handles:
    1. Context trimming (max N contents)
    2. Image Part stripping from old turns (copies, not in-place)
    3. Base64 / large-string cleanup in old tool responses
    4. Pending screenshot injection as native image Part (~258 tokens vs ~33K)
    5. Mode-specific tool filtering
    """
    try:
        contents = llm_request.contents
        if not contents:
            return None

        max_contents = settings.context_max_contents  # default 30
        keep_recent = 4  # keep these as-is (may contain current screenshots)

        # ----- 1. Context trimming + 2-3. Image/data stripping -----
        if len(contents) > max_contents:
            first_msg = contents[0]
            recent = contents[-max_contents:]
            if first_msg not in recent:
                recent = [first_msg] + recent[-(max_contents - 1):]
            contents = recent

        new_contents = []
        cutoff = max(0, len(contents) - keep_recent)

        for i, content in enumerate(contents):
            if i < cutoff:
                new_contents.append(_make_clean_content(content))
            else:
                new_contents.append(content)

        llm_request.contents = new_contents

        # ----- 4. Screenshot injection -----
        _inject_pending_screenshot(llm_request)

        # ----- 5. Mode-specific tool filtering -----
        _filter_tools_by_mode(callback_context, llm_request)

    except Exception as e:
        logger.warning(f"unified_before_model_callback error: {e}", exc_info=True)

    return None


def _inject_pending_screenshot(llm_request: LlmRequest) -> None:
    """Inject pending screenshot as a proper image Part (not base64 string)."""
    from ..tools.firerpa_tools import get_device_manager

    dm = get_device_manager()
    if dm is None:
        return

    screenshot_data = getattr(dm, "last_screenshot", None)
    screenshot_id = getattr(dm, "last_screenshot_id", None)

    if not screenshot_data or not screenshot_id:
        return

    # screenshot_data can be bytes or BytesIO — normalize to bytes
    if hasattr(screenshot_data, "getvalue"):
        img_bytes = screenshot_data.getvalue()
    elif isinstance(screenshot_data, (bytes, bytearray)):
        img_bytes = bytes(screenshot_data)
    else:
        logger.warning(f"Unexpected screenshot type: {type(screenshot_data)}")
        dm.last_screenshot = None
        dm.last_screenshot_id = None
        return

    img_size_kb = len(img_bytes) / 1024
    logger.info(
        f"📸 Injecting screenshot {screenshot_id} ({img_size_kb:.1f}KB) as native image Part"
    )

    image_part = types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")

    # Add as a NEW Content at the end — does NOT modify stored events
    llm_request.contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part(text=f"[Screenshot {screenshot_id}] Analyze this image:"),
                image_part,
            ],
        )
    )

    dm.last_screenshot = None
    dm.last_screenshot_id = None


def _filter_tools_by_mode(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> None:
    """Filter tool declarations based on current mode."""
    state = getattr(callback_context, "state", None)
    if state is None:
        logger.debug("Tool filter: no state on callback_context")
        return

    mode = None
    if isinstance(state, dict):
        mode = state.get("mode") or state.get("mode_config", {}).get("mode")
    elif hasattr(state, "get"):
        mode = state.get("mode")

    if not mode:
        logger.debug(f"Tool filter: mode not found in state (keys: {list(state.keys()) if hasattr(state, 'keys') else type(state)})")
        return

    if mode not in TOOLS_BY_MODE:
        logger.debug(f"Tool filter: mode '{mode}' not in TOOLS_BY_MODE")
        return

    allowed = TOOLS_BY_MODE[mode]

    if not (llm_request.config and llm_request.config.tools):
        return

    for tool in llm_request.config.tools:
        if hasattr(tool, "function_declarations") and tool.function_declarations:
            original_count = len(tool.function_declarations)
            tool.function_declarations = [
                fd for fd in tool.function_declarations if fd.name in allowed
            ]
            filtered_count = original_count - len(tool.function_declarations)
            if filtered_count > 0:
                logger.debug(
                    f"Tool filter [{mode}]: {original_count} → {len(tool.function_declarations)} "
                    f"({filtered_count} removed)"
                )


# ---------------------------------------------------------------------------
# after_tool_callback — compress tool responses at source
# ---------------------------------------------------------------------------

async def compress_tool_response(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    tool_response: dict,
) -> dict | None:
    """
    Compress large tool responses BEFORE they enter session history.

    Strips: XML dumps, base64 screenshots, and oversized strings.
    This is defense-in-depth alongside the before_model_callback.
    """
    if not isinstance(tool_response, dict):
        return tool_response

    try:
        modified = False
        tool_name = getattr(tool, "name", "unknown")

        # Strip XML (keep metadata)
        if "xml" in tool_response:
            xml_val = tool_response.get("xml", "")
            if isinstance(xml_val, str) and len(xml_val) > 500:
                tool_response["_xml_size"] = len(xml_val)
                tool_response["_xml_stripped"] = True
                del tool_response["xml"]
                modified = True

        # Strip base64 screenshots list
        if "screenshots" in tool_response:
            count = len(tool_response["screenshots"]) if isinstance(tool_response["screenshots"], list) else 1
            tool_response["screenshots_count"] = count
            del tool_response["screenshots"]
            modified = True

        # Truncate any remaining large string values
        for key in list(tool_response.keys()):
            val = tool_response[key]
            if isinstance(val, str) and len(val) > 2000 and key not in ("_xml_stripped",):
                tool_response[key] = val[:500] + f"...[truncated, was {len(val)} chars]"
                modified = True

        if modified:
            logger.debug(f"Compressed tool response for {tool_name}")

    except Exception as e:
        logger.warning(f"compress_tool_response error: {e}")

    return tool_response
