"""ChatModeMiddleware — answer only when addressed in ``chat_mode: mention`` topics.

Registered as an *inner* middleware, so it only runs for updates some handler
matched. Slash commands and service messages always pass through. In
supergroups whose topic is in ``mention`` mode:

* addressed messages continue to the handler, with the missed-message context
  placed in ``data["chat_context"]`` (the text handler prepends it);
* everything else is recorded in the backlog and the handler is skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject

from telegram_bot.core.messages import t
from telegram_bot.core.services.chat_context import (
    ChatBacklog,
    addressing,
    format_context,
    speaker_label,
)
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.types import channel_key

logger = logging.getLogger(__name__)


def _has_user_content(message: Message) -> bool:
    return bool(
        message.text
        or message.caption
        or message.photo
        or message.voice
        or message.document
        or message.forward_origin
    )


class ChatModeMiddleware(BaseMiddleware):
    def __init__(self, topic_config: TopicConfig, backlog: ChatBacklog) -> None:
        self.topic_config = topic_config
        self.backlog = backlog

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.chat.type != ChatType.SUPERGROUP:
            return await handler(event, data)
        if not _has_user_content(event) or (event.text or "").startswith("/"):
            return await handler(event, data)
        key = channel_key(event)
        settings = self.topic_config.get_topic(key[1])
        if settings.chat_mode != "mention":
            return await handler(event, data)

        bot = data["bot"]
        me = await bot.me()
        how = addressing(
            event, bot_id=me.id, bot_username=me.username or "", engine=settings.engine
        )
        speaker = speaker_label(event.from_user)
        content = event.text or event.caption or ""

        if how == "other_engine":
            await event.answer(t("ui.chat_other_engine", engine=settings.engine))
            return None
        if how is None:
            self.backlog.add(key, speaker, content or "[media]")
            return None

        if event.text:
            data["chat_context"] = format_context(self.backlog.drain(key), speaker)
        return await handler(event, data)
