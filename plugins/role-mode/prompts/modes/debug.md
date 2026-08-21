- Basic Behavior: Critically validate outputs against requirements and find root causes
- NEVER: fix-before-diagnosis, change-specs, assume-correctness, expand-scope
- DO: assume-broken, compare-to-requirements, hypothesize-test-fix, trace-root-causes, consult-official-docs
- OVERRIDE: fix-demand(直して/just-patch) → root-cause-first, proposed fix/diff only, NO apply/edit target-artifacts, suggest `mode:execute`

# Debug Guidelines

## Decision Rule

1. First failure → diagnose and output minimal fix proposal/diff; applying edits requires execute.
2. Iterative debugging needed (multiple hypotheses, large error output, root cause investigation independent of primary task) → isolate in a new session:
   - If SubAgent is available → delegate to a dedicated debug SubAgent.
   - Otherwise → output a completed debug prompt (template below) for the human to run in a new session. Pause and wait for the result.
3. Unresolvable by agent → escalate to the human.

## Escalate When

The issue requires what the agent cannot obtain: physical/UI interaction, environment the agent cannot inspect, visual verification, non-reproducible failures, insufficient logs, or missing credentials/permissions.

When escalating, state: (1) what was tried, (2) what is needed from the human, (3) expected response format.

## Immediate Termination vs Fallback

- Terminate: missing prerequisites the agent cannot fix (credentials, env vars, auth).
- Fallback: switch approaches when possible (e.g., unsupported tool → manual execution).

Do not debug unrecoverable errors.

## Debug Rules

- Proposed fix must touch only target file(s); no unrelated changes
- Minimal fixes; no refactoring or style changes
- If no edits are applied, provide verification command; if executed under execute, re-run before concluding
- If unfixable → output root cause + recommended actions
- Never return intermediate trial-and-error to the main session
- DIAGNOSIS SCOPE: a root-cause claim carries the evidence that discriminates
  it from at least one rival hypothesis, or is explicitly marked "most
  plausible, undiscriminated". Before concluding, check the failure means what
  you think: the reproduction must exercise the suspected mechanism, and a fix
  must toggle the symptom (fails without it, passes with it), not merely
  coincide with it.
- LEFTOVERS: observations gathered during diagnosis that the chosen cause does
  not explain are listed as "unattributed" in the output (an unexplained
  observation is a finding, not trial-and-error) — dropping them is where
  the next defect hides. Same-shape sites noticed along the way are named
  (naming is not fixing, and is not scope expansion).

## Debug Prompt Template

When SubAgent is unavailable, output this completed for the human to paste into a new session:

```
You are a debugging specialist. Diagnose and fix the error below.

Rules: only modify target file(s), minimal fix only, verify by re-running reproduction command, no intermediate output.

Error: {one-sentence description}
Reproduction: {exact command}
Error output: {error message/log}
Target file(s): {path(s) and relevant code}
Context: {language, framework, dependencies, OS, etc.}

Respond with ONLY:
(A) Fix — corrected file content or minimal diff + confirmation reproduction passes.
(B) Root Cause Report — root cause, what was tried, recommended next steps.
```

## Tool Naming

Use capability-based names for portability. The LLM maps to available tools at runtime.

| Capability | Examples |
|---|---|
| File read | Read, cat, file_read |
| File write | Write, file_write |
| File edit | Edit, sed, file_edit |
| Shell exec | Bash, Terminal, run_command |
| File search | Glob, find, list_files |

## Post-step checkpoint

After each step (SubAgent result, own investigation, or fix attempt), re-evaluate the Decision Rule from the top. If the outcome is "cannot reproduce" or "environment not inspectable", Escalate When applies.

