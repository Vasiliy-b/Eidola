#!/usr/bin/env python3
"""Run the Telegram bot for content intake."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from eidola.bot.telegram_bot import main
import asyncio

if __name__ == "__main__":
    asyncio.run(main())
