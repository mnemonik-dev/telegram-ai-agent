from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.filters import CommandObject
from aiogram.types import Chat, Message, User

from telegram_bot.core.handlers import users
from telegram_bot.core.services.user_store import UserStore


def _msg(user_id: int, text: str, reply_to: Message | None = None) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type="supergroup"),
        from_user=User(id=user_id, is_bot=False, first_name="U", username=f"u{user_id}"),
        text=text,
        reply_to_message=reply_to,
    )


def _cmd(args: str | None) -> CommandObject:
    return CommandObject(prefix="/", command="x", args=args)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = UserStore(tmp_path / "u.json", [1])
    replies: list[str] = []

    async def fake_answer(self: Message, text: str, **_: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    return store, replies


async def test_grant_by_seen_username(env) -> None:
    store, replies = env
    store.note_seen(7, "Bob")
    await users.handle_grant(_msg(1, "/grant @bob member"), _cmd("@bob member"), store)
    assert store.role_of(7) == "member"
    assert "member" in replies[-1]


async def test_grant_unknown_username_explains(env) -> None:
    store, replies = env
    await users.handle_grant(_msg(1, "/grant @ghost member"), _cmd("@ghost member"), store)
    assert store.role_of(7) is None
    assert "/invite" in replies[-1]


async def test_grant_by_reply(env) -> None:
    store, _ = env
    target = _msg(8, "hi")
    await users.handle_grant(_msg(1, "/grant viewer", reply_to=target), _cmd("viewer"), store)
    assert store.role_of(8) == "viewer"


async def test_grant_by_numeric_id_and_bad_role(env) -> None:
    store, replies = env
    await users.handle_grant(_msg(1, "/grant 42 admin"), _cmd("42 admin"), store)
    assert store.role_of(42) is None and "Unknown role" in replies[-1]
    await users.handle_grant(_msg(1, "/grant 42 member"), _cmd("42 member"), store)
    assert store.role_of(42) == "member"


async def test_non_operator_cannot_grant(env) -> None:
    store, replies = env
    store.grant(2, "member", by=1)
    await users.handle_grant(_msg(2, "/grant 5 operator"), _cmd("5 operator"), store)
    assert store.role_of(5) is None and replies == []


async def test_grant_bootstrap_refused(env) -> None:
    store, replies = env
    await users.handle_grant(_msg(1, "/grant 1 viewer"), _cmd("1 viewer"), store)
    assert store.role_of(1) == "operator" and "configuration" in replies[-1]


async def test_revoke(env) -> None:
    store, replies = env
    store.grant(5, "member", by=1)
    await users.handle_revoke(_msg(1, "/revoke 5"), _cmd("5"), store)
    assert store.role_of(5) is None and "revoked" in replies[-1]


async def test_invite_flow(env) -> None:
    store, replies = env
    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def me(self) -> SimpleNamespace:
            return SimpleNamespace(username="mybot")

        async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
            sent.append((chat_id, text))

    settings = SimpleNamespace(invite_ttl_hours=48)
    await users.handle_invite(_msg(1, "/invite member"), _cmd("member"), store, settings, FakeBot())
    link = replies[-1].split("\n")[-1]
    code = link.split("start=")[1]
    assert link.startswith("https://t.me/mybot?start=inv_")

    redeem_msg = _msg(9, f"/start {code}")
    await users.handle_invite_redeem(redeem_msg, store, FakeBot())
    assert store.role_of(9) == "member"
    assert sent and sent[0][0] == 1

    await users.handle_invite_redeem(redeem_msg, store, FakeBot())  # reuse
    assert "invalid" in replies[-1]


async def test_users_list_and_whoami(env) -> None:
    store, replies = env
    store.grant(5, "viewer", by=1, username="v")
    await users.handle_users(_msg(1, "/users"), store)
    assert "<code>5</code>" in replies[-1] and "(config)" in replies[-1]
    await users.handle_whoami(_msg(5, "/whoami"), store)
    assert "viewer" in replies[-1]
