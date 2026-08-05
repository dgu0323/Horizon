"""Cross-run cache for AI analysis results.

Hot items can persist across overlapping 24h fetch windows, so without a
cache every run re-spends tokens re-analyzing items a previous run already
scored. This module holds the pure cache logic; file IO lives in
``src/storage/manager.py`` (``data/analysis_cache.json``).
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from ..models import ContentItem

CACHE_VERSION = 1
DEFAULT_TTL_HOURS = 72

# Reasons set by ContentAnalyzer fallback paths; never cache these.
_FAILURE_REASONS = {"Analysis failed", "Analysis response parse failed"}


def empty_cache() -> dict:
    return {"version": CACHE_VERSION, "items": {}}


def prune(cache: dict, ttl_hours: int = DEFAULT_TTL_HOURS, now: Optional[datetime] = None) -> dict:
    """Drop entries older than the TTL (in-place, returns the cache)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=ttl_hours)
    kept = {}
    for item_id, entry in cache.get("items", {}).items():
        try:
            analyzed_at = datetime.fromisoformat(entry["analyzed_at"])
        except (KeyError, ValueError, TypeError):
            continue
        if analyzed_at.tzinfo is None:
            analyzed_at = analyzed_at.replace(tzinfo=timezone.utc)
        if analyzed_at >= cutoff:
            kept[item_id] = entry
    cache["items"] = kept
    return cache


def apply_cached_analysis(item: ContentItem, cache: dict) -> bool:
    """Restore news-analysis fields from cache. Returns True on a hit."""
    entry = cache.get("items", {}).get(item.id)
    if not entry:
        return False
    item.ai_score = entry.get("ai_score")
    item.ai_reason = entry.get("ai_reason")
    item.ai_summary = entry.get("ai_summary")
    item.ai_tags = entry.get("ai_tags") or []
    return True


def store_analysis(item: ContentItem, cache: dict, now: Optional[datetime] = None) -> None:
    """Record an item's news-analysis result; skips failed analyses."""
    if item.ai_score is None or item.ai_reason in _FAILURE_REASONS:
        return
    now = now or datetime.now(timezone.utc)
    cache.setdefault("items", {})[item.id] = {
        "analyzed_at": now.isoformat(),
        "ai_score": item.ai_score,
        "ai_reason": item.ai_reason,
        "ai_summary": item.ai_summary,
        "ai_tags": list(item.ai_tags),
    }


def apply_cached_opportunity(item: ContentItem, cache: dict) -> bool:
    """Restore metadata["opportunity"] from cache. Returns True on a hit."""
    entry = cache.get("items", {}).get(item.id)
    if not entry or "opportunity" not in entry:
        return False
    item.metadata["opportunity"] = entry["opportunity"]
    return True


def store_opportunity(item: ContentItem, cache: dict, now: Optional[datetime] = None) -> None:
    """Record an item's opportunity result onto its existing cache entry."""
    opportunity = item.metadata.get("opportunity")
    if opportunity is None:
        return
    now = now or datetime.now(timezone.utc)
    entry = cache.setdefault("items", {}).setdefault(
        item.id, {"analyzed_at": now.isoformat()}
    )
    entry["opportunity"] = opportunity
