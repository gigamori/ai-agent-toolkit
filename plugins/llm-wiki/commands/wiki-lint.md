---
description: Lint the active LLM wiki — deterministic graph/index checks plus a transcript-only type-specific lint (v1), reported as a prioritized "next questions" list. Read-only; never writes. Usage `/wiki-lint [--root <path>]`.
disable-model-invocation: true
allowed-tools: Bash(uv run *) Agent Read Write
---

# /wiki-lint

Arguments: `$ARGUMENTS` (an optional `--root <path>` override).

Explicit, read-only lint of the active wiki.

## Resolve the wiki root (multi-scope; do NOT hardcode the CWD)

The wiki root is **resolved**, not assumed to be the CWD. Resolve it via
`wiki_root_resolver` (scopes: prompt>pj>workspace>cwd), honoring an explicit
`--root <path>` from `$ARGUMENTS` as the top override (Q4). Parse `--root <path>`
out of `$ARGUMENTS` first; pass it as `prompt_root`, else pass nothing:

```bash
WIKI_ROOT="$(uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki resolve-root ${ROOT_OVERRIDE:+--root "$ROOT_OVERRIDE"})"
```

The `resolve-root` verb prints `<root>\t<scope>` (split on the tab). If it exits non-zero
(`NO-WIKI`), no wiki resolved — report that this command requires an active wiki
(pass `--root <path>` or run from a wiki root) and STOP. **Before acting, show
the user the resolved root and scope** (`active wiki: <root> (scope: ...)`).

You own the deterministic verbs; the read-only `wiki-lint` subagent (declared in
`agents/`, no Write/Edit/Bash tool) owns the interpretation. Lint NEVER writes
(05-plan §3.5), so there is **no resolved-value declaration** — there is no write
to precede (05-plan §3.5; D5 applies only to writes). Both verbs below are
read-only against the wiki.

## Step 1 — Run the deterministic checks (the `lint` verb) and capture the output

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki lint "$WIKI_ROOT"
```

Capture the verb's stdout **verbatim** as `$LINT_OUTPUT` (`missing-crossrefs:`,
`orphans:`, `integrity-ok:`, `index-missing:` / `index-stale:`, `tier-mismatch:`).

## Step 2 — Dispatch the read-only lint subagent

Invoke the Agent tool with `subagent_type: wiki-lint`, passing the resolved
`WIKI_ROOT` and the full `$LINT_OUTPUT` verbatim as its input. The subagent
interprets the findings, isolates the transcript decision-floor candidates, and
returns its "next questions" report containing a `---CANDIDATES---` block (a JSON
array of `{"span": ..., "speaker": ...}` pairs between two `---CANDIDATES---`
marker lines).

## Step 3 — Run the deterministic decision floor (the `floor-check` verb)

Extract the JSON array between the `---CANDIDATES---` marker lines of the
subagent's response. If the block is absent or the array is `[]`, skip this step.
Otherwise save the array to a temporary file (outside the wiki root) and feed it
to the verb on STDIN:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki floor-check < "$CANDIDATES_FILE"
```

The verb runs the deterministic `transcript_floor.check_decision_claim` per
candidate and prints `FLOOR-VIOLATION <gate> :: <span>` for every non-admissible
span (empty output = no violations).

## Step 4 — Relay the report

Relay the subagent's "next questions" report to the user verbatim, appending any
`FLOOR-VIOLATION` lines from Step 3 under a `Floor-check violations:` heading. If
the report or the floor-check flags missing cross-refs, orphans, integrity drift,
or transcript decision-floor violations, surface those at the top.
