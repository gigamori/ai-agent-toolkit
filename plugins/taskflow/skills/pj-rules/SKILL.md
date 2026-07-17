---
name: pj-rules
description: View or edit a taskflow project's per-project rules (`_projects/<project>/rules.md`) via natural-language commands. Invoke as `/pj-rules <intent>`. Reading needs no confirmation; every write shows a diff and requires explicit approval — there is no `-y` skip for writes (the file is injected into every future turn, so its blast radius is session-wide). Runs in the main session.
disable-model-invocation: true
allowed-tools: Bash(uv run python *) Bash(ls *) Read Write Edit AskUserQuestion
---

# /pj-rules

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

Design background: `_projects/harness-taskflow/project-notes/specs/project-rules-injection.md`
(development-repo design notes; not shipped with the plugin) §9.

## Step 1 — Parse arguments

`$ARGUMENTS` is a free-form natural-language instruction.

1. If `$ARGUMENTS` contains the literal token `-y` or `--yes`, strip it
   silently — this skill has no confirmation-skip. A `write` intent always
   asks for approval regardless (see Step 4); a `show`/`list` intent never
   needed one. Do not treat the flag as an error, just ignore it.
2. Trim whitespace. The remainder is `raw_input`.
3. If `raw_input` is empty, reply:

   ```
   Usage: /pj-rules <intent>
   examples:
     /pj-rules show
     /pj-rules このプロジェクトに「dist/ を直接編集しない」というルールを追加して
     /pj-rules always show these rules every turn
   ```

   and stop.

## Step 2 — Resolve the project

Identical to `/progress` Step 2 (same `[Progress Session]` header parsing,
same `state_file` read, same `$TASKFLOW_PROJECT_ROOTS` resolution).

1. Scan the current conversation context for the most recent line matching
   `[Progress Session] session_id=<uuid> sid8=<8chars> state_file=<path> current_project=<name>`.
2. Extract `state_file`, `session_id`, `sid8`, and `current_project`.

If no `[Progress Session]` header is found, reply `no project; set with pj:<project> first` and stop.

3. Read `state_file` and confirm its `project` field is non-empty. If empty
   or unreadable, reply `no project; set with pj:<project> first` and stop.

Locate the project root: split `$TASKFLOW_PROJECT_ROOTS` by `;`, use the
first `<root>/<project>/` that exists (fall back to `_projects/` in the
current workspace if the env var is unset). If none contains the project,
reply `project '<name>' not found` and stop.

## Step 3 — Classify intent (no router subagent)

Unlike `/progress`, this skill does not invoke a router subagent — the
action set is small and a write's body is authored by the main agent
regardless, so a read-only classifier subagent would only add latency
without changing what happens next.

Classify `raw_input` inline against this synonym table:

| action | triggers |
|---|---|
| `show` | show, list, 表示, 一覧, 見せて, 確認 |
| `write` | add / 追加, change / edit / 変更 / 直して, remove / 削除, or any request describing a new or changed rule, including requests to change `inject_every_turn` / `max_lines` |

If neither matches, reply `cannot parse: <raw_input>` with the two example
forms from Step 1's usage message, and stop.

## Step 4 — Dispatch on action

### action = `show`

```bash
uv run python ${CLAUDE_PLUGIN_ROOT}/scripts/pj_rules.py show "<project-root>"
```

- Exit 1 (`exists: false`): reply `no rules.md yet for <project>. Ask me to add a rule to create one.` and stop.
- Exit 2: report the stderr message and stop.
- Exit 0: echo a summary — heading count, `lines: N (cap: M)`, and **if
  `over_cap: true`, a soft warning** (`⚠ rules.md is N lines, over the M-line
  cap — consider trimming`; this is advisory, never blocking), plus the list
  of headings.

### action = `write`

1. **Before-count.** Run `pj_rules.py show "<project-root>"`. If exit 1
   (no file yet), `before_headings = 0` and the file will be created; note
   the project's rules default budget (`max_lines: 100`, `inject_every_turn: false`).
   If exit 0, parse `headings: N` as `before_headings`.

2. **Construct the new full body.** Read the current `rules.md` (if it
   exists). Build the complete replacement content from `raw_input`:
   - If the file doesn't exist: create frontmatter
     (`inject_every_turn: false` / `max_lines: 100`) + `# Rules` H1 + one new
     `## <short title>` entry with the rule as prose body.
   - If adding a rule: append a new `## <short title>` entry (never bury it
     under an existing heading — manifest extraction is one heading per rule).
   - If editing an existing rule: modify that entry's body in place, keeping
     its `## ` heading (rename the heading only if the user's request implies
     a new title).
   - If the request concerns `inject_every_turn` or `max_lines`: edit the
     frontmatter block. There is no separate "settings" action — this is
     still a `write`, still diffed and confirmed like any other change.
   - **Every rule entry MUST have a `## ` heading** — this is what the
     per-turn reminder in `session_init.py` extracts; a rule without one is
     invisible to that reminder.

3. **Diff and confirm.** Show the user the concrete change (unified diff, or
   for a new file, the full proposed content) via a plan-summary text block,
   then call AskUserQuestion:
   - question: `Apply this change to <project>'s rules.md?`
   - options: `Yes, apply` / `No, cancel`

   **There is no `-y` bypass for `write`** — always ask, even if the user's
   original input contained `-y` (stripped in Step 1). The file is injected
   into every future turn of this project, so the blast radius extends well
   past this turn.

4. If declined, reply `cancelled` and stop.

5. If approved, apply the change with `Write`/`Edit` to
   `<project-root>/rules.md`.

6. **After-count and deterministic verification.** Run
   `pj_rules.py show "<project-root>"` again; parse `headings: M` as
   `after_headings`.
   - If the intent was to **add** a new rule and `M` is not greater than
     `before_headings`: the edit likely failed to produce a proper `## `
     heading. Do NOT report success — tell the user the write applied but
     the new content may not be recognized as a rule (no `## ` heading found),
     and offer to fix the heading.
   - If `M` is less than `before_headings` (a heading was lost) on an
     **edit**: same failure handling — do not report success silently.
   - Otherwise, proceed to step 7.

7. **Make the change visible next turn.** Run:

   ```bash
   uv run python ${CLAUDE_PLUGIN_ROOT}/scripts/pj_rules.py reset-indexed "<state_file>"
   ```

   This resets only the `project_rules_indexed` field in the session's state
   file (merge-preserving — every other field, e.g. `progress_capture_done`,
   `exec_bind`, is left untouched by the script). Never hand-edit
   `state_file` with `Read`/`Edit` for this — always go through the script,
   which guarantees the other fields survive.

8. Report success: `updated <project>'s rules.md — full text will show again
   next turn.` If the after-show reported `over_cap: true`, append the same
   soft-warning as in `show`.

## Output rules

- Total response ≤ 20 lines (excluding the AskUserQuestion UI and any diff
  preview shown before it).
- Include the `[pj:<current_project>]` leading line per the Leading-line
  invariant above (it does not count toward the 20-line limit).

## Restrictions

- May modify **only**: `<project-root>/rules.md`, and — via
  `pj_rules.py reset-indexed` only, never by hand-editing — the current
  session's `state_file`'s `project_rules_indexed` field. Do NOT touch any
  other file.
- `write` always requires explicit `AskUserQuestion` approval; there is no
  flag or phrasing that skips it.
- Do NOT invoke `pj_rules.py reset-indexed` except immediately after a
  successful, heading-verified `write` (steps 6-7 above) — it is not a
  general "refresh" command.
- There is no model-autonomous write: every change to `rules.md` originates
  from an explicit user instruction in `raw_input`, never from the model's
  own judgment about what rules a project "should" have.
