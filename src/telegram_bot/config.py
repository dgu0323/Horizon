"""Environment-based configuration for the Telegram bot."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Set


class BotConfigError(ValueError):
    """Raised when the bot configuration is missing or invalid."""


@dataclass
class BotConfig:
    """Runtime configuration sourced from environment variables."""

    token: str
    data_dir: Path = Path("data")
    allowed_chat_ids: Set[int] = field(default_factory=set)

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Build config from TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_CHAT_IDS /
        TELEGRAM_DATA_DIR. Raises BotConfigError when the token is missing."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise BotConfigError(
                "TELEGRAM_BOT_TOKEN is not set. "
                "Create a bot via @BotFather and export the token "
                "(or add it to .env) before starting the bot."
            )

        allowed: Set[int] = set()
        raw_ids = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
        for part in raw_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                allowed.add(int(part))
            except ValueError as e:
                raise BotConfigError(
                    f"Invalid chat id in TELEGRAM_ALLOWED_CHAT_IDS: {part!r}"
                ) from e

        data_dir = Path(os.environ.get("TELEGRAM_DATA_DIR", "data").strip() or "data")
        return cls(token=token, data_dir=data_dir, allowed_chat_ids=allowed)
