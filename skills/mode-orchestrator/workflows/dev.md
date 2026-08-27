# Workflow spec: dev (development / implementation tasks)

A workflow spec supplies guidance for one task type. The todolist is authoritative;
this spec only recommends a sequence, pins an effort where needed, and sets failure
policy. See `docs/skills/mode-orchestrator/WORKFLOW_SPEC_AUTHORING.md`.

## Target task type

Development / implementation work: investigate → design → build → name the class → test → review → sync docs.

## Recommended step sequence

| id | mode | effort | task |
|---|---|---|---|
| investigate | survey | (infer) | Investigate the relevant source |
| design | plan | (infer) | Design — includes unit- and integration-test strategy from the spec |
| design-review | review-dev | (infer) | Review the design |
| implement | execute | (infer) | Implement + unit tests |
| class-disposition | execute | (infer) | Class disposition — enumerate sites sharing implementation's shape; assign fix / leave + reason / out of scope + reason; do not fix unsanctioned sites |
| integration-test | execute | (infer) | Run integration tests |
| implementation-review | review-dev | (infer) | Review the implementation |
| remediate | execute | (infer) | Remediate review findings |
| docs-sync-judgement | survey | ultra | Docs-sync judgement — enumerate affected surfaces and a drift list, or record no update |
| docs-sync-apply | execute | (infer) | Docs-sync apply — apply the drift list; report no-op if empty |

`(infer)` is no pin. A pin applies only to a todolist step declaring
`(workflow-step: ID)`; a matching mode or position does not bind it. Step
`docs-sync-judgement` pins `ultra` because cross-doc consistency and EN/JA
equivalence are judgement-heavy.

## Failure policy

- Recovery cycle cap: 2 per originating execute turn.

## Authoring guidance

- Test design belongs in step 2, derived from the spec.
- A `review-dev` finding needs a following `execute` remediation turn.
- Class disposition is unconditional and names sites; it does not bulk-fix them.
- Docs judgement and application stay separate; pin `ultra` only when a specific step needs it.
