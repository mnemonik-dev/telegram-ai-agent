"""Tests for Settings.workspace_resolver_url field and env-var mapping.

Verifies:
- Direct field assignment works.
- Env var TELEGRAM_AI_AGENT_CWD_RESOLVER_URL is picked up by pydantic-settings.
- Field defaults to None when env var is absent.
- URL scheme validator rejects non-http/https schemes (T07 F2).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from telegram_bot.core.config import Settings

# Minimal required field that Settings enforces.
_REQUIRED = {"telegram_bot_token": "test-token"}


# ---------------------------------------------------------------------------
# Direct field assignment
# ---------------------------------------------------------------------------


def test_workspace_resolver_url_direct_assignment() -> None:
    """workspace_resolver_url can be set directly when constructing Settings."""
    settings = Settings(
        _env_file=None,
        workspace_resolver_url="http://127.0.0.1:8765",
        **_REQUIRED,
    )
    assert settings.workspace_resolver_url == "http://127.0.0.1:8765"


def test_workspace_resolver_url_defaults_to_none() -> None:
    """workspace_resolver_url is None when not provided and env var is absent."""
    settings = Settings(_env_file=None, **_REQUIRED)
    assert settings.workspace_resolver_url is None


# ---------------------------------------------------------------------------
# Env-var loading
# ---------------------------------------------------------------------------


def test_workspace_resolver_url_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """TELEGRAM_AI_AGENT_CWD_RESOLVER_URL is loaded from the environment."""
    monkeypatch.setenv("TELEGRAM_AI_AGENT_CWD_RESOLVER_URL", "http://resolver.internal:9000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")

    settings = Settings(_env_file=None)

    assert settings.workspace_resolver_url == "http://resolver.internal:9000"


def test_workspace_resolver_url_env_var_unset_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When TELEGRAM_AI_AGENT_CWD_RESOLVER_URL is unset, field is None."""
    monkeypatch.delenv("TELEGRAM_AI_AGENT_CWD_RESOLVER_URL", raising=False)

    settings = Settings(_env_file=None, **_REQUIRED)

    assert settings.workspace_resolver_url is None


def test_workspace_resolver_url_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var takes precedence over the field's default None."""
    monkeypatch.setenv("TELEGRAM_AI_AGENT_CWD_RESOLVER_URL", "http://10.0.0.1:8765")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")

    settings = Settings(_env_file=None)

    assert settings.workspace_resolver_url == "http://10.0.0.1:8765"
    assert settings.workspace_resolver_url is not None


# ---------------------------------------------------------------------------
# URL scheme validator (T07 F2) — accept http/https, reject everything else
# ---------------------------------------------------------------------------


def test_workspace_resolver_url_http_accepted() -> None:
    """http:// scheme is accepted."""
    settings = Settings(
        _env_file=None,
        workspace_resolver_url="http://127.0.0.1:8765",
        **_REQUIRED,
    )
    assert settings.workspace_resolver_url == "http://127.0.0.1:8765"


def test_workspace_resolver_url_https_accepted() -> None:
    """https:// scheme is accepted."""
    settings = Settings(
        _env_file=None,
        workspace_resolver_url="https://resolver.internal:8765",
        **_REQUIRED,
    )
    assert settings.workspace_resolver_url == "https://resolver.internal:8765"


def test_workspace_resolver_url_none_accepted() -> None:
    """None is accepted (resolver disabled)."""
    settings = Settings(_env_file=None, workspace_resolver_url=None, **_REQUIRED)
    assert settings.workspace_resolver_url is None


def test_workspace_resolver_url_file_scheme_rejected() -> None:
    """file:// scheme raises ValidationError with a clear message."""
    with pytest.raises(ValidationError, match="http"):
        Settings(
            _env_file=None,
            workspace_resolver_url="file:///etc/passwd",
            **_REQUIRED,
        )


def test_workspace_resolver_url_ftp_scheme_rejected() -> None:
    """ftp:// scheme raises ValidationError."""
    with pytest.raises(ValidationError, match="http"):
        Settings(
            _env_file=None,
            workspace_resolver_url="ftp://resolver.internal:21",
            **_REQUIRED,
        )


def test_workspace_resolver_url_unknown_scheme_rejected() -> None:
    """Arbitrary unknown scheme raises ValidationError."""
    with pytest.raises(ValidationError, match="http"):
        Settings(
            _env_file=None,
            workspace_resolver_url="unix:///var/run/resolver.sock",
            **_REQUIRED,
        )
