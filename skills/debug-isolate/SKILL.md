---
name: debug-isolate
description: >
  Isolate iterative debugging in a forked subagent. Use when multiple hypotheses,
  large error output, or root cause investigation is needed independent of the
  primary task. Requires CLAUDE_CODE_FORK_SUBAGENT=1.
disable-model-invocation: true
allowed-tools: Bash(git stash *) Bash(git checkout *) Bash(git clean *)
---

Spawn a fork to debug the current failure. Do not use `context: fork` or
`isolation: worktree`. Rely on CLAUDE_CODE_FORK_SUBAGENT for fork generation.

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

**Resolved**: `git stash drop stash@{0}`. Report:
- Root cause
- What was fixed
- Changed files

**Unresolved after all revert cycles**: `git stash drop stash@{0}`. Report:
- What was tried and why it failed
- Hypotheses remaining
- Recommended next steps

---

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
