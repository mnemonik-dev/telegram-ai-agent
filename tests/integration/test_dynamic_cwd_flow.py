"""Integration tests for the DYNAMIC cwd resolution flow.

Tests the full path:
  check_dynamic_cwd_preconditions()
  → dynamic_cwd_lease() context manager
  → engine-spawn mock (send_streaming_response stand-in)

Uses httpx.MockTransport to mock workspace-manager HTTP calls.
No real Telegram or CC processes are involved.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from telegram_bot.core.services.dynamic_cwd import (
    MessageContext,
    build_task_id,
    check_dynamic_cwd_preconditions,
    dynamic_cwd_lease,
)
from telegram_bot.core.services.workspace_resolver import (
    WorkspaceCapacityError,
    WorkspaceConfigError,
    WorkspaceResolver,
    WorkspaceUnreachableError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_resolver(handler: httpx.MockTransport) -> WorkspaceResolver:
    """Build a WorkspaceResolver backed by a mock HTTP transport."""
    client = httpx.AsyncClient(transport=handler)
    return WorkspaceResolver(base_url="http://resolver.test", client=client, backoff_base=0.0)


def _json_response(status: int, body: object) -> httpx.Response:
    content = json.dumps(body).encode()
    return httpx.Response(status, content=content, headers={"content-type": "application/json"})


def _lease_ok(cwd: str = "/workspace/resolved") -> httpx.MockTransport:
    """Transport that returns 200 on POST and 204 on DELETE (happy-path)."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _json_response(200, {"cwd": cwd})
        # DELETE /worktree/{task_id}
        return httpx.Response(204)

    return httpx.MockTransport(_handler)


def _make_message_context(
    chat_id: int = 100,
    message_id: int = 42,
    thread_id: int | None = 10,
) -> MessageContext:
    return MessageContext(chat_id=chat_id, message_id=message_id, thread_id=thread_id)


# ---------------------------------------------------------------------------
# build_task_id
# ---------------------------------------------------------------------------


def test_build_task_id_forum_thread() -> None:
    """Forum thread produces chat-thread-msg format with digits and hyphens."""
    ctx = MessageContext(chat_id=123456789, message_id=99, thread_id=7)
    tid = build_task_id(ctx)
    assert tid == "123456789-7-99"
    assert 3 <= len(tid) <= 40


def test_build_task_id_main_thread_uses_sentinel() -> None:
    """No thread_id produces chat-M-msg format."""
    ctx = MessageContext(chat_id=100, message_id=5, thread_id=None)
    tid = build_task_id(ctx)
    assert tid == "100-M-5"


def test_build_task_id_negative_chat_id_abs() -> None:
    """Negative chat_id (group chats) is normalized with abs()."""
    ctx = MessageContext(chat_id=-1001234567890, message_id=1, thread_id=None)
    tid = build_task_id(ctx)
    assert not tid.startswith("-")
    assert "1001234567890" in tid


def test_build_task_id_max_length_within_bounds() -> None:
    """Worst-case: 13-digit chat, 10-digit thread, 10-digit message → 35 chars ≤ 40."""
    ctx = MessageContext(chat_id=9999999999999, message_id=9999999999, thread_id=9999999999)
    tid = build_task_id(ctx)
    assert len(tid) <= 40


# ---------------------------------------------------------------------------
# check_dynamic_cwd_preconditions
# ---------------------------------------------------------------------------


def test_static_cwd_preconditions_always_pass() -> None:
    """Non-DYNAMIC topics pass precondition checks with any exec_mode and resolver state."""
    check_dynamic_cwd_preconditions(dynamic_cwd=False, exec_mode="tmux", resolver=None)
    check_dynamic_cwd_preconditions(dynamic_cwd=False, exec_mode="subprocess", resolver=None)


def test_dynamic_plus_tmux_raises_config_error() -> None:
    """DYNAMIC + tmux raises WorkspaceConfigError at precondition check."""
    resolver = MagicMock(spec=WorkspaceResolver)
    with pytest.raises(WorkspaceConfigError, match="incompatible with exec_mode:tmux"):
        check_dynamic_cwd_preconditions(dynamic_cwd=True, exec_mode="tmux", resolver=resolver)


def test_dynamic_without_resolver_raises_config_error() -> None:
    """DYNAMIC with no resolver URL raises WorkspaceConfigError."""
    with pytest.raises(WorkspaceConfigError, match="TELEGRAM_AI_AGENT_CWD_RESOLVER_URL"):
        check_dynamic_cwd_preconditions(dynamic_cwd=True, exec_mode="subprocess", resolver=None)


# ---------------------------------------------------------------------------
# test_static_cwd_topic_does_not_call_resolver
# ---------------------------------------------------------------------------


async def test_static_cwd_topic_does_not_call_resolver() -> None:
    """Back-compat: static cwd topic yields static_cwd without calling resolver."""
    call_count = 0

    def _counting(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _json_response(200, {"cwd": "/should-not-be-called"})

    resolver = _make_resolver(httpx.MockTransport(_counting))
    static = Path("/my/static/path")
    msg_ctx = _make_message_context()

    async with dynamic_cwd_lease(
        resolver=resolver,
        dynamic_cwd=False,
        static_cwd=static,
        message_context=msg_ctx,
        repo="org/repo",
        topic_name="my-topic",
    ) as cwd:
        assert cwd == static

    assert call_count == 0, "Resolver should never be called for static cwd topics"


# ---------------------------------------------------------------------------
# test_dynamic_cwd_topic_calls_lease_then_release
# ---------------------------------------------------------------------------


async def test_dynamic_cwd_topic_calls_lease_then_release() -> None:
    """Happy path: lease called with correct task_id, yields resolved path, release called."""
    post_bodies: list[dict] = []
    delete_paths: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            post_bodies.append(json.loads(request.content))
            return _json_response(200, {"cwd": "/workspace/task-42"})
        delete_paths.append(str(request.url.path))
        return httpx.Response(204)

    resolver = _make_resolver(httpx.MockTransport(_handler))
    msg_ctx = _make_message_context(chat_id=100, message_id=42, thread_id=10)
    expected_task_id = build_task_id(msg_ctx)

    yielded_cwd: Path | None = None
    async with dynamic_cwd_lease(
        resolver=resolver,
        dynamic_cwd=True,
        static_cwd=Path("/fallback"),
        message_context=msg_ctx,
        repo="org/myrepo",
        topic_name="coding",
    ) as cwd:
        yielded_cwd = cwd

    assert yielded_cwd == Path("/workspace/task-42")
    assert len(post_bodies) == 1
    assert post_bodies[0]["task_id"] == expected_task_id
    assert post_bodies[0]["repo"] == "org/myrepo"
    assert post_bodies[0]["topic"] == "coding"
    assert len(delete_paths) == 1
    assert expected_task_id in delete_paths[0]


# ---------------------------------------------------------------------------
# test_dynamic_cwd_409_posts_capacity_message_no_engine_spawn
# ---------------------------------------------------------------------------


async def test_dynamic_cwd_409_raises_capacity_error() -> None:
    """409 from resolver propagates as WorkspaceCapacityError before engine spawn."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return _json_response(409, {"error": "capacity"})

    resolver = _make_resolver(httpx.MockTransport(_handler))
    engine_called = False

    with pytest.raises(WorkspaceCapacityError):
        async with dynamic_cwd_lease(
            resolver=resolver,
            dynamic_cwd=True,
            static_cwd=Path("/fallback"),
            message_context=_make_message_context(),
            repo="org/repo",
            topic_name="coding",
        ) as _cwd:
            engine_called = True  # must not reach here

    assert not engine_called


# ---------------------------------------------------------------------------
# test_dynamic_cwd_5xx_exhausted_posts_unreachable_message
# ---------------------------------------------------------------------------


async def test_dynamic_cwd_5xx_exhausted_raises_unreachable() -> None:
    """5xx retries exhausted → WorkspaceUnreachableError before engine spawn."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return _json_response(500, {"error": "internal"})

    resolver = _make_resolver(httpx.MockTransport(_handler))
    # Single retry to keep the test fast
    resolver._retries = 1

    engine_called = False

    with pytest.raises(WorkspaceUnreachableError):
        async with dynamic_cwd_lease(
            resolver=resolver,
            dynamic_cwd=True,
            static_cwd=Path("/fallback"),
            message_context=_make_message_context(),
            repo="org/repo",
            topic_name="coding",
        ) as _cwd:
            engine_called = True

    assert not engine_called


# ---------------------------------------------------------------------------
# test_dynamic_cwd_without_resolver_url_raises_at_startup
# ---------------------------------------------------------------------------


async def test_dynamic_cwd_without_resolver_raises_config_error() -> None:
    """DYNAMIC + resolver=None raises WorkspaceConfigError (fail-fast, engine not spawned)."""
    engine_called = False

    with pytest.raises(WorkspaceConfigError, match="TELEGRAM_AI_AGENT_CWD_RESOLVER_URL"):
        async with dynamic_cwd_lease(
            resolver=None,
            dynamic_cwd=True,
            static_cwd=Path("/fallback"),
            message_context=_make_message_context(),
            repo="default",
            topic_name="coding",
        ) as _cwd:
            engine_called = True

    assert not engine_called


# ---------------------------------------------------------------------------
# test_dynamic_cwd_plus_tmux_raises_clear_error
# ---------------------------------------------------------------------------


def test_dynamic_cwd_plus_tmux_raises_clear_error() -> None:
    """DYNAMIC + tmux exec_mode raises WorkspaceConfigError with clear message."""
    resolver = _make_resolver(_lease_ok())
    with pytest.raises(WorkspaceConfigError) as exc_info:
        check_dynamic_cwd_preconditions(dynamic_cwd=True, exec_mode="tmux", resolver=resolver)
    assert "tmux" in str(exc_info.value)
    assert "subprocess" in str(exc_info.value)


# ---------------------------------------------------------------------------
# test_release_called_even_on_engine_crash
# ---------------------------------------------------------------------------


async def test_release_called_even_on_engine_crash() -> None:
    """Release is called in finally even when the engine raises an exception."""
    release_calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _json_response(200, {"cwd": "/workspace/crash-test"})
        release_calls.append(str(request.url.path))
        return httpx.Response(204)

    resolver = _make_resolver(httpx.MockTransport(_handler))
    msg_ctx = _make_message_context()
    expected_task_id = build_task_id(msg_ctx)

    with pytest.raises(RuntimeError, match="engine exploded"):
        async with dynamic_cwd_lease(
            resolver=resolver,
            dynamic_cwd=True,
            static_cwd=Path("/fallback"),
            message_context=msg_ctx,
            repo="default",
            topic_name="test",
        ) as _cwd:
            raise RuntimeError("engine exploded")

    # Despite engine crash, release must have been called
    assert len(release_calls) == 1
    assert expected_task_id in release_calls[0]


# ---------------------------------------------------------------------------
# test_release_failure_logged_but_does_not_mask_main_error
# ---------------------------------------------------------------------------


async def test_release_failure_logged_does_not_mask_engine_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Release 5xx failure is logged as warning; original engine error propagates."""
    call_num = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_num
        call_num += 1
        if request.method == "POST":
            return _json_response(200, {"cwd": "/workspace/dual-fail"})
        # DELETE always fails with 500
        return _json_response(500, {"error": "release server error"})

    # Use retries=1 so release fails fast
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    resolver = WorkspaceResolver(
        base_url="http://resolver.test",
        client=client,
        backoff_base=0.0,
        retries=1,
    )

    with (
        caplog.at_level(logging.WARNING, logger="telegram_bot.core.services.dynamic_cwd"),
        pytest.raises(RuntimeError, match="primary engine error"),
    ):
        async with dynamic_cwd_lease(
            resolver=resolver,
            dynamic_cwd=True,
            static_cwd=Path("/fallback"),
            message_context=_make_message_context(),
            repo="default",
            topic_name="test",
        ) as _cwd:
            raise RuntimeError("primary engine error")

    # The original engine error must propagate, not a WorkspaceUnreachableError
    # Release failure must be logged as a warning, not raised
    release_warning = any("workspace release failed" in r.message for r in caplog.records)
    assert release_warning, "Expected a warning log about release failure"
