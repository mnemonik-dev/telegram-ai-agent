"""Entry point for the public Telegram-Claude-Code bot."""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from urllib.parse import urlsplit

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from telegram_bot.core.config import get_settings
from telegram_bot.core.handlers.cancel import router as cancel_router
from telegram_bot.core.handlers.commands import router as commands_router
from telegram_bot.core.handlers.forum_topic import router as forum_topic_router
from telegram_bot.core.handlers.forward import ForwardBatcher
from telegram_bot.core.handlers.forward import router as forward_router
from telegram_bot.core.handlers.mode import router as mode_router
from telegram_bot.core.handlers.photo import cleanup_old_tmp_files, ensure_tmp_dir
from telegram_bot.core.handlers.photo import router as photo_router
from telegram_bot.core.handlers.streaming import send_streaming_response
from telegram_bot.core.handlers.text import router as text_router
from telegram_bot.core.handlers.voice import router as voice_router
from telegram_bot.core.keyboards import topic_keyboard
from telegram_bot.core.messages import t
from telegram_bot.core.middleware.auth import AuthMiddleware
from telegram_bot.core.services.bot_commands import setup_bot_commands
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.dynamic_cwd import (
    MessageContext,
    check_dynamic_cwd_preconditions,
    dynamic_cwd_lease,
)
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.topic_runtime import BotDefaults, resolve_topic_runtime_config
from telegram_bot.core.services.transcriber import Transcriber
from telegram_bot.core.services.workspace_resolver import (
    WorkspaceCapacityError,
    WorkspaceConfigError,
    WorkspaceUnreachableError,
    resolver_from_settings,
)
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)


async def process_queue_item(
    channel_key: ChannelKey,
    prompt: str,
    source_messages: list[Message],
    target_session_id: str | None,
    *,
    bot: Bot,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
    workspace_resolver: object,  # WorkspaceResolver | None — avoid circular import hint
) -> None:
    """Send a queued prompt to CC; on session change, notify the user.

    For topics with ``cwd: DYNAMIC``, leases a per-message workspace from the
    external resolver before spawning the engine and releases it in a finally
    block covering engine exit, /cancel, timeout, and crash paths.
    """
    old_session_id = session_manager.get_current_session_id(channel_key)

    # After kill/reset, ignore reply-to-resume on the next message.
    if session_manager.consume_fresh_start(channel_key):
        target_session_id = None

    if target_session_id is not None:
        await session_manager.override_session(channel_key, target_session_id)

    session_changed = target_session_id is not None and target_session_id != old_session_id
    if session_changed and target_session_id:
        chat_id, thread_id = channel_key
        notification = t("ui.session_switched", sid=target_session_id[:8])
        try:
            await bot.send_message(
                chat_id,
                notification,
                reply_markup=topic_keyboard(),
                message_thread_id=thread_id,
            )
        except TelegramBadRequest:
            logger.warning(
                "Failed to send session switch notification (stale thread_id=%s)",
                thread_id,
                exc_info=True,
            )

    reply_message = source_messages[-1] if source_messages else None
    if reply_message is None:
        return

    # Resolve runtime config to detect cwd:DYNAMIC for this topic.
    chat_id, thread_id = channel_key
    topic_settings = topic_config.get_topic(thread_id)
    runtime = resolve_topic_runtime_config(
        topic_settings,
        BotDefaults(
            cwd=session_manager.default_cwd(),
            mcp_config=Path(session_manager.default_mcp_config_path()),
        ),
    )

    # Fail-fast config checks before touching the resolver.
    if runtime.dynamic_cwd:
        try:
            check_dynamic_cwd_preconditions(
                dynamic_cwd=runtime.dynamic_cwd,
                exec_mode=runtime.exec_mode,
                resolver=workspace_resolver,  # type: ignore[arg-type]
            )
        except WorkspaceConfigError as exc:
            logger.error("workspace config error for channel %s: %s", channel_key, exc)
            await bot.send_message(
                chat_id,
                t("ui.workspace_config_error", detail=str(exc)),
                message_thread_id=thread_id,
            )
            return

    # Build message context for task_id derivation.
    msg_ctx = MessageContext(
        chat_id=chat_id,
        message_id=reply_message.message_id,
        thread_id=thread_id,
    )

    # Lease workspace (no-op for static cwd topics).
    try:
        async with dynamic_cwd_lease(
            resolver=workspace_resolver,  # type: ignore[arg-type]
            dynamic_cwd=runtime.dynamic_cwd,
            static_cwd=runtime.cwd,
            message_context=msg_ctx,
            repo=runtime.repo or "default",
            topic_name=runtime.topic_name or str(thread_id or "main"),
        ) as resolved_cwd:
            # Pass the leased path as an explicit cwd_override so it wins over
            # any _apply_topic_config reset inside SessionManager.send_stream.
            # DYNAMIC topics have topic.cwd=None, so _apply_topic_config would
            # otherwise fall back to defaults.cwd, silently breaking isolation.
            effective_cwd_override = resolved_cwd if runtime.dynamic_cwd else None
            await send_streaming_response(
                reply_message,
                session_manager,
                channel_key,
                prompt,
                tmux_manager=tmux_manager,
                cwd_override=effective_cwd_override,
            )
    except WorkspaceCapacityError:
        logger.warning("workspace pool busy for channel %s, engine not spawned", channel_key)
        await bot.send_message(
            chat_id,
            t("ui.workspace_pool_busy"),
            message_thread_id=thread_id,
        )
    except WorkspaceUnreachableError:
        logger.error(
            "workspace resolver unreachable for channel %s, engine not spawned", channel_key
        )
        await bot.send_message(
            chat_id,
            t("ui.workspace_unreachable"),
            message_thread_id=thread_id,
        )
    except WorkspaceConfigError as exc:
        logger.error("workspace config error during spawn for channel %s: %s", channel_key, exc)
        await bot.send_message(
            chat_id,
            t("ui.workspace_config_error", detail=str(exc)),
            message_thread_id=thread_id,
        )


async def _start() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    settings = get_settings()
    bot = Bot(token=settings.telegram_bot_token)
    try:
        # Molyanov slash-commands (/tech-spec-planning, /do-task, …) live
        # in claude's skill bundle but CAN'T be advertised via Telegram's
        # setMyCommands — Bot API only accepts /[a-z0-9_]+/ for command
        # names, and the skills use hyphens. They still work for the
        # operator: typing `/tech-spec-planning idea` falls through to
        # claude (no aiogram handler matches), claude reads the slash
        # prefix and invokes the skill via its Skill tool. The
        # autocomplete just won't surface them.
        await setup_bot_commands(bot)
    except Exception:
        logger.warning("Failed to set Telegram bot commands", exc_info=True)

    topic_config = TopicConfig(settings.topic_config_path, settings.project_root)
    tmux_manager = TmuxManager(
        sessions_dir=Path(settings.project_root) / settings.tmux_sessions_dir,
    )
    tmux_manager.wire_live_buffer(bot=bot, topic_config=topic_config)
    tmux_manager.restore_all()
    session_manager = SessionManager(settings, topic_config=topic_config)
    transcriber = Transcriber(settings)
    forward_batcher = ForwardBatcher(bot=bot)
    workspace_resolver = resolver_from_settings(settings)
    if workspace_resolver is not None:
        # Strip userinfo/path/query from URL before logging — operator may embed
        # credentials in the URL (RFC 3986 userinfo e.g. http://user:token@host).
        # _u.netloc includes userinfo; use hostname + port to get only host:port.
        _u = urlsplit(settings.workspace_resolver_url or "")
        _host_part = _u.hostname or ""
        if _u.port is not None:
            _host_part = f"{_host_part}:{_u.port}"
        _safe_url = f"{_u.scheme}://{_host_part}" if _host_part else "(configured)"
        logger.info("Workspace resolver configured at %s", _safe_url)
    else:
        logger.info("No workspace resolver URL configured (cwd:DYNAMIC topics unsupported)")

    # Startup fail-fast: any DYNAMIC topic without a resolver URL is a
    # misconfiguration that would only surface when the first user message
    # arrives.  Detect and log it here so operators see it at boot, not at
    # runtime.
    if workspace_resolver is None:
        _dynamic_topic_ids = [
            thread_id
            for thread_id, ts in topic_config._topics.items()
            if getattr(ts, "dynamic_cwd", False)
        ]
        if _dynamic_topic_ids:
            logger.error(
                "Startup misconfiguration: %d topic(s) have cwd:DYNAMIC but "
                "TELEGRAM_AI_AGENT_CWD_RESOLVER_URL is not set — "
                "affected thread_ids: %s. Messages in these topics will fail with a "
                "WorkspaceConfigError until the resolver URL is configured.",
                len(_dynamic_topic_ids),
                ", ".join(str(tid) for tid in _dynamic_topic_ids),
            )

    async def _process_queue_item(
        channel_key: ChannelKey,
        prompt: str,
        source_messages: list[Message],
        target_session_id: str | None,
    ) -> None:
        await process_queue_item(
            channel_key,
            prompt,
            source_messages,
            target_session_id,
            bot=bot,
            session_manager=session_manager,
            tmux_manager=tmux_manager,
            topic_config=topic_config,
            workspace_resolver=workspace_resolver,
        )

    message_queue = MessageQueue(bot, session_manager, _process_queue_item)

    dp = Dispatcher()
    auth = AuthMiddleware(allowed_user_ids=settings.allowed_user_ids)
    dp.message.outer_middleware(auth)
    dp.callback_query.outer_middleware(auth)
    dp.message.filter(F.chat.type.in_({ChatType.PRIVATE, ChatType.SUPERGROUP}))

    # Order: commands -> cancel -> mode -> forward -> voice -> photo -> text
    # Forward BEFORE voice/photo so forwarded media is batched, not handled directly.
    # forum_topic_router runs first so topic_config.json is updated BEFORE
    # any text/forward handler tries to read mode/cwd for the new thread.
    dp.include_router(forum_topic_router)
    dp.include_router(commands_router)
    dp.include_router(cancel_router)
    dp.include_router(mode_router)
    dp.include_router(forward_router)
    dp.include_router(voice_router)
    dp.include_router(photo_router)
    dp.include_router(text_router)

    dp["session_manager"] = session_manager
    dp["transcriber"] = transcriber
    dp["forward_batcher"] = forward_batcher
    dp["message_queue"] = message_queue
    dp["queue"] = message_queue
    dp["settings"] = settings
    dp["topic_config"] = topic_config
    dp["tmux_manager"] = tmux_manager

    ensure_tmp_dir(session_manager.file_cache_dir)
    cleanup_old_tmp_files(session_manager.file_cache_dir)
    session_manager.load_mapping()
    session_manager.start_cleanup()

    periodic_cleanup_interval = 6 * 3600

    async def _periodic_tmp_cleanup() -> None:
        while True:
            await asyncio.sleep(periodic_cleanup_interval)
            try:
                deleted = cleanup_old_tmp_files(session_manager.file_cache_dir)
                logger.info("Periodic tmp cleanup: deleted %d files", deleted)
            except Exception:
                logger.warning("Periodic tmp cleanup failed", exc_info=True)

    cleanup_task = asyncio.create_task(_periodic_tmp_cleanup())

    async def _on_shutdown() -> None:
        logger.info("Shutting down: cleaning up sessions...")
        cleanup_task.cancel()
        await forward_batcher.shutdown()
        await message_queue.shutdown()
        await session_manager.shutdown()
        session_manager.save_mapping()
        tmux_manager._save_state()

    dp.shutdown.register(_on_shutdown)

    loop = asyncio.get_running_loop()
    _pending_stop: asyncio.Future[None] | None = None

    def _stop() -> None:
        nonlocal _pending_stop
        _pending_stop = asyncio.ensure_future(dp.stop_polling())

    loop.add_signal_handler(signal.SIGTERM, _stop)
    loop.add_signal_handler(signal.SIGINT, _stop)

    logger.info("Starting bot, allowed users: %d", len(settings.allowed_user_ids))
    await dp.start_polling(bot, handle_signals=False)


def main() -> None:
    try:
        asyncio.run(_start())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
