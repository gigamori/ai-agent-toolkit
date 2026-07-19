---
name: debug-isolate
description: >
  Isolate iterative debugging in a forked subagent that inherits the current
  failure context. Use when multiple hypotheses, large error output, or root
  cause investigation is needed independent of the primary task.
disable-model-invocation: true
argument-hint: "[--first]"
allowed-tools: Bash(git stash *) Bash(git checkout *) Bash(git clean *) Bash(git status *) Bash(git diff *)
---

Delegate debugging of the current failure to a fork. This skill runs inline; its
body delegates to the Agent/Task tool with `subagent_type: "fork"`, passing the
fork directive below as the fork's prompt. That fork inherits the full conversation
history (the current failure, the reproduction command) plus this inline skill body.

Do not use `context: fork` (it never inherits conversation history, so the fork
would not see the current failure) or `isolation: worktree` (the checkpoint/rollback
below operates on the shared working tree via `git stash`).

## Decision rule

- `$ARGUMENTS` contains the exact flag token `--first`: fork immediately, even
  for the first failure in the session. Match only the standalone token
  `--first`; the word "first" appearing anywhere else in the arguments (e.g.
  inside a pasted error message or payload) does not count.
- Otherwise:
  1. First failure in the current session: fix inline without forking.
  2. Iterative debugging needed (a prior inline fix failed, multiple hypotheses,
     large error output): fork.
- Unresolvable by agent: escalate to the human (use the escalation format in the
  fork directive).

## Checkpoint (parent, before spawning)

The parent creates the checkpoint before spawning the fork and owns it for the
entire lifecycle:

```bash
CKPT="debug-checkpoint-$(date +%s)"
git stash push -u -m "$CKPT"
if git stash list | grep -qF "$CKPT"; then
  git stash apply   # restore the tree; the stash entry remains as the checkpoint
else
  CKPT="NONE"       # tree was clean, no stash created; HEAD is the checkpoint
fi
```

This preserves the working tree state (including uncommitted and untracked files)
as a stash entry while keeping the working tree unchanged. Record `$CKPT` and
substitute it for `<CKPT>` in the fork directive below. Always resolve the stash
ref by message (`git stash list | grep -F "<CKPT>"`), never as `stash@{0}` — the
index shifts when other stashes are created.

## Freeze the working tree

The fork shares this working tree. From spawning until the fork result is
validated, the parent must not modify the working tree — no file edits and no
state-changing git commands. Queue primary-task changes until validation is done;
the fork's rollback (`git checkout . && git clean -fd`) would destroy them.

## Fork directive

Pass the following, with `<CKPT>` substituted, as the fork's prompt:

---

Debug the current failure.

Checkpoint stash message: `<CKPT>`. Resolve its ref with
`git stash list | grep -F "<CKPT>"`. If it is `NONE`, the tree equals HEAD and
rollback needs no stash apply. Never create, drop, or overwrite this stash; the
parent owns it.

### Debug stance

- Assume the code is broken until proven otherwise
- Root-cause-first: diagnose before fixing. Never patch without understanding the cause
- Compare actual behavior against requirements/specs
- Hypothesize → test → fix (in that order)
- Consult official docs when uncertain about API/library behavior
- If the user demands an immediate fix ("直して", "just patch it"), still do
  root-cause analysis first

### Immediate termination vs fallback

- Terminate: missing prerequisites the agent cannot fix (credentials, env vars, auth)
- Fallback: switch approaches when possible (e.g., unsupported tool → alternative)
- Do not debug unrecoverable errors

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
Partial improvement (fewer failing tests) resets the consecutive counter, but
every attempt still counts toward the total cap below.

After 3 consecutive failures with no improvement:

```bash
git checkout . && git clean -fd
git stash list | grep -F "<CKPT>"   # → stash@{n}
git stash apply <that ref>          # skip when <CKPT> is NONE
```

Then reassess using all findings accumulated in context so far.
Repeat up to 3 revert cycles. Hard cap: 10 fix attempts in total regardless of
counter resets — when reached, stop and report as unresolved.

### Termination

Do NOT drop the checkpoint stash; the parent drops it after validation. Report
the checkpoint stash message (`<CKPT>`) in either case. Keep quoted output short:
at most ~20 lines of the most relevant error/verification output, never full logs.

**Resolved**. Report:
- Root cause
- What was fixed
- Changed files
- Reproduction result (repro command re-run: pass/fail + exit code / key output)
- Checkpoint stash message

**Unresolved after all revert cycles or at the attempt cap**. Report:
- What was tried and why it failed
- Hypotheses remaining
- Recommended next steps
- Checkpoint stash message

### Escalate when

The issue requires what the agent cannot obtain: physical/UI interaction,
environment the agent cannot inspect, visual verification, non-reproducible
failures, insufficient logs, or missing credentials/permissions.

When escalating, state: (1) what was tried, (2) what is needed from the human,
(3) expected response format.

---

## Receive and validate the fork result

The fork shares this working tree and can silently run as a contextless
general-purpose agent (it gets the directive but not the conversation, so it
does not know which failure to debug). Validate before trusting the result. The
parent owns the checkpoint stash in every branch below.

### Criterion

Pass only if the returned report ties back to the specific failure already known
in this session — it must name the actual reproduction command, the observed
error text, or the failing test/identifier, AND its reported reproduction result
corresponds to that failure. Fail if it is generic, asks which failure is meant,
references a symptom never observed here, or reports an unrelated reproduction
result.

### Branch

- Pass → use the result, then drop the checkpoint last:
  `git stash list | grep -F "$CKPT"` → `git stash drop <ref>` (skip when NONE).
- Fail → do NOT drop the checkpoint yet; it is the only restore point. First
  surface (via `git status` / `git diff`, do not auto-discard) any unexpected
  working-tree changes the fork left. If the tree must be restored:
  `git checkout . && git clean -fd && git stash apply <ref>`. Only once the tree
  is settled, re-delegate the fork exactly once with the same directive (reusing
  the same checkpoint), then re-apply the criterion.
- Fail again → escalate (state: the failure, that two forks did not inherit
  context, and the recommended next step). Settle the tree as above; drop the
  checkpoint only after the human decides. Do not loop further.
