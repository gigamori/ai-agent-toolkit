---
name: wiki-reindex
description: Rebuild the optional qmd full-text search index for the active LLM wiki (writes only under <root>/.qmd/; never touches wiki pages). No-op when search_backend is not qmd or qmd is absent. Usage `/wiki-reindex [--root <path>]`.
disable-model-invocation: true
allowed-tools: Bash(uv run *)
---

# /wiki-reindex

Arguments: `$ARGUMENTS` (an optional `--root <path>` override).

Explicit, front-loaded rebuild of the optional external **qmd** search index
(optional-search-qmd.md S5 / DD1). This is the manual counterpart to the
lazy-on-query refresh: it builds/refreshes qmd's project-local index once so the
next `search` is fast. It is **read-only with respect to wiki pages** — it writes
ONLY under `<wiki-root>/.qmd/`, outside the two code gates (R10), so there is no
resolved-value declaration (D5 applies only to page writes).

## Resolve the wiki root (multi-scope; do NOT hardcode the CWD)

The wiki root is **resolved**, not assumed to be the CWD. Resolve it via
`wiki_root_resolver` (scopes: prompt>pj>workspace>cwd>child), honoring an explicit
`--root <path>` from `$ARGUMENTS` as the top override (Q4). Parse `--root <path>`
out of `$ARGUMENTS` first; pass it as `prompt_root`, else pass nothing:

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
{ read -r WIKI_ROOT; read -r WIKI_SCOPE; } <<<"$RESOLVED"
```

The `resolve-root` verb prints ONE VALUE PER LINE — `<root>` on line 1, `<scope>` on
line 2 — and the block above reads them in that order (`WIKI_ROOT`=root,
`WIKI_SCOPE`=scope). The old tab-separated form was removed 2026-08-07; do NOT
split on a tab. If it exits
non-zero (`NO-WIKI`), no wiki resolved — report that this skill requires an
active wiki (pass `--root <path>` or run from a wiki root) and STOP. **Before
acting, show the user the resolved root and scope** (`active wiki: <root> (scope:
...)`).

## Rebuild the qmd index

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki reindex "$WIKI_ROOT"
```

The `reindex` verb is **no-op-safe** and never crashes:

- `search_backend` is not `qmd` (the default `index`) → it prints
  `[reindex] search_backend=index (not qmd); nothing to reindex` and exits 0.
  (Set `search_backend: qmd` in the wiki's `SCHEMA.md` config to enable qmd.)
- `search_backend=qmd` but the `qmd` binary is not on PATH → it prints
  `[reindex] … not on PATH; skipped` and exits 0 (install qmd first).
- `search_backend=qmd` and qmd present → it runs `qmd init` (project-local
  `.qmd/`), `qmd collection add <root>/wiki` (the `wiki/` subtree only; `raw/` is
  never indexed), `qmd embed` (vectors for the hybrid query engine), then
  `qmd update`. The FIRST run downloads qmd's on-device models (~GB) — this is the
  one-time cost that makes later `search` queries fast. Idempotent: re-running
  only refreshes changed pages.

Relay the verb's output line to the user. Note that qmd artifacts live entirely
under `<wiki-root>/.qmd/` — the shipped `.gitignore` excludes `.qmd/` (it can be
GBs) so a user who versions the wiki never commits the index. No wiki page,
`SCHEMA.md`, `.llmwiki`, or `raw/` content is read for writing or modified.
