---
name: wiki-ingest
description: Ingest a 3rd-party source (FE-B) or a cc-log jsonl (FE-B') into the active wiki via the 2-stage extract→apply core. Accepts a single file OR a glob / directory (expanded by the driver, ingested one file per transaction). Explicit write-bearing skill (hook-independent). Usage `/wiki-ingest <path-or-source-or-glob> [doc_type=...] [external=...]`.
disable-model-invocation: true
allowed-tools: Bash(uv run *), Agent, AskUserQuestion, Write
---

# /wiki-ingest

Arguments: `$ARGUMENTS`

You are the ingest **orchestrator**. You do NOT run the deterministic envelope
step-by-step yourself — the `ingest_driver.py` CLI owns it (config resolution, the
single file-journal transaction, the normalization front-end, redaction, dedup, the
central join, index/log). Your job is to call the driver's verbs in order and to
dispatch the two LLM stages to subagents in between. You NEVER author wiki page
content yourself — the Stage2 apply-worker authors it and returns a page manifest;
YOU pass that manifest through the driver's `ingest-apply` verb, where the allowlist
write tool (`write_tool.WriteSession`) gates every page write.

The source argument may be **one file** or a **glob / directory**. A glob / directory
is expanded by the driver into a list of files, and **each file is ingested in its own
independent transaction** — there is NO batch-spanning transaction (see Step 0).

## Step 0 — Dispatch: single file vs glob / directory (deterministic, do NOT guess)

Parse `$ARGUMENTS` into the source token (the path-or-source-or-glob) plus any axis
overrides (`write_mode=...`, `apply_fanout_k=...`, `doc_type=...`, `external=...`).
Apply this **exact, deterministic rule** to the source token — do not infer intent:

> The source token is a **glob / directory** if ANY of the following holds:
> (a) it contains a glob metacharacter — one of `*`, `?`, or `[`; OR
> (b) it ends with a path separator (`/` or `\`); OR
> (c) it has no glob metacharacter and is an existing directory.
> Otherwise it is a **single file**.

(Rule (c) requires checking whether the token is a directory; if you cannot determine
that, treat a metacharacter-free, separator-free token as a single file — the default.
The driver applies the same trailing-separator / directory-only sugar internally, so a
bare `docs` directory token is also handled there.)

- **Single file** → run the per-file ingest cycle in Steps 1–5 **once** on that token,
  then report. (This is the unchanged classic flow.)
- **Glob / directory** → go to Step 0a: call `enumerate`, then loop Steps 1–5 once per
  returned file (G-c: one transaction per file), with failure-continue and a final
  summary (Step 0b).

### Step 0a — `enumerate`: expand the glob in the driver (NOT the shell)

Call the driver's read-only `enumerate` verb. The driver expands the glob in Python
(`pathlib.Path.glob`) — it is **never** the shell that expands it (G-a / R-1). You MUST
pass the glob/dir token **double-quoted** (as in the code block below) so the shell does
not glob-expand or word-split it; the literal pattern reaches the driver intact.

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest enumerate \
  "$WIKI_ROOT" "$GLOB"
```

Here `$GLOB` is the source token from Step 0, passed **quoted** (the double-quotes above
keep the shell from expanding `*`/`?`/`[` and the driver does the expansion). The driver
prints JSON `{"files": [<rel-path>...], "excluded": <count>, "pattern": <effective glob>}`:

- `files` — wiki-root-relative POSIX paths, sorted/deterministic. These are the per-file
  sources for the loop. Force-excluded wiki-internal paths (`raw/`, `wiki/`, `.git`,
  `SCHEMA.md`, `.llmwiki[.lock/.txn]`, `log.md`, `index.md`) are already dropped (G-b),
  and a directory-only token is already restricted to the text-type extension allowlist.
- `excluded` — count of dropped candidates (internal + non-text); carry it for the summary.
- `pattern` — the effective glob actually expanded (directory-only sugar surfaces
  `<dir>/**/*`); echo it to the user so the expansion is visible.

If `enumerate` exits non-zero it means **zero files matched** (incl. "all matches
excluded") — the driver raises this as an explicit error (G-d). Report its stderr and
stop; nothing was locked or written (`enumerate` is read-only: no lock, no checkpoint,
no sidecar).

### Step 0b — loop per file (one independent transaction each) + summary

For **each** `rel_path` in `files`, run the full per-file cycle Steps 1–5 with that
`rel_path` as `$SOURCE`. Each iteration is a **complete, independent transaction**:
`begin` acquires `.llmwiki.lock` and checkpoints (opens the journal), the stages run,
and `finish` commits (discards the journal) or rolls back (replays it) and releases the
lock for **that one file**. The transaction is owned
entirely by the driver via the `.llmwiki.txn` sidecar (the ONE INVARIANT below) — you
thread NO transaction state across files, and you do NOT wrap the loop in a single
spanning transaction. The loop is N independent driver transactions, one per file (G-c).

Maintain four counters across the loop: `total` (= len(files)), `succeeded`, `failed`,
`dedup_skipped`.

- A file whose `begin` reports `dedup_noop: true` (it also returns `auto_closed: true`)
  → the driver already closed that file's transaction (rolled back + released the lock);
  report the no-op only, do NOT call `finish` (there is no sidecar to finish). Count it as
  `dedup_skipped` and continue to the next file (Step 1's dedup branch).
- A file that completes Steps 1–5 with a `success` `finish` → count `succeeded`.
- **Failure-continue (G-f):** if ANY step for a file fails (a `begin` error after the
  marker check, a Stage error, a budget gate, or a non-success `finish`), roll back
  **just that file** by calling its own `finish fail` (Step 5) — never abort the other
  files — count it as `failed`, and **continue the loop**. One file's failure must not
  stop the batch (partial success is allowed).

After the loop, **always** report the summary line verbatim:

> `N total / M succeeded / K failed / S dedup-skipped`

with `N=total`, `M=succeeded`, `K=failed`, `S=dedup_skipped` (and optionally the
`excluded` count from `enumerate`). This summary is mandatory even if every file failed.

The Steps 1–5 below define ONE per-file cycle. In the single-file case you run them once;
in the glob/dir case you run them once per enumerated file. They are identical either way
— the per-file transaction invariant is the same.

## THE ONE UN-DROPPABLE INVARIANT (read first, never bypass)

> The whole ingest is ONE file-journal transaction (git-independent; supersedes
> D21), and that transaction is owned **entirely by `ingest_driver.py`**, not by
> you. `begin` `acquire_lock`s `.llmwiki.lock` THEN checkpoints (opens the
> write-ahead undo journal `.llmwiki.txn.d/`) BEFORE the front-end; `finish`
> performs exactly ONE `commit` (discard the journal, success) or `rollback`
> (replay it, fail) and always `release_lock`s. Between them the transaction state
> lives on disk in the `.llmwiki.txn` sidecar — you NEVER thread the journal dir,
> budget, lock handle, or fe-hash yourself; you pass the driver only the opaque
> `<root>` (plus `<source>` to `begin` and the `success|fail` outcome to `finish`).
> Every byte still passes through `write_tool.WriteSession` inside the
> `ingest-apply` verb YOU run over each worker's returned manifest (the verb
> journals each write); NEITHER LLM stage has a write tool — the Stage2
> apply-worker authors a manifest only (`tools: Read`), and Stage1 — which alone
> reads the untrusted raw source — likewise has **no write tool at all**
> (`tools: Read`). Trust is decided by *location* (`wiki/` vs `wiki/derived/`),
> not by the LLM. No git is invoked anywhere. (driver-plan §2/§3; design
> D17/D19/D20/D23; gitless-journal-transaction.md.)

If any step would write a wiki page outside the Stage2 allowlist tool, or would have
you thread transaction state by hand, STOP and report
`[BLOCKED: write outside transaction/allowlist]`.

> **Model requirement — do not run on a lightweight/minimal model.** This skill is a
> multi-stage orchestration (`begin` → Stage1 extract subagent → Stage2 apply subagent →
> `finish`). A lightweight or minimal model tends to drop the Stage2 apply dispatch, or
> mistake the raw Stage1 blob for finished pages, or skip the `finish` call — any of which
> leaves the transaction **open** (a stale `.llmwiki.lock` / `.llmwiki.txn` with no pages
> written; see the stuck-transaction recovery note at the end). Run it on a capable model.

### Resolve `WIKI_ROOT` (multi-scope; do NOT hardcode the CWD)

The wiki root is **resolved**, not assumed to be the CWD. Resolve it via
`wiki_root_resolver` (scopes: prompt>pj>workspace>cwd), honoring an explicit
`--root <path>` from `$ARGUMENTS` as the top override (Q4). Parse `--root <path>`
out of `$ARGUMENTS` first (it is NOT a `key=value` axis — strip it before the
Step 0 source/axis parse); pass it as `prompt_root`, else pass nothing:

Also capture the running session's own id as `SID` via the `${CLAUDE_SESSION_ID}`
skill-template substitution (the harness replaces this placeholder with the literal
session id before you see this text — it is NOT an OS env var) and thread it as `--sid`
so the resolver's session-aware pj fast-path (`_projects/_state/<sid>.json` read first,
D6) fires instead of degrading to a mtime-latest scan that can cross-talk between
concurrent sessions on different projects:

```bash
SID="${CLAUDE_SESSION_ID}"
RESOLVED="$(uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki resolve-root ${ROOT_OVERRIDE:+--root "$ROOT_OVERRIDE"} --sid "$SID")" \
  || { echo "resolve-root failed (NO-WIKI or resolver error) — stop"; }
IFS=$'\t' read -r WIKI_ROOT WIKI_SCOPE <<<"$RESOLVED"
```

The `resolve-root` verb prints `<root>\t<scope>` on stdout; the block above splits it
(`WIKI_ROOT`=root, `WIKI_SCOPE`=scope) so a stray tab never contaminates `$WIKI_ROOT`. If it exits non-zero (`NO-WIKI`), no wiki resolved —
report that this skill requires an active wiki (pass `--root <path>` or run
from a wiki root) and STOP. **Before acting, show the user the resolved root and
scope** (e.g. `active wiki: <root> (scope: pj|workspace|cwd|prompt)`). The driver
still enforces the marker and errors with "not a wiki root" if absent.

## Step 1 — `begin`: open the transaction, normalize, declare (one driver call)

Call the driver's `begin` verb once for THIS file. It detects the marker,
resolves+declares every config axis (D5), validates the consistency invariant,
acquires the lock then checkpoints (opens the journal) BEFORE the front-end, runs the matching
front-end (FE-B for a 3rd-party source file/text; FE-B' for a cc-log jsonl, which it
**projects** fork-aware from the `session_id` — via `cc_log_project.project_owned` over the
vendored DuckDB views, deduping turns by content-hash against the turn ledger — then pins
`doc_type:transcript`), runs redaction + content-hash dedup, writes
the raw artifact (unless a dedup no-op), writes the `.llmwiki.txn` sidecar, and prints
the contract JSON on stdout. `$SOURCE` is this cycle's source — the single-file token in
the single-file case, or one `rel_path` from the `enumerate` `files` list in the
glob/dir loop (Step 0b). The axis overrides (`write_mode=...`, `apply_fanout_k=...`,
`doc_type=...`, `external=...`) were parsed from `$ARGUMENTS` in Step 0 and apply
identically to every file in the loop.

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest begin \
  "$WIKI_ROOT" "$SOURCE" \
  ${KIND:+--kind="$KIND"} \
  ${DOC_TYPE:+--doc_type="$DOC_TYPE"} \
  ${EXTERNAL:+--external="$EXTERNAL"} \
  ${WRITE_MODE:+--write_mode="$WRITE_MODE"} \
  ${APPLY_FANOUT_K:+--apply_fanout_k="$APPLY_FANOUT_K"}
```

(Use `--kind=fe_b_prime` for a cc-log jsonl transcript; omit `--kind` / `--kind=auto`
for a 3rd-party source. The driver also echoes the resolved-value declaration to
stderr.)

From the printed JSON capture: `declaration` (list of `[wiki] <axis> = <value>
(<source>)` lines), `redacted_body`, `origin` (`fe_b`|`fe_b_prime`), `doc_type`,
`max_count`, `max_bytes`, `apply_fanout_k`, `dedup_noop`, `redaction_flags`.

Then:

- **Echo every `declaration` line to the user verbatim** before doing anything else
  (D5 — the resolved-value announcement precedes all stages). If `write_mode`
  resolved to `implicit`, announce loudly that per-apply confirmation is skipped.
- **Surface `redaction_flags`** to the user so the human gate sees what the FE
  redacted.
- **If `dedup_noop` is `true`:** report "already ingested (content-hash dedup
  no-op)" and STOP. `begin` also returned `auto_closed: true` — it already rolled
  back and released the lock itself, so do NOT call `finish` (no sidecar was
  written; a `finish` would error). Do NOT dispatch the stages.

If `begin` exits non-zero, report its stderr and stop:
- "not a wiki root" → this skill requires an active wiki.
- `config-inconsistency:` → the consistency invariant (`apply_fanout_k ≤ max_count`)
  was violated; nothing was locked or written. Report and stop.
- a lock-held error → another ingest holds `.llmwiki.lock`; report and stop (the
  driver already rolled back its checkpoint).

## Step 2 — Stage1 EXTRACT (no write tool; untrusted read)

Dispatch the `wiki-ingest-extract` subagent (declared in `agents/`) via the Agent
tool with `subagent_type: llm-wiki:wiki-ingest-extract` (the `llm-wiki:` namespace is
REQUIRED — a bare `wiki-ingest-extract` can shadow-resolve to an incompatible user-level
agent that holds no working tools, silently yielding a `tool_uses: 0` extraction). It is
the ONLY place the untrusted raw body is read, and it has **no write tool** (`tools:
Read`) — it emits proposed edits as text only.

Pass it the `redacted_body` and the `doc_type` from `begin`'s JSON:

- For FE-B input (`origin: fe_b`): pass `redacted_body` + the `doc_type` hint; the
  subagent classifies `doc_type` (unmatched → `default`).
- For FE-B' input (`origin: fe_b_prime`): `begin` already pinned
  `doc_type: transcript` (the FE-B' code floor). Pass `doc_type=transcript` and
  instruct the subagent to honor the pinned type and skip classification.

Capture its **proposed-edits blob** — the only artifact that crosses into Stage2.

## Step 3 — Decide fan-out (touch-count vs K; D23)

Count the affected pages in the Stage1 proposal and compare to `apply_fanout_k` from
`begin`'s JSON. ALWAYS get the clusters from the driver rather than splitting by hand
(F6 — clustering is code, not LLM) — call this even when the touched count is ≤ K (D-COV:
a single-cluster run still needs its ordinal for the C2 dispatch check):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest plan-fanout \
  "$WIKI_ROOT" "$STAGE1_TOUCHED_JSON"
```

`$STAGE1_TOUCHED_JSON` is either a path to a JSON file or inline JSON — either a list
of touched `rel_path`s or `{"touched": [rel_path, ...]}`. The driver reads K from the
sidecar and prints `{"clusters": [[rel_path, ...], ...]}`, each cluster ≤ K (a ≤ K
touched set yields a single cluster). Always call it: the 0-based INDEX of each cluster in
the returned list is that cluster's ORDINAL, which you pass to `ingest-apply` (Step 4) so
`finish` can prove every cluster was dispatched (C2 cluster-drop guard).

## Step 4 — Stage2 APPLY (worker authors; orchestrator runs the allowlist verb)

Dispatch the `wiki-ingest-apply` subagent via the Agent tool with
`subagent_type: llm-wiki:wiki-ingest-apply` (the `llm-wiki:` namespace is REQUIRED — a bare
name can shadow-resolve to an incompatible user-level agent), one per cluster on fan-out,
else one. The worker has **no write tool** (`tools: Read`): it authors each page's content and
returns — as its **final response text, and nothing else** — a page manifest, a JSON
array `[{"rel_path": ..., "content": ...}]` (`[]` if there is nothing to write). Its
ONLY input is the Stage1 proposed-edits blob (or one cluster of it) — **never the
raw untrusted source** (the quarantine seam, D17). You do not author page content
yourself; YOU run the write verb below over the worker's manifest.

Pass each apply-worker: the proposed-edits blob (or its cluster), the `origin` from
`begin`'s JSON (`fe_b` → source tier, `fe_b_prime` → derived tier), and the
`$WIKI_ROOT`, instructing it that its reply must be the manifest JSON array only.

For EACH worker's returned manifest: save it to a temporary file (outside the wiki
root), then run the driver's `ingest-apply` verb with that file on STDIN. The verb
reads the budget (`max_count`/`max_bytes`) from the `.llmwiki.txn` sidecar, maps the
origin (`fe_b` → `"source"`, `fe_b_prime` → `"derived"`), and stages every page
through one `write_tool.WriteSession`, committing it under the held lock (each write
journaled):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki ingest-apply "$WIKI_ROOT" "$ORIGIN" "$CLUSTER_ORDINAL" < "$MANIFEST_FILE"
```

`$ORIGIN` is the `fe_b`/`fe_b_prime` value from `begin`'s JSON; `$CLUSTER_ORDINAL` is
this cluster's 0-based index in the plan-fanout `clusters` list (`0` for the
single-cluster case); `$MANIFEST_FILE` is the temporary file holding that worker's
manifest JSON array. Passing the ordinal makes the verb append a dispatch receipt to the
sidecar so `finish` can confirm every planned cluster ran (C2). On success the verb
prints `written: <list>` (the written `rel_path`s) — the per-cluster success signal (a
cluster that printed `written:` was applied and its dispatch receipt recorded in the
sidecar). `finish` confirms completeness from those receipts (C2), so you do NOT pass
`expected_pages`.

On a rejected write the verb prints `REJECTED <gate> <reason>` and exits non-zero.
Route by gate — NEVER bypass or retry around the code gate:

- `budget` — count or total-size budget exceeded → **the human gate**: report the
  budget signal and call `finish` with outcome `fail` (Step 5); do NOT split
  silently or retry around it.
- `cross_namespace` / `path` / `protected` / `absolute` / `traversal` — the target
  is illegal (a derived-origin edit outside `wiki/derived/` (D20), a target outside
  `wiki/`, `SCHEMA.md`/`.llmwiki`/`raw/`, or an absolute/`..` path). Report the
  rejection and call `finish` with outcome `fail` (Step 5); never bypass.

## Step 5 — `finish`: central join, single commit OR rollback, always release

Call the driver's `finish` verb once. The driver reconstructs the lock handle and
checkpoint from the sidecar (you thread no state), confirms every planned cluster was
dispatched (via the sidecar dispatch receipts, C2), regenerates the index, appends the
log (FE-dispatched prefix), and then performs
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
  Step 1, or anything raised after `begin`). Do NOT pass `expected_pages`: `finish`
  verifies every planned cluster ran from the receipts (C2). (The `--expected_pages` flag
  remains for direct/legacy callers only.)
- `success` → the driver prints `{"committed": true}` (the journal was discarded).
  Report success to the user.
- `fail` → the driver prints `{"rolled_back": true}` (journal replayed: created
  files incl. orphan raw removed, modified files restored). Report the rollback.

This is the single file-journal transaction the invariant promises **for this one file**:
`begin` opened it before the front-end, the lock was held across both LLM stages and the
page writes, and `finish` performs exactly one of `commit` / `rollback` before
`release_lock` — all inside the driver, with no transaction state ever threaded by you.
In the glob/dir case the loop (Step 0b) repeats this whole `begin → … → finish` cycle
once per enumerated file, yielding N independent per-file transactions — NOT one
transaction spanning the batch — after which you return to Step 0b for the next file or
emit the final summary.

**Stuck-transaction recovery (symptom → abort).** Symptom of a transaction left
**open** — a run interrupted before `finish` (e.g. a lightweight model dropped the
Stage2 dispatch or skipped `finish`): a stale `.llmwiki.lock`, `.llmwiki.txn`, and/or
`.llmwiki.txn.d/` remain in the wiki root while `wiki/` has **no new pages**. Recovery is
the operator running the driver's `abort` verb manually (the orchestrator does NOT invoke
it automatically), which releases the lock and rolls back the open journal:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest abort "$WIKI_ROOT"
```
