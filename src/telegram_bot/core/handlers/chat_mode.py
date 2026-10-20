"""/chat — show or set how the engine behaves in a shared chat."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from telegram_bot.core.messages import t
from telegram_bot.core.services.topic_config import (
    _VALID_CHAT_MODES,
    TopicConfig,
    normalize_thread_id,
)

router = Router(name="chat_mode")


@router.message(Command("chat"))
async def handle_chat_command(
    message: Message, command: CommandObject, topic_config: TopicConfig
) -> None:
    thread_id = message.message_thread_id
    current = topic_config.get_topic(thread_id).chat_mode
    arg = (command.args or "").strip().lower()
    if not arg:
        await message.answer(t("ui.chat_mode_current", mode=current), parse_mode="HTML")
        return
    if arg not in _VALID_CHAT_MODES:
        await message.answer(t("ui.chat_mode_usage"), parse_mode="HTML")
        return
    if not await topic_config.update_chat_mode(normalize_thread_id(thread_id), arg):
        await message.answer(t("ui.chat_mode_write_failed"))
        return
    await message.answer(t("ui.chat_mode_set", mode=arg), parse_mode="HTML")
