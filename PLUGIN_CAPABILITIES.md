# Evols Claude Code Plugin — Capabilities & Technical Guide

---

## Complete Capabilities

### Hook Layer (automatic, no user action)

**SessionStart** (`plugin/hooks/session_start.py`)
- Fetches relevant team knowledge from Evols API using the working directory as the query
- Injects AI skills catalog into the session system message
- Initializes `session_state.json` with session ID, `started_at`, `tokens_retrieved`, `actual_savings`, `files_read`, `files_modified`, `discovery_tokens`, `plan_type`
- Configures the Evols MCP server in `.mcp.json` for the current working directory
- Refreshes `~/.evols/pricing.json` from LiteLLM's GitHub once per 24 hours

**UserPromptSubmit** (`plugin/hooks/user_prompt_submit.py`)
- On first prompt: re-injects relevant context if session_start didn't fire
- Stale session detection: clears session state if session ID doesn't match
- Redundancy check on the user's raw prompt text (≥ 30 chars), shows prior team work inline before Claude processes the prompt

**PostToolUse** (`plugin/hooks/post_tool_use.py`)
- After every `Read` call: appends path to `files_read` in session state
- After every `Write`/`Edit` call: appends path to `files_modified`
- After every tool call: accumulates `discovery_tokens` (raw output size ÷ 4 ≈ tokens)
- After `Bash`/`WebFetch` calls (≥ 40 chars): checks knowledge graph for redundant work and injects warning into Claude's reasoning chain via `additionalContext` (visible to Claude only, not terminal)
- After any MCP tool call: forwards response text to LightRAG knowledge graph (fire-and-forget)
- Caps accumulated paths at 50 each; keeps last 20 tool output summaries

**Stop / StopFailure** (`plugin/hooks/stop.py`)
- Parses the session JSONL transcript for exact token counts using streaming dedup (last record per `message.id` wins)
- Reads `costUSD` field from JSONL when present (Claude's own pre-calculated cost); falls back to `compute_cost()` only if absent
- Computes per-model cost using `~/.evols/pricing.json` (LiteLLM-sourced rates with hardcoded fallback)
- Computes burn rate: `total_tokens / session_minutes_elapsed`
- Tracks 5-hour billing block in `~/.evols/block_state.json`, accumulates tokens across sessions within the same block window
- Auto-syncs session to team knowledge graph via Haiku summarization of the transcript (skips trivial sessions)
- Forwards extracted knowledge summary to LightRAG
- Posts quota event to Evols API with: tokens used, tokens retrieved, tokens created, actual savings override, model, cost USD, CWD
- Displays: model, token breakdown, session duration, burn rate + projected block usage, session cost, 5h block progress + time-to-reset, team context savings, knowledge sync result
- On StopFailure: records rate limit hit with block usage display
- Cleans up `session_state.json`

### MCP Tool Layer (on-demand, Claude calls these)

| Tool | Description |
|------|-------------|
| `get_team_context` | Semantic search of the team knowledge graph by task description; returns pre-compiled context text with token savings estimate |
| `sync_session_context` | Manually add a knowledge entry with title, content, role, session type, entry type, tags, product area, token count; optionally attribute to a product |
| `link_to_product` | Attribute a knowledge entry to a specific product knowledge base |
| `check_redundancy` | Before starting any task, check if a teammate solved something similar in the past N hours; returns similarity score, their token cost, and estimated saving |
| `get_quota_status` | Full investment/reuse/net impact summary for the past N days: tokens invested, tokens retrieved, actual similarity-weighted savings, ROI%, knowledge entry counts |
| `get_skill` | Load full AI skill instructions on demand from the skills catalog |

### Knowledge Graph Layer (backend, transparent)

- **Three-layer search**: Layer 1 (50 tok/result index — title, tags, date, similarity), Layer 2 (preview + files context), Layer 3 (full content, increments retrieval counter)
- **Content hash dedup**: SHA256 16-char fingerprint per tenant prevents duplicate entries
- **Similarity-weighted savings**: partial-overlap retrievals get proportional credit
- **Investment vs. realized tracking**: creation events track `tokens_invested`; retrieval events track `actual_savings`; mixed sessions tracked separately

---

## Article: How the Evols Claude Code Plugin Works

### The Problem

Every AI coding session starts from zero.

You open Claude Code, describe your task, and the model begins exploring your codebase. It reads files, runs commands, fetches documentation, discovers patterns. By the end of a productive session, Claude has built a dense mental model of your system — what's been tried, what works, what the tricky edge cases are.

Then the session ends, and all of that is gone.

The next person on your team who touches the same area starts over. Claude reads the same files, discovers the same patterns, trips over the same pitfalls. Even you, returning to the same codebase tomorrow, start fresh. Every session pays the full exploration cost, again.

This isn't a niche problem. Claude Code charges against a token quota that resets every 5 hours. The average session burns 15,000–40,000 tokens just on exploration before any real work begins — reading files to understand structure, running commands to understand state, fetching docs to understand APIs. That's dead weight on every session, paid repeatedly.

There's a second, subtler problem: teams don't know what their AI is doing. No one can see which teammates hit rate limits today, whether the same problem is being solved in parallel, or whether the last sprint's AI-assisted research is accessible to the engineer who now needs it. The AI is a black box per user, per session.

The Evols Claude Code plugin solves both: it turns individual sessions into shared team memory, and it makes that memory automatically available at the start of every new session — so exploration tokens are spent once, not repeatedly.

---

### What It Does in Plain Terms

At the end of every Claude Code session, the plugin extracts the key insights and stores them in a team knowledge graph. At the start of every new session, it retrieves the most relevant entries and injects them into Claude's context before the first message.

The practical effect: when an engineer starts a session on the payments service tomorrow, they don't start from zero. Claude already knows that the idempotency key logic was refactored last week, that the webhook retry mechanism has a known off-by-one in the backoff timing, and that the staging environment requires a specific environment variable to enable test mode. That's context that would have cost 8,000 tokens to rediscover. It's now delivered in 1,000 tokens of pre-compiled knowledge.

Beyond that, the plugin watches for redundant work in real time. When Claude is about to run a `Bash` command or fetch a URL that a teammate already ran recently, it silently warns Claude mid-session: *a teammate ran this 6 hours ago and their result is in the knowledge graph — do you need to run it again?* The warning goes into Claude's reasoning chain, not the terminal. Claude can silently skip the redundant tool call and use the cached result instead.

---

### Steps to Use It

**Installation** is a one-time step:

```bash
bash <(curl -s https://api.evols.ai/install/claude-code)
```

The installer places three hook scripts and one MCP server into `~/.evols/`, registers them in your Claude Code settings, and writes your API key to `~/.evols/config.json`.

**After that, everything is automatic.** You don't change how you work with Claude Code. The hooks fire on lifecycle events without any user action:

1. Start a Claude Code session → team knowledge is injected automatically into the system message
2. Work normally → file reads, writes, and tool calls are tracked silently in the background
3. End the session → Claude summarizes the session via Haiku and adds the entry to the team graph; quota usage is recorded

**Manual tools** are available when you want explicit control. Claude can call these mid-session:

- `get_team_context("payments idempotency")` — pull specific knowledge on demand
- `sync_session_context(...)` — add a knowledge entry right now without waiting for session end
- `check_redundancy("implement webhook retry logic")` — explicitly ask if a teammate already built this
- `get_quota_status()` — see the team's token investment and savings for the past week

---

### Architecture: Four Layers

The plugin is structured in four layers, each with a distinct job:

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Lifecycle Hooks (session_start, stop, etc.)   │
│  Fire automatically on Claude Code events               │
├─────────────────────────────────────────────────────────┤
│  Layer 2: MCP Server (get_team_context, etc.)           │
│  Tools Claude calls mid-session on demand               │
├─────────────────────────────────────────────────────────┤
│  Layer 3: Session State (~/.evols/session_state.json)   │
│  Scratchpad that accumulates data across hook calls     │
├─────────────────────────────────────────────────────────┤
│  Layer 4: Evols API + Knowledge Graph (backend)         │
│  Stores, embeds, retrieves, and deduplicates entries    │
└─────────────────────────────────────────────────────────┘
```

**Layer 1 — Lifecycle Hooks** are Python scripts invoked by Claude Code on specific events. They receive a JSON payload on stdin and write a JSON response to stdout. They have no persistent process — each fires, does its work, and exits. There are four:

- `SessionStart` — fires when Claude Code starts
- `UserPromptSubmit` — fires before each prompt is sent to Claude
- `PostToolUse` — fires after each tool call completes
- `Stop` / `StopFailure` — fires when the session ends (normally or due to rate limit)

**Layer 2 — MCP Server** is a persistent process that Claude Code launches alongside the session. It exposes tools that Claude can call like any other tool (`Bash`, `Read`, etc.). The Evols MCP server runs locally and proxies calls to the Evols API.

**Layer 3 — Session State** is a JSON file at `~/.evols/session_state.json`. It's the communication channel between hooks, since hooks can't call each other directly. `SessionStart` creates it; `PostToolUse` updates it after every tool call; `Stop` reads it and then deletes it. It holds: session ID, working directory, start timestamp, tokens retrieved, actual savings, files read, files modified, discovery tokens accumulated, plan type.

**Layer 4 — Evols API** is the persistent backend. It stores knowledge entries with vector embeddings, handles semantic search, computes similarity scores, tracks quota events, and enforces deduplication.

---

### How the Data Flows

#### Session Start

```
Claude Code starts
  → session_start.py fires
    → refresh_pricing_cache() [if pricing.json > 24h old, fetch from LiteLLM GitHub]
    → fetch GET /api/v1/copilot/skills  [AI skills catalog]
    → fetch GET /api/v1/team-knowledge/relevant?query=<cwd>&top_k=5
    → write session_state.json  [tokens_retrieved, actual_savings, started_at, ...]
    → inject system message:
        "[Evols] Loaded 4 entries (3,200 tokens · ~22,400 tokens saved)"
        + AI skills catalog
        + compressed team knowledge text
    → write .mcp.json  [registers Evols MCP server for this project]
```

The query to the knowledge graph uses the current working directory path as the search term. This is surprisingly effective — `/Users/alice/projects/payments-service` is a stronger discriminator than it appears, because it matches entries tagged with related filenames, product areas, and technologies.

#### During the Session

Every tool call routes through `PostToolUse`:

```
Claude calls Read("/src/payments/idempotency.py")
  → post_tool_use.py fires
    → append "/src/payments/idempotency.py" to state.files_read
    → accumulate discovery_tokens += len(tool_output) // 4

Claude calls Bash("grep -r 'retry_count' ./src")
  → post_tool_use.py fires
    → accumulate discovery_tokens
    → query GET /api/v1/team-knowledge/redundancy-check?query=grep+-r+...
    → if similar entry found (similarity ≥ 0.75, within 48h):
        → inject additionalContext into Claude's reasoning
          "[Evols] A teammate already did similar work (87% match): ..."
        → Claude sees this warning; terminal does NOT
```

The `additionalContext` mechanism is the critical design choice here. It puts the warning in Claude's reasoning chain — the model can silently decide to skip the redundant tool call and use the cached result. The user never sees noise; Claude just makes a smarter decision.

#### Session End

```
Session ends
  → stop.py fires
    → reload MODEL_PRICING from ~/.evols/pricing.json
    → parse_transcript_usage(transcript_path)
        → for each JSONL line:
            → if "costUSD" present: add to cost_usd_sum
            → group usage by message.id, keep last record per ID
            → sum final usage totals
    → compute burn rate: total_tokens / session_minutes
    → update 5h block state in ~/.evols/block_state.json
    → extract_transcript_text() [last 60 turns]
    → auto_sync_knowledge() via Haiku:
        → POST to Anthropic API with session transcript
        → Claude Haiku extracts: title, content, entry_type, tags, product_area
        → if response == "SKIP": skip (trivial session, no knowledge worth storing)
        → POST to /api/v1/team-knowledge/entries
    → POST /api/v1/team-knowledge/quota/events
    → display summary to terminal
    → delete session_state.json
```

---

### The Key Calculations

#### 1. Token Count from Transcript

Claude Code writes a JSONL file during the session. Each API response generates multiple lines — one per streaming chunk — all sharing the same `message.id`. The usage fields in each chunk are *cumulative totals*, not deltas. Naively summing all lines would massively overcount.

```python
per_message: dict[str, tuple] = {}  # message_id → (usage, model)

for line in transcript:
    entry = json.loads(line)
    # Prefer Claude's own cost calculation when present
    if "costUSD" in entry:
        cost_usd_sum += float(entry["costUSD"])
    message_id = msg.get("id")
    if message_id:
        per_message[message_id] = (usage, model)  # last write wins
```

Final totals are computed by summing only the *last* record per `message.id`. This gives exact counts for input tokens, output tokens, cache reads, and cache writes.

#### 2. Session Cost

The cost calculation uses per-model pricing loaded from `~/.evols/pricing.json`:

```python
cost = (
    input_tokens  * input_rate  +
    output_tokens * output_rate +
    cache_reads   * cache_read_rate  +
    cache_writes  * cache_write_rate
) / 1_000_000
```

Rates (per million tokens):

| Model | Input | Output | Cache Read | Cache Write |
|-------|-------|--------|-----------|-------------|
| Opus | $5.00 | $25.00 | $0.50 | $6.25 |
| Sonnet | $3.00 | $15.00 | $0.30 | $3.75 |
| Haiku | $1.00 | $5.00 | $0.10 | $1.25 |

When the transcript contains `costUSD` fields (written by Claude directly), those are summed and used instead. This is more accurate than our calculation because Claude knows its own billing arithmetic.

The pricing file is fetched from LiteLLM's community-maintained GitHub JSON once per 24 hours. LiteLLM tracks Anthropic's published rates and updates when they change. The plugin falls back to the hardcoded table if the fetch fails or the file is malformed.

#### 3. Burn Rate and Projected Block Usage

```python
session_minutes = (now - started_at).total_seconds() / 60
burn_rate_per_min = total_tokens / session_minutes
projected_block = burn_rate_per_min * 5 * 60  # tokens over a full 5h block
```

This is an extrapolation, not a measurement — a session that was 80% exploration and 20% writing will have a different burn rate than one that's 20% exploration. The value is useful as a ceiling estimate: *if this pace continued for the full block, how much would it cost?*

#### 4. 5-Hour Billing Block

Claude Code's subscription quota resets every 5 hours, not daily. Tracking daily usage produces wrong % estimates. The plugin maintains `~/.evols/block_state.json`:

```json
{"block_start": "2026-05-01T10:00:00", "block_tokens": 34200}
```

On every session end, it checks elapsed time since `block_start`. If < 5 hours, it adds this session's tokens to `block_tokens` and shows remaining time. If ≥ 5 hours, it starts a new block. This accumulates correctly across multiple sessions within the same billing window.

#### 5. Similarity-Weighted Savings

This is the most important calculation — and the one most knowledge-sharing tools get wrong.

Naïve approach: if you retrieved a 5,000-token entry, you "saved" 5,000 tokens. But that's only true if the retrieved entry *perfectly* covers your task. If it only overlaps 30%, you'll still do 70% of the work yourself.

The plugin uses similarity score as a proxy for overlap:

```python
# Per entry at retrieval time:
measured_ratio = discovery_tokens / token_count
# discovery_tokens: raw tool output size before Haiku compression
# token_count: compressed entry size stored in the graph

entry_savings = token_count * (measured_ratio - 1) * similarity_score
```

`measured_ratio` is the measured compression factor for that specific entry — how much raw exploration was needed to produce it. A ratio of 8 means 8,000 tokens of tool output produced a 1,000-token entry. When a teammate retrieves that entry, they save `(8-1) * similarity * 1000 = 7000 * similarity` tokens.

`similarity_score` is the cosine similarity of the query embedding vs. the entry embedding, computed at retrieval time. A 0.90 match on a 1,000-token entry with 8x ratio saves ~6,300 tokens. A 0.50 match saves ~3,500 tokens. The savings scale proportionally rather than being all-or-nothing.

#### 6. Investment vs. Realized Savings Accounting

The plugin separates two fundamentally different events:

**Creation event** (when `tokens_created > 0`): A session produced new knowledge. The session's total token count is the *investment* — paid now, with potential future value. Savings are not claimed.

**Retrieval event** (when `tokens_retrieved > 0`): A session loaded existing knowledge. The similarity-weighted savings are the *realized savings* — value extracted from prior investment.

The dashboard shows both sides:

```
Knowledge Investment (12 creation sessions):
  Tokens invested:    ~284,000
  Potential value:    ~1,988,000  (if retrieved by the whole team)

Knowledge Reuse (8 retrieval sessions):
  Tokens retrieved:   ~18,400
  Actual savings:     ~128,800  ✦

Net impact: +128,800 tokens  (+45% ROI)
```

ROI here means: for every token spent creating knowledge, the team has recouped 1.45 tokens in retrieval savings so far. The potential value shows what's possible if the created knowledge is fully utilized.

#### 7. Deduplication

Before inserting a new knowledge entry, the backend computes:

```python
content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
```

A unique constraint on `(tenant_id, content_hash)` prevents the same content from being stored twice. This matters because `StopFailure` (rate limit) and a subsequent retry can both attempt to sync the same session.

---

### What Gets Stored and Why

Each knowledge entry contains:

| Field | Description |
|-------|-------------|
| `title` | One-line summary (≤ 80 chars), generated by Haiku |
| `content` | 3–8 sentence synthesis of problem, approach, decision, outcome |
| `entry_type` | `insight`, `decision`, `artifact`, `research_finding`, `pattern`, `context` |
| `tags` | 2–5 keywords for keyword-based retrieval |
| `product_area` | Which part of the product this affects |
| `files_read` | List of files Claude read during the session |
| `files_modified` | List of files Claude wrote or edited |
| `discovery_tokens` | Raw tool output size before compression (the honest savings basis) |
| `token_count` | Compressed entry size (what it costs to load this entry) |
| `model` | Which Claude model produced this entry |
| `content_hash` | 16-char SHA256 fingerprint for dedup |
| `vector embedding` | Computed by the backend for semantic search |

The separation of `discovery_tokens` (exploration cost) from `token_count` (storage cost) is what makes honest savings calculations possible. Without `discovery_tokens`, you can only say "this entry is 1,000 tokens." With it, you can say "this entry compressed 8,000 tokens of raw exploration into 1,000 tokens — anyone who retrieves it skips 7,000 tokens of work."

---

### Three-Layer Search

The knowledge graph supports three levels of detail to minimize unnecessary token consumption:

**Layer 1** (`GET /search/layer1`): Returns title, tags, date, similarity score only — about 50 tokens per result. Used for scanning: "do any of these entries look relevant?"

**Layer 2** (`GET /search/layer2`): Returns content preview, `files_read`, `files_modified`, compression ratio. Used for validation: "is this actually about what I'm working on?"

**Layer 3** (`GET /search/layer3`): Returns full content. Also increments `retrieval_count` on each entry so the dashboard can show which entries are actually being used. Used for consumption: "give me the full knowledge."

This progressive disclosure means a session that finds nothing useful in Layer 1 never pays for Layer 2 or 3 content.

---

### What This Is Not

The plugin does not replace writing tests, code review, or documentation. It is not a codebase indexer — it doesn't index your source files or answer "where is X defined." It specifically captures *AI session outcomes*: what was explored, what was decided, what patterns were found, what problems were solved. The unit is a session, not a file or a commit.

It also does not make token costs disappear. If your team is hitting rate limits, the plugin helps you spend tokens more efficiently — fewer repeated explorations, more context reuse — but it doesn't change the quota itself. The 5-hour block display is there specifically to show you when you're close to the ceiling so you can pace accordingly.
