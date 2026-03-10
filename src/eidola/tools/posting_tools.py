"""Posting tools for the Instagram agent.

Provides tools the agent calls during a posting session:
- get_posting_manifest: read what content to post (type, caption, media count)
- report_posting_result: mark success/failure in MongoDB
"""

import json
import logging
from datetime import date

from google.adk.tools import FunctionTool

from ..content.posting_scheduler import (
    get_posting_info,
    mark_posting_result,
)
from .firerpa_tools import DeviceManager, get_device_manager

logger = logging.getLogger("eidola.tools.posting")


def get_posting_manifest() -> dict:
    """Read the posting manifest from the device.

    Returns the content that needs to be posted: type, caption, media count.
    The media files are already uploaded to /sdcard/DCIM/ToPost/ on the device.

    Returns:
        Dict with:
        - has_content: bool - whether there's content to post
        - posting_flow: str - "feed_photo", "feed_carousel", "feed_video", "reel"
        - caption: str - the caption text to use
        - media_count: int - number of media files
        - content_id: str - content ID for reporting result
    """
    dm = get_device_manager()
    if dm is None:
        return {"has_content": False, "error": "No device connected"}

    try:
        from io import BytesIO
        manifest_path = "/sdcard/DCIM/ToPost/manifest.json"

        fd = BytesIO()
        dm.device.download_fd(manifest_path, fd)
        manifest = json.loads(fd.getvalue().decode("utf-8"))

        return {
            "has_content": True,
            "posting_flow": manifest.get("posting_flow", "feed_photo"),
            "caption": manifest.get("caption", ""),
            "media_count": len(manifest.get("media", [])),
            "content_id": manifest.get("content_id", ""),
            "account_id": manifest.get("account_id", ""),
        }
    except FileNotFoundError:
        return {"has_content": False}
    except Exception as e:
        logger.warning("Failed to read posting manifest: %s", e)
        return {"has_content": False, "error": str(e)}


def report_posting_result(content_id: str, success: bool, error_message: str = "", account_id: str = "") -> dict:
    """Report the result of a posting attempt.

    Call this AFTER successfully sharing the post or after a failure.

    Args:
        content_id: The content_id from the manifest
        success: True if post was shared successfully
        error_message: Error description if failed
        account_id: Account ID (from manifest). If empty, reads from device as fallback.

    Returns:
        Confirmation dict.
    """
    dm = get_device_manager()

    # Use provided account_id, fallback to reading from device manifest
    if not account_id and dm:
        try:
            from io import BytesIO
            fd = BytesIO()
            dm.device.download_fd("/sdcard/DCIM/ToPost/manifest.json", fd)
            manifest = json.loads(fd.getvalue().decode("utf-8"))
            account_id = manifest.get("account_id", "")
        except Exception as e:
            logger.error("report_posting_result: manifest read failed: %s", e)

    if not account_id:
        logger.error("report_posting_result: no account_id — cannot record result for %s", content_id)
        return {"reported": False, "error": "Could not determine account_id"}

    try:
        mark_posting_result(
            account_id=account_id,
            content_id=content_id,
            success=success,
            error=error_message if not success else None,
        )

        # Cleanup device posting folder on success
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
        return {"reported": True, "status": status}

    except Exception as e:
        logger.error("Failed to report posting result: %s", e)
        return {"reported": False, "error": str(e)}


def create_posting_tools() -> list[FunctionTool]:
    """Create posting-related FunctionTools for the agent."""
    return [
        FunctionTool(get_posting_manifest),
        FunctionTool(report_posting_result),
    ]
