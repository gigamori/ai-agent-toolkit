---
name: wiki-ingest
description: Ingest a 3rd-party source (FE-B) or a cc-log jsonl (FE-B') into the active wiki via the 2-stage extract→apply core. Accepts a single file OR a glob / directory (expanded by the driver, ingested one file per transaction). Explicit write-bearing skill (hook-independent). Usage `/wiki-ingest <path-or-source-or-glob> [doc_type=...] [external=...]`.
disable-model-invocation: true
allowed-tools: Bash(uv run --script *), Agent, AskUserQuestion, Write
---

# /wiki-ingest

Arguments: `$ARGUMENTS`

You are the ingest orchestrator. You do not run the deterministic envelope
step-by-step yourself — the `ingest_driver.py` CLI owns it (config resolution, the
single file-journal transaction, the normalization front-end, redaction, dedup, the
central join, index/log). Your job is to call the driver's verbs in order and to
dispatch the two LLM stages to subagents in between. You do not author wiki page
content yourself — the Stage2 apply-worker authors it and returns a page manifest;
you pass every cluster's manifest through the driver's compound `apply-finish` verb
(E3), where the allowlist write tool (`write_tool.WriteSession`) gates every page write
and, on success, the same verb performs the central join + single commit.

The source argument may be one file or a glob / directory. A glob / directory
is expanded by the driver into a list of files, and each file is ingested in its own
independent transaction — there is no batch-spanning transaction (see Step 0).

## THE ONE UN-DROPPABLE INVARIANT (read first, never bypass)

> The whole ingest is ONE file-journal transaction (git-independent; supersedes
> D21), and that transaction is owned **entirely by `ingest_driver.py`**, not by
> you. `begin` `acquire_lock`s `.llmwiki.lock` THEN checkpoints (opens the
> write-ahead undo journal `.llmwiki.txn.d/`) BEFORE the front-end; `finish`
> performs exactly ONE `commit` (discard the journal, success) or `rollback`
> (replay it, fail) and always `release_lock`s. Between them the transaction state
> lives on disk in the `.llmwiki.txn` sidecar — you NEVER thread the journal dir,
> budget, lock handle, or fe-hash yourself; you pass the driver only the opaque
> `<root>` (plus `<source>` to `begin`, the per-cluster manifests to `apply-finish`,
> and — only on a pre-apply stage failure — a `fail` outcome to `finish`).
> Every byte still passes through `write_tool.WriteSession` inside the
> `apply-finish` verb YOU run over the workers' returned manifests (the verb
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
> `apply-finish`). A lightweight or minimal model tends to drop the Stage2 apply dispatch, or
> mistake the raw Stage1 blob for finished pages, or skip the `apply-finish` call — any of which
> leaves the transaction **open** (a stale `.llmwiki.lock` / `.llmwiki.txn` with no pages
> written; see the stuck-transaction recovery note at the end). Run it on a capable model.

> **Execution discipline — run the cycle yourself, one driver call at a time.**
>
> - **NEVER delegate the cycle.** YOU execute the per-file cycle directly; do NOT hand it to
>   a general-purpose subagent. The ONLY Agent-tool dispatches are the two declared stage
>   workers (`llm-wiki:wiki-ingest-extract`, `llm-wiki:wiki-ingest-apply`).
> - **NEVER script the cycle.** No bash/python/PowerShell batch scripts wrapping or looping
>   the driver verbs (in a glob/dir loop, one file's `begin` → stages → `apply-finish`
>   completes before the next file's `begin` — the `.llmwiki.lock` depends on it). One driver
>   verb = one Bash invocation.
> - **NEVER parse driver stdout with tools.** No `jq` / `ConvertFrom-Json` / improvised
>   parsers — deterministic extraction is code-owned by the driver verbs and the stdout JSON
>   is deliberately small (E1); read the fields you need directly.
> - **NEVER hand-clear a stuck transaction.** Recover a residual `.llmwiki.lock` /
>   `.llmwiki.txn.d` ONLY with `ingest abort`, never `rm -f`. On a lock-held `begin` error,
>   STOP and run `ingest abort` first.

## Step 0 — Dispatch: single file vs glob / directory (deterministic, do not guess)

Parse `$ARGUMENTS` into the source token (the path-or-source-or-glob) plus any axis
overrides (`write_mode=...`, `apply_fanout_k=...`, `doc_type=...`, `external=...`).
Apply this exact, deterministic rule to the source token — do not infer intent:

> The source token is a glob / directory if any of the following holds:
> (a) it contains a glob metacharacter — one of `*`, `?`, or `[`; OR
> (b) it ends with a path separator (`/` or `\`); OR
> (c) it has no glob metacharacter and is an existing directory.
> Otherwise it is a single file.

(Rule (c) requires checking whether the token is a directory; if you cannot determine
that, treat a metacharacter-free, separator-free token as a single file — the default.
The driver applies the same trailing-separator / directory-only sugar internally, so a
bare `docs` directory token is also handled there.)

- **Single file** → run the per-file ingest cycle in Steps 1–5 once on that token,
  then report. (This is the unchanged classic flow.)
- **Glob / directory** → go to Step 0a: call `enumerate`, then loop Steps 1–5 once per
  returned file (G-c: one transaction per file), with failure-continue and a final
  summary (Step 0b).

### Step 0a — `enumerate`: expand the glob in the driver, not the shell

Call the driver's read-only `enumerate` verb. The driver expands the glob in Python
(`pathlib.Path.glob`) — it is never the shell that expands it (G-a / R-1). You must
pass the glob/dir token double-quoted (as in the code block below) so the shell does
not glob-expand or word-split it; the literal pattern reaches the driver intact.

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest enumerate \
  "$WIKI_ROOT" "$GLOB"
```

Here `$GLOB` is the source token from Step 0, passed quoted (the double-quotes above
keep the shell from expanding `*`/`?`/`[` and the driver does the expansion). The driver
prints JSON `{"files": [<rel-path>...], "excluded": <count>, "pattern": <effective glob>}`:

- `files` — wiki-root-relative POSIX paths, sorted/deterministic. These are the per-file
  sources for the loop. Force-excluded wiki-internal paths (`raw/`, `wiki/`, `.git`,
  `SCHEMA.md`, `.llmwiki[.lock/.txn]`, `log.md`, `index.md`) are already dropped (G-b),
  and a directory-only token is already restricted to the text-type extension allowlist.
- `excluded` — count of dropped candidates (internal + non-text); carry it for the summary.
- `pattern` — the effective glob actually expanded (directory-only sugar surfaces
  `<dir>/**/*`); echo it to the user so the expansion is visible.

If `enumerate` exits non-zero it means zero files matched (incl. "all matches
excluded") — the driver raises this as an explicit error (G-d). Report its stderr and
stop; nothing was locked or written (`enumerate` is read-only: no lock, no checkpoint,
no sidecar).

### Step 0b — loop per file (one independent transaction each) + summary

For each `rel_path` in `files`, run the full per-file cycle Steps 1–5 with that
`rel_path` as `$SOURCE`. Each iteration is a complete, independent transaction:
`begin` acquires `.llmwiki.lock` and checkpoints (opens the journal), the stages run,
and `apply-finish` commits (discards the journal) on success — or, when a stage fails
before apply, `finish fail` rolls back (replays it) — releasing the lock for that one
file. The transaction is owned
entirely by the driver via the `.llmwiki.txn` sidecar (the one invariant above) — you
thread no transaction state across files, and you do not wrap the loop in a single
spanning transaction. The loop is N independent driver transactions, one per file (G-c).

Maintain four counters across the loop: `total` (= len(files)), `succeeded`, `failed`,
`dedup_skipped`.

- A file whose `begin` reports `dedup_noop: true` (it also returns `auto_closed: true`)
  → the driver already closed that file's transaction (rolled back + released the lock);
  report the no-op only, do not call `finish` (there is no sidecar to finish). Count it as
  `dedup_skipped` and continue to the next file (Step 1's dedup branch).
- A file that completes Steps 1–5 with a committed `apply-finish` → count `succeeded`.
- **Failure-continue (G-f):** if any step for a file fails, roll back just that file —
  never abort the other files — count it as `failed`, and continue the loop. Route by
  where it failed (Step 5): a `begin` error → `begin` already rolled back (replayed the
  checkpoint) and released the lock itself, so do not call `finish` — report its stderr
  only; a Stage1/Stage2 error, or a `plan-fanout` budget gate (a failure before apply)
  → call its own `finish fail` (Step 5); an `apply-finish` REJECT (a write gate or an
  F2 ordinal/page-set mismatch) → `apply-finish` has already rolled that file back, so do
  not also call `finish`. One file's failure must not stop the batch (partial success is
  allowed).

After the loop, always report the summary line verbatim:

> `N total / M succeeded / K failed / S dedup-skipped`

with `N=total`, `M=succeeded`, `K=failed`, `S=dedup_skipped` (and optionally the
`excluded` count from `enumerate`). This summary is mandatory even if every file failed.

The Steps 1–5 below define one per-file cycle. In the single-file case you run them once;
in the glob/dir case you run them once per enumerated file. They are identical either way
— the per-file transaction invariant is the same.

### Resolve `WIKI_ROOT` (multi-scope; do not hardcode the CWD)

The wiki root is resolved, not assumed to be the CWD. Resolve it via
`wiki_root_resolver` (scopes: prompt>pj>workspace>cwd), honoring an explicit
`--root <path>` from `$ARGUMENTS` as the top override (Q4). Parse `--root <path>`
out of `$ARGUMENTS` first (it is not a `key=value` axis — strip it before the
Step 0 source/axis parse); pass it as `prompt_root`, else pass nothing:

Also capture the running session's own id as `SID` via the `${CLAUDE_SESSION_ID}`
skill-template substitution (the harness replaces this placeholder with the literal
session id before you see this text — it is not an OS env var) and thread it as `--sid`
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
from a wiki root) and STOP. Before acting, show the user the resolved root and
scope (e.g. `active wiki: <root> (scope: pj|workspace|cwd|prompt)`). The driver
still enforces the marker and errors with "not a wiki root" if absent.

## Step 1 — `begin`: open the transaction, normalize, declare (one driver call)

Call the driver's `begin` verb once for this file. It detects the marker,
resolves+declares every config axis (D5), validates the consistency invariant,
acquires the lock then checkpoints (opens the journal) before the front-end, runs the matching
front-end (FE-B for a 3rd-party source file/text; FE-B' for a cc-log jsonl, which it
projects fork-aware from the `session_id` — via `cc_log_project.project_owned` over the
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
for a 3rd-party source. An explicit `--kind=fe_b` ingests a plain `.jsonl` DATA file as
text, bypassing the session-log refusal `--kind=auto` applies to `.jsonl` sources. The
driver also echoes the resolved-value declaration to stderr.)

From the printed JSON capture: `declaration` (list of `[wiki] <axis> = <value>
(<source>)` lines), `declaration_hash` (E1/E4 — the code-side short hash used for the
per-file declaration-echo mitigation below), `raw_rel_path` (E1/E2 — the wiki-relative path
of the raw artifact `begin` wrote; `begin` no longer inlines the body, so Stage1 Reads the
raw from this path in Step 2), `stage1_blob_path` (#1 — the absolute path to Write the
Stage1 blob to in Step 2; code-authored under the system temp dir, so use it verbatim and
never reconstruct a temp path yourself), `origin` (`fe_b`|`fe_b_prime`), `doc_type`,
`max_count`, `max_bytes`, `apply_fanout_k`, `dedup_noop`, `redaction_flags`.

Then:

- **Declaration echo — per-run mitigation (E4 / D-2 / F4).** For the first ingest in
  this run (the single file, or the first file of a glob/dir loop), echo every `declaration`
  line verbatim (D5 — the resolved-value announcement precedes all stages) and remember its
  `declaration_hash`. For every later file in a loop, compare its `declaration_hash` to
  the first's — an equality check on the code-side hash only (never re-derive or diff the
  declaration text yourself, F4): if it is equal, emit the single line
  `declaration unchanged (= file 1)`; if it differs, echo that file's full `declaration`
  plus a warning that the resolved config changed. If `write_mode` resolved to `implicit`,
  announce loudly that per-apply confirmation is skipped.
- **Surface `redaction_flags`** to the user so the human gate sees what the FE
  redacted.
- **If `dedup_noop` is `true`:** report "already ingested (content-hash dedup
  no-op)" and STOP. `begin` also returned `auto_closed: true` — it already rolled
  back and released the lock itself, so do not call `finish` (no sidecar was
  written; a `finish` would error). Do not dispatch the stages.

If `begin` exits non-zero, report its stderr and stop:
- "not a wiki root" → this skill requires an active wiki.
- `config-inconsistency:` → the consistency invariant (`apply_fanout_k ≤ max_count`)
  was violated; nothing was locked or written. Report and stop.
- a lock-held error → another ingest holds `.llmwiki.lock`; report and stop (the
  driver already rolled back its checkpoint).

## Step 2 — Stage1 EXTRACT (no write tool; untrusted read)

Dispatch the `wiki-ingest-extract` subagent (declared in `agents/`) via the Agent
tool with `subagent_type: llm-wiki:wiki-ingest-extract` (the `llm-wiki:` namespace is
**required** — a bare `wiki-ingest-extract` can shadow-resolve to an incompatible user-level
agent that holds no working tools, silently yielding a `tool_uses: 0` extraction). It is
the only place the untrusted raw body is read, and it has **no write tool** (`tools:
Read`) — it emits proposed edits as a single JSON object (its agent-def Step 3 contract), never a write.

Instruct it to Read the raw artifact at `$WIKI_ROOT/<raw_rel_path>` — the
`raw_rel_path` from `begin`'s JSON (Step 1). `begin` no longer inlines the body (E1); the
raw was already journaled+written under the transaction, and Stage1 holds `tools: Read`, so
it reads the untrusted raw body from that path itself (E2 — no write tool added). Pass the
`doc_type` from `begin`'s JSON:

- For FE-B input (`origin: fe_b`): the raw at `raw_rel_path` is the redacted source body;
  pass the `doc_type` hint; the subagent classifies `doc_type` (unmatched → `default`).
- For FE-B' input (`origin: fe_b_prime`): the raw at `raw_rel_path` is the projected
  transcript, and `begin` already pinned `doc_type: transcript` (the FE-B' code floor). Pass
  `doc_type=transcript` and instruct the subagent to honor the pinned type and skip
  classification.

**Tier (#2 — symmetric with the Stage2 worker in Step 4).** The output tier is a
deterministic function of `origin`: instruct Stage1 to propose every affected page's
`rel_path` under `wiki/derived/…` for `origin: fe_b_prime` (derived tier), or under `wiki/…`
(non-derived) for `origin: fe_b` (source tier). The driver enforces the derived case in
code: for `fe_b_prime`, `plan-fanout` (Step 3) rejects a proposal whose touched pages are
not under `wiki/derived/`, and the Stage2 write gate (D20) admits only `wiki/derived/`. So
proposing the correct tier here is load-bearing, not cosmetic.

Stage1 returns a single JSON object — `{"touched": [rel_path, …], "edits": [{rel_path,
op, proposal}, …], "contradictions": […], "doc_type": …}` (its agent-def Step 3 contract; a
prose/Markdown blob makes `plan-fanout` fail `neither a file nor JSON`). Capture that JSON
blob and **Write it once** to the absolute path
`stage1_blob_path` from `begin`'s JSON (Step 1) — **use it verbatim; do not reconstruct a
temp path yourself** (#1: a hand-built `$TMPDIR/stage1-…` can be mis-resolved against the
CWD). Write the JSON exactly as returned; do not reformat or wrap it. Call this path
`$STAGE1_BLOB_PATH`; it is outside the wiki root (the system temp dir).
From here on the blob is passed by path only (never re-inlined into your context): to
`plan-fanout` (Step 3) and to each Stage2 worker (Step 4). It is the only artifact that
crosses into Stage2.

## Step 3 — Decide fan-out (touch-count vs K; D23)

Count the affected pages in the Stage1 proposal and compare to `apply_fanout_k` from
`begin`'s JSON. Always get the clusters from the driver rather than splitting by hand
(F6 — clustering is code, not LLM) — call this even when the touched count is ≤ K (D-COV:
a single-cluster run still needs its ordinal for the C2 dispatch check):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest plan-fanout \
  "$WIKI_ROOT" "$STAGE1_BLOB_PATH"
```

`$STAGE1_BLOB_PATH` is the Stage1 JSON blob file from Step 2 (a path — the driver reads the
touched-page set from the blob's `touched` field; it also still accepts a bare
`[rel_path, ...]` list or inline JSON). The driver reads K from the sidecar, persists
the resulting cluster plan to the sidecar as `planned_clusters` (C2 basis), and prints
`{"clusters": [[rel_path, ...], ...], "manifest_paths": [<absolute path>, ...]}`, each
cluster ≤ K (a ≤ K touched set yields a single cluster). `manifest_paths[i]` is the
code-authored absolute path to save cluster `i`'s manifest to (Step 4) — one per ordinal,
aligned to `clusters`. Always call it: the 0-based index of each cluster in the returned
list is that cluster's ordinal. `apply-finish` (Step 5) proves every cluster was dispatched
from that same `planned_clusters` (C2 cluster-drop guard), and the order you pass the
manifests to `apply-finish` is the ordinal — so keep the workers and manifests in this
cluster order.

## Step 4 — Stage2 APPLY (workers author manifests; you collect them per cluster)

Dispatch the `wiki-ingest-apply` subagent via the Agent tool with
`subagent_type: llm-wiki:wiki-ingest-apply` (the `llm-wiki:` namespace is **required** — a bare
name can shadow-resolve to an incompatible user-level agent), one per cluster on fan-out,
else one. The worker has **no write tool** (`tools: Read`): it authors each page's content and
returns — as its **final response text, and nothing else** — a page manifest, a JSON
array `[{"rel_path": ..., "content": ...}]` (`[]` if there is nothing to write). Its
only input is the Stage1 proposed-edits blob (or one cluster of it) — **never the
raw untrusted source** (the quarantine seam, D17). You do not author page content
yourself; the workers' manifests are applied by the `apply-finish` verb you run in Step 5.

Pass each apply-worker: the path `$STAGE1_BLOB_PATH` (Step 2) plus this cluster's
`rel_path` list from `plan-fanout` (Step 3), and the `origin` from `begin`'s JSON
(`fe_b` → source tier, `fe_b_prime` → derived tier), instructing it to Read the JSON blob
from that path (it holds `tools: Read` — no write tool added, E2), author each page from the
blob's `edits` entries whose `rel_path` is in its cluster, and reply with the manifest JSON
array only, restricted to its cluster's `rel_path`s.

For each worker's returned manifest, in plan-fanout cluster order (ordinal 0 first),
save each worker's manifest to `manifest_paths[ordinal]` verbatim (the code-authored path
from `plan-fanout`'s stdout — do not construct a temp path yourself). Collect the ordered
list of manifest file paths — one per cluster — and carry it to Step 5; their order is the
ordinal `apply-finish` verifies against `planned_clusters` (C2/F2). Do not apply any
manifest here — the single `apply-finish` call in Step 5 applies them all.

If a worker errors or fails to return a manifest, this file failed before apply: skip
the `apply-finish` call and roll it back via `finish fail` (Step 5's failure path).

## Step 5 — `apply-finish`: apply every manifest + central join + single commit (or `finish fail` on a pre-apply error)

When every cluster's worker returned a manifest (Step 4), run the driver's compound
`apply-finish` verb once for this file, passing one `--manifest` per cluster in ordinal
order. The verb reads `planned_clusters` + the budget (`max_count`/`max_bytes`) from the
`.llmwiki.txn` sidecar (you thread no state), maps the origin (`fe_b` → `"source"`,
`fe_b_prime` → `"derived"`), verifies F2 (manifest count == planned cluster count, and each
manifest's `rel_path`s ⊆ its planned cluster) before any write, then stages every manifest
through `write_tool.WriteSession` under the held lock (each write journaled) and — on full
success — performs the central join (index regenerate, log append with the FE-dispatched
prefix) and the single `commit`, releasing the lock and deleting the sidecar:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest apply-finish \
  "$WIKI_ROOT" "$ORIGIN" \
  --manifest "$MANIFEST_0" --manifest "$MANIFEST_1" ... \
  ${TITLE:+--title="$TITLE"}
```

`$ORIGIN` is the `fe_b`/`fe_b_prime` value from `begin`'s JSON; each `--manifest` is a
Step 4 manifest file, listed in cluster-ordinal order (`--manifest` position == ordinal). Do
not pass `expected_pages`: `apply-finish` proves every planned cluster ran from
`planned_clusters` (C2). `--title` threads into the log title exactly as the granular
`finish --title` did.

- **Success** (exit 0) → stdout `{"clusters":[{"ordinal":N,"written":[rel_path,...]},...],"committed":true}`
  (the journal was discarded). Report success to the user.
- **REJECT** (exit non-zero) → stderr `REJECTED <gate> <reason>` + stdout
  `{"rolled_back":true}`. `apply-finish` has ALREADY rolled this file back (journal replayed:
  created files incl. orphan raw removed, modified files restored) and released the lock — so
  do NOT also call `finish`. Report the gate. NEVER bypass or retry around the code gate.
  Gates: `budget` (count / total-size overflow → the human gate), `manifest_count` /
  `cluster_pageset` (an F2 ordinal/page-set mismatch), or `cross_namespace` / `path` /
  `protected` / `absolute` / `traversal` (an illegal target — a derived-origin edit outside
  `wiki/derived/` (D20), a target outside `wiki/`, `SCHEMA.md`/`.llmwiki`/`raw/`, or an
  absolute/`..` path).

**Pre-apply failure path — `finish fail`.** When a file failed before you could run
`apply-finish` (a Stage1/Stage2 error, or a `plan-fanout` budget gate), the transaction is
still open, so roll it back explicitly. (A `begin` error is NOT routed here — see Step 1:
`begin` already rolled back and released the lock itself on failure, so do NOT call `finish`
for a `begin` error.)

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest finish \
  "$WIKI_ROOT" fail
```

The driver prints `{"rolled_back": true}` (journal replayed, lock released, sidecar
deleted). Report the rollback. (The granular `finish` verb — and its `--expected_pages`
flag — remain for this failure path and for direct/legacy callers; the success/commit path
is owned by `apply-finish`.)

This is the single file-journal transaction the invariant promises for this one file:
`begin` opened it before the front-end, the lock was held across both LLM stages and the
page writes, and the closing verb performs exactly one of `commit` (`apply-finish` success)
/ `rollback` (`apply-finish` REJECT, or `finish fail` on a pre-apply error) before
`release_lock` — all inside the driver, with no transaction state ever threaded by you.
In the glob/dir case the loop (Step 0b) repeats this whole `begin → … → apply-finish` cycle
once per enumerated file, yielding N independent per-file transactions — not one
transaction spanning the batch — after which you return to Step 0b for the next file or
emit the final summary.

**Stuck-transaction recovery (symptom → abort).** Symptom of a transaction left
open — a run interrupted before the closing `apply-finish`/`finish` (e.g. a lightweight
model dropped the Stage2 dispatch or skipped the closing verb): a stale `.llmwiki.lock`,
`.llmwiki.txn`, and/or
`.llmwiki.txn.d/` remain in the wiki root while `wiki/` has no new pages. Recovery is
the operator running the driver's `abort` verb manually (the orchestrator does not invoke
it automatically), which releases the lock and rolls back the open journal:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest abort "$WIKI_ROOT"
```

⟦INGEST-DISCIPLINE⟧ Before every driver call, re-confirm: no delegation, no wrapper scripts, no stdout-parsing tools, no manual lock removal — one verb, one Bash call.
