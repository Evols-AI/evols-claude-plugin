#!/usr/bin/env python3
"""
Evols PostToolUse Hook
Runs after every tool call completes.
- For expensive tools (Bash, WebFetch): checks if a teammate already did similar work
  and injects a warning into Claude's reasoning chain via additionalContext
- Captures notable tool outputs to session_state.json for Stop hook auto-sync context
"""

import sys
import json
import os
import urllib.request
import urllib.parse
from pathlib import Path

EVOLS_DIR = Path.home() / ".evols"
CONFIG_FILE = EVOLS_DIR / "config.json"
SESSION_STATE_FILE = EVOLS_DIR / "session_state.json"

# Tools worth checking for redundancy — expensive operations a teammate may have already done
REDUNDANCY_CHECK_TOOLS = {"Bash", "WebFetch"}

# Tools whose outputs are worth capturing for knowledge sync context
KNOWLEDGE_TOOLS = {"Write", "Edit", "Bash", "WebFetch"}

# MCP tool prefix — any tool matching this is forwarded to LightRAG
MCP_TOOL_PREFIX = "mcp__"

# Minimum response length worth indexing (skip empty pings, tiny acks)
LIGHTRAG_MIN_LENGTH = 80

SIMILARITY_THRESHOLD = 0.75
LOOKBACK_HOURS = 48


def load_config():
    # Plugin marketplace sets CLAUDE_PLUGIN_OPTION_* vars; install.sh sets EVOLS_* directly
    api_url = os.environ.get("EVOLS_API_URL") or os.environ.get("CLAUDE_PLUGIN_OPTION_EVOLS_API_URL", "")
    api_key = os.environ.get("EVOLS_API_KEY") or os.environ.get("CLAUDE_PLUGIN_OPTION_EVOLS_API_KEY", "")
    plan_type = os.environ.get("EVOLS_PLAN") or os.environ.get("CLAUDE_PLUGIN_OPTION_EVOLS_PLAN", "")
    if api_url and api_key:
        return {"api_url": api_url, "api_key": api_key, "plan_type": plan_type or "pro"}
    if not CONFIG_FILE.exists():
        return None
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_lightrag_config() -> dict | None:
    """Load LightRAG connection details from env or ~/.evols/config.json."""
    url = os.environ.get("LIGHTRAG_URL") or os.environ.get("CLAUDE_PLUGIN_OPTION_LIGHTRAG_URL", "")
    api_key = os.environ.get("LIGHTRAG_API_KEY") or os.environ.get("CLAUDE_PLUGIN_OPTION_LIGHTRAG_API_KEY", "")
    if url:
        return {"url": url.rstrip("/"), "api_key": api_key}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            lr_url = cfg.get("lightrag_url", "")
            if lr_url:
                return {"url": lr_url.rstrip("/"), "api_key": cfg.get("lightrag_api_key", "")}
        except Exception:
            pass
    return None


def get_lightrag_jwt(lightrag_cfg: dict) -> str:
    """Exchange API key for a JWT via /login (form-encoded). Caches in session_state."""
    try:
        state_path = SESSION_STATE_FILE
        if state_path.exists():
            state = json.loads(state_path.read_text())
            cached = state.get("lightrag_jwt", "")
            if cached:
                return cached
        api_key = lightrag_cfg.get("api_key", "")
        if not api_key:
            return ""
        data = urllib.parse.urlencode({"username": "evols", "password": api_key}).encode()
        req = urllib.request.Request(
            f"{lightrag_cfg['url']}/login", data=data, method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=5)
        token = json.loads(resp.read()).get("access_token", "")
        if token and state_path.exists():
            state = json.loads(state_path.read_text())
            state["lightrag_jwt"] = token
            state_path.write_text(json.dumps(state))
        return token
    except Exception:
        return ""


def forward_to_lightrag(lightrag_cfg: dict, text: str, source_label: str) -> None:
    """POST a text document to LightRAG asynchronously (fire-and-forget)."""
    url = f"{lightrag_cfg['url']}/documents/text"
    payload = json.dumps({"text": text, "file_source": source_label}).encode("utf-8")
    token = get_lightrag_jwt(lightrag_cfg)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def extract_task_description(tool_name: str, tool_input: dict) -> str:
    """
    Extract a meaningful task description from the tool call input.
    Used as the redundancy-check query.
    """
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        # Strip common noise, keep the meaningful part
        return cmd.strip()[:300]
    elif tool_name == "WebFetch":
        url = tool_input.get("url", "")
        prompt = tool_input.get("prompt", "")
        return f"{prompt} {url}".strip()[:300]
    return ""


def check_redundancy(api_url: str, api_key: str, query: str) -> dict | None:
    params = urllib.parse.urlencode({
        "query": query,
        "hours": LOOKBACK_HOURS,
        "similarity_threshold": SIMILARITY_THRESHOLD,
    })
    url = f"{api_url.rstrip('/')}/api/v1/team-knowledge/redundancy-check?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def build_additional_context(result: dict) -> str:
    """Format the redundancy match as a concise context block for Claude."""
    best = result["similar_entries"][0]
    title = best["title"]
    hours_ago = best.get("hours_ago", "?")
    token_count = best.get("token_count", 0)
    similarity = best.get("similarity", 0)
    preview = best.get("content_preview", "")
    saving = result.get("estimated_saving", 0)

    lines = [
        f"[Evols] A teammate already did similar work ({similarity:.0%} match):",
        f'  "{title}" — {hours_ago:.0f}h ago · ~{token_count:,} tokens · ~{saving:,} tokens saved if reused',
        "",
        preview,
        "",
        "Consider whether the above covers this sub-task before proceeding.",
    ]
    return "\n".join(lines)


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    tool_output = hook_input.get("tool_response", hook_input.get("tool_output", ""))
    if isinstance(tool_output, dict):
        tool_output = json.dumps(tool_output)

    # ── 1. Capture notable outputs for Stop hook auto-sync ─────────────────────
    try:
        if SESSION_STATE_FILE.exists() and tool_name in KNOWLEDGE_TOOLS and len(str(tool_output)) > 200:
            with open(SESSION_STATE_FILE) as f:
                state = json.load(f)
            outputs = state.get("tool_outputs", [])
            outputs.append({"tool": tool_name, "summary": str(tool_output)[:300]})
            state["tool_outputs"] = outputs[-20:]  # Keep last 20
            with open(SESSION_STATE_FILE, "w") as f:
                json.dump(state, f)
    except Exception:
        pass

    # ── 2. Forward MCP tool responses to LightRAG knowledge graph ─────────────
    if tool_name.startswith(MCP_TOOL_PREFIX) and len(str(tool_output)) >= LIGHTRAG_MIN_LENGTH:
        lightrag_cfg = load_lightrag_config()
        if lightrag_cfg:
            # Label: "mcp__slack__list_messages / session_abc123"
            session_id = "unknown"
            try:
                if SESSION_STATE_FILE.exists():
                    with open(SESSION_STATE_FILE) as f:
                        session_id = json.load(f).get("session_id", "unknown")
            except Exception:
                pass
            source_label = f"{tool_name}/{session_id}"
            forward_to_lightrag(lightrag_cfg, str(tool_output), source_label)

    # ── 3. Sub-task redundancy check for expensive tools ──────────────────────
    if tool_name not in REDUNDANCY_CHECK_TOOLS:
        sys.exit(0)

    config = load_config()
    if not config:
        sys.exit(0)

    api_url = config.get("api_url", "")
    api_key = config.get("api_key", "")
    if not api_url or not api_key:
        sys.exit(0)

    # Skip trivially short commands (cd, echo, ls, etc.)
    description = extract_task_description(tool_name, tool_input)
    if len(description) < 40:
        sys.exit(0)

    result = check_redundancy(api_url, api_key, description)
    if not result or not result.get("found"):
        sys.exit(0)

    # Inject into Claude's reasoning chain — Claude sees this, terminal does NOT
    additional_context = build_additional_context(result)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": additional_context,
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
