from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandObject
from aiogram.types import Chat, Message, MessageEntity, Update, User

from telegram_bot.core.handlers import chat_mode as chat_handlers
from telegram_bot.core.middleware.chat_mode import ChatModeMiddleware
from telegram_bot.core.services.chat_context import (
    ChatBacklog,
    addressing,
    format_context,
)
from telegram_bot.core.services.topic_config import TopicConfig

BOT = User(id=999, is_bot=True, first_name="Bot", username="FabricBot")
ALICE = User(id=11, is_bot=False, first_name="Alice", username="alice")
BOB = User(id=12, is_bot=False, first_name="Bob")


def _msg(
    text: str | None,
    user: User = ALICE,
    *,
    thread: int | None = 5,
    reply_to: Message | None = None,
    entities: list[MessageEntity] | None = None,
    **extra: Any,
) -> Message:
    return Message(
        message_id=50,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type="supergroup", is_forum=True),
        message_thread_id=thread,
        from_user=user,
        text=text,
        reply_to_message=reply_to,
        entities=entities,
        **extra,
    )


def _addr(m: Message, engine: str = "claude") -> str | None:
    return addressing(m, bot_id=BOT.id, bot_username="FabricBot", engine=engine)


# ----- addressing -----


def test_plain_message_not_addressed() -> None:
    assert _addr(_msg("hello team")) is None


def test_mention_by_username_case_insensitive() -> None:
    assert _addr(_msg("hey @fabricbot please look")) == "bot"


def test_reply_to_bot_message_is_addressed() -> None:
    bot_msg = _msg("an answer", user=BOT)
    assert _addr(_msg("thanks, and also...", reply_to=bot_msg)) == "bot"


def test_reply_to_human_not_addressed() -> None:
    assert _addr(_msg("agree", reply_to=_msg("idea", user=BOB))) is None


def test_topic_creation_reply_is_not_a_reply_to_bot() -> None:
    # A bot-created topic makes every message "reply" to the creation message.
    created = Message(
        message_id=5,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type="supergroup", is_forum=True),
        message_thread_id=5,
        from_user=BOT,
        forum_topic_created={"name": "proj", "icon_color": 1},
    )
    assert _addr(_msg("just chatting", reply_to=created)) is None


def test_engine_tag_matches_topic_engine() -> None:
    assert _addr(_msg("@claude refactor this"), "claude") == "bot"
    assert _addr(_msg("@Codex review"), "codex") == "bot"


def test_other_engine_tag() -> None:
    assert _addr(_msg("@codex review"), "claude") == "other_engine"


def test_email_is_not_an_engine_tag() -> None:
    assert _addr(_msg("mail me at me@claude.ai")) is None


def test_text_mention_entity() -> None:
    ent = MessageEntity(type="text_mention", offset=0, length=3, user=BOT)
    assert _addr(_msg("bot do it", entities=[ent])) == "bot"


# ----- backlog / context -----


def test_format_context_orders_and_attributes() -> None:
    log = ChatBacklog()
    key = (-100, 5)
    log.add(key, "Alice", "first")
    log.add(key, "Bob", "second")
    out = format_context(log.drain(key), "Carol")
    assert out.index("Alice: first") < out.index("Bob: second") < out.index("Carol: ")
    assert out.endswith("Carol: ")
    assert log.drain(key) == []


def test_context_without_backlog_still_attributes() -> None:
    out = format_context([], "Carol")
    assert "Messages since" not in out and out.endswith("Carol: ")


def test_backlog_is_capped_and_per_channel() -> None:
    log = ChatBacklog(maxlen=3)
    for i in range(10):
        log.add((1, 1), "A", f"m{i}")
    log.add((1, 2), "B", "other")
    assert [e.text for e in log.drain((1, 1))] == ["m7", "m8", "m9"]
    assert [e.text for e in log.drain((1, 2))] == ["other"]


def test_context_budget_drops_oldest() -> None:
    log = ChatBacklog()
    for i in range(30):
        log.add((1, 1), "A", f"{i}:" + "x" * 480)
    out = format_context(log.drain((1, 1)), "C")
    assert "A: 29:" in out and "A: 0:" not in out


# ----- config -----


def _config(tmp_path: Path, mode: str | None) -> TopicConfig:
    topic: dict[str, Any] = {"name": "t"}
    if mode:
        topic["chat_mode"] = mode
    path = tmp_path / "topics.json"
    path.write_text(json.dumps({"topics": {"5": topic}}))
    return TopicConfig(str(path), str(tmp_path))


def test_chat_mode_defaults_and_invalid_fall_back(tmp_path: Path) -> None:
    assert _config(tmp_path, None).get_topic(5).chat_mode == "direct"
    assert _config(tmp_path, "bogus").get_topic(5).chat_mode == "direct"
    assert _config(tmp_path, "mention").get_topic(5).chat_mode == "mention"


async def test_chat_command_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path, None)
    replies: list[str] = []

    async def fake_answer(self: Message, text: str, **_: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    cmd = CommandObject(prefix="/", command="chat", args="mention")
    await chat_handlers.handle_chat_command(_msg("/chat mention"), cmd, cfg)
    assert TopicConfig(cfg._config_path, str(tmp_path)).get_topic(5).chat_mode == "mention"
    bad = CommandObject(prefix="/", command="chat", args="loud")
    await chat_handlers.handle_chat_command(_msg("/chat loud"), bad, cfg)
    assert "Usage" in replies[-1]


# ----- middleware through a real Dispatcher -----


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = _config(tmp_path, "mention")
    backlog = ChatBacklog()
    seen: list[tuple[str | None, str | None]] = []
    replies: list[str] = []

    async def fake_answer(self: Message, text: str, **_: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)

    async def fake_me(self: Bot) -> User:
        return BOT

    monkeypatch.setattr(Bot, "me", fake_me)

    dp = Dispatcher()
    dp.message.middleware(ChatModeMiddleware(cfg, backlog))

    @dp.message()
    async def catch_all(message: Message, chat_context: str | None = None) -> None:
        seen.append((message.text, chat_context))

    async def feed(msg: Message, uid: int = 1) -> None:
        await dp.feed_update(Bot("123456:TEST"), Update(update_id=uid, message=msg))

    return SimpleNamespace(feed=feed, seen=seen, replies=replies, backlog=backlog, cfg=cfg)


async def test_unaddressed_is_recorded_not_handled(rig) -> None:
    await rig.feed(_msg("we should use postgres", ALICE))
    assert rig.seen == [] and len(rig.backlog) == 1


async def test_addressed_gets_context_and_clears_backlog(rig) -> None:
    await rig.feed(_msg("we should use postgres", ALICE))
    await rig.feed(_msg("or sqlite?", BOB), 2)
    await rig.feed(_msg("@FabricBot what do you think?", ALICE), 3)
    ((text, ctx),) = rig.seen
    assert text == "@FabricBot what do you think?"
    assert ctx and "Alice (@alice): we should use postgres" in ctx
    assert "Bob: or sqlite?" in ctx and ctx.endswith("Alice (@alice): ")
    assert len(rig.backlog) == 0


async def test_commands_always_pass(rig) -> None:
    await rig.feed(_msg("/status"))
    assert rig.seen == [("/status", None)]


async def test_other_engine_tag_hints(rig) -> None:
    await rig.feed(_msg("@codex check this"))
    assert rig.seen == [] and "/engine" in rig.replies[-1]


async def test_direct_mode_untouched(rig) -> None:
    await rig.cfg.update_chat_mode(5, "direct")
    await rig.feed(_msg("anything"))
    assert rig.seen == [("anything", None)]


async def test_private_chat_untouched(rig) -> None:
    private = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=11, type="private"),
        from_user=ALICE,
        text="hi",
    )
    await rig.feed(private)
    assert rig.seen == [("hi", None)]
