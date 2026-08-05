"""Agent-Reach adapter scraper.

Agent-Reach is an access layer for hard-to-reach platforms (Twitter/X,
Xiaohongshu, Bilibili, V2EX, LinkedIn, Reddit, ...). Most of its channels
are just routing/health-check wrappers around external CLIs that need login
cookies, so this adapter only uses the two channels that expose a real
Python data API returning plain dicts:

* ``v2ex``   — public JSON API, no credentials required (hot topics and
  per-node latest topics).
* ``xueqiu`` — public endpoints with an optional login cookie (hot posts).
* ``bilibili`` — search via the external ``bili`` CLI
  (``pipx install bilibili-cli``), no login required. Bilibili's risk
  control 412-blocks yt-dlp, so bili-cli is the only viable backend; it is
  invoked as a subprocess (``bili search <keyword> --type video --json``).

Results are mapped into ContentItem instances so the rest of the Horizon
pipeline (deduplication, AI scoring, enrichment, summarization) treats them
the same way as RSS or Hacker News items.

Design notes:

* The ``agent_reach`` package is an optional dependency. If the import
  fails, ``agent_reach_path`` (a local checkout of the Agent-Reach repo) is
  appended to ``sys.path`` as a fallback. When Agent-Reach is unavailable,
  a platform is misconfigured, or a fetch fails, the scraper logs a warning
  and returns what it has (possibly zero items) instead of raising — the
  pipeline must never go down because of this source.
* Agent-Reach channel methods are synchronous (urllib-based), so they run
  in a worker thread via ``asyncio.to_thread``. The bilibili search runs
  through ``asyncio.create_subprocess_exec`` with a timeout.
* V2EX topics carry a ``created`` unix timestamp, so the ``since`` time
  window is honored. Xueqiu hot posts and Bilibili search results carry no
  timestamp; their ``published_at`` is set to fetch time, which always
  falls inside the window.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, List, Optional

import httpx

from .base import BaseScraper
from ..models import (
    AgentReachConfig,
    AgentReachPlatformConfig,
    ContentItem,
    SourceType,
)

logger = logging.getLogger(__name__)

# (module path, channel class name) inside the agent_reach package,
# keyed by platform name. Only channels with a real Python data API belong
# here; bilibili goes through the external bili CLI instead (see
# _fetch_bilibili), and the remaining CLI/cookie-driven platforms are out
# of scope for this adapter.
_CHANNEL_CLASSES = {
    "v2ex": ("agent_reach.channels.v2ex", "V2EXChannel"),
    "xueqiu": ("agent_reach.channels.xueqiu", "XueqiuChannel"),
}

# External CLI backing the bilibili platform (bilibili-cli, no login needed).
_BILIBILI_COMMAND = "bili"
_BILIBILI_TIMEOUT_SEC = 30


class AgentReachScraper(BaseScraper):
    """Scraper backed by Agent-Reach channels with Python data APIs."""

    SOURCE_TYPE = SourceType.AGENT_REACH

    def __init__(self, config: AgentReachConfig, http_client: httpx.AsyncClient):
        """Initialize the scraper.

        Args:
            config: Agent-Reach source configuration.
            http_client: Shared async HTTP client (unused; Agent-Reach
                channels manage their own HTTP, kept for interface parity).
        """
        super().__init__({"agent_reach": config}, http_client)
        self.ar_config = config

    async def fetch(self, since: datetime) -> List[ContentItem]:
        """Fetch content items from all enabled Agent-Reach platforms.

        Args:
            since: Only fetch items published after this time (applied to
                platforms whose records carry a timestamp).

        Returns:
            List[ContentItem]: Fetched content items (empty when Agent-Reach
                is unavailable).
        """
        if not self.ar_config.enabled:
            return []

        items: List[ContentItem] = []
        for platform in self.ar_config.platforms:
            if not platform.enabled:
                continue
            try:
                if platform.name == "bilibili":
                    raws = await self._fetch_bilibili(platform)
                else:
                    channel = self._load_channel(platform.name)
                    if channel is None:
                        continue
                    raws = await asyncio.to_thread(
                        self._fetch_platform, channel, platform
                    )
            except Exception as exc:
                logger.warning(
                    "Agent-Reach platform %s fetch failed: %s",
                    platform.name,
                    exc,
                )
                continue
            for raw in raws:
                item = self._raw_to_item(platform.name, raw, platform)
                if item is None:
                    continue
                # Only timestamped records are checked against the window.
                if platform.name == "v2ex" and item.published_at < self._ensure_utc(since):
                    continue
                items.append(item)
        return items

    def _load_channel(self, platform_name: str) -> Optional[Any]:
        """Instantiate the Agent-Reach channel for a platform, or None."""
        entry = _CHANNEL_CLASSES.get(platform_name)
        if entry is None:
            logger.warning(
                "Agent-Reach platform %s is not supported by this adapter "
                "(supported: %s, bilibili); skipping",
                platform_name,
                ", ".join(sorted(_CHANNEL_CLASSES)),
            )
            return None

        module_name, class_name = entry
        module = self._import_module(module_name)
        if module is None:
            return None
        try:
            return getattr(module, class_name)()
        except Exception as exc:
            logger.warning(
                "Could not instantiate Agent-Reach channel %s: %s",
                class_name,
                exc,
            )
            return None

    def _import_module(self, module_name: str) -> Optional[Any]:
        """Import an agent_reach module, with a sys.path fallback."""
        try:
            return importlib.import_module(module_name)
        except ImportError:
            pass

        if not self._extend_sys_path():
            logger.warning(
                "agent_reach is not installed and no usable agent_reach_path "
                "is configured; skipping Agent-Reach source"
            )
            return None
        try:
            return importlib.import_module(module_name)
        except ImportError as exc:
            logger.warning("Could not import %s: %s", module_name, exc)
            return None

    def _extend_sys_path(self) -> bool:
        """Append agent_reach_path to sys.path if it points at a directory."""
        path = self.ar_config.agent_reach_path
        if not path:
            return False
        expanded = os.path.expanduser(path)
        if not os.path.isdir(expanded):
            logger.warning("agent_reach_path %s is not a directory", expanded)
            return False
        if expanded not in sys.path:
            sys.path.insert(0, expanded)
        return True

    @staticmethod
    def _fetch_platform(
        channel: Any, platform: AgentReachPlatformConfig
    ) -> List[dict]:
        """Call the platform's data methods and return raw dict records."""
        if platform.name == "v2ex":
            raws = list(channel.get_hot_topics(limit=platform.fetch_limit))
            for node in platform.nodes:
                raws.extend(
                    channel.get_node_topics(node, limit=platform.fetch_limit)
                )
            return raws
        if platform.name == "xueqiu":
            return list(channel.get_hot_posts(limit=platform.fetch_limit))
        return []

    async def _fetch_bilibili(
        self, platform: AgentReachPlatformConfig
    ) -> List[dict]:
        """Search Bilibili via the bili CLI for each configured keyword."""
        if not platform.keywords:
            logger.warning(
                "Agent-Reach bilibili platform has no keywords configured; "
                "skipping"
            )
            return []
        raws: List[dict] = []
        for keyword in platform.keywords:
            results = await self._run_bili_search(keyword, platform.fetch_limit)
            for raw in results:
                raws.append({**raw, "keyword": keyword})
        return raws

    async def _run_bili_search(self, keyword: str, limit: int) -> List[dict]:
        """Run ``bili search <keyword> --type video --json`` and return records.

        Every failure mode (CLI missing, timeout, non-zero exit, non-JSON or
        not-ok payload) degrades to a warning plus an empty list so one bad
        keyword never aborts the run.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                _BILIBILI_COMMAND,
                "search",
                keyword,
                "--type",
                "video",
                "-n",
                str(limit),
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning(
                "bili CLI (%s) is not installed; skipping bilibili search "
                "(install: pipx install bilibili-cli)",
                _BILIBILI_COMMAND,
            )
            return []

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_BILIBILI_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("bili search timed out for keyword %r", keyword)
            return []

        if proc.returncode != 0:
            logger.warning(
                "bili search failed for keyword %r (exit %s)",
                keyword,
                proc.returncode,
            )
            return []

        try:
            payload = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            logger.warning(
                "bili search returned non-JSON output for keyword %r: %s",
                keyword,
                exc,
            )
            return []

        if not isinstance(payload, dict) or not payload.get("ok"):
            logger.warning(
                "bili search returned a not-ok payload for keyword %r", keyword
            )
            return []

        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return [raw for raw in data if isinstance(raw, dict)]

    def _raw_to_item(
        self,
        platform_name: str,
        raw: Any,
        platform: AgentReachPlatformConfig,
    ) -> Optional[ContentItem]:
        """Map one raw Agent-Reach record into a ContentItem (None to skip)."""
        if not isinstance(raw, dict):
            return None
        try:
            if platform_name == "v2ex":
                return self._v2ex_to_item(raw, platform)
            if platform_name == "xueqiu":
                return self._xueqiu_to_item(raw, platform)
            if platform_name == "bilibili":
                return self._bilibili_to_item(raw, platform)
        except Exception as exc:
            logger.warning(
                "Skipping invalid Agent-Reach %s record: %s", platform_name, exc
            )
        return None

    def _v2ex_to_item(
        self, raw: dict, platform: AgentReachPlatformConfig
    ) -> Optional[ContentItem]:
        """Map a V2EX topic dict (id/title/url/replies/node_*/content/created)."""
        title = (raw.get("title") or "").strip()
        url = (raw.get("url") or "").strip()
        topic_id = raw.get("id")
        created = raw.get("created") or 0
        if not title or not url or not topic_id or not created:
            return None

        published = datetime.fromtimestamp(created, tz=timezone.utc)
        meta = {
            "platform": "v2ex",
            "source_name": "agent-reach:v2ex",
            "node_name": raw.get("node_name"),
            "node_title": raw.get("node_title"),
            "replies": raw.get("replies"),
            "category": platform.category,
        }
        return ContentItem(
            id=self._generate_id("agent_reach", "v2ex", str(topic_id)),
            source_type=self.SOURCE_TYPE,
            title=title,
            url=url,
            content=(raw.get("content") or "").strip() or None,
            author=None,  # hot/node topic lists do not include the author
            published_at=published,
            metadata={k: v for k, v in meta.items() if v is not None},
        )

    def _xueqiu_to_item(
        self, raw: dict, platform: AgentReachPlatformConfig
    ) -> Optional[ContentItem]:
        """Map a Xueqiu hot-post dict (id/title/text/author/likes/url)."""
        title = (raw.get("title") or "").strip()
        text = (raw.get("text") or "").strip()
        if not title:
            title = text[:80]
        url = (raw.get("url") or "").strip()
        post_id = raw.get("id")
        if not title or not url or not post_id:
            return None

        # The public timeline endpoint carries no timestamp, so use fetch
        # time; the item is "hot right now" by definition.
        meta = {
            "platform": "xueqiu",
            "source_name": "agent-reach:xueqiu",
            "likes": raw.get("likes"),
            "category": platform.category,
        }
        return ContentItem(
            id=self._generate_id("agent_reach", "xueqiu", str(post_id)),
            source_type=self.SOURCE_TYPE,
            title=title,
            url=url,
            content=text or None,
            author=(raw.get("author") or "").strip() or None,
            published_at=datetime.now(timezone.utc),
            metadata={k: v for k, v in meta.items() if v is not None},
        )

    def _bilibili_to_item(
        self, raw: dict, platform: AgentReachPlatformConfig
    ) -> Optional[ContentItem]:
        """Map a bili-cli search record (bvid/title/author/play/duration)."""
        bvid = (raw.get("bvid") or "").strip()
        title = (raw.get("title") or "").strip()
        if not bvid or not title:
            return None

        # Search results carry no publish timestamp, so use fetch time.
        meta = {
            "platform": "bilibili",
            "source_name": "agent-reach:bilibili",
            "play": raw.get("play"),
            "duration": raw.get("duration"),
            "keyword": raw.get("keyword"),
            "category": platform.category,
        }
        return ContentItem(
            id=self._generate_id("agent_reach", "bilibili", bvid),
            source_type=self.SOURCE_TYPE,
            title=title,
            url=f"https://www.bilibili.com/video/{bvid}",
            content=None,
            author=(raw.get("author") or "").strip() or None,
            published_at=datetime.now(timezone.utc),
            metadata={k: v for k, v in meta.items() if v is not None},
        )

    @staticmethod
    def _ensure_utc(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
