"""Tests for DYNAMIC cwd sentinel support in TopicConfig / TopicSettings."""

from __future__ import annotations

import json
import logging

from telegram_bot.core.services.topic_config import (
    DYNAMIC_CWD_SENTINEL,
    TopicConfig,
    TopicSettings,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(path, topic_dict: dict) -> None:
    """Write a minimal topic_config.json with one topic at thread_id 1."""
    path.write_text(
        json.dumps({"topics": {"1": topic_dict}}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _get_topic(tmp_path, topic_dict: dict) -> TopicSettings:
    config_path = tmp_path / "topic_config.json"
    _write_config(config_path, topic_dict)
    return TopicConfig(str(config_path), ".").get_topic(1)


# ---------------------------------------------------------------------------
# AC1 — DYNAMIC sentinel
# ---------------------------------------------------------------------------


def test_dynamic_cwd_recognized(tmp_path) -> None:
    """cwd: "DYNAMIC" produces TopicSettings(cwd=None, dynamic_cwd=True)."""
    topic = _get_topic(
        tmp_path,
        {"name": "T", "type": "assistant", "mode": "free", "cwd": "DYNAMIC"},
    )

    assert topic.dynamic_cwd is True
    assert topic.cwd is None


def test_dynamic_sentinel_constant_value() -> None:
    """DYNAMIC_CWD_SENTINEL is the exact string "DYNAMIC"."""
    assert DYNAMIC_CWD_SENTINEL == "DYNAMIC"


# ---------------------------------------------------------------------------
# Back-compat
# ---------------------------------------------------------------------------


def test_static_cwd_back_compat(tmp_path) -> None:
    """cwd: "/some/path" still produces TopicSettings(cwd=path, dynamic_cwd=False)."""
    project = tmp_path / "project"
    project.mkdir()

    topic = _get_topic(
        tmp_path,
        {"name": "T", "type": "assistant", "mode": "free", "cwd": str(project)},
    )

    assert topic.cwd == str(project)
    assert topic.dynamic_cwd is False


def test_none_cwd_back_compat(tmp_path) -> None:
    """Missing cwd field produces TopicSettings(cwd=None, dynamic_cwd=False)."""
    topic = _get_topic(tmp_path, {"name": "T", "type": "assistant", "mode": "free"})

    assert topic.cwd is None
    assert topic.dynamic_cwd is False


# ---------------------------------------------------------------------------
# Lowercase "dynamic" is NOT a sentinel
# ---------------------------------------------------------------------------


def test_dynamic_string_only_in_cwd(tmp_path, caplog) -> None:
    """cwd: "dynamic" (lowercase) is NOT the sentinel — treated as a path string.

    Because "dynamic" is not an absolute path it falls back to cwd=None with a
    warning, and dynamic_cwd remains False.
    """
    with caplog.at_level(logging.WARNING, logger="telegram_bot.core.services.topic_config"):
        topic = _get_topic(
            tmp_path,
            {"name": "T", "type": "assistant", "mode": "free", "cwd": "dynamic"},
        )

    assert topic.dynamic_cwd is False
    # Relative path → falls back to None with a warning logged
    assert topic.cwd is None
    assert any("Relative cwd" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Engine / exec_mode / stream_mode combinations with DYNAMIC
# ---------------------------------------------------------------------------


def test_dynamic_with_mode_combinations(tmp_path) -> None:
    """DYNAMIC cwd is compatible with all engine + stream_mode combinations."""
    combinations = [
        ("claude", "subprocess", "verbose"),
        ("claude", "subprocess", "live"),
        ("claude", "subprocess", "minimal"),
        ("codex", "subprocess", "verbose"),
        ("codex", "subprocess", "live"),
        ("codex", "subprocess", "minimal"),
    ]

    for engine, exec_mode, stream_mode in combinations:
        config_path = tmp_path / f"cfg_{engine}_{exec_mode}_{stream_mode}.json"
        config_path.write_text(
            json.dumps(
                {
                    "topics": {
                        "1": {
                            "name": "T",
                            "type": "assistant",
                            "mode": "free",
                            "cwd": "DYNAMIC",
                            "engine": engine,
                            "exec_mode": exec_mode,
                            "stream_mode": stream_mode,
                        }
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        topic = TopicConfig(str(config_path), ".").get_topic(1)

        assert topic.dynamic_cwd is True, f"Failed for {engine}/{exec_mode}/{stream_mode}"
        assert topic.cwd is None, f"cwd not None for {engine}/{exec_mode}/{stream_mode}"
        assert topic.engine == engine
        assert topic.exec_mode == exec_mode
        assert topic.stream_mode == stream_mode


# ---------------------------------------------------------------------------
# DYNAMIC + tmux warning (T03 deferred enforcement)
# ---------------------------------------------------------------------------


def test_dynamic_incompatible_with_tmux_warning(tmp_path, caplog) -> None:
    """cwd=DYNAMIC + exec_mode=tmux: config loads but a warning is emitted.

    Actual enforcement (refusing to spawn) is deferred to T03.  Here we only
    verify that the config layer documents the incompatibility via a log warning
    and that the resulting TopicSettings is still valid (dynamic_cwd=True).
    """
    with caplog.at_level(logging.WARNING, logger="telegram_bot.core.services.topic_config"):
        topic = _get_topic(
            tmp_path,
            {
                "name": "T",
                "type": "assistant",
                "mode": "free",
                "cwd": "DYNAMIC",
                "exec_mode": "tmux",
            },
        )

    assert topic.dynamic_cwd is True
    assert topic.cwd is None
    assert topic.exec_mode == "tmux"
    # A warning about the incompatible combination must be logged
    assert any("exec_mode=tmux" in r.message for r in caplog.records)
