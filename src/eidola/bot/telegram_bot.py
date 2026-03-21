"""Telegram bot entry point for content intake.

Runs the bot in polling mode. Token loaded from .env via pydantic-settings.

Usage:
    python -m eidola.bot.telegram_bot

Or from project root:
    python scripts/run_telegram_bot.py
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from ..config import settings
from .handlers import router

logger = logging.getLogger("eidola.bot")


def create_bot() -> tuple[Bot, Dispatcher]:
    """Create and configure bot + dispatcher."""
    if not settings.telegram_bot_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN not set in .env file. "
            "Create a bot via @BotFather and add the token to .env"
        )

    bot_kwargs: dict = {
        "token": settings.telegram_bot_token,
        "default": DefaultBotProperties(parse_mode=None),
    }
    if settings.telegram_bot_api_url:
        bot_kwargs["base_url"] = settings.telegram_bot_api_url
        logger.info("Using Local Bot API: %s", settings.telegram_bot_api_url)

    bot = Bot(**bot_kwargs)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    return bot, dp


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    bot, dp = create_bot()

    me = await bot.get_me()
    logger.info("Bot started: @%s (%s)", me.username, me.full_name)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
