-- inspect-pi-log: layered views over Pi Coding Agent session logs (JSONL).
-- Base reads ~/.pi/agent/sessions/**/*.jsonl as raw JSON objects (no schema
-- inference), so heterogeneous entry shapes never collide. `~` is expanded by
-- DuckDB and stored literally in the catalog (no absolute path baked in). The
-- glob also captures children under `subagents/` subdirectories.
--
-- That glob below is an ANCHOR: when $PI_CODING_AGENT_SESSION_DIR or
-- $PI_CODING_AGENT_DIR is set, scripts/query.py replaces it (literal string
-- match, exactly one occurrence) with the list of every session root that has
-- logs, env universes first. Keep it spelled out once, quoted, in the FROM
-- clause only — the mentions in these comments stay unquoted on purpose.
--
-- scripts/query.py applies these definitions into an in-memory DuckDB on every
-- run, then executes the user's SQL. The views read the logs lazily, so results
-- are always fresh (each query re-reads the logs). No persistent database.
-- Views are ordered by dependency (DuckDB binds a view at creation time).
--
-- Pi vs Claude Code, key differences baked into these views:
--   * session_id is NOT stored in-line; it is derived from the file name's UUID.
--   * A message entry wraps role/content under `message`; type axis is 1 level
--     deeper than CC. Roles: user / assistant / toolResult / bashExecution / ...
--   * tool_result is a standalone message (role=toolResult), not a content block.
--   * assistant content blocks: text / thinking / toolCall {id,name,arguments}.
--   * lineage: header.parentSession (file path) + `session-lineage` custom entry
--     (handoff) + parent-side `sessionLineage` in details (subagent/skill-fork).

-- L0: one row per JSONL line (raw JSON + file identity + derived session_id) -----------
CREATE OR REPLACE VIEW pi_record AS
SELECT
  json                                             AS j,
  filename                                         AS file_path_raw,
  regexp_replace(filename, '^.*[\\/]', '')         AS file_name,
  -- session_id = the 36-char UUID in the file name `<ts>_<uuid>.jsonl`
  -- (spec decision: derive from file name, not from any in-line field).
  regexp_extract(
    regexp_replace(filename, '^.*[\\/]', ''),
    '([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', 1
  )                                                AS session_id,
  -- file_kind: children spawned by subagent/skill-fork live under a `subagents/`
  -- subdirectory (invisible to /resume); everything else is a top-level session.
  CASE WHEN regexp_matches(filename, '[\\/]subagents[\\/]') THEN 'subagent' ELSE 'session' END AS file_kind,
  json->>'type'                                    AS record_type
FROM read_json_objects('~/.pi/agent/sessions/**/*.jsonl',
       format='newline_delimited', filename=true, ignore_errors=true);

-- L1: event grain (one entry, all 10 entry types + header) ------------------------------
CREATE OR REPLACE VIEW pi_event AS
SELECT
  try_cast(j->>'timestamp' AS timestamp)           AS ts,
  session_id,
  j->>'id'                                         AS id,
  j->>'parentId'                                   AS parent_id,
  record_type                                      AS type,
  j->'message'->>'role'                            AS role,
  -- model: assistant message model; for model_change entries, its modelId.
  coalesce(j->'message'->>'model', j->>'modelId')  AS model,
  j->>'provider'                                   AS provider,           -- model_change only
  j->>'thinkingLevel'                              AS thinking_level,     -- thinking_level_change only
  j->'message'->>'stopReason'                      AS stop_reason,
  j->'message'->>'errorMessage'                    AS error_message,
  j->'message'->>'responseId'                      AS response_id,
  try_cast(j->'message'->'usage'->>'input'      AS bigint) AS usage_input,
  try_cast(j->'message'->'usage'->>'output'     AS bigint) AS usage_output,
  try_cast(j->'message'->'usage'->>'cacheRead'  AS bigint) AS usage_cache_read,
  try_cast(j->'message'->'usage'->>'cacheWrite' AS bigint) AS usage_cache_write,
  try_cast(j->'message'->'usage'->>'totalTokens' AS bigint) AS usage_total,
  try_cast(j->'message'->'usage'->'cost'->>'total' AS double) AS cost_total,
  j->>'customType'                                 AS custom_type,
  file_kind
FROM pi_record
WHERE record_type IN ('session','message','model_change','thinking_level_change',
                      'compaction','branch_summary','custom','custom_message','label','session_info');

-- L2: block grain (UNNEST message.content for user/assistant) ----------------------------
-- content is an ARRAY of blocks or a plain string; the string form is normalized to a
-- single text block. toolResult is a separate message (role=toolResult) handled in pi_tool.
CREATE OR REPLACE VIEW pi_block AS
WITH base AS (
  SELECT
    j->>'id'                                       AS record_id,
    session_id,
    j->'message'->>'role'                          AS role,
    try_cast(j->>'timestamp' AS timestamp)         AS ts,
    file_name, file_kind,
    CASE
      WHEN json_type(j->'message'->'content') = 'ARRAY'
        THEN cast(j->'message'->'content' AS JSON[])
      WHEN json_type(j->'message'->'content') = 'VARCHAR'
        THEN [json_object('type','text','text', j->'message'->>'content')]
      ELSE CAST(NULL AS JSON[])
    END                                            AS blocks
  FROM pi_record
  WHERE record_type = 'message' AND (j->'message'->>'role') IN ('user','assistant')
),
exploded AS (
  SELECT
    record_id, session_id, role, ts, file_name, file_kind,
    unnest(generate_series(1, len(blocks)))        AS block_index,
    unnest(blocks)                                 AS blk
  FROM base
  WHERE blocks IS NOT NULL
)
SELECT
  record_id, session_id, role, ts, file_name, file_kind, block_index,
  blk->>'type'                                     AS block_type,   -- text / thinking / toolCall / image
  blk->>'text'                                     AS text,
  blk->>'thinking'                                 AS thinking,
  blk->>'id'                                       AS tool_call_id, -- toolCall block
  blk->>'name'                                     AS tool_name,
  blk->'arguments'                                 AS tool_arguments
FROM exploded;

-- L3: tool-call grain (toolCall block <-> toolResult message, joined per session) --------
-- Pi tool-call ids are short (8 chars) and NOT globally unique, so the call<->result
-- join is on (session_id, tool_call_id), not the id alone.
CREATE OR REPLACE VIEW pi_tool AS
WITH calls AS (
  SELECT tool_call_id, session_id, tool_name, tool_arguments, ts AS ts_call, file_kind, file_name
  FROM pi_block
  WHERE block_type = 'toolCall'
),
results AS (
  SELECT
    j->'message'->>'toolCallId'                    AS tool_call_id,
    session_id,
    try_cast(j->>'timestamp' AS timestamp)         AS ts_result,
    try_cast(j->'message'->>'isError' AS boolean)  AS is_error,
    j->'message'->>'toolName'                      AS result_tool_name,
    (SELECT string_agg(c->>'text', '\n')
       FROM unnest(cast(j->'message'->'content' AS JSON[])) AS u(c)
      WHERE (c->>'type') = 'text')                 AS result_text,
    j->'message'->'details'                        AS details,
    j->'message'->'details'->>'diff'               AS diff,
    try_cast(j->'message'->'details'->>'firstChangedLine' AS bigint) AS first_changed_line,
    j->'message'->'details'->>'truncation'         AS truncation,
    j->'message'->'details'->>'fullOutputPath'     AS full_output_path
  FROM pi_record
  WHERE record_type = 'message' AND (j->'message'->>'role') = 'toolResult'
)
SELECT
  c.tool_call_id,
  c.session_id,
  c.tool_name,
  c.tool_arguments,
  -- file path lives on the input side; accept the legacy `file_path` alias too.
  coalesce(c.tool_arguments->>'path', c.tool_arguments->>'file_path') AS file_path,
  c.ts_call,
  r.ts_result,
  r.is_error,
  r.result_text,
  r.details,
  r.diff,
  r.first_changed_line,
  r.truncation,
  r.full_output_path,
  c.file_kind,
  c.file_name
FROM calls c
LEFT JOIN results r ON r.tool_call_id = c.tool_call_id AND r.session_id = c.session_id;

-- L4: turn grain (per-session user prompt cadence + latency) -----------------------------
CREATE OR REPLACE VIEW pi_turn AS
SELECT
  session_id,
  id                                               AS user_entry_id,
  ts                                               AS user_ts,
  lead(ts) OVER (PARTITION BY session_id ORDER BY ts) AS next_ts,
  date_diff('millisecond', ts,
            lead(ts) OVER (PARTITION BY session_id ORDER BY ts)) AS latency_ms
FROM pi_event
WHERE type = 'message' AND role = 'user';

-- L7: compaction boundaries (intra-session) ---------------------------------------------
CREATE OR REPLACE VIEW pi_compaction AS
SELECT
  session_id,
  try_cast(j->>'timestamp' AS timestamp)           AS ts,
  j->>'parentId'                                   AS parent_id,
  j->>'summary'                                    AS summary,
  j->>'firstKeptEntryId'                           AS first_kept_entry_id,
  try_cast(j->>'tokensBefore' AS bigint)           AS tokens_before,
  try_cast(j->>'fromHook' AS boolean)              AS from_hook
FROM pi_record
WHERE record_type = 'compaction';

-- L8: in-file branch points (Pi-specific) -----------------------------------------------
-- branch_summary entries mark a return from an explored branch; `from_id` is the entry
-- the branch diverged from (a fan-out point that has multiple children).
CREATE OR REPLACE VIEW pi_branch AS
SELECT
  session_id,
  j->>'id'                                         AS branch_entry_id,
  j->>'parentId'                                   AS parent_id,
  j->>'fromId'                                     AS from_id,
  j->>'summary'                                    AS summary,
  try_cast(j->>'timestamp' AS timestamp)           AS ts
FROM pi_record
WHERE record_type = 'branch_summary';

-- L5: session grain (one row per session_id; header-only/empty sessions allowed) --------
CREATE OR REPLACE VIEW pi_session AS
WITH hdr AS (
  SELECT
    session_id,
    any_value(j->>'cwd')                           AS cwd,
    any_value(j->>'version')                       AS format_version,
    any_value(j->>'parentSession')                 AS parent_session_path
  FROM pi_record WHERE record_type = 'session' GROUP BY session_id
),
names AS (
  SELECT session_id, max(j->>'name') AS info_name
  FROM pi_record WHERE record_type = 'session_info' GROUP BY session_id
),
first_user AS (
  SELECT session_id, arg_min(text, ts) AS first_user_text
  FROM pi_block
  WHERE role = 'user' AND block_type = 'text' AND text IS NOT NULL
  GROUP BY session_id
),
ev AS (
  SELECT
    session_id,
    any_value(file_kind)                           AS file_kind,
    min(ts)                                        AS started,
    max(ts)                                        AS ended,
    count(*) FILTER (WHERE type='message' AND role='user')       AS n_user,
    count(*) FILTER (WHERE type='message' AND role='assistant')  AS n_assistant,
    count(*) FILTER (WHERE type='message' AND role='toolResult') AS n_tool_result,
    count(*) FILTER (WHERE type='compaction')                    AS n_compaction,
    count(*) FILTER (WHERE type='message' AND role='assistant' AND stop_reason='error') AS n_error,
    sum(usage_input)                               AS tok_input,
    sum(usage_output)                              AS tok_output,
    sum(usage_cache_read)                          AS tok_cache_read,
    sum(usage_cache_write)                         AS tok_cache_write,
    sum(cost_total)                                AS cost_total
  FROM pi_event GROUP BY session_id
),
tools AS (
  SELECT session_id, count(*) AS n_tool FROM pi_tool GROUP BY session_id
)
SELECT
  ev.session_id,
  ev.file_kind,
  coalesce(nm.info_name, fu.first_user_text)       AS title,
  ev.started, ev.ended,
  h.cwd,
  h.format_version,
  ev.n_user, ev.n_assistant, ev.n_tool_result,
  coalesce(tl.n_tool, 0)                           AS n_tool,
  ev.n_error, ev.n_compaction,
  ev.tok_input, ev.tok_output, ev.tok_cache_read, ev.tok_cache_write, ev.cost_total,
  h.parent_session_path
FROM ev
LEFT JOIN hdr        h  USING (session_id)
LEFT JOIN names      nm USING (session_id)
LEFT JOIN first_user fu USING (session_id)
LEFT JOIN tools      tl USING (session_id);

-- L6: lineage edges (one row per parent->child edge, kind-resolved + bundle) -------------
-- Edge sources, in ascending kind-priority (specific overrides the header default):
--   1. header.parentSession  -> edge, default kind 'fork'
--   2. child `session-lineage` custom entry (kind='handoff', + parent_leaf_id, goal)
--   3. parent-side details.sessionLineage (kind 'subagent' | 'skill-fork')
-- Edges are matched on the session UUIDs (extracted from paths), not raw paths,
-- to avoid Windows path-spelling variance. bundle_id = the topmost root reached by
-- following non-handoff edges upward (handoff = a separate thread, excluded).
CREATE OR REPLACE VIEW pi_link AS
WITH RECURSIVE
uuid_re(p) AS (VALUES ('([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})')),
header_edges AS (
  SELECT
    r.session_id                                   AS child_session_id,
    regexp_extract(r.j->>'parentSession', (SELECT p FROM uuid_re), 1) AS parent_session_id,
    'fork'                                         AS kind,
    CAST(NULL AS VARCHAR)                          AS parent_leaf_id,
    CAST(NULL AS VARCHAR)                          AS goal,
    1                                              AS prio
  FROM pi_record r
  WHERE r.record_type = 'session' AND (r.j->>'parentSession') IS NOT NULL
),
lineage_entries AS (
  SELECT
    r.session_id                                   AS child_session_id,
    coalesce(r.j->'data'->>'parentSessionId',
             regexp_extract(r.j->'data'->>'parentSessionPath', (SELECT p FROM uuid_re), 1)) AS parent_session_id,
    r.j->'data'->>'kind'                           AS kind,
    r.j->'data'->>'parentLeafId'                   AS parent_leaf_id,
    r.j->'data'->>'goal'                           AS goal,
    3                                              AS prio
  FROM pi_record r
  WHERE r.record_type = 'custom' AND (r.j->>'customType') = 'session-lineage'
),
subagent_edges AS (
  SELECT
    res->'sessionLineage'->>'childSessionId'       AS child_session_id,
    r.session_id                                   AS parent_session_id,
    res->'sessionLineage'->>'kind'                 AS kind,
    CAST(NULL AS VARCHAR)                          AS parent_leaf_id,
    CAST(NULL AS VARCHAR)                          AS goal,
    3                                              AS prio
  FROM pi_record r,
       unnest(cast(r.j->'message'->'details'->'results' AS JSON[])) AS u(res)
  WHERE r.record_type = 'message' AND (r.j->'message'->>'role') = 'toolResult'
    AND json_type(r.j->'message'->'details'->'results') = 'ARRAY'
    AND (res->'sessionLineage') IS NOT NULL
),
skillfork_edges AS (
  SELECT
    r.session_id                                   AS parent_session_id,
    r.j->'details'->'sessionLineage'->>'childSessionId' AS child_session_id,
    r.j->'details'->'sessionLineage'->>'kind'      AS kind,
    CAST(NULL AS VARCHAR)                          AS parent_leaf_id,
    CAST(NULL AS VARCHAR)                          AS goal,
    3                                              AS prio
  FROM pi_record r
  WHERE r.record_type = 'custom_message' AND (r.j->>'customType') = 'skill-fork-result'
    AND (r.j->'details'->'sessionLineage') IS NOT NULL
),
all_edges AS (
  SELECT child_session_id, parent_session_id, kind, parent_leaf_id, goal, prio FROM header_edges
  UNION ALL
  SELECT child_session_id, parent_session_id, kind, parent_leaf_id, goal, prio FROM lineage_entries
  UNION ALL
  SELECT child_session_id, parent_session_id, kind, parent_leaf_id, goal, prio FROM subagent_edges
  UNION ALL
  SELECT child_session_id, parent_session_id, kind, parent_leaf_id, goal, prio FROM skillfork_edges
),
resolved AS (
  SELECT
    parent_session_id,
    child_session_id,
    arg_max(kind, prio)           AS kind,
    max(parent_leaf_id)           AS parent_leaf_id,
    max(goal)                     AS goal
  FROM all_edges
  WHERE child_session_id IS NOT NULL AND parent_session_id IS NOT NULL
  GROUP BY parent_session_id, child_session_id
),
chain AS (
  SELECT child_session_id AS node, parent_session_id AS root, 1 AS depth
  FROM resolved WHERE kind <> 'handoff'
  UNION ALL
  SELECT c.node, r.parent_session_id, c.depth + 1
  FROM chain c
  JOIN resolved r ON r.child_session_id = c.root AND r.kind <> 'handoff'
),
bundle AS (
  SELECT node, arg_max(root, depth) AS bundle_id FROM chain GROUP BY node
)
SELECT
  rv.kind,
  rv.parent_session_id,
  rv.child_session_id,
  rv.parent_leaf_id,
  rv.goal,
  b.bundle_id
FROM resolved rv
LEFT JOIN bundle b ON b.node = rv.child_session_id;
