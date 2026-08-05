from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx

from src.scrapers.huggingface import HuggingFaceScraper, SOURCE_NAME
from src.models import SourceType

SINCE = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)


def _config(**overrides) -> SimpleNamespace:
    defaults = dict(
        enabled=True,
        fetch_models=True,
        fetch_datasets=True,
        limit=10,
        min_likes=0,
        category="ai",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _response(payload) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _client(models_payload=None, datasets_payload=None) -> AsyncMock:
    """Mock client routing by URL; payload None means the endpoint raises."""
    responses = {
        HuggingFaceScraper.MODELS_URL: models_payload,
        HuggingFaceScraper.DATASETS_URL: datasets_payload,
    }

    async def _get(url, **kwargs):
        payload = responses[url]
        if payload is None:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))
        return _response(payload)

    client = AsyncMock()
    client.get.side_effect = _get
    return client


def _model_entry(**overrides) -> dict:
    entry = {
        "id": "MiniMaxAI/MiniMax-H3",
        "likes": 2249,
        "trendingScore": 2166,
        "downloads": 0,
        "tags": ["diffusers", "safetensors", "text-to-video"],
        "lastModified": "2026-08-01T10:45:18.000Z",
    }
    entry.update(overrides)
    return entry


def _dataset_entry(**overrides) -> dict:
    entry = {
        "id": "XYZAILab/XYZ-Aquila-SFT",
        "author": "XYZAILab",
        "likes": 351,
        "trendingScore": 149,
        "downloads": 931,
        "tags": ["task_categories:text-generation", "license:apache-2.0"],
        "lastModified": "2026-07-29T08:28:13.000Z",
    }
    entry.update(overrides)
    return entry


def test_models_conversion() -> None:
    client = _client(models_payload=[_model_entry()], datasets_payload=[])
    scraper = HuggingFaceScraper(_config(), client)

    items = asyncio.run(scraper.fetch(SINCE))

    assert len(items) == 1
    item = items[0]
    assert item.id == f"{SOURCE_NAME}:model:MiniMaxAI/MiniMax-H3"
    assert item.source_type == SourceType.HUGGINGFACE
    assert item.title == "MiniMaxAI/MiniMax-H3 (🔥2166 trending)"
    assert str(item.url) == "https://huggingface.co/MiniMaxAI/MiniMax-H3"
    assert item.author == "MiniMaxAI"
    assert "Likes: 2249" in item.content
    assert "Downloads: 0" in item.content
    assert "text-to-video" in item.content
    assert "2026-08-01T10:45:18.000Z" in item.content
    assert item.published_at == datetime(2026, 8, 1, 10, 45, 18, tzinfo=timezone.utc)
    assert item.metadata["platform"] == "huggingface"
    assert item.metadata["kind"] == "model"
    assert item.metadata["likes"] == 2249
    assert item.metadata["trending_score"] == 2166
    assert item.metadata["tags"] == ["diffusers", "safetensors", "text-to-video"]
    assert item.metadata["category"] == "ai"


def test_datasets_conversion() -> None:
    client = _client(models_payload=[], datasets_payload=[_dataset_entry()])
    scraper = HuggingFaceScraper(_config(), client)

    items = asyncio.run(scraper.fetch(SINCE))

    assert len(items) == 1
    item = items[0]
    assert item.id == f"{SOURCE_NAME}:dataset:XYZAILab/XYZ-Aquila-SFT"
    assert str(item.url) == "https://huggingface.co/datasets/XYZAILab/XYZ-Aquila-SFT"
    assert item.author == "XYZAILab"
    assert item.metadata["kind"] == "dataset"
    assert item.metadata["downloads"] == 931


def test_min_likes_filter() -> None:
    payload = [
        _model_entry(id="a/popular", likes=500),
        _model_entry(id="a/niche", likes=5),
    ]
    client = _client(models_payload=payload, datasets_payload=[])
    scraper = HuggingFaceScraper(_config(min_likes=100), client)

    items = asyncio.run(scraper.fetch(SINCE))

    assert [i.metadata["likes"] for i in items] == [500]


def test_since_window_filters_by_last_modified() -> None:
    payload = [
        _model_entry(id="a/fresh", lastModified="2026-07-20T00:00:00.000Z"),
        _model_entry(id="a/stale", lastModified="2026-06-01T00:00:00.000Z"),
        _model_entry(id="a/unknown", lastModified=None),
    ]
    client = _client(models_payload=payload, datasets_payload=[])
    scraper = HuggingFaceScraper(_config(), client)

    items = asyncio.run(scraper.fetch(SINCE))

    ids = {str(i.url) for i in items}
    assert any("a/fresh" in str(i.url) for i in items)
    assert any("a/unknown" in str(i.url) for i in items)  # missing lastModified passes
    assert not any("a/stale" in str(i.url) for i in items)
    assert len(ids) == 2


def test_single_endpoint_failure_degrades() -> None:
    client = _client(models_payload=None, datasets_payload=[_dataset_entry()])
    scraper = HuggingFaceScraper(_config(), client)

    items = asyncio.run(scraper.fetch(SINCE))

    assert len(items) == 1
    assert items[0].metadata["kind"] == "dataset"


def test_empty_results() -> None:
    client = _client(models_payload=[], datasets_payload=[])
    scraper = HuggingFaceScraper(_config(), client)

    assert asyncio.run(scraper.fetch(SINCE)) == []


def test_disabled_returns_empty_without_requests() -> None:
    client = _client(models_payload=[_model_entry()], datasets_payload=[])
    scraper = HuggingFaceScraper(_config(enabled=False), client)

    assert asyncio.run(scraper.fetch(SINCE)) == []
    client.get.assert_not_called()


def test_fetch_flags_skip_endpoints() -> None:
    client = _client(models_payload=[_model_entry()], datasets_payload=[_dataset_entry()])
    scraper = HuggingFaceScraper(_config(fetch_datasets=False), client)

    items = asyncio.run(scraper.fetch(SINCE))

    assert len(items) == 1
    assert items[0].metadata["kind"] == "model"


def test_request_params_use_trending_sort() -> None:
    client = _client(models_payload=[], datasets_payload=[])
    scraper = HuggingFaceScraper(_config(limit=42), client)

    asyncio.run(scraper.fetch(SINCE))

    params = client.get.call_args_list[0].kwargs["params"]
    assert params == {"sort": "likes7d", "direction": "-1", "limit": "42"}
