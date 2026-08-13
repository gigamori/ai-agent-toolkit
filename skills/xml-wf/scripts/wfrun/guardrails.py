"""Static text appended to every step prompt.

This is the only surviving descendant of the v1 defensive-prompt apparatus:
four rules aimed at the step agent (the orchestrator is Python and needs no
persuasion).

Rule 4 (the `DECISION:` channel) is injected on every step rather than opted
into per step, because the whole point is that forks are not foreseeable --
an attribute declaring "this step may need a ruling" could only ever be set
where someone already predicted the fork (xml-wf-decision-request.md §12).
Its firing barriers are inline for the same reason the fields are: a rule
that has to be recalled from elsewhere is a rule that decays.

Rule 4's `output:` requirement is stated in the positive ("when work-state is
complete the output line is REQUIRED"). The first wording said only "leave the
output line out entirely unless work-state is complete" -- a permission to omit,
with the requirement left implicit in the template line above it -- and a sonnet
step duly returned `work-state: complete` with no `output:` line (measured
2026-08-13, twice out of two in an E2E and 3 of 6 `complete` samples in the
eval; the rewording did not measurably move that rate). It is asked for because
form (a) adopts that value and cannot happen without it -- but the system no
longer depends on getting it: a payload that omits it degrades to a (b) re-run
under B_REASON_NO_OUTPUT instead of failing the run
(xml-wf-decision-request.md §1, §6, §12).

Rule 4 deliberately avoids the word "reply" and pivots on "final response":
each execution layer defines where the final response goes (run-cc: the
response itself; run-llm layer B: stepio.RESULT_PROTOCOL's result FILE, with
the reply channel carrying only a one-line OK/ERROR). An earlier wording said
"Reply with exactly these lines", and a layer-B subagent duly put the payload
into the reply channel and wrote no result file -- the request vanished as an
`aborted` (measured 2026-08-13 E2E; xml-wf-decision-request.md §12).
"""

GUARDRAILS = """\
---
You are an agent executing a single step within a workflow.
1. If an error blocks you, do NOT self-repair and do NOT fabricate substitute \
results. Return a concise technical report starting with "ERROR:" as your final \
response and stop. Recovery belongs to the orchestrator.
2. Do not write to any file that the task does not explicitly name.
3. Do not substitute, abbreviate, or summarize the deliverable. Produce exactly \
the artifacts at exactly the paths the task specifies.
4. If you reach a fork you may not settle alone -- two defensible readings, or \
the task simply silent -- do NOT quietly pick one. Fire only when you cannot \
proceed without a ruling: following a recommendation already recorded upstream \
is not a fork, and a blocking error is rule 1, not this. Make exactly these \
lines your final response and stop (deliver the final response wherever this \
prompt's response protocol says it goes):
DECISION: <one-line summary of the fork>
fork: <what is ambiguous>
options:
  1. <option> -- <the cost of choosing it wrongly>
  2. <option> -- <the cost of choosing it wrongly>
recommendation: <option number>
work-state: <complete|stopped>
output: <the value that becomes this step's output>
Replace each <...> with the value alone and never copy these explanations into \
it. Give two or more options, numbered from 1. recommendation takes an option \
number or the bare word none. work-state takes exactly one bare word: complete \
if the deliverable is already written and only its reading is open, stopped if \
you halted at the fork. When work-state is complete the output line is \
REQUIRED; when it is stopped, omit that line entirely. Refer to data by path; \
never paste file contents into the request."""

ASK_PROMPT = """\
Answer the following question based on facts. If it references file paths, \
actually read those files before judging.

Question: {question}"""

DEBUG_PROMPT = """\
You are a debugging agent diagnosing a failed workflow step.
Analyze the failure information below and decide an action:
- RETRY: only if one re-run with a concrete fix instruction is likely to \
succeed. Write the instruction (appended to the original task) in fix_instruction.
- FAIL: if the cause lies in the environment, input data, or workflow design — \
or whenever you are uncertain.

Never fabricate substitute results. Never perform the step's own task.

## Failed step definition
{step_xml}

## Prompt that was sent
{prompt}

## Execution result
exit_code: {exit_code}
{result}

## stderr
{stderr}"""

DEBUG_SCHEMA = """\
{"type":"object","properties":{"action":{"type":"string","enum":["RETRY","FAIL"]},\
"reason":{"type":"string"},"fix_instruction":{"type":"string"}},\
"required":["action","reason"]}"""

# The `decider="llm"` adjudicator (xml-wf-decision-request.md §15.2). Its role
# text is a constant rather than a .claude/agents definition on purpose: the
# escalation clause below is part of the contract, and a role file a user can
# swap is a role file that can drop it -- the one place fail-closed must not be
# editable (§5). adp.diagnose's discover_agents dependency is the deliberate
# counter-example; it also buys a failure mode (role missing -> FAIL) that
# adjudication does not want.
DECIDE_ROLE = """\
You adjudicate a fork raised by a workflow step. You did not do the step's \
work and you must not do it now: rule on the fork only.
You must NOT settle a fork whose consequences are irreversible, outward-facing \
(anything that leaves this machine or reaches a third party), or that changes \
what the workflow is trying to achieve. Escalate those to a human instead. \
Escalate whenever you are uncertain: a human ruling costs one round trip, a \
wrong autonomous ruling propagates downstream."""

# Structured output is forced here and only here. The human-facing answer file
# stays the §13.3 text format -- the caller renders this object into it -- so
# both deciders still land in decision.parse_answer, the single validity gate
# (§13.3, §15.2). Free-text generation would reintroduce the transcription trap
# §12 measured (a model copying a template's parenthetical into the value).
DECIDE_PROMPT = """\
## Decision request from step '{step_id}'

{request}

## Your ruling

Return one of:
- verdict "option" with option set to the number of the listed option you \
choose, and text saying why.
- verdict "none" if no listed option is right; text must say what to do \
instead, since the step re-runs on that text alone.
- verdict "escalate" if the escalation clause applies; text says why it does."""

DECIDE_SCHEMA = """\
{"type":"object","properties":{"verdict":{"type":"string",\
"enum":["option","none","escalate"]},"option":{"type":"integer"},\
"text":{"type":"string"}},"required":["verdict","text"]}"""

# The same adjudication, for an execution facility with no forced structured
# output (the pi backend -- xml-wf-decision-request.md §17.1). The ruling is
# written in the §13.3 answer format itself, so decision.parse_answer stays the
# single validity gate for every decider: schema where one exists, this shape
# where it does not.
#
# Three rules earn their place, all from measurement:
# - the template block carries `<...>` placeholders only, with every condition
#   after it, and states requirements in the positive: an explanation next to a
#   value gets transcribed with the value, and a requirement phrased as
#   permission-to-omit gets read as permission (§12, both measured).
# - "final response" rather than "reply": each execution layer defines where a
#   final response goes, and "reply" collided with a layer's own reply-channel
#   contract once already (§12).
# - nothing above the ruling and no quoting of the request. Prose above a
#   protocol line is measured behaviour (§16's preamble claim exists because a
#   step did it 3 times in 10), and here it would push the first line off the
#   anchor that decides between `option:` and `escalate:`. Quoting is worse
#   than untidy: the reply is classified before it reaches the adjudicator's
#   own parsing, so a quoted request can make the whole response read as a
#   fresh DECISION: request (§17.1).
DECIDE_PROMPT_TEXT = """\
## Decision request from step '{step_id}'

{request}

## Your ruling

Make your final response exactly one of these two shapes and nothing else.

To settle it:
option: <the number of the option you choose, or the bare word none>
<why -- one short paragraph>

To decline it:
escalate: <why the escalation clause applies>

Choose the second shape when the escalation clause applies, and whenever you \
are unsure. Use `option: none` only when no listed option is right: the step \
re-runs on your text alone, so say what to do instead. Start your final \
response with `option:` or `escalate:` -- write nothing above it, and do not \
quote the request back."""

ASK_SCHEMA = """\
{"type":"object","properties":{"answer":{"type":"boolean"},"reason":{"type":"string"}},\
"required":["answer","reason"]}"""
