"""Telegram alert utilities for admin notifications.

Sends error alerts, posting confirmations, and system status updates
to the configured chat (same chat where SMM operates).

Thread-safe: uses httpx for sync sending from any context (worker, scheduler, etc.)
without requiring a running aiogram Bot instance.
"""

import logging
from datetime import datetime

import httpx

from ..config import settings

logger = logging.getLogger("eidola.bot.alerts")

_BOT_API = "https://api.telegram.org"


def _get_api_base() -> str:
    if settings.telegram_bot_api_url:
        return settings.telegram_bot_api_url.rstrip("/")
    return _BOT_API


def _send_message(chat_id: int, text: str, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message via HTTP API (no bot instance needed)."""
    if not settings.telegram_bot_token or not chat_id:
        return False

    url = f"{_get_api_base()}/bot{settings.telegram_bot_token}/sendMessage"
    try:
        resp = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Telegram API error %d: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.error("Failed to send alert: %s", e)
        return False


def _get_chat_id() -> int:
    return settings.telegram_alert_chat_id


# --- Public API ---

def alert_error(component: str, error: str, context: str = "") -> bool:
    """Send error alert to admin chat."""
    chat_id = _get_chat_id()
    if not chat_id:
        return False

    text = (
        f"<b>ERROR</b> [{component}]\n"
        f"{_escape(error)}"
    )
    if context:
        text += f"\n\n<i>{_escape(context)}</i>"
    return _send_message(chat_id, text)


def alert_posting_success(account_id: str, content_id: str) -> bool:
    """Notify admin that a post was published successfully."""
    chat_id = _get_chat_id()
    if not chat_id:
        return False

    text = (
        f"<b>POST PUBLISHED</b>\n"
        f"Account: <code>{_escape(account_id)}</code>\n"
        f"Content: <code>{_escape(content_id)}</code>\n"
        f"Time: {datetime.utcnow().strftime('%H:%M UTC')}"
    )
    return _send_message(chat_id, text)


def alert_posting_failed(account_id: str, content_id: str, error: str) -> bool:
    """Notify admin that posting failed."""
    chat_id = _get_chat_id()
    if not chat_id:
        return False

    text = (
        f"<b>POST FAILED</b>\n"
        f"Account: <code>{_escape(account_id)}</code>\n"
        f"Content: <code>{_escape(content_id)}</code>\n"
        f"Error: {_escape(error)}"
    )
    return _send_message(chat_id, text)


def alert_uniqualization_done(content_id: str, variants_count: int) -> bool:
    """Notify that uniqualization completed and content was auto-distributed."""
    chat_id = _get_chat_id()
    if not chat_id:
        return False

    text = (
        f"<b>CONTENT READY</b>\n"
        f"ID: <code>{_escape(content_id)}</code>\n"
        f"Variants: {variants_count}\n"
        f"Status: uniqualized + distributed"
    )
    return _send_message(chat_id, text)


def alert_uniqualization_failed(content_id: str, error: str) -> bool:
    """Notify that uniqualization failed."""
    chat_id = _get_chat_id()
    if not chat_id:
        return False

    text = (
        f"<b>UNIQUALIZATION FAILED</b>\n"
        f"Content: <code>{_escape(content_id)}</code>\n"
        f"Error: {_escape(error)}"
    )
    return _send_message(chat_id, text)


def alert_disk_space(free_gb: float, threshold_gb: float, path: str = "/") -> bool:
    """Alert when disk space is critically low."""
    chat_id = _get_chat_id()
    if not chat_id:
        return False

    text = (
        f"<b>DISK SPACE LOW</b>\n"
        f"Path: <code>{_escape(path)}</code>\n"
        f"Free: {free_gb:.1f} GB (threshold: {threshold_gb:.1f} GB)"
    )
    return _send_message(chat_id, text)


def alert_cleanup_report(
    deleted_variant_files: int,
    deleted_device_files: int,
    expired_items: int,
    freed_mb: float,
) -> bool:
    """Send cleanup summary to admin."""
    chat_id = _get_chat_id()
    if not chat_id:
        return False

    text = (
        f"<b>CLEANUP DONE</b>\n"
        f"Variant files: {deleted_variant_files}\n"
        f"Device files: {deleted_device_files}\n"
        f"Expired items: {expired_items}\n"
        f"Freed: {freed_mb:.1f} MB"
    )
    return _send_message(chat_id, text)


def _escape(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
