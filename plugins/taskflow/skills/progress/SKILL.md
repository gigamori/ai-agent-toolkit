---
name: progress
description: Manage taskflow project task progress via natural-language commands. Invoke as `/progress <intent> [-y]`. The intent is routed by the progress-router subagent into (action, targets); the main agent confirms with the user (unless `-y`) and executes. Runs in the main session.
disable-model-invocation: true
allowed-tools: Bash(uv run python *) Bash(mv *) Bash(mkdir *) Bash(ls *) Bash(cat *) Bash(stat *) Read Write Edit Agent AskUserQuestion
---

# /progress

Arguments: `$ARGUMENTS`

Execute the procedure below exactly. Report each step's outcome to the user.

Leading-line invariant: every reply this command produces — including the
literal "reply ... and stop" templates below — follows the taskflow
RESPONSE LEADING LINES rule: when a project is assigned (the
`[Progress Session]` header's `current_project` / the state file's `project`
field resolved in Step 2 is non-empty), include `[pj:<current_project>]` in
the reply's leading lines (near the beginning, before the main body; it may
follow other leading lines such as `[Mode:]` — not necessarily the literal
first line). Omit it only when no project is assigned.

## Step 1 — Parse arguments

`$ARGUMENTS` is a free-form natural-language instruction with an optional
`-y` / `--yes` flag anywhere in the input.

1. Scan for the literal tokens `-y` and `--yes`. If found, set
   `skip_confirm = true` and remove them from the input.
2. Trim whitespace. The remainder is `raw_input`.
3. If `raw_input` is empty, reply:

   ```
   Usage: /progress <intent> [-y]
   examples:
     /progress check
     /progress audit
     /progress rebuild
     /progress タスクXを完了にして
     /progress Xに着手
     /progress Xを戻して
   ```

   and stop.

## Step 2 — Resolve the project

1. Scan the current conversation context for the most recent line matching the
   pattern `[Progress Session] session_id=<uuid> sid8=<8chars> state_file=<path> current_project=<name>`.
2. Extract `state_file`, `session_id`, `sid8`, and `current_project` from that
   header.

If no `[Progress Session]` header is found in context, reply:

```
no project; set with pj:<project> first
```

and stop.

3. Read the `state_file` and confirm its `project` field is non-empty.

If the `project` field is empty or the state file cannot be read, reply:

```
no project; set with pj:<project> first
```

and stop.

Use `sid8` from the header as the session identifier (for router context).

Then locate the project root. Split the environment variable
`$TASKFLOW_PROJECT_ROOTS` by `;` into a list of root directories. Check each
`<root>/<project>/` in order and use the first that exists. If
`$TASKFLOW_PROJECT_ROOTS` is unset, fall back to `_projects/` in the current
workspace.

If no root contains the project, reply `project '<name>' not found` and stop.

## Step 3 — Invoke the progress-router subagent

The router spec is built into the subagent definition body
(`${CLAUDE_PLUGIN_ROOT}/agents/progress-router.md`); the skill does NOT inline
it. Pass only the JSON context block as the prompt.

1. Construct the prompt as the JSON context block:

   ```json
   {"project_root": "<absolute project root from Step 2>", "raw_input": "<raw_input from Step 1>", "session_id": "<first 8 chars of state filename from Step 2>"}
   ```

2. Invoke the Agent tool with `subagent_type: taskflow:progress-router` and
   the prompt above. The router runs read-only.
3. Parse the returned JSON object. Fields: `action`, `targets`, `confidence`,
   `reasoning`.
4. If the router response is not valid JSON (parse error, prose, or empty):
   - Derive `action` from `raw_input` using the synonym table **and its
     matching + tie-break rules** in the router spec (Step 1 of the body of
     `${CLAUDE_PLUGIN_ROOT}/agents/progress-router.md`) — i.e. English tokens
     match on a word boundary (never as a substring), Japanese tokens match as
     a substring, and a multi-match resolves to the sentence's main verb.
     If no synonym matches, treat as `action: "unknown"`.
   - Set `targets: []`, `confidence: "high"`.
   - Proceed to Step 4 with this synthetic result.

## Step 4 — Validate the router result

| Condition | Reply and stop |
|---|---|
| `action: "unknown"` | `cannot parse: <raw_input>` + `reasoning: <reasoning>` + 1-line of valid actions/synonyms |
| `action in {approve, revert, start}` AND `len(targets) >= 2` AND `confidence: "low"` | `ambiguous: <raw_input>`. List the returned targets with `<stem> \| <H1>`. |
| `action in {approve, revert, start}` AND `len(targets) == 0` | `no match for '<raw_input>'`. List up to 10 candidates from the action's candidate folder(s) with `<stem> \| <H1>`. |

### Status mismatch warning

If any target has `status_mismatch: true`, include a warning line in the
plan summary (Step 5):

```
⚠ <stem> is in <current_status> (expected <primary_folder> for <action>)
```

The user confirms or cancels as usual.

## Step 5 — Confirm with user (unless `-y`)

If `skip_confirm` is true, skip this step and proceed to Step 6.

For `check`, `audit`, `sync`, `rebuild`: skip confirmation (these are
non-destructive read or rebuild operations). Proceed to Step 6.

For `approve` / `revert` / `start`:

1. Print a plan summary in text (before the AskUserQuestion call):

   ```
   router: <reasoning>  (confidence: <level>)
   action: <action>
   targets:
     - <stem> (<current_status> → <target_status>): <h1>
     ...
   ```

2. Call AskUserQuestion with:
   - question: `Execute <action> on <N> target(s)?`
     - If any target has `status_mismatch: true` (`<K>` = count of such
       targets), instead use: `Execute <action> on <N> target(s)? — ⚠ <K> task(s) skip 1_in_progress (0_todo → 2_done)`
   - options:
     - label: `Yes, execute`, description: `Apply the move + log update`
       - If any target has `status_mismatch: true`, instead use description:
         `Apply the move + log update (incl. <K> task(s) jumping 0_todo → 2_done)`
     - label: `No, cancel`, description: `Stop without changes`

3. If the user picks `No, cancel`, reply `cancelled` and stop. Otherwise
   proceed to Step 6.

## Step 6 — Dispatch on action

Plugin script paths:

- `${CLAUDE_PLUGIN_ROOT}/scripts/check_progress.py`
- `${CLAUDE_PLUGIN_ROOT}/scripts/audit_progress.py`
- `${CLAUDE_PLUGIN_ROOT}/scripts/rebuild_progress.py`

### action = `check`

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/check_progress.py "<project-root>"
```

Read stdout. If `OK: no drift...`, reply `OK: <project>` and stop. Otherwise
summarize findings in ≤ 20 lines using the existing format.

### action = `audit`

```bash
uv run python ${CLAUDE_PLUGIN_ROOT}/scripts/audit_progress.py "<project-root>"
```

Echo the stdout verbatim (≤ 50 lines in the common case). This action is
read-only.

### action in {`sync`, `rebuild`}

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/rebuild_progress.py "<project-root>"
```

Echo stdout.

### action = `approve`

For each target in `targets`:

1. `mkdir -p "<project-root>/tasks/2_done"` (idempotent).
2. `mv "<project-root>/<current_file>" "<project-root>/tasks/2_done/<basename of current_file>"`.
3. Edit the moved file:
   - frontmatter `updated:` → today (YYYY-MM-DD)
   - **clear the `## Next Steps` section content**: keep the `## Next Steps` header line, remove all lines between it and the next section heading (`## ...`) or the `<!-- @log:begin -->` marker — whichever comes first. Leave one blank line after the header for readability.
   - append one line to the `<!-- @log:begin --> ... <!-- @log:end -->` block: `- <today>: approved → 2_done`

The Next Steps clear is destructive of historical "what was unfinished at approve time" — that intent is preserved by the `@log` entry on approve and any prior `[s:<sid>]: completed` entries. Audit no longer flags 2_done files regardless of Next Steps content, but clearing keeps file content consistent with semantics.

After all targets, run `rebuild_progress.py` and report.

### action = `revert`

For each target in `targets`:

1. `mkdir -p "<project-root>/tasks/<target_status>"` (idempotent).
2. `mv "<project-root>/<current_file>" "<project-root>/tasks/<target_status>/<basename of current_file>"`.
3. Edit the moved file's frontmatter `updated:` to today. Append one line:
   `- <today>: reverted to <target_status>`.

After all targets, run `rebuild_progress.py` and report.

### action = `start`

For each target in `targets`:

1. `mkdir -p "<project-root>/tasks/1_in_progress"` (idempotent).
2. `mv "<project-root>/<current_file>" "<project-root>/tasks/1_in_progress/<basename of current_file>"`.
3. Edit the moved file's frontmatter `updated:` to today. Append one line:
   `- <today>: started → 1_in_progress`.

After all targets, run `rebuild_progress.py` and report.

## Output rules

- Echo each Bash command's relevant output (or a 1-line summary).
- For state-changing actions (`approve`, `revert`, `start`), list each moved file as
  `<stem>: <current_status> → <target_status>`.
- Total response ≤ 30 lines (excluding the AskUserQuestion UI).
- Include the `[pj:<current_project>]` leading line per the Leading-line
  invariant above (it does not count toward the 30-line limit).

## Restrictions

- Do NOT edit task body or H1 — only the following are permitted per action:
  - `updated:` frontmatter (always)
  - one `<!-- @log -->` line append (always)
  - clearing the `## Next Steps` section content (on `approve` only, per the dispatch step above)
- Do NOT modify `progress.md` directly; only via `rebuild_progress.py`.
- Do NOT touch files outside `<project-root>/tasks/`.
- The progress-router subagent is read-only. Do NOT pass it write authority,
  and do NOT use its output to bypass user confirmation when `-y` is absent.
