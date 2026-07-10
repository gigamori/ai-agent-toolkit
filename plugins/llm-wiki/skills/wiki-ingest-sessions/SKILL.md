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
(3) loop the SAME per-session `begin → Stage1 → Stage2 → finish` cycle that
`/wiki-ingest` runs — **once per sid, each in its own independent transaction** — with
failure-continue and a final summary. You NEVER author wiki page content yourself —
the Stage2 apply-worker authors it and returns a page manifest; YOU pass that manifest
through the driver's `ingest-apply` verb, where the allowlist write tool
(`write_tool.WriteSession`) gates every page write.

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
> `begin` and the `success|fail` outcome to `finish`). Every byte still passes through
> `write_tool.WriteSession` inside the `ingest-apply` verb YOU run over each worker's
> returned manifest (the verb journals each write); NEITHER LLM stage has a write
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
> Stage2 apply subagent → `finish`). A lightweight or minimal model tends to drop the
> Stage2 apply dispatch, or mistake the raw Stage1 blob for finished pages, or skip the
> `finish` call — any of which leaves that session's transaction **open** (a stale
> `.llmwiki.lock` / `.llmwiki.txn` with no pages written; see the stuck-transaction
> recovery note at the end) and stalls the whole per-session loop. Run it on a capable model.

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
journal), the stages run, and `finish` commits (discards the journal) or rolls back
(replays it) and releases the lock for **that one session**. The transaction is owned entirely by the
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
- A sid that completes Steps 4–8 with a `success` `finish` → count `succeeded`.
- **Failure-continue:** if ANY step for a sid fails (a `begin` error after the marker
  check, a Stage error, a budget gate, or a non-success `finish`), roll back **just that
  sid** by calling its own `finish fail` (Step 8) — never abort the other sessions —
  count it as `failed`, and **continue the loop**. One session's failure must not stop
  the batch (partial success is allowed; F3 makes the shared prefix survive on the next
  sid).

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

From the printed JSON capture: `declaration`, `redacted_body`, `origin` (always
`fe_b_prime` here), `doc_type` (`transcript`), `max_count`, `max_bytes`,
`apply_fanout_k`, `dedup_noop`, `redaction_flags`, and **`ledger_skipped`** (the count of
turns this session dropped because a prior ingest already owns them — add it to the loop's
`ledger_skipped_turns` counter, Step 3 / F6).

Then:

- **Echo every `declaration` line to the user verbatim** before doing anything else (D5).
  If `write_mode` resolved to `implicit`, announce loudly that per-apply confirmation is
  skipped.
- **Surface `redaction_flags`** so the human gate sees what the FE redacted.
- **Accumulate `ledger_skipped`** into `ledger_skipped_turns` (the summary must reflect
  every session's ledger skips, including dedup-no-op sessions).
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
emits proposed edits as text only.

Pass it the `redacted_body` and the `doc_type` from `begin`'s JSON. Path B input is
always `origin: fe_b_prime`, so `begin` already pinned `doc_type: transcript` (the FE-B'
code floor). Pass `doc_type=transcript` and instruct the subagent to honor the pinned
type and skip classification.

Capture its **proposed-edits blob** — the only artifact that crosses into Stage2.

## Step 6 — Decide fan-out (touch-count vs K; D23)

Count the affected pages in the Stage1 proposal and compare to `apply_fanout_k` from
`begin`'s JSON. ALWAYS get the clusters from the driver rather than splitting by hand
(clustering is code, not LLM) — call this even when the touched count is ≤ K (D-COV: a
single-cluster run still needs its ordinal for the C2 dispatch check):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest plan-fanout \
  "$WIKI_ROOT" "$STAGE1_TOUCHED_JSON"
```

`$STAGE1_TOUCHED_JSON` is either a path to a JSON file or inline JSON — either a list of
touched `rel_path`s or `{"touched": [rel_path, ...]}`. The driver reads K from the
sidecar and prints `{"clusters": [[rel_path, ...], ...]}`, each cluster ≤ K (a ≤ K
touched set yields a single cluster). Always call it: the 0-based INDEX of each cluster in
the returned list is that cluster's ORDINAL, which you pass to `ingest-apply` (Step 7) so
`finish` can prove every cluster was dispatched (C2 cluster-drop guard).

## Step 7 — Stage2 APPLY (worker authors; orchestrator runs the allowlist verb)

Dispatch the `wiki-ingest-apply` subagent via the Agent tool with
`subagent_type: llm-wiki:wiki-ingest-apply` (the `llm-wiki:` namespace is REQUIRED — a bare
name can shadow-resolve to an incompatible user-level agent), one per cluster on fan-out,
else one. The worker has **no write tool** (`tools: Read`): it authors each page's content and
returns — as its **final response text, and nothing else** — a page manifest, a JSON
array `[{"rel_path": ..., "content": ...}]` (`[]` if there is nothing to write). Its ONLY
input is the Stage1 proposed-edits blob (or one cluster of it) — **never the raw projected
source** (the quarantine seam, D17). You do not author page content yourself; YOU run the
write verb below over the worker's manifest.

Pass each apply-worker: the proposed-edits blob (or its cluster), the `origin` from
`begin`'s JSON (`fe_b_prime` → derived tier), and the `$WIKI_ROOT`, instructing it that
its reply must be the manifest JSON array only.

For EACH worker's returned manifest: save it to a temporary file (outside the wiki root),
then run the driver's `ingest-apply` verb with that file on STDIN. The verb reads the
budget (`max_count`/`max_bytes`) from the `.llmwiki.txn` sidecar, maps the origin
(`fe_b_prime` → `"derived"`), and stages every page through one
`write_tool.WriteSession`, committing it under the held lock (each write journaled):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki ingest-apply "$WIKI_ROOT" "$ORIGIN" "$CLUSTER_ORDINAL" < "$MANIFEST_FILE"
```

`$ORIGIN` is the `fe_b_prime` value from `begin`'s JSON; `$CLUSTER_ORDINAL` is this
cluster's 0-based index in the plan-fanout `clusters` list (`0` for the single-cluster
case); `$MANIFEST_FILE` is the temporary file holding that worker's manifest JSON array.
Passing the ordinal makes the verb append a dispatch receipt to the sidecar so `finish`
can confirm every planned cluster ran (C2). On success the verb prints
`written: <list>` (the written `rel_path`s) — the per-cluster success signal (a cluster
that printed `written:` was applied and its dispatch receipt recorded in the sidecar).
`finish` confirms completeness from those receipts (C2), so you do NOT pass
`expected_pages`.

On a rejected write the verb prints `REJECTED <gate> <reason>` and exits non-zero. Route
by gate — NEVER bypass or retry around the code gate:

- `budget` — count or total-size budget exceeded → **the human gate**: report the budget
  signal and call `finish` with outcome `fail` (Step 8); do NOT split silently or retry
  around it.
- `cross_namespace` / `path` / `protected` / `absolute` / `traversal` — the target is
  illegal (a derived-origin edit outside `wiki/derived/` (D20), a target outside `wiki/`,
  `SCHEMA.md`/`.llmwiki`/`raw/`, or an absolute/`..` path). Report the rejection and call
  `finish` with outcome `fail` (Step 8); never bypass.

## Step 8 — `finish`: central join, single commit OR rollback, always release

Call the driver's `finish` verb once for THIS sid. The driver reconstructs the lock handle
and checkpoint from the sidecar (you thread no state), confirms every planned cluster was
dispatched (via the sidecar dispatch receipts, C2), regenerates the index, appends the
log (FE-B' prefix), appends the novel
turn-content-hash entries to the ledger LAST inside the same transaction, and then performs
exactly ONE `commit` (success) or `rollback` (fail), releasing the lock and deleting the
sidecar on every path.

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest finish \
  "$WIKI_ROOT" "$OUTCOME" \
  ${TITLE:+--title="$TITLE"}
```

- `$OUTCOME` is `success` when every worker's manifest was applied cleanly (each
  `ingest-apply` verb call printed `written:`, recording its cluster dispatch receipt);
  `fail` on any failure (Stage2 error, a `REJECTED` gate, `dedup_noop` short-circuit from
  Step 4, or anything raised after `begin`). Do NOT pass `expected_pages`: `finish`
  verifies every planned cluster ran from the receipts (C2). (The `--expected_pages` flag
  remains for direct/legacy callers only.)
- `success` → the driver prints `{"committed": true}` (the journal was discarded, and the
  session's novel turns are now owned in the ledger). Count `succeeded`, report success.
- `fail` → the driver prints `{"rolled_back": true}` (journal replayed: created files incl.
  orphan raw removed, modified files restored, ledger append reverted). Count `failed`,
  report the rollback, and continue the loop.

This is the single file-journal transaction the invariant promises **for this one
session**: `begin` opened it before the front-end, the lock was held across both LLM
stages and the page writes, and `finish` performs exactly one of `commit` / `rollback`
before `release_lock` — all inside the driver, with no transaction state ever threaded by
you. The loop (Step 3) repeats this whole `begin → … → finish` cycle once per sid,
yielding N independent per-session transactions — NOT one transaction spanning the
batch — after which you return to Step 3 for the next sid or emit the final summary.

**Stuck-transaction recovery (symptom → abort).** Symptom of a session's transaction left
**open** — a per-session cycle interrupted before `finish` (e.g. a lightweight model dropped
the Stage2 dispatch or skipped `finish`): a stale `.llmwiki.lock`, `.llmwiki.txn`, and/or
`.llmwiki.txn.d/` remain in the wiki root while `wiki/` has **no new pages** for that
session. Recovery is the operator running the driver's `abort` verb manually (the
orchestrator does NOT invoke it automatically), which releases the lock and rolls back the
open journal:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest abort "$WIKI_ROOT"
```
