"""Claude Code modes and their system prompts / tool whitelists.

Extracted from `claude.py`. The public core ships generic tools only;
assistant-specific MCP tools are attached at runtime via
`SessionManager.extend_mode_tools`.

Prompts live in `src/telegram_bot/prompts/<mode>.md` and are re-read
per-call with an mtime cache, so a prompt change lands without a bot
restart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

_MODE_PROMPT_FALLBACKS: dict[str, tuple[str, ...]] = {
    "task": ("task.md", "task-manager.md", "default.md"),
    "knowledge": ("knowledge.md", "default.md"),
    "free": ("free.md", "default.md"),
    "project": ("project.md", "default.md"),
    "blog": ("blog.md", "default.md"),
}


def _read_prompt_with_fallback(mode: str) -> str:
    """Read the mode prompt, falling back to public generic prompt names."""
    for filename in _MODE_PROMPT_FALLBACKS[mode]:
        path = _PROMPTS_DIR / filename
        try:
            return path.read_text()
        except FileNotFoundError:
            continue
    return ""


# Preload prompts once at import so the first `_get_mode_prompt` call is
# cheap. Public builds may ship only default.md and task-manager.md.
TASK_MODE_PROMPT = _read_prompt_with_fallback("task")
KNOWLEDGE_MODE_PROMPT = _read_prompt_with_fallback("knowledge")
FREE_MODE_PROMPT = _read_prompt_with_fallback("free")
PROJECT_MODE_PROMPT = _read_prompt_with_fallback("project")
BLOG_MODE_PROMPT = _read_prompt_with_fallback("blog")

Mode = Literal["task", "knowledge", "free", "project", "blog"]

# Default mode used when no per-topic mode is configured (auto-registered topics).
DEFAULT_MODE: Mode = "free"

# --- Allowed tools per mode ---
#
# Core keeps only generic tools. Assistant-specific MCP tools are attached
# by the private entry point via SessionManager.extend_mode_tools.

_BOT_MCP_TOOLS = "mcp__bot__send_message,mcp__bot__send_image,mcp__bot__send_document"

# Allow the entire Kaneo MCP server's toolset (whoami, list_workspaces,
# create_task, update_task_status, move_task, create_task_comment, etc. —
# 18 tools per apps/api/src/mcp/tools.ts in usekaneo/kaneo). Kaneo's MCP
# server is only spawned when KANEO_MCP_URL + KANEO_MCP_BEARER are set in
# the bot env (see bot_mcp_runtime._kaneo_server_from_env); when unset
# the tools simply won't be available at session-start time, so leaving
# this in every mode is safe for bots that haven't been paired.
_KANEO_MCP_TOOLS = "mcp__kaneo"
_GITHUB_MCP_TOOLS = "mcp__github"
_COMMON = f"Skill,{_BOT_MCP_TOOLS},{_KANEO_MCP_TOOLS},{_GITHUB_MCP_TOOLS}"
_RW = "Read,Write,Edit,Grep,Glob,Bash,Agent"

TASK_MODE_TOOLS = f"{_COMMON},Read,Grep,Glob,Bash,Agent"
KNOWLEDGE_MODE_TOOLS = f"{_COMMON},{_RW}"
FREE_MODE_TOOLS = f"{_COMMON},{_RW}"
PROJECT_MODE_TOOLS = f"{_BOT_MCP_TOOLS},{_KANEO_MCP_TOOLS},{_GITHUB_MCP_TOOLS},{_RW},Skill"
BLOG_MODE_TOOLS = f"{_COMMON},{_RW}"

_MODE_TOOLS: dict[str, str] = {
    "task": TASK_MODE_TOOLS,
    "knowledge": KNOWLEDGE_MODE_TOOLS,
    "free": FREE_MODE_TOOLS,
    "project": PROJECT_MODE_TOOLS,
    "blog": BLOG_MODE_TOOLS,
}

# prompts/ content is looked up per call with an mtime-cached scan, so a new
# prompts/<mode>.md dropped at runtime is picked up without a bot restart.
_mode_prompts_cache: tuple[int, dict[str, str]] = (-1, {})


def _get_mode_prompt(mode: Mode) -> str:
    """Return the system prompt for a mode; fall back to 'free' if missing."""
    global _mode_prompts_cache
    try:
        mtime = _PROMPTS_DIR.stat().st_mtime_ns
    except OSError:
        prompts = _mode_prompts_cache[1]
    else:
        if mtime != _mode_prompts_cache[0]:
            _mode_prompts_cache = (
                mtime,
                {p.stem: p.read_text() for p in _PROMPTS_DIR.glob("*.md")},
            )
        prompts = _mode_prompts_cache[1]
    if mode in prompts:
        return prompts[mode]
    if mode == "task" and "task-manager" in prompts:
        return prompts["task-manager"]
    return prompts.get("free", prompts.get("default", ""))
