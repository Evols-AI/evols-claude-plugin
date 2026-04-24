#!/usr/bin/env python3
"""
Evols SessionStart Hook
Runs when a new Claude Code session begins.
- Fetches relevant team knowledge from Evols API
- Injects context into the session
- Initializes session state for token tracking
"""

import sys
import json
import os
import uuid
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path

EVOLS_DIR = Path.home() / ".evols"
CONFIG_FILE = EVOLS_DIR / "config.json"
SESSION_STATE_FILE = EVOLS_DIR / "session_state.json"


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


def fetch_pm_skills_catalog(api_url, api_key):
    """Call GET /api/v1/copilot/skills to get the lightweight skills catalog."""
    url = f"{api_url.rstrip('/')}/api/v1/copilot/skills"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            skills = json.loads(resp.read())
        if not skills:
            return None
        # Build a lightweight catalog grouped by category (names + descriptions only)
        lines = ["## PM Skills Available", ""]
        for skill in skills:
            name = skill.get("name", "")
            desc = skill.get("description", "")
            lines.append(f"- **{name}**: {desc}")
        lines += [
            "",
            "To apply a skill, call the `get_pm_skill` MCP tool with the skill name.",
            "Then follow those instructions for the rest of the conversation.",
        ]
        return "\n".join(lines)
    except Exception:
        return None


def fetch_relevant_context(api_url, api_key, query, top_k=5):
    """Call GET /api/v1/team-knowledge/relevant"""
    params = urllib.parse.urlencode({"query": query, "top_k": top_k})
    url = f"{api_url.rstrip('/')}/api/v1/team-knowledge/relevant?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def ensure_mcp_config(cwd: str):
    """Write evols MCP server entry into <cwd>/.mcp.json if not already present."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if not plugin_root or not plugin_data:
        return

    mcp_path = Path(cwd) / ".mcp.json"
    try:
        config = json.loads(mcp_path.read_text()) if mcp_path.exists() else {}
    except Exception:
        config = {}

    servers = config.setdefault("mcpServers", {})
    if "evols" in servers:
        return

    api_key = os.environ.get("EVOLS_API_KEY") or os.environ.get("CLAUDE_PLUGIN_OPTION_EVOLS_API_KEY", "")
    plan = os.environ.get("EVOLS_PLAN") or os.environ.get("CLAUDE_PLUGIN_OPTION_EVOLS_PLAN", "pro")

    servers["evols"] = {
        "command": str(Path(plugin_data) / "venv" / "bin" / "python3"),
        "args": [str(Path(plugin_root) / "plugin" / "mcp_server" / "server.py")],
        "env": {
            "EVOLS_API_URL": "https://api.evols.ai",
            "EVOLS_API_KEY": api_key,
            "EVOLS_PLAN": plan,
        },
    }

    try:
        mcp_path.write_text(json.dumps(config, indent=2) + "\n")
    except Exception:
        pass


def main():
    import urllib.parse

    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        hook_input = {}

    session_id = hook_input.get("session_id") or str(uuid.uuid4())
    cwd = hook_input.get("cwd", os.getcwd())

    ensure_mcp_config(cwd)

    # Load config
    config = load_config()
    if not config:
        print(json.dumps({"systemMessage": "[Evols] Not configured. Create ~/.evols/config.json:\n  {\"api_url\": \"https://...\", \"api_key\": \"evols_...\", \"plan_type\": \"pro\"}\nGet your credentials from your Evols dashboard."}))
        sys.exit(0)

    api_url = config.get("api_url", "")
    api_key = config.get("api_key", "")
    if not api_url or not api_key:
        print(json.dumps({"systemMessage": "[Evols] Not configured. Create ~/.evols/config.json:\n  {\"api_url\": \"https://...\", \"api_key\": \"evols_...\", \"plan_type\": \"pro\"}\nGet your credentials from your Evols dashboard."}))
        sys.exit(0)

    # Initialize session state.
    # tokens_input/output are NOT tracked here — exact counts come from transcript_path JSONL at session end.
    # Only tokens_retrieved is tracked (comes from Evols API, not in transcript).
    EVOLS_DIR.mkdir(parents=True, exist_ok=True)
    session_state = {
        "session_id": session_id,
        "cwd": cwd,
        "started_at": datetime.utcnow().isoformat(),
        "tokens_retrieved": 0,
        "tool_outputs": [],
        "plan_type": config.get("plan_type", "pro"),
    }
    with open(SESSION_STATE_FILE, "w") as f:
        json.dump(session_state, f)

    # Fetch PM skills catalog and team knowledge in parallel (sequential here, both fast)
    skills_catalog = fetch_pm_skills_catalog(api_url, api_key)
    context_data = fetch_relevant_context(api_url, api_key, query=cwd, top_k=5)

    sections = []

    # PM skills section
    if skills_catalog:
        sections.append(
            "[Evols] PM Skills loaded. When the user's request maps to one of the skills below, "
            "call `get_pm_skill` with that skill name to load full instructions, then apply them.\n\n"
            + skills_catalog
        )

    # Team knowledge section
    if not context_data or context_data.get("entry_count", 0) == 0:
        sections.append("[Evols] Team knowledge graph active. No relevant context yet for this workspace — context will build as your team works.")
    else:
        session_state["tokens_retrieved"] = context_data.get("tokens_retrieved", 0)
        with open(SESSION_STATE_FILE, "w") as f:
            json.dump(session_state, f)

        tokens_retrieved = context_data.get("tokens_retrieved", 0)
        tokens_saved = context_data.get("tokens_saved_estimate", 0)
        entry_count = context_data.get("entry_count", 0)
        context_text = context_data.get("context_text", "")
        header = f"[Evols] Loaded {entry_count} team knowledge entries ({tokens_retrieved} tokens retrieved · ~{tokens_saved} tokens saved vs. compiling fresh)"
        sections.append(f"{header}\n\n{context_text}")

    print(json.dumps({"systemMessage": "\n\n---\n\n".join(sections)}))
    sys.exit(0)


if __name__ == "__main__":
    main()
