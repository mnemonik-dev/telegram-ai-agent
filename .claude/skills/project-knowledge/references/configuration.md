# Configuration

Environment is loaded by `telegram_bot.core.config.Settings`.

Required:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_USER_IDS`

Common optional settings:

- `BOT_LANG`: `en` or `ru`.
- `DEFAULT_CWD`: default working directory for new topics; public default is `.`.
- `FILE_CACHE_DIR`: downloaded media cache.
- `TOPIC_CONFIG_PATH`: defaults to `./topic_config.json`.
- `TMUX_SESSIONS_DIR`: defaults to `./tmux_sessions`.
- `DEEPGRAM_API_KEY`: enables voice transcription.
- `TELEGRAM_AI_AGENT_CWD_RESOLVER_URL`: HTTP resolver base URL for dynamic
  per-task cwd. Required when any topic uses `cwd: "DYNAMIC"`; unused
  otherwise. Mapped to `Settings.workspace_resolver_url`.

`topic_config.example.json` is public-safe and can be copied to
`topic_config.json`. The real `topic_config.json` is runtime config and must not
be committed.

Topic fields:

- `name`: human-readable topic label.
- `type`: `assistant` or `project`.
- `mode`: public prompt mode. `free` is the standard project/general prompt.
  `task` is a replaceable example of a second prompt mode.
- `cwd`: absolute project path, `null` for `DEFAULT_CWD`, or the sentinel
  string `"DYNAMIC"` to lease a fresh per-message workspace from an external
  HTTP resolver. The `DYNAMIC` sentinel sets the derived
  `TopicSettings.dynamic_cwd` flag (used by `dynamic_cwd_lease` and
  `check_dynamic_cwd_preconditions`); see the README "Dynamic Cwd Resolution"
  section for the setup walkthrough and HTTP contract. `DYNAMIC` requires
  `exec_mode: "subprocess"` and a configured
  `TELEGRAM_AI_AGENT_CWD_RESOLVER_URL`.
- `mcp_config`: absolute MCP config path or `null` for bot-generated config.
- `stream_mode`: `verbose`, `live`, or `minimal`.
- `exec_mode`: `subprocess` or `tmux`.
- `engine`: `claude` or `codex`.
- `model`: optional model override.

Runtime prefers Claude Code when both engines are available. If a topic is
configured for a missing engine and the other CLI is installed, the bot switches
the topic to the available engine, persists that change, resets the active
session id, and posts the same "engine changed, active session was reset" notice
to the topic that a manual `/engine` switch would. The conversation never moves
to a new provider silently. If neither CLI is installed, the bot still starts
and tells the user to install Claude Code or Codex.

Voice transcription requires a Deepgram API key in `DEEPGRAM_API_KEY`; leave it
empty to disable voice messages.

Never commit `.env`, `.mcp*.json`, session JSON files, `tmux_sessions/`,
`data/`, virtual environments, Python caches, or test/lint/typecheck caches.

For end-user installation and service setup, use `bot-setup`. For forum topic
creation and project wiring, use `topic-setup`. Both skills must exist in
`.claude/skills/` and `.codex/skills/`.
