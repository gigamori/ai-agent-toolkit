- Basic Behavior: Critically validate outputs against requirements and find root causes
- NEVER: fix-before-diagnosis, change-specs, assume-correctness, expand-scope
- DO: assume-broken, compare-to-requirements, hypothesize-test-fix, trace-root-causes, consult-official-docs

# Debug Guidelines

## Decision Rule

1. Diagnose first; output the root cause and a minimal fix proposal/diff. Applying edits is only the task's business when it explicitly says so.
2. Unresolvable with the tools and context you have (cannot reproduce, environment not inspectable, missing credentials/permissions, insufficient logs) → stop via the workflow ERROR: protocol, reporting what was tried and what is missing. Do not guess.

## Debug Rules

- Proposed fix must touch only target file(s); no unrelated changes
- Minimal fixes; no refactoring or style changes
- Provide the verification command for any proposed fix; if the task has you apply and verify, re-run it before concluding
- If unfixable → output root cause + recommended actions
- Do not debug unrecoverable errors (missing prerequisites you cannot fix)
- DIAGNOSIS SCOPE: a root-cause claim carries the evidence discriminating it from at least one rival hypothesis, or is marked "most plausible, undiscriminated"; the reproduction must exercise the suspected mechanism and the proposed fix must toggle the symptom — fails without it, passes with it — rather than merely coinciding with it
- LEFTOVERS: observations the chosen cause does not explain are listed as "unattributed" in the output, since that is where the next defect hides, and same-shape sites noticed along the way are named — naming is neither fixing nor scope expansion
