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


def main():
    import urllib.parse

    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        hook_input = {}

    session_id = hook_input.get("session_id") or str(uuid.uuid4())
    cwd = hook_input.get("cwd", os.getcwd())

    # Load config
    config = load_config()
    if not config:
        # Plugin not configured — silent exit
        sys.exit(0)

    api_url = config.get("api_url", "")
    api_key = config.get("api_key", "")
    if not api_url or not api_key:
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

    # Fetch relevant context using cwd as query hint
    context_data = fetch_relevant_context(api_url, api_key, query=cwd, top_k=5)

    if not context_data or context_data.get("entry_count", 0) == 0:
        # No relevant context yet — print welcome message on first use
        print("\n[Evols] Team knowledge graph: no relevant context yet for this workspace. "
              "Context will build as your team works.\n")
        sys.exit(0)

    # Update session state with retrieved tokens
    session_state["tokens_retrieved"] = context_data.get("tokens_retrieved", 0)
    with open(SESSION_STATE_FILE, "w") as f:
        json.dump(session_state, f)

    # Output context to inject into session
    tokens_retrieved = context_data.get("tokens_retrieved", 0)
    tokens_saved = context_data.get("tokens_saved_estimate", 0)
    entry_count = context_data.get("entry_count", 0)

    output_lines = [
        f"\n[Evols] Loaded {entry_count} team knowledge entries "
        f"({tokens_retrieved} tokens retrieved · ~{tokens_saved} tokens saved vs. compiling fresh)\n",
        context_data.get("context_text", ""),
    ]
    print("\n".join(output_lines))
    sys.exit(0)


if __name__ == "__main__":
    main()
