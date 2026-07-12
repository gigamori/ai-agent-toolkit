---
name: wiki-ingest-sessions
description: Ingest EVERY cc-log session of the active wiki's resolved scope (Path B) into the wiki via the same 2-stage extract→apply core as /wiki-ingest. Resolves the session-id SET with the driver's read-only `session-plan` verb (ts-ascending; the set follows `--workspace` / `--pj <name>` / the resolved wiki scope), then ingests each session in its OWN independent transaction (one sid = one begin→finish, `--kind=fe_b_prime`). Explicit write-bearing skill (hook-independent). Usage `/wiki-ingest-sessions [--workspace | --pj <name>] [--root <path>] [doc_type=...] [write_mode=...] [apply_fanout_k=...]`.
disable-model-invocation: true
allowed-tools: Bash(uv run *), Bash(rm -rf *), Agent, AskUserQuestion, Write
---

# /wiki-ingest-sessions

Arguments: `$ARGUMENTS`

You are the Path B ingest **orchestrator**. This is the session-set-wide sibling of
`/wiki-ingest`: instead of one source token, you ingest **every cc-log session of the
resolved set** (workspace union, a taskflow project, or the current project — see Step 2).
You do NOT run the deterministic envelope yourself — the
`ingest_driver.py` CLI owns it (config resolution, the single file-journal transaction
per session, the FE-B' projector front-end, redaction, the turn-content-hash ledger
dedup, the central join, index/log). Your job is to (1) resolve the wiki root, (2) call
the driver's read-only `session-plan` verb to get the ts-ascending session-id set, and
(3) loop the SAME per-session `begin → Stage1 → Stage2 → apply-finish` cycle that
`/wiki-ingest` runs — **once per sid, each in its own independent transaction** — with
failure-continue and a final summary. You NEVER author wiki page content yourself —
the Stage2 apply-worker authors it and returns a page manifest; YOU pass every cluster's
manifest through the driver's compound `apply-finish` verb (E3), where the allowlist write
tool (`write_tool.WriteSession`) gates every page write and, on success, the same verb
performs the central join + single commit.

Each session is a **complete, independent transaction**: there is NO batch-spanning
transaction across sessions (mirrors `/wiki-ingest`'s glob/dir loop, keyed per-sid here
instead of per-file).

## THE ONE UN-DROPPABLE INVARIANT (read first, never bypass)

> The whole ingest of ONE session is ONE file-journal transaction (git-independent;
> supersedes D21), and that transaction is owned **entirely by `ingest_driver.py`**, not
> by you. `begin` `acquire_lock`s `.llmwiki.lock` THEN checkpoints (opens the
> write-ahead undo journal `.llmwiki.txn.d/`) BEFORE the front-end; `finish` performs
> exactly ONE `commit` (discard the journal, success) or `rollback` (replay it, fail)
> and always `release_lock`s. Between them the transaction state lives on disk in the
> `.llmwiki.txn` sidecar — you NEVER thread the journal dir, budget, lock handle, or
> fe-hash yourself; you pass the driver only the opaque `<root>` (plus `<sid>` to
> `begin`, the per-cluster manifests to `apply-finish`, and — only on a pre-apply stage
> failure — a `fail` outcome to `finish`). Every byte still passes through
> `write_tool.WriteSession` inside the `apply-finish` verb YOU run over the workers'
> returned manifests (the verb journals each write); NEITHER LLM stage has a write
> tool — the Stage2 apply-worker authors a manifest only (`tools: Read`), and Stage1 —
> which alone reads the untrusted projected transcript — likewise has **no write tool
> at all** (`tools: Read`). Trust is decided by *location* (`wiki/` vs
> `wiki/derived/`), not by the LLM. No git is invoked anywhere. (driver-plan §2/§3;
> design D17/D19/D20/D23; gitless-journal-transaction.md.)

If any step would write a wiki page outside the Stage2 allowlist tool, or would have you
thread transaction state by hand, STOP and report
`[BLOCKED: write outside transaction/allowlist]`.

> **Model requirement — do not run on a lightweight/minimal model.** This skill is a
> multi-stage orchestration run once **per session** (`begin` → Stage1 extract subagent →
> Stage2 apply subagent → `apply-finish`). A lightweight or minimal model tends to drop the
> Stage2 apply dispatch, or mistake the raw Stage1 blob for finished pages, or skip the
> `apply-finish` call — any of which leaves that session's transaction **open** (a stale
> `.llmwiki.lock` / `.llmwiki.txn` with no pages written; see the stuck-transaction
> recovery note at the end) and stalls the whole per-session loop. Run it on a capable model.

> **Execution discipline — run the loop yourself, one driver call at a time (a real Path B
> run has failed by violating every rule below; each is a hard rule, not style).**
>
> - **NEVER delegate the loop.** YOU — the session running this skill — execute Steps 3–8
>   directly. Do NOT hand the loop, or any single step of it, to a general-purpose subagent.
>   The ONLY Agent-tool dispatches in this skill are the two declared stage workers
>   (`llm-wiki:wiki-ingest-extract`, `llm-wiki:wiki-ingest-apply`); a delegated orchestrator
>   cannot spawn them and will improvise a broken harness around the design.
> - **NEVER script the loop.** Do NOT write bash/python/PowerShell batch scripts that wrap or
>   loop the driver verbs (e.g. `begin` over all sids up front). One driver verb = one Bash
>   invocation; sids run strictly sequential — one sid's `begin` → stages → `apply-finish`
>   completes its transaction before the next sid's `begin` (the `.llmwiki.lock` and the
>   ledger read-after-write depend on it; a batched `begin` deadlocks on the held lock).
> - **NEVER parse driver stdout with tools.** No `jq`, no `ConvertFrom-Json`, no improvised
>   python parsers. Every deterministic extraction is already code-owned by a driver verb
>   (`session-plan` / `project-batch` / `begin` / `plan-fanout` / `apply-finish`); the stdout
>   JSON is deliberately small (E1) — read the fields you need directly from it.
> - **NEVER hand-clear a stuck transaction.** A residual `.llmwiki.lock` / `.llmwiki.txn.d`
>   (from an interrupted prior run) is recovered ONLY by `ingest abort` — never `rm -f` the
>   lock/journal (a partial `rm` leaves the `.llmwiki.txn.d` directory and a half-open
>   transaction). If `begin` reports a lock-held error, STOP and run `ingest abort` first.

The turn ledger makes Path B **idempotent and incremental**: a turn already owned by a
prior ingest (Path A or a previous Path B run) is dropped at projection time by the
projector's ledger diff, so a re-run files only the novel turns. Because that dedup is
silent per-turn, this skill MUST surface the **ledger-skipped turn count** in the
summary (see Step 3) so an incremental re-run is never a silent no-op.

## Step 0 — Parse arguments (deterministic, do NOT guess)

Parse `$ARGUMENTS` into:

- `--workspace` — OPTIONAL explicit selector (D3, a bare boolean flag, no value). When
  present, EVERY sid registered across the whole workspace's `_projects/_state/*.json`
  is planned (no project filter) — mutually exclusive with `--pj` (pass at most one of
  the two; if both are given, `--workspace` wins, mirroring the driver).
- `--pj <name>` — OPTIONAL project selector (space form `--pj <name>` or `--pj=<name>`).
  When present, only the sessions assigned to `<name>` are planned; when BOTH `--pj` and
  `--workspace` are ABSENT, the driver follows the resolved wiki scope (`$WIKI_SCOPE` from
  Step 1) — do NOT pass a project name from the CWD or guess a scope yourself, the
  driver's `session-plan` verb owns that resolution (Step 2).
- `--root <path>` — OPTIONAL top-override for the wiki root (Q4). It is NOT a `key=value`
  axis — strip it out first, before the axis parse.
- axis overrides (`doc_type=...`, `write_mode=...`, `apply_fanout_k=...`, `external=...`)
  — the same axes `/wiki-ingest` accepts; they apply identically to every session in the
  loop.

Do NOT auto-sniff or invent a project name, and do NOT decide the session SET yourself —
that decision is made in code by the driver (D2: determinism stays in the driver, never
the LLM). If `--pj`/`--workspace` are both absent and the resolved scope's session set is
unresolvable (e.g. scope `pj`/`prompt` with no active taskflow project for this session),
the driver's `session-plan` verb fails closed (Step 2) with guidance to pass `--pj <name>`
— surface that error, do not guess a fallback.

## Step 1 — Resolve `WIKI_ROOT` (multi-scope; do NOT hardcode the CWD)

The wiki root is **resolved**, not assumed to be the CWD. Resolve it via
`wiki_root_resolver` (scopes: prompt>pj>workspace>cwd), honoring an explicit
`--root <path>` from Step 0 as the top override (Q4). Pass it as `prompt_root`, else pass
nothing (identical mechanism/wording to `/wiki-ingest`). Also capture the running
session's own id as `SID` via the `${CLAUDE_SESSION_ID}` skill-template substitution (the
harness replaces this placeholder with the literal session id before you see this text —
it is NOT an OS env var) and thread it as `--sid` so the resolver's session-aware pj
fast-path (`_projects/_state/<sid>.json` read first, D6) fires instead of degrading to a
mtime-latest scan that can cross-talk between concurrent sessions on different projects:

```bash
SID="${CLAUDE_SESSION_ID}"
RESOLVED="$(uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki resolve-root ${ROOT_OVERRIDE:+--root "$ROOT_OVERRIDE"} --sid "$SID")" \
  || { echo "resolve-root failed (NO-WIKI or resolver error) — stop"; }
IFS=$'\t' read -r WIKI_ROOT WIKI_SCOPE <<<"$RESOLVED"
```

The `resolve-root` verb prints `<root>\t<scope>` on stdout; the block above splits it
(`WIKI_ROOT`=root, `WIKI_SCOPE`=scope) so a stray tab never contaminates `$WIKI_ROOT`. If it exits non-zero (`NO-WIKI`), no wiki resolved — report
that this skill requires an active wiki (pass `--root <path>` or run from a wiki root)
and STOP. **Before acting, show the user the resolved root and scope** (e.g.
`active wiki: <root> (scope: pj|workspace|cwd|prompt)`). The driver still enforces the
marker and errors with "not a wiki root" if absent. `$WIKI_SCOPE` also feeds Step 2's
no-args session-set resolution (D2) — do not discard it.

## Step 2 — `session-plan`: resolve the session-id set (read-only, ts-ascending)

Call the driver's read-only `session-plan` verb to get the set of sessions to ingest,
already ordered by session-start timestamp **ascending** (the ts-asc order is required so
that, under the first-ingested-owns ledger, the earliest session normally owns a shared
prefix). This verb opens NO transaction (no lock, no checkpoint, no sidecar) — it only
reads.

Pass exactly ONE selector, chosen from Step 0/Step 1's inputs, plus always `--sid "$SID"`
(from Step 1 — required for the no-args `pj`/`prompt`-scope active-project resolution, D2,
and for the cwd-scope running-session ground truth, D4, unchanged):

- Step 0's `--workspace` flag was given → pass `--workspace`.
- else Step 0's `--pj <name>` was given → pass `--pj "$PJ"`.
- else (both absent — the no-args case, D2) → pass `--scope "$WIKI_SCOPE"` (from Step 1)
  so the driver follows the resolved wiki scope.

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest session-plan "$WIKI_ROOT" --workspace --sid "$SID"
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest session-plan "$WIKI_ROOT" --pj "$PJ" --sid "$SID"
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest session-plan "$WIKI_ROOT" --scope "$WIKI_SCOPE" --sid "$SID"
```

Here `$PJ` is the `--pj <name>` value from Step 0. The driver prints JSON
`{"sids": [<sid>...], "scope": "pj"|"workspace"|"cwd", "pattern": <str>}`:

- `sids` — the session ids to ingest, **ts-ascending** (novel-turn ownership is NOT
  decided here — each `begin`'s ledger diff decides it). These are the per-session
  sources for the loop. Capture `len(sids)` as the **resolved sid count** for the summary.
- `scope` — `"workspace"` (explicit `--workspace`, or no-args following a
  workspace-scoped wiki), `"pj"` (explicit `--pj <name>`, or no-args resolving this
  session's active taskflow project), or `"cwd"` (no-args, the current project's CC
  session directory — D4, unchanged); echo it so the user sees which resolution fired.
- `pattern` — the provenance of the resolve (the `_projects/_state` glob for `pj` /
  `workspace`, or the CC project dir for `cwd`); echo it to the user so the expansion is
  visible.

If `session-plan` exits non-zero it means the set could not be resolved — zero matches
(no `--pj`/`--workspace` project set, or an unresolvable current CC dir), a no-args
`pj`/`prompt`-scope session with no active taskflow project (report the driver's guidance
to pass `--pj <name>` — do NOT silently retry with a guessed project), or a non-wiki-root —
and the driver raises this as an explicit fail-closed error. Report its stderr and stop;
nothing was locked or written (`session-plan` is read-only).

**Scope note (`--pj` coverage limit):** the `--pj <name>` scope (and the no-args
`pj`/`prompt`-scope resolution it also backs) resolves the session set from taskflow's
`_projects/_state/*.json` entries whose `project == <name>` — i.e. ONLY sessions that
taskflow registered for that project. It is NOT the whole CC session directory: a session
with no `_state` file (or one created by another tool) is not in the `--pj` set. `--workspace`
(and no-args on a workspace-scoped wiki) widens this to the UNION of every `_state`
entry regardless of project (D3) — still bounded by what taskflow registered, but no
longer filtered to one project. To ingest EVERY CC session of the current project
regardless of taskflow registration, omit both flags on a cwd-scoped wiki and let the
driver resolve the current project's CC directory as ground truth (`scope: "cwd"`, D4).

## Step 2b — `project-batch`: extract all sessions' turns in ONE scan (read-only, F-H1)

Before the loop, call the read-only `project-batch` verb ONCE for the whole `sids` set.
This runs the expensive projection (a single scan of `~/.claude/projects/**/*.jsonl` for
ALL sids at once) and writes each session's extracted turns to a per-sid JSON file under a
fresh temp dir. Without this, each `begin` would re-scan the entire corpus (N sessions → N
full scans); with it, the corpus is scanned exactly once. This verb opens NO transaction
(no lock, no checkpoint, no sidecar) and writes only OUTSIDE the wiki root (the temp dir is
never journaled, never enumerated).

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest project-batch \
  "$WIKI_ROOT" $SIDS
```

Here `$SIDS` is the ts-ascending session-id list from Step 2 (all of them, space-separated,
in order). The driver prints JSON `{"out_dir": <temp dir>, "turns": {<sid>: <path>, ...},
"scanned": <count>}`:

- `turns` — a map from each `sid` to the path of its pre-extracted turn-JSON. Capture this
  map; in Step 4 you pass `begin` the `--turns=<path>` for the current sid so it does NOT
  re-scan. The turns are boilerplate-stripped and carry their content hash (the dedup /
  ledger key), so `begin`'s per-sid projection (dedup + ledger diff) stays byte-consistent.
- `out_dir` — the temp dir holding all the per-sid files. **Remember it; you MUST delete it
  after the loop (Step 3's cleanup).**
- `scanned` — the sid count (sanity: equals `len(sids)`).

If `project-batch` exits non-zero (not a wiki root, or an empty sid set), report its stderr
and stop; nothing was locked or written.

## Step 3 — Loop per sid (one independent transaction each) + summary

For **each** `sid` in `sids` (in the returned ts-ascending order), run the full per-sid
cycle Steps 4–8 with that `sid` as `$SOURCE` and its pre-extracted turn file
`turns[$sid]` (from Step 2b's map) as `$TURNS_PATH`. Each iteration is a **complete,
independent transaction**: `begin` acquires `.llmwiki.lock` and checkpoints (opens the
journal), the stages run, and `apply-finish` commits (discards the journal) on success —
or, when a stage fails **before** apply, `finish fail` rolls back (replays the journal) —
releasing the lock for **that one session**. The transaction is owned entirely by the
driver via the `.llmwiki.txn` sidecar (the ONE INVARIANT above) — you thread NO
transaction state across sessions, and you do NOT wrap the loop in a single spanning
transaction. The loop is N independent driver transactions, one per sid.

Because the loop is **sequential**, each `begin` reads the ledger AFTER the previous
session's `finish` appended its novel turns (read-after-write). This is what makes a
shared prefix file exactly once (ledger idempotent, first-ingested-owns) and what absorbs
partial failure: a session that FAILS never appended to the ledger, so the next session's
`begin` still sees that shared prefix as novel and files it (F3 — no prefix loss).

Maintain five counters across the loop: `total` (= len(sids)), `succeeded`, `failed`,
`dedup_skipped`, and `ledger_skipped_turns` (sum of the `ledger_skipped` value from each
`begin`'s JSON — Step 4).

- A sid whose `begin` reports `dedup_noop: true` (it also returns `auto_closed: true`)
  → the driver already closed that sid's transaction (rolled back + released the lock);
  report the no-op only, do NOT call `finish` (there is no sidecar to finish). Count it as
  `dedup_skipped` and continue to the next sid (Step 4's dedup branch).
  (Still add that `begin`'s `ledger_skipped` to `ledger_skipped_turns` — an all-owned
  session is exactly the incremental case F6 must not hide.)
- A sid that completes Steps 4–8 with a committed `apply-finish` → count `succeeded`.
- **Failure-continue:** if ANY step for a sid fails, roll back **just that sid** —
  never abort the other sessions — count it as `failed`, and **continue the loop**.
  Route by where it failed (Step 8): a failure **before** apply (a `begin` error after the
  marker check, a Stage1/Stage2 error, or a `plan-fanout` budget gate) → call its own
  `finish fail` (Step 8); an `apply-finish` **REJECT** (a write gate or an F2 ordinal/
  page-set mismatch) → `apply-finish` has ALREADY rolled that sid back, so do NOT also call
  `finish`. One session's failure must not stop the batch (partial success is allowed; F3
  makes the shared prefix survive on the next sid).

After the loop, **always** report the summary. First the verbatim per-transaction line:

> `N total / M succeeded / K failed / S dedup-skipped`

with `N=total`, `M=succeeded`, `K=failed`, `S=dedup_skipped`. Then, on their own lines,
the two Path B additions (mandatory — do NOT omit them; an incremental re-run must not
look silent, RS-d):

> `resolved sessions: <len(sids)> (scope: <scope>)`
> `ledger-skipped turns: <ledger_skipped_turns>`

This full summary is mandatory even if every session failed, and even if
`ledger_skipped_turns` is 0 (0 is a real, informative answer — it means nothing was
already owned). Report the whole `sids` set as planned; NEVER truncate the loop early or
silently cut it off (RS-d).

**After the summary, clean up the Step 2b temp dir.** Delete `out_dir` (the temp directory
`project-batch` returned) — it holds the pre-extracted per-sid turn files, which are no
longer needed once the loop is done. Do this on EVERY exit path (all-success, partial, or
all-failed), and report the deletion in one line (do not delete silently, and do not leave
it behind). It lives outside the wiki root, so removing it touches no wiki state.

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest project-batch-cleanup \
  "$OUT_DIR"   # the project-batch temp dir; report: "cleaned up <OUT_DIR>"
```

The driver-owned `project-batch-cleanup` verb (NOT a bare `rm -rf`) deletes the dir:
it REFUSES unless `$OUT_DIR`'s basename is a `llmwiki-turns-*` temp dir directly under
the system temp dir (the two properties `project-batch`'s mkdtemp guarantees), so a
mistyped or drifted `$OUT_DIR` can never delete an unrelated path. A crashed loop that
never reaches this line is caught by the driver's backstop prune (stale `llmwiki-turns-*`
dirs are removed at the next `project-batch`).

The Steps 4–8 below define ONE per-sid cycle — identical to `/wiki-ingest`'s per-file
cycle, keyed on a sid rather than a file path.

## Step 4 — `begin`: open the transaction, project + normalize, declare (one driver call)

Call the driver's `begin` verb once for THIS sid, with `--kind=fe_b_prime` (Path B is
always FE-B' — a cc-log session) and `--turns="$TURNS_PATH"` (this sid's pre-extracted
turns from Step 2b). It detects the marker, resolves+declares every config axis (D5),
validates the consistency invariant, acquires the lock then checkpoints (opens the journal)
BEFORE the front-end, runs the FE-B' front-end. With `--turns`, the front-end does NOT
re-scan the corpus (F-H1) — it consumes the pre-extracted turns (the session plus its agent
children, thinking excluded, boilerplate stripped, each carrying its content hash) and runs
only the cheap per-sid half: length-independent exact-dedup within the session, ledger diff
to drop already-owned turns, and `doc_type:transcript` pinned. Then redaction +
content-hash dedup, writes the raw artifact (unless a dedup no-op), writes the
`.llmwiki.txn` sidecar, and prints the contract JSON on stdout. `$SOURCE` is this cycle's
sid (the driver derives the session id via `Path(source).stem`, so a bare sid is accepted
exactly as a `<sid>.jsonl` path is; it must match the `--turns` file's sid or `begin` fails
closed).

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest begin \
  "$WIKI_ROOT" "$SOURCE" \
  --kind=fe_b_prime \
  --turns="$TURNS_PATH" \
  --out_dir="$OUT_DIR" \
  ${DOC_TYPE:+--doc_type="$DOC_TYPE"} \
  ${EXTERNAL:+--external="$EXTERNAL"} \
  ${WRITE_MODE:+--write_mode="$WRITE_MODE"} \
  ${APPLY_FANOUT_K:+--apply_fanout_k="$APPLY_FANOUT_K"}
```

(`--kind=fe_b_prime` is fixed for Path B — every session is a cc-log transcript; the axis
overrides parsed in Step 0 apply identically to every sid. The driver also echoes the
resolved-value declaration to stderr.) `$TURNS_PATH` is `turns[$SOURCE]` from Step 2b's
map — the pre-extracted turn-JSON for THIS sid, so `begin` does NOT re-scan the corpus
(F-H1); it runs only the cheap per-sid projection (dedup + ledger diff + markdown). The
driver fails closed if the `--turns` file's sid does not match `$SOURCE`.

From the printed JSON capture: `declaration`, `declaration_hash` (E1/E4 — the code-side
short hash used for the per-sid declaration-echo mitigation below), `raw_rel_path` (E1/E2 —
the wiki-relative path of the raw artifact `begin` wrote; `begin` no longer inlines the
body, so Stage1 Reads the raw from this path in Step 5), `stage1_blob_path` (#1 — the
ABSOLUTE path to Write the Stage1 blob to in Step 5; code-authored under `$OUT_DIR`, so use
it verbatim and never reconstruct a temp path yourself), `origin` (always `fe_b_prime`
here), `doc_type` (`transcript`), `max_count`,
`max_bytes`, `apply_fanout_k`, `dedup_noop`, `redaction_flags`, and **`ledger_skipped`** (the count of
turns this session dropped because a prior ingest already owns them — add it to the loop's
`ledger_skipped_turns` counter, Step 3 / F6).

Then:

- **Declaration echo — per-sid mitigation (E4 / D-2 / F4).** For the **first** sid in the
  loop, echo every `declaration` line verbatim (D5) and remember that sid's
  `declaration_hash`. For every **later** sid, compare its `declaration_hash` to the first
  sid's — an **equality check on the code-side hash only** (never re-derive or diff the
  declaration text yourself, F4): if it is EQUAL, emit the single line
  `declaration unchanged (= sid 1)`; if it DIFFERS, echo that sid's **full** `declaration`
  plus a warning that the resolved config changed for this sid. If `write_mode` resolved to
  `implicit`, announce loudly that per-apply confirmation is skipped.
- **Surface `redaction_flags`** so the human gate sees what the FE redacted.
- **Accumulate `ledger_skipped`** into `ledger_skipped_turns` (the summary must reflect
  every session's ledger skips, including dedup-no-op sessions).
- **Per-sid narration is ONE status line (E4).** Apart from the declaration handling above
  and the redaction-flag surfacing, keep each sid's progress to a single concise status
  line (e.g. `sid <k>/<N> <sid>: <outcome>`) — do not emit a verbose multi-line report per
  sid; the full accounting is the end-of-loop summary.
- **If `dedup_noop` is `true`:** report "already ingested (content-hash dedup no-op)".
  `begin` also returned `auto_closed: true` — it already rolled back and released the
  lock itself, so do NOT call `finish` (no sidecar was written; a `finish` would error).
  Count the sid as `dedup_skipped`, and continue to the next sid. Do NOT dispatch the
  stages.

If `begin` exits non-zero, roll back this sid (Step 8 `finish fail` is not needed —
`begin` already released its own checkpoint on failure), count it as `failed`, report its
stderr, and **continue the loop** (failure-continue):
- "not a wiki root" → the resolved root lost its marker; report and stop the whole run.
- `config-inconsistency:` → the consistency invariant (`apply_fanout_k ≤ max_count`) was
  violated; nothing was locked or written for this sid.
- a lock-held error → another ingest holds `.llmwiki.lock`; report and stop the whole run
  (the driver already rolled back its checkpoint) — do not race the loop against a
  foreign lock.

## Step 5 — Stage1 EXTRACT (no write tool; untrusted read)

Dispatch the `wiki-ingest-extract` subagent (declared in `agents/`) via the Agent tool
with `subagent_type: llm-wiki:wiki-ingest-extract` (the `llm-wiki:` namespace is REQUIRED —
a bare `wiki-ingest-extract` can shadow-resolve to an incompatible user-level agent that
holds no working tools, silently yielding a `tool_uses: 0` extraction). It is the ONLY
place the projected transcript is read, and it has **no write tool** (`tools: Read`) — it
emits proposed edits as a **single JSON object** (its agent-def Step 3 contract), never a write.

Instruct it to **Read the raw artifact at `$WIKI_ROOT/<raw_rel_path>`** — the
`raw_rel_path` from `begin`'s JSON (Step 4). `begin` no longer inlines the body
(E1); the raw was already journaled+written under the transaction,
and Stage1 holds `tools: Read`, so it reads the untrusted projected transcript from that
path itself (E2 — no write tool added). Pass the `doc_type` from `begin`'s JSON. Path B
input is always `origin: fe_b_prime`, so `begin` already pinned `doc_type: transcript` (the
FE-B' code floor). Pass `doc_type=transcript` and instruct the subagent to honor the pinned
type and skip classification.

**Tier (#2 — symmetric with the Stage2 worker in Step 7).** Path B is always
`origin: fe_b_prime` → the **derived tier**. Instruct Stage1 to propose EVERY affected
page's `rel_path` under `wiki/derived/…`. The driver enforces this in code: `plan-fanout`
(Step 6) rejects a proposal whose touched pages are not under `wiki/derived/`, and the
Stage2 write gate (D20) only admits `wiki/derived/` for this origin — a base-tier `wiki/…`
path fails. So proposing the correct tier here is load-bearing, not cosmetic.

Stage1 returns a **single JSON object** — `{"touched": [rel_path, …], "edits": [{rel_path,
op, proposal}, …], "contradictions": […], "doc_type": …}` (its agent-def Step 3 contract; a
prose/Markdown blob makes `plan-fanout` fail `neither a file nor JSON`). Capture that JSON
blob and **Write it ONCE** to the ABSOLUTE path
`stage1_blob_path` from `begin`'s JSON (Step 4) — **use it verbatim; do NOT reconstruct a
temp path yourself** (#1: a hand-built `$OUT_DIR/stage1-…` was mis-resolved against the CWD
on Windows, failing the Read once per sid). Write the JSON exactly as returned; do NOT
reformat or wrap it. Call this path `$STAGE1_BLOB_PATH`. It lives
under `$OUT_DIR` (code placed it there because `begin` was passed `--out_dir="$OUT_DIR"`),
so it rides the existing `project-batch-cleanup` sweep — no new temp mechanism (E2/F3).
From here on the blob is passed **by path only** (never re-inlined into your context): to
`plan-fanout` (Step 6) and to each Stage2 worker (Step 7). It is the only artifact that
crosses into Stage2.

## Step 6 — Decide fan-out (touch-count vs K; D23)

Count the affected pages in the Stage1 proposal and compare to `apply_fanout_k` from
`begin`'s JSON. ALWAYS get the clusters from the driver rather than splitting by hand
(clustering is code, not LLM) — call this even when the touched count is ≤ K (D-COV: a
single-cluster run still needs its ordinal for the C2 dispatch check):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest plan-fanout \
  "$WIKI_ROOT" "$STAGE1_BLOB_PATH"
```

`$STAGE1_BLOB_PATH` is the Stage1 JSON blob file from Step 5 (a path — the driver reads the
touched-page set from the blob's `touched` field; it also still accepts a bare
`[rel_path, ...]` list or inline JSON). The driver reads K from the sidecar, persists
the resulting cluster plan to the sidecar as `planned_clusters` (C2 basis), and prints
`{"clusters": [[rel_path, ...], ...]}`, each cluster ≤ K (a ≤ K touched set yields a single
cluster). Always call it: the 0-based INDEX of each cluster in the returned list is that
cluster's ORDINAL. `apply-finish` (Step 8) proves every cluster was dispatched from that
same `planned_clusters` (C2 cluster-drop guard), and the ORDER you pass the manifests to
`apply-finish` IS the ordinal — so keep the workers and manifests in this cluster order.

## Step 7 — Stage2 APPLY (workers author manifests; you collect them per cluster)

Dispatch the `wiki-ingest-apply` subagent via the Agent tool with
`subagent_type: llm-wiki:wiki-ingest-apply` (the `llm-wiki:` namespace is REQUIRED — a bare
name can shadow-resolve to an incompatible user-level agent), one per cluster on fan-out,
else one. The worker has **no write tool** (`tools: Read`): it authors each page's content and
returns — as its **final response text, and nothing else** — a page manifest, a JSON
array `[{"rel_path": ..., "content": ...}]` (`[]` if there is nothing to write). Its ONLY
input is the Stage1 proposed-edits blob (or one cluster of it) — **never the raw projected
source** (the quarantine seam, D17). You do not author page content yourself; the workers'
manifests are applied by the `apply-finish` verb YOU run in Step 8.

Pass each apply-worker: the **path** `$STAGE1_BLOB_PATH` (Step 5) plus this cluster's
`rel_path` list from `plan-fanout` (Step 6), and the `origin` from `begin`'s JSON
(`fe_b_prime` → derived tier), instructing it to **Read** the JSON blob from that path (it
holds `tools: Read` — no write tool added, E2), author each page from the blob's `edits`
entries whose `rel_path` is in its cluster, and reply with the manifest JSON array only,
restricted to its cluster's `rel_path`s.

For EACH worker's returned manifest, in **plan-fanout cluster order (ordinal 0 first)**,
save it to its own temp file under `$OUT_DIR` (so it rides the `project-batch-cleanup`
sweep — no new temp mechanism, E2/F3), e.g. `$OUT_DIR/manifest-$SOURCE-<ordinal>.json`.
Collect the ordered list of manifest file paths — one per cluster — and carry it to Step 8;
their ORDER is the ordinal `apply-finish` verifies against `planned_clusters` (C2/F2). Do
NOT apply any manifest here — the single `apply-finish` call in Step 8 applies them all.

If a worker errors or fails to return a manifest, this sid failed **before** apply: skip
the `apply-finish` call and roll the sid back via `finish fail` (Step 8's failure path).

## Step 8 — `apply-finish`: apply every manifest + central join + single commit (or `finish fail` on a pre-apply error)

When every cluster's worker returned a manifest (Step 7), run the driver's compound
`apply-finish` verb ONCE for THIS sid, passing one `--manifest` per cluster **in ordinal
order**. The verb reads `planned_clusters` + the budget (`max_count`/`max_bytes`) from the
`.llmwiki.txn` sidecar (you thread no state), maps the origin (`fe_b_prime` → `"derived"`),
verifies F2 (manifest count == planned cluster count, and each manifest's `rel_path`s ⊆ its
planned cluster) BEFORE any write, then stages every manifest through
`write_tool.WriteSession` under the held lock (each write journaled) and — on full success —
performs the central join (index regenerate, log append with the FE-B' prefix, ledger append
of the novel turn-content-hash entries LAST) and the single `commit`, releasing the lock and
deleting the sidecar:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest apply-finish \
  "$WIKI_ROOT" "$ORIGIN" \
  --manifest "$MANIFEST_0" --manifest "$MANIFEST_1" ... \
  ${TITLE:+--title="$TITLE"}
```

`$ORIGIN` is the `fe_b_prime` value from `begin`'s JSON; each `--manifest` is a Step 7
manifest file, listed in cluster-ordinal order (`--manifest` position == ordinal). Do NOT
pass `expected_pages`: `apply-finish` proves every planned cluster ran from
`planned_clusters` (C2). `--title` threads into the log title exactly as the granular
`finish --title` did.

- **Success** (exit 0) → stdout `{"clusters":[{"ordinal":N,"written":[rel_path,...]},...],"committed":true}`
  (the journal was discarded, and the session's novel turns are now owned in the ledger).
  Count `succeeded`; report the one status line (pages written).
- **REJECT** (exit non-zero) → stderr `REJECTED <gate> <reason>` + stdout
  `{"rolled_back":true}`. `apply-finish` has ALREADY rolled this sid back (journal replayed:
  created files incl. orphan raw removed, modified files restored, ledger append reverted)
  and released the lock — so do NOT also call `finish`. Count `failed`, report the gate, and
  continue the loop. NEVER bypass or retry around the code gate. Gates: `budget` (count /
  total-size overflow → the human gate), `manifest_count` / `cluster_pageset` (an F2
  ordinal/page-set mismatch), or `cross_namespace` / `path` / `protected` / `absolute` /
  `traversal` (an illegal target — a derived-origin edit outside `wiki/derived/` (D20), a
  target outside `wiki/`, `SCHEMA.md`/`.llmwiki`/`raw/`, or an absolute/`..` path).

**Pre-apply failure path — `finish fail`.** When a sid failed **before** you could run
`apply-finish` (a `begin` error after the marker check — see Step 4 — a Stage1/Stage2 error,
or a `plan-fanout` budget gate), the transaction is still open, so roll it back explicitly:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest finish \
  "$WIKI_ROOT" fail
```

The driver prints `{"rolled_back": true}` (journal replayed, lock released, sidecar
deleted). Count `failed`, report the rollback, and continue the loop. (The granular
`finish` verb — and its `--expected_pages` flag — remain for this failure path and for
direct/legacy callers; the success/commit path is owned by `apply-finish`.)

This is the single file-journal transaction the invariant promises **for this one
session**: `begin` opened it before the front-end, the lock was held across both LLM
stages and the page writes, and the closing verb performs exactly one of `commit`
(`apply-finish` success) / `rollback` (`apply-finish` REJECT, or `finish fail` on a
pre-apply error) before `release_lock` — all inside the driver, with no transaction state
ever threaded by you. The loop (Step 3) repeats this whole `begin → … → apply-finish` cycle
once per sid, yielding N independent per-session transactions — NOT one transaction spanning
the batch — after which you return to Step 3 for the next sid or emit the final summary.

**Stuck-transaction recovery (symptom → abort).** Symptom of a session's transaction left
**open** — a per-session cycle interrupted before the closing `apply-finish`/`finish` (e.g.
a lightweight model dropped the Stage2 dispatch or skipped the closing verb): a stale
`.llmwiki.lock`, `.llmwiki.txn`, and/or
`.llmwiki.txn.d/` remain in the wiki root while `wiki/` has **no new pages** for that
session. Recovery is the operator running the driver's `abort` verb manually (the
orchestrator does NOT invoke it automatically), which releases the lock and rolls back the
open journal:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest abort "$WIKI_ROOT"
```
