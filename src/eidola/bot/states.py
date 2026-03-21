"""FSM states for Telegram bot content intake flow."""

from aiogram.fsm.state import State, StatesGroup


class ContentUpload(StatesGroup):
    """States for content upload conversation."""
    awaiting_caption = State()
    awaiting_type = State()
    confirming = State()


class ContentEdit(StatesGroup):
    """States for /edit command — editing existing content caption."""
    awaiting_new_caption = State()
