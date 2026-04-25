#!/usr/bin/env python3
"""
Evols Stop / StopFailure Hook
Runs when a Claude Code session ends (normally or due to rate limit).
- Reads EXACT token counts from the transcript JSONL (API usage fields)
- Syncs quota event to Evols API
- Displays token savings summary to user
- Auto-syncs session knowledge via Haiku summarization of transcript
"""

import sys
import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

EVOLS_DIR = Path.home() / ".evols"
CONFIG_FILE = EVOLS_DIR / "config.json"
SESSION_STATE_FILE = EVOLS_DIR / "session_state.json"

PLAN_DAILY_LIMITS = {
    "pro": 88000,
    "max": 220000,
    "team": 440000,
    "enterprise": 880000,
}


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
    config_file = Path.home() / ".evols" / "config.json"
    if config_file.exists():
        try:
            with open(config_file) as f:
                cfg = json.load(f)
            lr_url = cfg.get("lightrag_url", "")
            if lr_url:
                return {"url": lr_url.rstrip("/"), "api_key": cfg.get("lightrag_api_key", "")}
        except Exception:
            pass
    return None


def get_lightrag_jwt(lightrag_cfg: dict) -> str:
    """Exchange API key for a JWT via /login (form-encoded). Caches in session_state."""
    import urllib.parse
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
        req = urllib.request.Request(f"{lightrag_cfg['url']}/login", data=data, method="POST")
        resp = urllib.request.urlopen(req, timeout=5)
        token = json.loads(resp.read()).get("access_token", "")
        if token and state_path.exists():
            state = json.loads(state_path.read_text())
            state["lightrag_jwt"] = token
            state_path.write_text(json.dumps(state))
        return token
    except Exception:
        return ""


def forward_summary_to_lightrag(lightrag_cfg: dict, title: str, content: str, session_id: str) -> None:
    """POST a session summary document to LightRAG."""
    text = f"# {title}\n\n{content}"
    payload = json.dumps({"text": text, "file_source": f"session_summary/{session_id}"}).encode("utf-8")
    token = get_lightrag_jwt(lightrag_cfg)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(
            f"{lightrag_cfg['url']}/documents/text",
            data=payload,
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


def parse_transcript_usage(transcript_path: str) -> dict:
    """
    Parse the session JSONL transcript to get exact token counts.
    Each assistant message entry contains a 'message.usage' object
    with the real API token counts — no estimation needed.
    """
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    usage = entry.get("message", {}).get("usage", {})
                    if usage:
                        for key in totals:
                            totals[key] += usage.get(key, 0)
                except Exception:
                    continue
    except Exception:
        pass
    return totals


def extract_transcript_text(transcript_path: str) -> str:
    """
    Extract assistant messages and tool uses from transcript for knowledge sync.
    Returns a condensed text representation of the session.
    """
    lines_out = []
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    role = msg.get("role", "")
                    content = msg.get("content", "")

                    if role == "user" and isinstance(content, str) and content.strip():
                        lines_out.append(f"User: {content[:300]}")
                    elif role == "assistant":
                        if isinstance(content, str) and content.strip():
                            lines_out.append(f"Assistant: {content[:500]}")
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict):
                                    if block.get("type") == "text":
                                        lines_out.append(f"Assistant: {block.get('text', '')[:500]}")
                                    elif block.get("type") == "tool_use":
                                        lines_out.append(f"Tool: {block.get('name')}({json.dumps(block.get('input', {}))[:200]})")
                except Exception:
                    continue
    except Exception:
        pass
    return "\n".join(lines_out[-60:])  # Last 60 turns to stay within context


# Module-level slot so callers can access the last extracted knowledge dict
_last_extracted_knowledge: dict | None = None


def auto_sync_knowledge(api_url, api_key, session_id, transcript_text, token_count, plan_type):
    """
    Call Anthropic API (Haiku) to extract structured knowledge from the session,
    then POST it to the team knowledge graph.
    Also stores the extracted dict in _last_extracted_knowledge for LightRAG forwarding.
    """
    global _last_extracted_knowledge
    _last_extracted_knowledge = None

    try:
        import urllib.request

        prompt = (
            "You are extracting team knowledge from an AI coding session transcript.\n\n"
            "Given the session below, extract a knowledge entry with:\n"
            "- title: one-line description of what was accomplished (max 80 chars)\n"
            "- content: problem statement, approach taken, key decisions, outcome (3-8 sentences)\n"
            "- entry_type: one of: insight, decision, artifact, research_finding, pattern, context\n"
            "- tags: 2-5 comma-separated keywords\n"
            "- product_area: the product/code area affected (or empty string)\n\n"
            "If the session is trivial (e.g. just questions, no real work), respond with: SKIP\n\n"
            f"Session transcript:\n{transcript_text}\n\n"
            "Respond ONLY with a JSON object with keys: title, content, entry_type, tags, product_area"
        )

        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        }

        # Get API key from Anthropic env var (available in Claude Code sessions)
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            return None

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())

        raw = result.get("content", [{}])[0].get("text", "").strip()
        if raw == "SKIP" or not raw:
            return None

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        extracted = json.loads(raw.strip())
        _last_extracted_knowledge = extracted

        # POST to Evols API
        sync_payload = {
            "title": extracted.get("title", "Untitled session"),
            "content": extracted.get("content", ""),
            "role": "other",
            "session_type": "code",
            "entry_type": extracted.get("entry_type", "insight"),
            "tags": [t.strip() for t in extracted.get("tags", "").split(",") if t.strip()],
            "product_area": extracted.get("product_area") or None,
            "source_session_id": session_id,
            "session_tokens_used": token_count,
        }

        sync_req = urllib.request.Request(
            f"{api_url.rstrip('/')}/api/v1/team-knowledge/entries",
            data=json.dumps(sync_payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(sync_req, timeout=10) as resp:
            entry = json.loads(resp.read())
            return entry.get("id")

    except Exception:
        return None


def post_quota_event(api_url, api_key, payload):
    url = f"{api_url.rstrip('/')}/api/v1/team-knowledge/quota/events"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def main():
    is_failure = "--failure" in sys.argv

    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        hook_input = {}

    config = load_config()
    if not config:
        sys.exit(0)

    api_url = config.get("api_url", "")
    api_key = config.get("api_key", "")
    plan_type = config.get("plan_type", "pro")
    if not api_url or not api_key:
        sys.exit(0)

    # ── Get exact token counts from transcript ─────────────────────
    transcript_path = hook_input.get("transcript_path", "")
    usage = {}
    if transcript_path:
        usage = parse_transcript_usage(transcript_path)

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read_tokens = usage.get("cache_read_input_tokens", 0)
    cache_create_tokens = usage.get("cache_creation_input_tokens", 0)
    total_tokens = input_tokens + output_tokens + cache_create_tokens
    # Cache reads are discounted (0.1x) — count them separately for display

    # ── Get tokens_retrieved from session state ────────────────────
    tokens_retrieved = 0
    session_id = "unknown"
    cwd = ""
    if SESSION_STATE_FILE.exists():
        try:
            with open(SESSION_STATE_FILE) as f:
                state = json.load(f)
            tokens_retrieved = state.get("tokens_retrieved", 0)
            session_id = state.get("session_id", "unknown")
            cwd = state.get("cwd", "")
        except Exception:
            pass

    tokens_saved = tokens_retrieved * 7  # 8x compression ratio

    # ── Sync quota event to API ────────────────────────────────────
    event_type = "rate_limit_hit" if is_failure else "session_end"
    post_quota_event(api_url, api_key, {
        "session_id": session_id,
        "tokens_used": total_tokens,
        "tokens_retrieved": tokens_retrieved,
        "event_type": event_type,
        "tool_name": "claude-code",
        "plan_type": plan_type,
        "cwd": cwd,
    })

    # ── Auto-sync knowledge via Haiku ──────────────────────────────
    synced_entry_id = None
    extracted_knowledge = None
    if not is_failure and transcript_path and total_tokens > 500:
        transcript_text = extract_transcript_text(transcript_path)
        if transcript_text:
            synced_entry_id = auto_sync_knowledge(
                api_url, api_key, session_id, transcript_text, total_tokens, plan_type
            )
            extracted_knowledge = _last_extracted_knowledge

    # ── Forward session summary to LightRAG knowledge graph ────────
    if extracted_knowledge and not is_failure:
        lightrag_cfg = load_lightrag_config()
        if lightrag_cfg:
            title = extracted_knowledge.get("title", f"Session {session_id}")
            content = extracted_knowledge.get("content", "")
            if content:
                forward_summary_to_lightrag(lightrag_cfg, title, content, session_id)

    # ── Display summary ────────────────────────────────────────────
    daily_limit = PLAN_DAILY_LIMITS.get(plan_type, 88000)
    quota_pct = round((total_tokens / daily_limit) * 100, 1) if daily_limit else 0

    if is_failure:
        print(f"\n[Evols] Session ended due to rate limit.")
        print(f"  Tokens used: {total_tokens:,}  ({quota_pct}% of {plan_type} daily quota)")
        print(f"  This event has been recorded for your team's quota tracking.\n")
    else:
        print(f"\n[Evols] Session complete.")
        print(f"  Input tokens:   {input_tokens:,}")
        print(f"  Output tokens:  {output_tokens:,}")
        if cache_read_tokens:
            print(f"  Cache reads:    {cache_read_tokens:,}  (discounted)")
        print(f"  Session total:  {total_tokens:,}  ({quota_pct}% of {plan_type} daily quota)")
        if tokens_retrieved > 0:
            print(f"  Team context:   {tokens_retrieved:,} tokens retrieved · ~{tokens_saved:,} saved vs. compiling fresh")
        if synced_entry_id:
            print(f"  Knowledge sync: entry #{synced_entry_id} added to team graph automatically")
        else:
            print(f"  Knowledge sync: call sync_session_context to share insights with your team")
        print()

    # ── Clean up session state ─────────────────────────────────────
    try:
        SESSION_STATE_FILE.unlink()
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
