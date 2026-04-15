# Evols — Claude Code Plugin

Eliminate handoff tax. The Evols plugin connects Claude Code to your team's shared knowledge graph, automatically loading context from past sessions and flagging when a teammate already solved what you're about to work on.

## What it does

| When | What happens |
|------|-------------|
| Session starts | Fetches the most relevant team knowledge entries for your current working directory and injects them as context |
| You submit a prompt | Checks if a teammate recently solved something similar — shows a preview with token savings estimate before Claude starts |
| Claude runs an expensive tool (Bash, WebFetch) | Checks if that sub-task was already done recently and injects a note into Claude's reasoning chain |
| Session ends | Reads exact token counts from the transcript, records quota usage, auto-syncs a knowledge entry via Haiku summarization |
| You call `sync_session_context` | Interactively summarize the session and optionally link it to a product in your knowledge base |

---

## Installation

Works identically in the **Claude Code CLI** and the **VSCode extension**.

**Step 1 — Add the Evols marketplace:**
```
/plugin marketplace add Evols-AI/evols-claude-plugin
```

**Step 2 — Install the plugin:**
```
/plugin install evols@Evols-AI-evols-claude-plugin
```

Claude Code will prompt you for three values:

| Field | Description |
|-------|-------------|
| **Evols API URL** | Your workspace URL — find it in your Evols dashboard |
| **Evols API Key** | From Settings → API Keys → New Key (starts with `evols_`). Stored in your system keychain — never written to disk in plain text. |
| **Claude plan** | `pro`, `max`, `team`, or `enterprise`. Defaults to `pro`. Used to calculate quota percentages in the session summary. |

**Step 3 — Activate:**
```
/reload-plugins
```

On the next session start, the plugin automatically creates a Python venv and installs its dependencies. This happens once and repeats only when dependencies change.

---

## User experience after installation

### CLI

Every session start prints a status line to stderr:

```
[Evols] 3 team knowledge entries loaded (1,240 tokens · ~8,680 saved vs. fresh)
```

Or, if no relevant context exists yet:
```
[Evols] Team knowledge graph active. No relevant context yet —
        use sync_session_context to add your first entry.
```

If your first prompt matches recent team work (≥75% similarity), you see a preview before Claude processes anything:

```
[Evols] Prior team work found (83% match)
------------------------------------------------------------
  "JWT auth flow with refresh token rotation"
  4h ago · ~2,400 tokens · ~2,260 tokens saved if reused
------------------------------------------------------------
We implemented JWT auth using PyJWT with a 15-minute access
token and 7-day refresh token. The refresh endpoint is at
/api/v1/auth/refresh and rotates the refresh token on use...
------------------------------------------------------------
Continuing with your prompt. Reference the above if it covers your need.
To abort: Ctrl+C
```

At session end, a summary prints automatically:

```
[Evols] Session complete
  Input tokens:   18,430
  Output tokens:  4,210
  Cache reads:    6,800  (discounted)
  Session total:  22,640  (25.7% of pro daily quota)
  Team context:   1,240 tokens retrieved · ~8,680 saved vs. compiling fresh
  Knowledge sync: entry #47 added to team graph automatically
```

### VSCode extension

The experience is identical to CLI. The plugin system runs the same hooks in both environments.

- **Session start**: hook output appears as a system message at the top of the chat
- **Prompt submit**: redundancy warnings appear as system messages before Claude's first response
- **MCP tools**: `get_team_context`, `sync_session_context`, and `get_quota_status` appear in Claude's available tools — Claude can call them, or you can ask Claude to call them directly
- **Session end**: the Stop hook summary appears as a system message after Claude's last response

### MCP tools (available in both CLI and VSCode)

| Tool | What it does |
|------|-------------|
| `get_team_context` | Fetch the most relevant knowledge entries for a query. Claude uses this automatically when context might help. |
| `sync_session_context` | Summarize the current session and add it to the team graph. Prompts you to optionally link the entry to a product. |
| `get_quota_status` | Show token usage stats and knowledge graph summary for your team. |

Example: ask Claude to sync after a productive session:
```
sync this session to the team knowledge base
```
Claude calls `sync_session_context`, adds the entry, then asks which product (if any) to link it to.

---

## Updating

Plugin updates happen automatically if auto-update is enabled (default for marketplace plugins). To update manually:

```
/plugin update evols@Evols-AI-evols-claude-plugin
/reload-plugins
```

---

## Configuration

All configuration is set at install time via `userConfig` prompts. To change a value after installation, uninstall and reinstall:

```
/plugin uninstall evols@Evols-AI-evols-claude-plugin
/plugin install evols@Evols-AI-evols-claude-plugin
```

The API key (`EVOLS_API_KEY`) is stored in your system keychain. On macOS this is Keychain Access; on Linux it falls back to `~/.claude/.credentials.json`.

> **Note on JWT tokens**: If you copy a JWT from your browser session instead of generating a proper API key, it will expire in 24 hours and hooks will silently fail. Always use a long-lived `evols_...` key from Settings → API Keys → New Key.

---

## How it works

### Hooks

The plugin registers five [Claude Code hooks](https://code.claude.com/docs/en/hooks):

| Hook | Trigger | What Evols does |
|------|---------|-----------------|
| `SessionStart` | New session begins | Sets up Python venv (first run only), fetches relevant team context, initializes session state |
| `UserPromptSubmit` | Before Claude processes your prompt | On first prompt: redundancy check against last 48h of team work. Shows preview — no tokens spent. |
| `PostToolUse` | After Bash or WebFetch tool completes | Redundancy check for the sub-task. Injects result into Claude's reasoning chain via `additionalContext` (Claude sees it, terminal does not). Captures notable outputs for end-of-session sync. |
| `Stop` | Session ends normally | Reads exact token counts from transcript JSONL, records quota event, auto-syncs knowledge via Haiku, prints summary |
| `StopFailure` | Session ends due to rate limit | Same as Stop, records as `rate_limit_hit` event type |

Hooks use only Python stdlib (`json`, `urllib`, `os`) — no pip dependencies required.

### MCP server

The MCP server (`plugin/mcp_server/server.py`) runs as a subprocess managed by Claude Code. It uses the `mcp` Python library and communicates with the Evols backend API. Its Python environment is a venv at `~/.claude/plugins/data/evols-*/venv/`, created automatically on first session start.

### Session state

Hooks share state via `~/.evols/session_state.json`. This file is created on session start and cleaned up on session end. It tracks: session ID, working directory, tokens retrieved from the knowledge graph, and notable tool outputs collected during the session.

---

## Directory structure

```
evols-claude-plugin/
├── .claude-plugin/
│   ├── plugin.json           # Marketplace manifest + hook/MCP config
│   └── marketplace.json      # Marketplace catalog
├── .mcp.json                 # MCP server config
├── plugin/
│   ├── hooks/
│   │   ├── session_start.py      # SessionStart hook
│   │   ├── user_prompt_submit.py # UserPromptSubmit hook
│   │   ├── post_tool_use.py      # PostToolUse hook
│   │   └── stop.py               # Stop + StopFailure hook
│   └── mcp_server/
│       ├── server.py             # MCP server (get_team_context, sync_session_context, get_quota_status)
│       └── requirements.txt      # mcp, requests
└── README.md
```

---

## Submitting to the official Anthropic marketplace

To make the plugin discoverable at `/plugin install evols@claude-plugins-official`:

- Claude.ai: [claude.ai/settings/plugins/submit](https://claude.ai/settings/plugins/submit)
- Console: [platform.claude.com/plugins/submit](https://platform.claude.com/plugins/submit)

Until then, users add the Evols marketplace directly:
```
/plugin marketplace add Evols-AI/evols-claude-plugin
```
