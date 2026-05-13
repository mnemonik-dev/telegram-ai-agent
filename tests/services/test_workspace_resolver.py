"""Unit tests for workspace_resolver.py.

Uses httpx.MockTransport (built into httpx, no extra deps) since respx is not
present in the project's dev-dependencies.  MockTransport accepts a handler
callable ``(request) -> httpx.Response``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import ValidationError

from telegram_bot.core.services.workspace_resolver import (
    LeaseRequest,
    WorkspaceBadRequestError,
    WorkspaceCapacityError,
    WorkspaceResolver,
    WorkspaceResolverError,
    WorkspaceUnreachableError,
    resolver_from_settings,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_resolver(
    handler: httpx.MockTransport | None = None, **kwargs: object
) -> WorkspaceResolver:
    """Create a WorkspaceResolver with an injected mock transport."""
    client = httpx.AsyncClient(transport=handler) if handler is not None else None
    return WorkspaceResolver(base_url="http://resolver.test", client=client, **kwargs)  # type: ignore[arg-type]


def _mock_transport(*responses: httpx.Response) -> httpx.MockTransport:
    """Return a MockTransport that replays *responses* in sequence."""
    responses_iter = iter(responses)

    def _handler(request: httpx.Request) -> httpx.Response:
        return next(responses_iter)

    return httpx.MockTransport(_handler)


def _json_response(status: int, body: object) -> httpx.Response:
    import json

    content = json.dumps(body).encode()
    return httpx.Response(status, content=content, headers={"content-type": "application/json"})


def _good_lease_request() -> LeaseRequest:
    return LeaseRequest(task_id="CHAT-1-MSG-42", repo="org/repo", topic="coding")


# ---------------------------------------------------------------------------
# test_lease_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_success() -> None:
    """POST 200 with valid cwd returns a Path."""
    transport = _mock_transport(_json_response(200, {"cwd": "/workspace/task-42"}))
    resolver = _make_resolver(transport)

    result = await resolver.lease(_good_lease_request())

    assert isinstance(result, Path)
    assert result == Path("/workspace/task-42")


# ---------------------------------------------------------------------------
# test_lease_409_raises_capacity_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_409_raises_capacity_error() -> None:
    """POST 409 raises WorkspaceCapacityError immediately (no retry)."""
    transport = _mock_transport(_json_response(409, {"error": "capacity"}))
    resolver = _make_resolver(transport)

    with pytest.raises(WorkspaceCapacityError):
        await resolver.lease(_good_lease_request())


# ---------------------------------------------------------------------------
# test_lease_5xx_retries_then_fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_5xx_retries_then_fails() -> None:
    """Three consecutive 500s → WorkspaceUnreachableError after all retries."""
    transport = _mock_transport(
        _json_response(500, {"error": "internal"}),
        _json_response(500, {"error": "internal"}),
        _json_response(500, {"error": "internal"}),
    )
    resolver = _make_resolver(transport, retries=3, backoff_base=0.0)

    with pytest.raises(WorkspaceUnreachableError):
        await resolver.lease(_good_lease_request())


# ---------------------------------------------------------------------------
# test_lease_5xx_retries_then_succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_5xx_retries_then_succeeds() -> None:
    """Two 500s then 200 — succeeds on third attempt, returns Path."""
    transport = _mock_transport(
        _json_response(500, {"error": "internal"}),
        _json_response(500, {"error": "internal"}),
        _json_response(200, {"cwd": "/workspace/recovered"}),
    )
    resolver = _make_resolver(transport, retries=3, backoff_base=0.0)

    result = await resolver.lease(_good_lease_request())

    assert result == Path("/workspace/recovered")


# ---------------------------------------------------------------------------
# test_lease_timeout_retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_timeout_retries() -> None:
    """Timeout errors are retried; exhausting retries raises WorkspaceUnreachableError."""
    call_count = 0

    def _timing_out(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.TimeoutException("timed out", request=request)

    transport = httpx.MockTransport(_timing_out)
    resolver = _make_resolver(transport, retries=3, backoff_base=0.0)

    with pytest.raises(WorkspaceUnreachableError):
        await resolver.lease(_good_lease_request())

    assert call_count == 3


# ---------------------------------------------------------------------------
# test_lease_400_no_retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_400_no_retry() -> None:
    """HTTP 400 raises WorkspaceBadRequestError immediately without retrying."""
    call_count = 0

    def _bad_request(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _json_response(400, {"error": "bad task_id"})

    transport = httpx.MockTransport(_bad_request)
    resolver = _make_resolver(transport, retries=3, backoff_base=0.0)

    with pytest.raises(WorkspaceBadRequestError):
        await resolver.lease(_good_lease_request())

    assert call_count == 1, "400 must not trigger retries"


# ---------------------------------------------------------------------------
# test_release_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_success() -> None:
    """DELETE 204 completes without raising."""
    transport = _mock_transport(httpx.Response(204))
    resolver = _make_resolver(transport)

    await resolver.release("CHAT-1-MSG-42")  # must not raise


# ---------------------------------------------------------------------------
# test_release_404_idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_404_idempotent() -> None:
    """DELETE 404 is treated as success (idempotent), no exception raised."""
    transport = _mock_transport(httpx.Response(404))
    resolver = _make_resolver(transport)

    await resolver.release("CHAT-1-MSG-42")  # must not raise


# ---------------------------------------------------------------------------
# test_release_5xx_retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_5xx_retries() -> None:
    """Three consecutive 500s on DELETE → WorkspaceUnreachableError after retries."""
    transport = _mock_transport(
        _json_response(500, {"error": "server error"}),
        _json_response(500, {"error": "server error"}),
        _json_response(500, {"error": "server error"}),
    )
    resolver = _make_resolver(transport, retries=3, backoff_base=0.0)

    with pytest.raises(WorkspaceUnreachableError):
        await resolver.release("CHAT-1-MSG-42")


# ---------------------------------------------------------------------------
# test_invalid_task_id_rejected
# ---------------------------------------------------------------------------


def test_invalid_task_id_rejected() -> None:
    """task_id with lowercase letters or special chars fails Pydantic validation."""
    with pytest.raises(ValidationError):
        LeaseRequest(task_id="chat-lowercase", repo="org/repo", topic="coding")

    with pytest.raises(ValidationError):
        LeaseRequest(task_id="CHAT@42!", repo="org/repo", topic="coding")

    with pytest.raises(ValidationError):
        # Too short
        LeaseRequest(task_id="AB", repo="org/repo", topic="coding")


# ---------------------------------------------------------------------------
# test_url_trailing_slash_normalised
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_url_trailing_slash_normalised() -> None:
    """base_url with trailing slash produces the same endpoint as one without."""
    captured_urls: list[str] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return _json_response(200, {"cwd": "/workspace/x"})

    transport = httpx.MockTransport(_capture)

    # With trailing slash
    client_with = httpx.AsyncClient(transport=transport)
    resolver_with = WorkspaceResolver(
        base_url="http://resolver.test/", client=client_with, backoff_base=0.0
    )
    await resolver_with.lease(_good_lease_request())

    # Without trailing slash
    client_without = httpx.AsyncClient(transport=transport)
    resolver_without = WorkspaceResolver(
        base_url="http://resolver.test", client=client_without, backoff_base=0.0
    )
    await resolver_without.lease(_good_lease_request())

    assert captured_urls[0] == captured_urls[1] == "http://resolver.test/worktree"


# ---------------------------------------------------------------------------
# test_resolver_from_settings_returns_none_when_url_missing
# ---------------------------------------------------------------------------


def test_resolver_from_settings_returns_none_when_url_missing() -> None:
    """resolver_from_settings returns None when workspace_resolver_url is unset."""
    settings = MagicMock()
    settings.workspace_resolver_url = None

    result = resolver_from_settings(settings)

    assert result is None


def test_resolver_from_settings_returns_resolver_when_url_set() -> None:
    """resolver_from_settings returns a WorkspaceResolver when URL is configured."""
    settings = MagicMock()
    settings.workspace_resolver_url = "http://127.0.0.1:8765"

    result = resolver_from_settings(settings)

    assert isinstance(result, WorkspaceResolver)


# ---------------------------------------------------------------------------
# test_logging_does_not_include_url_with_secrets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logging_does_not_include_url_with_secrets(caplog: pytest.LogCaptureFixture) -> None:
    """Log records never contain the resolver URL (defensive: treat URL as opaque).

    We use a synthetic URL that looks like it embeds a token.  Even though our
    resolver is loopback-only (no auth), the logging hygiene must hold regardless.
    """
    secret_url = "http://token-abc123secret@resolver.internal"
    call_count = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _json_response(200, {"cwd": "/workspace/secure"})

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport)
    resolver = WorkspaceResolver(
        base_url=secret_url,
        client=client,
        backoff_base=0.0,
    )

    with caplog.at_level(logging.DEBUG, logger="telegram_bot.core.services.workspace_resolver"):
        await resolver.lease(_good_lease_request())

    secret_fragment = "token-abc123secret"
    for record in caplog.records:
        assert secret_fragment not in record.getMessage(), (
            f"Secret URL fragment found in log record: {record.getMessage()!r}"
        )


# ---------------------------------------------------------------------------
# WorkspaceResolverError is base of all typed exceptions
# ---------------------------------------------------------------------------


def test_exception_hierarchy() -> None:
    """All typed exceptions inherit from WorkspaceResolverError."""
    assert issubclass(WorkspaceCapacityError, WorkspaceResolverError)
    assert issubclass(WorkspaceUnreachableError, WorkspaceResolverError)
    assert issubclass(WorkspaceBadRequestError, WorkspaceResolverError)
