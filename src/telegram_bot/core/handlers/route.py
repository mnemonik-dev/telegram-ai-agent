"""/route — view and change which engine/model each Symphony stage uses.

Talks to Symphony's ``/routing`` API at ``Settings.workspace_resolver_url``
(the workspace-manager base URL). Changes apply to the next dispatch with no
redeploy. Operator-only (gated in AuthMiddleware and re-checked here).
"""

from __future__ import annotations

import html
import logging
from typing import Any

import httpx
from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from telegram_bot.core.config import Settings
from telegram_bot.core.messages import t
from telegram_bot.core.services.user_store import UserStore

logger = logging.getLogger(__name__)

router = Router(name="route")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10)


async def _call(settings: Settings, method: str, path: str, **kwargs: Any) -> httpx.Response:
    base = (settings.workspace_resolver_url or "").rstrip("/")
    async with _client() as client:
        return await client.request(method, f"{base}{path}", **kwargs)


def _render(data: dict[str, Any]) -> str:
    stages: dict[str, dict[str, str]] = data.get("stages", {})
    engines = ", ".join(data.get("engines", [])) if "engines" in data else ""
    lines = [t("ui.route_header")]
    if stages:
        for stage, entry in sorted(stages.items()):
            parts = " ".join(f"{k}=<code>{html.escape(v)}</code>" for k, v in entry.items())
            lines.append(f"• <b>{html.escape(stage)}</b> {parts}")
    else:
        lines.append(t("ui.route_empty"))
    if engines:
        lines.append(t("ui.route_engines", engines=html.escape(engines)))
    return "\n".join(lines)


@router.message(Command("route"))
async def handle_route(
    message: Message, command: CommandObject, settings: Settings, user_store: UserStore
) -> None:
    if message.from_user is None or user_store.role_of(message.from_user.id) != "operator":
        return
    if not settings.workspace_resolver_url:
        await message.answer(t("ui.route_no_symphony"))
        return
    args = (command.args or "").split()
    try:
        if not args:
            resp = await _call(settings, "GET", "/routing")
        elif len(args) == 2 and args[1].lower() == "reset":
            resp = await _call(settings, "DELETE", f"/routing/{args[0].lower()}")
        elif len(args) == 3 and args[1].lower() in ("engine", "model"):
            resp = await _call(
                settings, "PUT", f"/routing/{args[0].lower()}", json={args[1].lower(): args[2]}
            )
        else:
            await message.answer(t("ui.route_usage"), parse_mode=ParseMode.HTML)
            return
    except httpx.HTTPError:
        logger.warning("route: Symphony unreachable", exc_info=True)
        await message.answer(t("ui.route_unreachable"))
        return
    if resp.status_code == 422:
        detail = resp.json().get("detail", "invalid")
        await message.answer(
            t("ui.route_rejected", detail=html.escape(str(detail))), parse_mode=ParseMode.HTML
        )
        return
    if resp.status_code >= 400:
        await message.answer(t("ui.route_unreachable"))
        return
    await message.answer(_render(resp.json()), parse_mode=ParseMode.HTML)
