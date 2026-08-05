"""Entry point: ``python -m src.telegram_bot``."""

import asyncio
import logging
import sys

from dotenv import load_dotenv

from .bot import TelegramBot
from .commands import CommandRouter
from .config import BotConfig, BotConfigError
from .store import DailyItemsStore, SaveStore


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    load_dotenv()

    try:
        config = BotConfig.from_env()
    except BotConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    router = CommandRouter(
        items_store=DailyItemsStore(config.data_dir),
        save_store=SaveStore(config.data_dir),
    )
    bot = TelegramBot(
        token=config.token,
        router=router,
        allowed_chat_ids=config.allowed_chat_ids,
    )

    logging.getLogger(__name__).info(
        "Starting Telegram bot (data_dir=%s, whitelist=%s)",
        config.data_dir,
        sorted(config.allowed_chat_ids) if config.allowed_chat_ids else "off",
    )
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
