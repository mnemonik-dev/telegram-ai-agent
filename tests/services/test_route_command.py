from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from aiogram.filters import CommandObject
from aiogram.types import Chat, Message, User

from telegram_bot.core.handlers import route
from telegram_bot.core.services.user_store import UserStore


def _msg(user_id: int) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type="supergroup"),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        text="/route",
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = UserStore(tmp_path / "u.json", [1])
    store.grant(2, "member", by=1)
    replies: list[str] = []
    seen: list[httpx.Request] = []
    state: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        stage = request.url.path.rsplit("/", 1)[-1]
        if request.method == "PUT":
            body = __import__("json").loads(request.content)
            if body.get("engine") == "bad":
                return httpx.Response(422, json={"detail": "unknown engine: 'bad'"})
            state.setdefault(stage, {}).update(body)
        elif request.method == "DELETE":
            state.pop(stage, None)
        return httpx.Response(200, json={"stages": state, "engines": ["claude", "codex", "kimi"]})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(route, "_client", lambda: httpx.AsyncClient(transport=transport))

    async def fake_answer(self: Message, text: str, **_: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    settings = SimpleNamespace(workspace_resolver_url="http://symphony:8080/")
    return SimpleNamespace(store=store, replies=replies, seen=seen, settings=settings)


async def _run(env, user: int, args: str | None, settings: Any = None) -> None:
    cmd = CommandObject(prefix="/", command="route", args=args)
    await route.handle_route(_msg(user), cmd, settings or env.settings, env.store)


async def test_show_empty(env) -> None:
    await _run(env, 1, None)
    assert "No overrides" in env.replies[-1] and "kimi" in env.replies[-1]
    assert str(env.seen[0].url) == "http://symphony:8080/routing"


async def test_set_model_and_engine_then_reset(env) -> None:
    await _run(env, 1, "code model sonnet")
    await _run(env, 1, "code engine kimi")
    assert "sonnet" in env.replies[-1] and "kimi" in env.replies[-1]
    await _run(env, 1, "code reset")
    assert "No overrides" in env.replies[-1]


async def test_rejection_is_reported(env) -> None:
    await _run(env, 1, "code engine bad")
    assert "Rejected" in env.replies[-1] and "unknown engine" in env.replies[-1]


async def test_bad_usage(env) -> None:
    await _run(env, 1, "code frobnicate x y z")
    assert "Usage" in env.replies[-1] and env.seen == []


async def test_member_ignored(env) -> None:
    await _run(env, 2, "code model sonnet")
    assert env.replies == [] and env.seen == []


async def test_no_symphony_configured(env) -> None:
    await _run(env, 1, None, SimpleNamespace(workspace_resolver_url=None))
    assert "not configured" in env.replies[-1]


async def test_unreachable(env, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        route, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom))
    )
    await _run(env, 1, None)
    assert "Couldn't reach" in env.replies[-1]
