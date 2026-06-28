# inspect-cc-log — investigation queries

Ready-to-run SQL. Replace `<sid>` / `<root_sid>` with a real `session_id`.
Reminder: parenthesize any `->>` used in a `WHERE` predicate.

Run with:
```bash
uv run scripts/query.py --sql "<one query below>"
```

## Event & record extraction

```sql
-- Change history of one file (chronological, with diff hunk count)
select ts_call, tool_name, json_array_length(structured_patch) hunks, file_path
from cc_tool
where file_path ilike '%run_sql.py%' and tool_name in ('Edit','Write')
order by ts_call;

-- Messages containing specific text (user + assistant + thinking)
select ts, session_id, role, block_type, left(text, 200)
from cc_block
where text ilike '%revert%' or thinking ilike '%revert%'
order by ts;

-- Records that ran a specific shell command
select ts_call, session_id, tool_input->>'command' cmd, is_error
from cc_tool
where tool_name = 'Bash' and (tool_input->>'command') ilike '%git push%';

-- Turns the user interrupted
select ts, session_id, text
from cc_block
where text in ('[Request interrupted by user]',
               '[Request interrupted by user for tool use]');

-- Tool runs the user rejected
select ts_call, session_id, tool_name
from cc_tool
where is_error and result_text ilike '%doesn''t want to proceed%';

-- Permission-denied tools, ranked
select tool_name, count(*) n
from cc_tool
where is_error and result_text ilike 'Permission to use %has been denied%'
group by 1 order by 2 desc;

-- Edit failures by cause (no-match / unread / stale)
select ts_call, session_id, file_path,
  case when result_text ilike '%String to replace not found%' then 'no-match'
       when result_text ilike '%has not been read yet%'       then 'unread'
       when result_text ilike '%has been modified since%'     then 'stale' end fail
from cc_tool
where tool_name = 'Edit' and is_error;

-- Hook blocked the turn from ending
select ts, session_id, content
from cc_event
where subtype = 'informational' and content ilike '%hook blocked the turn%';

-- Slash-command invocations (/resume, /config, /doctor, …)
select ts, session_id, regexp_extract(content, '<command-name>([^<]+)', 1) cmd
from cc_event
where subtype = 'local_command' and content like '%<command-name>%';

-- Compaction boundaries (trigger, before/after tokens, duration)
select ts, session_id, trigger, pre_tokens, post_tokens, duration_ms
from cc_compaction
order by ts;

-- Model refusal -> fallback events (queried from raw records)
select (j->>'timestamp')::timestamp ts, j->>'sessionId' session_id,
       j->>'originalModel' from_model, j->>'fallbackModel' to_model
from cc_record
where record_type = 'system' and (j->>'subtype') = 'model_refusal_fallback';

-- API errors (e.g. overloaded) and retry attempt
select (j->>'timestamp')::timestamp ts, j->>'sessionId' session_id,
       j->'error'->>'status' status, j->>'retryAttempt' attempt
from cc_record
where record_type = 'system' and (j->>'subtype') = 'api_error';

-- Sessions that created a PR
select session_id, pr_number, title from cc_session where pr_number is not null;

-- Top token-spending sessions
select session_id, title, tok_input + tok_output tot
from cc_session order by tot desc nulls last limit 20;

-- Most-edited files
select file_path, count(*) edits, count(distinct session_id) sessions
from cc_tool
where tool_name in ('Edit','Write') and file_path is not null
group by 1 order by 2 desc limit 30;
```

## Joins

```sql
-- Tool call paired with its result (arguments + outcome)
select b.tool_name, b.tool_input, t.is_error, t.file_path
from cc_block b join cc_tool t using (tool_use_id)
where b.block_type = 'tool_use';

-- Tool calls with session metadata (which session/branch/title)
select t.ts_call, s.title, s.git_branch, t.tool_name, t.file_path
from cc_tool t join cc_session s using (session_id)
where t.file_path ilike '%SKILL.md%' order by t.ts_call;

-- Whole fork tree, one timeline (root session + every agent child)
select e.ts, e.file_kind, e.role, e.type, left(e.content, 80)
from cc_event e where e.bundle_id = '<root_sid>' order by e.ts;

-- Files touched by fork children under a root session
select t.file_kind, t.tool_name, t.file_path
from cc_tool t
where t.bundle_id = '<root_sid>' and t.file_kind = 'agent' and t.file_path is not null;

-- Action paired with the assistant text in the same record (intent next to action)
select tb.text intent, ub.tool_name, ub.tool_input
from cc_block ub
join cc_block tb on tb.record_uuid = ub.record_uuid and tb.block_type = 'text'
where ub.block_type = 'tool_use';

-- Error tool -> next action in the same session (recovery tracing)
select ts_call, tool_name, is_error,
  lead(tool_name) over (partition by session_id order by ts_call) next_tool
from cc_tool where session_id = '<sid>' order by ts_call;

-- The message a compaction cut from (pre-compaction tail)
select c.session_id, c.trigger, b.role, left(b.text, 200)
from cc_compaction c join cc_block b on b.record_uuid = c.logical_parent_uuid;

-- Session list with fork-child count and compaction count
select s.session_id, s.title, count(distinct f.agent_id) forks, s.n_compaction
from cc_session s left join cc_fork f on f.parent_session_id = s.session_id
group by 1, 2, s.n_compaction order by forks desc;

-- Which skills launched, and how many tool calls each made
select attribution_skill, count(*) calls
from cc_event where attribution_skill is not null
group by 1 order by 2 desc;

-- Resolve a past session by title (extract-cc-log replacement), then read its turns
select b.ts, b.role, left(b.text, 200)
from cc_session s join cc_block b using (session_id)
where s.title ilike '%bridge-session%' order by b.ts;
```

## Full-session timeline (messages + tools + system events merged)

```sql
select ts, kind, detail, info from (
  select ts, 'msg' kind, role detail, left(text, 120) info
    from cc_block where session_id = '<sid>'
  union all
  select ts_call, 'tool', tool_name, coalesce(file_path, tool_input->>'command')
    from cc_tool where session_id = '<sid>'
  union all
  select ts, 'sys', subtype, left(content, 120)
    from cc_event where session_id = '<sid>' and type = 'system'
) order by ts;
```
