"""Posting tools for the Instagram agent.

Provides tools the agent calls during a posting session:
- get_posting_manifest: read what content to post (type, caption, media count)
- report_posting_result: mark success/failure in MongoDB
- type_posting_caption: type the caption from manifest (LLM-proof)
"""

import json
import logging

from google.adk.tools import FunctionTool

from ..content.posting_scheduler import mark_posting_result
from .firerpa_tools import get_device_manager

logger = logging.getLogger("eidola.tools.posting")

# Manifest data cached when get_posting_manifest() is called.
# report_posting_result() and type_posting_caption() use this
# instead of trusting LLM-provided arguments.
_last_manifest: dict | None = None

_CAPTION_BLOCKLIST = frozenset({
    "test post", "test caption", "#automated", "#testing", "#automation",
    "instagram agent", "scheduled test", "engagement mode", "our agent",
    "our bot", "automation", "this is a test", "test for",
    "beautiful test", "scheduled post", "test post caption",
})


def _caption_looks_suspicious(text: str) -> bool:
    """Check if caption text contains red-flag phrases."""
    lower = text.lower()
    return any(phrase in lower for phrase in _CAPTION_BLOCKLIST)


def get_posting_manifest() -> dict:
    """Read the posting manifest from the device.

    Returns the content that needs to be posted: type, caption, media count.
    The media files are already uploaded to /sdcard/DCIM/ToPost/ on the device.

    Returns:
        Dict with:
        - has_content: bool - whether there's content to post
        - posting_flow: str - "feed_photo", "feed_carousel", "feed_video", "reel"
        - caption: str - the caption text to use (may be empty — that is valid)
        - media_count: int - number of media files
        - content_id: str - content ID (used internally for reporting)
        - account_id: str - account ID (used internally for reporting)
    """
    global _last_manifest

    dm = get_device_manager()
    if dm is None:
        return {"has_content": False, "error": "No device connected"}

    try:
        from io import BytesIO
        manifest_path = "/sdcard/DCIM/ToPost/manifest.json"

        fd = BytesIO()
        dm.device.download_fd(manifest_path, fd)
        manifest = json.loads(fd.getvalue().decode("utf-8"))

        _last_manifest = {
            "content_id": manifest.get("content_id", ""),
            "account_id": manifest.get("account_id", ""),
            "caption": manifest.get("caption", ""),
        }

        caption = manifest.get("caption", "")
        return {
            "has_content": True,
            "posting_flow": manifest.get("posting_flow", "feed_photo"),
            "caption": caption,
            "has_caption": bool(caption and caption.strip()),
            "media_count": len(manifest.get("media", [])),
            "content_id": manifest.get("content_id", ""),
            "account_id": manifest.get("account_id", ""),
        }
    except FileNotFoundError:
        return {"has_content": False}
    except Exception as e:
        logger.warning("Failed to read posting manifest: %s", e)
        return {"has_content": False, "error": str(e)}


def report_posting_result(success: bool, error_message: str = "") -> dict:
    """Report the result of a posting attempt.

    Call this AFTER successfully sharing the post or after a failure.
    content_id and account_id are read automatically from the manifest
    that was loaded by get_posting_manifest().

    Args:
        success: True if post was shared successfully
        error_message: Error description if failed

    Returns:
        Confirmation dict.
    """
    global _last_manifest
    dm = get_device_manager()

    if _last_manifest is None:
        logger.info("report_posting_result: _last_manifest is None — already reported or never loaded")
        return {"reported": True, "note": "already reported (no manifest in memory)"}

    content_id = _last_manifest.get("content_id", "")
    account_id = _last_manifest.get("account_id", "")

    if not content_id or not account_id:
        if dm:
            try:
                from io import BytesIO
                fd = BytesIO()
                dm.device.download_fd("/sdcard/DCIM/ToPost/manifest.json", fd)
                manifest = json.loads(fd.getvalue().decode("utf-8"))
                content_id = content_id or manifest.get("content_id", "")
                account_id = account_id or manifest.get("account_id", "")
            except Exception as e:
                logger.error("report_posting_result: manifest read failed: %s", e)

    if not account_id or not content_id:
        logger.error(
            "report_posting_result: missing ids (content_id=%s, account_id=%s)",
            content_id, account_id,
        )
        return {"reported": False, "error": "Could not determine content_id/account_id"}

    try:
        mark_posting_result(
            account_id=account_id,
            content_id=content_id,
            success=success,
            error=error_message if not success else None,
        )

        if success and dm:
            try:
                dm.device.execute_script("rm -rf /sdcard/DCIM/ToPost/*")
                dm.device.execute_script(
                    "am broadcast -a android.intent.action.MEDIA_MOUNTED "
                    "-d file:///sdcard/DCIM"
                )
                logger.info("Cleaned posting folder + refreshed MediaStore")
            except Exception:
                pass

        status = "posted" if success else "failed"
        logger.info("Posting result reported: %s/%s -> %s", content_id, account_id, status)
        _last_manifest = None
        return {"reported": True, "status": status}

    except Exception as e:
        logger.error("Failed to report posting result: %s", e)
        return {"reported": False, "error": str(e)}


def type_posting_caption() -> dict:
    """Type the posting caption from the manifest.

    Reads the caption stored when get_posting_manifest() was called and types
    it into the active text field. If the manifest caption is empty, this tool
    skips typing (posts without caption are valid).

    Returns:
        Dict with typed=True/skipped=True and the caption text.
    """
    global _last_manifest

    if not _last_manifest:
        return {"error": "No manifest loaded — get_posting_manifest() was not called this session."}

    caption = _last_manifest.get("caption", "")

    if not caption or not caption.strip():
        logger.info("type_posting_caption: empty caption — skipping (post without text)")
        return {"skipped": True, "reason": "No caption in manifest — posting without text is fine."}

    if _caption_looks_suspicious(caption):
        logger.critical("type_posting_caption: BLOCKED suspicious caption: %r", caption)
        return {"error": f"Caption blocked by safety filter: {caption!r}"}

    from .firerpa_tools import get_type_text_fn
    _type_text = get_type_text_fn()
    if _type_text is None:
        return {"error": "type_text not initialized — device tools not created yet"}

    try:
        result = _type_text(caption)
        logger.info("type_posting_caption: typed %d chars", len(caption))
        return {"typed": True, "caption": caption, "chars": len(caption)}
    except Exception as e:
        logger.error("type_posting_caption: failed to type: %s", e)
        return {"error": str(e)}


def create_posting_tools() -> list[FunctionTool]:
    """Create posting-related FunctionTools for the agent."""
    return [
        FunctionTool(get_posting_manifest),
        FunctionTool(report_posting_result),
        FunctionTool(type_posting_caption),
    ]
