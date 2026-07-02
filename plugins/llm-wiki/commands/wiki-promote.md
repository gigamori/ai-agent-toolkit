---
description: Promote a derived synthesis page to source tier (wiki/derived/X.md → wiki/X.md) — a code-driven move + inbound link-rewrite, gated on explicit human approval and a contamination check. Usage `/wiki-promote <wiki/derived/X.md> [--root <path>]`.
disable-model-invocation: true
allowed-tools: Bash(uv run *) Read AskUserQuestion
---

# /wiki-promote

Arguments: `$ARGUMENTS` (a single `wiki/derived/X.md` path, plus an optional
`--root <path>` override).

## Resolve the wiki root (multi-scope; do NOT hardcode the CWD)

The wiki root is **resolved**, not assumed to be the CWD. Resolve it via
`wiki_root_resolver` (scopes: prompt>pj>workspace>cwd), honoring an explicit
`--root <path>` from `$ARGUMENTS` as the top override (Q4). Parse `--root <path>`
out of `$ARGUMENTS` first (separate from the `wiki/derived/X.md` path argument);
pass it as `prompt_root`, else pass nothing:

```bash
WIKI_ROOT="$(uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki resolve-root ${ROOT_OVERRIDE:+--root "$ROOT_OVERRIDE"})"
```

The `resolve-root` verb prints `<root>\t<scope>` (split on the tab to get `WIKI_ROOT` and the
scope). If it exits non-zero (`NO-WIKI`), no wiki resolved — report that this
command requires an active wiki (pass `--root <path>` or run from a wiki root)
and STOP. **Before acting, show the user the resolved root and scope** (`active
wiki: <root> (scope: ...)`). The remaining steps use this resolved `$WIKI_ROOT`
(a `.llmwiki` marker is enforced by `marker.detect` in Step 1).

## THE ONE UN-DROPPABLE INVARIANT (read first; highest salience)

> **derived→source promotion happens ONLY via `promote.promote` (move, not copy;
> code-driven link-rewrite) AFTER explicit human approval, and a contaminated
> derived page is never promoted.** (05-plan §4.5; design D15/D20; gitless-journal-transaction.md.) You do NOT
> hand-edit pages to simulate the move — the code does the move, the
> provenance-flip, and the inbound link-rewrite.

This is the only sanctioned cross-namespace (derived→source) transition. No
`promote.promote` call may run before the human approves.

## Step 1 — Resolve config and DECLARE (D5; 05-plan §4.5)

Promote is write-bearing, so emit the one-line resolved-value declaration BEFORE
the move:

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki declare "$WIKI_ROOT"
```

The `declare` verb detects the marker, resolves every config axis, and prints the
one-line resolved-value declaration `[wiki] write_mode = <value> (<source>)` (the
D5 announcement REQUIRED before the move). It is read-only — no move, no write.
Echo that line. If it prints `NOT-A-WIKI` (exit 2), stop.

## Step 2 — Present candidate + contamination, then get human approval

Show the candidate page and run the contamination check. `detect_contamination`
flags inline transclusion of derived content / an explicit `<!-- derived-inline -->`
marker — a contaminated page CANNOT be promoted (a source page may reference
derived by link only, D20):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki promote-check "$WIKI_ROOT" "$DERIVED_REL"
```

The `promote-check` verb is **read-only — it does NOT move** (it imports only
`derived_to_source_path` / `detect_contamination`, never `promote.promote`). It is
the pre-approval preview: it prints `dest: <wiki/X.md>` and
`contamination: <reasons-list>` (a non-empty list means the page CANNOT be
promoted). No file is touched here.

Present to the user: the candidate path, the destination (`wiki/X.md`), and the
contamination result. If contamination is non-empty, STOP — report that the page
cannot be promoted until the inline-derived content is removed.

If clean, ask for explicit approval via AskUserQuestion ("Promote
`wiki/derived/X.md` → `wiki/X.md`? This moves the file and rewrites inbound
references."). Do NOT proceed without an explicit yes. (05-plan §4.2:
human-approval-precedes-move.)

## Step 3 — Run the deterministic promote INSIDE the transaction

Only after approval, run the move inside the single file-journal transaction so a
failed promote rolls back cleanly (the move + inbound rewrites are journaled):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki promote "$WIKI_ROOT" "$DERIVED_REL" "<Title>"
```

The `promote` verb runs the deterministic move INSIDE the single file-journal transaction:
`promote.promote` (move + flip provenance + rewrite inbound refs), then index
regenerate and a `promote`/`source` log append — exactly the Step3 work, and ONLY
reachable here, after the human approval above. Pass the optional `<Title>` as the
third argument (it is the log title; omitted, it defaults to the relative path). On
success it prints `promoted -> <dest_rel>` and `rewritten: <paths>`.

`promote.promote` raises `PromoteRejected` on a non-derived source, missing file,
derived contamination, or an existing destination — surface the reason. Report the
new `dest_rel` and the rewritten inbound paths to the user.
