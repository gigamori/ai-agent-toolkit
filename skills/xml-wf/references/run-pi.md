# Run mode (run-pi): execute the workflow on the pi CLI

Same batch execution as run-cc — **control flow is handled deterministically by
wfrun; never interpret the XML yourself and perform steps on its behalf** — but
each step, and each `ask=` judgment, runs as a `pi -p` call instead of a
`claude -p` one. Use it where claude is not available or not wanted; nothing in
this mode requires the claude CLI.

The procedure is run-cc's (validate → confirm parameters → execute → report).
Only the backend differs, and one flag selects it:

```bash
$WFRUN run <xml> -p key=value ... --backend pi --inherit-model <model>
```

`--backend` takes `auto` (the default), `cc`, or `pi`. `auto` reads
`CLAUDE_CODE_SESSION_ID`: set → `cc`, unset → `pi`. The detection is done in
code, not left to the caller to remember — a forgotten flag would otherwise
run the whole workflow on whichever CLI happened to be installed. An explicit
`cc`/`pi` overrides the probe, and a mismatch between the two prints a warning
without stopping.

`resume` does **not** re-detect. `run` records the resolved backend in
`<run-dir>/backend.json`, and `resume` reads it, so a run cannot execute its
first half on one CLI and its second on another. A run directory from before
this was tracked has no `backend.json` and resumes as `cc`.

`--inherit-model <model>` should also be given, with the model this session
is currently running as (a concrete identifier, not a canonical difficulty
class such as `haiku`/`opus` — it bypasses `model_map.json` and is used
as-is). Without it, a step with no `model=` of its own (no step attribute, no
role-frontmatter default) does not fall back to any documented default — pi
picks from whatever providers happen to be enabled in the local config, which
is per-machine, undocumented, and was observed in practice to land on a
completely different provider (`openai-codex/gpt-5.4-mini`) than the one this
very session was running under. A `note:` line at run start names which
step(s) would be affected. `resume` inherits `--inherit-model` the same way
it inherits `--backend`, via `<run-dir>/inherit_model.json` — there is no
override flag on `resume` itself.

## What is not available here

Two workflow features are **refused before any pi process starts**. Neither is
degraded silently: the run stops at startup and names the step.

### `schema=`

pi has no forced-structured-output flag. Prompting for JSON and parsing it
back would replace a guarantee with a likelihood, which is not what `schema=`
promises, so the workflow is rejected instead.

### `on-error="debug"`

Debug diagnosis has no pi implementation: it is built on the claude CLI's
structured output and finds its debug role in Claude Code's own config tree.
Left enabled it would fail every diagnosis while still looking like a working
feature.

Both rejections point at build mode for a compatible rewrite — see the two
sections below for what the rewrite looks like.

## `decider="llm"` works here

An llm decider settles a step's `DECISION:` fork in-process, so the run
continues unattended, exactly as under `--backend cc`. The difference is only
how the ruling is made machine-readable: the cc adjudicator is given a forced
output schema, and this one — having no such facility — is asked to write the
answer format itself:

```
option: <a number from the request's own list, or the bare word none>
<why>
```

or, to decline the fork:

```
escalate: <why the escalation clause applies>
```

Both go through the same validator a human's answer file goes through. Anything
else — prose above the ruling, an option number outside the list, `option: none`
with nothing after it, an empty or errored reply — settles nothing: the run
stops at the fork and a person answers it with `wfrun resume --answer`, the
same as `decider="human"`. The rejected text is kept beside the request as
`<request-id>_llm-attempt<NN>.md`, and the answer path is left empty for the
human.

`decider-model` resolves through the **`llm`** table of `model_map.json` (steps
resolve the same way), so it takes any model name your pi install accepts.

Because that name is free-form, `wfrun validate --backend pi` (and the
validation `wfrun run --backend pi` does first) checks it: every `model=` and
every adjudicator an `llm` decider would actually be sent is matched against
`pi --list-models`, and a name matching nothing fails validation
(`pi-model-unavailable`) instead of failing when the process launches. The
match mirrors pi's own resolver — exact on `id` or `provider/id`, else
substring on `id`, which is why the canonical `opus` reaches `opus[1m]`. Two
things it cannot see: pi also matches a model's display name, which
`--list-models` does not print, and the catalog does not reflect pi's
authenticated-only filter. Both make the check narrower than pi, so it never
rejects a name pi could resolve by id. An unreadable catalog is reported as
`pi-model-unverified`, not as a pass.

**This is prompt adherence, not an enforced format.** Without a schema to force
the shape, how often a given model produces a usable ruling is a measured
property of that model — see the sampling harness at
`scripts/evals/adjudicator_smoke.py`. Every failure is fail-closed (the fork
reaches a human), so the cost of a weak model here is round trips, not wrong
rulings.

## Replacing `schema=`

`schema=` is refused, so a step that needs a value downstream has to put that
value **in a file** and let `expect-file` police it.

| What `schema=` was doing | Rewrite |
|---|---|
| Handing one scalar to a later `test=` | Have the step write the value to a file, declare it in `expect-file=`, and pass the **path** downstream with `output-type="value"`. Branch on the file with `ask=`, or with a step that reads and checks it |
| Packing several properties into one variable | Split into one step (and one file) per property, each with its own `expect-file=` |
| Producing JSON as the deliverable itself | Write the JSON to a file, declare it in `expect-file=`, and let a following step parse it — a parse failure fails that step |

Worked example. Before, with `schema=` guaranteeing an integer:

```xml
<step id="s2_count" role="writer" output="line_count" output-type="value"
      schema='{"type":"object","properties":{"line_count":{"type":"integer"}},"required":["line_count"]}'>
  <task>Read the file {poem_path} and return its line count.</task>
</step>
<if test="int({line_count}) &gt;= 1"> ... </if>
```

After, with `expect-file` guaranteeing the artifact:

```xml
<step id="s2_count" role="writer" output="count_path" output-type="value"
      expect-file="output/line_count.txt">
  <task>Read the file {poem_path}, count its lines, and write ONLY that
        integer (no other text) to output/line_count.txt.
        Return only the relative path of the file you wrote.</task>
</step>
<if ask="Does the file {count_path} contain an integer of 1 or greater?"> ... </if>
```

**This is a reduction, not an equivalence.** `schema=` guaranteed the *shape*
of the output; `expect-file` guarantees only that the file *exists*. Nothing
checks that it holds an integer. That part moves to `ask=` — a likelihood, not
a guarantee — or to a verification step that reads the file and returns
`ERROR:` when it is malformed, which stops the run as a `guardrail`.

If a branch genuinely needs the value in a variable for `test=`, drop `schema=`
and use `output-type="value"` with "return only the number" in the task. Such a
step is also asked by the injected guardrails to emit a `VALUE:` line, which the
runner reads back (spec.md § Meaning of output-type) — your task sentence
reinforces that rather than being the only thing carrying it. But the line is
fail-open, not enforced: when it is missing the whole response body lands in the
variable and `int()` can fail at run time, so set `on-error` on that step so the
failure stops the run instead of propagating.

## Replacing `on-error="debug"`

A debug cycle did two things: diagnose the failure, then retry with the fix.
Pick whichever half the step actually needed.

| Instead of `on-error="debug"` | When it fits |
|---|---|
| `on-error="fail"` (the default) | The failure needs a human. Stop and let `wfrun resume` pick up after the fix |
| `retry=N` | The failure is transient. A plain retry covers most of what a debug-retry cycle achieved, without the diagnosis round trip |
| `on-error="ignore"` + a following verification step | The run should continue and the failure needs *recording* rather than fixing |

## Behaviour that differs from run-cc

These are not refusals — the workflow runs — but the guarantee is not the same,
so know them before relying on them.

- **`retry=` is doubled.** pi retries internally on its own (3 attempts,
  exponential backoff) before returning a failure to wfrun, and wfrun then
  applies `retry=` on top. Total attempts multiply. `wf.max` and `retry=`
  still bound the run, so it cannot run away — but budget the numbers with
  the doubling in mind.
- **A rate limit is not distinguishable from any other error.** pi reports
  errors as a provider's raw JSON without a field that reliably yields an
  HTTP status, so there is no `transient` class here: a 429 consumes `retry=`
  like a genuine failure would.
- **`budget-usd` mostly does not bite.** The canonical model names resolve to
  a bridge provider that pi has no price table for, so it reports real token
  counts with a cost of 0. `budget-usd` only constrains a run whose models are
  natively priced in pi.
- **`permission-mode` is ignored.** pi has no permission layer, so there is
  nothing to widen and nothing to deny. `--permission-mode` on the command
  line is accepted and has no effect.
- **`tools=` is enforced, and its names are translated.** `Read`, `Write`,
  `Edit`, `Grep`, `Bash` map to their lowercase pi equivalents and `Glob` maps
  to `find`. A name with no mapping — `MultiEdit`, `NotebookEdit`, `Task`,
  `Agent`, or a typo — **stops the step** rather than being dropped, because
  pi's own `--tools` ignores unknown names without a word and would otherwise
  hand the step a child with no tools at all. Unlike the Agent tool in
  run-llm, this restriction is real: a read-only step is genuinely read-only.
- **A CC-style argument specifier is widened, not matched.** pi's `--tools`
  has no per-command restriction, so an entry like `Bash(git:*)` cannot be
  honored as written. Rather than refusing the step (which would take away a
  tool it genuinely needs — git, in that example), the leading name is
  granted with no argument restriction at all — `Bash(git:*)` becomes the
  bare `bash` tool. This is silent nowhere: a `note:` line at run start names
  every step where this widening happened.
- **Steps start slower.** A cold `pi -p` call takes on the order of fifteen
  seconds before the model does any work. Size each step's `timeout=` with
  that startup included; a step that would finish in a minute under run-cc
  needs headroom here.

## Model names

`model=` resolves through `model_map.json`'s **`llm`** table (run-cc uses
`cc`). The bundled map is the identity and the canonical names — `haiku`,
`sonnet`, `opus` — resolve correctly on pi as they are, so no configuration is
needed. A map hand-edited to hold claude CLI names for some other purpose will
not resolve here; keep the identity map, or put names pi accepts in `llm`.
