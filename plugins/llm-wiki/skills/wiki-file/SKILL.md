---
name: wiki-file
description: File the conversation you are having right now into the active wiki, via the same 2-stage extract→apply core as /wiki-ingest-docs. Fixes the source to the RUNNING session id, `--kind=fe_b_prime`, and `--cutoff=last-user` (the invocation itself is an instruction, not payload); free text after the command NARROWS which turns are filed. For a 3rd-party document use `/wiki-ingest-docs`; for OTHER sessions' logs use `/wiki-ingest-sessions`. Explicit write-bearing skill (hook-independent). Usage `/wiki-file [narrowing text]`.
disable-model-invocation: true
allowed-tools: Bash(uv run --script *), Agent, AskUserQuestion, Write
---

# /wiki-file

Arguments: `$ARGUMENTS`

You are the filing orchestrator for the RUNNING session. This is
`/wiki-ingest-docs` with its arguments fixed: there is no new pipeline, front-end,
driver verb, ledger, or transaction behavior here — only a source you do not ask the
user for. You do not run the deterministic envelope yourself; the `ingest_driver.py`
CLI owns it (config resolution, the single file-journal transaction, the FE-B'
projector front-end, redaction, the turn-content-hash ledger dedup, the central join,
index/log). Your job is to call the driver's verbs in order and dispatch the two LLM
stages to subagents in between. You do not author wiki page content yourself — the
Stage2 apply-worker authors it and returns a page manifest; you pass that manifest
through the driver's compound `apply-finish` verb (E3), where the allowlist write tool
(`write_tool.WriteSession`) gates every page write and, on success, the same verb
performs the central join + single commit.

Three things are FIXED and are never taken from `$ARGUMENTS`:

| fixed | value | why |
|---|---|---|
| source | the running session's own sid | the user should not have to know their session id |
| `--kind` | `fe_b_prime` | the source is a cc session log; this also means the driver's `.jsonl` auto-kind gate is never reached |
| `--cutoff` | `last-user` | your own invocation turn is an INSTRUCTION about the wiki, not conversation content (D6) |

`$ARGUMENTS` is therefore NEVER a path and NEVER a flag — it is free text that
NARROWS which turns are filed (Step 0). Filing is incremental by construction: the
turn ledger drops every turn a prior run already owns, so re-running this command
files only what is new.

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
> - **NEVER delegate the cycle.** YOU execute it directly; do NOT hand it to a
>   general-purpose subagent. The ONLY Agent-tool dispatches are the two declared stage
>   workers (`llm-wiki:wiki-ingest-extract`, `llm-wiki:wiki-ingest-apply`).
> - **NEVER script the cycle.** No bash/python/PowerShell batch scripts wrapping the
>   driver verbs. One driver verb = one Bash invocation.
> - **NEVER parse driver stdout with tools.** No `jq` / `ConvertFrom-Json` / improvised
>   parsers — deterministic extraction is code-owned by the driver verbs and the stdout JSON
>   is deliberately small (E1); read the fields you need directly.
> - **NEVER hand-clear a stuck transaction.** Recover a residual `.llmwiki.lock` /
>   `.llmwiki.txn.d` ONLY with `ingest abort`, never `rm -f`. On a lock-held `begin` error,
>   STOP and run `ingest abort` first.

## Step 0 — Resolve the wiki root and the session id

The wiki root is resolved, not assumed to be the CWD. Resolve it via
`wiki_root_resolver` (scopes: prompt>pj>workspace>cwd>child), honoring an explicit
`--root <path>` from `$ARGUMENTS` as the top override (Q4) — parse and strip it before
reading the rest of `$ARGUMENTS` as narrowing text.

Capture the running session's own id as `SID` via the `${CLAUDE_SESSION_ID}`
skill-template substitution (the harness replaces this placeholder with the literal
session id before you see this text — it is not an OS env var). `SID` is BOTH the
resolver's session-aware pj fast-path input (`--sid`, so it does not degrade to a
mtime-latest scan that can cross-talk between concurrent sessions) AND this command's
ingest source:

```bash
SID="${CLAUDE_SESSION_ID}"
RESOLVED="$(uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki resolve-root ${ROOT_OVERRIDE:+--root "$ROOT_OVERRIDE"} --sid "$SID")" \
  || { echo "resolve-root failed (NO-WIKI or resolver error) — stop"; }
{ read -r WIKI_ROOT; read -r WIKI_SCOPE; } <<<"$RESOLVED"
```

The `resolve-root` verb prints ONE VALUE PER LINE on stdout — `<root>` on line 1,
`<scope>` on line 2. If it exits non-zero (`NO-WIKI`), no wiki resolved — report that
this skill requires an active wiki (pass `--root <path>` or run from a wiki root) and
STOP. Before acting, show the user the resolved root and scope (e.g.
`active wiki: <root> (scope: pj|workspace|cwd|child|prompt)`).

## Step 0a — Narrowing: which of the two flows this run takes

After stripping `--root`, whatever remains in `$ARGUMENTS` is narrowing text.

- **Empty (the common case) → FLOW A.** Go straight to Step 1 with no `--turns`.
  The projector extracts this session itself. No LLM is in the turn-selection path
  at all.
- **Non-empty → FLOW B (Step 0b).** The user is scoping which turns to file (e.g.
  `最後の回答だけ`, `retry-policy の議論だけ`). This needs a turn list you can filter,
  so extract it with code first and then REMOVE entries.

Narrowing NEVER switches pipeline: both flows are FE-B' with the same ledger and the
same raw-transcript snapshot. A page name the user mentions in the narrowing text is
context for the stages, not a routing decision.

### Step 0b — FLOW B only: code-extract, then DROP entries

Call the read-only `project-batch` verb for this one sid. It opens no transaction (no
lock, no checkpoint, no sidecar) and writes only outside the wiki root:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest project-batch \
  "$WIKI_ROOT" "$SID" --kind=fe_b_prime
```

It prints `{"out_dir": <temp dir>, "turns": {<sid>: <path>}, "scanned": 1}`. Capture
`out_dir` (for cleanup in Step 7) and `turns[$SID]` as `$TURNS_PATH`.

Now edit `$TURNS_PATH` — under exactly two rules:

> **(1) You may DELETE whole entries from the `turns` array. You may not change anything
> else** — not a character of any surviving entry's `role`, `projected_text`, `hash`,
> `uuid`, or `ts`, and not the file's `sid` / `origin` fields.
>
> **(2) NEVER delete anything from the LAST USER-ROLE entry onward.** That entry is your
> own `/wiki-file` invocation turn and it is the cutoff's anchor (D13): `begin` drops the
> last user-role turn and everything after it. Delete the anchor and the cutoff lands on
> the user's last real turn instead, silently removing the very content this run exists
> to file. Narrow from the FRONT and the MIDDLE only.
>
> Count from the last USER entry, NOT from the end of the file: your own narration is
> flushed to the log mid-turn, so the file's literal last entries are usually
> `assistant` records that came AFTER the invocation. Guarding only "the last entry"
> guards the wrong record.

Rule (1) is enforced in code, not by your care: `begin` re-computes every surviving
entry's hash from its own role+text and REFUSES the whole ingest on any mismatch (D14).
An entry therefore survives byte-for-byte or the run fails — you cannot rewrite,
summarize, merge, or invent a turn through this channel. If you find yourself wanting
to edit text, the answer is to delete that entry instead.

Rule (2) is NOT code-enforced — a turn list is a legitimate shape either way, so the
driver cannot tell a deliberate tail-trim from this mistake. Check it before you write
the file: find the LAST entry whose `role` is `user`, and keep it plus everything after
it. (It usually looks EMPTY: D7 strips the `/wiki-file` line at extraction, so its
`projected_text` is `""`. An empty trailing user entry is the anchor, not junk — do not
"tidy" it away.)

Deleting is not destructive: a dropped turn is never filed, so it never enters the
ledger, and a later `/wiki-file` can still file it.

## Step 1 — `begin`: open the transaction, project, normalize, declare

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest begin \
  "$WIKI_ROOT" "$SID" \
  --kind=fe_b_prime \
  --cutoff=last-user \
  ${TURNS_PATH:+--turns="$TURNS_PATH"} \
  ${WRITE_MODE:+--write_mode="$WRITE_MODE"} \
  ${APPLY_FANOUT_K:+--apply_fanout_k="$APPLY_FANOUT_K"}
```

`--turns` is present in FLOW B only. `--cutoff=last-user` is passed in BOTH flows: it
drops the last user-role turn and everything after it — chosen by ROLE and ORDER only,
never by the turn's text, so the invocation turn anchors the cut even when D7 has left
it empty (Step 0b rule (2)) — which is this very invocation and the narration that
follows it. Note what this does NOT cost
you: the previous turn's assistant answer is before your invocation, so it IS filed.
Only this run's own report is never captured — and the next `/wiki-file` picks up
anything after the cutoff that is still worth keeping.

Do NOT pass `--doc_type`: the FE-B' code floor pins `doc_type: transcript`.

From the printed JSON capture: `declaration`, `raw_rel_path`, `stage1_blob_path`,
`origin` (always `fe_b_prime` here), `doc_type` (always `transcript`), `max_count`,
`max_bytes`, `apply_fanout_k`, `dedup_noop`, `ledger_skipped`, `redaction_flags`.

Then:

- Echo every `declaration` line verbatim (D5 — the resolved-value announcement precedes
  all stages). If `write_mode` resolved to `implicit`, announce loudly that per-apply
  confirmation is skipped.
- **Surface `redaction_flags`** so the human gate sees what the FE redacted.
- **Surface `ledger_skipped`** — the number of turns already owned by a prior filing.
  On a re-run this is how the user sees the run was incremental rather than a no-op.
- Do NOT narrate the `cutoff_dropped` count. The cut is expected, not an event: this
  command always stops before its own invocation turn, and that is already stated in
  the skill's description above. A per-run number is not actionable — its value tracks
  how much harness narration happened, not anything the user did.
- **If `dedup_noop` is `true`:** nothing new to file since the last run. Report that and
  STOP — `begin` returned `auto_closed: true`, having already rolled back and released
  the lock itself, so do NOT call `finish` (there is no sidecar) and do NOT dispatch the
  stages.

If `begin` exits non-zero, report its stderr and stop:
- "not a wiki root" → this skill requires an active wiki.
- `extract: cc session file not found for sid …` → no CC log universe holds `<SID>.jsonl`,
  so there is nothing to project (FLOW A only — FLOW B passes `--turns` and does not
  re-scan). Nothing was locked or written. Re-check the `${CLAUDE_SESSION_ID}` substitution
  (and `$CLAUDE_CONFIG_DIR` if the corpus lives outside `~/.claude`); do NOT retry with a
  guessed sid.
- `config-inconsistency:` → the consistency invariant (`apply_fanout_k ≤ max_count`) was
  violated; nothing was locked or written.
- a `--turns` hash-check failure (FLOW B) → an entry was EDITED, not merely dropped.
  Nothing was locked or written. Redo Step 0b's filtering with deletions only.
- a lock-held error → another ingest holds `.llmwiki.lock`; report and stop (the driver
  already rolled back its checkpoint).

## Step 2 — Stage1 EXTRACT (no write tool; untrusted read)

Dispatch via the Agent tool with `subagent_type: llm-wiki:wiki-ingest-extract` (the
`llm-wiki:` namespace is **required** — a bare `wiki-ingest-extract` can shadow-resolve
to an incompatible user-level agent that holds no working tools, silently yielding a
`tool_uses: 0` extraction). It is the only place the untrusted raw body is read, and it
has **no write tool** (`tools: Read`).

Instruct it to Read the raw artifact at `$WIKI_ROOT/<raw_rel_path>` — the projected
transcript. Pass `doc_type=transcript` and instruct it to honor the pinned type and
skip classification (the FE-B' code floor). Pass the user's narrowing text, if any, as
CONTEXT for what matters in this conversation — it reaches the stages through
`$ARGUMENTS`, a channel independent of the transcript, which is why Step 1's cutoff
loses nothing.

**Tier (#2).** `origin` is `fe_b_prime`, so instruct Stage1 to propose every affected
page's `rel_path` under `wiki/derived/…` (the DERIVED tier). The driver enforces this in
code — `plan-fanout` rejects a proposal whose touched pages leave `wiki/derived/`, and
the Stage2 write gate (D20) admits only that prefix — so proposing the correct tier is
load-bearing, not cosmetic.

Stage1 returns a single JSON object — `{"touched": [rel_path, …], "edits": [{rel_path,
op, proposal}, …], "contradictions": […], "doc_type": …}` (its agent-def Step 3
contract; a prose/Markdown blob makes `plan-fanout` fail `neither a file nor JSON`).
**Write it once**, exactly as returned, to the absolute `stage1_blob_path` from `begin`'s
JSON — **use that path verbatim; do not reconstruct a temp path yourself** (#1). Call it
`$STAGE1_BLOB_PATH`. From here the blob is passed by path only, never re-inlined.

## Step 3 — Decide fan-out (touch-count vs K; D23)

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest plan-fanout \
  "$WIKI_ROOT" "$STAGE1_BLOB_PATH"
```

Always call it, even when the touched count is ≤ K (D-COV: a single-cluster run still
needs its ordinal for the C2 dispatch check). Clustering is code, not LLM (F6). The
driver persists the plan to the sidecar and prints `{"clusters": [[rel_path, ...], ...],
"manifest_paths": [<absolute path>, ...]}`, each cluster ≤ K, `manifest_paths[i]`
aligned to `clusters[i]`. The 0-based index of each cluster is its ordinal; keep the
workers and manifests in that order.

## Step 4 — Stage2 APPLY (workers author manifests; you collect them)

Dispatch with `subagent_type: llm-wiki:wiki-ingest-apply` (the `llm-wiki:` namespace is
**required**), one per cluster on fan-out, else one. The worker has **no write tool**
(`tools: Read`): it authors each page's content and returns — as its **final response
text, and nothing else** — a page manifest, a JSON array `[{"rel_path": ...,
"content": ...}]` (`[]` if there is nothing to write). Its only input is the Stage1
proposed-edits blob (or one cluster of it) — **never the raw untrusted source** (the
quarantine seam, D17).

Pass each worker: `$STAGE1_BLOB_PATH`, this cluster's `rel_path` list, and
`origin: fe_b_prime` (→ derived tier). Save each returned manifest verbatim to
`manifest_paths[ordinal]` (the code-authored path from Step 3 — do not construct one).
Collect the ordered list of manifest paths for Step 5. Apply nothing here.

If a worker errors or fails to return a manifest, this run failed before apply: skip
`apply-finish` and roll back via `finish fail` (Step 6's failure path).

## Step 5 — Pre-apply confirmation (D5; skip only on `write_mode=implicit`)

**When `write_mode` resolved to `explicit`** (the template default — so this is the normal
path, not an edge case), ask before any page is written. Use `AskUserQuestion` with the
page list drawn from the Step 4 manifests:

- List every `rel_path` you are about to write, and for each say whether it is a NEW page
  or an UPDATE to an existing one. This is the point of the gate: the user asked to file,
  but never chose which pages get created — this is where LLM-authored content becomes
  visible before it lands.
- Approved → run Step 6.
- Declined → run Step 6's `finish fail` path. Nothing is written, the journal is replayed
  and the lock released. Report the rollback. Do NOT re-ask, re-word, or retry around a
  decline.

**When `write_mode` resolved to `implicit`**, skip the question — you already announced at
Step 1 that per-apply confirmation is skipped — and go straight to Step 6.

Two carve-outs, both deliberate:

- `llm-wiki:` marker-driven operations are exempt from EVERY confirmation path including
  this one (M-d) — but no marker reaches this skill, so the exemption never fires here.
- **A non-interactive run cannot answer this question.** `claude -p` and any other
  print-mode / automated caller has no way to reply, and the transaction is OPEN with the
  lock held while you wait — stalling here strands it exactly like a missed closing verb.
  If you cannot ask, do NOT guess an answer and do NOT proceed to write: take Step 6's
  `finish fail` path and report that the run needs either an interactive session or an
  explicit `write_mode=implicit` override. Automated callers must pass
  `write_mode=implicit` deliberately.

## Step 6 — `apply-finish`: apply the manifests + central join + single commit

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest apply-finish \
  "$WIKI_ROOT" fe_b_prime \
  --manifest "$MANIFEST_0" --manifest "$MANIFEST_1" ... \
  ${TITLE:+--title="$TITLE"}
```

Each `--manifest` is listed in cluster-ordinal order (position == ordinal). Do not pass
`expected_pages`: `apply-finish` proves every planned cluster ran from `planned_clusters`
(C2). The verb reads the plan + budget from the sidecar (you thread no state), verifies
F2 (manifest count == planned cluster count, each manifest's `rel_path`s ⊆ its planned
cluster) before any write, stages every manifest through `write_tool.WriteSession` under
the held lock, and — on full success — performs the central join (index regenerate, log
append) and the single `commit`.

- **Success** (exit 0) → stdout
  `{"clusters":[{"ordinal":N,"written":[rel_path,...]},...],"committed":true}`. Report
  the written pages, plus the `ledger_skipped` count from Step 1 so an incremental run
  reads as incremental.
- **REJECT** (exit non-zero) → stderr `REJECTED <gate> <reason>` + stdout
  `{"rolled_back":true}`. `apply-finish` has ALREADY rolled back and released the lock —
  do NOT also call `finish`. Report the gate. NEVER bypass or retry around the code gate.
  Gates: `budget` (count / total-size overflow → the human gate), `manifest_count` /
  `cluster_pageset` (an F2 mismatch), or `cross_namespace` / `path` / `protected` /
  `absolute` / `traversal` (an illegal target — for this origin, anything outside
  `wiki/derived/`).

**Pre-apply failure path — `finish fail`.** When the run failed before `apply-finish` (a
Stage1/Stage2 error, or a `plan-fanout` budget gate), the transaction is still open:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest finish \
  "$WIKI_ROOT" fail
```

The driver prints `{"rolled_back": true}` (journal replayed, lock released, sidecar
deleted). Report the rollback. (A `begin` error is NOT routed here — `begin` already
rolled back and released the lock itself.)

## Step 7 — FLOW B only: clean up the temp dir

If Step 0b ran, delete its temp dir with the driver verb — code owns the deletion (C3):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest project-batch-cleanup \
  "$OUT_DIR"
```

Run it on both the success and failure paths (the temp turn JSON is pre-redaction).
Never `rm -rf` the directory yourself.

**Stuck-transaction recovery (symptom → abort).** A run interrupted before the closing
`apply-finish`/`finish` leaves a stale `.llmwiki.lock`, `.llmwiki.txn`, and/or
`.llmwiki.txn.d/` while `wiki/derived/` has no new pages. Recovery is the operator
running the driver's `abort` verb manually:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest abort "$WIKI_ROOT"
```

⟦INGEST-DISCIPLINE⟧ Before every driver call, re-confirm: no delegation, no wrapper scripts, no stdout-parsing tools, no manual lock removal — one verb, one Bash invocation.
