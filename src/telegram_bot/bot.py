"""Telegram Bot API client and long-polling loop (httpx only, no framework)."""

import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional

import httpx

from .commands import CommandRouter

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# Telegram rejects messages longer than 4096 characters.
MAX_MESSAGE_LENGTH = 4096

# Long-polling timeout (seconds) for getUpdates.
POLL_TIMEOUT = 30

# Backoff after a network / API error before polling again.
ERROR_BACKOFF_SEC = 5.0


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Split text into chunks of at most `limit` chars, preferring newlines."""
    if not text:
        return [""]
    chunks = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    chunks.append(remaining)
    return chunks


class TelegramBot:
    """Long-polling bot that routes messages through a CommandRouter."""

    def __init__(
        self,
        token: str,
        router: CommandRouter,
        allowed_chat_ids: Optional[Iterable[int]] = None,
    ):
        self.token = token
        self.router = router
        # None = open to everyone; an empty whitelist would lock everyone out,
        # so only a non-empty configured whitelist is enforced.
        allowed = set(allowed_chat_ids or [])
        self.allowed_chat_ids: Optional[set] = allowed or None

    async def run(self, client: Optional[httpx.AsyncClient] = None) -> None:
        """Poll getUpdates forever; pass `client` to inject a mocked transport."""
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                base_url=f"{API_BASE}/bot{self.token}",
                timeout=httpx.Timeout(POLL_TIMEOUT + 10.0),
            )
        try:
            offset: Optional[int] = None
            while True:
                try:
                    updates = await self._get_updates(client, offset)
                except httpx.HTTPError as e:
                    logger.warning("getUpdates failed: %s; retrying", e)
                    await asyncio.sleep(ERROR_BACKOFF_SEC)
                    continue
                for update in updates:
                    offset = update.get("update_id", 0) + 1
                    await self.handle_update(client, update)
        finally:
            if owns_client:
                await client.aclose()

    async def handle_update(self, client: httpx.AsyncClient, update: Dict[str, Any]) -> None:
        """Route one update; non-text messages and foreign chats are ignored."""
        message = update.get("message") or {}
        text = message.get("text")
        chat_id = (message.get("chat") or {}).get("id")
        if text is None or chat_id is None:
            return
        if self.allowed_chat_ids is not None and chat_id not in self.allowed_chat_ids:
            logger.info("Ignoring message from non-whitelisted chat %s", chat_id)
            return
        for reply in self.router.handle(chat_id, text):
            await self.send_message(client, chat_id, reply)

    async def send_message(self, client: httpx.AsyncClient, chat_id: int, text: str) -> None:
        """Send text, splitting into multiple messages when over 4096 chars."""
        for chunk in split_message(text):
            response = await client.post(
                "/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
            )
            response.raise_for_status()

    async def _get_updates(
        self, client: httpx.AsyncClient, offset: Optional[int]
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"timeout": POLL_TIMEOUT}
        if offset is not None:
            payload["offset"] = offset
        response = await client.post("/getUpdates", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise httpx.HTTPError(f"getUpdates returned not-ok: {data}")
        return data.get("result") or []
