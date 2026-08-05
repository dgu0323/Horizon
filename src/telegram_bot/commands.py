"""Command handling for the opportunity-radar bot.

Pure text-in / text-out routing — no network access here, so every command
is trivially unit-testable.
"""

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .store import DailyItemsStore, SaveStore, item_opportunity, opportunity_score

HELP_TEXT = (
    "📡 Horizon 机会雷达 Bot\n"
    "\n"
    "可用命令：\n"
    "/today — 今日条目列表(新闻分 + 机会分)\n"
    "/top10 — 机会评分 Top 10\n"
    "/save <序号或id> — 收藏该条\n"
    "/why <序号或id> — 机会判断与反方观点\n"
    "/sources <序号或id> — 原文 / 讨论 / 参考链接\n"
    "/validate <序号或id> — 最快验证步骤与 MVP\n"
    "/saves — 我的收藏\n"
    "/help — 显示本帮助"
)

NO_ITEMS_TEXT = "今天还没有生成日报数据,稍后再试试吧。"


def _format_score(value: Any) -> str:
    """Format a 0-10 score compactly (8.0 -> '8'), '—' when missing."""
    if value is None:
        return "—"
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "—"


def _item_line(index: int, item: Dict[str, Any]) -> str:
    """One listing line: number, title, news score and opportunity score."""
    title = str(item.get("title") or "(无标题)")
    news = _format_score(item.get("ai_score"))
    opp = _format_score(opportunity_score(item))
    return f"{index}. {title}\n   ⭐️ 新闻 {news}/10 · 💡 机会 {opp}/10"


class CommandRouter:
    """Maps incoming message text to reply text for one chat."""

    def __init__(self, items_store: DailyItemsStore, save_store: SaveStore):
        self.items_store = items_store
        self.save_store = save_store

    def handle(self, chat_id: int, text: str, today: Optional[date] = None) -> List[str]:
        """Return the reply message(s) for an incoming text."""
        text = (text or "").strip()
        if not text.startswith("/"):
            return [HELP_TEXT]

        command, _, arg = text.partition(" ")
        # Allow /command@botname as produced by group chats.
        command = command[1:].split("@", 1)[0].lower()
        arg = arg.strip()

        handlers = {
            "start": self._help,
            "help": self._help,
            "today": self._today,
            "top10": self._top10,
            "save": self._save,
            "why": self._why,
            "sources": self._sources,
            "validate": self._validate,
            "saves": self._saves,
        }
        handler = handlers.get(command)
        if handler is None:
            return [f"未知命令 /{command}\n\n{HELP_TEXT}"]
        return handler(chat_id, arg, today)

    # --- command handlers -------------------------------------------------

    def _help(self, chat_id: int, arg: str, today: Optional[date]) -> List[str]:
        return [HELP_TEXT]

    def _today(self, chat_id: int, arg: str, today: Optional[date]) -> List[str]:
        date_str, items = self.items_store.load(today)
        if date_str is None:
            return [NO_ITEMS_TEXT]
        if not items:
            return [f"📅 {date_str} 没有选中的条目。"]
        lines = [f"📅 {date_str} 机会雷达({len(items)} 条)", ""]
        lines += [_item_line(i + 1, item) for i, item in enumerate(items)]
        lines += ["", "回复 /why <序号> 看机会分析 · /save <序号> 收藏"]
        return ["\n".join(lines)]

    def _top10(self, chat_id: int, arg: str, today: Optional[date]) -> List[str]:
        date_str, items = self.items_store.load(today)
        if date_str is None:
            return [NO_ITEMS_TEXT]
        # Keep original numbering; items without an opportunity sort last.
        ranked = sorted(
            enumerate(items),
            key=lambda pair: (
                opportunity_score(pair[1]) is None,
                -(opportunity_score(pair[1]) or 0.0),
            ),
        )[:10]
        if not ranked:
            return [f"📅 {date_str} 没有选中的条目。"]
        lines = [f"🏆 机会评分 Top 10({date_str})", ""]
        for index, item in ranked:
            lines.append(_item_line(index + 1, item))
        return ["\n".join(lines)]

    def _save(self, chat_id: int, arg: str, today: Optional[date]) -> List[str]:
        date_str, items = self.items_store.load(today)
        if date_str is None:
            return [NO_ITEMS_TEXT]
        item, error = self._resolve_item(arg, items, "/save")
        if error:
            return [error]
        added = self.save_store.add(chat_id, item, date_str)
        title = item.get("title", "")
        if added:
            return [f"✅ 已收藏:{title}\n用 /saves 查看你的收藏列表。"]
        return [f"已收藏过了:{title}"]

    def _why(self, chat_id: int, arg: str, today: Optional[date]) -> List[str]:
        result = self._item_with_opportunity(arg, today, "/why")
        if isinstance(result, str):
            return [result]
        index, item, opportunity = result
        lines = [self._item_header(index, item, opportunity)]
        reason = str(opportunity.get("reason") or "").strip()
        counter = str(opportunity.get("counter_view") or "").strip()
        if reason:
            lines.append(f"- 机会判断:{reason}")
        if counter:
            lines.append(f"- 反方观点:{counter}")
        if not reason and not counter:
            lines.append("该条没有机会判断内容。")
        return ["\n".join(lines)]

    def _sources(self, chat_id: int, arg: str, today: Optional[date]) -> List[str]:
        date_str, items = self.items_store.load(today)
        if date_str is None:
            return [NO_ITEMS_TEXT]
        item, error = self._resolve_item(arg, items, "/sources")
        if error:
            return [error]
        metadata = item.get("metadata") or {}
        lines = [f"🔗 {item.get('title', '')}"]
        url = str(item.get("url") or "").strip()
        if url:
            lines.append(f"- 原文:{url}")
        discussion_url = str(metadata.get("discussion_url") or "").strip()
        if discussion_url:
            lines.append(f"- 讨论:{discussion_url}")
        sources = metadata.get("sources") or []
        if isinstance(sources, list) and sources:
            lines.append("- 参考:")
            for source in sources:
                if not isinstance(source, dict):
                    continue
                source_title = str(source.get("title") or "").strip()
                source_url = str(source.get("url") or "").strip()
                if source_url:
                    lines.append(f"  · {source_title or source_url} — {source_url}")
                elif source_title:
                    lines.append(f"  · {source_title}")
        if len(lines) == 1:
            lines.append("该条没有可用的链接。")
        return ["\n".join(lines)]

    def _validate(self, chat_id: int, arg: str, today: Optional[date]) -> List[str]:
        result = self._item_with_opportunity(arg, today, "/validate")
        if isinstance(result, str):
            return [result]
        index, item, opportunity = result
        lines = [self._item_header(index, item, opportunity)]
        validation = str(opportunity.get("validation_step") or "").strip()
        mvp = str(opportunity.get("mvp") or "").strip()
        if validation:
            lines.append(f"- 最快验证:{validation}")
        if mvp:
            lines.append(f"- MVP:{mvp}")
        if not validation and not mvp:
            lines.append("该条没有验证建议。")
        return ["\n".join(lines)]

    def _saves(self, chat_id: int, arg: str, today: Optional[date]) -> List[str]:
        entries = self.save_store.list(chat_id)
        if not entries:
            return ["你还没有收藏任何条目。用 /today 查看今日雷达,/save <序号> 收藏。"]
        lines = [f"⭐ 我的收藏({len(entries)} 条)", ""]
        for i, entry in enumerate(entries, start=1):
            news = _format_score(entry.get("ai_score"))
            opp = _format_score(entry.get("opportunity_score"))
            lines.append(
                f"{i}. {entry.get('title', '')}\n"
                f"   {entry.get('date', '')} · ⭐️ 新闻 {news}/10 · 💡 机会 {opp}/10"
            )
        return ["\n".join(lines)]

    # --- helpers ----------------------------------------------------------

    def _resolve_item(
        self, arg: str, items: List[Dict[str, Any]], usage: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Resolve a 1-based index or an item id to an item of the day."""
        if not arg:
            return None, f"用法:{usage} <序号或id>(序号见 /today)"
        if arg.isdigit():
            index = int(arg)
            if 1 <= index <= len(items):
                return items[index - 1], None
            return None, f"序号 {index} 超出范围(1-{len(items)})。"
        for item in items:
            if item.get("id") == arg:
                return item, None
        return None, f"找不到条目:{arg}(用 /today 查看序号)"

    def _item_with_opportunity(
        self, arg: str, today: Optional[date], usage: str
    ) -> "Tuple[int, Dict[str, Any], Dict[str, Any]] | str":
        """Resolve an item that has an opportunity analysis, or an error text."""
        date_str, items = self.items_store.load(today)
        if date_str is None:
            return NO_ITEMS_TEXT
        item, error = self._resolve_item(arg, items, usage)
        if error:
            return error
        opportunity = item_opportunity(item)
        if not opportunity:
            return f"《{item.get('title', '')}》没有商业机会分析。"
        index = items.index(item) + 1
        return index, item, opportunity

    def _item_header(
        self, index: int, item: Dict[str, Any], opportunity: Dict[str, Any]
    ) -> str:
        opp = _format_score(opportunity_score(item))
        news = _format_score(item.get("ai_score"))
        return (
            f"#{index} {item.get('title', '')}\n"
            f"💡 机会 {opp}/10 · ⭐️ 新闻 {news}/10"
        )
