# Run mode (run-cc): validate, execute, report

**Control flow is handled deterministically by wfrun. Never interpret the XML
yourself and perform steps on its behalf.**

This is the **claude CLI backend** of batch execution: every step, debug
diagnosis and replan builder runs as an isolated `claude -p` call. `wfrun run`
selects it with `--backend cc`, and the default `--backend auto` picks it when
`CLAUDE_CODE_SESSION_ID` is set. Where claude is unavailable or unwanted, the
pi backend runs the same workflows with two exceptions it refuses at startup —
see `references/run-pi.md`.

## Procedure

### 1. Static validation
```bash
$WFRUN validate <xml>
```
If there are errors, report them and **stop** (do not fix them yourself —
fixing belongs to Build mode). List warnings and confirm with the user whether
to proceed.

### 2. Confirm parameters
Show the step tree and `<param>` list via `$WFRUN plan <xml>`. Confirm values
for `required` parameters with the user (skip if defaults cover everything).

### 3. Execute
```bash
$WFRUN run <xml> -p key=value ... --permission-mode acceptEdits --inherit-model <model>
```
- Add `--permission-mode acceptEdits` for workflows that write files. wfrun
  forwards it only to steps whose `tools=` can write — read-only steps
  (survey/review with e.g. `tools="Read,Grep,Glob"`) never see the widened
  permission, so restricting tools per step is worthwhile
- Add `--inherit-model <model>` with the model this session is currently
  running as (a concrete identifier, not a canonical difficulty class — it is
  used as-is). Without it, a step with no `model=` of its own (no step
  attribute, no role-frontmatter default) falls back to "claude configured
  default" rather than this session's model, and a `note:` line at run start
  names which step(s) did
- For long runs, start in the background and report progress by watching
  `status` / `step_count` / `cost_usd` in `runs/<name>_<ts>/state.json`

### 4. Report results
On success, report:
- Deliverable paths (files named in tasks, plus `runs/<ts>/outputs/`)
- Step count and total cost (state.json)
- Notable events (`while-max-reached`, `failed-ignored` steps, `replan`
  regeneration attempts — see events.jsonl; generated continuations are saved
  under `runs/<ts>/replans/`)

## On failure / resume (resume mode enters here)

1. Present `error` from `state.json` and the failing step's
   `runs/<ts>/steps/<id>_<n>/prompt.md` and `result.json` (key points of the tail)
2. Analyze the cause and offer the user options:
   - **Resume as-is** (transient failure): `$WFRUN resume runs/<ts>/`
   - **Fix then resume**: edit the failing task text in the run dir's
     `workflow.xml`, then resume (do not change already-succeeded step
     definitions; succeeded steps — including replan generations — are not re-run)
   - **Fix the source XML** (design problem): revise via Build mode and start a new run
3. Never re-run the same failure repeatedly without user instruction

Resume may need `--base-dir` (the directory agents/rules resolve against —
normally where the source XML lives). See `$WFRUN resume --help`.

`resume` takes no `--inherit-model` of its own: it reads the value the
original `run` recorded in `<run-dir>/inherit_model.json`, so a step that only
executes after the resume gets the same model resolution the run started with.
A run directory predating that file resumes as if none had been given.
