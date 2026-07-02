---
description: Lint the active LLM wiki — deterministic graph/index checks plus a transcript-only type-specific lint (v1), reported as a prioritized "next questions" list. Read-only; never writes. Usage `/wiki-lint [--root <path>]`.
disable-model-invocation: true
allowed-tools: Bash(uv run *) Agent Read
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

This command does no work itself beyond dispatch: it invokes the read-centric
`wiki-lint` subagent (declared in `agents/`) and relays its report. Lint NEVER
writes (05-plan §3.5), so there is **no resolved-value declaration** — there is no
write to precede (05-plan §3.5; D5 applies only to writes).

Invoke the Agent tool with `subagent_type: wiki-lint`, passing the resolved
`WIKI_ROOT`.
Relay its "next questions" report to the user verbatim. If the report flags
missing cross-refs, orphans, integrity drift, or transcript decision-floor
violations, surface those at the top.
