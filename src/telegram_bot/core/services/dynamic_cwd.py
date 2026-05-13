"""Per-message dynamic cwd resolution via external workspace resolver.

Provides a context manager that leases a workspace cwd for the duration of
a single engine invocation and guarantees release in a finally block — covering
normal exit, /cancel, timeout, and crash paths.

Usage::

    async with dynamic_cwd_lease(
        resolver=resolver,
        runtime_config=runtime_config,
        message_context=msg_ctx,
        session_manager=session_manager,
        channel_key=key,
    ) as cwd:
        await send_streaming_response(...)

When ``runtime_config.dynamic_cwd`` is False the context manager is a no-op
that yields the static ``runtime_config.cwd`` — existing topics are unaffected.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from telegram_bot.core.services.workspace_resolver import (
    LeaseRequest,
    WorkspaceConfigError,
    WorkspaceResolver,
)

# Re-exported so callers can catch workspace errors from a single import site.
from telegram_bot.core.services.workspace_resolver import (
    WorkspaceCapacityError as WorkspaceCapacityError,
)
from telegram_bot.core.services.workspace_resolver import (
    WorkspaceUnreachableError as WorkspaceUnreachableError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageContext:
    """Minimal per-message identifiers needed to derive a unique task_id."""

    chat_id: int
    message_id: int
    thread_id: int | None = None


def build_task_id(ctx: MessageContext) -> str:
    """Derive a deterministic task_id from message context.

    Produces strings matching ``^[A-Z0-9-]{3,40}$`` (digits + hyphens).
    chat_id ≤ 13 digits, thread_id ≤ 10 digits, message_id ≤ 10 digits →
    worst-case length is 13+1+10+1+10 = 35 ≤ 40.

    Parameters
    ----------
    ctx:
        Frozen message context with chat_id, message_id, and optional thread_id.

    Returns
    -------
    str
        A unique task_id suitable for ``LeaseRequest.task_id``.

    Raises
    ------
    ValueError
        When the derived id falls outside the allowed [3, 40] char range.
    """
    chat = str(abs(ctx.chat_id))
    # 'M' for main (non-forum) thread so the id stays all-uppercase-safe
    thread = str(ctx.thread_id) if ctx.thread_id is not None else "M"
    msg = str(ctx.message_id)
    raw = f"{chat}-{thread}-{msg}"
    if not (3 <= len(raw) <= 40):
        raise ValueError(f"task_id length out of range: {len(raw)!r} for id {raw!r}")
    return raw


@asynccontextmanager
async def dynamic_cwd_lease(
    resolver: WorkspaceResolver | None,
    dynamic_cwd: bool,
    static_cwd: Path,
    message_context: MessageContext,
    repo: str,
    topic_name: str,
) -> AsyncIterator[Path]:
    """Async context manager that yields a resolved cwd path.

    When ``dynamic_cwd`` is False, yields ``static_cwd`` immediately (no-op
    for existing topics — zero overhead on the happy path).

    When ``dynamic_cwd`` is True:
    - Validates that a resolver is available and exec_mode compatibility was
      checked by the caller (exec_mode check is done before entering the context
      manager — see ``check_dynamic_cwd_preconditions``).
    - Calls ``resolver.lease(...)`` to allocate a workspace.
    - Yields the resulting ``Path``.
    - Calls ``resolver.release(task_id)`` in a ``finally`` block; release
      failures are logged as warnings and never mask the primary exception.

    Parameters
    ----------
    resolver:
        Configured ``WorkspaceResolver``, or ``None`` when no resolver URL is
        set.  Passing ``None`` while ``dynamic_cwd=True`` raises
        ``WorkspaceConfigError``.
    dynamic_cwd:
        Whether the topic uses ``cwd: DYNAMIC``.
    static_cwd:
        Fallback path used when ``dynamic_cwd=False``.
    message_context:
        Per-message identifiers; used to build the unique task_id.
    repo:
        Repo name sent to the resolver (e.g. ``"org/repo"`` or ``"default"``).
    topic_name:
        Topic name sent to the resolver for tracing/labelling.

    Yields
    ------
    Path
        The working directory to pass to the engine subprocess.

    Raises
    ------
    WorkspaceConfigError
        ``dynamic_cwd=True`` but ``resolver`` is ``None`` (misconfiguration).
    WorkspaceCapacityError
        Resolver returned HTTP 409 — pool exhausted; caller should surface this
        to the user and skip spawning the engine.
    WorkspaceUnreachableError
        All retry attempts exhausted; caller should surface diagnostic and skip
        spawning the engine.
    """
    if not dynamic_cwd:
        yield static_cwd
        return

    if resolver is None:
        raise WorkspaceConfigError(
            "cwd:DYNAMIC requires TELEGRAM_AI_AGENT_CWD_RESOLVER_URL to be configured"
        )

    task_id = build_task_id(message_context)
    request = LeaseRequest(
        task_id=task_id,
        repo=repo,
        base_ref="main",
        topic=topic_name,
    )
    logger.info(
        "dynamic_cwd: leasing workspace task_id=%s repo=%s topic=%s",
        task_id,
        repo,
        topic_name,
    )
    cwd = await resolver.lease(request)
    try:
        yield cwd
    finally:
        try:
            await resolver.release(task_id)
            logger.info("dynamic_cwd: released workspace task_id=%s", task_id)
        except Exception as exc:
            # Release failure must never mask the primary exception from the
            # engine; log and swallow.
            logger.warning(
                "dynamic_cwd: workspace release failed for task_id=%s: %s",
                task_id,
                exc,
                exc_info=True,
            )


def check_dynamic_cwd_preconditions(
    dynamic_cwd: bool,
    exec_mode: str,
    resolver: WorkspaceResolver | None,
) -> None:
    """Fail-fast checks for DYNAMIC cwd before attempting a lease.

    Call this synchronously before entering ``dynamic_cwd_lease`` so that
    configuration errors surface as clear exceptions rather than silent
    no-ops or deferred failures.

    Parameters
    ----------
    dynamic_cwd:
        Whether the topic uses ``cwd: DYNAMIC``.
    exec_mode:
        The resolved exec_mode for this topic (``"subprocess"`` or ``"tmux"``).
    resolver:
        The configured ``WorkspaceResolver``, or ``None``.

    Raises
    ------
    WorkspaceConfigError
        ``dynamic_cwd=True`` combined with ``exec_mode="tmux"`` (incompatible:
        tmux sessions persist across messages and cannot receive a fresh cwd
        per message), or ``dynamic_cwd=True`` with no resolver URL configured.
    """
    if not dynamic_cwd:
        return
    if exec_mode == "tmux":
        raise WorkspaceConfigError(
            "cwd:DYNAMIC is incompatible with exec_mode:tmux — tmux sessions persist "
            "across messages and cannot receive a fresh cwd per message. "
            "Change exec_mode to 'subprocess' or remove cwd:DYNAMIC."
        )
    if resolver is None:
        raise WorkspaceConfigError(
            "cwd:DYNAMIC requires TELEGRAM_AI_AGENT_CWD_RESOLVER_URL to be configured"
        )
