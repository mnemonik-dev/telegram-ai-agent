"""Authentication middleware — role-based access via UserStore."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from telegram_bot.core.messages import t
from telegram_bot.core.services.user_store import ROLES, UserStore

logger = logging.getLogger(__name__)

# Commands only operators may run. Members keep everything else.
OPERATOR_COMMANDS = frozenset({"invite", "grant", "revoke", "users", "relogin", "sync_commands"})
# The only commands a read-only viewer may run.
VIEWER_COMMANDS = frozenset({"whoami", "help", "start"})

_COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@\w+)?(?:\s|$)")
INVITE_START_RE = re.compile(r"^/start(?:@\w+)?\s+(inv_[A-Za-z0-9_-]{6,64})\s*$")


def _is_forum_topic_event(event: TelegramObject) -> bool:
    """Service messages emitted by Telegram when a forum topic is created/edited.

    These bypass the user whitelist because `from_user` is whoever triggered the
    change — often the bot itself (when CC creates topics via Bot API), or a
    group admin with topic permissions. The forum_topic handler needs to see them
    regardless of who fired them, otherwise topic_config.json is never updated
    when the bot creates a topic.
    """
    if not isinstance(event, Message):
        return False
    return event.forum_topic_created is not None or event.forum_topic_edited is not None


def command_name(text: str | None) -> str | None:
    match = _COMMAND_RE.match(text or "")
    return match.group(1).lower() if match else None


class AuthMiddleware(BaseMiddleware):
    def __init__(self, store: UserStore) -> None:
        self.store = store

    def _note_seen(self, event: TelegramObject) -> None:
        user = getattr(event, "from_user", None)
        if user is not None:
            self.store.note_seen(user.id, user.username)
        if isinstance(event, Message):
            for member in event.new_chat_members or []:
                self.store.note_seen(member.id, member.username)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if _is_forum_topic_event(event):
            return await handler(event, data)
        user = getattr(event, "from_user", None)
        if user is None:
            logger.warning("Ignored event without a sender")
            return None
        self._note_seen(event)

        role = self.store.role_of(user.id)
        if role is None:
            # A stranger may only redeem an invite, in a private chat.
            if (
                isinstance(event, Message)
                and event.chat.type == ChatType.PRIVATE
                and INVITE_START_RE.match(event.text or "")
            ):
                data["role"] = None
                return await handler(event, data)
            # Unauthorized access is a security signal — leave a WARNING so
            # `journalctl -p warning -u telegram-bot` surfaces a probe or
            # a leaked-token pattern.
            logger.warning("Ignored message from unauthorized user: %s", user.id)
            return None

        data["role"] = role
        if isinstance(event, Message):
            cmd = command_name(event.text)
            if cmd in OPERATOR_COMMANDS and role != "operator":
                await event.answer(t("ui.users_operators_only"))
                return None
            if role == "viewer" and cmd not in VIEWER_COMMANDS:
                # Stay quiet in shared chats; explain only in private.
                if event.chat.type == ChatType.PRIVATE:
                    await event.answer(t("ui.users_viewer_readonly"))
                return None
        elif isinstance(event, CallbackQuery) and role == "viewer":
            await event.answer(t("ui.users_viewer_readonly"), show_alert=True)
            return None
        return await handler(event, data)


__all__ = ["ROLES", "AuthMiddleware", "command_name"]
