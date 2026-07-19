# LLM orchestration mode (run-llm): execute the XML as a control plane

Alternative to `--run-cc` (wfrun batch execution). Use it for interactive
sessions where the user wants **supervision, permission, or intervention at
each step**. When deterministic guarantees matter, use `--run-cc`.

This protocol is platform-independent: any agent with wfrun (Python) and a
subagent facility (the Agent tool in Claude Code; the equivalent subtask
delegation elsewhere) can act as the orchestrator.

## Your role (read first — it conditions every decision below)

You are the **control plane**, not a processor of task content. The design
principle of this mode is: **no task content ever enters your context** — LLMs
have a goal-completion bias (seeing the goal invites skipping procedure and
pre-empting results), and task content is precisely its fuel. The only things
allowed through your context are **the control skeleton, step ids, file paths,
ok/error, and true/false**. All content flows through wfrun and files:

- Task instructions are assembled **into files** by `wfrun prompt` (you never read them)
- Results are written **into files** by subagents (you never read them)
- Variables are written **into vars.json** by `wfrun record` (you never read or write it)
- Conditions are judged by `wfrun eval` / `wfrun ask --quiet` (you never compute them)

## Preparation (unconditionally, in this order)

1. `$WFRUN validate <xml>` — on errors, stop and report (do not fix)
2. `$WFRUN plan <xml>` — this output (control skeleton: ids, agents, branches,
   retry/on-error) is the only knowledge of the workflow you are allowed.
   **Do not read the XML itself**
3. Create the run dir `runs/<name>_<ts>-llm/` and write `vars.json` with the
   resolved `<param>` values (ask the user for missing required ones) — the
   single moment you write values
4. Create an empty `steps.log`

## Step execution protocol (every step, these 4 moves in order)

```bash
# 1. Assemble (output = path + dispatch facts only; never look inside)
#    The dispatch line shows resolved values: role=<name>|inline, mode=<name>,
#    model=..., tools=... — model is already runner-resolved through
#    model_map.json ("model=X (mapped from Y)" when a mapping applied): pass
#    the resolved name verbatim, never translate it yourself.
#    The prompt file already contains the full role and
#    mode text (joined into one file: the Agent tool has no system-prompt
#    input, unlike run-cc's --append-system-prompt), so any generic subagent
#    will do; apply model/effort from the dispatch line when delegating
$WFRUN prompt <xml> <id> --vars vars.json \
    --out steps/<id>_prompt.md --result steps/<id>_result.md

# 2. Delegate (this fixed message is ALL you hand to the subagent; when the
#    dispatch line shows tools=..., include the second sentence verbatim with
#    that list — tool names are control facts, not task content)
"Read steps/<id>_prompt.md and execute its instructions. Use only these
 tools: <tools from the dispatch line>. The response protocol is inside the
 file. Reply to me with a single line starting with OK or ERROR."

# 3. Record (output = ok/error only; never look at the result body)
$WFRUN record <xml> <id> --result steps/<id>_result.md --vars vars.json --log steps.log

# 4. Report one line to the user ("<id>: ok/error", progress = steps.log lines / max), move on
```

## Evaluating control structures

- `test=` : `$WFRUN eval "<expr>" --vars vars.json` — branch only on the
  literal `true`/`false` it prints
- `ask=` : `$WFRUN ask "<question>" --vars vars.json --quiet --log steps.log` —
  same (the reason goes straight to the log file; you do not see it)
- `while`/`each` : repeat the full 4-move protocol every iteration. Stop and
  report when steps.log reaches the workflow's `max`

## `<replan>` nodes (dynamic continuation, one level deep)

1. `$WFRUN prompt <xml> <replan-id> --vars vars.json --out steps/<id>_prompt.md
   --result replans/<id>.xml` — assembles the **builder** prompt (same firewall:
   you never see it)
2. Delegate with the same fixed message (the builder role is inside the file)
3. Validate the generated continuation **programmatically**:
   `$WFRUN validate replans/<id>.xml --as-child --defined-vars vars.json`
   - errors → re-delegate with `--fix "<the validator error lines>"`, at most
     `retry` times, then follow on-error
4. `$WFRUN plan replans/<id>.xml` — the child control skeleton; execute it with
   this same protocol, counting its steps toward `max`, then continue after the
   replan node. Generated continuations must never contain another `<replan>`
   (the validator enforces this)

## On error (authority you do NOT have)

When record returns `error`: if `retry` remains, redo from move 1 (no fix).
When exhausted, follow `on-error` (shown by plan) — `fail` = stop and report /
`ignore` = move on / `debug` = delegate a diagnosis subagent with: "You are
the debug role defined in .claude/agents/debug.md — read that file and adopt
it. Then read steps/<id>_prompt.md and steps/<id>_result.md, diagnose, answer
RETRY or FAIL, and if RETRY write fix instructions to steps/<id>_fix.md". On
RETRY, redo from move 1 with `--fix "$(cat steps/<id>_fix.md)"`, **exactly once**.
**You have no authority to read result files, debug, devise workarounds, or
fabricate substitute results.**
Stopping is not failure in this mode — every trace is on disk and the user can
decide; that is the normal terminal state.

## Enforcement boundaries (what is NOT deterministic here)

Two of run-cc's structural guarantees degrade to prompt level in this mode;
know them, do not paper over them:

- **`tools=` is advisory.** The Agent tool has no per-call tool restriction,
  so the dispatch line's tools cannot be enforced on the subagent — the
  "Use only these tools" sentence in the fixed message is a likelihood lever
  only. Step `expect-file`/`schema` checks (`wfrun record`) still verify
  outcomes deterministically.
- **The no-read firewall is prompt-level.** Nothing mechanically stops the
  orchestrator from Reading prompt/result files. A partial deterministic
  backstop: add this PreToolUse hook to the project's `.claude/settings.json`
  (restart the session to load it; `wfrun prompt` prints a note when the
  marker is absent). It denies Read/Grep/Glob on the run's `vars.json` — the
  one file no agent ever legitimately reads through tools (wfrun accesses it
  in-process; step and debug subagents never touch it):

  ```json
  {
    "hooks": {
      "PreToolUse": [{
        "matcher": "Read|Grep|Glob",
        "hooks": [{
          "type": "command",
          "command": "uv run python -c 'import json,sys; d=json.load(sys.stdin); p=\" \".join(str(v) for v in (d.get(\"tool_input\") or {}).values()); b=\"-llm/vars.json\" in p; b and sys.stderr.write(\"xml-wf-llm-guard: run-llm vars.json is off-limits to agents\"); sys.exit(2 if b else 0)'"
        }]
      }]
    }
  }
  ```

  Prompt/result files stay hook-free by design: hooks fire for subagent tool
  calls too, and the step subagent must Read its prompt file (the debug
  subagent its result file) — a broader deny would break them. And any hook
  can be bypassed via Bash file reads, so this closes the *accidental* read
  path, not the deliberate one. The primary defense remains ⟦STEP-GATE⟧ below.

---

⟦STEP-GATE⟧ Immediately before issuing each delegation, verify three conditions —
(1) your previous output quotes the path line printed by `wfrun prompt`
(2) the delegation message is the fixed text above, containing no summary or
    paraphrase of task content
(3) you have not Read the prompt, result, or vars.json for this step
If any is missing, do not delegate — perform the missing move first.
"Checking the prompt file's content", "summarizing results", and "batching
multiple steps" are not diligence or efficiency; they are protocol violations.
