#!/usr/bin/env python3
"""
Evols UserPromptSubmit Hook
Runs before every prompt is sent to Claude.
- On first prompt of a session: initializes session state + injects team context
- Every prompt: checks for redundant work (if enabled)
- Token counts are NOT estimated here — exact counts come from transcript_path at session end
"""

import sys
import json
import os
import uuid
import urllib.request
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


def fetch_relevant_context(api_url, api_key, query, top_k=5):
    params = urllib.parse.urlencode({"query": query, "top_k": top_k})
    url = f"{api_url.rstrip('/')}/api/v1/team-knowledge/relevant?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def check_redundancy(api_url, api_key, prompt_text, lookback_hours=48):
    """
    Hit the redundancy-check endpoint with the user's prompt.
    Returns the response dict or None on failure.
    """
    # Truncate prompt to a useful description length
    query = prompt_text.strip()[:300]
    params = urllib.parse.urlencode({
        "query": query,
        "hours": lookback_hours,
        "similarity_threshold": 0.75,
    })
    url = f"{api_url.rstrip('/')}/api/v1/team-knowledge/redundancy-check?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def init_session(config, hook_input):
    """Initialize session state on first prompt. Only tracks what can't come from transcript."""
    session_id = hook_input.get("session_id") or str(uuid.uuid4())
    cwd = hook_input.get("cwd", os.getcwd())

    state = {
        "session_id": session_id,
        "cwd": cwd,
        "started_at": datetime.now().isoformat(),
        "tokens_retrieved": 0,   # from Evols API — not in transcript
        "plan_type": config.get("plan_type", "pro"),
        "context_injected": False,
    }

    EVOLS_DIR.mkdir(parents=True, exist_ok=True)
    with open(SESSION_STATE_FILE, "w") as f:
        json.dump(state, f)

    return state


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    config = load_config()
    if not config:
        sys.exit(0)

    api_url = config.get("api_url", "")
    api_key = config.get("api_key", "")

    # Warn if config has a JWT instead of a long-lived API key
    if api_key.startswith("eyJ"):
        print(
            "\n[Evols] ⚠  Your config uses a JWT token which expires in 24h.\n"
            "         Go to Settings → API Keys → New Key and update ~/.evols/config.json\n"
            "         with an 'evols_...' key to avoid silent auth failures.\n",
            file=sys.stderr
        )

    # ── First prompt of session: init state + inject context + redundancy check ──
    is_first_prompt = not SESSION_STATE_FILE.exists()

    if is_first_prompt:
        state = init_session(config, hook_input)
        cwd = state["cwd"]

        if api_url and api_key:
            # 1. Inject relevant context based on working directory
            context_data = fetch_relevant_context(api_url, api_key, query=cwd, top_k=5)

            if not context_data or context_data.get("entry_count", 0) == 0:
                print("\n[Evols] Team knowledge graph active. No relevant context yet — "
                      "use sync_session_context to add your first entry.\n", file=sys.stderr)
            else:
                tokens_retrieved = context_data.get("tokens_retrieved", 0)
                tokens_saved = context_data.get("tokens_saved_estimate", 0)
                entry_count = context_data.get("entry_count", 0)

                state["tokens_retrieved"] = tokens_retrieved
                state["context_injected"] = True
                with open(SESSION_STATE_FILE, "w") as f:
                    json.dump(state, f)

                print(
                    f"\n[Evols] {entry_count} team knowledge entries loaded "
                    f"({tokens_retrieved} tokens · ~{tokens_saved} saved vs. fresh)\n",
                    file=sys.stderr
                )

            # 2. Redundancy check — show prior work inline if a teammate solved this recently
            prompt_text = hook_input.get("prompt", "") or ""
            if len(prompt_text.strip()) > 30:
                redundancy = check_redundancy(api_url, api_key, prompt_text)
                if redundancy and redundancy.get("found"):
                    best = redundancy["similar_entries"][0]
                    token_cost = best.get("token_count", 0)
                    hours_ago = best.get("hours_ago", "?")
                    saving = redundancy.get("estimated_saving", 0)
                    preview = best.get("content_preview", "")
                    sep = "-" * 60
                    print(
                        f"\n[Evols] Prior team work found ({best.get('similarity', 0):.0%} match)\n"
                        f"{sep}\n"
                        f"  \"{best['title']}\"\n"
                        f"  {hours_ago:.0f}h ago · ~{token_cost:,} tokens · ~{saving:,} tokens saved if reused\n"
                        f"{sep}\n"
                        f"{preview}\n"
                        f"{sep}\n"
                        f"Continuing with your prompt. Reference the above if it covers your need.\n"
                        f"To abort: Ctrl+C\n",
                        file=sys.stderr
                    )

    sys.exit(0)


if __name__ == "__main__":
    main()
