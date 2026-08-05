from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models import (
    SOURCE_REGISTRY,
    AgentReachConfig,
    SourcesConfig,
    SourceType,
)
from src.scrapers.agent_reach import AgentReachScraper


SINCE = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)


def _scraper(config: AgentReachConfig) -> AgentReachScraper:
    return AgentReachScraper(config, AsyncMock())


def _v2ex_config(**overrides) -> AgentReachConfig:
    platform = {"name": "v2ex", "enabled": True, "fetch_limit": 5, "nodes": []}
    platform.update(overrides.pop("platform_overrides", {}))
    return AgentReachConfig(enabled=True, platforms=[platform], **overrides)


def _v2ex_topic(topic_id: int = 123, created: int = 1785808678) -> dict:
    return {
        "id": topic_id,
        "title": "How I built a micro SaaS",
        "url": f"https://www.v2ex.com/t/{topic_id}",
        "replies": 42,
        "node_name": "create",
        "node_title": "分享创造",
        "content": "Some content about the project",
        "created": created,
    }


def _xueqiu_post(post_id: int = 456) -> dict:
    return {
        "id": post_id,
        "title": "",
        "text": "Market is rallying on AI earnings",
        "author": "trader123",
        "likes": 88,
        "url": f"https://xueqiu.com/1/{post_id}",
    }


def _patch_channel(monkeypatch: pytest.MonkeyPatch, channel: MagicMock) -> None:
    """Make _load_channel return the given mock channel."""
    monkeypatch.setattr(
        AgentReachScraper, "_load_channel", lambda self, name: channel
    )


def test_v2ex_topic_mapped_to_content_item(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = MagicMock()
    channel.get_hot_topics.return_value = [_v2ex_topic()]
    _patch_channel(monkeypatch, channel)

    items = asyncio.run(_scraper(_v2ex_config()).fetch(SINCE))

    assert len(items) == 1
    item = items[0]
    assert item.id == "agent_reach:v2ex:123"
    assert item.source_type == SourceType.AGENT_REACH
    assert item.title == "How I built a micro SaaS"
    assert str(item.url) == "https://www.v2ex.com/t/123"
    assert item.content == "Some content about the project"
    assert item.published_at == datetime.fromtimestamp(1785808678, tz=timezone.utc)
    assert item.metadata["platform"] == "v2ex"
    assert item.metadata["node_name"] == "create"
    assert item.metadata["replies"] == 42


def test_v2ex_topics_older_than_since_are_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = MagicMock()
    channel.get_hot_topics.return_value = [
        _v2ex_topic(topic_id=1, created=1_000_000),  # 1970, older than SINCE
        _v2ex_topic(topic_id=2, created=1785808678),  # 2026-08-04, within window
    ]
    _patch_channel(monkeypatch, channel)

    items = asyncio.run(_scraper(_v2ex_config()).fetch(SINCE))

    assert [item.id for item in items] == ["agent_reach:v2ex:2"]


def test_v2ex_nodes_fetched_in_addition_to_hot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = MagicMock()
    channel.get_hot_topics.return_value = []
    channel.get_node_topics.return_value = [_v2ex_topic()]
    _patch_channel(monkeypatch, channel)

    config = _v2ex_config(platform_overrides={"nodes": ["python"]})
    items = asyncio.run(_scraper(config).fetch(SINCE))

    channel.get_node_topics.assert_called_once_with("python", limit=5)
    assert len(items) == 1


def test_xueqiu_post_mapped_to_content_item(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = MagicMock()
    channel.get_hot_posts.return_value = [_xueqiu_post()]
    _patch_channel(monkeypatch, channel)

    config = AgentReachConfig(
        enabled=True,
        platforms=[{"name": "xueqiu", "enabled": True, "fetch_limit": 5}],
    )
    items = asyncio.run(_scraper(config).fetch(SINCE))

    assert len(items) == 1
    item = items[0]
    assert item.id == "agent_reach:xueqiu:456"
    # Empty title falls back to the text body.
    assert item.title == "Market is rallying on AI earnings"
    assert item.author == "trader123"
    assert item.metadata["platform"] == "xueqiu"
    assert item.metadata["likes"] == 88


def test_disabled_source_returns_empty() -> None:
    config = AgentReachConfig(enabled=False, platforms=[{"name": "v2ex"}])
    assert asyncio.run(_scraper(config).fetch(SINCE)) == []


def test_unsupported_platform_is_skipped() -> None:
    config = AgentReachConfig(
        enabled=True, platforms=[{"name": "twitter", "enabled": True}]
    )
    assert asyncio.run(_scraper(config).fetch(SINCE)) == []


def test_missing_agent_reach_package_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No installed package and no path fallback -> warning + zero items."""

    def _raise_import_error(name: str, package: object = None) -> object:
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(importlib, "import_module", _raise_import_error)
    config = _v2ex_config(agent_reach_path=None)
    assert asyncio.run(_scraper(config).fetch(SINCE)) == []


def test_bad_agent_reach_path_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_import_error(name: str, package: object = None) -> object:
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(importlib, "import_module", _raise_import_error)
    config = _v2ex_config(agent_reach_path="/nonexistent/agent-reach")
    assert asyncio.run(_scraper(config).fetch(SINCE)) == []


def test_platform_fetch_exception_skips_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = MagicMock()
    channel.get_hot_topics.side_effect = RuntimeError("network down")
    _patch_channel(monkeypatch, channel)

    assert asyncio.run(_scraper(_v2ex_config()).fetch(SINCE)) == []


def test_malformed_records_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = MagicMock()
    channel.get_hot_topics.return_value = [
        "not-a-dict",
        {"id": 1},  # missing title/url/created
        _v2ex_topic(),
    ]
    _patch_channel(monkeypatch, channel)

    items = asyncio.run(_scraper(_v2ex_config()).fetch(SINCE))
    assert len(items) == 1


def test_config_parsing_and_registry() -> None:
    sources = SourcesConfig(
        **{
            "agent_reach": {
                "enabled": True,
                "agent_reach_path": "../Agent-Reach",
                "platforms": [
                    {"name": "v2ex", "fetch_limit": 10, "nodes": ["create"]},
                    {"name": "xueqiu", "enabled": False},
                ],
            }
        }
    )
    config = sources.agent_reach
    assert config is not None
    assert config.enabled is True
    assert config.agent_reach_path == "../Agent-Reach"
    assert [p.name for p in config.platforms] == ["v2ex", "xueqiu"]
    assert config.platforms[0].fetch_limit == 10
    assert config.platforms[0].nodes == ["create"]
    assert config.platforms[1].enabled is False

    assert SourceType.AGENT_REACH.value == "agent_reach"
    assert "agent_reach" in SOURCE_REGISTRY


def test_config_absent_by_default() -> None:
    assert SourcesConfig().agent_reach is None
