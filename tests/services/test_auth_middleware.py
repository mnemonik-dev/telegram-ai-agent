from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram.types import Chat, Message, User

from telegram_bot.core.middleware.auth import AuthMiddleware, command_name
from telegram_bot.core.services.user_store import UserStore


def _msg(user_id: int, text: str, chat_type: str = "supergroup") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-100 if chat_type != "private" else user_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="U", username=f"u{user_id}"),
        text=text,
    )


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = UserStore(tmp_path / "u.json", [1])
    store.grant(2, "member", by=1)
    store.grant(3, "viewer", by=1)
    replies: list[str] = []

    async def fake_answer(self: Message, text: str, **_: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    reached: list[dict[str, Any]] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        reached.append(data)
        return "ok"

    return AuthMiddleware(store), store, replies, reached, handler


async def _run(setup, msg: Message) -> Any:
    mw, _store, _replies, _reached, handler = setup
    return await mw(handler, msg, {})


async def test_operator_and_member_pass(setup) -> None:
    assert await _run(setup, _msg(1, "hello")) == "ok"
    assert await _run(setup, _msg(2, "hello")) == "ok"
    assert [d["role"] for d in setup[3]] == ["operator", "member"]


async def test_stranger_dropped_silently(setup) -> None:
    assert await _run(setup, _msg(9, "hello")) is None
    assert setup[2] == [] and setup[3] == []


async def test_member_blocked_from_operator_commands(setup) -> None:
    for cmd in ("/invite", "/grant @x member", "/revoke 5", "/users", "/relogin@bot"):
        assert await _run(setup, _msg(2, cmd)) is None
    assert len(setup[2]) == 5 and setup[3] == []


async def test_member_may_use_other_commands(setup) -> None:
    assert await _run(setup, _msg(2, "/engine")) == "ok"


async def test_operator_may_use_operator_commands(setup) -> None:
    assert await _run(setup, _msg(1, "/grant @x member")) == "ok"


async def test_viewer_is_read_only(setup) -> None:
    assert await _run(setup, _msg(3, "do something")) is None
    assert await _run(setup, _msg(3, "/whoami")) == "ok"
    assert setup[2] == []  # supergroup: silent


async def test_viewer_told_in_private(setup) -> None:
    assert await _run(setup, _msg(3, "hi", chat_type="private")) is None
    assert len(setup[2]) == 1


async def test_stranger_may_redeem_invite_in_private_only(setup) -> None:
    text = "/start inv_abcdef123"
    assert await _run(setup, _msg(9, text, chat_type="private")) == "ok"
    assert setup[3][-1]["role"] is None
    assert await _run(setup, _msg(9, text, chat_type="supergroup")) is None


async def test_note_seen_caches_username_even_for_strangers(setup) -> None:
    await _run(setup, _msg(9, "hello"))
    assert setup[1].resolve_username("@u9") == 9


def test_command_name() -> None:
    assert command_name("/Grant@MyBot x") == "grant"
    assert command_name("hello") is None
    assert command_name(None) is None
