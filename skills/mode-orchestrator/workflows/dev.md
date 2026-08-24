# Workflow spec: dev (development / implementation tasks)

A workflow spec supplies guidance for one task type. The todolist is authoritative;
this spec only recommends a sequence, pins an effort where needed, and sets failure
policy. See `docs/skills/mode-orchestrator/WORKFLOW_SPEC_AUTHORING.md`.

## Target task type

Development / implementation work: investigate → design → build → name the class → test → review → sync docs.

## Recommended step sequence

| order | mode | effort | task |
|---|---|---|---|
| 1 | survey | (infer) | Investigate the relevant source |
| 2 | plan | (infer) | Design — includes unit- and integration-test strategy from the spec |
| 3 | review-dev | (infer) | Review the design |
| 4 | execute | (infer) | Implement + unit tests |
| 5 | execute | (infer) | Class disposition — enumerate sites sharing step 4's shape; assign fix / leave + reason / out of scope + reason; do not fix unsanctioned sites |
| 6 | execute | (infer) | Run integration tests |
| 7 | review-dev | (infer) | Review the implementation |
| 8 | execute | (infer) | Remediate review findings |
| 9 | survey | high | Docs-sync judgement — enumerate affected surfaces and a drift list, or record no update |
| 10 | execute | (infer) | Docs-sync apply — apply the drift list; report no-op if empty |

`(infer)` uses the shared execution profile. Step 9 pins `high` because cross-doc
consistency and EN/JA equivalence are judgement-heavy.

## Failure policy

- Recovery cycle cap: 2 per originating execute turn.

## Authoring guidance

- Test design belongs in step 2, derived from the spec.
- A `review-dev` finding needs a following `execute` remediation turn.
- Class disposition is unconditional and names sites; it does not bulk-fix them.
- Docs judgement and application stay separate; pin `high` only when a specific step needs it.
