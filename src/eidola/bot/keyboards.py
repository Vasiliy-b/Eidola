"""Inline keyboards for Telegram bot."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def content_type_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for selecting content type when video is uploaded."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Пост", callback_data="type:feed_video"),
            InlineKeyboardButton(text="Reel", callback_data="type:reel"),
        ],
        [
            InlineKeyboardButton(text="Story", callback_data="type:story_video"),
        ],
    ])


def confirm_keyboard() -> InlineKeyboardMarkup:
    """Confirmation keyboard before starting uniqualization."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Подтвердить", callback_data="confirm:yes"),
            InlineKeyboardButton(text="Отменить", callback_data="confirm:cancel"),
        ],
        [
            InlineKeyboardButton(text="Изменить текст", callback_data="confirm:edit_caption"),
        ],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отменить", callback_data="confirm:cancel")],
    ])
