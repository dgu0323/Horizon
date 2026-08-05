import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.ai.opportunity_analyzer import OpportunityAnalyzer
from src.models import ContentItem, SourceType


def _make_item(item_id: str) -> ContentItem:
    return ContentItem(
        id=item_id,
        source_type=SourceType.REDDIT,
        title=f"Item {item_id}",
        url="https://example.com/item",
        published_at=datetime(2026, 4, 26, tzinfo=timezone.utc),
    )


def _valid_result() -> dict:
    return {
        "score": 8.0,
        "reason": "Repeated complaints about existing tools",
        "problem": "Founders waste hours on manual invoicing",
        "target_user": "Solo SaaS founders",
        "mvp": "A single-page invoice generator",
        "distribution": "r/SaaS, HN Show",
        "monetization": "$9/month subscription",
        "counter_view": "Market is crowded",
        "validation_step": "Post a landing page and DM 20 users",
        "tags": ["micro-saas", "devtools"],
    }


def test_analyze_item_writes_opportunity_metadata():
    async def complete(**kwargs):
        return json.dumps(_valid_result())

    item = _make_item("reddit:test:1")
    item.ai_score = 7.5
    item.ai_summary = "news summary"

    asyncio.run(OpportunityAnalyzer(SimpleNamespace(complete=complete))._analyze_item(item))

    opportunity = item.metadata["opportunity"]
    assert opportunity["score"] == 8.0
    assert opportunity["problem"] == "Founders waste hours on manual invoicing"
    assert opportunity["validation_step"] == "Post a landing page and DM 20 users"
    assert opportunity["tags"] == ["micro-saas", "devtools"]
    # News-analysis fields must not be overwritten
    assert item.ai_score == 7.5
    assert item.ai_summary == "news summary"


@pytest.mark.parametrize(
    "result",
    [
        {**_valid_result(), "score": 11},
        {**_valid_result(), "score": float("nan")},
        {k: v for k, v in _valid_result().items() if k != "mvp"},
    ],
)
def test_analyze_item_malformed_result_leaves_metadata_unset(result):
    async def complete(**kwargs):
        return json.dumps(result)

    item = _make_item("reddit:test:invalid")

    asyncio.run(OpportunityAnalyzer(SimpleNamespace(complete=complete))._analyze_item(item))

    assert "opportunity" not in item.metadata


def test_analyze_batch_processes_all_items(monkeypatch):
    analyzer = OpportunityAnalyzer(SimpleNamespace())
    items = [_make_item(f"reddit:test:{i}") for i in range(3)]

    async def fake_analyze_item(item):
        item.metadata["opportunity"] = _valid_result()

    monkeypatch.setattr(analyzer, "_analyze_item", fake_analyze_item)

    result = asyncio.run(analyzer.analyze_batch(items))

    assert [item.id for item in result] == [item.id for item in items]
    assert all("opportunity" in item.metadata for item in result)


def test_language_instruction_defaults_to_english():
    analyzer = OpportunityAnalyzer(SimpleNamespace())
    assert "English" in analyzer._get_language_instruction()


def test_language_instruction_uses_configured_languages():
    client = SimpleNamespace(config=SimpleNamespace(languages=["zh"]))
    analyzer = OpportunityAnalyzer(client)
    assert "Simplified Chinese" in analyzer._get_language_instruction()
