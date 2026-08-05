"""HuggingFace trending models/datasets scraper.

Fetches trending rankings from the public HuggingFace Hub API (no key
required) and emits them as ContentItems. Models and datasets are fetched
from independent endpoints; a failure of one does not affect the other.
Items can be filtered by a minimum like count and by the `since` window
(using each entry's lastModified timestamp when present).
"""

from datetime import datetime, timezone
from typing import Any, List, Optional

import httpx

from ..models import ContentItem, SourceType
from .base import BaseScraper

SOURCE_TYPE = SourceType.HUGGINGFACE

# Native source label used for item IDs and metadata, independent of the
# placeholder SourceType above, so IDs stay stable when the enum is updated.
SOURCE_NAME = "huggingface"


class HuggingFaceScraper(BaseScraper):
    """Scraper for HuggingFace trending models and datasets endpoints."""

    MODELS_URL = "https://huggingface.co/api/models"
    DATASETS_URL = "https://huggingface.co/api/datasets"

    def __init__(self, config: Any, http_client: httpx.AsyncClient):
        """Initialize scraper.

        Args:
            config: HuggingFace source configuration (SimpleNamespace-style
                object with enabled/fetch_models/fetch_datasets/limit/
                min_likes/category attributes)
            http_client: Shared async HTTP client
        """
        super().__init__(config, http_client)
        self.cfg = config

    async def fetch(self, since: datetime) -> List[ContentItem]:
        """Fetch trending models and datasets and apply filters."""
        if not getattr(self.cfg, "enabled", False):
            return []

        since_utc = self._as_utc(since)
        items: List[ContentItem] = []
        seen_ids: set[str] = set()

        endpoints = []
        if getattr(self.cfg, "fetch_models", True):
            endpoints.append((self.MODELS_URL, "model"))
        if getattr(self.cfg, "fetch_datasets", True):
            endpoints.append((self.DATASETS_URL, "dataset"))

        for url, kind in endpoints:
            entries = await self._fetch_endpoint(url)
            for entry in entries:
                item = self._entry_to_item(entry, kind)
                if item is None:
                    continue
                if item.id in seen_ids:
                    continue
                if self._likes_int(entry) < getattr(self.cfg, "min_likes", 0):
                    continue
                if not self._within_window(entry, since_utc):
                    continue
                seen_ids.add(item.id)
                items.append(item)

        items.sort(key=lambda x: x.metadata.get("trending_score", 0), reverse=True)
        return items

    async def _fetch_endpoint(self, url: str) -> List[dict]:
        """Call one HuggingFace API endpoint, degrading to [] on failure."""
        params = {
            "sort": "likes7d",
            "direction": "-1",
            "limit": str(getattr(self.cfg, "limit", 20)),
        }
        try:
            response = await self.client.get(
                url,
                params=params,
                headers={"Accept": "application/json", "User-Agent": "Horizon/1.0"},
                timeout=20.0,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return []

        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [entry for entry in payload if isinstance(entry, dict)]

    def _entry_to_item(self, entry: dict, kind: str) -> Optional[ContentItem]:
        """Convert a raw HuggingFace API entry into a ContentItem."""
        entry_id = entry.get("id")
        if not entry_id:
            return None

        likes = self._likes_int(entry)
        downloads = self._int(entry.get("downloads"))
        trending_score = self._int(entry.get("trendingScore"))
        tags = [t for t in entry.get("tags") or [] if isinstance(t, str)]
        last_modified = self._parse_datetime(entry.get("lastModified"))

        title = f"{entry_id} (🔥{trending_score} trending)"
        if kind == "dataset":
            url = f"https://huggingface.co/datasets/{entry_id}"
        else:
            url = f"https://huggingface.co/{entry_id}"

        content_lines = [
            f"Trending HuggingFace {kind}: {entry_id}",
            f"Likes: {likes}",
            f"Downloads: {downloads}",
            f"Trending score: {trending_score}",
        ]
        if last_modified:
            content_lines.append(f"Last modified: {entry.get('lastModified')}")
        if tags:
            content_lines.append("")
            content_lines.append(f"Tags: {', '.join(tags)}")

        return ContentItem(
            id=self._generate_id(SOURCE_NAME, kind, entry_id),
            source_type=SOURCE_TYPE,
            title=title,
            url=url,
            content="\n".join(content_lines),
            author=entry.get("author") or self._owner_from_id(entry_id),
            published_at=last_modified or datetime.now(timezone.utc),
            metadata={
                "platform": SOURCE_NAME,
                "kind": kind,
                "likes": likes,
                "downloads": downloads,
                "trending_score": trending_score,
                "tags": tags,
                "category": getattr(self.cfg, "category", None),
            },
        )

    def _within_window(self, entry: dict, since: datetime) -> bool:
        """Check lastModified against the since window; missing values pass."""
        last_modified = self._parse_datetime(entry.get("lastModified"))
        if last_modified is None:
            return True
        return last_modified >= since

    @staticmethod
    def _owner_from_id(entry_id: str) -> Optional[str]:
        """Derive the owner/namespace from an 'owner/name' id."""
        return entry_id.split("/")[0] if "/" in entry_id else None

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """Normalize a datetime to aware UTC (naive values assumed UTC)."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        """Parse an ISO 8601 timestamp, returning None on failure."""
        if not value or not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return HuggingFaceScraper._as_utc(parsed)

    @staticmethod
    def _likes_int(entry: dict) -> int:
        """Pull like count out of an entry, coercing to int."""
        return HuggingFaceScraper._int(entry.get("likes"))

    @staticmethod
    def _int(value: Any) -> int:
        """Best-effort conversion to int, defaulting to 0."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
