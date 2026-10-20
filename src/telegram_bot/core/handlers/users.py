"""User management: /invite, /grant, /revoke, /users, /whoami and invite redeem.

Operator-only commands are gated twice: AuthMiddleware blocks non-operators,
and each handler re-checks the role against the store.
"""

from __future__ import annotations

import contextlib
import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, User

from telegram_bot.core.config import Settings
from telegram_bot.core.messages import t
from telegram_bot.core.middleware.auth import INVITE_START_RE
from telegram_bot.core.services.user_store import (
    ROLES,
    BootstrapOperatorError,
    Role,
    UserStore,
    parse_role,
)

logger = logging.getLogger(__name__)

router = Router(name="users")

_ROLES_TEXT = ", ".join(ROLES)


def _label(user: User) -> str:
    return f"@{user.username}" if user.username else (user.full_name or str(user.id))


def _is_operator(message: Message, store: UserStore) -> bool:
    return message.from_user is not None and store.role_of(message.from_user.id) == "operator"


async def _resolve_target(
    message: Message, token: str | None, store: UserStore
) -> tuple[int, str | None, str | None, str] | None:
    """Pick the user a command refers to: reply target, numeric id or @username.

    Returns (user_id, username, display_name, label) or None when unknown.
    """
    reply = message.reply_to_message
    if token is None and reply is not None and reply.from_user and not reply.from_user.is_bot:
        u = reply.from_user
        return u.id, u.username, u.full_name, _label(u)
    if token is None:
        return None
    if token.lstrip("-").isdigit():
        return int(token), None, None, token
    resolved = store.resolve_username(token)
    if resolved is None:
        return None
    return resolved, token.lstrip("@"), None, "@" + token.lstrip("@")


@router.message(F.text.regexp(INVITE_START_RE.pattern), F.chat.type == ChatType.PRIVATE)
async def handle_invite_redeem(message: Message, user_store: UserStore, bot: Bot) -> None:
    user = message.from_user
    match = INVITE_START_RE.match(message.text or "")
    if user is None or match is None:
        return
    result = user_store.redeem(match.group(1), user.id, username=user.username, name=user.full_name)
    if result is None:
        await message.answer(t("ui.invite_invalid"))
        return
    role, created_by = result
    await message.answer(t("ui.invite_redeemed", role=role), parse_mode=ParseMode.HTML)
    logger.info("Invite redeemed by %s as %s (created by %s)", user.id, role, created_by)
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(
            created_by,
            t("ui.invite_joined_notice", who=html.escape(_label(user)), role=role),
            parse_mode=ParseMode.HTML,
        )


@router.message(Command("invite"))
async def handle_invite(
    message: Message, command: CommandObject, user_store: UserStore, settings: Settings, bot: Bot
) -> None:
    if not _is_operator(message, user_store) or message.from_user is None:
        return
    role: Role | None = parse_role(command.args or "member")
    if role is None:
        await message.answer(t("ui.users_bad_role", roles=_ROLES_TEXT))
        return
    code = user_store.create_invite(role, by=message.from_user.id)
    username = (await bot.me()).username
    await message.answer(
        t(
            "ui.invite_created",
            role=role,
            hours=settings.invite_ttl_hours,
            link=f"https://t.me/{username}?start={code}",
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.message(Command("grant"))
async def handle_grant(message: Message, command: CommandObject, user_store: UserStore) -> None:
    if not _is_operator(message, user_store) or message.from_user is None:
        return
    args = (command.args or "").split()
    reply_mode = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and len(args) == 1
    )
    token: str | None
    if reply_mode:
        token, role_raw = None, args[0]
    elif len(args) == 2:
        token, role_raw = args
    else:
        await message.answer(t("ui.grant_usage", roles=_ROLES_TEXT), parse_mode=ParseMode.HTML)
        return
    role = parse_role(role_raw)
    if role is None:
        await message.answer(t("ui.users_bad_role", roles=_ROLES_TEXT))
        return
    target = await _resolve_target(message, token, user_store)
    if target is None:
        await message.answer(
            t("ui.grant_unknown_user", who=html.escape(token or "?")), parse_mode=ParseMode.HTML
        )
        return
    uid, username, name, label = target
    try:
        user_store.grant(uid, role, by=message.from_user.id, username=username, name=name)
    except BootstrapOperatorError:
        await message.answer(
            t("ui.users_bootstrap", who=html.escape(label)), parse_mode=ParseMode.HTML
        )
        return
    logger.info("Role %s granted to %s by %s", role, uid, message.from_user.id)
    await message.answer(
        t("ui.grant_ok", who=html.escape(label), role=role), parse_mode=ParseMode.HTML
    )


@router.message(Command("revoke"))
async def handle_revoke(message: Message, command: CommandObject, user_store: UserStore) -> None:
    if not _is_operator(message, user_store) or message.from_user is None:
        return
    token = (command.args or "").split()[0] if command.args else None
    target = await _resolve_target(message, token, user_store)
    if target is None:
        await message.answer(t("ui.revoke_usage"))
        return
    uid, _username, _name, label = target
    try:
        removed = user_store.revoke(uid)
    except BootstrapOperatorError:
        await message.answer(
            t("ui.users_bootstrap", who=html.escape(label)), parse_mode=ParseMode.HTML
        )
        return
    key = "ui.revoke_ok" if removed else "ui.revoke_none"
    logger.info("Revoke %s by %s -> removed=%s", uid, message.from_user.id, removed)
    await message.answer(t(key, who=html.escape(label)), parse_mode=ParseMode.HTML)


@router.message(Command("users"))
async def handle_users(message: Message, user_store: UserStore) -> None:
    if not _is_operator(message, user_store):
        return
    rows = user_store.list_users()
    lines = [t("ui.users_header", count=len(rows))]
    for row in rows:
        who = f"@{row['username']}" if row["username"] else ""
        tag = " (config)" if row["bootstrap"] else ""
        lines.append(
            f"• <b>{row['role']}</b> {html.escape(who)} <code>{row['id']}</code>{tag}".replace(
                "  ", " "
            )
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("whoami"))
async def handle_whoami(message: Message, user_store: UserStore) -> None:
    user = message.from_user
    if user is None:
        return
    await message.answer(
        t("ui.whoami", id=user.id, role=user_store.role_of(user.id) or "none"),
        parse_mode=ParseMode.HTML,
    )
