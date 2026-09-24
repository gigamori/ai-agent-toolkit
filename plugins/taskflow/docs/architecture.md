# taskflow internal architecture (v0.5.0)

Internal design document for developers — read this when you need to understand or modify how the plugin works.

## Context-management types

### Four roles

| Type | Role | Lifetime | Audience | Context injection |
|---|---|---|---|---|
| `progress.md` | Task index: TODO / In Progress / Completed tables + free-text sections (Architecture / Key Decisions / Open Issues / Reference Materials) | Project lifetime | Human + AI | The hook injects a capped `## In Progress` digest every ordinary turn (`hooks/progress_digest.py`, `IN_PROGRESS_ROWS_MAX` rows, `in-progress: <shown> of <total> rows shown` footer). The subagent reads `view_progress.py`'s stdout to judge but returns none of it. Anything beyond the digest — the rows it cut, the TODO or Completed tables — the main agent gets by running `view_progress.py` itself |
| `tasks/` | One file per task; status by folder (`0_todo`/`1_in_progress`/`2_done`); body + append-only `<!-- @log -->` block | Task lifetime | AI + Human | Subagent lists `1_in_progress/`, may read a body to judge which tasks the turn is about, and returns their filenames only, on a `[tasks-rel: <file>.md ...]` pointer line — never a body. The main agent opens a named file only when it is going to work on it |
| `project-notes/` | Project-specific persistent knowledge, organized by category (`specs/`, `investigations/`, `checks/`, `procedures/`, `backlog/`, `_archive/`) | Project lifetime | AI | Subagent returns nothing from here and reads nothing under `project-notes/` (`agents/project-router.md` Step 5). Notes reach the main agent only through the hook-injected `project-notes (index rows):` block (`hooks/notes_push.py`), plus the `query-lsp` symbol route described under project-notes below |
| `plans/`, `memory/` | Auto-archived copies of the Claude config dir (`$CLAUDE_CONFIG_DIR`, default `~/.claude`) | Archive | Human | Never injected. Not to be referenced. |

### Role boundaries

- `progress.md` table region (between `<!-- @table:begin -->` and `<!-- @table:end -->`): auto-generated from task files. Never hand-edit. Exactly one region per file, enforced rather than assumed: `rebuild_progress.py` keeps the first and drops any others, and `check_progress.py`'s `duplicate_table_region` reports the state before a rebuild reaches it. A second region is what a writer leaves behind when it misses the markers and takes the append branch, and it is silent damage — `view_progress.py` caps the first region only, so every extra Completed table reaches an agent's context uncapped.
- Line endings on the shared files are LF, written explicitly (`newline="\n"`), not left to the platform — `rebuild_progress.py` for `progress.md`, and, for the task markdown, every writer that appends to it: `session_progress_capture.py`'s `append_auto_binding`, `note_links.py`'s `append_note_link`, and `session_init.py`'s `_projects/index.md` scaffold write. `progress.md` and the task markdown are read by the Pi taskflow extension as well, which reads them raw and matches `\n` literally; a CRLF write from this side makes its markers unmatchable, and it then appends a region instead of replacing one. Python's own readers hide the difference behind universal newlines, so this side cannot detect the damage by reading.
- The Completed section lists every file in `tasks/2_done/`. `rebuild_progress.py` never truncates it. Bounding context cost is `view_progress.py`'s job: it reads `progress.md`, drops all but the most recent `TASKFLOW_CONTEXT_DONE_ROWS_MAX` Completed rows (default 10, `0` = unlimited, CLI `--limit` / `--all` override), appends a `[context view]` footnote when it dropped any, and writes the result to stdout — it never writes to a file. Because the view is a line-level subset of the file rather than a re-render from `tasks/`, it cannot disagree with the file; the `#` column is deliberately not renumbered, so a view is visibly a tail.
- `progress.md` free-text sections: hand-edited; both LLM and human contribute.
- `tasks/<status>/<file>.md`: each file has frontmatter (priority, created, updated, optional dependencies), an H1 title (=summary shown in progress.md), a mutable body, and an append-only log block.
- `project-notes/<category>/`: reusable knowledge across tasks. Distill durable findings from `2_done/` tasks here.
- `auto-memory` (`$CLAUDE_CONFIG_DIR`, default `~/.claude`, `/projects/.../memory/`): a human-facing artifact; the LLM does not reference it directly.

## Single authority (v0.2.2 core principle)

| Field | Source of truth | How to change |
|---|---|---|
| Task status (TODO / In Progress / Completed) | Folder of the task file | Move the file via `/progress start`/`approve`/`unstart` (the table is regenerated from task files by `/progress rebuild`; `sync` is an alias of `rebuild`) |
| Task summary | Task file H1 line | Edit in the task file body |
| Task priority | `priority:` in frontmatter | Edit frontmatter |
| Notes category | Folder under `project-notes/` | Move the file |
| Architecture / Key Decisions / Open Issues / Reference Materials | progress.md section content | Hand-edit |

The table region in `progress.md` is a **rebuilt cache**, not authoritative. The notes `index.md` is a metadata index, the body of each note is authoritative.

## tasks/ 3-state lifecycle

### Folder model

```
tasks/
  0_todo/             not started
  1_in_progress/      started; work ongoing
  2_done/             complete (human-approved)
```

### Transition rules

| Transition | Trigger | Actor |
|---|---|---|
| `0_todo` → `1_in_progress` | `/progress start <id>`, or `mv` the file directly | Human or LLM |
| `1_in_progress` → `2_done` | **Explicit human approval** via `/progress approve <id>` | Human invokes |
| `1_in_progress` → `0_todo` | Send back / postpone via `/progress unstart <id>` | Human invokes |
| `2_done` → `1_in_progress` | Reopen via `/progress start <id>` | Human invokes |

The router does NOT auto-promote tasks in v0.2.2. All transitions are user-driven via `/progress` sub-actions or explicit file moves.

### Approval gate

Transitioning into `2_done/` requires explicit human approval. The subagent emits a `stale_hint` if `1_in_progress/` items have not been updated for ≥ 14 days (suggests running `/progress check`). Note: `stale_hint` is an mtime-based approximation; the authoritative staleness check is `/progress check`, which reads the `updated:` frontmatter field.

### Coordination with `progress.md`

`progress.md` table rows are auto-generated from task files via `rebuild_progress.py`. To update a task's appearance in progress.md, edit the task file (frontmatter, H1, or folder) and run `/progress rebuild`. Direct table editing inside `<!-- @table -->` is forbidden.

## project-notes (pointer surfacing)

### Index-file approach

Each project has a `project-notes/index.md` — a four-column table: `File | Description | Tags | Updated`. The `File` column includes the category prefix (e.g., `specs/api-design.md`).

The subagent surfaces no project-notes at all. Note selection left the router at `c6f9cdb`; `agents/project-router.md` Step 5 now forbids reading anything under `project-notes/` — not `index.md`, not a body file, not a directory listing — and forbids emitting a notes block. The two fields that used to carry it, `project_notes_summary` and `project_notes_relevant`, are retired and their absence from the agent definition is asserted by `tests/test_router_pointer_contract.py`. The main agent receives index rows from the deterministic per-turn push instead (`hooks/notes_push.py`, the `project-notes (index rows):` block), and reads note bodies itself.

The verbatim vs. bounded-population split still governs the subagent (`agents/project-router.md` §Output fidelity): how much of any population reaches its output is a script's decision, never the subagent's. Its one explicit exemption is now the `[tasks-rel:]` pointer — which active tasks are relevant to `prompt_summary` is a semantic judgment, so that line has no size cap. Note-row population size is no longer the subagent's concern at all; `hooks/notes_push.py` decides it (`K = 8`).

### Fixed taxonomy

| Category | Purpose |
|---|---|
| `specs/` | Designs, decisions, ADRs, proposals |
| `investigations/` | Research, analysis, post-mortems, retrospectives |
| `checks/` | Verification items, checklists (no judgment) |
| `procedures/` | Step-by-step instructions for humans |
| `backlog/` | Candidate items, ideas, issue tracker entries |
| `_archive/` | Exhausted; no longer authoritative |

Category is the **folder name**, not a frontmatter field. Move the file to change category.

### No fallback walk

The router reads nothing under `project-notes/` on this build, so the question of a fallback walk does not arise for it: no `Glob`, no `Grep`, no `ls`, no `index.md` read (`agents/project-router.md` Step 5). If a note exists on disk, it has a row in `index.md`; an unregistered note is drift, detected by `/progress check`. The note routes documented in `notes_guidelines.md` are the main agent's own, not the router's.

## `/progress` operations

`/progress` is a slash command (skill: `skills/progress/SKILL.md`). Natural-language input is parsed by the **progress-router subagent** (`agents/progress-router.md`, `model: sonnet`, read-only) into `(action, targets)`; the main agent confirms via `AskUserQuestion` (skipped with `-y`) and executes. This is a separate subagent from the per-turn project-router.

| Sub-action | Effect |
|---|---|
| `check` | Run drift / stale / approval-pending detection across 16 checks (`check_progress.py`), then the always-on footers (`notes without summary: n of N`; `near-duplicate note pairs: n`; unchecked anchor symbols when no query-lsp snapshot exists; `vector coverage: n of N docs` = `Total - Pending` / `Total` from `node <bin/qmd> status`, run in `project-notes/` with a 10 s ceiling and gated on a `project-notes/.qmd/index.yaml` or `index.yml` FILE, since a bare directory would let qmd resolve an ancestor's or the global index — every qmd failure arm prints no line and leaves the exit code alone). Read-only — deletion of anything it reports (e.g. dead lock sidecars) is `scripts/clean_locks.py`'s job. |
| `audit` | Classify each task by `## Next Steps` state: pending / completion_candidate / untracked / clean (`audit_progress.py`). Read-only. |
| `rebuild` (alias `sync`) | Regenerate the `<!-- @table -->` block from task files (`rebuild_progress.py`). |
| `start <id>` | Move a task `0_todo/ → 1_in_progress/` (also reopens `2_done/ → 1_in_progress/`). |
| `approve <id>...` | Move tasks `1_in_progress/ → 2_done/` (human-approved); clears the `## Next Steps` content on move. |
| `unstart <id>` | Move a task back to TODO (`1_in_progress → 0_todo`; from `2_done` it is a non-adjacent jump confirmed with a ⚠). |

The per-turn project-router stays lightweight. Heavy checks and state transitions are explicitly invoked on demand via `/progress`.

## Session lifecycle

### Per-turn flow

```
user prompt
  │
  ▼ [UserPromptSubmit hook] session_init.py
  │  ├─ first turn: create state_file
  │  │   1. parse the first `pj:xx` (at start or after any whitespace; not necessarily line 1)
  │  │   2. if absent: keep empty — NO path-based inference
  │  │   3. fork detection: if forked (a parent `[Progress Session]` marker — or
  │  │      a matching first user-entry — survives in a sibling JSONL; message
  │  │      uuids are rewritten on fork, so they do NOT identify the parent),
  │  │      inherit `project` + `inherited_tasks` from the parent state
  │  │   4. write the state_file (see "state_file" schema below)
  │  │
  │  ├─ subsequent turns:
  │  │   1. if `pj:xx` is present, update the project
  │  │   2. otherwise keep the current value from state_file (no inference)
  │  │
  │  └─ injection (only when current_project is non-empty, or `pj:?` discovery):
  │     "[Progress Session] session_id=... state_file=... current_project=... [discovery=1]"
  │     (discovery=1 is emitted on `pj:?` turns only; the field is absent otherwise)
  │     + bootstrap: BOOTSTRAP_BLOCK (ROUTER / RESPONSE-LEADING-LINES / TRUNCATION,
  │                  ASCII-only, ≤1200 chars) — placed first, immediately after the
  │                  header and ahead of fork_context/ACTION_REQUIRED/everything else,
  │                  so it survives preview truncation. Gated on inject_rules,
  │                  inject_guidelines_full, inject_index or inject_rules_full — the
  │                  turns whose payload is large enough to be persisted rather than
  │                  delivered inline. Absent on an ordinary turn, which already
  │                  carries guidelines_reminder.md's own ROUTER line.
  │     + static_rules: body of project_routing.md (once per session)
  │     + guidelines:  full 3 files on first turn / after compact; keyword reminder otherwise
  │                    (reminder variant via env TASKFLOW_GUIDELINES_REMINDER: `full` default =
  │                    guidelines_reminder.md ~750 tok / `manifest` = guidelines_reminder_manifest.md
  │                    ~460 tok — guideline rules compressed to recall labels; the ROUTER /
  │                    RESPONSE-LEADING-LINES lines stay byte-identical full text in both;
  │                    falls back to full if the manifest file is missing)
  │     + project_index: index.md on project switch
  │     + project_rules: rules.md full body on switch / `##` heading manifest otherwise
  │                      (full every turn if frontmatter inject_every_turn: true; absent if no rules.md)
  │     + ACTION_REQUIRED banner: every turn while progress.md is missing — the full
  │                      scaffold procedure on a heavy turn (same gate as the bootstrap
  │                      branch), a one-line standing notice on every other turn
  │     + fork_context: first turn of a forked session
  │     + progress_digest (hooks/progress_digest.py): `[Project Progress: <project>]` +
  │                      the `## In Progress` header, separator and up to
  │                      IN_PROGRESS_ROWS_MAX (=5) data rows from progress.md, +
  │                      `in-progress: <shown> of <total> rows shown`. Ordinary turns
  │                      only — never on a heavy turn (the bootstrap branch: inject_rules
  │                      / inject_guidelines_full / inject_index / inject_rules_full),
  │                      because that turn's payload already exceeds the inline budget
  │                      and a digest would arrive with its footer cut off; never on a
  │                      discovery turn. Replaced by the one-line
  │                      `[Project Progress] In Progress digest omitted (context budget)`
  │                      when the whole assembled additionalContext would exceed
  │                      INLINE_BUDGET_U16 (10,000 UTF-16 code units) — the guard's only
  │                      contract is that the digest never causes the crossing.
  │     + project_notes push (hooks/notes_push.py): `project-notes (index rows):` + up to K=8
  │                      verbatim index.md rows +
  │                      `project-notes matched: M of N rows (K shown, S strong)`.
  │                      Order: L1 (a note whose anchors: path == a path this turn named /
  │                      read (`.reads` tail) / wrote (`.touched` tail)) → L2 (same leaf
  │                      directory, ≤ ⌈K/2⌉) → topic = the union of the qmd structured query
  │                      rank (`node <bin/qmd> query 'vec: …\nlex: …' --no-rerank --json`,
  │                      K + QMD_MARGIN rows requested, timeout TASKFLOW_QMD_TIMEOUT s,
  │                      default 6 — the synchronous 2.7-2.9 s per-turn cost is accepted
  │                      explicitly — only when project-notes/.qmd/ exists) and the
  │                      in-process lexical rank over Description+Tags, taken alternately so
  │                      neither list is scored against the other. `_archive/` excluded,
  │                      note bodies never read for output. qmd expected but failing →
  │                      lexical only + `[project-notes] vector layer unavailable: <reason>`
  │                      on every turn the degrade persists. Env: TASKFLOW_NOTES_PUSH
  │                      off|lexical|auto, TASKFLOW_QMD_BIN.
  │     (when current_project is empty and not discovery → NOTHING is injected; router not invoked)
  │
  ▼ [LLM] detects [Progress Session] carrying discovery=1, or a non-empty current_project
  │  1. build a JSON context block — session_id, state_file, current_project, discovery,
  │     leading_lines, prompt_summary (router spec lives in agents/project-router.md; do NOT inline it)
  │  2. invoke the project-router subagent via the Agent tool (subagent_type: taskflow:project-router).
  │     discovery=1 wins whether or not current_project is set; neither condition → do not invoke
  │
  ▼ [project-router subagent] runs on an isolated generation path (model: sonnet)
  │  1. determine the turn type: discovery / routing / nothing to route
  │  D. discovery turns only: read _projects/index.md and rank every row by MEANING
  │     (never keyword or slug overlap) as strong / related / weak / far, one line each;
  │     then stop — a discovery turn runs no other step
  │  2. applicability decision (skip / apply)
  │  3. on apply: read index.md only if the project's scope must be settled; run
  │     view_progress.py for its own judgment. Nothing this step reads is emitted —
  │     the digest and the index reach the main agent from hooks
  │  4. tasks: choose by meaning which In Progress tasks the turn is about; may read a
  │     body to decide, never emits one; emit stale_hint if any are >14 days old
  │  5. project-notes: nothing. Reads nothing under project-notes/, emits no notes block
  │  6. return judgments and pointers only — action, project, progress_exists,
  │     an optional [tasks-rel:] line, stale_hint (no auto-promotion in v0.2.2)
  │
  ▼ [LLM] receives the subagent result
  │  - apply → use as context for executing the task
  │  - skip  → skip project management
  │
  ▼ task execution
  │
  ▼ [PostToolUse hooks] on each Write / Edit / NotebookEdit / Bash (and Read):
     • task_rebuild_progress.py — regenerate the progress.md table when a tasks/ file changed
     • touched_capture.py       — append the written path(s) to `{session_id}.touched`
     • read_capture.py          — (Read only) append the read path to `{session_id}.reads`,
                                  tail-truncated to 200 lines; never `.touched`, whose raw
                                  line count is the round cursor
  │
  ▼ [PreCompact hook] precompact_flush.py — only when the conversation is compacted
     (manual /compact or auto; no matcher). NOT a compaction-specific mechanism: it is
     the SECOND CALL SITE of the same round computation the Stop hook runs
     (project-notes/specs/capture-detection-gaps.md §2).
     1. compute the pending set = A_r over the in-flight ledger slice, via the shared
        `resolve_touch_cursor` / `compute_round_active` in session_progress_capture.py
        — imported, never re-implemented. Detection limit (F-5): the PreCompact payload
        carries no `last_assistant_message`, so A_r's `[tasks:]` exec-carry component is
        not computable here; flushing that class stays Stop-only
     2. pending non-empty → append `(auto) unflushed at compaction; summary pending (r{N})`
        to each pending task (through `append_auto_binding`, so `log_lock` + text-key
        idempotency apply). N = `capture.round` + 1 = the round the NEXT Stop commits, so
        two compactions inside one round produce one line
     3. print ONE plain-text line: `Preserve verbatim in the summary: unwritten per-task
        progress (results, decisions, remaining steps) for: <tasks>`. stdout is joined into
        the summarizer's instructions AND survives as a `PreCompact [<cmd>] completed
        successfully: <text>` message in the post-compaction conversation (both channels
        measured). JSON output is NOT parsed by Claude Code — it would be pasted verbatim —
        so the channel is deliberately plain text
     4. pending empty → print NOTHING and exit 0. Any stdout invalidates the precomputed-
        compaction reuse, so silence on the common path is a requirement, not a nicety
     5. `{session_id}.bind` is READ-ONLY here — the Stop hook is its sole writer (§10.1),
        which removes the PreCompact↔Stop write race structurally and leaves the cursor
        unmoved, so the Stop after a compaction still forms its round over the same slice.
        Because this hook therefore cannot resync `log_seen`, `count_sid_lines` permanently
        EXCLUDES its placeholder lines (F-1 (b)) — counting them would make the next Stop
        read "the agent self-logged" and drop the round in silence
```

### Session end

A round is normally closed by the Stop hook below. When a compaction lands mid-round the
`PreCompact` hook (`precompact_flush.py`, see "Per-turn flow" above) closes the gap first:
it flushes a placeholder for whatever the round has not written yet and asks the summarizer
to preserve that progress verbatim, WITHOUT touching `{session_id}.bind` — so everything
below still runs exactly as it would have.

```
session end
  │
  ▼ [Stop hook #1] session_sync.py
  │  1. read `project` from state_file
  │  2. empty project or directory missing → skip
  │  3. copy files modified in the last 10 minutes:
  │     <config dir>/plans/*.md                          → _projects/<project>/plans/
  │     <config dir>/projects/{encoded_cwd}/memory/*.md  → _projects/<project>/memory/
  │     (<config dir> = $CLAUDE_CONFIG_DIR if set, else ~/.claude)
  │     Note: the 10-minute window selects ALL recently-modified plans regardless
  │     of which project they belong to. The plans/ directory is an archive that
  │     may contain cross-project plans copied in the same window. This is
  │     intentional archive behavior, not a bug.
  │
  ▼ [Stop hook #2] session_progress_capture.py  (design: project-notes/specs/exec-binding.md
     and note-task-link.md §10 for the async capture apply-path)
     1. read `project` from state_file
        (self-heal: if empty, recover from a `[pj:...]` line in the assistant's last message)
     2. read the per-session `{session_id}.touched` ledger written by the PostToolUse hook
        `touched_capture.py` (NOT a jsonl scan / git diff); resolve touched task md by basename.
        The ledger is append-only with one line per write event, so its RAW line count doubles
        as a ROUND cursor: `raw[touch_cursor:]` is exactly the activity since the last committed
        round, and only that slice defines this round's work
        (project-notes/specs/capture-detection-gaps.md §1.2, D1). `touch_cursor` / `round` /
        `log_seen` / `round_base` live inside the `{session_id}.bind` `capture` dict. A `.bind`
        written before those keys existed bootstraps to the END of the ledger (§1.8 M-1), so
        upgrading never replays history
     3. exec-binding: union-merge any `[tasks: a.md b.md]` carry from the assistant's last
        message into state `exec_bind`, then code-bind those owning tasks' `@log`. Resolution
        is PRIMARY-project-only by design (`capture-detection-gaps.md` §3.6): a `.touched` line
        derives its project from the path it carries, but a `[tasks:]` carry is a bare NAME
        with no path evidence. A carry naming no task md in the primary project is therefore
        reported ONCE as `exec-skip(unresolved)` (stderr + block reason, with a best-effort
        "exists in: <project>" hint when the basename is found in another already-resolved
        project) instead of vanishing without a trace. Only the REPORT is bounded — by a bare
        basename in `exec_tried` — while resolution keeps retrying every Stop, which is what
        lets a task claimed before it is created still bind later
     4. async capture apply-path (§10): scan for `{session_id}.r{N}.capture` sidecars and APPLY
        them deterministically first, oldest round first (`confirmed` → `@log` summaries,
        `note_links` → task `@notes`), then consume each one. Applying before the placeholder
        backstop is what lets a real summary win over a placeholder (`@log` is append-only, so
        a placeholder cannot be overwritten). The round `N` in the NAME is the sidecar's
        identity (`capture-detection-gaps.md` §4.4, R-1): a sidecar is membership-checked
        against the closed set of ITS OWN round, read from `capture.history` (the last
        `_ROUND_HISTORY_K` = 3 rounds' frozen `items`, each entry carrying that round's
        `tasks` / `notes` / `allow_tasks`), not against whatever `capture.items`
        holds at apply time. Only the CURRENT round's sidecar moves the lifecycle to `done`
        and only it suppresses that Stop's expiry check — an earlier round's late arrival
        applies silently beside the open round instead of deferring it. The current-round row
        additionally requires `round > 0`: an `r0` name collides with the default a `.bind`
        without capture state reports, and used to fall through to a fail-open apply with no
        membership gate at all (F-5) — rounds start at 1, so `r0` is always a stray write and
        is discarded. A sidecar naming a round outside the retained window is consumed
        unapplied and reported once as `round-mismatch`. A TORN (unreadable / non-JSON)
        sidecar is silently retried while its round is inside the window — it may still be
        mid-write — and gets the same consume-then-report disposal (`... unreadable and
        outside history`) once its round ages out, instead of lingering until the 7-day sweep
        with no terminal state (F-4). The `applied summary:` report line carries the round
        the summary was gated on (`{key} (r{N})`), so a late apply is attributable from the
        stderr/block report; the `@log` body stays round-free — its text is the idempotency
        key and part of the agent contract (F-9). Whenever a real summary applies for a key
        (capture-detection-gaps.md §1.12, W8), this step also re-hashes that task's
        `## Next Steps` section (LF-normalized, trailing whitespace and surrounding blank
        lines stripped — a raw-byte hash would falsely clear the debt on this host, whose
        own `@log` append re-encodes the whole file to CRLF) and compares it against
        `ns_hash`: unchanged → `ns_debt` for that key increments; changed → `ns_debt` resets
        to 0 and `ns_hash` is re-frozen on the new text. The pre-R-1 un-suffixed
        `{session_id}.capture` name is **no longer read at all** — its compatibility branch was
        retired once nothing could produce it (the hook only ever hands out the per-round name
        and the agent contract forbids constructing another), so the apply path scans
        `{session_id}.r{N}.capture` and nothing else. A stray file under the old name is
        therefore inert; it is not applied, not reported, and ages out through the same 7-day
        `_CLEANUP_SUFFIXES` sweep that collects every other marker. Retirement by plain removal
        rather than by a "discard and report" branch was deliberate: reporting would mean
        carrying a scan and a report-wording branch permanently for an event no code path can
        cause.
        Entries outside that closed set are skipped (F7a), and a
        `note_links[].note` is rejected outright — independent of that membership set — unless
        it is project-relative under `project-notes/` AND resolves inside the project root (a
        `..` segment satisfies the prefix and still escapes, so both are checked, and each
        reject names its reason on stderr). A sidecar that arrives AFTER its round
        expired still applies: the resolved round's `items` / `round_base` are retained (they
        are replaced only when the next round is requested), so a subagent slower than the
        expiry keeps its summary and its note links instead of having every entry
        membership-skipped (§1.9, W5).
     5. qmd index follow-up (`hooks/qmd_index.py`): every project in `project_roots` is
        offered — the session's own first, then the rest by name. Two gates, both required:
        `TASKFLOW_NOTES_PUSH` == `auto` (the retrieval layer's own axis, normalized the same
        way `notes_push.build_notes_push` normalizes it — transcribed from that call site, not
        shared, since the rule is not a function there; no axis of its own is added) and a
        `project-notes/.qmd/` DIRECTORY.
        The two halves are driven separately.
        LEXICAL: `node <bin/qmd> update` SYNCHRONOUSLY, ceiling `UPDATE_TIMEOUT_S` = 5 s (NOT
        `TASKFLOW_QMD_TIMEOUT`, which is the query path's Pi-shared contract constant), run
        only for a project this ROUND's ledger slice wrote a `project-notes/` deliverable into
        (`NOTES_WRITTEN`) — round-scoped on purpose, since the whole-session `touched` scan
        would re-run `update` on every later Stop for notes indexed rounds ago.
        VECTOR: `node <bin/qmd> embed` DETACHED (`EMBED_ARGS`, qmd's own GPU default;
        `NO_GPU_FLAG` is appended only by the CPU escalation below, and `-f` is absent and must
        stay absent — it rebuilds every embedding in the collection), fired on the STATE the
        index is in rather than on this round's writes. `node <bin/qmd> status` is probed
        (`STATUS_ARGS`) and its `Pending:` count — documents whose vectors are missing or
        incomplete; `Vectors:` counts chunks and is deliberately unused — decides: `pending
        <= 0` → `current`, no spawn. The probe is gated on a `project-notes/.qmd/index.yaml`
        or `index.yml` FILE (`QMD_LOCAL_CONFIGS`), since a bare directory lets qmd walk up and
        answer for an ancestor's or the global index; without one the project is not probed
        (`no-local-index`). Per-probe ceiling `STATUS_TIMEOUT_S` = 3 s, clamped further by ONE
        `FOLLOW_BUDGET_S` = 4 s wall-clock deadline (`new_budget()`) shared by every probe in
        the Stop, so the project count cannot multiply the Stop's latency (`no-budget` past
        it); a probe that fails to spawn, exits non-zero, times out, or prints unrecognised
        output yields no number either (`status-failed`). Measured 0.26 s against an
        80-doc / 8.4 MB index.
        EVERY arm leaving the lag unknown — `no-local-index`, `no-budget`, `status-failed` —
        falls back to the round's note write: `pending_docs` returns `(None, status)` and
        `follow_index` returns that status ONLY when `notes != NOTES_WRITTEN`, so a
        `NOTES_WRITTEN` round reaches the spawn with `pending = None` (hence
        `retry_is_futile` False and `pending: None` in the lock). Corollary: `status-failed`
        and `no-budget` can only ever be REPORTED for a project this round wrote no notes into;
        a second corollary is that a machine whose probe fails on EVERY Stop keeps the GPU→CPU
        escalation below from ever firing, since both read the same nothing-achieved signal.
        A third follows from the same signal without needing a broken probe: the escalation
        wants a Stop on which the count did NOT move, so on a machine whose GPU backend aborts
        the embed (`GGML_VK_DISABLE_COOPMAT=1` is the known fix — see the setup step, which is
        load-bearing for THIS path now that the spawn is GPU-first, not only for the manual
        first build) a project being actively written to pays one aborted GPU spawn per
        note-writing Stop until a quieter one lets the CPU run start.
        Every failure arm is a skip: the Stop path never fails on index maintenance.
        Two caps: ONE embed per STOP — the loop breaks on `HALT_STATUSES` (`not-auto`,
        `no-bin`, `locked`, `embedding`, `embedding-cpu`, `embed-failed`); `futile-retry` is
        deliberately NOT among them, since nothing is running and the Stop's spawn is unspent —
        and ONE embed at a time per project, on `project-notes/.qmd/embed.lock` — JSON
        `{pid, sid, round, pending, mode}`, read by two gates: `lock_blocks` on PID LIVENESS
        ALONE (`sid` / `round` are diagnostics and gate nothing), and `next_embed_mode` when the
        pid is dead AND the recorded `pending` equals the count read now (`retry_is_futile`):
        that run achieved nothing, so ONE escalation follows — `mode: gpu` retries on the CPU
        (`embedding-cpu`, `NO_GPU_FLAG`), `mode: cpu` is withheld (`futile-retry`), and a lock
        with no `mode` key, or an unrecognised one, retries on the GPU. A pending count that
        fell, rose, or is unreadable (`None`) retries on the GPU whatever mode the lock names —
        `mode` records which device ONE run was ASKED for, never what the machine CAN do (nor
        necessarily what qmd ran on: an inherited `QMD_FORCE_CPU` / `QMD_LLAMA_GPU=false`
        overrides the flag, costing one extra run before the escalation settles), and nothing
        signals a repaired GPU, so a sticky CPU would hold such a machine at the fallback rate
        forever. The lock is never deleted
        (liveness is decided from the pid, so a leftover file is self-clearing and no cleanup
        path can race the spawn). `update` runs even when the lock blocks: the cap is on the
        embed half, and holding the cheap lexical refresh behind an embed that is minutes long
        on the CPU fallback (789 chunks / 82 docs measured: 93.9 s on GPU, ~155 min on CPU)
        would leave the freshest notes out of the `lex:` layer for that whole window.
        Reporting: `LOUD_STATUSES` (`no-bin`, `status-failed`, `no-budget`, `locked`,
        `futile-retry`, `embedding`, `embedding-cpu`, `embed-failed`) print
        `[progress capture] qmd-follow(<project>): <status> [s:<sid>]` on stderr; `not-auto`,
        `no-qmd`, `no-local-index` and `current` are silent.
     6. round-active set → request capture: A_r = task md written in this round's ledger slice
        ∪ owners of the notes written in it (resolved through the reverse index — work that
        reaches a task only via a project-note) ∪ this Stop's `[tasks:]` exec carry, MINUS tasks
        the agent already logged itself this round (`count_sid_lines` > `log_seen` — the reason
        a guidelines-following turn spawns nothing) and MINUS the `tried_tasks` 打止め set.
        When A_r is non-empty, or a freshly written `project-notes/` deliverable has no owning
        task, commit `capture.status=requested` (freezing A_r as the round's closed
        `items.tasks` set, advancing `touch_cursor` and `round`) and block once with an
        instruction to spawn the `taskflow:progress-capture` subagent.
        The same commit also freezes `items.allow_tasks`: A_r as it stands BEFORE the self-log
        subtraction (exec carry included), hence a superset of `items.tasks`. It is a membership
        allow-set ONLY — `_apply_capture` gates `confirmed` on the UNION of `items.tasks` and
        `items.allow_tasks`, so a summary naming a task the agent had already logged itself this
        round is applied instead of being skipped as out-of-membership, while `items.tasks` stays
        the sole driver of the expiry backstop and of the `log_seen` / `round_base` baselines —
        which is what keeps a self-logged task free of placeholders. The `referenced` over-bind
        boundary (`round_task_set`) is driven by `items.note_owners` instead (capture-detection-
        gaps.md §1.11, W7) — the subset of `items.tasks` that came from resolving a note owner
        out of THIS round's slice, so a task that entered the round only through a `[tasks:]` exec
        carry (or, as `items.tasks` itself does, a direct task-md write) does not also pick up a
        stray `(referenced)` line for some unrelated note it happens to own from an earlier round.
        The round additionally freezes `items.carry_only`: true when the round's slice contributed
        no task write and no note owner of its own, i.e. it was opened SOLELY by exec carry — the
        expiry backstop skips such a round wholesale rather than placeholder-binding a round that
        did no real work (§1.11 residual: a round mixing carry with real slice writes is NOT
        carry-only, and the carry-derived task still gets a placeholder in that case). The
        `note_links` membership set (`items.notes`) is unaffected. A `.bind` or `history` entry
        written before `allow_tasks` existed carries no `allow_tasks`, so the union degenerates to
        `items.tasks` and the pre-existing behaviour
        holds (fail-open).
        The commit also freezes `ns_hash` for any key in this round's `active` set that does
        not already carry one — the FIRST time a task enters an open round, before any of
        its `## Next Steps` accounting can happen (capture-detection-gaps.md §1.12, W8); a
        task with no `## Next Steps` section is never frozen and stays outside ns tracking
        entirely. The context block handed to the subagent carries `next_steps_stale` — every
        qualified key whose `ns_debt` has reached the threshold (1) and that has not already
        had its rewrite proposal presented (`tried_ns`) — but ONLY when that set is non-empty;
        the key is OMITTED, not emitted as `[]`, when nothing is stale, so a round with no
        staleness produces byte-identical context JSON to before W8. The subagent's own `next_steps`
        response is never written back to the task file — it is folded into the SAME Stop
        block the hook already uses for genesis-task suggestions (no new notification
        channel, §1.12 user ruling), rendered under its own "Proposed Next Steps rewrites"
        heading with a `(next-steps)` prefix so it reads distinctly from a genesis
        `(proposed)` task suggestion in the same block, and the key is added to `tried_ns`
        the instant its proposal is folded in, capping it at one presentation per task per
        session.
        The context block handed to it carries ABSOLUTE, forward-slashed `sidecar_path` /
        `project_root` (the same values this hook reads), so the subagent's write/read basis
        cannot drift from the hook's regardless of its cwd
        (project-notes/specs/capture-context-abs-path.md). `sidecar_path` is the PER-ROUND
        `{session_id}.r{N}.capture` and the block also carries `round` as an echo; the round
        identity is decided by the hook's file name, so the subagent's contract gains no new
        output field (§4.4.1 D1/D6). The preceding Stop-block line
        `round ledger entries (unclassified; diagnostic only): ...` is a separate,
        non-authoritative diagnostic: it is the current round's normalized, first-occurrence-
        deduped ledger slice, capped at `MAX_TOUCHED_IN_INJECTION` (30). It can contain entries
        that classify to neither task nor note, and it can read `(none)` when an exec carry opens
        a capture request without a ledger entry. `touched_tasks` and `note_writes` in the JSON
        context are the classified authority; the capture subagent receives that context, not a
        separate contract based on the diagnostic line.
     7. expiry (30 s, `TASKFLOW_CAPTURE_EXPIRY_S`): if no sidecar appears, the deterministic
        backstop takes over for THAT ROUND's closed `items` set — `referenced` over-bind of the
        note-write owners resolvable via the reverse index first (so an owner keeps the more
        specific provenance), then a placeholder for every item the round has not produced a
        line for yet. **Exception (capture-detection-gaps.md §1.11, W7)**: a round whose
        `items.carry_only` is true skips the placeholder step wholesale — such a round was opened
        SOLELY by a `[tasks:]` exec carry, its slice is empty, and "touched; summary pending"
        would be false for it. A real summary still applies normally if a sidecar lands for it.
        **Exception (a task currently filed under `tasks/2_done/`)**: the placeholder step also
        skips a task whose current path's parent directory is `2_done`, read live off
        `current_index` at backstop time — a task moved into `2_done` before the Stop is skipped,
        one moved back out picks its placeholder up again on the next Stop. The `referenced`
        over-bind above and any real summary are unaffected by a task's status folder.
        The note scan behind the `referenced` over-bind is whole-session by necessity — the
        requesting round consumed its own ledger slice when it committed — so the owner set,
        not the scan, is what bounds it: an owner the scan reaches for a note this round never
        touched gets no line. That owner set is `items.note_owners`, not `items.tasks` — a
        basename that entered `items.tasks` only via exec carry (or a direct task-md write) is not
        thereby entitled to a `(referenced)` line for some other note it happens to own.
        Placeholder / `referenced` notes carry an `(r{N})` round tag, which is
        also their idempotency key: binding is now keyed on the text `[s:<sid>]: <note>`
        rather than on the bare presence of a `[s:<sid>]` line, so one session binds one task
        once per ROUND instead of once per session (§1.5). A round already satisfied by a real
        summary, a `referenced` over-bind, or the agent's own `@log` line gets no placeholder —
        judged against `round_base`, the count FROZEN when the round was requested, falling
        back for an unfrozen key to the `log_seen` snapshot taken at the START of this Stop
        (the live dict is useless there: the self-log pass has already raised it to the current
        count). "Satisfied" means satisfied BY THIS ROUND: applying an earlier round's late
        sidecar advances `round_base` for the keys it wrote to, by exactly the number of lines
        THAT apply appended, so a task active in round `N` and in round `N+1` still gets its
        `(r{N+1})` placeholder when round `N`'s judgement finally lands. The delta and not the
        absolute count is what moves, so a line the open round did produce — an agent self-log
        written before the late sidecar arrived — still counts as satisfying it, and an
        idempotent re-apply (which appends nothing) moves nothing.
        Both backstops are re-entered on every later Stop while the round stays
        resolved, so each also refuses to re-append its own text key — that presence, being
        monotone, is what keeps the gate silent instead of re-reporting a no-op write (§1.9,
        W5).
        A task md carrying NEITHER `<!-- @log:begin -->` NOR `<!-- @log:end -->` no longer
        blocks the bind: the block is GENERATED (before the `@notes` block when one exists,
        otherwise at EOF) and the line lands inside it
        (project-notes/specs/capture-detection-gaps.md §4.2). Only ambiguous damage — e.g.
        two `@log:begin` — is still unbindable, and it is now REPORTED once on stderr and in
        the block reason as `bind-skip(no-anchor)` rather than dropped silently (§4.3).
     8. gate (INV-1, no-loop): return {"decision":"block", ...} ONLY to (b) report a code
        auto-bind / applied capture entry, (c) report a NEW exec-bind skip — either
        `auto-skip(ambiguous)` (resolved, but no writable `@log` block) or
        `exec-skip(unresolved)` (no task md of that name in the primary project) — (d) spawn
        capture, or to surface `proposals` — each bounded by the `{session_id}.bind` sidecar
        (`exec_tried` / `tried_notes` / `tried_tasks` 打止め sets; `exec_tried` holds a
        repo-relative path for the former and a bare basename for the latter). `requested` is committed
        BEFORE the block, so the next Stop re-enters via the requested/pending branch and never
        re-blocks. A task that can never be bound (ambiguous `@log` damage) is surfaced
        ONCE as `bind-skip(no-anchor)` and then suppressed by `tried_tasks` — it does NOT
        loop the gate.
        (The former "(a) Round1-remind a missing touched task" condition was REMOVED when the
        inline Round1 reminder was replaced by the async capture path — option-a, §10.2.)
     bind writes take the advisory write lock `log_lock.py`. Its acquire is bounded
     (INV-2, no-deadlock); the serialization is best-effort and degrades unlocked on
     timeout. The sidecar lives at `<project_root>/.locks/<task-basename>.lock` — keyed
     on the task BASENAME, matching how `_task_basename_index` resolves tasks, so a
     status-folder move cannot split one task across two lock files. See
     "Write serialization" below for what that lock does and does not cover.
```

### Write serialization (`log_lock.py`, protocol v2)

`hooks/log_lock.py` implements one half of a lock protocol **shared with the Pi taskflow
extension** (`packages/taskflow/src/write-lock.ts`). Both halves must derive the same lock
path and follow the same acquire/release discipline; a one-sided implementation protects
nothing, because the other harness walks straight past it. Neither side may be changed
unilaterally.

Callers: `hooks/note_links.py` (`@notes`), `hooks/session_progress_capture.py` (`@log`),
and `scripts/rebuild_progress.py` (`progress.md`). `write_lock` is an alias of `log_lock`,
preferred by callers that are not writing an `@log` block.

| | |
|---|---|
| Mechanism | `O_CREAT\|O_EXCL` sidecar — "existence == lock". The fd is held open for the locked region; release is close → unlink on **both** platforms. |
| Key (task md) | `<project_root>/.locks/<task-basename>.lock`, `<project_root>` = parent of the nearest `tasks/` ancestor. |
| Key (`progress.md`) | `<project_root>/.locks/progress.md.lock`, where `<project_root>` is `progress.md`'s **own** parent — it is a sibling of `tasks/`, not a child. The two rules define `<project_root>` differently on purpose. |
| Acquire | Bounded by `TASKFLOW_LOCK_TIMEOUT` (default 3.0 s). On expiry: **degrade unlocked with one warning line** — never throw, never block unbounded (INV-2). A degraded call never unlinks the sidecar; it does not own it. |
| Stale break | A sidecar older than `TASKFLOW_LOCK_STALE` (default 10 s) may be unlinked and re-created by a waiter. Only one racer wins the subsequent exclusive create. |

**Scope of protection — and four things it does NOT cover.** Each is a known, accepted
property of the protocol, not a defect awaiting a fix here:

1. **The Edit-tool path is still unprotected (R-lock gap).** This is an *advisory* lock
   between processes that cooperate by calling the helper. An LLM/hand edit happens at the
   tool layer and cannot acquire it. A hook write racing an Edit-tool write on the same file
   can still lose an update. Out of scope by design; logged, never treated as solved.
2. **No automatic release on crash.** "Existence == lock" has no kernel-backed owner, so a
   sidecar left by a killed holder is only reclaimed once it ages past
   `TASKFLOW_LOCK_STALE`. This is the deliberate trade against `flock`: it structurally
   removes the orphaned-inode hazard (a locked fd surviving while a fresh inode appears at
   the same path) at the cost of a bounded post-crash wait. Holds are sub-millisecond, so
   the trade is heavily favourable. `scripts/clean_locks.py` does **not** sweep this
   population — stale-break does.
3. **"Holders release within the stale threshold" is an implicit precondition.** A
   sidecar's mtime is set once at create time and never refreshed, so a holder running
   longer than `TASKFLOW_LOCK_STALE` *looks* stale while still live. On POSIX a waiter can
   then unlink a live holder's sidecar, and that holder's own release unlinks
   unconditionally — which can cascade into removing a successor's fresh sidecar. On win32
   the OS prevents it: an open handle makes another process's unlink fail. Measured holds
   are sub-millisecond against a 10 s threshold — four orders of margin — which is why this
   is acceptable rather than alarming.
4. **The stale-break TOCTOU window is narrowed, not eliminated.** The check re-reads mtime
   immediately before unlinking, but between that read and the unlink the old holder could
   release and a third party create a fresh sidecar, which the break would then remove. The
   Pi side carries the identical residual window. Do not describe it as solved.

**Rollout constraint.** The pre-v2 implementation opened with `O_CREAT` (no `EXCL`) and then
`flock`ed, so it ignores a v2 sidecar's existence entirely. During any mixed-version window
between the two harnesses there is no cross-harness protection at all — not degraded
protection, none.

## state_file

Path: `_projects/_state/{session_id}.json`

The hook (`session_init.py`) writes the full schema below. The project-router subagent is read-only and does not write state. Capture round-state is NOT a JSON field — it lives in sidecar files (to avoid clobbering by concurrent state rewrites): `{session_id}.bind` (the `capture` lifecycle `{status, items, requested_ts, tried_notes, tried_tasks}` — `items` being that round's frozen closed set `{tasks, notes, allow_tasks, note_owners, carry_only}`, where `allow_tasks` is the pre-self-log-subtraction task set that widens the `confirmed` membership gate only, and is absent from a `.bind` written before the key existed (the gate then falls back to `tasks` alone); `note_owners` (capture-detection-gaps.md §1.11, W7) is the post-self-log-subtraction subset of `tasks` that came from resolving a note owner out of THIS round's slice — it drives the `referenced` over-bind boundary, falling back to `tasks` when absent — and `carry_only` is a bool, true when the round's slice contributed no task write and no note owner of its own (opened solely by `[tasks:]` exec carry), which skips the expiry placeholder backstop for the round wholesale — plus the round state `{touch_cursor, round, log_seen, round_base, history}` — `history` being the frozen `items` (`tasks` / `notes` / `allow_tasks` / `note_owners` / `carry_only`) of the last 3 rounds, keyed by round number, which is what lets a sidecar delivered after its round closed still be membership-checked against its own round (R-1) — and `exec_tried` skip records — the exec-carry 打止め set, holding BOTH `_rel()` repo-relative paths of resolved-but-unbindable tasks AND bare basenames of carries that resolved to no task at all, two shapes that are disjoint because a `_rel()` value always starts `_projects/` — plus `## Next Steps` freshness debt (capture-detection-gaps.md §1.12, W8): `ns_hash` (`{qualified key: digest}`, the section's normalized content hash frozen the first time a task enters an open round), `ns_debt` (`{qualified key: int}`, rounds a real summary has applied while that digest stayed unchanged), and `tried_ns` (qualified keys whose rewrite proposal has already been surfaced once, 打止め) — all three sit directly under `capture`, not inside `items`, because their only consumer is `main()` itself (`_apply_capture` never receives them); writer = this hook only — `precompact_flush.py` reads it but never writes), `{session_id}.touched` (the append-only touched-path ledger written by `touched_capture.py`), and `{session_id}.r{N}.capture` (the async judgment sidecar for round `N`; writer = the `taskflow:progress-capture` subagent only, at the absolute path the hook handed it, consumed and unlinked by the hook after a successful apply — the un-suffixed `{session_id}.capture` is the pre-R-1 name; its compat branch has been retired, so such a file is never read and only the 7-day sweep collects it). (`{session_id}.captured` is a legacy marker, no longer written — only swept by the 7-day cleanup.)

```json
{
  "project": "<current active project, or empty string>",
  "rules_loaded": true,
  "indexed_project": "<last project the index was injected for>",
  "guidelines_loaded": true,
  "project_rules_indexed": "<last project the rules.md full body was injected for>",
  "origin": "cc",
  "parent_session_id": "<parent session id if forked; absent otherwise>",
  "inherited_tasks": ["<task filenames inherited from the parent, if forked>"],
  "exec_bind": ["<owning task basenames carried via [tasks:]; union-merged, append-only>"],
  "symbol_route_probe": [{"ts": "<iso8601>", "due": true, "code": 1, "m": 12, "k": 8}],
}
```

`rules_loaded` / `guidelines_loaded` / `indexed_project` / `project_rules_indexed` gate the once-per-session (or per-switch) injections and are reset by `session_compact_reset.py` after auto-compaction. `project_rules_indexed` tracks the last project whose `rules.md` full body was injected; resetting it on compaction re-injects the primer (compaction summarizes the primer body away). `origin` identifies the generator (`cc` = Claude Code). `parent_session_id` / `inherited_tasks` appear only on forked sessions. `symbol_route_probe` is an observation ledger, not a gate: one entry per turn the push ran, capped at the last `SYMBOL_ROUTE_MAX` (100). `due` is `notes_push.symbol_route_due(paths, s, k)` — the STRONG candidates overflowed (`s > k`) and the turn touched a code file — which is the denominator for asking whether the model took the symbol route on the turns that route is written for. `code` / `m` / `s` / `k` are kept so the question can be re-asked under a different predicate without re-running anything; `m` alone cannot reproduce `due`, which is why `s` is stored beside it. Nothing injected reads it; a model shown the flag would stop being what the flag measures.

### Writers

| Actor | Timing | Condition |
|---|---|---|
| `session_init.py` (hook) | every turn once the workspace is opted-in (`_projects/` exists) — including projectless turns, which write empty-`project` state (swept after 7 days; see below) | writes the full schema; project from explicit `pj:` only — **no path inference** |
| `session_progress_capture.py` (hook) | session end | recovers `project` from a `[pj:...]` line (self-heal); union-merges `exec_bind` from a `[tasks:]` carry in the assistant's last message |
| `scripts/pj_rules.py reset-indexed` (via `/pj-rules` skill) | after a confirmed `rules.md` write | merge-preserving reset of `project_rules_indexed` only, so the updated body re-primes next turn; never hand-edited by the skill |

### Readers

| Actor | Timing | Purpose |
|---|---|---|
| `session_init.py` (hook) | 2nd turn onward | fetch `current_project` + injection flags |
| `session_sync.py` (hook) | session end | determine the copy-destination project |
| `session_progress_capture.py` (hook) | session end | determine the project for the capture prompt |
| `session_compact_reset.py` (hook) | on compaction | reset injection flags (other fields preserved) |

## `pj:` syntax

Place `pj:<project_name>` near the beginning of the prompt. It is recognized at the start of the prompt or after any whitespace, so it may follow other leading lines (e.g. `mode:`); it need NOT be the literal first line. **`pj:` (and `norouter`) are only recognized within the first 500 characters of the prompt** — occurrences beyond that window are ignored (prevents accidental project switches from literal mentions in body text).

| Input | Effect |
|---|---|
| `pj:pi-studio-dev` | Set / switch the project |
| `pj:none` | Clear the project (declare no matching project) |
| `pj:?` | Discovery: rank `_projects/index.md` by relevance; does NOT change the active project |
| `norouter` | Bypass taskflow entirely for this turn (the hook exits without writing state or injecting) |
| omitted | Keep the existing value; the project is NOT inferred from context |

### Rationale

The YAML `key: value` form is a shape the model already recognizes as a metadata declaration. `pj=xx` (clashes with shell variables), `#pj=xx` (clashes with H1), `@pj=xx` (clashes with `@mention`), and `project=xx` (too long) were all considered and rejected in favor of `pj:xx`.

## subagent delegation — design decision

### Problem

When routing and task execution share a single generation path, they compete for attention. On technically dense tasks, routing was repeatedly skipped.

### Resolution

Delegate routing to a dedicated subagent (`model: sonnet`, isolated generation path). The main agent's system prompt keeps only a short instruction to "invoke the subagent." Attention is now separated.

The router subagent is intentionally lightweight: per-turn reads + decisions only. Heavy operations (drift detection, rebuild, approval bulk moves) live in the `/progress` slash command which runs on user demand.

## Additional mechanisms

### Fork inheritance

When a session is forked, Claude Code copies the parent's JSONL transcript, rewriting `sessionId` **and** — in current Claude Code — each message `uuid`, so uuid comparison cannot identify the parent. On the first turn of a new session, `session_init.py` (`detect_parent_session`) instead relies on two signals that survive the copy: (1) **primary** — the parent's injected `[Progress Session] session_id=<parent>` marker, preserved verbatim: it scans the transcript head (`PARENT_MARKER_RE`, first `PARENT_SCAN_LINES` lines) for a `session_id` other than its own; (2) **fallback** — the first `type=user` entry's `(timestamp, message content)` pair, matched against the head of recent sibling JSONLs in the same directory. The child then inherits the parent's `project` from the parent state file, and scans the project's `1_in_progress/` tasks for the parent session's `[s:<sid>]` log tag. The scan first tries the 12-char tail tag (new format); if that yields zero matches, it falls back to the first-8 prefix (legacy format for sessions before the tail-12 migration). A `[Forked Session]` block is injected on that first turn so the LLM continues the inherited tasks (logging under the new `session_id`).

### `origin` field

State files carry `origin: "cc"` (generator = Claude Code). `backfill_origin.py` is a one-shot utility that stamps `origin: "cc"` onto pre-existing state files that predate the field.

### Progress-capture self-heal

If `session_progress_capture.py` finds an empty `project` in state at session end, it recovers the project from a `[pj:<name>]` line in the assistant's last message (searched within the leading-line region, order-agnostic — the `[pj:]` line shares that region with other plugins' leading lines such as `[Mode:]`). This rescues cases where `session_init.py` failed to persist the project on the first turn, or a fork inherited an empty parent state.

### exec-binding (`[tasks:]` carry)

When a session does task work whose result lands OUTSIDE the task's own `tasks/<status>/*.md` file (execution-by-reference), the LLM lists the owning task filename(s) in a `[tasks: a.md b.md]` leading line. `session_progress_capture.py` union-merges these into the state `exec_bind` array and code-binds each owning task's `@log` (provenance note `(auto) executed via [tasks:] carry`), so the work is recorded even though `tasks/` was never edited. Bind failure (no `@log:end`) is surfaced once as `auto-skip(ambiguous)` and recorded in `exec_tried` to stop retrying (INV-1 c). A carry that resolves to NO task md in the project is surfaced once as `exec-skip(unresolved)` — with a best-effort `(exists in: <project>)` hint when the basename is found in another project this session already resolved — and 打止め'd by a bare basename in `exec_tried`; before that it was lost with zero trace on every channel, since execution-by-reference leaves no `.touched` line by construction. Resolution itself is never suppressed: a task claimed before it exists still binds on a later Stop. The `[tasks:]` instruction is wired into the injected prompts (`project_routing.md` "Response leading lines" + `guidelines_reminder.md`), parallel to `[pj:]`; direct task-file edits need no `[tasks:]` (the PostToolUse `.touched` capture records them). See `project-notes/specs/exec-binding.md`.

### ACTION_REQUIRED preflight banner

While a project is active but its `progress.md` does not yet exist, `session_init.py` injects an `!!ACTION_REQUIRED (preflight)` banner every turn. The banner has two variants, selected by the same `heavy_turn` gate that decides the full guidelines:

- **heavy turn** (new session, project re-entry, project switch, the turn after compaction): the full procedure — ask the user to approve, then scaffold `index.md` and `project-notes/index.md`, add the matching row to `_projects/index.md`, and run `scripts/rebuild_progress.py` for `progress.md`, before starting user work. This scaffolding is permitted even inside Plan mode.
- **every other turn**: a standing notice naming the same writes, with no instruction to ask. The procedure is already in context on those turns, and restating "ask the user to approve" there made the LLM re-ask on every turn instead of proceeding (measured: 7 re-asks across 6 consecutive turns). The banner carries no record of whether the user already declined — the hook cannot observe that — so the notice is unconditional while `progress.md` is missing.

`progress.md` is generated, never hand-written by the LLM: `rebuild_progress.py` creates it from its own skeleton with the `@table` markers a later rebuild needs. The PostToolUse auto-rebuild only fires on `tasks/` paths, so at scaffold time the script is run explicitly.

The two variants carry different amounts of format, and the reason is delivery, not wording taste. The banner is assembled immediately after the bootstrap block, inside the preview a heavy turn's over-budget payload collapses to; that preview is cut back to the preceding line break and the banner is a single line, so one unit over drops the whole banner rather than truncating it. The heavy variant therefore carries the procedure only — and points at the rebuild command rather than inlining it, because an absolute plugin path would make the fit machine-dependent. `_projects/<project>/index.md`'s skeleton and the rebuild invocation live in `project_routing.md` "Project index format" (canonical), and the skeleton is repeated in the standing notice, which is the variant the approval-then-write turn receives and is not preview-bound (measured: notice line ends at u16 945, whole non-heavy payload 6,253 against the 10,000 inline cap).

`fork_context` is assembled AFTER the banner for the same reason: its `inherited_tasks` list is a comma-joined filename list with no length bound, and anything ahead of the banner pushes the banner's line toward that cut. Losing an advisory "continue these tasks" block beats losing a blocking preflight instruction, and it also makes the banner's measured fit independent of whether the session was forked.

`tests/test_session_init_bootstrap.py` pins the preview fit on both counting bases (u16 against 2,000 and UTF-8 bytes against 2,048 — the two primary figures disagree and a non-ASCII `state_file` path separates them), a long-project-name arm since the name is interpolated more than once, the banner-before-fork_context order, the agreement between the canonical skeleton and the notice (every column-0 element of the canonical fence quoted in the notice, not its section names alone — the purpose line went missing under the narrower gate), and that no injected block reaches the agent with an unexpanded `${CLAUDE_PLUGIN_ROOT}`.

**Prompt blocks carrying a command must be in the expansion list.** The harness does not expand `${CLAUDE_PLUGIN_ROOT}` in hook stdout (only in `hooks.json` commands and agent definitions), and the agent's own shell has no such variable, so `session_init.py` substitutes it before assembly — for `guidelines_content` and `routing_content`. A prompt file that gains a command and is not in that list delivers a path the agent cannot resolve, and the agent then goes looking for the script. The commands themselves use `uv run --script`, which the `scripts/` entry points require because they declare inline dependencies.

The `!!ACTION_REQUIRED (preflight): ` prefix is byte-identical in both variants. Design: `project-notes/specs/scaffold-banner-heavy-gate.md`.

## Path resolution

### Paths used by hooks

| Path | Resolution |
|---|---|
| `_projects/` (every root-resolving hook: `session_init.py`, `session_sync.py`, `session_compact_reset.py`, `session_progress_capture.py`, `touched_capture.py`, and `precompact_flush.py`, which imports the already-resolved `PROGRESS_ROOT` / `STATE_DIR` / `STATE_ROOT` from `session_progress_capture` instead of deriving its own; the payload-derived hooks resolve no root — see the layer table below) | `STATE_ROOT + '/_projects'`, where `STATE_ROOT = _find_state_root(os.getcwd()) or os.getcwd()` — the nearest ancestor of the cwd (the cwd itself included) that holds `_projects/_state`, falling back to the cwd when no ancestor qualifies. `_find_state_root` is defined **once**, in `touched_capture.py`, and imported by the other four (`from touched_capture import _find_state_root`); no copy of the walk exists. The same `STATE_ROOT` is the base each hook normalizes recorded paths against — `touched_capture.py`, `session_progress_capture.py` and `precompact_flush.py` all set `cwd = STATE_ROOT` in `main()` before calling `normalize_path(p, cwd)` / `_rel(path, cwd)` — so the ledger location, the ledger read base and the relative-path base always move together. |
| `_projects/_state/` — the **bulk stale-marker sweep target only** (`session_progress_capture.py`) | `SWEEP_STATE_DIR = os.getcwd() + '/_projects/_state'`. Deliberately CWD-pinned and deliberately **not** following the ancestor search; it is a second, separately named module-scope constant sitting next to the search-derived root. See the sweep-pin row in the layer table below. |
| `prompts/` | derived from `__file__` back to the plugin root |
| `<config dir>/plans/` | `<config dir>` = `$CLAUDE_CONFIG_DIR` if set, else `os.path.expanduser('~/.claude')` |
| `<config dir>/projects/.../memory/` | same `<config dir>` + encode CWD (`lower().replace(':', '-').replace('/', '-')`) |

`$CLAUDE_CONFIG_DIR` is read **literally**, replicating Claude Code's own behavior (verified 2026-07-28): no `expanduser` and no variable expansion, so `~/x` is a cwd-relative literal path, and relative values are resolved with `os.path.abspath` against the process CWD. `session_sync.py` runs inside the writer session's process tree and therefore resolves a **single** config dir (env if set, else `~/.claude`). The kanban reader (`generate_kanban.py`'s `build_cc_session_index`) is the exception: it scans the **union** of `$CLAUDE_CONFIG_DIR` and `~/.claude` (`_cc_config_dirs()`), env first, so a UUID present in both universes resolves to the env one. Per-workspace divergence of `CLAUDE_CONFIG_DIR` is unsupported — see the note in `README.md`.

### The Stop hook is single-project for *state*, multi-project for *touched resolution*

The session's project (`_state/<session_id>.json` → `project`) selects **one** primary project, and that stays true for the state file, for the `.bind` sidecar, and for the `[tasks:]` exec-binding carry. Touched-task resolution is **not** limited to it: a session legitimately writes into more than one project of the same repository, and until 2026-08-09 every such write was silently dropped (the task was basename-matched into the session's project and then rejected by the boundary guard).

`session_progress_capture.py` / `precompact_flush.py` therefore derive the project from **each `.touched` line** (`^_projects/([^/]+)/`) rather than from `state['project']`:

| Step | Rule |
|---|---|
| Extract | `_projects/<name>/...` → `<name>`; a line that does not start with `_projects/` (an absolute path from another repository, a cwd-external write) is out of scope — cross-**repo** binding is not attempted. |
| Validate | `<name>` counts as a project only if `_projects/<name>/tasks/` is a directory. This is what rejects `_projects/_state/...` lines, which match the pattern but name the sidecar directory. |
| Resolve | Each accepted project gets its own basename index, its own note reverse index, and its own boundary guard, so a basename that exists in two projects binds the copy the ledger actually named. |
| Key | Every internal task key — `capture.items.tasks`, `capture.items.allow_tasks`, `capture.items.note_owners`, `tried_tasks`, `log_seen`, `round_base` — is the qualified `"<project>/<basename>"`, as are the capture-context `touched_tasks`, the `@log`-related stderr/block report lines, and the PreCompact stdout list. A `.bind` predating this change holds bare basenames; they are read as the primary project's keys and rewritten qualified on the next Stop. `capture.items.carry_only` is a bool, not a key, and needs no such qualification. |

The capture subagent's context block carries `project_roots` (`{name: absolute root}`) alongside the primary `project_root` so it can resolve a qualified task, and its sidecar entries may carry an optional `project` field (absent = primary). Note paths in that sidecar stay project-relative under `project-notes/`, read against the entry's own project root. Design: `project-notes/specs/capture-detection-gaps.md` §3.

### Hooks (ancestor-anchored) vs. the command / viewer layer (`TASKFLOW_PROJECT_ROOTS`)

Project-root resolution is **deliberately split** between two layers, and the two are NOT unified:

| Layer | Resolves `_projects/` via | Rationale |
|---|---|---|
| **Hooks** (`session_init.py`, `session_sync.py`, `session_compact_reset.py`, `session_progress_capture.py`, `touched_capture.py`, and `precompact_flush.py`, which imports the resolved names from `session_progress_capture`) | `_find_state_root(os.getcwd()) or os.getcwd()` — nearest ancestor (cwd included) holding `_projects/_state`, else the cwd itself | A hook process inherits the cwd the session was **launched** in, and that value is fixed for the session's whole life — a `cd` inside a Bash tool call does not reach hook processes. A pure-CWD anchor therefore turned a subdirectory launch into a whole-session silent no-op: `_projects/` resolved to a path that does not exist, `session_init.py` wrote no state (or, on a `pj:` engagement, bootstrapped a **stray** `<subdir>/_projects/` tree), the `.touched` ledger was never written, and the Stop hook opened no round. The walk re-anchors every one of them on the tree that actually holds the state, and the `or os.getcwd()` fallback reproduces the pre-change value byte-for-byte whenever no ancestor qualifies — including the ordinary repo-root launch, where the walk stops at the cwd itself. `touched_capture.py` received it first (2026-08-19); the remaining five followed on 2026-08-20, because a ledger the Stop hook cannot find is no better than one that was never written. An unrelated tree that happens to hold `_projects/_state` is reachable by the walk, but the `<session_id>.json` orphan guard in `main()` stops a foreign ledger from being written — a second wall only, defeated by a fixture that aligns the SID, so it is never the basis for test isolation. |
| **Sweep pin** (`session_progress_capture.py`'s `_cleanup_stale_markers` target only) | `SWEEP_STATE_DIR = os.getcwd() + '/_projects/_state'` — CWD-pinned, does **not** follow the walk | The bulk sweep is a predicate-driven `os.remove` loop that runs before stdin is read, i.e. before any session identity is known. Letting it follow the walk would grow its reachable-cwd set from "a directory that contains `_projects`" to "every descendant of a directory that holds `_projects/_state`" — the tmp trees E2E fixtures are built in included — re-arming the 2026-07-17 incident from every subdirectory of the repo, and invalidating the empirical basis on which `TASKFLOW_SWEEP_MAX = 50` was chosen. Pinning keeps the bulk sweep's blast radius byte-for-byte what it was: in a subdirectory-launched session the pinned path does not exist, `os.listdir` raises, the enclosing `except OSError` swallows it, and the sweep is the same silent no-op it always was — that session performs no GC, and the next repo-root session collects what it left. This is **not** the stronger claim that no deletion follows the walk: the SID-scoped round-sidecar removals in `main()` (`scan_round_sidecars(STATE_DIR, session_id)`) operate on this session's own `<sid>.r<N>.capture` files under the **resolved** `STATE_DIR` and must follow it, or the fix breaks. The two constants are required to stay different under a nested cwd (`tests/test_hooks_state_root_rollout.py`); unifying them is the refactor that undoes the isolation the pin buys. |
| **Payload-derived** (`task_rebuild_progress.py`, `notes_index_reminder.py`) | the `_projects/<project>/` prefix of the written path in the hook's own payload — `os.getcwd()` is never read | These two act only when the payload text itself already names `_projects/<project>/...` (a `re.search` over the written path, resp. over the Bash command), so the root travels with the payload. They were immune to the launch-cwd failure mode described above even before the ancestor walk landed, which is why neither belongs in the hooks row. |
| **Command / viewer layer** (`/progress` skill, `scripts/generate_kanban.py`) | `$TASKFLOW_PROJECT_ROOTS` — a `;`-separated list of root dirs, first existing `<root>/<project>/` wins; falls back to `_projects/` in the CWD when unset | These are explicitly user-invoked (a viewer, an on-demand command) and may legitimately need to reach a project that lives under a different root than the current CWD. |

The premise this split was originally argued from — that a hook "fires inside one Claude Code session whose CWD *is* the workspace" — does not hold, and the hooks row above is what replaced it. Measured on Claude Code 2.1.233 (2026-08-19, three-arm probe): hook cwd is the cwd the session was launched in and nothing moves it afterwards (a same-session `cd` in a Bash tool call leaves `os.getcwd()` unchanged in the PostToolUse process), but a session launched in `<ws>/sub` reports `<ws>/sub` — and reports it identically in `os.getcwd()`, in `CLAUDE_PROJECT_DIR`, and in the hook payload's `cwd` field, so neither of the latter two is a better anchor than the cwd itself. As of 2026-08-20 no hook rests on the premise any more; the only cwd-pinned path left in the hook layer is the sweep target, and that pin is a safety property rather than an anchoring one.

Two cwd derivations in the hook layer deliberately do **not** follow the walk, and must not be "unified" with it: `SWEEP_STATE_DIR` (above), and `session_sync.py`'s `MEMORY_DIR`, whose `<config dir>/projects/<encoded cwd>/memory` path is Claude Code's own key for the **launch** cwd — re-anchoring it would read a different project's memory directory.

This boundary is intentional: the per-turn hook path stays anchored to a single tree derived from the launch cwd and side-effect-predictable, while the multi-root flexibility is confined to the on-demand command/viewer layer. (Unifying the two — e.g. making hooks honor `TASKFLOW_PROJECT_ROOTS` — was considered and rejected; a hook that silently retargets a different root mid-session would break the "one session ↔ one workspace" invariant the state files rely on. The ancestor walk does not reopen this: it runs once, at module scope, from a cwd that cannot move during the session, so the resolved root is as fixed as the cwd was.)

**Migration note — `.bind` `exec_tried` key shape.** `_rel(path, cwd)` values persist in the `exec_tried` list of `_state/<sid>.bind`, and `_load_bind` / `_save_bind` round-trip that list verbatim (no normalization, no validation on either leg). Before 2026-08-20 every reachable configuration produced keys starting `_projects/`, and after the change it does again — but a `.bind` written from a subdirectory cwd during the short window in which `PROGRESS_ROOT` already followed the walk while the `_rel` base was still `os.getcwd()` holds `../_projects/…` keys. Such a file stays fully readable: it parses, and `reminded` plus the whole `capture` lifecycle block are honoured unchanged. The one degradation is that its `../_projects/…` entries can never match the probe again, so an exec-bind whose 打止め was recorded in the old shape is retried **once**, emits one extra `auto-skip(ambiguous)` line, and appends the new-shape key beside the dead one; the second Stop is silent. The dead entry is never pruned. No migration code exists and none is planned — the cost is one redundant cycle per affected task, once.

### When `_projects/` is absent

`session_init.py` (UserPromptSubmit) **bootstraps** `_projects/`, `_projects/_state/`, and a template `_projects/index.md` when they are missing — but only on the first prompt that includes an explicit `pj:<project>` or `pj:?` discovery. Since the ancestor walk landed, the tree is created under the resolved `STATE_ROOT`, so a session launched inside a subdirectory of an already-opted-in workspace joins the existing tree instead of bootstrapping a stray `<subdir>/_projects/` beside it. In a workspace that has **not** been opted in (no `_projects/` anywhere up the chain), a prompt without any `pj:` engagement `sys.exit(0)`s immediately without creating `_projects/`, so the plugin can be enabled in any workspace without side-effects until the user first engages taskflow. The other hooks (`session_sync.py`, `session_progress_capture.py`, and `task_rebuild_progress.py`'s project-dir check) treat a missing `_projects/` as a harmless no-op and `sys.exit(0)`.

Note: this no-side-effect property applies **only before opt-in**. Once `_projects/` exists, `session_init.py` writes a `_state/<session_id>.json` file **every turn**, including projectless turns (`pj:none`, `pj:?` discovery, a fork inheriting an empty parent, or simply no `pj:` while `_projects/` is present) — those produce an empty-`project` state file. This is deliberate (the F5a "stop writing projectless state" proposal was withdrawn: it would have broken the capture self-heal path and the fork-detection memo). Empty-`project` state is bounded by the 7-day stale-marker sweep in `session_progress_capture.py`'s `_cleanup_stale_markers` (non-empty `project` state is kept indefinitely, since `generate_kanban.py` resolves full UUIDs from it). The sweep's per-Stop delete budget (json + sidecar markers, combined) is capped at `TASKFLOW_SWEEP_MAX` (default 50, env-overridable); past-cutoff candidates are removed oldest-mtime-first, so a capped sweep still makes monotonic progress across Stops instead of re-selecting the same subset, and any deletion (or a cap hit) is logged to stderr. This follows a 2026-07-17 incident where an uncapped, unlogged sweep run against the real `_projects/_state/` with the wrong CWD deleted 250 session-state files in one Stop (see `project-notes/specs/capture-hook-sweep-sandbox.md`). The sweep's target is the CWD-pinned `SWEEP_STATE_DIR`, not the walk-resolved `STATE_DIR` — see the sweep-pin row under "Path resolution" for why the two are kept apart. One consequence worth stating: the `main()` gate that admits the sweep (`isdir(PROGRESS_ROOT)`) is walk-resolved while its target is pinned, so a subdirectory-launched session passes the gate and then sweeps nothing. That session performs no GC at all; the backlog is collected by the next session launched at the state root.

## Directory layout

```
_projects/
  index.md               all-projects index
  _state/                session state (hook-managed)
    {session_id}.json
  <project>/
    index.md             project overview
    progress.md          task index (free-text sections + auto-regenerated table region)
    tasks/
      0_todo/            not started
      1_in_progress/     started; work ongoing
      2_done/            complete (human-approved)
    project-notes/
      index.md           4-column: File | Description | Tags | Updated
      specs/             designs, decisions, ADRs
      investigations/    research, analysis, post-mortems
      checks/            verification items, checklists
      procedures/        step-by-step instructions
      backlog/           candidate items, ideas
      _archive/          exhausted
    _archive/            project-level archive (legacy pre-v0.2.2 files, etc.)
    plans/               plan copies (archived by Stop hook)
    memory/              memory copies (archived by Stop hook)
```
