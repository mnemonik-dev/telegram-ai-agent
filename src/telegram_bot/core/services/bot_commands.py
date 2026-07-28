"""Telegram bot command menu registration."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeUnion,
)


class BotCommandSetter(Protocol):
    async def set_my_commands(
        self,
        commands: list[BotCommand],
        scope: BotCommandScopeUnion | None = None,
        language_code: str | None = None,
        request_timeout: int | None = None,
    ) -> bool: ...


@dataclass(frozen=True)
class LocalizedBotCommand:
    command: str
    ru_description: str
    en_description: str


PUBLIC_BOT_COMMANDS: tuple[LocalizedBotCommand, ...] = (
    LocalizedBotCommand("start", "Запустить бота", "Start the bot"),
    LocalizedBotCommand("clear", "Сбросить текущий чат", "Reset the current chat"),
    LocalizedBotCommand("cancel", "Отменить текущую обработку", "Cancel current processing"),
    LocalizedBotCommand("language", "Сменить язык интерфейса", "Change interface language"),
    LocalizedBotCommand("mode", "Выбрать режим выполнения", "Choose execution mode"),
    LocalizedBotCommand("stream", "Выбрать режим ответов", "Choose response mode"),
    LocalizedBotCommand("engine", "Выбрать Claude Code или Codex", "Choose Claude Code or Codex"),
    LocalizedBotCommand(
        "status",
        "Движок/модель/авторизация чата",
        "Chat engine/model/auth status",
    ),
    LocalizedBotCommand(
        "model",
        "Задать модель для этого чата",
        "Set the model for this chat",
    ),
    LocalizedBotCommand(
        "relogin",
        "Сбросить сессию и перечитать креды",
        "Reset session and reload credentials",
    ),
    LocalizedBotCommand(
        "sync_commands",
        "Пересканировать команды движка",
        "Rescan engine commands",
    ),
    LocalizedBotCommand("resume", "Возобновить сохраненную сессию", "Resume a saved session"),
    LocalizedBotCommand("kill", "Остановить tmux-сессию", "Stop the tmux session"),
    LocalizedBotCommand("tui", "Открыть панель TUI", "Open the TUI panel"),
    LocalizedBotCommand(
        "tail",
        "Открыть панель TUI (старый алиас)",
        "Open the TUI panel (legacy alias)",
    ),
)


# Claude-side skill commands (Molyanov methodology) shipped via the
# claude-skills bundle in $HOME/.claude/{skills,commands}. Telegram
# doesn't natively know these — registering them in setMyCommands just
# makes them discoverable in the `/` autocomplete. The actual text
# message still flows through to claude verbatim (aiogram's Command()
# router has no matching handler, so it falls through to the default
# text path); claude then invokes the matching skill via its Skill tool.
MOLYANOV_BOT_COMMANDS: tuple[LocalizedBotCommand, ...] = (
    LocalizedBotCommand(
        "user_spec_planning",
        "Создать user-spec фичи (Molyanov)",
        "Plan a feature's user-spec (Molyanov)",
    ),
    LocalizedBotCommand(
        "tech_spec_planning",
        "Создать tech-spec из user-spec",
        "Plan a tech-spec from a user-spec",
    ),
    LocalizedBotCommand(
        "decompose_tech_spec",
        "Разбить tech-spec на задачи",
        "Decompose a tech-spec into tasks",
    ),
    LocalizedBotCommand(
        "do_task",
        "Выполнить задачу из tasks/<NN>.md",
        "Execute a task from tasks/<NN>.md",
    ),
    LocalizedBotCommand(
        "do_feature",
        "Выполнить фичу командой агентов",
        "Execute a feature with an agent team",
    ),
    LocalizedBotCommand(
        "write_code",
        "Написать код через TDD и ревью",
        "Write code with TDD and review",
    ),
    LocalizedBotCommand(
        "code_reviewing",
        "Провести ревью кода",
        "Review code",
    ),
    LocalizedBotCommand(
        "test_master",
        "Стратегия тестирования / ревью тестов",
        "Testing strategy / test review",
    ),
    LocalizedBotCommand(
        "security_auditor",
        "Аудит безопасности (OWASP)",
        "Security audit (OWASP)",
    ),
    LocalizedBotCommand(
        "pre_deploy_qa",
        "Приёмочное тестирование перед деплоем",
        "Pre-deploy acceptance testing",
    ),
    LocalizedBotCommand(
        "post_deploy_qa",
        "Верификация после деплоя",
        "Post-deploy verification",
    ),
    LocalizedBotCommand(
        "done",
        "Завершить фичу: документация и архив",
        "Finalize a feature: docs + archive",
    ),
)


def build_bot_commands(
    language_code: str,
    *,
    extra_commands: Sequence[LocalizedBotCommand] = (),
) -> list[BotCommand]:
    """Build Telegram Bot API commands for one language."""
    if language_code not in {"ru", "en"}:
        raise ValueError(f"Unsupported bot command language: {language_code}")

    commands = (*PUBLIC_BOT_COMMANDS, *extra_commands)
    return [
        BotCommand(
            command=command.command,
            description=(
                command.ru_description if language_code == "ru" else command.en_description
            ),
        )
        for command in commands
    ]


async def setup_bot_commands(
    bot: BotCommandSetter,
    *,
    extra_commands: Sequence[LocalizedBotCommand] = (),
) -> None:
    """Register the same command menu in every scope a client may read.

    Telegram resolves the command list per chat by walking scopes from most
    specific (chat → all_chat_administrators → all_group_chats / all_private_chats)
    down to default and returning the first non-empty list — it does not merge
    them. Writing only to default leaves any pre-existing list in a narrower
    scope shadowing the new commands. Writing the full list everywhere keeps
    the menu consistent regardless of past state.
    """
    ru_commands = build_bot_commands("ru", extra_commands=extra_commands)
    en_commands = build_bot_commands("en", extra_commands=extra_commands)

    scopes: tuple[BotCommandScopeUnion | None, ...] = (
        None,
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
    )

    for scope in scopes:
        await bot.set_my_commands(ru_commands, scope=scope)
        await bot.set_my_commands(ru_commands, scope=scope, language_code="ru")
        await bot.set_my_commands(en_commands, scope=scope, language_code="en")
