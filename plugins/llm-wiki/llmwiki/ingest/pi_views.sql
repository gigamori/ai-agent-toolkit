-- pi_views.sql: DuckDB views over a single pi session JSONL file.
--
-- The session file path is injected at runtime by pi_log_project.py
-- replacing the placeholder __PI_SESSION_FILE__ with the actual file path.
--
-- Pi session JSONL format (session-manager.ts, 2026-07-03):
--   Line 1: SessionHeader { type: "session", version, id, cwd, parentSession? }
--   Subsequent lines: SessionEntry with fields including:
--     id, parentId, type, timestamp (ISO-8601), message?, ...
--   Entry types include: "message", "model_change", "compaction",
--     "branch_summary", "custom", etc.
--   For "message" entries: message.role in ("user", "assistant", "toolResult"),
--     message.content is a string or array of content blocks.
--   Note: the top-level timestamp field is named "timestamp" (not "ts").
--   Note: nested JSON extraction via j->'message'->>'role' fails in DuckDB 1.5+
--         for chained arrow operators; use json_extract_string() instead.
--
-- This file defines one view: pi_message
--   One row per message entry with role in ("user","assistant").
--   Columns: entry_id, parent_id, role, ts, text

-- L0: raw JSONL rows (skip the header line type="session")
CREATE OR REPLACE VIEW pi_raw AS
SELECT
  json                              AS j,
  json->>'type'                     AS entry_type
FROM read_json_objects('__PI_SESSION_FILE__',
       format='newline_delimited', ignore_errors=true)
WHERE (json->>'type') != 'session';

-- L1: message entries with text content
-- Only "user" and "assistant" roles are projected; "toolResult" entries are
-- excluded because they carry tool outputs, not conversation content.
-- Active-path note (F5, pinned at P6): pi session trees are observed to be
-- linear (no branching in real data). Chronological order by "timestamp"
-- is sufficient to linearize the active path; the id/parentId DFS in
-- pi_log_project._active_path is kept for correctness but the "roots" issue
-- (toolResult entries interleave the chain) is resolved here at the SQL level:
-- the ORDER BY ts ASC, entry_id ASC on pi_message already gives the correct
-- chronological sequence for user/assistant turns.
CREATE OR REPLACE VIEW pi_message AS
SELECT
  j->>'id'                                    AS entry_id,
  j->>'parentId'                              AS parent_id,
  json_extract_string(j, '$.message.role')    AS role,
  try_cast(j->>'timestamp' AS timestamp)      AS ts,
  CASE
    WHEN json_type(json_extract(j, '$.message.content')) = 'VARCHAR'
      THEN json_extract_string(j, '$.message.content')
    WHEN json_type(json_extract(j, '$.message.content')) = 'ARRAY'
      THEN (
        SELECT string_agg(
          CASE
            WHEN blk->>'type' = 'text' THEN blk->>'text'
            ELSE ''
          END,
          E'\n'
        )
        FROM unnest(try_cast(json_extract(j, '$.message.content') AS JSON[])) AS t(blk)
        WHERE blk->>'type' = 'text'
      )
    ELSE NULL
  END                                         AS text
FROM pi_raw
WHERE entry_type = 'message'
  AND json_extract_string(j, '$.message.role') IN ('user', 'assistant');
