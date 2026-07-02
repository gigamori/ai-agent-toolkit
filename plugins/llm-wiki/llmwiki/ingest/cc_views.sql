-- inspect-cc-log: layered views over Claude Code session logs (JSONL).
-- Base reads ~/.claude/projects/**/*.jsonl as raw JSON objects (no schema inference),
-- so heterogeneous record shapes never collide. `~` is expanded by DuckDB and stored
-- literally in the catalog (no absolute path baked in).
--
-- scripts/query.py applies these definitions into an in-memory DuckDB on every run,
-- then executes the user's SQL. The views read the logs lazily, so results are always
-- fresh (each query re-reads the logs). No persistent database, no materialization.
-- Views are ordered by dependency (DuckDB binds a view at creation time).

-- L0: one row per JSONL line (raw JSON + file identity) --------------------------------
CREATE OR REPLACE VIEW cc_record AS
SELECT
  json                                            AS j,
  filename                                        AS file_path_raw,
  regexp_replace(filename, '^.*[\\/]', '')        AS file_name,
  CASE
    WHEN regexp_replace(filename, '^.*[\\/]', '') LIKE 'agent-%'  THEN 'agent'
    WHEN regexp_replace(filename, '^.*[\\/]', '') = 'journal.jsonl' THEN 'journal'
    ELSE 'session'
  END                                             AS file_kind,
  json->>'type'                                   AS record_type
FROM read_json_objects('~/.claude/projects/**/*.jsonl',
       format='newline_delimited', filename=true, ignore_errors=true);

-- L1: event grain (DAG records: assistant/user/attachment/system) -----------------------
-- bundle_id = session_id: fork-child records already carry the parent session_id
-- (verified 308/308), so filtering by a root session_id co-locates its fork children.
CREATE OR REPLACE VIEW cc_event AS
SELECT
  try_cast(j->>'timestamp' AS timestamp)          AS ts,
  j->>'sessionId'                                 AS session_id,
  j->>'sessionId'                                 AS bundle_id,
  j->>'uuid'                                       AS uuid,
  j->>'parentUuid'                                 AS parent_uuid,
  record_type                                      AS type,
  j->>'subtype'                                    AS subtype,
  j->'message'->>'role'                            AS role,
  j->'message'->>'model'                           AS model,
  j->>'version'                                    AS version,
  j->>'gitBranch'                                  AS git_branch,
  j->>'cwd'                                        AS cwd,
  j->>'entrypoint'                                 AS entrypoint,
  j->>'userType'                                   AS user_type,
  try_cast(j->>'isSidechain' AS boolean)           AS is_sidechain,
  j->>'agentId'                                    AS agent_id,
  j->>'slug'                                       AS slug,
  j->>'permissionMode'                             AS permission_mode,
  j->>'requestId'                                  AS request_id,
  try_cast(j->'message'->'usage'->>'input_tokens'  AS bigint) AS usage_input,
  try_cast(j->'message'->'usage'->>'output_tokens' AS bigint) AS usage_output,
  try_cast(j->'message'->'usage'->>'cache_read_input_tokens'     AS bigint) AS usage_cache_read,
  try_cast(j->'message'->'usage'->>'cache_creation_input_tokens' AS bigint) AS usage_cache_creation,
  j->>'attributionSkill'                           AS attribution_skill,
  j->>'attributionPlugin'                          AS attribution_plugin,
  j->>'attributionAgent'                           AS attribution_agent,
  j->>'attributionMcpServer'                       AS attribution_mcp_server,
  j->>'attributionMcpTool'                         AS attribution_mcp_tool,
  try_cast(j->>'isCompactSummary' AS boolean)      AS is_compact_summary,
  j->'message'->>'stop_reason'                     AS stop_reason,
  j->>'content'                                    AS content,
  file_name,
  file_kind
FROM cc_record
WHERE record_type IN ('assistant','user','attachment','system');

-- L2: block grain (UNNEST message.content; fixes the content[0]-only loss) --------------
-- message.content is an ARRAY of blocks or a plain string; the string form is
-- normalized to a single text block so nothing is dropped.
CREATE OR REPLACE VIEW cc_block AS
WITH base AS (
  SELECT
    j->>'uuid'                                     AS record_uuid,
    j->>'sessionId'                                AS session_id,
    j->'message'->>'role'                          AS role,
    try_cast(j->>'timestamp' AS timestamp)         AS ts,
    file_name,
    CASE
      WHEN json_type(j->'message'->'content') = 'ARRAY'
        THEN cast(j->'message'->'content' AS JSON[])
      WHEN json_type(j->'message'->'content') = 'VARCHAR'
        THEN [json_object('type','text','text', j->'message'->>'content')]
      ELSE CAST(NULL AS JSON[])
    END                                            AS blocks
  FROM cc_record
  WHERE record_type IN ('assistant','user')
),
exploded AS (
  SELECT
    record_uuid, session_id, role, ts, file_name,
    unnest(generate_series(1, len(blocks)))        AS block_index,
    unnest(blocks)                                 AS blk
  FROM base
  WHERE blocks IS NOT NULL
)
SELECT
  record_uuid, session_id, role, ts, file_name, block_index,
  blk->>'type'                                     AS block_type,
  blk->>'text'                                     AS text,
  blk->>'thinking'                                 AS thinking,
  coalesce(blk->>'id', blk->>'tool_use_id')        AS tool_use_id,
  blk->>'name'                                     AS tool_name,
  blk->'input'                                     AS tool_input,
  blk->>'content'                                  AS tool_result_content,
  try_cast(blk->>'is_error' AS boolean)            AS is_error
FROM exploded;

-- L3: tool-call grain (call <-> result joined by tool_use_id; 100% pairing) -------------
CREATE OR REPLACE VIEW cc_tool AS
WITH calls AS (
  SELECT tool_use_id, session_id, tool_name, tool_input, ts AS ts_call
  FROM cc_block
  WHERE block_type = 'tool_use'
),
recs_arr AS (
  SELECT
    j, file_kind, file_name,
    try_cast(j->>'timestamp' AS timestamp)         AS ts,
    j->>'sessionId'                                AS session_id
  FROM cc_record
  WHERE record_type = 'user'
    AND json_type(j->'message'->'content') = 'ARRAY'
),
results AS (
  SELECT
    blk->>'tool_use_id'                            AS tool_use_id,
    r.ts                                           AS ts_result,
    try_cast(blk->>'is_error' AS boolean)          AS is_error,
    blk->>'content'                                AS result_text,
    r.j->'toolUseResult'->>'filePath'              AS file_path,
    r.j->'toolUseResult'->'structuredPatch'        AS structured_patch,
    try_cast(r.j->'toolUseResult'->>'userModified' AS boolean) AS user_modified,
    r.j->'toolUseResult'->>'stdout'                AS stdout,
    r.j->'toolUseResult'->>'stderr'                AS stderr,
    try_cast(r.j->'toolUseResult'->>'interrupted' AS boolean)  AS interrupted,
    r.file_kind, r.file_name
  FROM recs_arr r,
       unnest(cast(r.j->'message'->'content' AS JSON[])) AS u(blk)
  WHERE blk->>'type' = 'tool_result'
)
SELECT
  c.tool_use_id,
  c.session_id,
  c.session_id                                     AS bundle_id,
  c.tool_name,
  c.tool_input,
  c.ts_call,
  r.ts_result,
  r.is_error,
  r.result_text,
  r.file_path,
  r.structured_patch,
  r.user_modified,
  r.stdout,
  r.stderr,
  r.interrupted,
  r.file_kind,
  r.file_name
FROM calls c
LEFT JOIN results r USING (tool_use_id);

-- L6: fork edges (cross-session; the only structural parent backlink) -------------------
CREATE OR REPLACE VIEW cc_fork AS
SELECT
  j->>'agentId'                                    AS agent_id,
  j->>'parentSessionId'                            AS parent_session_id,
  j->>'parentLastUuid'                             AS parent_last_uuid,
  try_cast(j->>'contextLength' AS bigint)          AS context_length,
  j->>'parentSessionId'                            AS bundle_id,
  'fork-context-ref'                               AS lineage_source
FROM cc_record
WHERE record_type = 'fork-context-ref';

-- L7: compaction boundaries (intra-session; kept separate from fork edges) --------------
CREATE OR REPLACE VIEW cc_compaction AS
SELECT
  j->>'sessionId'                                  AS session_id,
  try_cast(j->>'timestamp' AS timestamp)           AS ts,
  j->>'logicalParentUuid'                          AS logical_parent_uuid,
  j->'compactMetadata'->>'trigger'                 AS trigger,
  try_cast(j->'compactMetadata'->>'preTokens'  AS bigint) AS pre_tokens,
  try_cast(j->'compactMetadata'->>'postTokens' AS bigint) AS post_tokens,
  try_cast(j->'compactMetadata'->>'durationMs' AS bigint) AS duration_ms,
  j->'compactMetadata'->'preservedSegment'->>'tailUuid' AS preserved_tail_uuid
FROM cc_record
-- NOTE: parenthesize the ->> extraction. DuckDB mis-plans a chained
-- `<col>=... AND j->>'k'=...` conjunction (casts the row to numeric); (j->>'k')=... is safe.
WHERE record_type = 'system' AND (j->>'subtype') = 'compact_boundary';

-- L5: session grain (one row per session_id; fork children fold in via shared sid) ------
CREATE OR REPLACE VIEW cc_session AS
WITH titles AS (
  SELECT
    j->>'sessionId' AS sid,
    max(CASE WHEN record_type='custom-title' THEN j->>'customTitle' END) AS custom_title,
    max(CASE WHEN record_type='ai-title'     THEN j->>'aiTitle'     END) AS ai_title,
    max(CASE WHEN record_type='last-prompt'  THEN j->>'lastPrompt'  END) AS last_prompt
  FROM cc_record
  WHERE record_type IN ('custom-title','ai-title','last-prompt')
  GROUP BY 1
),
ev AS (
  SELECT
    session_id,
    min(file_kind)                                 AS file_kind,
    min(ts)                                        AS started,
    max(ts)                                        AS ended,
    any_value(cwd)                                 AS cwd,
    any_value(git_branch)                          AS git_branch,
    max(version)                                   AS version,
    any_value(entrypoint)                          AS entrypoint,
    count(*) FILTER (WHERE type='user')            AS n_user,
    count(*) FILTER (WHERE type='assistant')       AS n_assistant,
    count(*) FILTER (WHERE file_kind='agent')      AS n_agent_events,
    sum(usage_input)                               AS tok_input,
    sum(usage_output)                              AS tok_output,
    sum(usage_cache_read)                          AS tok_cache_read
  FROM cc_event
  GROUP BY session_id
),
tools AS (
  SELECT session_id, count(*) AS n_tool, count(*) FILTER (WHERE is_error) AS n_error
  FROM cc_tool GROUP BY session_id
),
comp AS (
  SELECT session_id, count(*) AS n_compaction FROM cc_compaction GROUP BY session_id
),
pr AS (
  SELECT j->>'sessionId' AS sid, max(try_cast(j->>'prNumber' AS bigint)) AS pr_number
  FROM cc_record WHERE record_type='pr-link' GROUP BY 1
),
br AS (
  SELECT j->>'sessionId' AS sid, any_value(j->>'bridgeSessionId') AS bridge_session_id
  FROM cc_record WHERE record_type='bridge-session' GROUP BY 1
)
SELECT
  ev.session_id,
  ev.file_kind,
  ev.session_id                                    AS bundle_id,
  coalesce(t.custom_title, t.ai_title, t.last_prompt) AS title,
  ev.started, ev.ended, ev.cwd, ev.git_branch, ev.version, ev.entrypoint,
  ev.n_user, ev.n_assistant, ev.n_agent_events,
  coalesce(tl.n_tool, 0)                           AS n_tool,
  ev.tok_input, ev.tok_output, ev.tok_cache_read,
  coalesce(c.n_compaction, 0)                      AS n_compaction,
  coalesce(tl.n_error, 0)                          AS n_error,
  pr.pr_number,
  br.bridge_session_id
FROM ev
LEFT JOIN titles t ON t.sid = ev.session_id
LEFT JOIN tools  tl ON tl.session_id = ev.session_id
LEFT JOIN comp   c  ON c.session_id = ev.session_id
LEFT JOIN pr        ON pr.sid = ev.session_id
LEFT JOIN br        ON br.sid = ev.session_id;

-- L4: turn grain (approximate: per-session prompt cadence + latency) --------------------
CREATE OR REPLACE VIEW cc_turn AS
SELECT
  session_id,
  session_id                                       AS bundle_id,
  uuid                                             AS user_uuid,
  ts                                               AS user_ts,
  lead(ts) OVER (PARTITION BY session_id ORDER BY ts) AS next_ts,
  date_diff('millisecond', ts,
            lead(ts) OVER (PARTITION BY session_id ORDER BY ts)) AS latency_ms
FROM cc_event
WHERE type = 'user' AND role = 'user';
