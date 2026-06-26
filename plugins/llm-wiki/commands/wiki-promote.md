---
description: Promote a derived synthesis page to source tier (wiki/derived/X.md → wiki/X.md) — a code-driven move + inbound link-rewrite, gated on explicit human approval and a contamination check. Usage `/wiki-promote <wiki/derived/X.md> [--root <path>]`.
disable-model-invocation: true
allowed-tools: Bash(uv run python *) Read AskUserQuestion
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
WIKI_ROOT="$(uv run python - "${ROOT_OVERRIDE:-}" <<'PY'
import sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")
import wiki_root_resolver
arg = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
res = wiki_root_resolver.resolve(arg)
if res is None:
    print("NO-WIKI", file=sys.stderr); raise SystemExit(2)
print(f"{res.root}\t{res.scope}")
PY
)"
```

The script prints `<root>\t<scope>` (split on the tab to get `WIKI_ROOT` and the
scope). If it exits non-zero (`NO-WIKI`), no wiki resolved — report that this
command requires an active wiki (pass `--root <path>` or run from a wiki root)
and STOP. **Before acting, show the user the resolved root and scope** (`active
wiki: <root> (scope: ...)`). The remaining steps use this resolved `$WIKI_ROOT`
(a `.llmwiki` marker is enforced by `marker.detect` in Step 1).

## THE ONE UN-DROPPABLE INVARIANT (read first; highest salience)

> **derived→source promotion happens ONLY via `promote.promote` (move, not copy;
> code-driven link-rewrite) AFTER explicit human approval, and a contaminated
> derived page is never promoted.** (05-plan §4.5; design D15/D20/D21.) You do NOT
> hand-edit pages to simulate the move — the code does the move, the
> provenance-flip, and the inbound link-rewrite.

This is the only sanctioned cross-namespace (derived→source) transition. No
`promote.promote` call may run before the human approves.

## Step 1 — Resolve config and DECLARE (D5; 05-plan §4.5)

Promote is write-bearing, so emit the one-line resolved-value declaration BEFORE
the move:

```bash
uv run python - "$WIKI_ROOT" <<'PY'
import sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")
import marker, config_resolver as cr
root = sys.argv[1]
m = marker.detect(root)
if m is None:
    print("NOT-A-WIKI"); raise SystemExit(2)
res = cr.resolve_all({}, cr.load_config(m.schema_path))
print(cr.declare(res["write_mode"]))     # REQUIRED before the move (D5)
PY
```

Echo the `[wiki] write_mode = <value> (<source>)` line. If `NOT-A-WIKI`, stop.

## Step 2 — Present candidate + contamination, then get human approval

Show the candidate page and run the contamination check. `detect_contamination`
flags inline transclusion of derived content / an explicit `<!-- derived-inline -->`
marker — a contaminated page CANNOT be promoted (a source page may reference
derived by link only, D20):

```bash
uv run python - "$WIKI_ROOT" "$DERIVED_REL" <<'PY'
import sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")
import promote
from pathlib import Path
root, rel = sys.argv[1], sys.argv[2]
text = (Path(root) / rel).read_text(encoding="utf-8")
reasons = promote.detect_contamination(text)
print("dest:", promote.derived_to_source_path(rel))
print("contamination:", reasons)        # non-empty -> cannot promote
PY
```

Present to the user: the candidate path, the destination (`wiki/X.md`), and the
contamination result. If contamination is non-empty, STOP — report that the page
cannot be promoted until the inline-derived content is removed.

If clean, ask for explicit approval via AskUserQuestion ("Promote
`wiki/derived/X.md` → `wiki/X.md`? This moves the file and rewrites inbound
references."). Do NOT proceed without an explicit yes. (05-plan §4.2:
human-approval-precedes-move.)

## Step 3 — Run the deterministic promote INSIDE the transaction

Only after approval, run the move inside the single git transaction so a failed
promote rolls back cleanly (D21):

```bash
uv run python - "$WIKI_ROOT" "$DERIVED_REL" <<'PY'
import sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")
import transaction, promote, wiki_index, wiki_log
from promote import PromoteRejected
root, rel = sys.argv[1], sys.argv[2]
try:
    with transaction.transaction(root, f"promote | {rel}"):
        result = promote.promote(root, rel)     # move + flip provenance + rewrite
        wiki_index.regenerate(root)             # tier flips source after the move
        wiki_log.append(root + "/log.md", "promote", "source", "<Title>")
    print("promoted ->", result.dest_rel)
    print("rewritten:", result.rewritten)
except PromoteRejected as e:
    print("PROMOTE-REJECTED:", e.reason)        # non-derived/missing/contaminated/dest-exists
    raise
PY
```

`promote.promote` raises `PromoteRejected` on a non-derived source, missing file,
derived contamination, or an existing destination — surface the reason. Report the
new `dest_rel` and the rewritten inbound paths to the user.
