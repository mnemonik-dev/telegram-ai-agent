"""Shared-chat context: who is the engine being spoken to, and what did it miss.

Used by ChatModeMiddleware for topics with ``chat_mode: mention``. Messages
that are not addressed to the engine are kept in a small in-memory backlog
and handed to the engine, attributed by sender, together with the next
message that *is* addressed to it. The backlog is per process: after a
restart only the engine's own session history remains.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from aiogram.types import Message, User

from telegram_bot.core.types import ChannelKey

BACKLOG_MAX_ENTRIES = 30
ENTRY_MAX_CHARS = 500
CONTEXT_MAX_CHARS = 6000

_ENGINE_TAG_RE = re.compile(r"(?<![\w@])@(claude|codex)\b", re.IGNORECASE)

Addressing = Literal["bot", "other_engine"]


@dataclass(frozen=True)
class BacklogEntry:
    speaker: str
    text: str
    ts: float


class ChatBacklog:
    def __init__(self, maxlen: int = BACKLOG_MAX_ENTRIES) -> None:
        self._maxlen = maxlen
        self._entries: dict[ChannelKey, deque[BacklogEntry]] = {}

    def add(self, key: ChannelKey, speaker: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        bucket = self._entries.setdefault(key, deque(maxlen=self._maxlen))
        bucket.append(BacklogEntry(speaker, text[:ENTRY_MAX_CHARS], time.time()))

    def drain(self, key: ChannelKey) -> list[BacklogEntry]:
        bucket = self._entries.pop(key, None)
        return list(bucket) if bucket else []

    def __len__(self) -> int:
        return sum(len(b) for b in self._entries.values())


def speaker_label(user: User | None) -> str:
    if user is None:
        return "someone"
    if user.username:
        return f"{user.full_name} (@{user.username})"
    return user.full_name


def addressing(
    message: Message, *, bot_id: int, bot_username: str, engine: str
) -> Addressing | None:
    """How (if at all) this message is addressed to the engine.

    * ``bot``          — @botname, a reply to one of the bot's messages, or an
                         @claude/@codex tag naming the topic's current engine.
    * ``other_engine`` — tags an engine that is not the topic's current one.
    """
    text = message.text or message.caption or ""
    entities = (message.entities or []) + (message.caption_entities or [])

    handle = f"@{bot_username}".lower()
    if handle != "@" and handle in text.lower():
        return "bot"
    for ent in entities:
        if ent.type == "text_mention" and ent.user is not None and ent.user.id == bot_id:
            return "bot"

    reply = message.reply_to_message
    # In a forum topic every message "replies" to the topic's creation message;
    # when the bot created the topic that would look like a reply to the bot.
    if (
        reply is not None
        and reply.forum_topic_created is None
        and reply.message_id != message.message_thread_id
        and reply.from_user is not None
        and reply.from_user.id == bot_id
    ):
        return "bot"

    tags = {m.lower() for m in _ENGINE_TAG_RE.findall(text)}
    if engine.lower() in tags:
        return "bot"
    if tags:
        return "other_engine"
    return None


def format_context(entries: list[BacklogEntry], speaker: str) -> str:
    """Prefix for the addressed message: missed messages, then who is talking."""
    lines = ["[Group chat: several people share this conversation; each line shows its sender.]"]
    if entries:
        picked: list[str] = []
        budget = CONTEXT_MAX_CHARS
        for entry in reversed(entries):  # newest first, drop the oldest on overflow
            line = f"{entry.speaker}: {entry.text}"
            if budget - len(line) < 0:
                break
            budget -= len(line)
            picked.append(line)
        lines.append("[Messages since you last replied]")
        lines.extend(reversed(picked))
        lines.append("[Now addressed to you]")
    return "\n".join(lines) + f"\n{speaker}: "
