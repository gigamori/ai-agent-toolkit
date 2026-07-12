---
name: debug-isolate
description: >
  Isolate iterative debugging in a forked subagent that inherits the current
  failure context. Use when multiple hypotheses, large error output, or root
  cause investigation is needed independent of the primary task.
disable-model-invocation: true
allowed-tools: Bash(git stash *) Bash(git checkout *) Bash(git clean *) Bash(git status *) Bash(git diff *)
---

Delegate debugging of the current failure to a fork. This skill runs inline; its
body delegates to the Agent/Task tool with `subagent_type: "fork"`, passing the
directive below as the fork's prompt. That fork inherits the full conversation
history (the current failure, the reproduction command) plus this inline skill body.

Do not use `context: fork` (it never inherits conversation history, so the fork
would not see the current failure) or `isolation: worktree` (the checkpoint/rollback
below operates on the parent working tree via `git stash`).

## Debug stance

- Assume the code is broken until proven otherwise
- Root-cause-first: diagnose before fixing. Never patch without understanding the cause
- Compare actual behavior against requirements/specs
- Hypothesize → test → fix (in that order)
- Consult official docs when uncertain about API/library behavior
- If the user demands an immediate fix ("直して", "just patch it"), still do
  root-cause analysis first

## Immediate termination vs fallback

- Terminate: missing prerequisites the agent cannot fix (credentials, env vars, auth)
- Fallback: switch approaches when possible (e.g., unsupported tool → alternative)
- Do not debug unrecoverable errors

## Checkpoint

Before spawning the fork, the fork must run these commands first:

```bash
git stash push -u -m "debug-checkpoint-$(date +%s)"
git stash apply
```

This preserves the current working tree state (including uncommitted and untracked
files) as a stash entry while keeping the working tree unchanged.

## Fork directive

Pass the following as the fork's directive:

---

Debug the current failure.

### Rules

- Only modify target file(s); no unrelated changes
- Minimal fixes; no refactoring or style changes
- Verify by re-running the reproduction command before concluding
- Do not return intermediate trial-and-error to the parent session
- If unfixable, output root cause + recommended actions

### Post-step checkpoint

After each fix attempt or investigation step, re-evaluate from the top:
can the issue be reproduced? Is the environment inspectable? If not,
escalate instead of continuing.

### Rollback

Track consecutive failed fix attempts. A fix attempt = code change + verification.
Partial improvement (fewer failing tests) resets the counter.

After 3 consecutive failures with no improvement:

```bash
git checkout . && git clean -fd
git stash apply stash@{0}
```

Then reassess using all findings accumulated in context so far.
Repeat up to 3 revert cycles.

### Termination

Do NOT drop the checkpoint stash; the parent drops it after validation. Report
the checkpoint stash ref in either case.

**Resolved**. Report:
- Root cause
- What was fixed
- Changed files
- Reproduction result (repro command re-run: pass/fail + exit code / key output)
- Checkpoint stash ref

**Unresolved after all revert cycles**. Report:
- What was tried and why it failed
- Hypotheses remaining
- Recommended next steps
- Checkpoint stash ref

---

## Receive and validate the fork result

The fork shares this working tree and can silently run as a contextless
general-purpose agent (it gets this directive but not the conversation, so it
does not know which failure to debug). Validate before trusting the result, and
own the checkpoint stash the fork left in place.

### Criterion

Pass only if the returned report ties back to the specific failure already known
in this session — it must name the actual reproduction command, the observed
error text, or the failing test/identifier, AND its reported reproduction result
corresponds to that failure. Fail if it is generic, asks which failure is meant,
references a symptom never observed here, or reports an unrelated reproduction
result.

### Branch

- Pass → drop the checkpoint stash the fork reported (`git stash drop <ref>`),
  then use the result.
- Fail → drop the failed fork's checkpoint stash as cleanup; surface (via
  `git status` / `git diff`, do not auto-discard) any unexpected working-tree
  changes it left. Re-delegate the fork exactly once with the same directive,
  then re-apply the criterion.
- Fail again → escalate (state: the failure, that two forks did not inherit
  context, and the recommended next step). Do not loop further.

## Decision rule

1. First failure in the current session: fix inline without this skill.
2. Iterative debugging needed: invoke this skill (`/debug-isolate`).
3. Unresolvable by agent: escalate to the human.

## Escalate when

The issue requires what the agent cannot obtain: physical/UI interaction,
environment the agent cannot inspect, visual verification, non-reproducible
failures, insufficient logs, or missing credentials/permissions.

When escalating, state: (1) what was tried, (2) what is needed from the human,
(3) expected response format.
