"""Routing smoke: real Dispatcher + middleware + users/commands routers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User

from telegram_bot.core.handlers.commands import router as commands_router
from telegram_bot.core.handlers.users import router as users_router
from telegram_bot.core.middleware.auth import AuthMiddleware
from telegram_bot.core.services.user_store import UserStore


def _update(user_id: int, text: str, uid: int = 1) -> Update:
    msg = Message(
        message_id=uid,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="U", username=f"u{user_id}"),
        text=text,
    )
    return Update(update_id=uid, message=msg)


async def test_invite_start_goes_to_redeem_not_welcome(tmp_path: Path, monkeypatch) -> None:
    store = UserStore(tmp_path / "u.json", [1])
    replies: list[str] = []

    async def fake_answer(self: Message, text: str, **_: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)

    async def fake_send(self: Bot, chat_id: int, text: str, **_: Any) -> None:
        replies.append(f"to{chat_id}:{text}")

    monkeypatch.setattr(Bot, "send_message", fake_send)

    dp = Dispatcher()
    dp.message.outer_middleware(AuthMiddleware(store))
    dp.include_router(users_router)
    dp.include_router(commands_router)
    dp["user_store"] = store
    dp["settings"] = SimpleNamespace(invite_ttl_hours=48)
    bot = Bot("123456:TEST")

    code = store.create_invite("member", by=1)
    await dp.feed_update(bot, _update(9, f"/start {code}"))
    assert store.role_of(9) == "member"
    assert any("member" in r for r in replies)

    # A stranger's plain /start is ignored; the member's plain /start is not
    # swallowed by the redeem handler.
    replies.clear()
    await dp.feed_update(bot, _update(77, "/start", uid=2))
    assert replies == []
