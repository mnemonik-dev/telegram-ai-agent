"""CommandRegistry — discover engine slash commands and bridge Telegram aliases.

The engine (Claude Code CLI) resolves slash commands from
``$HOME/.claude/commands/*.md`` (command files) and exposes skills from
``$HOME/.claude/skills/<name>/SKILL.md`` as slash commands too. Their
canonical names use dashes (``/do-task``, ``/tech-spec-planning``) — but
Telegram bot commands only allow ``[a-z0-9_]{1,32}``, so an operator can
only ever *type* the underscore spelling.

This module closes that gap in both directions:

- **Discovery**: scan the engine HOME once at startup (and again on
  ``/sync_commands``) and expose the found commands for Telegram's
  ``setMyCommands`` autocomplete, names normalized to underscore.
- **Canonicalization**: rewrite an incoming ``/do_task args`` message to
  the canonical ``/do-task args`` before it is enqueued as the engine
  prompt, so exactly one command file per command is sufficient and the
  operator may type either spelling.

Scan failures are non-fatal by design: a broken frontmatter or an absent
directory degrades to "no discovered commands", never to a crash.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from telegram_bot.core.services.bot_commands import LocalizedBotCommand

logger = logging.getLogger(__name__)

# Telegram Bot API constraints (https://core.telegram.org/bots/api#botcommand)
_TELEGRAM_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_TELEGRAM_MAX_COMMANDS = 100
_TELEGRAM_DESC_MAX = 256
_TELEGRAM_DESC_MIN = 3

_FRONTMATTER_DELIMITER = "---"


@dataclass(frozen=True)
class DiscoveredCommand:
    """One engine-side slash command usable through the Telegram gateway."""

    canonical: str  # engine-side name, e.g. "do-task"
    alias: str  # telegram-safe name, e.g. "do_task"
    description: str
    source: str  # "command" (commands/*.md) | "skill" (skills/*/SKILL.md)


def _parse_frontmatter_description(path: Path) -> str:
    """Extract the frontmatter ``description`` from a command/skill .md file.

    Handles both inline (``description: text``) and block scalars
    (``description: |`` followed by indented lines — the first non-empty
    line wins). Returns "" when the file has no parseable description;
    callers substitute a fallback.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""

    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return ""

    in_block = False
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == _FRONTMATTER_DELIMITER:
            break
        if in_block:
            # First non-empty line of the block scalar is the description.
            if stripped:
                return stripped
            continue
        if stripped.startswith("description:"):
            value = stripped[len("description:") :].strip()
            if value in {"|", ">", "|-", ">-"}:
                in_block = True
                continue
            return value.strip("\"'")
    return ""


def _telegram_alias(canonical: str) -> str | None:
    """Map a canonical command name to its Telegram-safe alias, or None."""
    alias = canonical.lower().replace("-", "_")
    return alias if _TELEGRAM_COMMAND_RE.fullmatch(alias) else None


def _clamp_description(description: str, fallback: str) -> str:
    desc = " ".join(description.split()) or fallback
    if len(desc) < _TELEGRAM_DESC_MIN:
        desc = fallback
    return desc[:_TELEGRAM_DESC_MAX]


class CommandRegistry:
    """Discovered engine commands + Telegram alias bridging.

    ``claude_home`` is the HOME the engine subprocess runs with — command
    files live in ``<claude_home>/.claude/commands`` and skills in
    ``<claude_home>/.claude/skills``.
    """

    def __init__(self, claude_home: Path) -> None:
        self._claude_home = claude_home
        self._by_alias: dict[str, DiscoveredCommand] = {}
        self._dropped: tuple[str, ...] = ()

    @property
    def commands(self) -> tuple[DiscoveredCommand, ...]:
        return tuple(self._by_alias.values())

    @property
    def dropped(self) -> tuple[str, ...]:
        """Canonical names that could not be exposed to Telegram (invalid/capped)."""
        return self._dropped

    def scan(self) -> int:
        """(Re)scan the engine HOME. Returns the number of discovered commands.

        Precedence on alias collision: command files beat skills (a command
        file is an explicit operator-authored entry point), and among
        command files the dash spelling beats its underscore twin so the
        canonical prompt rewrite is stable when a bundle ships both.
        """
        found: dict[str, DiscoveredCommand] = {}
        dropped: list[str] = []

        def consider(canonical: str, description: str, source: str) -> None:
            alias = _telegram_alias(canonical)
            if alias is None:
                dropped.append(canonical)
                logger.warning(
                    "command %r cannot be exposed as a Telegram command "
                    "(alias would violate [a-z0-9_]{1,32})",
                    canonical,
                )
                return
            existing = found.get(alias)
            if existing is not None:
                keep_existing = (
                    # command files beat skills
                    (existing.source == "command" and source == "skill")
                    # dash canonical beats underscore twin from the same source
                    or (existing.source == source and "-" in existing.canonical)
                )
                if keep_existing:
                    return
            found[alias] = DiscoveredCommand(
                canonical=canonical,
                alias=alias,
                description=_clamp_description(description, canonical),
                source=source,
            )

        commands_dir = self._claude_home / ".claude" / "commands"
        try:
            command_files = sorted(commands_dir.glob("*.md"))
        except OSError:
            command_files = []
        for path in command_files:
            consider(path.stem, _parse_frontmatter_description(path), "command")

        skills_dir = self._claude_home / ".claude" / "skills"
        try:
            skill_files = sorted(skills_dir.glob("*/SKILL.md"))
        except OSError:
            skill_files = []
        for path in skill_files:
            consider(path.parent.name, _parse_frontmatter_description(path), "skill")

        self._by_alias = dict(sorted(found.items()))
        self._dropped = tuple(dropped)
        logger.info(
            "command registry: %d commands discovered under %s (%d not exposable)",
            len(self._by_alias),
            self._claude_home / ".claude",
            len(dropped),
        )
        return len(self._by_alias)

    def canonicalize(self, text: str) -> str:
        """Rewrite a leading ``/alias`` to its canonical engine spelling.

        ``/do_task 05`` → ``/do-task 05`` (when the canonical name differs).
        ``/do_task@my_bot 05`` also resolves — Telegram clients append the
        bot mention when picking from autocomplete in groups. Text that is
        not a discovered alias passes through unchanged.
        """
        if not text.startswith("/"):
            return text
        first, sep, rest = text.partition(" ")
        name = first[1:]
        # Strip the @botname mention Telegram appends in group autocomplete.
        name, _, _mention = name.partition("@")
        entry = self._by_alias.get(name.lower())
        if entry is None or entry.canonical == name:
            return text
        rewritten = f"/{entry.canonical}{sep}{rest}"
        logger.info("canonicalized telegram command %r -> %r", first, f"/{entry.canonical}")
        return rewritten

    def localized_commands(
        self, *, reserved_slots: int = 0
    ) -> tuple[LocalizedBotCommand, ...]:
        """Build setMyCommands entries, respecting Telegram's 100-command cap.

        ``reserved_slots`` is the number of bot built-in commands that share
        the menu; discovered commands beyond the remaining budget are
        dropped (alphabetically last first) and reported via ``dropped``.
        """
        budget = max(0, _TELEGRAM_MAX_COMMANDS - reserved_slots)
        entries = list(self._by_alias.values())
        overflow = entries[budget:]
        if overflow:
            self._dropped = tuple(
                dict.fromkeys([*self._dropped, *(e.canonical for e in overflow)])
            )
            logger.warning(
                "command registry: %d commands beyond Telegram's %d-command cap "
                "not registered: %s",
                len(overflow),
                _TELEGRAM_MAX_COMMANDS,
                ", ".join(e.canonical for e in overflow),
            )
        return tuple(
            LocalizedBotCommand(e.alias, e.description, e.description)
            for e in entries[:budget]
        )
