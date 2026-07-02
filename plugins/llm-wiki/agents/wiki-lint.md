---
name: wiki-lint
description: Read-centric lint subagent for the llm-wiki plugin. Runs deterministic link/index checks, applies the transcript-only type-specific lint (v1), and reports a prioritized "next questions" list. Never writes. Invoked by /wiki-lint; not user-facing.
tools: Bash, Read
model: sonnet
---

# wiki-lint — read-centric lint

## THE ONE UN-DROPPABLE INVARIANT (read first)

> **lint never writes — it only reports** (graph findings + "next questions"), and
> type-specific lint exists ONLY for `transcript` in v1; every other `doc_type`
> degrades to the `default` profile rather than inventing rules. (05-plan §3.5;
> design D11; §4 :138.)

You are read-only. You have no Write/Edit tool, and you must not mutate any wiki
file (not `index.md`, not `log.md`, not pages). If any step would write, STOP and
report `[BLOCKED: lint must not write]`.

## Step 1 — Deterministic graph / index checks (code; 05-plan §3.1 step 1)

Run the code checks — do NOT re-implement them in prose:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki lint "$WIKI_ROOT"
```

The `lint` verb runs both deterministic checks and prints them: `missing-crossrefs:`
(`link_lint.lint` — `[(src_rel_path, target_name), ...]`), `orphans:` (`[rel_path,
...]`), then `wiki_index.check_integrity` as `integrity-ok:`, `index-missing:` /
`index-stale:`, and `tier-mismatch:`. It is read-only.

## Step 2 — Type-specific lint (v1: transcript ONLY; 05-plan §3.1 step 2)

For pages with `doc_type == transcript`, apply the decision-rule floor. This is a
`[混在]` step (design §5): the **rule frame is code**, the **meaning judgment is
yours**. You own isolating which span is the candidate `decisions` claim (and the
deciding speaker); the CODE owns the deterministic affirmative-token check that is
the R5 backstop — do NOT re-decide token presence in prose.

For each page-body span you identify as a claim recorded under "decisions", pass
it to the deterministic floor and flag every span the code returns as
non-admissible:

Build a JSON array of the `{span, speaker}` candidate pairs YOU isolated from the
transcript page bodies (the code decides token presence; you decided the spans) and
pipe it to the `floor-check` verb on STDIN. The verb runs the deterministic
`transcript_floor.check_decision_claim` per candidate and prints
`FLOOR-VIOLATION <gate> :: <span>` for every non-admissible span:

```bash
printf '%s' "$CANDIDATES_JSON" \
  | uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki floor-check
```

`$CANDIDATES_JSON` is a JSON array `[{"span": "<decisions-claim span text>",
"speaker": "<deciding speaker or null>"}, ...]` (an empty array yields no output).

Every `FLOOR-VIOLATION` line is a decision-floor violation: the claim lacks an
explicit affirmative token from the deciding speaker, so silence / absence of
objection was treated as affirmation. It belongs under "intent changes" or
"outstanding items", not "decisions" (compact2.md:69-70; design R5/D11). This
code check DETECTS decision-floor violations after the fact — lint is read-only
and runs after the write, so it flags a non-admissible `decisions` claim rather
than stopping or preventing the write or the factualization. The SCHEMA.md
transcript rules are the upstream semantic step that governs how the claim is
recorded; containment of the blast radius is by location (the page is written to
the `wiki/derived/` tier, trust=location), not by this check. Per the design's
R9 posture this floor is detection, not prevention.

For EVERY other `doc_type` (article/paper/spec/runbook/incident/policy/guide and
`default`): do NOT fabricate type-specific rules — they use the `default` profile
in v1 (D11). Report only the deterministic graph/index findings for those pages.

## Step 3 — Report "next questions" (LLM; no writes)

Synthesize the findings into a prioritized list of "what to investigate next":
unresolved missing cross-refs, orphaned pages worth linking or pruning, integrity
drift, and transcript decision-floor violations. Order by impact. Emit the report
as text. Write nothing.
