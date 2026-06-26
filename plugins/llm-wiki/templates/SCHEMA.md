---
# SCHEMA.md frontmatter — machine-readable contract.
# Read by the plugin's own scripts (NOT the CC settings mechanism).
# This file is config-only (D6). Detection is the `.llmwiki` marker's job.
#
# config keys are PROPOSED names (design §7-C, frozen as proposals).
# Resolution per axis (code): prompt-explicit > wiki-local config > built-in default.
# Empty value -> built-in default shown in the comment.
config:
  activation_scope: scoped     # scoped|always|manual    (empty -> scoped)
  read_grounding:  implicit    # implicit|explicit        (empty -> implicit)
  write_mode:      explicit    # explicit|implicit        (empty -> explicit)
  write_autocommit: auto       # forced true when write_mode=implicit (floor)
  override_scope:  operation   # operation|session        (empty -> operation)
  apply_fanout_k:  10          # touch pages <=K inline, >K -> per-cluster subagent (D23)
  max_count:       100         # max pages a write tx may touch (budget gate, D-a)  (empty -> 100)
  max_bytes:       10485760    # max total write bytes per tx = 10 MiB (budget gate, D-a)  (empty -> 10485760)

# doc_type_profiles — 8 types seeded from compact2.md:54-89, plus mandatory `default`.
# LLM extraction covers all types for free; type-specific deterministic lint is
# use-driven and staged (v1 = transcript only). Prune/extend on the wiki side.
# Each profile: { preserve: [...], rules: [...] }.
doc_type_profiles:
  paper:        # Research / technical paper (compact2.md:55-57)
    preserve: [question, thesis, method, setup, results, metrics, baselines, limitations]
    rules:
      - prefer result-supported claims over broad abstract framing
  spec:         # Specification / design doc (compact2.md:59-61)
    preserve: [purpose, components, interfaces, flow, decisions, constraints, non-goals, risks]
    rules:
      - prefer signatures and behavioral rules over full code
  runbook:      # Procedure / runbook / SOP (compact2.md:63-65)
    preserve: [purpose, preconditions, tools, permissions, steps, branches, rollback, validation, safety]
    rules:
      - keep sequence integrity
  transcript:   # Conversation / meeting transcript (compact2.md:67-71)
    preserve: [objective, intent changes, decisions, work performed, artifacts, corrections, outstanding items, current state]
    rules:
      # v1 lint floor (compact2.md:69-70): blocks LLM-generated claims from being factualized.
      - record a claim under "decisions" only with an explicit affirmative token from the deciding speaker
      - treat silence or absence of objection as non-affirmation; keep such claims under "intent changes" or "outstanding items"
      - quote only decision-critical wording
  incident:     # Debug / incident log / postmortem (compact2.md:73-75)
    preserve: [problem, symptoms, impact, investigation, hypotheses, tests, root cause, fixes, verification, remaining risks]
    rules:
      - distinguish fact, hypothesis, confirmed cause, workaround, permanent fix
  policy:       # Policy / contract / terms (compact2.md:77-79)
    preserve: [scope, parties, definitions, obligations, permissions, prohibitions, exceptions, durations, termination logic, ambiguities]
    rules:
      - do not resolve ambiguity by inference
  article:      # Explanatory / narrative article (compact2.md:81-83)
    preserve: [explanatory flow, major conceptual blocks, named examples, practical notes, author assessment]
    rules:
      - do not let examples dominate structure
  guide:        # Guide / roundup / walkthrough / comparison / recommendation list (compact2.md:85-88)
    preserve: [grouped items, category blocks, comparison criteria, practical details, author assessment]
    rules:
      - keep original grouping
      - when listed items are the core payload, prefer preserving the full item set
  default:      # MANDATORY (D13). Used when doc_type classification matches no seeded type.
    preserve: [main named items, decisions, outcomes, key entities, operational identifiers, definitions]
    rules:
      - generic extraction; emit a "profile candidate" note to log.md so the open set stays safe
      - do not invent missing information; use "Unknown" / "Not specified" when needed
---

# SCHEMA.md — wiki contract

This file is the per-wiki, machine-and-human-readable contract. The plugin (an
immutable engine) never rewrites it; the wiki maintainer edits it by hand. The
frontmatter above carries `config` and `doc_type_profiles`; the prose below is
the contract that ingest, query, and lint procedures must honor.

## 1. Directory contract (design §1)

A wiki-root holds:

```
<wiki-root>/
  .llmwiki            # marker (D8): { version, schema: SCHEMA.md }
  SCHEMA.md           # this file — contract prose + YAML frontmatter (config + doc_type_profiles)
  index.md            # content-oriented catalog
  log.md              # append-only, prefix convention (see §4)
  raw/                # immutable sources (LLM reads only; redacted on ingest, D16)
    <hash>.<ext>      # provenance:source (id = content-hash, D18)
    derived/<hash>.md # provenance:derived (conversation/cc-log snapshot, redacted)
    assets/           # optional images
  wiki/               # LLM-generated pages = provenance:source-tier (promoted)
    <page>.md
    derived/<page>.md # provenance:derived synthesis (un-promoted, D15); promote -> wiki/
```

Rules:
- Marker presence is the wiki activation condition (when activation_scope=scoped).
  SCHEMA.md is config-only and is never used for detection (avoids generic-name
  misdetection).
- The marker's `schema` field is forward-compatible. SCHEMA.md is fixed for now;
  if renamed, update `.llmwiki.schema` to the new path (F-9).
- `raw/` is immutable. Content under `raw/` is read-only to the LLM; ingest writes
  it once, after redaction.

## 2. Tag axes (design §2) — 3 orthogonal axes

Pages and sources are tagged on three orthogonal axes. The trust boundary is
**location**, not a frontmatter flag.

- **provenance** — `source` / `derived`. Structured by location: `wiki/` = source
  tier, `wiki/derived/` = derived. Also mirrored in page frontmatter as a
  redundant field consistent with location. Promotion to source tier happens only
  via explicit `promote` (D15). The location is decisive for trust.
- **source_ref** (source-side origin, D12) — `{ raw_path: <relative, always>,
  external_locator?: <url|permalink> }`. Carries the citation form and re-fetch
  handle. Medium is derived from the locator (not enumerated). `raw_path` is
  ALWAYS relative; absolute paths are forbidden (secret).
- **derived origin** (derived-side origin, D12) — closed set, fixed two values:
  `conversation` / `cc-log`.
- **doc_type** — one of: transcript, article, paper, spec, runbook, incident,
  policy, guide (unknown -> `default`, D13). Selects the extraction profile and
  the type-specific lint.
- **epistemic-status** — per-claim, in the page body, OPTIONAL enrichment (D15).
  The value space is defined by the doc_type profile (no fixed enum). NOT a safety
  mechanism — the trust boundary is location; if epistemic-status is absent, safety
  is unaffected.

Page frontmatter therefore exposes (G3): `provenance`, `source_ref`,
`derived_origin`, `doc_type`; `epistemic-status` lives per-claim in the body.

## 3. config resolution (design §3)

Each axis resolves independently (code):
1. Prompt-explicit value -> use it (persistence governed by `override_scope`).
2. Otherwise the wiki-local `config` value above.
3. Empty -> built-in default (shown in the frontmatter comments).
4. Any write-bearing operation declares the resolved value + its source in one
   line before writing (D5).
5. An ingest-unit git checkpoint is the default in all modes (D14): confirm/stash a
   clean tree before ingest, commit on success, restore the wiki path on failure.
6. `write_mode` controls only whether a confirmation is shown before applying
   (not whether a commit happens). `write_mode=implicit` is announced loudly at
   session start (explicit notice that confirmation is skipped).

> wiki-local config is read by the plugin's own script parsing this frontmatter —
> it does not depend on the CC settings mechanism.

Config key names above are PROPOSED (design §7-C); treat them as proposals.

## 4. log.md prefix convention (design §4) — grep-parseable

`log.md` is append-only. Every entry header begins at the line start with `## [`
so a parser can read it via `grep "^## \[" log.md | tail`. The fixed token shape:

```
## [YYYY-MM-DD] <op>|<provenance-or-origin> | <Title>
```

Examples (front-end dispatch):
```
## [YYYY-MM-DD] ingest|source | <Title>
## [YYYY-MM-DD] file|derived  | <Title>
## [YYYY-MM-DD] file|cc-log    | <Title>
```

- `ingest|source` = 3rd-party source ingest (FE-B).
- `file|derived` = conversation/filing snapshot (FE-A).
- `file|cc-log` = cc-log jsonl snapshot (FE-B').

## 5. SCHEMA.md maintenance

- SCHEMA.md is wiki-local and per-wiki (D1). The plugin engine never edits it.
- The ingest core's allowlist write tool REJECTS `SCHEMA.md`, `.llmwiki`, and
  `raw/` as write targets (D19); the LLM cannot mutate this file during ingest.
- The maintainer (a human) edits it explicitly. Uncommitted hand-edits are stashed
  as an ingest-checkpoint precondition (R8).
- Profile pruning/extending is done on the wiki side: start from the 8 seeded types,
  drop unneeded types, add types as needed. Adding a type without a type-specific
  lint still yields LLM extraction along the profile; deterministic lint follows
  use-driven. Classification matching no type degrades to `default` and emits a
  "profile candidate" to log.md, so the open set stays safe and ingest never breaks.
- If SCHEMA.md is renamed, update `.llmwiki.schema` to the new path (F-9).
