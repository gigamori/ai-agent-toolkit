- Basic Behavior: Review a system design against quality dimensions and report a calibrated verdict — material findings + why, or an explicit clean pass
- NEVER: micro-fix, rubber-stamp, manufacture-findings, inflate-severity, expand-scope, assume-correctness
- DO: apply-lens-as-triggers, report-issues-with-rationale, calibrate-severity, pass-when-clean, surface-unlisted-concerns, cite-design-evidence

# Review lens — System Design Review

When the review target is a system design, use this tree as your lens.
Each node is a trigger, not a checklist: per node, recall relevant
considerations, apply them to the design, and report issues + why.
Add concerns matching a node's intent even if unlisted.

## Behaves correctly?  [dynamic / runtime]
  (AI-executed target: runtime nodes here are deterministic-software-oriented — translate or skip per "trigger, not checklist".)
- Path coverage
  - happy / error & exception paths
  - concurrency, races, idempotency
  - tx boundaries, rollback & cleanup
  - authz branching
  - graceful degradation; msg ordering / dup / loss

## Built correctly?  [static / structure]
- Existing-architecture consistency (rules to follow)
  - layering, dependency direction, SoC
  - naming, design-pattern adherence
  - backward compat, API versioning
- Modifiability & testability
  - impact locality; hidden coupling
  - substitutable deps; test ease

## Decisions sound?  [design judgment]
- tech choices, data model
- consistency model (strong / eventual)
- interface (user: friction, clear next action / system: misuse-resistant, granularity)

## Cross-cutting
- load & scalability (throughput / latency / bottlenecks)
- security (built-in: authn/authz, input validation, least privilege)
- observability (errors not swallowed; instrumentation seams; traceability)
- data accumulation (cross-flow orphans / stranded state)

## AI-executed target?  [non-deterministic / cognitive-load]
(additive to the nodes above; fires for prompt / skill / agent / LLM-run spec)
- Substrate fit: verifiable/repeatable → code (incl. LLM-authored-then-run); open/novel/judgment → LLM; mis-allocation either way = defect; deterministic check at load-bearing boundaries; few code↔LLM crossings
- Non-determinism: robustness across runs + the non-compliance path, not single-trace correctness; evaluate by sampling
- Cognitive-load budget: rules/branches/params/dispositions trade followability for coverage
- Likelihood steering: salience / placement / low-token reminders / examples over verbose rules (tradeoff: decay, over-steer)
- Concentrate: one un-droppable invariant; spec degrades by silent partial-drop
