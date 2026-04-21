#!/usr/bin/env python3
"""
Evols MCP Server
Provides Claude Code (and other MCP-compatible tools) with:
  - get_team_context: Retrieve relevant team knowledge for current task
  - sync_session_context: Add a knowledge entry to the team graph
  - get_quota_status: Team token savings summary

Install dependencies: pip install mcp requests
"""

import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' package required. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Error: 'mcp' package required. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #

CONFIG_FILE = Path.home() / ".evols" / "config.json"


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    # Fallback to environment variables
    return {
        "api_url": os.environ.get("EVOLS_API_URL", ""),
        "api_key": os.environ.get("EVOLS_API_KEY", ""),
        "plan_type": os.environ.get("EVOLS_PLAN", "pro"),
    }


def api_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


# ------------------------------------------------------------------ #
# MCP Server
# ------------------------------------------------------------------ #

mcp = FastMCP("evols")


@mcp.tool()
def get_team_context(
    query: str,
    role: str = "",
    top_k: int = 5,
) -> str:
    """
    Retrieve relevant team knowledge for your current task.

    Use this at the start of a session or before tackling a problem to:
    - Find what your teammates already know about this topic
    - Get pre-compiled context instead of starting from scratch
    - See estimated tokens saved vs. compiling fresh

    Args:
        query: Describe what you're working on (e.g. "onboarding drop-off analysis", "pricing research")
        role: Optional filter — pm, engineer, designer, qa (leave blank for all)
        top_k: Number of entries to retrieve (default 5, max 20)
    """
    config = load_config()
    api_url = config.get("api_url", "")
    api_key = config.get("api_key", "")

    if not api_url or not api_key:
        return "Evols not configured. Run the install script: bash ~/.evols/install.sh"

    params = {"query": query, "top_k": min(top_k, 20)}
    if role:
        params["role"] = role

    try:
        resp = requests.get(
            f"{api_url.rstrip('/')}/api/v1/team-knowledge/relevant",
            params=params,
            headers=api_headers(api_key),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return f"Could not reach Evols API: {e}"

    if data.get("entry_count", 0) == 0:
        return "No relevant team knowledge found yet. Your team's knowledge graph grows as sessions complete."

    tokens_retrieved = data.get("tokens_retrieved", 0)
    tokens_saved = data.get("tokens_saved_estimate", 0)
    entry_count = data.get("entry_count", 0)

    header = (
        f"## Team Knowledge — {entry_count} entries retrieved\n"
        f"*{tokens_retrieved} tokens · ~{tokens_saved} tokens saved vs. compiling fresh*\n\n"
    )
    return header + data.get("context_text", "")


@mcp.tool()
def sync_session_context(
    title: str,
    content: str,
    role: str = "other",
    session_type: str = "other",
    entry_type: str = "insight",
    tags: str = "",
    product_area: str = "",
    session_tokens_used: int = 0,
) -> str:
    """
    Add a knowledge entry to the team graph from this session.

    Call this at the end of a session to capture insights, decisions,
    or research findings so your team inherits this context automatically.

    Args:
        title: Short descriptive title (e.g. "SMB churn triggers — retention research")
        content: The compiled knowledge (insights, decisions, key findings)
        role: Your role — pm, engineer, designer, qa, other
        session_type: Session type — research, planning, code, analysis, review, other
        entry_type: insight, decision, artifact, research_finding, pattern, context
        tags: Comma-separated tags (e.g. "onboarding,retention,smb")
        product_area: Product area this relates to (e.g. "onboarding", "billing")
        session_tokens_used: Exact token count this session consumed to produce this knowledge.
            Teammates see this when check_redundancy finds this entry — it tells them
            precisely how many tokens they save by not redoing the same work.
    """
    config = load_config()
    api_url = config.get("api_url", "")
    api_key = config.get("api_key", "")

    if not api_url or not api_key:
        return "Evols not configured. Run the install script: bash ~/.evols/install.sh"

    payload = {
        "title": title,
        "content": content,
        "role": role,
        "session_type": session_type,
        "entry_type": entry_type,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "product_area": product_area or None,
        "session_tokens_used": session_tokens_used or None,
    }

    try:
        resp = requests.post(
            f"{api_url.rstrip('/')}/api/v1/team-knowledge/entries",
            json=payload,
            headers=api_headers(api_key),
            timeout=10,
        )
        resp.raise_for_status()
        entry = resp.json()
    except Exception as e:
        return f"Could not sync to Evols: {e}"

    token_count = entry.get("token_count", 0)
    entry_id = entry.get("id")
    cost_line = f"  Session cost stored: {session_tokens_used:,} tokens" if session_tokens_used else f"  Size: ~{token_count} tokens"

    base = (
        f"✓ Added to team knowledge graph (entry #{entry_id})\n"
        f"  Title: {title}\n"
        f"  Role: {role} · Type: {entry_type}\n"
        f"{cost_line}\n"
        f"  Your team inherits this context from their next session.\n"
    )

    # Fetch available products and ask the user if they want to attribute this entry
    try:
        prod_resp = requests.get(
            f"{api_url.rstrip('/')}/api/v1/team-knowledge/products",
            headers=api_headers(api_key),
            timeout=5,
        )
        products = prod_resp.json() if prod_resp.ok else []
    except Exception:
        products = []

    if not products:
        return base

    product_list = "\n".join(
        f"  {i + 1}) {p['name']}" for i, p in enumerate(products)
    )
    return (
        base
        + f"\n"
        + f"Would you also like to sync this to a product knowledge base?\n"
        + f"This makes it visible to PMs and the rest of the team in the product context.\n"
        + f"\n"
        + f"Available products:\n"
        + product_list
        + f"\n  {len(products) + 1}) Skip\n"
        + f"\n"
        + f"Reply with the number or product name, then call link_to_product(entry_id={entry_id}, ...)."
    )


@mcp.tool()
def link_to_product(entry_id: int, product_name: str) -> str:
    """
    Attribute a knowledge entry to a product after the user confirms which product.
    Call this immediately after the user replies to the product attribution prompt
    from sync_session_context.

    Args:
        entry_id: The entry ID returned by sync_session_context
        product_name: The product name the user chose (exact name or "skip" to skip)
    """
    if product_name.lower() == "skip":
        return f"Skipped product attribution for entry #{entry_id}. Entry remains in team knowledge only."

    config = load_config()
    api_url = config.get("api_url", "")
    api_key = config.get("api_key", "")

    if not api_url or not api_key:
        return "Evols not configured."

    # Resolve product name to ID
    try:
        prod_resp = requests.get(
            f"{api_url.rstrip('/')}/api/v1/team-knowledge/products",
            headers=api_headers(api_key),
            timeout=5,
        )
        prod_resp.raise_for_status()
        products = prod_resp.json()
    except Exception as e:
        return f"Could not fetch products: {e}"

    # Case-insensitive match
    match = next(
        (p for p in products if p["name"].lower() == product_name.lower()),
        None
    )
    if not match:
        names = ", ".join(p["name"] for p in products)
        return f"Product '{product_name}' not found. Available: {names}"

    try:
        resp = requests.patch(
            f"{api_url.rstrip('/')}/api/v1/team-knowledge/entries/{entry_id}/link-product",
            params={"product_id": match["id"]},
            headers=api_headers(api_key),
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as e:
        return f"Could not link entry to product: {e}"

    return (
        f"✓ Entry #{entry_id} linked to '{match['name']}'\n"
        f"  It will now appear in the '{match['name']}' product knowledge base\n"
        f"  visible to PMs and the whole team."
    )


@mcp.tool()
def check_redundancy(task_description: str, lookback_hours: int = 48) -> str:
    """
    Before starting any non-trivial task, check if a teammate already solved this.

    Call this at the start of a session when you know what the user wants to build.
    If a match is found, you'll see:
    - What the teammate built and when
    - Their actual session token cost (from the knowledge entry)
    - Your estimated saving (their cost minus retrieval cost)
    - That saving as % of the user's daily plan quota

    Args:
        task_description: What the user is about to work on (1-2 sentences)
        lookback_hours: How far back to check (default 48h)
    """
    config = load_config()
    api_url = config.get("api_url", "")
    api_key = config.get("api_key", "")

    if not api_url or not api_key:
        return "Evols not configured."

    try:
        resp = requests.get(
            f"{api_url.rstrip('/')}/api/v1/team-knowledge/redundancy-check",
            params={"query": task_description, "hours": lookback_hours},
            headers=api_headers(api_key),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return f"Could not reach Evols API: {e}"

    if not data.get("found"):
        return "✓ No redundant work found. Proceed — this looks new to your team."

    plan_type = config.get("plan_type", "pro")
    plan_limits = {"pro": 88000, "max": 220000, "team": 440000, "enterprise": 880000}
    daily_limit = plan_limits.get(plan_type, 88000)

    best = data["similar_entries"][0]
    token_cost = best["token_count"]
    saving = data.get("estimated_saving", max(0, token_cost - 140))
    saving_pct = round((saving / daily_limit) * 100, 1) if daily_limit > 0 else 0
    hours_ago = best.get("hours_ago", "?")

    lines = [
        f"⚠  Redundant work detected.",
        f"",
        f"  {best['role'].capitalize()} solved \"{best['title']}\" {hours_ago:.0f}h ago.",
        f"  Their session cost: ~{token_cost:,} tokens",
        f"  Retrieval cost:     ~140 tokens",
        f"  Estimated saving:   ~{saving:,} tokens",
        f"",
        f"  Your plan ({plan_type}): {daily_limit:,} tokens/day",
        f"  This saving preserves ~{saving_pct}% of your daily quota.",
        f"",
        f'  Use: get_team_context("{task_description[:50]}") to inherit their work.',
    ]

    if len(data["similar_entries"]) > 1:
        lines.append(f"\n  {len(data['similar_entries']) - 1} more similar entries available via get_team_context.")

    return "\n".join(lines)


@mcp.tool()
def get_pm_skill(skill_name: str) -> str:
    """
    Load full instructions for an Evols PM skill.

    Call this when the user's request maps to one of the PM skills listed in
    the session system message. Once loaded, follow the skill's instructions
    for the rest of the conversation.

    Args:
        skill_name: Exact skill name from the catalog (e.g. "identify-assumptions", "business-model")
    """
    config = load_config()
    api_url = config.get("api_url", "")
    api_key = config.get("api_key", "")

    if not api_url or not api_key:
        return "Evols not configured. Run the install script: bash ~/.evols/install.sh"

    try:
        resp = requests.get(
            f"{api_url.rstrip('/')}/api/v1/copilot/skills/{skill_name}",
            headers=api_headers(api_key),
            timeout=10,
        )
        if resp.status_code == 404:
            return f"Skill '{skill_name}' not found. Check the skill name from the session catalog."
        resp.raise_for_status()
        skill = resp.json()
    except Exception as e:
        return f"Could not load skill '{skill_name}': {e}"

    name = skill.get("name", skill_name)
    description = skill.get("description", "")
    instructions = skill.get("instructions", "")
    category = skill.get("category", "")

    header = f"## Skill: {name}"
    if category:
        header += f" [{category}]"
    if description:
        header += f"\n{description}"

    return f"{header}\n\n{instructions}"


@mcp.tool()
def get_quota_status(days: int = 7) -> str:
    """
    Show your team's token savings summary.

    Displays:
    - Total tokens saved this week vs. compiling knowledge fresh
    - Quota extension percentage
    - Knowledge graph growth
    - Rate limit incidents

    Args:
        days: Lookback period in days (default 7)
    """
    config = load_config()
    api_url = config.get("api_url", "")
    api_key = config.get("api_key", "")

    if not api_url or not api_key:
        return "Evols not configured."

    try:
        resp = requests.get(
            f"{api_url.rstrip('/')}/api/v1/team-knowledge/quota/summary",
            params={"days": days},
            headers=api_headers(api_key),
            timeout=10,
        )
        resp.raise_for_status()
        s = resp.json()
    except Exception as e:
        return f"Could not reach Evols API: {e}"

    lines = [
        f"## Evols Team Intelligence — Last {s['period_days']} days",
        f"",
        f"  Sessions tracked:          {s['sessions']}",
        f"  Tokens used:               ~{s['tokens_used']:,}",
        f"  Tokens retrieved (graph):  ~{s['tokens_retrieved']:,}",
        f"  Est. tokens saved:         ~{s['tokens_saved_estimate']:,}  ✦",
        f"  Quota extended by:         ~{s['quota_extended_pct']}%",
        f"",
        f"  Knowledge graph entries:   {s['knowledge_entries_total']} total  (+{s['knowledge_entries_new']} this period)",
    ]
    if s.get("rate_limit_hits", 0) > 0:
        lines.append(f"  Rate limit hits:           {s['rate_limit_hits']} (recorded for team visibility)")

    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    mcp.run(transport="stdio")
