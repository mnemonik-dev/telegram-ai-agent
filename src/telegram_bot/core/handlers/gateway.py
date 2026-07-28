"""Gateway commands: /status, /relogin, /sync_commands.

These make the Telegram surface self-sufficient on quota days:

- /status         — effective engine/model/config source + auth env presence
- /relogin        — reset the engine session, re-read credentials from .env,
                    probe the engine, report the verbatim outcome
- /sync_commands  — rescan $HOME/.claude/{commands,skills} and re-register
                    the Telegram command menu without a bot restart

Kept in a dedicated module (not commands.py) to minimize the upstream
rebase surface — see work/tg-slash-command-gateway in coding-fabric.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import dotenv_values

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers.forward import ForwardBatcher
from telegram_bot.core.messages import t
from telegram_bot.core.services.bot_commands import PUBLIC_BOT_COMMANDS, setup_bot_commands
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.command_registry import CommandRegistry
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.topic_config import TopicConfig, normalize_thread_id
from telegram_bot.core.types import channel_key

logger = logging.getLogger(__name__)

router = Router(name="gateway")

# Credential vars the bot re-reads from .env on /relogin. The engine
# subprocess inherits os.environ, so refreshing these here is enough for
# the NEXT spawn — no systemd restart required. Values never appear in
# chat or logs.
_CREDENTIAL_ENV_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")

_PROBE_TIMEOUT_SEC = 90


def _mask_presence(name: str) -> str:
    value = os.environ.get(name, "")
    return f"{name}=set" if value else f"{name}=unset"


@router.message(Command("status"))
async def handle_status(
    message: Message,
    topic_config: TopicConfig,
    session_manager: SessionManager,
    message_queue: MessageQueue,
) -> None:
    """Report the effective configuration for the current chat."""
    key = channel_key(message)
    settings = topic_config.get_topic(key[1])
    source = (
        t("ui.status_source_override")
        if topic_config.has_topic(key[1])
        else t("ui.status_source_defaults")
    )
    session_id = session_manager.get_current_session_id(key)
    await message.answer(
        t(
            "ui.status_report",
            engine=settings.engine,
            model=settings.model or "(engine default)",
            exec_mode=settings.exec_mode,
            stream_mode=settings.stream_mode,
            mode=settings.mode,
            cwd=settings.cwd
            or ("DYNAMIC" if settings.dynamic_cwd else str(session_manager.default_cwd())),
            source=source,
            session=session_id[:8] if session_id else "(none)",
            busy=str(message_queue.is_busy(key)).lower(),
            auth=", ".join(_mask_presence(name) for name in _CREDENTIAL_ENV_VARS[:2]),
        )
    )


def _reload_credentials_from_env_file(project_root: str) -> int:
    """Re-read credential vars from <project_root>/.env into os.environ.

    Returns the number of vars updated. Missing file or unchanged values
    are not errors — the point is picking up an operator's live edit
    (e.g. a fresh CLAUDE_CODE_OAUTH_TOKEN after quota exhaustion).
    """
    env_path = Path(project_root) / ".env"
    try:
        values = dotenv_values(env_path)
    except OSError:
        logger.warning("relogin: cannot read %s", env_path, exc_info=True)
        return 0
    updated = 0
    for name in _CREDENTIAL_ENV_VARS:
        value = values.get(name)
        if value and value != os.environ.get(name):
            os.environ[name] = value
            updated += 1
            logger.info("relogin: refreshed %s from %s", name, env_path)
    return updated


async def _probe_claude(cwd: Path) -> tuple[bool, str]:
    """One-shot `claude -p ping` with the CURRENT os.environ.

    Returns (ok, detail) where detail is the reply text on success or the
    stderr/stdout tail on failure — verbatim, because the Anthropic error
    text ("credit balance is too low", "Invalid API key", rate limits) is
    exactly what the operator needs to see.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "claude",
            "--output-format",
            "text",
            "-p",
            "Reply with exactly: pong",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
    except FileNotFoundError:
        return False, t("ui.cc_not_found")
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_PROBE_TIMEOUT_SEC
        )
    except TimeoutError:
        process.kill()
        return False, f"probe timed out after {_PROBE_TIMEOUT_SEC}s"
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    if process.returncode == 0 and out:
        return True, out[-300:]
    detail = (err or out or f"exit code {process.returncode}")[-500:]
    return False, detail


@router.message(Command("model"))
async def handle_model(
    message: Message,
    topic_config: TopicConfig,
) -> None:
    """Show or persist a per-chat model override.

    `/model` shows the current override; `/model sonnet` persists one;
    `/model reset` returns to the engine default. Works in forum topics
    and the General chat alike. The override is read at engine spawn, so
    it applies from the next message (send /clear first for a clean
    session). Bracketed variants like `sonnet[1m]` are rejected by the
    model validator — the 1M-context beta requires paid usage credits
    and is exactly what an operator uses /model to escape.
    """
    key = channel_key(message)
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if not arg:
        current = topic_config.get_topic(key[1]).model
        await message.answer(t("ui.model_current", model=current or "(engine default)"))
        return

    if arg.lower() in {"reset", "default", "none"}:
        if await topic_config.update_model(normalize_thread_id(key[1]), None):
            await message.answer(t("ui.model_reset"))
        else:
            await message.answer(t("ui.model_write_failed"))
        return

    if not await topic_config.update_model(normalize_thread_id(key[1]), arg):
        await message.answer(t("ui.model_invalid", model=arg))
        return
    await message.answer(t("ui.model_set", model=arg))


@router.message(Command("relogin"))
async def handle_relogin(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    topic_config: TopicConfig,
    settings: Settings,
) -> None:
    """Reset the engine session for this chat and re-establish auth.

    The bot's engine subprocesses authenticate from env vars, so
    "relogin" means: drop session state → re-read .env → verify with a
    one-shot probe. Token VALUES are never accepted via chat.
    """
    key = channel_key(message)
    await message.answer(t("ui.relogin_start"))

    forward_batcher.clear(key)
    await message_queue.clear(key)
    await session_manager.kill_session(key)

    updated = _reload_credentials_from_env_file(settings.project_root)
    if updated:
        await message.answer(t("ui.relogin_env", count=updated))

    engine = topic_config.get_topic(key[1]).engine
    if engine != "claude":
        await message.answer(t("ui.relogin_probe_skipped", engine=engine))
        return

    ok, detail = await _probe_claude(session_manager.default_cwd())
    if ok:
        await message.answer(t("ui.relogin_ok", engine=engine, reply=detail))
    else:
        await message.answer(t("ui.relogin_failed", detail=detail))


@router.message(Command("sync_commands"))
async def handle_sync_commands(
    message: Message,
    command_registry: CommandRegistry | None = None,
) -> None:
    """Rescan engine command files and re-register the Telegram menu."""
    if command_registry is None or message.bot is None:
        await message.answer(t("ui.sync_commands_unavailable"))
        return
    count = await asyncio.to_thread(command_registry.scan)
    extra = command_registry.localized_commands(reserved_slots=len(PUBLIC_BOT_COMMANDS))
    try:
        await setup_bot_commands(message.bot, extra_commands=extra)
    except Exception:
        logger.warning("sync_commands: setMyCommands failed", exc_info=True)
    dropped = (
        t("ui.sync_commands_dropped", names=", ".join(command_registry.dropped))
        if command_registry.dropped
        else ""
    )
    await message.answer(t("ui.sync_commands_done", count=count, dropped=dropped))
