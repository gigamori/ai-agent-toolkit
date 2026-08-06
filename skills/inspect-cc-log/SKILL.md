---
name: inspect-cc-log
description: >
  Investigate past Claude Code sessions by querying the JSONL logs with SQL over
  a set of pre-built DuckDB views (conversation text, tool calls with arguments,
  file changes, forks, compaction, per-session aggregates). Use when reconstructing
  what happened in a prior session, auditing tool/subagent calls, tracing a file's
  change history, finding messages or events across sessions, or bundling a fork
  tree. Supersedes extract-cc-log: resolve a session by title via cc_session, then
  read its turns from cc_block. Trigger: session log, transcript, past session,
  tool history, file change history, fork, compaction, 過去セッション, 行動履歴, ログ調査.
allowed-tools: "Bash(uv run *query.py *)"
---

# Inspect CC Log

Eight layered DuckDB **views** over `~/.claude/projects/**/*.jsonl`. Pick a view,
then `select`/`where` on its columns; `join` across views by the ids below.

If `$CLAUDE_CONFIG_DIR` is set, the views read that config dir's `projects/`
**and** `~/.claude/projects` (whichever actually holds logs), so a moved config
dir needs no flag.

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

## Views — what one row means

| view | one row = | key columns |
|---|---|---|
| `cc_record` | one JSONL line (raw) | `j` (raw JSON), `file_name`, `file_kind` (session/agent/journal), `record_type` |
| `cc_event` | one DAG record (assistant/user/attachment/system) | `ts`, `session_id`, `bundle_id`, `uuid`, `parent_uuid`, `type`, `subtype`, `role`, `model`, `git_branch`, `cwd`, `entrypoint`, `version`, `is_sidechain`, `agent_id`, `permission_mode`, `usage_*`, `attribution_*`, `content`, `file_kind` |
| `cc_block` | one content block (UNNEST of message.content) | `record_uuid`, `session_id`, `ts`, `block_index`, `block_type`, `text`, `thinking`, `tool_use_id`, `tool_name`, `tool_input`, `tool_result_content`, `is_error` |
| `cc_tool` | one tool call + its result | `tool_use_id`, `session_id`, `bundle_id`, `tool_name`, `tool_input`, `ts_call`, `ts_result`, `is_error`, `result_text`, `file_path`, `structured_patch`, `user_modified`, `stdout`, `stderr`, `interrupted`, `file_kind` |
| `cc_turn` | one user prompt + latency | `session_id`, `user_uuid`, `user_ts`, `next_ts`, `latency_ms` |
| `cc_session` | one session_id (fork children fold in) | `title`, `started`, `ended`, `cwd`, `git_branch`, `version`, `entrypoint`, `n_user`, `n_assistant`, `n_tool`, `n_error`, `n_compaction`, `n_agent_events`, `tok_*`, `pr_number`, `bridge_session_id`, `bundle_id` |
| `cc_fork` | one fork edge (cross-session) | `agent_id`, `parent_session_id`, `parent_last_uuid`, `context_length`, `bundle_id`, `lineage_source` |
| `cc_compaction` | one compaction boundary (intra-session) | `session_id`, `ts`, `logical_parent_uuid`, `trigger`, `pre_tokens`, `post_tokens`, `duration_ms`, `preserved_tail_uuid` |

## ids — what each identifies and how to join

| id | identifies | join use |
|---|---|---|
| `session_id` | one session (`<uuid>.jsonl`) | primary key linking all views of a session |
| `uuid` / `parent_uuid` | one record / its parent | walk the conversation DAG; tool result → emitter |
| `tool_use_id` (`toolu_…`) | one tool call | `cc_block` ↔ `cc_tool` join key (call ↔ result) |
| `bundle_id` | the fork-tree root session | `where bundle_id=<root>` gathers a whole fork tree (children share the parent session_id) |
| `agent_id` | one Agent-tool child (`agent-*.jsonl`) | `cc_fork` → `parent_session_id` |
| `parent_last_uuid` | parent's last record at fork time | locate the fork point in the parent session |
| `logical_parent_uuid` | pre-compaction tail (intra-session) | `cc_compaction` → the message a compaction cut from |
| `request_id` (`req_…`) | one API call | group assistant turn + usage/error |
| `bridge_session_id` (`cse_…`) | server-side bridge session (metadata only) | not a lineage edge; see design.md |

Fork edges (`cc_fork`: `parent_session_id` / `parent_last_uuid`) and compaction
edges (`cc_compaction`: `logical_parent_uuid`) are separate record types and are
never mixed.

## Gotcha — parenthesize `->>` in WHERE

DuckDB mis-plans `<col> = x AND j->>'k' = y` (it can try to cast the whole row to a
number). Always parenthesize a JSON extraction used in a predicate:
`... AND (j->>'subtype') = 'compact_boundary'`. The views already do this; apply it
in ad-hoc queries that reach into `cc_record.j` or `tool_input`.

## Gotcha — non-ASCII text (titles, Japanese prompts) still garbling

`query.py` forces UTF-8 stdout/stderr, so its own JSON output is always correct
UTF-8 bytes. If a title or prompt text still shows as replacement characters
(`�`) or mismatched glyphs, the corruption is happening **after** the script —
typically an agent harness or terminal on a non-UTF-8-locale system (e.g. cp932
on JA Windows) re-decoding the piped output for display. That re-decode is lossy
in its own right: once a harness has captured and shown you the garbled text, that
*is* the data it has to work with — piping through the shell is not a safe read
path here. Work around it by writing the query output to a file and reading that
file with a UTF-8-aware file-read tool instead of piping/catting/printing it
through the shell.

## More

- Ready-to-run investigation queries (file history, event detection, fork bundling,
  joins): [references/sql-examples.md](references/sql-examples.md).
- Design, grain rationale, and verified log facts: [references/design.md](references/design.md).
