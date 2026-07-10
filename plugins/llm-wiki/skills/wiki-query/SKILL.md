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
below (Steps 1–3) — including the read-enumeration in Step 1.

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
(`WIKI_ROOT`=root, `WIKI_SCOPE`=scope) so a stray tab+scope never contaminates `$WIKI_ROOT`. If it exits non-zero (`NO-WIKI`), no wiki resolved —
report that this skill requires an active wiki (pass `--root <path>` or run from
a wiki root) and STOP. The filing in Step 3 MUST write to this SAME resolved
root — the marker hook keyed its directive off this active wiki, so binding
`$WIKI_ROOT` here is what makes the filing land in the wiki the hook decided.

## Step 1 — Retrieve pages across BOTH namespaces (D22; 05-plan §2.1 step 1)

Ground every answer in BOTH `wiki/` (source tier) AND `wiki/derived/` (derived
tier). Phrase the user's question into a short search query, then call the
`search` verb — it prints candidate pages as `<tier> <rel_path>` per line (tier
decided in code via `wiki_index.tier_of(path)`, never guessed):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki search "$WIKI_ROOT" --q "<your phrased query>"
```

`search` dispatches the backend INTERNALLY (DEC-3), so you always call this one
verb regardless of config:

- **`search_backend=index` (default):** returns the FULL page set across `wiki/`
  and `wiki/derived/` — identical to the old `scan-pages` enumeration (the `--q`
  is accepted but not used for ranking). Select the relevant pages yourself, as
  before.
- **`search_backend=qmd` (opt-in, large wikis):** returns a RANKED top-k of the
  most relevant pages (external qmd full-text backend; optional-search-qmd.md).
  A one-line `[search] …` notice on stderr means qmd was unavailable/degraded and
  the verb fell back to the index enumeration — treat the output as the index
  case. The FIRST qmd-backed query builds the index + embeddings inline (one-time,
  ~GB models); run `/wiki-reindex` beforehand to front-load that cost.

Either way the output shape and the read-only invariant are the same. Read
`index.md` and the returned pages under both directories with the Read tool (for
the qmd case the returned list is already the ranked shortlist to read).

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

The `file` verb performs the whole FE-A filing path: it FIRST emits the D5
resolved-value declaration line, then runs FE-A on the answer text (redaction +
content-hash dedup, `provenance:derived`); on a content-hash dedup hit it prints
`dedup no-op` and exits without writing; otherwise, inside a single transaction, it
writes the `raw/derived/<hash>.md` provenance snapshot AND the **redacted** page
(origin `derived`, landing under `wiki/derived/`), surfaces any `redaction-flags`
for the human gate, regenerates the index, and appends the log. Pass `<page>` (the
resolved `wiki/derived/<slug>.md` for the marker trigger,
else the page name you generated from the answer) and `<title>`; pipe the answer
page content on STDIN:

```bash
printf '%s' "$ANSWER_CONTENT" \
  | uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki file "$WIKI_ROOT" "wiki/derived/<page>.md" "<Title>"
```

The verb echoes the `[wiki] write_mode = <value> (<source>)` line first (D5). It
honors `WriteRejected` exactly as the ingest apply-worker does — on rejection it
prints `REJECTED <gate> <reason>` and re-raises (budget → route to the human
gate; cross_namespace → keep the target under `wiki/derived/`). Never promote here.
