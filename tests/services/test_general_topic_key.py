"""General-chat (thread_id=None → GENERAL_TOPIC_KEY) config support."""

from __future__ import annotations

import asyncio
import json

from telegram_bot.core.services.topic_config import (
    GENERAL_TOPIC_KEY,
    TopicConfig,
    normalize_thread_id,
)


def test_normalize_thread_id_maps_none_to_general_key() -> None:
    assert normalize_thread_id(None) == GENERAL_TOPIC_KEY
    assert normalize_thread_id(463) == 463


def test_get_topic_none_reads_general_entry(tmp_path) -> None:
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps({"topics": {"0": {"engine": "codex", "stream_mode": "minimal"}}}),
        encoding="utf-8",
    )
    config = TopicConfig(str(config_path), ".")
    settings = config.get_topic(None)
    assert settings.engine == "codex"
    assert settings.stream_mode == "minimal"


def test_get_topic_none_defaults_without_general_entry(tmp_path) -> None:
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(json.dumps({"topics": {}}), encoding="utf-8")
    config = TopicConfig(str(config_path), ".")
    settings = config.get_topic(None)
    assert settings.engine == "claude"
    assert not config.has_topic(None)


def test_engine_write_under_general_key_roundtrips(tmp_path) -> None:
    config_path = tmp_path / "topic_config.json"
    config = TopicConfig(str(config_path), ".")

    ok = asyncio.run(
        config.update_engine_model(normalize_thread_id(None), "codex", None)
    )
    assert ok

    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["topics"]["0"]["engine"] == "codex"

    # Read path honors the runtime override for the General chat.
    assert config.get_topic(None).engine == "codex"
    assert config.has_topic(None)


def test_general_and_forum_entries_are_independent(tmp_path) -> None:
    config_path = tmp_path / "topic_config.json"
    config = TopicConfig(str(config_path), ".")

    assert asyncio.run(config.update_engine(normalize_thread_id(None), "codex"))
    assert asyncio.run(config.update_engine(463, "claude"))

    assert config.get_topic(None).engine == "codex"
    assert config.get_topic(463).engine == "claude"
