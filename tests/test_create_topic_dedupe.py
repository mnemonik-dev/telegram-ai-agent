from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import pytest


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "bot_mcp_server_dedupe", Path("mcp-servers/bot/server.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}
    text = ""

    def json(self) -> dict:
        return {"ok": True, "result": {"message_thread_id": 77}}


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "topic_config.json"
    cfg.write_text(
        json.dumps(
            {"topics": {"0": {"name": "Alpha"}, "12": {"name": "Alpha"}, "13": {"name": "My  App"}}}
        )
    )
    monkeypatch.setenv("TOPIC_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    mod = _load()
    calls: list[dict] = []

    def fake_post(url: str, json: dict, timeout: int) -> _Resp:
        calls.append(json)
        return _Resp()

    monkeypatch.setattr(mod.httpx, "post", fake_post)
    mod.calls = calls
    return mod


def test_existing_name_is_reused(server) -> None:
    out = json.loads(server.create_topic("  my app "))
    assert out == {"thread_id": 13, "name": "my app", "existing": True}
    assert server.calls == []


def test_general_key_zero_is_never_returned(server) -> None:
    assert json.loads(server.create_topic("alpha"))["thread_id"] == 12


def test_new_name_creates(server) -> None:
    out = json.loads(server.create_topic("Brand New"))
    assert out == {"thread_id": 77, "name": "Brand New", "existing": False}
    assert len(server.calls) == 1


def test_force_creates_duplicate(server) -> None:
    out = json.loads(server.create_topic("Alpha", force=True))
    assert out["existing"] is False and len(server.calls) == 1


def test_missing_or_corrupt_config_falls_back_to_create(server, tmp_path: Path) -> None:
    Path(server._topic_config_path()).write_text("{nope")
    assert json.loads(server.create_topic("Alpha"))["existing"] is False
