# Workflow spec: dev (development / implementation tasks)

A workflow spec supplies **defaults and guidance** for one task type. It is weakly
coupled: the todolist is always authoritative; this spec only fills in a default
step sequence, a mode→model table, and failure-policy parameters, and its
mismatches surface as warnings (never rejections). See
`docs/skills/mode-orchestrator/WORKFLOW_SPEC_AUTHORING.md` for the spec format.

## Target task type

Development / implementation work: investigate → design → build → test → review → sync docs.

## Recommended step sequence

| order | mode | model | task |
|---|---|---|---|
| 1 | survey | (table) | Investigate the relevant source |
| 2 | plan | (table) | Design — includes the unit- AND integration-test strategy (design the tests here, from the spec) |
| 3 | review-dev | (table) | Review the design |
| 4 | execute | (table) | Implement + unit tests |
| 5 | execute | (table) | Run integration tests |
| 6 | review-dev | (table) | Review the implementation |
| 7 | execute | (table) | Remediate review findings |
| 8 | survey | **opus (pinned)** | docs-sync judgement — enumerate affected doc surfaces and build a drift list (or record a no-update judgement) |
| 9 | execute | (table) | docs-sync apply — apply the drift list (incl. EN/JA parity); no-op report if the list is empty |

`(table)` = resolved from the mode→model defaults below. Step 8 pins `opus`
explicitly (tier 1) because cross-doc consistency and EN/JA equivalence are
judgement-heavy; this keeps the blanket `survey: sonnet` default intact for
ordinary investigation (step 1).

## mode→model defaults

| mode | model |
|---|---|
| survey | sonnet |
| execute | sonnet |
| plan | opus |
| review-dev | opus |
| debug | opus |

These are tier-2 defaults (see the model precedence in SKILL.md): an explicit
per-step model overrides them; where neither applies, the session model is
inherited.

## Failure policy

- Recovery cycle cap: 2 per originating execute turn.

## Authoring guidance

- **Test design belongs in the plan step (2)**, derived from the spec — do not
  design tests after implementation (that fits tests to the code, not the
  requirement).
- **Review findings need a remediation turn**: a `review-dev` turn only reports
  (it never edits), so place an `execute` turn after it (step 7) to act on the
  findings.
- **Doc updates split into judgement then application**: a `survey` turn (step 8)
  decides which docs drift and builds the list; an `execute` turn (step 9)
  applies it. The judgement turn traces not only what changed but the path along
  which the change is observed in each doc surface, and records a no-update
  judgement explicitly when nothing drifts. Give the judgement turn a stronger
  model by pinning it on the step (e.g. `opus`) rather than raising the blanket
  `survey` default.
