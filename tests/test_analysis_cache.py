"""Tests for the cross-run AI analysis cache."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import src.orchestrator as orchestrator_module
from src.ai.cache import (
    apply_cached_analysis,
    apply_cached_opportunity,
    empty_cache,
    prune,
    store_analysis,
    store_opportunity,
)
from src.models import AIConfig, Config, ContentItem, FilteringConfig, SourceType, SourcesConfig
from src.orchestrator import HorizonOrchestrator
from src.storage.manager import StorageManager


def _make_item(item_id: str = "hackernews:story:1") -> ContentItem:
    return ContentItem(
        id=item_id,
        source_type=SourceType.HACKERNEWS,
        title="Some story",
        url="https://example.com/story",
        published_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )


def _make_config(opportunity_enabled: bool = False) -> Config:
    return Config(
        ai=AIConfig(provider="openai", model="test", api_key_env="TEST_API_KEY", languages=[]),
        sources=SourcesConfig(),
        filtering=FilteringConfig(opportunity_enabled=opportunity_enabled),
    )


def test_analysis_roundtrip():
    item = _make_item()
    item.ai_score = 8.0
    item.ai_reason = "Important"
    item.ai_summary = "Summary"
    item.ai_tags = ["ai"]

    cache = empty_cache()
    store_analysis(item, cache)

    restored = _make_item()
    assert apply_cached_analysis(restored, cache) is True
    assert restored.ai_score == 8.0
    assert restored.ai_reason == "Important"
    assert restored.ai_summary == "Summary"
    assert restored.ai_tags == ["ai"]


def test_apply_returns_false_on_miss():
    assert apply_cached_analysis(_make_item(), empty_cache()) is False


@pytest.mark.parametrize("reason", ["Analysis failed", "Analysis response parse failed"])
def test_store_skips_failed_analysis(reason):
    item = _make_item()
    item.ai_score = 0.0
    item.ai_reason = reason

    cache = empty_cache()
    store_analysis(item, cache)

    assert cache["items"] == {}


def test_store_skips_unanalyzed_item():
    cache = empty_cache()
    store_analysis(_make_item(), cache)
    assert cache["items"] == {}


def test_prune_drops_entries_older_than_ttl():
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    cache = empty_cache()
    cache["items"] = {
        "fresh": {"analyzed_at": (now - timedelta(hours=10)).isoformat()},
        "stale": {"analyzed_at": (now - timedelta(hours=100)).isoformat()},
        "broken": {"analyzed_at": "not-a-date"},
    }

    prune(cache, ttl_hours=72, now=now)

    assert list(cache["items"]) == ["fresh"]


def test_opportunity_roundtrip_and_miss():
    item = _make_item()
    opportunity = {"score": 7.0, "reason": "Real pain"}
    cache = empty_cache()

    assert apply_cached_opportunity(item, cache) is False

    item.metadata["opportunity"] = opportunity
    store_opportunity(item, cache)

    restored = _make_item()
    assert apply_cached_opportunity(restored, cache) is True
    assert restored.metadata["opportunity"] == opportunity


def test_store_opportunity_ignores_items_without_result():
    cache = empty_cache()
    store_opportunity(_make_item(), cache)
    assert cache["items"] == {}


def test_analyze_content_uses_cache_and_skips_ai(tmp_path, monkeypatch):
    storage = StorageManager(data_dir=str(tmp_path))
    orchestrator = HorizonOrchestrator(_make_config(), storage)

    cached_item = _make_item("hackernews:story:cached")
    cached_item.ai_score = 9.0
    cached_item.ai_reason = "Cached"
    cached_item.ai_summary = "Cached summary"
    cached_item.ai_tags = ["cached"]
    cache = empty_cache()
    store_analysis(cached_item, cache)
    storage.save_analysis_cache(cache)

    new_item = _make_item("hackernews:story:new")
    analyzed_ids = []

    class FakeAnalyzer:
        def __init__(self, client):
            pass

        async def analyze_batch(self, items):
            for item in items:
                analyzed_ids.append(item.id)
                item.ai_score = 7.0
                item.ai_reason = "Fresh"
                item.ai_summary = "Fresh summary"
                item.ai_tags = ["fresh"]
            return items

    monkeypatch.setattr(orchestrator_module, "create_ai_client", lambda config: object())
    monkeypatch.setattr(orchestrator_module, "ContentAnalyzer", FakeAnalyzer)

    result = asyncio.run(orchestrator._analyze_content([cached_item, new_item]))

    assert analyzed_ids == ["hackernews:story:new"]
    assert result[0].ai_reason == "Cached"
    assert result[1].ai_reason == "Fresh"

    saved = storage.load_analysis_cache()
    assert saved["items"]["hackernews:story:new"]["ai_score"] == 7.0


def test_analyze_content_fully_cached_creates_no_ai_client(tmp_path, monkeypatch):
    storage = StorageManager(data_dir=str(tmp_path))
    orchestrator = HorizonOrchestrator(_make_config(), storage)

    item = _make_item()
    item.ai_score = 8.0
    item.ai_reason = "Cached"
    cache = empty_cache()
    store_analysis(item, cache)
    storage.save_analysis_cache(cache)

    def fail_create_client(config):
        raise AssertionError("AI client must not be created on full cache hit")

    monkeypatch.setattr(orchestrator_module, "create_ai_client", fail_create_client)

    fresh_item = _make_item()
    asyncio.run(orchestrator._analyze_content([fresh_item]))

    assert fresh_item.ai_score == 8.0
    assert fresh_item.ai_reason == "Cached"


def test_analyze_opportunities_reuses_cache(tmp_path, monkeypatch):
    storage = StorageManager(data_dir=str(tmp_path))
    orchestrator = HorizonOrchestrator(_make_config(opportunity_enabled=True), storage)

    cached_item = _make_item("hackernews:story:cached")
    cached_item.ai_score = 9.0
    new_item = _make_item("hackernews:story:new")
    new_item.ai_score = 8.0

    cache = empty_cache()
    cache["items"]["hackernews:story:cached"] = {
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "ai_score": 9.0,
        "opportunity": {"score": 7.0, "reason": "Cached opportunity"},
    }
    storage.save_analysis_cache(cache)

    scored_ids = []

    class FakeOpportunityAnalyzer:
        def __init__(self, client):
            pass

        async def analyze_batch(self, items):
            for item in items:
                scored_ids.append(item.id)
                item.metadata["opportunity"] = {"score": 6.0, "reason": "Fresh"}
            return items

    monkeypatch.setattr(orchestrator_module, "create_ai_client", lambda config: object())
    monkeypatch.setattr(orchestrator_module, "OpportunityAnalyzer", FakeOpportunityAnalyzer)

    asyncio.run(orchestrator._analyze_opportunities([cached_item, new_item]))

    assert scored_ids == ["hackernews:story:new"]
    assert cached_item.metadata["opportunity"]["reason"] == "Cached opportunity"
    assert new_item.metadata["opportunity"]["reason"] == "Fresh"

    saved = storage.load_analysis_cache()
    assert saved["items"]["hackernews:story:new"]["opportunity"]["score"] == 6.0


def test_storage_cache_io_roundtrip_and_invalid(tmp_path):
    storage = StorageManager(data_dir=str(tmp_path))
    assert storage.load_analysis_cache() == empty_cache()

    cache = empty_cache()
    cache["items"]["x"] = {"analyzed_at": "2026-08-04T00:00:00+00:00"}
    storage.save_analysis_cache(cache)
    assert storage.load_analysis_cache()["items"]["x"]

    (tmp_path / "analysis_cache.json").write_text("not json", encoding="utf-8")
    assert storage.load_analysis_cache() == empty_cache()
