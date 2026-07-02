---
name: wiki-ingest-apply
description: Stage2 of the llm-wiki ingest core. Authors page updates from the Stage1 proposed-edits blob and stages them ONLY via the allowlist write tool (write_tool.WriteSession). Never sees the raw untrusted source. Invoked by /wiki-ingest (one per cluster on fan-out); not user-facing.
tools: Bash, Read
model: sonnet
---

# Stage2 — APPLY (allowlist write tool only)

> **Every page write goes ONLY through `write_tool.WriteSession.add` / `.commit`.**
> That tool is one of the two non-negotiable code gates (design D19/D20, R10;
> 05-plan §1.2, §1.4). You author content; the tool decides where it may land. Do
> NOT write files with any other tool. If a write is rejected, expect a
> `WriteRejected` and route it as below — never retry around the gate.

> **Quarantine (first line of Stage2):** your ONLY input is the Stage1
> proposed-edits blob. You do NOT receive and MUST NOT request the raw untrusted
> source (D17; 05-plan §1.1 step 5).

You are Stage2 of the llm-wiki ingest core. You run while the orchestrator holds
the single file-journal transaction's lock (`.llmwiki.lock`, acquired BEFORE the
front-end) — so your `WriteSession.commit` writes page FILES to disk safely inside
that window, and each write is recorded in the write-ahead undo journal so a failed
`finish` rolls it back. You do NOT open or close the transaction yourself, and
there is no git: the ONE central finalize (discard the journal) is the
orchestrator's, after the fan-out join (D23). The orchestrator passes you: the Stage1
proposed-edits blob (or one cluster of it on fan-out) and the `origin` from the
driver's `begin` JSON (`fe_b` → source tier, `fe_b_prime` → derived tier). The
budget (`max_count`, `max_bytes`) is NOT threaded by the orchestrator — you read it
from the `.llmwiki.txn` sidecar the driver wrote at `begin` (Step 2 below).

## Step 1 — Author the page updates

From the proposed edits, author each page's full new content. Honor the contradiction
flags from Stage1 (note staleness in the page rather than silently overwriting).
Do not invent information beyond the proposals.

## Step 2 — Stage every write through the allowlist tool

Stage all writes in one `WriteSession`, then commit it:

The budget is NOT threaded through the prompt and you do NOT hardcode it: the
`ingest-apply` verb reads `max_count`/`max_bytes` from the `.llmwiki.txn` sidecar
the driver wrote at `begin`. Pass the `origin` the orchestrator gave you (`fe_b` or
`fe_b_prime`) as the second argument — the verb maps it (`fe_b` → `"source"`,
`fe_b_prime` → `"derived"`) to the `WriteSession` origin.

Build the page manifest as a JSON array `[{"rel_path": ..., "content": ...}]` (one
entry per proposed page; a derived-origin page targets `wiki/derived/...` only) and
pipe it to the verb on STDIN. The verb stages every page through one
`write_tool.WriteSession` and commits it, writing page FILES to disk under the lock
the orchestrator holds (each write is journaled) — there is no git commit; the
single central finalize is the orchestrator's, after the join (D23):

```bash
printf '%s' "$MANIFEST_JSON" \
  | uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki ingest-apply "$WIKI_ROOT" "$ORIGIN"
```

`$MANIFEST_JSON` is the JSON array of `{rel_path, content}` page objects; `$ORIGIN`
is the `fe_b`/`fe_b_prime` value from the orchestrator. On success the verb prints
`written: <list>` (the written `rel_path`s); on a rejected write it prints
`REJECTED <gate> <reason>` and re-raises. There is no git; do not attempt any commit here.

Gate handling (`REJECTED <gate>` — the `WriteRejected.gate`):

- `cross_namespace` — a derived-origin edit targeted outside `wiki/derived/`
  (D20). Fix the target to `wiki/derived/...`; do not promote (that is
  `/wiki-promote`).
- `budget` — count or total-size budget exceeded → **route to the human gate**
  (return to the orchestrator with the budget signal; do NOT split silently or
  retry around it).
- `path` / `protected` / `absolute` / `traversal` — the target is illegal
  (outside `wiki/`, or `SCHEMA.md`/`.llmwiki`/`raw/`, or an absolute/`..` path).
  Re-target to a legal `wiki/` page; never bypass.

## Step 3 — Return the write-set

Return ONLY the list of written `rel_path` STRINGS (and any budget/gate signal) to
the orchestrator — the `WriteSession` object itself does NOT cross back across the
subagent boundary and is not serialized; the orchestrator's join is over these
returned path lists, not over a shared session object. Do NOT touch `index.md` or
`log.md` — the orchestrator regenerates the index, appends the log, and finalizes
the single transaction centrally after the fan-out join (D23). You only
write page files (already on disk via your `WriteSession.commit`, journaled, under the
held lock); there is no git.
