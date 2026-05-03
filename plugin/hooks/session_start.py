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
PRICING_FILE = EVOLS_DIR / "pricing.json"

# LiteLLM community pricing source — refreshed once per day at session start
LITELLM_PRICING_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"

# Tier-to-model-ID fragments used to map LiteLLM entries to our tiers
_LITELLM_TIER_KEYS = {
    "opus":   ["claude-opus-4",   "claude-3-opus"],
    "sonnet": ["claude-sonnet-4", "claude-3-5-sonnet"],
    "haiku":  ["claude-haiku-4",  "claude-3-5-haiku", "claude-3-haiku"],
}


def refresh_pricing_cache() -> None:
    """
    Fetch LiteLLM pricing JSON and write ~/.evols/pricing.json.
    Only refreshes if the file is older than 24 h or missing.
    Silently skips on any network or parse error — hardcoded fallback remains.
    """
    try:
        if PRICING_FILE.exists():
            age_h = (datetime.utcnow() - datetime.utcfromtimestamp(PRICING_FILE.stat().st_mtime)).total_seconds() / 3600
            if age_h < 24:
                return
        req = urllib.request.Request(LITELLM_PRICING_URL, headers={"User-Agent": "evols-plugin/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = json.loads(resp.read())

        pricing = {}
        for tier, keys in _LITELLM_TIER_KEYS.items():
            for key in keys:
                entry = raw.get(key, {})
                ir = entry.get("input_cost_per_token")
                or_ = entry.get("output_cost_per_token")
                if ir and or_:
                    cr = entry.get("cache_read_input_token_cost", ir * 0.1)
                    cw = entry.get("cache_creation_input_token_cost", ir * 1.25)
                    # LiteLLM stores per-token; we store per-MTok
                    pricing[tier] = [ir * 1e6, or_ * 1e6, cr * 1e6, cw * 1e6]
                    break  # use first match for this tier

        if len(pricing) == 3:
            EVOLS_DIR.mkdir(parents=True, exist_ok=True)
            with open(PRICING_FILE, "w") as f:
                json.dump(pricing, f)
    except Exception:
        pass


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


def fetch_skills_catalog(api_url, api_key):
    """Call GET /api/v1/copilot/skills to get the lightweight skills catalog."""
    url = f"{api_url.rstrip('/')}/api/v1/copilot/skills"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            skills = json.loads(resp.read())
        if not skills:
            return None
        # Build a lightweight catalog grouped by category (names + descriptions only)
        lines = ["## AI Skills Available", ""]
        for skill in skills:
            name = skill.get("name", "")
            desc = skill.get("description", "")
            lines.append(f"- **{name}**: {desc}")
        lines += [
            "",
            "To apply a skill, call the `get_skill` MCP tool with the skill name.",
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

    # Refresh LiteLLM pricing cache in the background (max once/day, silent on failure)
    refresh_pricing_cache()

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
    # tokens_retrieved and actual_savings come from the Evols API at session start.
    EVOLS_DIR.mkdir(parents=True, exist_ok=True)
    session_state = {
        "session_id": session_id,
        "cwd": cwd,
        "started_at": datetime.utcnow().isoformat(),
        "tokens_retrieved": 0,
        "actual_savings": 0,   # similarity-weighted, set below if context was loaded
        "tool_outputs": [],
        "files_read": [],
        "files_modified": [],
        "discovery_tokens": 0,
        "plan_type": config.get("plan_type", "pro"),
    }
    with open(SESSION_STATE_FILE, "w") as f:
        json.dump(session_state, f)

    # Fetch AI skills catalog and team knowledge in parallel (sequential here, both fast)
    skills_catalog = fetch_skills_catalog(api_url, api_key)
    context_data = fetch_relevant_context(api_url, api_key, query=cwd, top_k=5)

    sections = []

    # AI skills section
    if skills_catalog:
        sections.append(
            "[Evols] AI Skills loaded. When the user's request maps to one of the skills below, "
            "call `get_skill` with that skill name to load full instructions, then apply them.\n\n"
            + skills_catalog
        )

    # Team knowledge section
    if not context_data or context_data.get("entry_count", 0) == 0:
        sections.append("[Evols] Team knowledge graph active. No relevant context yet for this workspace — context will build as your team works.")
    else:
        tokens_retrieved = context_data.get("tokens_retrieved", 0)
        # actual_savings is similarity-weighted by the backend — more honest than flat tokens_retrieved * 7
        actual_savings = context_data.get("actual_savings", context_data.get("tokens_saved_estimate", 0))
        session_state["tokens_retrieved"] = tokens_retrieved
        session_state["actual_savings"] = actual_savings
        with open(SESSION_STATE_FILE, "w") as f:
            json.dump(session_state, f)

        entry_count = context_data.get("entry_count", 0)
        context_text = context_data.get("context_text", "")
        header = f"[Evols] Loaded {entry_count} team knowledge entries ({tokens_retrieved:,} tokens retrieved · ~{actual_savings:,} tokens saved vs. compiling fresh)"
        sections.append(f"{header}\n\n{context_text}")

    print(json.dumps({"systemMessage": "\n\n---\n\n".join(sections)}))
    sys.exit(0)


if __name__ == "__main__":
    main()
