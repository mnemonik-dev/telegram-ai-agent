# Architecture

The bot runtime is split into:

- `src/telegram_bot/__main__.py` - public entry point, aiogram wiring, shutdown.
- `src/telegram_bot/core/handlers/` - Telegram command, text, media, voice, forward, topic, and TUI handlers.
- `src/telegram_bot/core/services/` - session management, provider adapters, topic config, streaming, tmux, resume, MCP runtime, and transcription.
- `src/telegram_bot/core/tui/` - tmux TUI capture, modal detection, keyboard controls, routing, and transcript helpers.
- `mcp-servers/bot/` - MCP server that lets an agent send messages or files back to Telegram.
- `src/telegram_bot/prompts/` - generic public prompt modes.

Two independent runtime axes are important:

- `engine`: `claude` or `codex`.
- `exec_mode`: `subprocess` or `tmux`.

Engine selection is availability-aware: Claude Code is preferred by default,
Codex is used when Claude Code is missing and Codex exists, and the bot remains
online with a user-facing install message when neither CLI is available.

`stream_mode` controls Telegram progress delivery:

- `verbose`: separate progress messages.
- `live`: editable progress buffer plus final answer.
- `minimal`: final-answer oriented delivery.

Forum topics are isolated by `(chat_id, thread_id)`. Session mappings and tmux
state are runtime files and must not be committed.

Per-task dynamic cwd resolution is implemented by two cooperating services
under `core/services/`:

- `workspace_resolver.py`: async `httpx` client `WorkspaceResolver` that
  speaks the lease/release HTTP contract (`POST /worktree`,
  `DELETE /worktree/{task_id}`). Carries no Mnemonic-specific logic, retries
  `5xx`/network errors with exponential backoff, and surfaces capacity and
  unreachable errors as typed exceptions.
- `dynamic_cwd.py`: `dynamic_cwd_lease` async context manager that leases a
  cwd at the start of an engine invocation and releases it in `finally`,
  guaranteeing pool return on normal exit, `/cancel`, timeout, and crash.
  Also exports `MessageContext`, `build_task_id()`, and
  `check_dynamic_cwd_preconditions()` for fail-fast config validation.

The composition root in `src/telegram_bot/__main__.py` instantiates the
resolver at startup via `resolver_from_settings(settings)` and wraps
`send_streaming_response` inside `dynamic_cwd_lease` in `process_queue_item`.
Topics with a static `cwd` skip the lease path entirely. The resolver URL
comes from `Settings.workspace_resolver_url`, populated from
`TELEGRAM_AI_AGENT_CWD_RESOLVER_URL`. End-user documentation for the feature
lives in the README "Dynamic Cwd Resolution" section; the configuration
reference in `configuration.md` lists the env var and the topic-level
sentinel.
