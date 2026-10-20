"""UserStore — runtime-editable users, roles and one-time invites.

Roles (low -> high): viewer, member, operator.

* operator — everything, including /invite /grant /revoke /users.
* member   — talks to the engine in chats and topics.
* viewer   — read-only: only /whoami, /help, /start.

Users in ``Settings.allowed_user_ids`` are *bootstrap operators*: always
operators, never stored and never revocable at runtime (change the config
and restart to alter them). Everyone else lives in a small JSON file so
roles change instantly, without a redeploy.

The Bot API cannot resolve an @username to an id, so the store also keeps a
username -> id cache of everyone the bot has seen in a chat; ``/grant
@name`` works for people who have written in (or joined) a chat the bot
is in. Invites cover everyone else: a one-time deep link whose code is
stored only as a hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

Role = Literal["viewer", "member", "operator"]
ROLES: tuple[Role, ...] = ("viewer", "member", "operator")
INVITE_PREFIX = "inv_"
_SEEN_LIMIT = 5000


class BootstrapOperatorError(Exception):
    """Raised when someone tries to change a config-defined operator."""


def parse_role(raw: str) -> Role | None:
    value = raw.strip().lower()
    for role in ROLES:
        if role == value:
            return role
    return None


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class UserStore:
    def __init__(
        self,
        path: Path,
        bootstrap_operators: Iterable[int] = (),
        *,
        invite_ttl_hours: int = 48,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._bootstrap = frozenset(int(i) for i in bootstrap_operators)
        self._ttl = invite_ttl_hours * 3600
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._load()

    # ----- persistence -------------------------------------------------

    def _load(self) -> dict[str, Any]:
        data: Any = {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            logger.exception("users file %s unreadable; starting empty", self._path)
        if not isinstance(data, dict):
            data = {}
        for key in ("users", "invites", "seen"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        loaded: dict[str, Any] = data
        return loaded

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)

    # ----- roles -------------------------------------------------------

    def is_bootstrap(self, user_id: int) -> bool:
        return user_id in self._bootstrap

    def role_of(self, user_id: int) -> Role | None:
        if user_id in self._bootstrap:
            return "operator"
        with self._lock:
            entry = self._data["users"].get(str(user_id))
        role = entry.get("role") if isinstance(entry, dict) else None
        return role if role in ROLES else None

    def grant(
        self,
        user_id: int,
        role: Role,
        *,
        by: int,
        username: str | None = None,
        name: str | None = None,
    ) -> None:
        if user_id in self._bootstrap:
            raise BootstrapOperatorError(str(user_id))
        with self._lock:
            prev = self._data["users"].get(str(user_id)) or {}
            self._data["users"][str(user_id)] = {
                "role": role,
                "username": username or prev.get("username"),
                "name": name or prev.get("name"),
                "added_by": by,
                "added_at": int(self._clock()),
            }
            if username:
                self._data["seen"][username.lower()] = user_id
            self._save()

    def revoke(self, user_id: int) -> bool:
        if user_id in self._bootstrap:
            raise BootstrapOperatorError(str(user_id))
        with self._lock:
            if self._data["users"].pop(str(user_id), None) is None:
                return False
            self._save()
            return True

    def list_users(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [
            {"id": uid, "role": "operator", "username": None, "bootstrap": True}
            for uid in sorted(self._bootstrap)
        ]
        with self._lock:
            for key, entry in self._data["users"].items():
                rows.append(
                    {
                        "id": int(key),
                        "role": entry.get("role"),
                        "username": entry.get("username"),
                        "bootstrap": False,
                    }
                )
        return rows

    # ----- username cache ----------------------------------------------

    def note_seen(self, user_id: int, username: str | None) -> None:
        if not username:
            return
        key = username.lower()
        with self._lock:
            seen = self._data["seen"]
            if seen.get(key) == user_id:
                return
            seen.pop(key, None)
            seen[key] = user_id
            while len(seen) > _SEEN_LIMIT:
                seen.pop(next(iter(seen)))
            # Keep stored users' display username fresh.
            entry = self._data["users"].get(str(user_id))
            if isinstance(entry, dict):
                entry["username"] = username
            try:
                self._save()
            except OSError:
                logger.exception("cannot write users file %s", self._path)

    def resolve_username(self, username: str) -> int | None:
        with self._lock:
            uid = self._data["seen"].get(username.lstrip("@").lower())
        return int(uid) if uid is not None else None

    # ----- invites -----------------------------------------------------

    def create_invite(self, role: Role, *, by: int) -> str:
        code = INVITE_PREFIX + secrets.token_urlsafe(9)
        with self._lock:
            now = self._clock()
            invites = self._data["invites"]
            for key in [k for k, v in invites.items() if v.get("expires_at", 0) < now]:
                del invites[key]
            invites[_hash(code)] = {
                "role": role,
                "created_by": by,
                "expires_at": int(now + self._ttl),
            }
            self._save()
        return code

    def redeem(
        self,
        code: str,
        user_id: int,
        *,
        username: str | None = None,
        name: str | None = None,
    ) -> tuple[Role, int] | None:
        """Consume an invite. Returns (role, created_by) or None if invalid."""
        with self._lock:
            invite = self._data["invites"].pop(_hash(code), None)
            if invite is None or invite.get("expires_at", 0) < self._clock():
                if invite is not None:
                    self._save()
                return None
            role: Role = "operator" if user_id in self._bootstrap else invite["role"]
            if user_id not in self._bootstrap:
                current = self._data["users"].get(str(user_id)) or {}
                # An invite never demotes someone who already has a higher role.
                if current.get("role") in ROLES and ROLES.index(current["role"]) > ROLES.index(
                    role
                ):
                    role = current["role"]
                self._data["users"][str(user_id)] = {
                    "role": role,
                    "username": username,
                    "name": name,
                    "added_by": invite["created_by"],
                    "added_at": int(self._clock()),
                }
            if username:
                self._data["seen"][username.lower()] = user_id
            self._save()
            return role, int(invite["created_by"])
