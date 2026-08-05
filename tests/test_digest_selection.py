"""Tests for digest selection lanes: opportunity fast-track and per-source floor."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from src.models import AIConfig, Config, ContentItem, FilteringConfig, SourceType, SourcesConfig
from src.orchestrator import HorizonOrchestrator


def _make_item(item_id: str, source: SourceType, score: float) -> ContentItem:
    item = ContentItem(
        id=item_id,
        source_type=source,
        title=f"Item {item_id}",
        url="https://example.com/item",
        published_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )
    item.ai_score = score
    return item


def _make_orchestrator(**filtering_kwargs) -> HorizonOrchestrator:
    config = Config(
        ai=AIConfig(provider="openai", model="test", api_key_env="TEST_API_KEY", languages=[]),
        sources=SourcesConfig(),
        filtering=FilteringConfig(ai_score_threshold=6.0, **filtering_kwargs),
    )
    return HorizonOrchestrator(config, SimpleNamespace())


async def _identity_dedup(items, *, log=True):
    return items


def _run_filter(orchestrator, items, monkeypatch):
    monkeypatch.setattr(orchestrator, "merge_topic_duplicates", _identity_dedup)
    result = asyncio.run(orchestrator.filter_items(items, apply_balance=False, log=False))
    return result.items


def test_opportunity_fast_track_includes_low_news_score_item(monkeypatch):
    orchestrator = _make_orchestrator(opportunity_digest_min_score=6.0)
    low_news_high_opp = _make_item("reddit:a", SourceType.REDDIT, 3.0)
    low_news_high_opp.metadata["opportunity"] = {"score": 7.0, "reason": "real pain"}
    plain_low = _make_item("reddit:b", SourceType.REDDIT, 5.5)
    high_news = _make_item("hackernews:c", SourceType.HACKERNEWS, 8.0)

    result = _run_filter(orchestrator, [low_news_high_opp, plain_low, high_news], monkeypatch)

    ids = [item.id for item in result]
    assert "reddit:a" in ids
    assert "hackernews:c" in ids
    assert "reddit:b" not in ids


def test_opportunity_fast_track_disabled_when_unconfigured(monkeypatch):
    orchestrator = _make_orchestrator()  # opportunity_digest_min_score defaults to None
    item = _make_item("reddit:a", SourceType.REDDIT, 3.0)
    item.metadata["opportunity"] = {"score": 9.0, "reason": "great"}

    result = _run_filter(orchestrator, [item], monkeypatch)

    assert result == []


def test_per_source_floor_guarantees_top_n_below_threshold(monkeypatch):
    orchestrator = _make_orchestrator(per_source_min_items=2)
    v2ex_items = [
        _make_item("agent_reach:v2ex:1", SourceType.AGENT_REACH, 2.0),
        _make_item("agent_reach:v2ex:2", SourceType.AGENT_REACH, 1.5),
        _make_item("agent_reach:v2ex:3", SourceType.AGENT_REACH, 1.0),
    ]
    hn_items = [
        _make_item("hackernews:a", SourceType.HACKERNEWS, 9.0),
        _make_item("hackernews:b", SourceType.HACKERNEWS, 8.0),
        _make_item("hackernews:c", SourceType.HACKERNEWS, 7.0),
    ]

    result = _run_filter(orchestrator, v2ex_items + hn_items, monkeypatch)

    ids = [item.id for item in result]
    # top-2 v2ex floored in despite low scores; third one stays out
    assert "agent_reach:v2ex:1" in ids
    assert "agent_reach:v2ex:2" in ids
    assert "agent_reach:v2ex:3" not in ids
    # all three HN items clear the threshold on their own
    assert all(f"hackernews:{c}" in ids for c in "abc")


def test_per_source_floor_does_not_duplicate_threshold_items(monkeypatch):
    orchestrator = _make_orchestrator(per_source_min_items=3)
    items = [
        _make_item("hackernews:a", SourceType.HACKERNEWS, 9.0),
        _make_item("hackernews:b", SourceType.HACKERNEWS, 8.0),
    ]

    result = _run_filter(orchestrator, items, monkeypatch)

    assert [item.id for item in result] == ["hackernews:a", "hackernews:b"]


def test_floor_and_fast_track_combine(monkeypatch):
    orchestrator = _make_orchestrator(
        opportunity_digest_min_score=6.0, per_source_min_items=1
    )
    opp_item = _make_item("rss:a", SourceType.RSS, 2.0)
    opp_item.metadata["opportunity"] = {"score": 8.0, "reason": "pain"}
    floor_item = _make_item("agent_reach:v2ex:1", SourceType.AGENT_REACH, 1.0)

    result = _run_filter(orchestrator, [opp_item, floor_item], monkeypatch)

    ids = {item.id for item in result}
    assert ids == {"rss:a", "agent_reach:v2ex:1"}
