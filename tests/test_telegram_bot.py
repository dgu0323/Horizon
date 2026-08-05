"""Tests for the opportunity-radar Telegram bot (src/telegram_bot)."""

import asyncio
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from src.telegram_bot.__main__ import main as bot_main
from src.telegram_bot.bot import MAX_MESSAGE_LENGTH, TelegramBot, split_message
from src.telegram_bot.commands import HELP_TEXT, CommandRouter
from src.telegram_bot.config import BotConfig, BotConfigError
from src.telegram_bot.store import DailyItemsStore, SaveStore

TODAY = date(2026, 8, 4)
TODAY_STR = TODAY.isoformat()


def make_item(
    item_id: str,
    title: str,
    ai_score=None,
    opp_score=None,
    reason: str = "",
    counter_view: str = "",
    validation_step: str = "",
    mvp: str = "",
    discussion_url: str = "",
    sources=None,
) -> dict:
    """Build a ContentItem.model_dump-shaped dict for fixtures."""
    metadata = {"discussion_url": discussion_url, "sources": sources or []}
    if opp_score is not None or reason:
        metadata["opportunity"] = {
            "score": opp_score,
            "reason": reason,
            "problem": "some problem",
            "target_user": "indie hackers",
            "mvp": mvp,
            "distribution": "SEO",
            "monetization": "subscription",
            "counter_view": counter_view,
            "validation_step": validation_step,
            "tags": ["saas"],
        }
    return {
        "id": item_id,
        "source_type": "hackernews",
        "title": title,
        "url": f"https://example.com/{item_id}",
        "content": None,
        "author": "alice",
        "published_at": "2026-08-04T08:00:00+00:00",
        "fetched_at": "2026-08-04T09:00:00+00:00",
        "metadata": metadata,
        "ai_score": ai_score,
        "ai_reason": None,
        "ai_summary": None,
        "ai_tags": ["ai"],
    }


@pytest.fixture
def items() -> list:
    return [
        make_item(
            "hn:story:1",
            "AI code review tool",
            ai_score=8.5,
            opp_score=7.5,
            reason="Teams drown in PRs",
            counter_view="Incumbents will ship this",
            validation_step="DM 20 CTOs this week",
            mvp="GitHub Action posting review summaries",
            discussion_url="https://news.ycombinator.com/item?id=1",
            sources=[{"title": "PR stats", "url": "https://example.com/stats"}],
        ),
        make_item(
            "hn:story:2",
            "Open source vector DB",
            ai_score=6.0,
            opp_score=9.0,
            reason="RAG demand keeps growing",
        ),
        make_item("hn:story:3", "Quiet news item", ai_score=5.0),
    ]


@pytest.fixture
def data_dir(tmp_path: Path, items: list) -> Path:
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    path = summaries / f"horizon-{TODAY_STR}-items.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return tmp_path


@pytest.fixture
def router(data_dir: Path) -> CommandRouter:
    return CommandRouter(
        items_store=DailyItemsStore(data_dir), save_store=SaveStore(data_dir)
    )


def run(coro):
    return asyncio.run(coro)


# --- daily items loading ----------------------------------------------------


def test_loads_todays_items(data_dir: Path):
    date_str, items = DailyItemsStore(data_dir).load(TODAY)
    assert date_str == TODAY_STR
    assert len(items) == 3


def test_falls_back_to_latest_previous_day(data_dir: Path):
    date_str, items = DailyItemsStore(data_dir).load(date(2026, 8, 10))
    assert date_str == TODAY_STR
    assert len(items) == 3


def test_returns_none_when_no_files(tmp_path: Path):
    date_str, items = DailyItemsStore(tmp_path).load(TODAY)
    assert date_str is None
    assert items == []


# --- /today and /top10 -------------------------------------------------------


def test_today_lists_items_with_scores(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/today", today=TODAY)
    assert TODAY_STR in reply
    assert "1. AI code review tool" in reply
    assert "新闻 8.5/10" in reply
    assert "机会 7.5/10" in reply
    # Item without opportunity shows a dash instead of a score.
    assert "3. Quiet news item" in reply
    assert "机会 —/10" in reply


def test_top10_sorts_by_opportunity_score(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/top10", today=TODAY)
    pos_best = reply.index("Open source vector DB")  # opp 9.0
    pos_second = reply.index("AI code review tool")  # opp 7.5
    pos_none = reply.index("Quiet news item")  # no opportunity -> last
    assert pos_best < pos_second < pos_none
    # Original /today numbering is preserved.
    assert "2. Open source vector DB" in reply


def test_today_reports_missing_data(tmp_path: Path):
    router = CommandRouter(
        items_store=DailyItemsStore(tmp_path), save_store=SaveStore(tmp_path)
    )
    [reply] = router.handle(chat_id=1, text="/today", today=TODAY)
    assert "还没有生成日报数据" in reply


# --- /save and /saves --------------------------------------------------------


def test_save_persists_per_chat(router: CommandRouter, data_dir: Path):
    [reply] = router.handle(chat_id=42, text="/save 1", today=TODAY)
    assert "已收藏" in reply

    saves_path = data_dir / "telegram_saves.json"
    saves = json.loads(saves_path.read_text(encoding="utf-8"))
    entry = saves["42"][0]
    assert entry["id"] == "hn:story:1"
    assert entry["title"] == "AI code review tool"
    assert entry["opportunity_score"] == 7.5
    assert entry["date"] == TODAY_STR

    # Saving the same item again does not duplicate it.
    router.handle(chat_id=42, text="/save hn:story:1", today=TODAY)
    assert len(SaveStore(data_dir).list(42)) == 1

    # Saves are isolated per chat.
    assert SaveStore(data_dir).list(7) == []


def test_saves_lists_own_entries(router: CommandRouter):
    router.handle(chat_id=42, text="/save 2", today=TODAY)
    [reply] = router.handle(chat_id=42, text="/saves", today=TODAY)
    assert "我的收藏" in reply
    assert "Open source vector DB" in reply

    [empty] = router.handle(chat_id=99, text="/saves", today=TODAY)
    assert "还没有收藏" in empty


def test_save_rejects_bad_arguments(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/save", today=TODAY)
    assert "用法" in reply
    [reply] = router.handle(chat_id=1, text="/save 99", today=TODAY)
    assert "超出范围" in reply
    [reply] = router.handle(chat_id=1, text="/save nope:id", today=TODAY)
    assert "找不到条目" in reply


# --- /why, /sources, /validate -----------------------------------------------


def test_why_shows_reason_and_counter_view(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/why 1", today=TODAY)
    assert "机会判断:Teams drown in PRs" in reply
    assert "反方观点:Incumbents will ship this" in reply
    assert "#1 AI code review tool" in reply


def test_why_accepts_item_id(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/why hn:story:2", today=TODAY)
    assert "RAG demand keeps growing" in reply


def test_why_without_opportunity(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/why 3", today=TODAY)
    assert "没有商业机会分析" in reply


def test_sources_shows_urls(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/sources 1", today=TODAY)
    assert "原文:https://example.com/hn:story:1" in reply
    assert "讨论:https://news.ycombinator.com/item?id=1" in reply
    assert "PR stats — https://example.com/stats" in reply


def test_sources_without_links(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/sources 3", today=TODAY)
    assert "原文:https://example.com/hn:story:3" in reply
    assert "讨论" not in reply


def test_validate_shows_step_and_mvp(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/validate 1", today=TODAY)
    assert "最快验证:DM 20 CTOs this week" in reply
    assert "MVP:GitHub Action posting review summaries" in reply


def test_validate_without_fields(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/validate 2", today=TODAY)
    assert "没有验证建议" in reply
    [reply] = router.handle(chat_id=1, text="/validate 3", today=TODAY)
    assert "没有商业机会分析" in reply


# --- help and unknown commands ------------------------------------------------


def test_start_and_help_return_help_text(router: CommandRouter):
    assert router.handle(chat_id=1, text="/start", today=TODAY) == [HELP_TEXT]
    assert router.handle(chat_id=1, text="/help", today=TODAY) == [HELP_TEXT]
    assert router.handle(chat_id=1, text="hello", today=TODAY) == [HELP_TEXT]


def test_unknown_command(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/nope", today=TODAY)
    assert "未知命令" in reply


def test_command_with_botname_suffix(router: CommandRouter):
    [reply] = router.handle(chat_id=1, text="/today@horizon_radar_bot", today=TODAY)
    assert "机会雷达" in reply


# --- message splitting --------------------------------------------------------


def test_split_message_short_text():
    assert split_message("hello") == ["hello"]


def test_split_message_prefers_newline_boundaries():
    lines = [f"line {i} " + "x" * 90 for i in range(50)]
    text = "\n".join(lines)
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
    assert all(not chunk.startswith(" ") for chunk in chunks)
    # Reassembling the chunks yields every original line exactly once.
    assert [line for chunk in chunks for line in chunk.split("\n")] == lines


def test_split_message_hard_breaks_long_lines():
    text = "x" * (MAX_MESSAGE_LENGTH * 2 + 10)
    chunks = split_message(text)
    assert [len(chunk) for chunk in chunks] == [
        MAX_MESSAGE_LENGTH,
        MAX_MESSAGE_LENGTH,
        10,
    ]


# --- bot HTTP layer (mocked transport) ----------------------------------------


class FakeTelegramAPI:
    """httpx.MockTransport handler recording sent messages."""

    def __init__(self, updates: list | None = None):
        self.updates = updates or []
        self.sent: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        if request.url.path.endswith("/sendMessage"):
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(404, json={"ok": False})


def make_bot(api: FakeTelegramAPI, router: CommandRouter, allowed=None):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(api), base_url="https://api.telegram.org/botTEST"
    )
    bot = TelegramBot(token="TEST", router=router, allowed_chat_ids=allowed)
    return bot, client


def test_send_message_splits_long_text(router: CommandRouter):
    api = FakeTelegramAPI()
    bot, client = make_bot(api, router)
    text = "\n".join(f"item {i} " + "y" * 90 for i in range(60))
    run(bot.send_message(client, chat_id=1, text=text))
    run(client.aclose())
    assert len(api.sent) > 1
    assert all(len(m["text"]) <= MAX_MESSAGE_LENGTH for m in api.sent)
    assert all(m["chat_id"] == 1 for m in api.sent)


def test_handle_update_routes_and_replies(router: CommandRouter):
    api = FakeTelegramAPI()
    bot, client = make_bot(api, router)
    update = {
        "update_id": 1,
        "message": {"message_id": 1, "chat": {"id": 42}, "text": "/why 1"},
    }
    run(bot.handle_update(client, update))
    run(client.aclose())
    assert len(api.sent) == 1
    assert api.sent[0]["chat_id"] == 42
    assert "Teams drown in PRs" in api.sent[0]["text"]


def test_handle_update_ignores_non_whitelisted_chat(router: CommandRouter):
    api = FakeTelegramAPI()
    bot, client = make_bot(api, router, allowed={42})
    update = {
        "update_id": 1,
        "message": {"message_id": 1, "chat": {"id": 7}, "text": "/today"},
    }
    run(bot.handle_update(client, update))
    run(client.aclose())
    assert api.sent == []


def test_handle_update_ignores_non_text_updates(router: CommandRouter):
    api = FakeTelegramAPI()
    bot, client = make_bot(api, router)
    run(bot.handle_update(client, {"update_id": 1, "message": {"chat": {"id": 1}}}))
    run(bot.handle_update(client, {"update_id": 2, "edited_message": {}}))
    run(client.aclose())
    assert api.sent == []


def test_get_updates_parses_result(router: CommandRouter):
    updates = [{"update_id": 5, "message": {"chat": {"id": 1}, "text": "/start"}}]
    api = FakeTelegramAPI(updates=updates)
    bot, client = make_bot(api, router)
    result = run(bot._get_updates(client, offset=5))
    run(client.aclose())
    assert result == updates


# --- configuration and entry point --------------------------------------------


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1, 2 ,3")
    monkeypatch.setenv("TELEGRAM_DATA_DIR", "/tmp/radar")
    config = BotConfig.from_env()
    assert config.token == "tok"
    assert config.allowed_chat_ids == {1, 2, 3}
    assert config.data_dir == Path("/tmp/radar")


def test_config_defaults(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_DATA_DIR", raising=False)
    config = BotConfig.from_env()
    assert config.allowed_chat_ids == set()
    assert config.data_dir == Path("data")


def test_config_requires_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(BotConfigError, match="TELEGRAM_BOT_TOKEN"):
        BotConfig.from_env()


def test_config_rejects_bad_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1,abc")
    with pytest.raises(BotConfigError, match="abc"):
        BotConfig.from_env()


def test_main_exits_gracefully_without_token(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert bot_main() == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
