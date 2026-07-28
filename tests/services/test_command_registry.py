"""CommandRegistry — discovery, Telegram aliasing, canonicalization, caps."""

from __future__ import annotations

from pathlib import Path

from telegram_bot.core.services.command_registry import CommandRegistry


def _make_home(tmp_path: Path) -> Path:
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    return tmp_path


def _write_command(home: Path, name: str, description: str) -> None:
    (home / ".claude" / "commands" / f"{name}.md").write_text(
        f"---\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _write_block_command(home: Path, name: str, first_line: str) -> None:
    (home / ".claude" / "commands" / f"{name}.md").write_text(
        f"---\ndescription: |\n  {first_line}\n\n  Use when: whatever\n---\n",
        encoding="utf-8",
    )


def _write_skill(home: Path, name: str, description: str) -> None:
    skill_dir = home / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n",
        encoding="utf-8",
    )


def test_scan_discovers_commands_and_skills(tmp_path) -> None:
    home = _make_home(tmp_path)
    _write_command(home, "do-task", "Execute a task")
    _write_skill(home, "tech-spec-planning", "Plan a tech spec")

    registry = CommandRegistry(home)
    assert registry.scan() == 2

    aliases = {c.alias: c for c in registry.commands}
    assert aliases["do_task"].canonical == "do-task"
    assert aliases["do_task"].description == "Execute a task"
    assert aliases["tech_spec_planning"].canonical == "tech-spec-planning"
    assert aliases["tech_spec_planning"].source == "skill"


def test_scan_block_scalar_description(tmp_path) -> None:
    home = _make_home(tmp_path)
    _write_block_command(home, "do-feature", "Execute feature with a team of agents.")

    registry = CommandRegistry(home)
    registry.scan()
    (cmd,) = registry.commands
    assert cmd.description == "Execute feature with a team of agents."


def test_dash_canonical_wins_over_underscore_twin(tmp_path) -> None:
    home = _make_home(tmp_path)
    _write_command(home, "do-task", "Dash spelling")
    _write_command(home, "do_task", "Underscore twin")

    registry = CommandRegistry(home)
    assert registry.scan() == 1
    (cmd,) = registry.commands
    assert cmd.canonical == "do-task"


def test_command_file_wins_over_skill(tmp_path) -> None:
    home = _make_home(tmp_path)
    _write_command(home, "code-reviewing", "Command entry point")
    _write_skill(home, "code-reviewing", "Skill entry")

    registry = CommandRegistry(home)
    assert registry.scan() == 1
    (cmd,) = registry.commands
    assert cmd.source == "command"


def test_canonicalize_rewrites_alias_and_keeps_args(tmp_path) -> None:
    home = _make_home(tmp_path)
    _write_command(home, "do-task", "Execute a task")
    registry = CommandRegistry(home)
    registry.scan()

    assert registry.canonicalize("/do_task 05 quickly") == "/do-task 05 quickly"
    # @botname mention from group autocomplete is stripped before lookup.
    assert registry.canonicalize("/do_task@fabric_bot 05") == "/do-task 05"
    # Unknown commands and plain text pass through untouched.
    assert registry.canonicalize("/model sonnet") == "/model sonnet"
    assert registry.canonicalize("hello") == "hello"


def test_canonicalize_noop_when_canonical_is_already_underscored(tmp_path) -> None:
    home = _make_home(tmp_path)
    _write_command(home, "done", "Finalize a feature")
    registry = CommandRegistry(home)
    registry.scan()
    assert registry.canonicalize("/done") == "/done"


def test_invalid_names_are_dropped_and_reported(tmp_path) -> None:
    home = _make_home(tmp_path)
    # 40 chars — exceeds Telegram's 32-char command limit.
    _write_command(home, "x" * 40, "Too long")
    _write_command(home, "ok-cmd", "Fine")

    registry = CommandRegistry(home)
    assert registry.scan() == 1
    assert registry.dropped == ("x" * 40,)


def test_localized_commands_respects_telegram_cap(tmp_path) -> None:
    home = _make_home(tmp_path)
    for i in range(30):
        _write_command(home, f"cmd-{i:02d}", f"Command {i}")

    registry = CommandRegistry(home)
    registry.scan()
    entries = registry.localized_commands(reserved_slots=95)
    assert len(entries) == 5
    # Everything beyond the budget is reported as dropped, not silently lost.
    assert len(registry.dropped) == 25


def test_scan_missing_home_is_empty_not_crash(tmp_path) -> None:
    registry = CommandRegistry(tmp_path / "nonexistent")
    assert registry.scan() == 0
    assert registry.canonicalize("/do_task x") == "/do_task x"
