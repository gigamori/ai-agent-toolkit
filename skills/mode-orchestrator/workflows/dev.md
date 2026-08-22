# Workflow spec: dev (development / implementation tasks)

A workflow spec supplies **defaults and guidance** for one task type. It is weakly
coupled: the todolist is always authoritative; this spec only fills in a default
step sequence, a mode→model table, and failure-policy parameters, and its
mismatches surface as warnings (never rejections). See
`docs/skills/mode-orchestrator/WORKFLOW_SPEC_AUTHORING.md` for the spec format.

## Target task type

Development / implementation work: investigate → design → build → name the class → test → review → sync docs.

## Recommended step sequence

| order | mode | model | task |
|---|---|---|---|
| 1 | survey | (table) | Investigate the relevant source |
| 2 | plan | (table) | Design — includes the unit- AND integration-test strategy (design the tests here, from the spec) |
| 3 | review-dev | (table) | Review the design |
| 4 | execute | (table) | Implement + unit tests |
| 5 | execute | (table) | Class disposition — enumerate the sites sharing the shape of what step 4 changed, and give each one a disposition (fix / leave + reason / out of scope + reason). Naming only: fix nothing this todolist has not already sanctioned |
| 6 | execute | (table) | Run integration tests |
| 7 | review-dev | (table) | Review the implementation |
| 8 | execute | (table) | Remediate review findings |
| 9 | survey | **opus (pinned)** | docs-sync judgement — enumerate affected doc surfaces and build a drift list (or record a no-update judgement) |
| 10 | execute | (table) | docs-sync apply — apply the drift list (incl. EN/JA parity); no-op report if the list is empty |

`(table)` = resolved from the mode→model defaults below. Step 9 pins `opus`
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
  (it never edits), so place an `execute` turn after it (step 8) to act on the
  findings.
- **Class disposition is its own turn (step 5), and it is unconditional**: a fix
  is one instance of a class far more often than it is unique, and which of the
  two it is cannot be known before the enumeration — so a step that runs "when
  the class looks big" never runs. It is a separate turn rather than a clause on
  step 4 because the enumeration multiplies the wall-clock and tool-call cost of
  whichever turn carries it; each turn holds one per-mode wall-clock budget, and
  spending that multiplier inside step 4's budget risks aborting the
  implementation itself, while a turn of its own starts from a fresh one. A
  todolist without this step still runs — the spec only warns (`SKILL.md:106`) —
  so expect that warning on any list written before this spec gained step 5.
- **Naming is not sweeping**: step 5 names sites and assigns dispositions; it
  does not bulk-fix. Editing the other sites is scope expansion — propose it,
  with the inventory attached, and let the user rule. A turn that reports "3
  sites, 1 fixed here, 2 left because <reason>" has done the step correctly.
- **Doc updates split into judgement then application**: a `survey` turn (step 9)
  decides which docs drift and builds the list; an `execute` turn (step 10)
  applies it. The judgement turn traces not only what changed but the path along
  which the change is observed in each doc surface, and records a no-update
  judgement explicitly when nothing drifts. Give the judgement turn a stronger
  model by pinning it on the step (e.g. `opus`) rather than raising the blanket
  `survey` default.
