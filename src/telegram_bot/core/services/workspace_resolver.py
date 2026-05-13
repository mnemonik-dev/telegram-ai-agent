"""External HTTP resolver for per-task cwd. Generic, not Mnemonic-specific.

Contract (matches coding-fabric workspace-manager §2.3):
  POST   /worktree              body: LeaseRequest  → 200 LeaseResponse | 409
  DELETE /worktree/{task_id}                        → 204 | 404 (idempotent)

Retry policy: 5xx / network errors → 3 attempts, 1 s / 2 s / 4 s backoff.
4xx (other than 409) → fail-fast, no retry.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WorkspaceResolverError(Exception):
    """Base class for resolver errors."""


class WorkspaceCapacityError(WorkspaceResolverError):
    """Resolver returned 409 — pool full. Recoverable: try again later."""


class WorkspaceUnreachableError(WorkspaceResolverError):
    """Resolver unavailable (5xx exhausted retries or timeout)."""


class WorkspaceConfigError(WorkspaceResolverError):
    """Configuration error (e.g., resolver URL missing while DYNAMIC requested)."""


class WorkspaceBadRequestError(WorkspaceResolverError):
    """Resolver returned 4xx (other than 409) — fail-fast."""


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LeaseRequest(BaseModel):
    """Validated request body for POST /worktree."""

    task_id: str = Field(..., min_length=3, max_length=40, pattern=r"^[A-Z0-9\-]{3,40}$")
    repo: str = Field(..., max_length=128)
    base_ref: str = Field("main", max_length=64)
    topic: str = Field(..., max_length=64)


class LeaseResponse(BaseModel):
    """Validated response body from POST /worktree."""

    cwd: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class WorkspaceResolver:
    """Async HTTP client for an external workspace resolver.

    Implements lease/release against the HTTP contract defined in §2.3 of the
    mnemonic-tg-bridge tech spec.  The client is generic; it carries no
    Mnemonic-specific logic.

    Parameters
    ----------
    base_url:
        Base URL of the resolver, e.g. ``http://127.0.0.1:8765``.  Trailing
        slashes are stripped.
    timeout_s:
        Per-request timeout in seconds.  Applies to the entire request cycle
        (connect + read).
    retries:
        Number of total attempts (not *extra* retries) for 5xx / network errors.
        Must be >= 1.
    backoff_base:
        Base for exponential backoff in seconds.  Attempt *i* (0-indexed) sleeps
        ``backoff_base * 2**i`` before the *next* attempt.
    client:
        Optional pre-built ``httpx.AsyncClient``.  When provided the caller owns
        its lifecycle; when ``None`` a fresh client is created per-request.
        Inject a ``MockTransport``-backed client in tests.
    """

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 5.0,
        retries: int = 3,
        backoff_base: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._retries = max(1, retries)
        self._backoff_base = backoff_base
        self._client = client  # injectable for tests (e.g., MockTransport)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def lease(self, request: LeaseRequest) -> Path:
        """POST /worktree and return the resolved working-directory path.

        Retries on 5xx and network errors.  Raises immediately on 4xx.

        Parameters
        ----------
        request:
            Fully-validated ``LeaseRequest`` (Pydantic enforces task_id regex).

        Returns
        -------
        Path
            Absolute path of the allocated workspace.

        Raises
        ------
        WorkspaceCapacityError
            Resolver returned HTTP 409 (pool exhausted).
        WorkspaceBadRequestError
            Resolver returned any other 4xx (bad input, fail-fast).
        WorkspaceUnreachableError
            All retry attempts exhausted (5xx or timeout).
        """
        url = f"{self._base_url}/worktree"
        payload = request.model_dump()
        last_exc: Exception | None = None

        for attempt in range(self._retries):
            if attempt > 0:
                delay = self._backoff_base * (2 ** (attempt - 1))
                logger.info(
                    "workspace_resolver: lease retry attempt %d/%d for task_id=%s (backoff %.1fs)",
                    attempt + 1,
                    self._retries,
                    request.task_id,
                    delay,
                )
                await asyncio.sleep(delay)

            try:
                response = await self._post(url, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.debug(
                    "workspace_resolver: lease network/timeout error (attempt %d/%d): %s",
                    attempt + 1,
                    self._retries,
                    type(exc).__name__,
                )
                last_exc = exc
                continue

            logger.debug(
                "workspace_resolver: lease response status=%d (attempt %d/%d)",
                response.status_code,
                attempt + 1,
                self._retries,
            )

            if response.status_code == 200:
                lease_response = LeaseResponse.model_validate(response.json())
                cwd = lease_response.cwd
                logger.info(
                    "workspace_resolver: lease success task_id=%s cwd=%s",
                    request.task_id,
                    cwd,
                )
                return Path(cwd)

            if response.status_code == 409:
                logger.info(
                    "workspace_resolver: capacity exhausted (409) task_id=%s",
                    request.task_id,
                )
                raise WorkspaceCapacityError(
                    f"Workspace pool exhausted (HTTP 409) for task_id={request.task_id}"
                )

            if 400 <= response.status_code < 500:
                logger.warning(
                    "workspace_resolver: bad request (HTTP %d) task_id=%s — fail-fast",
                    response.status_code,
                    request.task_id,
                )
                raise WorkspaceBadRequestError(
                    f"Resolver returned HTTP {response.status_code} for task_id={request.task_id}"
                )

            # 5xx — record and retry
            logger.debug(
                "workspace_resolver: server error HTTP %d (attempt %d/%d) task_id=%s",
                response.status_code,
                attempt + 1,
                self._retries,
                request.task_id,
            )
            last_exc = httpx.HTTPStatusError(
                f"HTTP {response.status_code}",
                request=response.request,
                response=response,
            )

        logger.error(
            "workspace_resolver: lease failed after %d attempts task_id=%s",
            self._retries,
            request.task_id,
        )
        raise WorkspaceUnreachableError(
            f"Workspace resolver unreachable after {self._retries} attempts "
            f"for task_id={request.task_id}"
        ) from last_exc

    async def release(self, task_id: str) -> None:
        """DELETE /worktree/{task_id} — idempotent (404 is treated as success).

        Retries on 5xx and network errors.

        Parameters
        ----------
        task_id:
            Identifier of the workspace to release.

        Raises
        ------
        WorkspaceUnreachableError
            All retry attempts exhausted (5xx or timeout).
        """
        url = f"{self._base_url}/worktree/{task_id}"
        last_exc: Exception | None = None

        for attempt in range(self._retries):
            if attempt > 0:
                delay = self._backoff_base * (2 ** (attempt - 1))
                logger.info(
                    "workspace_resolver: release retry attempt %d/%d task_id=%s (backoff %.1fs)",
                    attempt + 1,
                    self._retries,
                    task_id,
                    delay,
                )
                await asyncio.sleep(delay)

            try:
                response = await self._delete(url)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.debug(
                    "workspace_resolver: release network/timeout error (attempt %d/%d): %s",
                    attempt + 1,
                    self._retries,
                    type(exc).__name__,
                )
                last_exc = exc
                continue

            logger.debug(
                "workspace_resolver: release response status=%d (attempt %d/%d)",
                response.status_code,
                attempt + 1,
                self._retries,
            )

            if response.status_code in (204, 404):
                logger.info(
                    "workspace_resolver: release success task_id=%s (HTTP %d)",
                    task_id,
                    response.status_code,
                )
                return

            if 400 <= response.status_code < 500:
                # Non-idempotent 4xx on DELETE — fail-fast, same as lease
                logger.warning(
                    "workspace_resolver: release bad request (HTTP %d) task_id=%s — fail-fast",
                    response.status_code,
                    task_id,
                )
                raise WorkspaceBadRequestError(
                    f"Resolver returned HTTP {response.status_code} on release "
                    f"for task_id={task_id}"
                )

            # 5xx — record and retry
            logger.debug(
                "workspace_resolver: release server error HTTP %d (attempt %d/%d) task_id=%s",
                response.status_code,
                attempt + 1,
                self._retries,
                task_id,
            )
            last_exc = httpx.HTTPStatusError(
                f"HTTP {response.status_code}",
                request=response.request,
                response=response,
            )

        logger.error(
            "workspace_resolver: release failed after %d attempts task_id=%s",
            self._retries,
            task_id,
        )
        raise WorkspaceUnreachableError(
            f"Workspace resolver unreachable after {self._retries} attempts "
            f"on release for task_id={task_id}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _post(self, url: str, json: dict[str, Any]) -> httpx.Response:
        """Execute a POST request, reusing an injected client or creating a fresh one."""
        if self._client is not None:
            return await self._client.post(url, json=json)
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_s)) as client:
            return await client.post(url, json=json)

    async def _delete(self, url: str) -> httpx.Response:
        """Execute a DELETE request, reusing an injected client or creating a fresh one."""
        if self._client is not None:
            return await self._client.delete(url)
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_s)) as client:
            return await client.delete(url)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def resolver_from_settings(settings: Any) -> WorkspaceResolver | None:
    """Build a ``WorkspaceResolver`` from Settings if a URL is configured; else ``None``.

    Parameters
    ----------
    settings:
        A ``Settings`` instance (or any object exposing ``workspace_resolver_url``).
        Accepts ``Any`` to avoid a circular import dependency on the config module.

    Returns
    -------
    WorkspaceResolver | None
        A configured resolver, or ``None`` when
        ``settings.workspace_resolver_url`` is absent or empty.
    """
    url: str | None = getattr(settings, "workspace_resolver_url", None)
    if not url:
        return None
    return WorkspaceResolver(base_url=url)
