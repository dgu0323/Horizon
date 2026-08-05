"""Persistence helpers for the Telegram bot: daily items and per-chat saves.

Items are kept as plain dicts (the JSON persisted by
``StorageManager.save_daily_items``) — datetime fields stay ISO strings and
are never parsed here.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .._file_utils import _atomic_write_text

_ITEMS_PREFIX = "horizon-"
_ITEMS_SUFFIX = "-items.json"


def item_opportunity(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the item's opportunity analysis dict, or None."""
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return None
    opportunity = metadata.get("opportunity")
    return opportunity if isinstance(opportunity, dict) else None


def opportunity_score(item: Dict[str, Any]) -> Optional[float]:
    """Return the numeric opportunity score of an item, or None."""
    opportunity = item_opportunity(item)
    if not opportunity:
        return None
    try:
        return float(opportunity.get("score"))
    except (TypeError, ValueError):
        return None


class DailyItemsStore:
    """Loads the daily selected-items JSON, falling back to the latest day."""

    def __init__(self, data_dir: Path | str = "data"):
        self.summaries_dir = Path(data_dir) / "summaries"

    def load(self, today: Optional[date] = None) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """Return (date_str, items); date_str is None when no items file exists."""
        today = today or date.today()
        items = self._load_file(today.isoformat())
        if items is not None:
            return today.isoformat(), items
        for path in sorted(self._item_files(), reverse=True):
            date_str = path.name[len(_ITEMS_PREFIX) : -len(_ITEMS_SUFFIX)]
            items = self._load_file(date_str)
            if items is not None:
                return date_str, items
        return None, []

    def _item_files(self) -> List[Path]:
        if not self.summaries_dir.exists():
            return []
        return [
            p
            for p in self.summaries_dir.glob(f"{_ITEMS_PREFIX}*{_ITEMS_SUFFIX}")
            if p.is_file()
        ]

    def _load_file(self, date_str: str) -> Optional[List[Dict[str, Any]]]:
        path = self.summaries_dir / f"{_ITEMS_PREFIX}{date_str}{_ITEMS_SUFFIX}"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []


class SaveStore:
    """Per-chat saved items, persisted to telegram_saves.json."""

    def __init__(self, data_dir: Path | str = "data"):
        self.path = Path(data_dir) / "telegram_saves.json"

    def list(self, chat_id: int) -> List[Dict[str, Any]]:
        """Return the saved entries of one chat (oldest first)."""
        return list(self._load_all().get(str(chat_id), []))

    def add(self, chat_id: int, item: Dict[str, Any], date_str: str) -> bool:
        """Save an item for a chat; return False when it was already saved."""
        all_saves = self._load_all()
        entries = all_saves.setdefault(str(chat_id), [])
        item_id = item.get("id")
        if any(entry.get("id") == item_id for entry in entries):
            return False
        entries.append(
            {
                "id": item_id,
                "title": item.get("title", ""),
                "url": str(item.get("url") or ""),
                "ai_score": item.get("ai_score"),
                "opportunity_score": opportunity_score(item),
                "date": date_str,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._save_all(all_saves)
        return True

    def _load_all(self) -> Dict[str, List[Dict[str, Any]]]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_all(self, all_saves: Dict[str, List[Dict[str, Any]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(all_saves, indent=2, ensure_ascii=False)
        _atomic_write_text(self.path, f"{content}\n")
