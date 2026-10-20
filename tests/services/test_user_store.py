from __future__ import annotations

from pathlib import Path

import pytest

from telegram_bot.core.services.user_store import (
    BootstrapOperatorError,
    UserStore,
    parse_role,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _store(tmp_path: Path, clock: _Clock | None = None, boot: tuple[int, ...] = (1,)) -> UserStore:
    return UserStore(tmp_path / "users.json", boot, invite_ttl_hours=1, clock=clock or _Clock())


def test_bootstrap_operator_and_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.role_of(1) == "operator"
    assert store.role_of(2) is None


def test_grant_persists_across_restart(tmp_path: Path) -> None:
    _store(tmp_path).grant(2, "member", by=1, username="Bob")
    again = _store(tmp_path)
    assert again.role_of(2) == "member"
    assert again.resolve_username("@bob") == 2


def test_bootstrap_cannot_be_changed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(BootstrapOperatorError):
        store.grant(1, "viewer", by=1)
    with pytest.raises(BootstrapOperatorError):
        store.revoke(1)


def test_revoke(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.grant(2, "member", by=1)
    assert store.revoke(2) is True
    assert store.role_of(2) is None
    assert store.revoke(2) is False


def test_invite_is_single_use(tmp_path: Path) -> None:
    store = _store(tmp_path)
    code = store.create_invite("member", by=1)
    assert store.redeem(code, 5, username="eve") == ("member", 1)
    assert store.role_of(5) == "member"
    assert store.redeem(code, 6) is None
    assert store.role_of(6) is None


def test_invite_expires(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    code = store.create_invite("member", by=1)
    clock.now += 3601
    assert store.redeem(code, 5) is None


def test_invite_code_is_not_stored_in_clear(tmp_path: Path) -> None:
    store = _store(tmp_path)
    code = store.create_invite("viewer", by=1)
    assert code not in (tmp_path / "users.json").read_text()


def test_invite_never_demotes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.grant(5, "operator", by=1)
    code = store.create_invite("viewer", by=1)
    assert store.redeem(code, 5) == ("operator", 1)
    assert store.role_of(5) == "operator"


def test_unknown_code_rejected(tmp_path: Path) -> None:
    assert _store(tmp_path).redeem("inv_nope", 5) is None


def test_corrupt_file_starts_empty(tmp_path: Path) -> None:
    (tmp_path / "users.json").write_text("{broken")
    assert _store(tmp_path).role_of(2) is None


def test_file_is_private(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.grant(2, "member", by=1)
    assert (tmp_path / "users.json").stat().st_mode & 0o077 == 0


def test_parse_role() -> None:
    assert parse_role(" Member ") == "member"
    assert parse_role("admin") is None
