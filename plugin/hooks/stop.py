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
PRICING_FILE = EVOLS_DIR / "pricing.json"
BLOCK_STATE_FILE = EVOLS_DIR / "block_state.json"

PLAN_DAILY_LIMITS = {
    "pro": 88000,
    "max": 220000,
    "team": 440000,
    "enterprise": 880000,
}

# Claude Code subscription resets every 5 hours, not daily
BLOCK_HOURS = 5
PLAN_BLOCK_LIMITS = {
    "pro": 22000,
    "max": 55000,
    "team": 110000,
    "enterprise": 220000,
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


_FALLBACK_PRICING = {
    # (input_per_mtok, output_per_mtok, cache_read_per_mtok, cache_write_per_mtok)
    "opus":   (5.00, 25.00, 0.50, 6.25),
    "sonnet": (3.00, 15.00, 0.30, 3.75),
    "haiku":  (1.00,  5.00, 0.10, 1.25),
}


def load_pricing() -> dict:
    """
    Load model pricing from ~/.evols/pricing.json (updated at install time from LiteLLM).
    Falls back to hardcoded defaults if file is missing or malformed.
    Expected format: {"opus": [5.0, 25.0, 0.5, 6.25], "sonnet": [...], "haiku": [...]}
    """
    try:
        if PRICING_FILE.exists():
            with open(PRICING_FILE) as f:
                data = json.load(f)
            pricing = {}
            for tier in ("opus", "sonnet", "haiku"):
                v = data.get(tier)
                if v and len(v) == 4:
                    pricing[tier] = tuple(float(x) for x in v)
            if len(pricing) == 3:
                return pricing
    except Exception:
        pass
    return dict(_FALLBACK_PRICING)


MODEL_PRICING = load_pricing()


def model_tier(model_id: str) -> str:
    """Map a full model ID string to pricing tier key."""
    m = model_id.lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    return "sonnet"  # default — covers sonnet + unknown models


def compute_cost(model_id: str, input_tokens: int, output_tokens: int,
                 cache_read: int, cache_write: int) -> float:
    tier = model_tier(model_id)
    ir, or_, cr, cw = MODEL_PRICING[tier]
    return (
        input_tokens  * ir +
        output_tokens * or_ +
        cache_read    * cr +
        cache_write   * cw
    ) / 1_000_000


def parse_transcript_usage(transcript_path: str) -> dict:
    """
    Parse the session JSONL transcript to get exact token counts.

    Claude Code writes multiple JSONL lines per API response (one per streaming chunk),
    all sharing the same message.id. Each chunk's usage fields are cumulative totals,
    not deltas — so summing them would massively overcount. We keep only the LAST
    record per message.id, which contains the final usage tally for that response.

    Claude also writes a costUSD field at the top level of some entries — we prefer
    this pre-calculated value over our own compute_cost() estimate.
    """
    # message_id -> (usage dict, model string) for that message — last write wins
    per_message: dict[str, tuple] = {}
    no_id_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    model_seen = ""
    cost_usd_sum = 0.0
    has_cost_usd = False
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # Extract pre-calculated cost when Claude provides it
                    if "costUSD" in entry:
                        cost_usd_sum += float(entry["costUSD"])
                        has_cost_usd = True
                    msg = entry.get("message", {})
                    usage = msg.get("usage", {})
                    if not usage:
                        continue
                    model = msg.get("model", "")
                    if model:
                        model_seen = model  # last model seen wins (highest tier for mixed sessions)
                    message_id = msg.get("id")
                    if message_id:
                        per_message[message_id] = (usage, model)
                    else:
                        for key in no_id_totals:
                            no_id_totals[key] += usage.get(key, 0)
                except Exception:
                    continue
    except Exception:
        pass

    totals = dict(no_id_totals)
    for usage, model in per_message.values():
        for key in totals:
            totals[key] += usage.get(key, 0)
        if model:
            model_seen = model

    totals["model"] = model_seen
    if has_cost_usd:
        totals["cost_usd"] = cost_usd_sum
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


def auto_sync_knowledge(api_url, api_key, session_id, transcript_text, token_count, plan_type,
                        files_read=None, files_modified=None, discovery_tokens=0, model=""):
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
            "discovery_tokens": discovery_tokens or None,
            "files_read": files_read or [],
            "files_modified": files_modified or [],
            "model": model or None,
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
    # Reload pricing in case session_start refreshed it during this session
    global MODEL_PRICING
    MODEL_PRICING = load_pricing()

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
    model = usage.get("model", "") or "claude-sonnet"
    # Prefer costUSD written by Claude into the transcript; fall back to our calculation
    if "cost_usd" in usage:
        session_cost_usd = usage["cost_usd"]
    else:
        session_cost_usd = compute_cost(model, input_tokens, output_tokens, cache_read_tokens, cache_create_tokens)
    # Cache reads are discounted (0.1x) — count them separately for display

    # ── Get tokens_retrieved + granular capture from session state ─
    tokens_retrieved = 0
    actual_savings_override = None
    session_id = "unknown"
    cwd = ""
    files_read = []
    files_modified = []
    discovery_tokens = 0
    started_at_str = ""
    if SESSION_STATE_FILE.exists():
        try:
            with open(SESSION_STATE_FILE) as f:
                state = json.load(f)
            tokens_retrieved = state.get("tokens_retrieved", 0)
            if "actual_savings" in state:
                actual_savings_override = state["actual_savings"]
            session_id = state.get("session_id", "unknown")
            cwd = state.get("cwd", "")
            files_read = state.get("files_read", [])
            files_modified = state.get("files_modified", [])
            discovery_tokens = state.get("discovery_tokens", 0)
            started_at_str = state.get("started_at", "")
        except Exception:
            pass

    # ── Compute burn rate (tokens per minute) ─────────────────────
    burn_rate_per_min = 0.0
    session_minutes = 0.0
    if started_at_str:
        try:
            started_at = datetime.fromisoformat(started_at_str)
            session_minutes = (datetime.utcnow() - started_at).total_seconds() / 60.0
            if session_minutes >= 1.0:
                burn_rate_per_min = total_tokens / session_minutes
        except Exception:
            pass

    # ── 5-hour billing block tracking ──────────────────────────────
    # Claude Code quota resets every BLOCK_HOURS, not daily.
    # Track cumulative tokens in the current block via block_state.json.
    block_tokens_total = total_tokens
    block_start = datetime.utcnow()
    block_time_remaining_min = BLOCK_HOURS * 60.0
    try:
        if BLOCK_STATE_FILE.exists():
            with open(BLOCK_STATE_FILE) as f:
                block_state = json.load(f)
            block_start = datetime.fromisoformat(block_state.get("block_start", datetime.utcnow().isoformat()))
            elapsed_h = (datetime.utcnow() - block_start).total_seconds() / 3600.0
            if elapsed_h < BLOCK_HOURS:
                block_tokens_total = block_state.get("block_tokens", 0) + total_tokens
                block_time_remaining_min = (BLOCK_HOURS - elapsed_h) * 60.0
            else:
                # Block expired — start a new one
                block_start = datetime.utcnow()
                block_tokens_total = total_tokens
                block_time_remaining_min = BLOCK_HOURS * 60.0
        else:
            block_start = datetime.utcnow()

        with open(BLOCK_STATE_FILE, "w") as f:
            json.dump({
                "block_start": block_start.isoformat(),
                "block_tokens": block_tokens_total,
            }, f)
    except Exception:
        pass

    # ── Auto-sync knowledge via Haiku (run first so we know if new knowledge was created) ──
    synced_entry_id = None
    extracted_knowledge = None
    if not is_failure and transcript_path and total_tokens > 500:
        transcript_text = extract_transcript_text(transcript_path)
        if transcript_text:
            synced_entry_id = auto_sync_knowledge(
                api_url, api_key, session_id, transcript_text, total_tokens, plan_type,
                files_read=files_read, files_modified=files_modified,
                discovery_tokens=discovery_tokens, model=model,
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

    # ── Sync quota event to API ────────────────────────────────────
    # tokens_created: the full session cost is the investment when new knowledge was synced.
    # Only count as investment when we actually created a knowledge entry (not SKIP sessions).
    tokens_created = total_tokens if synced_entry_id else 0
    event_type = "rate_limit_hit" if is_failure else "session_end"
    quota_payload = {
        "session_id": session_id,
        "tokens_used": total_tokens,
        "tokens_retrieved": tokens_retrieved,
        "tokens_created": tokens_created,
        "event_type": event_type,
        "tool_name": "claude-code",
        "plan_type": plan_type,
        "model": model,
        "cost_usd": round(session_cost_usd, 6),
        "cwd": cwd,
    }
    if actual_savings_override is not None:
        quota_payload["actual_savings_override"] = actual_savings_override
    post_quota_event(api_url, api_key, quota_payload)

    # ── Display summary ────────────────────────────────────────────
    block_limit = PLAN_BLOCK_LIMITS.get(plan_type, 22000)
    block_pct = round((block_tokens_total / block_limit) * 100, 1) if block_limit else 0
    block_remaining_h = int(block_time_remaining_min // 60)
    block_remaining_m = int(block_time_remaining_min % 60)

    if is_failure:
        print(f"\n[Evols] Session ended due to rate limit.")
        print(f"  Tokens used this session: {total_tokens:,}")
        print(f"  5h block usage:  {block_tokens_total:,} / {block_limit:,}  ({block_pct}%  ·  resets in {block_remaining_h}h {block_remaining_m}m)")
        print(f"  This event has been recorded for your team's quota tracking.\n")
    else:
        print(f"\n[Evols] Session complete.")
        print(f"  Model:          {model}")
        print(f"  Input tokens:   {input_tokens:,}")
        print(f"  Output tokens:  {output_tokens:,}")
        if cache_read_tokens:
            print(f"  Cache reads:    {cache_read_tokens:,}  (discounted)")
        print(f"  Session total:  {total_tokens:,}  ({round(session_minutes)}m)")
        if burn_rate_per_min >= 1.0:
            projected_block = int(burn_rate_per_min * BLOCK_HOURS * 60)
            print(f"  Burn rate:      {burn_rate_per_min:.0f} tok/min  →  ~{projected_block:,} projected/block")
        print(f"  Session cost:   ${session_cost_usd:.4f}")
        print(f"  5h block usage: {block_tokens_total:,} / {block_limit:,}  ({block_pct}%  ·  resets in {block_remaining_h}h {block_remaining_m}m)")
        if tokens_retrieved > 0:
            # Use similarity-weighted savings from session state; fall back to flat if missing
            display_savings = actual_savings_override if actual_savings_override is not None else tokens_retrieved * 7
            print(f"  Team context:   {tokens_retrieved:,} tokens retrieved  →  ~{display_savings:,} tokens saved vs. compiling fresh")
        if synced_entry_id:
            # This session created knowledge — savings come later when teammates retrieve it
            print(f"  Knowledge sync: entry #{synced_entry_id} added to team graph  (investment: {tokens_created:,} tokens)")
            print(f"                  Savings realized when you or teammates retrieve this context in future sessions.")
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
