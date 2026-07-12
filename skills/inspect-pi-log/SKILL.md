---
name: inspect-pi-log
description: >
  Investigate past Pi Coding Agent sessions by querying the JSONL logs with SQL over
  a set of pre-built DuckDB views (conversation text, tool calls with arguments,
  file changes, session lineage/bundles, compaction, in-file branches, per-session
  aggregates). Use when reconstructing what happened in a prior Pi session, auditing
  tool/subagent calls, tracing a file's change history, finding messages or events
  across sessions, or bundling a subagent/skill-fork/handoff/fork tree. Resolve a
  session by title via pi_session, then read its turns from pi_block. Trigger: pi
  session log, transcript, past session, tool history, file change history, subagent,
  skill-fork, handoff, fork, bundle, compaction, 過去セッション, 行動履歴, ログ調査.
allowed-tools: "Bash(uv run *query.py *)"
---

# Inspect Pi Log

Nine layered DuckDB **views** over `~/.pi/agent/sessions/**/*.jsonl` (Pi Coding Agent
session logs; the glob also captures `subagents/` children). Pick a view, then
`select`/`where` on its columns; `join` across views by the ids below.

## Run a query

```bash
uv run scripts/query.py --sql "<SQL>"
```
Self-contained: the script defines the views in-memory and runs your SQL — no setup,
no database file, no connection config. The views read the logs lazily, so results
are always fresh (each query re-reads the logs, a few seconds). Output is JSON
`{columns, rows, row_count}`; SQL can also be piped on stdin.

Defaults: 200 rows / 50KB output (`--max-rows` / `--max-bytes` to change). Keep it to
one `select` per call.

## Pi vs Claude Code (if you know inspect-cc-log)

- **session_id is not stored in-line** — it is derived from the file name's UUID
  (`<ts>_<uuid>.jsonl`). Every view already carries `session_id`.
- **The type axis is one level deeper**: a conversation row is `type='message'` with
  the role under `message.role` (`user` / `assistant` / `toolResult` / `bashExecution`
  / …). `pi_event.type` is the entry type; `pi_event.role` is the message role.
- **tool_result is a standalone message** (role=`toolResult`), not a content block.
  `pi_tool` joins the `toolCall` block to that message.
- **Lineage is richer**: fork (file-level `parentSession`), in-file branch, subagent,
  skill-fork, and pi-studio `/handoff` — all unified in `pi_link`.

## Views — what one row means

| view | one row = | key columns |
|---|---|---|
| `pi_record` | one JSONL line (raw) | `j` (raw JSON), `file_name`, `session_id`, `file_kind` (session/subagent), `record_type` |
| `pi_event` | one entry (all 10 types + header) | `ts`, `session_id`, `id`, `parent_id`, `type`, `role`, `model`, `provider`, `thinking_level`, `stop_reason`, `error_message`, `response_id`, `usage_*`, `usage_total`, `cost_total`, `custom_type`, `file_kind` |
| `pi_block` | one content block (UNNEST of message.content) | `record_id`, `session_id`, `ts`, `block_index`, `block_type` (text/thinking/toolCall/image), `text`, `thinking`, `tool_call_id`, `tool_name`, `tool_arguments` |
| `pi_tool` | one tool call + its result | `tool_call_id`, `session_id`, `tool_name`, `tool_arguments`, `file_path`, `ts_call`, `ts_result`, `is_error`, `result_text`, `details`, `diff`, `first_changed_line`, `truncation`, `full_output_path`, `file_kind` |
| `pi_turn` | one user prompt + latency | `session_id`, `user_entry_id`, `user_ts`, `next_ts`, `latency_ms` |
| `pi_session` | one session_id (empty/header-only allowed) | `file_kind`, `title` (session_info.name → first user text), `started`, `ended`, `cwd`, `format_version`, `n_user`, `n_assistant`, `n_tool_result`, `n_tool`, `n_error`, `n_compaction`, `tok_*`, `cost_total`, `parent_session_path` |
| `pi_link` | one lineage edge (parent→child) | `kind` (fork/subagent/skill-fork/handoff), `parent_session_id`, `child_session_id`, `parent_leaf_id` (handoff only), `goal` (handoff only), `bundle_id` |
| `pi_compaction` | one compaction (intra-session) | `session_id`, `ts`, `parent_id`, `summary`, `first_kept_entry_id`, `tokens_before`, `from_hook` |
| `pi_branch` | one in-file branch point (Pi-specific) | `session_id`, `branch_entry_id`, `parent_id`, `from_id`, `summary`, `ts` |

## ids — what each identifies and how to join

| id | identifies | join use |
|---|---|---|
| `session_id` | one session (UUID from `<ts>_<uuid>.jsonl`) | primary key linking all views of a session |
| `id` / `parent_id` | one entry / its parent (8-hex) | walk the conversation DAG within a session |
| `tool_call_id` | one tool call (8-char, **not** globally unique) | `pi_block` ↔ `pi_tool`; call ↔ result join is on **(session_id, tool_call_id)** |
| `parent_session_id` / `child_session_id` | the two ends of a lineage edge (`pi_link`) | join to `pi_session.session_id`; join on the UUID, not the path |
| `bundle_id` | the root session of a spawn tree | `pi_link.bundle_id` gathers a whole fork/subagent/skill-fork tree (handoff excluded) |
| `parent_leaf_id` | parent's leaf entry at handoff time | locate the handoff point in the parent session |
| `first_kept_entry_id` | the entry a compaction kept as its new tail | `pi_compaction` → the surviving context boundary |
| `from_id` | the entry a branch diverged from | `pi_branch` → the fan-out point in the same file |

Lineage kinds and how `pi_link` resolves them (specific overrides the header default):

1. `header.parentSession` (a file path) → an edge, default `kind='fork'`.
2. a child `session-lineage` custom entry → `kind='handoff'` (+ `parent_leaf_id`, `goal`).
3. parent-side `sessionLineage` in details → `kind='subagent'` or `'skill-fork'`.

`bundle_id` follows non-handoff edges to the topmost root. Handoff is a summarized
separate thread and is **excluded** from bundles (filter `kind='handoff'` to follow it).

## Gotcha — parenthesize `->`/`->>` in WHERE

DuckDB mis-plans `<col> = x AND j->>'k' = y` (it can bind the arrow against a boolean
or cast the row to a number). Always parenthesize a JSON extraction used in a predicate:
`... AND (j->>'customType') = 'session-lineage'`. The views already do this; apply it in
ad-hoc queries that reach into `pi_record.j` or `tool_arguments`/`details`.

## Verified log facts (measured 2026-07-07, 539 files / ~11,000 entries)

- toolCall ↔ toolResult pairing = **99.17%** (2507/2528); the unpaired calls are
  aborted / errored assistant turns that emitted a call but no result.
- Entry types: session / message / model_change / thinking_level_change / compaction /
  branch_summary / custom / custom_message / label / session_info. Roles observed:
  user / assistant / toolResult (bashExecution / branchSummary / compactionSummary = 0).
- `subagents/` children carry `header.parentSession` and are invisible to `/resume`
  (enumeration is non-recursive); `pi_link` recovers them via the parent's `sessionLineage`.

## Example queries

```bash
# Find a session by title, newest first
uv run scripts/query.py --sql "SELECT session_id, started, title, n_user, n_tool FROM pi_session WHERE title ILIKE '%handoff%' ORDER BY started DESC LIMIT 20"

# Read a session's conversation text in order
uv run scripts/query.py --sql "SELECT ts, role, block_type, coalesce(text, thinking, tool_name) FROM pi_block WHERE session_id='<sid>' ORDER BY ts, block_index"

# A file's change history across all sessions (edit/write diffs)
uv run scripts/query.py --sql "SELECT session_id, ts_call, tool_name, first_changed_line FROM pi_tool WHERE file_path ILIKE '%config.ts' AND diff IS NOT NULL ORDER BY ts_call"

# Bundle a spawn tree (subagent/skill-fork/fork) from a root session
uv run scripts/query.py --sql "SELECT kind, parent_session_id, child_session_id FROM pi_link WHERE bundle_id='<root_sid>'"

# Errored / aborted assistant turns
uv run scripts/query.py --sql "SELECT session_id, ts, model, stop_reason, error_message FROM pi_event WHERE role='assistant' AND stop_reason IN ('error','aborted') ORDER BY ts DESC LIMIT 50"

# Per-session token + cost totals
uv run scripts/query.py --sql "SELECT session_id, title, tok_input, tok_output, tok_cache_read, cost_total FROM pi_session ORDER BY cost_total DESC NULLS LAST LIMIT 20"
```
