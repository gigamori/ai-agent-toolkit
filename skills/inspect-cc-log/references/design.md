# inspect-cc-log — design & verified log facts

Design rationale for the layered views, plus the empirical facts they rest on.
Investigated against the local corpus (`~/.claude/projects/**/*.jsonl`,
~1.78k files / ~160k records).

## Goals & decisions

- Investigate past CC sessions by SQL: conversation, tool calls + arguments, file
  changes, forks, compaction, per-session aggregates.
- Both single-session reconstruction and cross-session analysis.
- Access path: **views only** (always fresh; each query re-reads the logs; no
  materialization). Supersedes extract-cc-log (title lookup → `cc_session` + `where`).
- Self-contained: `scripts/query.py` opens an in-memory DuckDB, defines the views,
  and runs the query. No persistent database, no connection config, no run-sql
  dependency. Defining 8 views is microseconds; the cost is the log read at query time.

## Why layered views (not one wide table)

CC records span four grains; flattening into one row shape forces NULL-sparsity,
loses relational edges, and breaks aggregation. So: one base + grain-aligned views.

| view | grain | source |
|---|---|---|
| `cc_record` | one JSONL line (raw JSON) | `read_json_objects` over the glob |
| `cc_event` | one DAG record | cc_record (assistant/user/attachment/system) |
| `cc_block` | one content block | UNNEST of `message.content` |
| `cc_tool` | one tool call + result | tool_use ↔ tool_result by `tool_use_id` |
| `cc_turn` | one user prompt + latency | cc_event |
| `cc_session` | one session_id | aggregate of cc_event/cc_tool/cc_compaction |
| `cc_fork` | one fork edge | `fork-context-ref` records |
| `cc_compaction` | one compaction boundary | `system/compact_boundary` records |

## Base extraction

`read_json_objects(glob, format='newline_delimited', filename=true, ignore_errors=true)`
returns a raw `json` column + `filename`, immune to per-record schema heterogeneity
(notably `message.content` being string OR array). `~` is expanded and stored
literally in the catalog, so no absolute path is baked into a view definition.

## Verified log facts the views encode

- **Record types (16)**: assistant, user, attachment, file-history-snapshot,
  queue-operation, ai-title, last-prompt, system, mode, permission-mode,
  custom-title, bridge-session, fork-context-ref, started, result, pr-link.
  `started`/`result` live only in `journal.jsonl`; `fork-context-ref` only in
  `agent-*.jsonl`.
- **`system` subtypes (8)**: stop_hook_summary, api_error, turn_duration,
  local_command, away_summary, model_refusal_fallback, compact_boundary,
  informational.
- **Content blocks**: text, tool_use{id,name,input}, tool_result{tool_use_id,content,is_error},
  thinking, image, fallback. `message.content` is an ARRAY or a plain string; the
  string form is normalized to one text block so multi-block records are not
  truncated (the old `content[0]`-only loss; ~25k user blocks at index > 0).
- **Tool pairing**: `tool_use.id == tool_result.tool_use_id` (100%; verified ~149k
  pairs). `sourceToolAssistantUUID` is NOT a reliable pairing key (57%) and is not used.
- **`toolUseResult` variants** (top-level on the user record): Edit/Write carry
  `filePath`, `structuredPatch` (hunks `{oldStart,oldLines,newStart,newLines,lines}`),
  `userModified`; Bash carries `stdout`/`stderr`/`interrupted` (no numeric exit code).
- **Fork lineage (cross-session)**: `fork-context-ref{agentId, parentSessionId,
  parentLastUuid, contextLength}` in `agent-*.jsonl`; `parentSessionId`/`parentLastUuid`
  resolve into the parent file. The only structural child→parent backlink.
- **Compaction lineage (intra-session)**: `compact_boundary.logicalParentUuid`
  → `compactMetadata.preservedSegment.tailUuid`. Separate record type from fork
  edges; the two never co-occur and are kept in separate views.
- **bridge-session** (`{sessionId, bridgeSessionId: cse_…, lastSequenceNum}`):
  a local↔server bridge pointer, 1:1 per session, NOT a lineage edge; surfaced only
  as `cc_session.bridge_session_id`. Not in fork bundling. (Detail: the §9.5 entry
  in claude-code-context-inheritance.md.)

## Resolved build-time questions

- **read_json_objects**: column is `json`; `filename=true` and `~`-glob both work.
- **Agent record session_id (was TBD)**: fork-child records carry the **parent's**
  `sessionId` (verified 308/308). So `bundle_id = session_id` is sufficient —
  filtering a root session id already co-locates its fork children; `cc_fork`
  retains the explicit edge metadata (`agent_id`, `parent_last_uuid`, `context_length`).
- **No persistence needed**: an earlier design baked views into a file-backed
  `.duckdb` for the run-sql engine (which runs without AUTOCOMMIT, so it required an
  explicit `BEGIN`/`COMMIT`). The self-contained `query.py` defines the views in an
  in-memory DuckDB per run instead, dropping the database file, the COMMIT dance, the
  `cc` connection, and the run-sql dependency.
- **DuckDB WHERE gotcha**: `<col> = x AND j->>'k' = y` can mis-plan and try to cast
  the whole row to a number. Parenthesize the extraction: `(j->>'k') = y`. The views
  apply this; ad-hoc queries reaching into `cc_record.j`/`tool_input` must too.

## Costs & limits

- Each query re-reads all logs (a few seconds; ~0.3–2s observed per view count, more
  for wide scans). File pruning by `session_id` is not possible (sid lives inside the
  JSON, not the filename), so single-session queries pay the same scan — accepted in
  exchange for always-fresh data and zero materialization.
- `query.py` caps output at 200 rows / 50KB; use `count`/`group by`/`limit` for breadth.
- Live appends during a query make counts a momentary snapshot (read-only; no risk).

## Files

- `scripts/query.py` — self-contained runner (in-memory views + the query, JSON out).
- `scripts/views.sql` — the 8 view definitions (dependency-ordered).
- `references/sql-examples.md` — investigation queries.
