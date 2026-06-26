---
name: wiki-query
description: Answer questions from the active LLM wiki. Use when a wiki is active (a "wiki-active" context is injected, or the CWD has a `.llmwiki` marker) and the user asks about content the wiki may hold, asks to look something up, recall a decision, find a page, or summarize what is known. Reads both `wiki/` and `wiki/derived/` and cites every claim by page path. Read-only by default; files an answer only when the user explicitly asks to file/save it.
---

# wiki-query

Answer the user's question by grounding it in the active wiki. This skill is
read-only by default; it writes ONLY on an explicit filing trigger — either a
marker-derived filing directive (mandatory, no confirmation) or a natural-language
explicit ask (see the last section).

## THE ONE UN-DROPPABLE INVARIANT (read first)

> **query never writes except on an explicit filing instruction, and every answer
> is grounded in BOTH namespaces with path-encoded-tier citations.** (05-plan §2.5;
> design D22/D3.) Read is implicit (no gate); write is explicit and independent —
> never let an implicit read leak into an implicit write.

## Step 0 — Resolve `WIKI_ROOT` (multi-scope; do NOT hardcode the CWD)

The wiki root is **resolved**, not assumed to be the CWD, and it MUST be the
SAME active wiki the marker hook keyed its filing directive off. Resolve it via
`wiki_root_resolver` (scopes: prompt>pj>workspace>cwd), honoring an explicit
`--root <path>` if the user passed one as the top override (Q4). Parse
`--root <path>` out of the request first (it is NOT a `key=value` axis); pass it
as `prompt_root`, else pass nothing. Do this **before any `$WIKI_ROOT` use**
below (Steps 1–3) — including the read-enumeration in Step 1:

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

The script prints `<root>\t<scope>` on stdout (split on the tab to get
`WIKI_ROOT` and the scope). If it exits non-zero (`NO-WIKI`), no wiki resolved —
report that this skill requires an active wiki (pass `--root <path>` or run from
a wiki root) and STOP. The filing in Step 3 MUST write to this SAME resolved
root — the marker hook keyed its directive off this active wiki, so binding
`$WIKI_ROOT` here is what makes the filing land in the wiki the hook decided.

## Step 1 — Read across BOTH namespaces (D22; 05-plan §2.1 step 1)

Ground every answer in BOTH `wiki/` (source tier) AND `wiki/derived/` (derived
tier). Enumerate pages deterministically (tier comes from the path via code, never
guessed):

```bash
uv run python - "$WIKI_ROOT" <<'PY'
import sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")
import wiki_index
root = sys.argv[1]
for pe in wiki_index.scan_pages(root):     # covers wiki/ AND wiki/derived/
    print(pe.tier, pe.rel_path)            # tier = wiki_index.tier_of(path), code-decided
PY
```

Read `index.md` and the relevant pages under both directories with the Read tool.

## Step 2 — Answer with path-citations; the path IS the tier (D22)

Cite every claim by the source page's `rel_path`. The path itself signals the
tier — `wiki/<page>.md` is source, `wiki/derived/<page>.md` is derived — so you
surface tier *from the path* and add no separate tier rule. A claim grounded only
in a `wiki/derived/` page is derived synthesis (not yet source-tier); say so by
its path. Do not present derived content as source-tier fact.

## Step 3 — File the answer on an explicit filing trigger

> **filing-is-explicit-only.** Do NOT write unless filing is explicitly
> triggered. Read being implicit never implies write. There are exactly TWO
> explicit triggers (below); absent both, this skill stays read-only.

### Trigger (1) — marker directive present → MANDATORY, no confirmation (plan §3 B-2, §0 M-d/M-e)

If a `[llm-wiki:file]` filing directive is present in your injected context (the
hook emits it deterministically when the prompt carries the inline marker
`llm-wiki:file[=<page-slug>]`; the directive says "you MUST file the answer via
the FE-A path … this is mandatory, not optional"), then filing is **mandatory**:

- File the answer via the FE-A path below WITHOUT re-judging whether to file —
  the decision is already fixed by the marker. Do not weigh it against the
  read-only default.
- File WITHOUT any confirmation: marker operations are explicit-by-definition and
  are excluded from all confirmation paths, including the `write_mode` pre-apply
  confirmation (plan §0 M-d). The safety envelope itself is NOT skipped (only the
  confirmation is) — see below.
- Page name: if the directive carries a slug, the page name is **fixed** to
  `wiki/derived/<slug>.md` — do NOT choose it yourself. If the directive gives no
  slug, **generate** the page name from the answer content (plan §0 M-e; this part
  is non-deterministic by design, L-a).

### Trigger (2) — natural-language explicit ask → as before (backward compatible)

When (and only when) the user explicitly asks in natural language to
file/save/record this answer (05-plan §2.2), file it. This legacy path is
unchanged and remains intact; the marker directive is an ADDITIONAL deterministic
trigger, not a replacement for it.

### Filing execution (identical for BOTH triggers; unchanged)

On either trigger, the filing path is FE-A → allowlist write tool
(`origin="derived"`) → single transaction, landing in `wiki/derived/` (it reuses
the ingest write-envelope — no new write seam; D20, §5/R10). FIRST emit the
one-line resolved-value declaration (D5) — still emitted for the record even on
the marker trigger (only the confirmation is skipped, not the safety envelope;
plan §0 M-d, §3 B-3) — THEN write. For the marker trigger with a fixed slug, use
that slug as `<page>`; otherwise generate `<page>` from the answer:

```bash
uv run python - "$WIKI_ROOT" <<'PY'
import sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")
import marker, config_resolver as cr, frontends, transaction, wiki_index, wiki_log
from write_tool import WriteSession, WriteRejected
root = sys.argv[1]
m = marker.detect(root)
res = cr.resolve_all({}, cr.load_config(m.schema_path))
print(cr.declare(res["write_mode"]))   # REQUIRED before any write (D5)
# Filing = FE-A on the answer text, then write inside the transaction:
#   fe = frontends.fe_a(root, answer_text)          # provenance:derived
#   if fe.exists: print("dedup no-op"); raise SystemExit(0)
#   with transaction.transaction(root, "file|derived | <Title>"):
#       sess = WriteSession(root, origin="derived")
#       sess.add("wiki/derived/<page>.md", "<answer page content>")
#       sess.commit()
#       wiki_index.regenerate(root)
#       op, tag = wiki_log.header_for_fe_a()         # ("file","derived")
#       wiki_log.append(root + "/log.md", op, tag, "<Title>")
PY
```

Honor `WriteRejected` exactly as the ingest apply-worker does (budget → human
gate; cross_namespace → keep it under `wiki/derived/`). Never promote here.
