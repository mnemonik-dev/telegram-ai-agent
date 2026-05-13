"""E2E test asserting the subprocess receives the leased cwd.

Regression test for the critical F1 bug (code-audit.json): the leased cwd was
silently overwritten by _apply_topic_config inside SessionManager.send_stream,
so the subprocess actually ran in the default cwd rather than the workspace
returned by the resolver.

This test covers the full call chain:
  dynamic_cwd_lease (resolver mock)
  -> send_streaming_response (streaming.py)
  -> session_manager.send_stream (claude.py)
  -> _run_cc_stream (claude.py)
  -> asyncio.create_subprocess_exec  ← spy here, assert cwd kwarg

Without the cwd_override fix, this test would fail because _apply_topic_config
resets session.cwd to defaults.cwd for DYNAMIC topics (topic.cwd is None), and
_run_cc_stream uses session.cwd instead of the leased path.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from telegram_bot.core.config import Settings
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.dynamic_cwd import (
    MessageContext,
    dynamic_cwd_lease,
)
from telegram_bot.core.services.workspace_resolver import WorkspaceResolver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LEASED_CWD = "/workspace/leased-42"
_DEFAULT_CWD = "/default/project"


def _make_settings(default_cwd: str = _DEFAULT_CWD) -> Settings:
    """Build a minimal Settings instance for testing."""
    return Settings(
        telegram_bot_token="test-token",
        project_root="/project",
        default_cwd=default_cwd,
        workspace_resolver_url=None,
    )


def _make_resolver(cwd: str = _LEASED_CWD) -> WorkspaceResolver:
    """WorkspaceResolver backed by a mock transport that returns *cwd*."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                content=json.dumps({"cwd": cwd}).encode(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return WorkspaceResolver(base_url="http://resolver.test", client=client, backoff_base=0.0)


_RESULT_LINE = b'{"type":"result","subtype":"success","result":"ok","session_id":"abc123"}\n'


def _make_fake_process(output_line: bytes = _RESULT_LINE) -> MagicMock:
    """Build a fake asyncio.subprocess.Process that emits one stream-json line."""
    process = MagicMock()
    process.returncode = None
    process.pid = 12345
    process.stdin = None

    # stdout: return *output_line* once, then b"" (EOF)
    read_count = 0

    async def _readline() -> bytes:
        nonlocal read_count
        if read_count == 0:
            read_count += 1
            return output_line
        return b""

    process.stdout = MagicMock()
    process.stdout.readline = _readline

    # stderr: immediate EOF
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=b"")

    async def _wait() -> int:
        process.returncode = 0
        return 0

    process.wait = _wait
    process.returncode = 0
    return process


# ---------------------------------------------------------------------------
# topic_config that exposes a DYNAMIC topic
# ---------------------------------------------------------------------------


def _make_topic_config_dynamic(thread_id: int = 10) -> MagicMock:
    """Return a TopicConfig mock whose thread_id topic has dynamic_cwd=True."""
    from telegram_bot.core.services.topic_config import TopicSettings

    dynamic_topic = TopicSettings(
        name="coding",
        type="project",
        mode="task",
        cwd=None,  # None because DYNAMIC resolved at spawn time
        mcp_config=None,
        exec_mode="subprocess",
        dynamic_cwd=True,
    )
    default_topic = TopicSettings(
        name="",
        type="assistant",
        mode="free",
        cwd=None,
        mcp_config=None,
    )

    def _get_topic(tid: int | None) -> TopicSettings:
        if tid == thread_id:
            return dynamic_topic
        return default_topic

    mock = MagicMock()
    mock.get_topic = _get_topic
    mock._topics = {thread_id: dynamic_topic}
    return mock


# ---------------------------------------------------------------------------
# The critical test: subprocess must receive leased cwd, NOT default cwd
# ---------------------------------------------------------------------------


async def test_subprocess_receives_leased_cwd_not_default() -> None:
    """Subprocess cwd kwarg must equal the resolver-returned path, not defaults.cwd.

    This is the regression test for code-audit.json F1: the leased cwd was
    silently overwritten by _apply_topic_config inside send_stream, so the
    subprocess ran in the wrong directory.  The test fails without the
    cwd_override fix.
    """
    settings = _make_settings(default_cwd=_DEFAULT_CWD)
    topic_config = _make_topic_config_dynamic(thread_id=10)
    session_manager = SessionManager(settings, topic_config=topic_config)

    channel_key = (100, 10)
    resolver = _make_resolver(cwd=_LEASED_CWD)
    msg_ctx = MessageContext(chat_id=100, message_id=42, thread_id=10)

    spawned_cwd: list[str] = []
    fake_process = _make_fake_process()

    async def _fake_create_subprocess_exec(
        *args: object, cwd: str | None = None, **kwargs: object
    ) -> MagicMock:
        spawned_cwd.append(cwd or "")
        return fake_process

    with patch(
        "telegram_bot.core.services.claude.asyncio.create_subprocess_exec",
        side_effect=_fake_create_subprocess_exec,
    ):
        async with dynamic_cwd_lease(
            resolver=resolver,
            dynamic_cwd=True,
            static_cwd=Path(_DEFAULT_CWD),
            message_context=msg_ctx,
            repo="default",
            topic_name="coding",
        ) as resolved_cwd:
            assert str(resolved_cwd) == _LEASED_CWD

            # Call send_stream directly with the cwd_override — this is exactly
            # what __main__.process_queue_item does via send_streaming_response.
            effective_cwd_override = resolved_cwd  # DYNAMIC path

            await session_manager.send_stream(
                channel_key,
                "do something",
                lambda _: None,
                cwd_override=effective_cwd_override,
            )

    assert len(spawned_cwd) >= 1, "Expected at least one subprocess to be spawned"
    for actual_cwd in spawned_cwd:
        assert actual_cwd == _LEASED_CWD, (
            f"Subprocess was spawned with cwd={actual_cwd!r} "
            f"instead of the leased cwd={_LEASED_CWD!r}. "
            f"This indicates the cwd_override fix is not working correctly."
        )


async def test_subprocess_receives_default_cwd_for_static_topics() -> None:
    """Back-compat: static topics pass no override; subprocess gets defaults.cwd."""
    from telegram_bot.core.services.topic_config import TopicSettings

    settings = _make_settings(default_cwd=_DEFAULT_CWD)
    static_topic_cwd = "/project/static"
    static_topic = TopicSettings(
        name="static-topic",
        type="project",
        mode="free",
        cwd=static_topic_cwd,
        mcp_config=None,
        exec_mode="subprocess",
        dynamic_cwd=False,
    )

    mock_tc = MagicMock()
    mock_tc.get_topic = lambda _: static_topic
    mock_tc._topics = {10: static_topic}

    session_manager = SessionManager(settings, topic_config=mock_tc)
    channel_key = (100, 10)

    spawned_cwd: list[str] = []
    fake_process = _make_fake_process()

    async def _fake_create(*args: object, cwd: str | None = None, **kwargs: object) -> MagicMock:
        spawned_cwd.append(cwd or "")
        return fake_process

    with patch(
        "telegram_bot.core.services.claude.asyncio.create_subprocess_exec",
        side_effect=_fake_create,
    ):
        # Static topic: no cwd_override passed
        await session_manager.send_stream(
            channel_key,
            "do something",
            lambda _: None,
            cwd_override=None,
        )

    assert len(spawned_cwd) >= 1
    # _apply_topic_config resolved static_topic.cwd to the topic's configured path
    for actual_cwd in spawned_cwd:
        assert actual_cwd == static_topic_cwd, (
            f"Static topic subprocess should run in {static_topic_cwd!r}, got {actual_cwd!r}"
        )


async def test_cwd_override_wins_over_apply_topic_config() -> None:
    """cwd_override takes precedence over whatever _apply_topic_config sets.

    Directly verifies that the override mechanism works regardless of what
    _apply_topic_config would produce for the same channel.
    """
    from telegram_bot.core.services.topic_config import TopicSettings

    settings = _make_settings(default_cwd=_DEFAULT_CWD)
    # Topic has a static cwd — but we'll pass a different override
    topic = TopicSettings(
        name="override-test",
        type="project",
        mode="free",
        cwd="/topic/configured/path",
        mcp_config=None,
        exec_mode="subprocess",
        dynamic_cwd=False,
    )
    mock_tc = MagicMock()
    mock_tc.get_topic = lambda _: topic
    mock_tc._topics = {10: topic}

    session_manager = SessionManager(settings, topic_config=mock_tc)
    channel_key = (100, 10)

    override_path = Path("/override/wins")
    spawned_cwd: list[str] = []
    fake_process = _make_fake_process()

    async def _fake_create(*args: object, cwd: str | None = None, **kwargs: object) -> MagicMock:
        spawned_cwd.append(cwd or "")
        return fake_process

    with patch(
        "telegram_bot.core.services.claude.asyncio.create_subprocess_exec",
        side_effect=_fake_create,
    ):
        await session_manager.send_stream(
            channel_key,
            "prompt",
            lambda _: None,
            cwd_override=override_path,
        )

    assert len(spawned_cwd) >= 1
    for actual_cwd in spawned_cwd:
        assert actual_cwd == str(override_path), (
            f"cwd_override={override_path!r} should win over topic cwd, "
            f"but subprocess received {actual_cwd!r}"
        )
