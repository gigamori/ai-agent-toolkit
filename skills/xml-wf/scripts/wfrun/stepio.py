"""Step prompt assembly and result recording, shared by the deterministic
executor and the LLM-orchestrator helper commands (wfrun prompt / record).

In run-llm mode these functions are the content firewall: task bodies and
step outputs flow XML -> prompt file -> subagent -> result file -> vars.json
entirely inside Python, so the orchestrating LLM's context carries only step
ids, file paths, and ok/error signals.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import ccdeny, model, modes
from . import decision as decision_mod
from .agents import AgentDef, discover_agents
from .guardrails import (GUARDRAILS, VALUE_LINE_PLACEHOLDER,
                         VALUE_LINE_PREFIX, VALUE_LINE_RULE)
from .interp import InterpError, interpolate


class StepIOError(Exception):
    pass


def write_text_atomic(path: str | Path, text: str) -> None:
    """Write `text` such that a concurrent reader never observes a partial
    file: write a sibling temp file, then `os.replace` onto the target
    (atomic on both POSIX and Windows).

    Required for the A layer's cross-process files: `wait` polls exit.json
    from another process while the wrapper is writing it (a torn read used
    to crash `wait` outright), and `dispatch` reads attempts.json while a
    finishing wrapper appends to it -- a torn attempts.json reads back as
    "no attempts yet" through `load_attempts`'s error fallback, silently
    resetting the runaway cap."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _missing_expected(expect_file: str, variables: dict, step_id: str,
                      base_dir: str | Path | None) -> list[str]:
    """expect-file paths (comma-separated, {var}-interpolated) that do not
    exist. Relative paths resolve against `base_dir` when given -- the same
    directory the step's subprocess ran in -- mirroring
    executor._missing_expected. With base_dir=None they resolve against the
    caller's cwd (the B layer's documented "orchestrator cwd = subagent
    cwd" premise)."""
    paths = interpolate(expect_file, variables)
    missing = []
    for part in (p.strip() for p in paths.split(",")):
        if not part:
            continue
        path = Path(part)
        if not path.is_absolute() and base_dir is not None:
            path = Path(base_dir) / path
        if not path.is_file():
            missing.append(part)
    return missing


def sentinel_line(step_id: str, key: str | None = None) -> str:
    """The completion marker a run-llm step must write as the LAST non-empty
    line of its result file (reliability-spec.md §4.1). Absence of this line
    is what lets `record`/`poll` tell "still writing" or "died mid-write"
    apart from "wrote a genuinely short/empty answer" -- the well-formedness
    of a <replan> continuation's own XML already proves completeness, so
    replans are deliberately exempted (reliability-spec.md §0, §4.1).

    `key` names one attempt (its cycle, sequence and token). A keyed marker
    completes that attempt only: a late write by an earlier attempt carries
    the earlier key, so it can never pass for a later attempt's result."""
    if key is None:
        return f"[[WFRUN-END step={step_id}]]"
    return f"[[WFRUN-END step={step_id} attempt={key}]]"


# Random bytes in a delegation description and an attempt key: the step id,
# cycle and sequence repeat across runs and sessions, so only the token keeps
# one attempt's subagent and sentinel apart from another's.
DELEGATION_TOKEN_BYTES = 4


def delegation_description(step_id: str, cycle: int, seq: int, token: str) -> str:
    """The description a run-llm layer-B step attempt is delegated under.

    On Claude Code the orchestrator passes it verbatim as the Agent call's
    description, which is how `record` finds that subagent's transcript
    (ccdeny). Elsewhere it is printed and ignored."""
    return f"wfrun {step_id} c{cycle:02d} a{seq:02d} {token}"


def strip_sentinel_line(text: str, step_id: str,
                        sentinel: str | None = None) -> tuple[str, bool]:
    """Drop the trailing sentinel line, if the last non-empty line of `text`
    is exactly `sentinel` (default: the unkeyed `sentinel_line(step_id)`).
    Returns (text, present). Any other marker -- another step's, another
    attempt's -- counts as absent.

    Call this (and modes.strip_mode_line) before any value extraction or
    file-content check: the sentinel is protocol furniture, not part of the
    step's actual output (reliability-spec.md §4.2)."""
    expected = sentinel if sentinel is not None else sentinel_line(step_id)
    lines = text.splitlines()
    idx = len(lines) - 1
    while idx >= 0 and lines[idx].strip() == "":
        idx -= 1
    if idx >= 0 and lines[idx].strip() == expected:
        return "\n".join(lines[:idx]), True
    return text, False


def handle_path(step_id: str, result_path: str | Path) -> Path:
    """Sibling `<id>_handle.json` next to a step's result file (written by
    `wfrun prompt --result`, read by `wfrun record`/`wfrun poll`;
    reliability-spec.md §4.1/§4.3). It names the step's current attempt.
    Its absence means a pre-Phase-2.2 ("legacy") caller: no sentinel/aborted
    machinery applies then."""
    return Path(result_path).parent / f"{step_id}_handle.json"


# Keys every per-attempt handle carries. A handle without them was written by
# an older wfrun, which kept one result file per step.
ATTEMPT_HANDLE_KEYS = ("step_id", "cycle", "attempt", "token", "result_path",
                       "sentinel")


def is_attempt_handle(handle) -> bool:
    return isinstance(handle, dict) and all(k in handle for k in ATTEMPT_HANDLE_KEYS)


def attempt_ledger_path(steps_dir: str | Path, step_id: str, cycle: int) -> Path:
    """The layer-B attempt ledger of one cycle (one step-node visit), the same
    name the A layer gives its own: `steps/<id>_cNN_attempts.json`."""
    return Path(steps_dir) / f"{step_id}_c{cycle:02d}_attempts.json"


def load_handle(step_id: str, result_path: str | Path) -> dict | None:
    path = handle_path(step_id, result_path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# The DECISION: sentence closes a measured loss path (2026-08-13 E2E): the
# reply vocabulary is OK/ERROR and a decision is neither, so without an
# explicit rule a subagent that raised one had nothing valid to reply -- one
# put the whole payload into the reply channel, wrote no file, and the
# request vanished as `aborted`. `OK {step_id}` adds no new vocabulary (the
# delegation message and record's claim parsing are untouched), and the file
# is authoritative anyway: record answers a DECISION: file with exit 4
# regardless of the claim.
RESULT_PROTOCOL = """\
## Response protocol
Write your full final response to this file: {result_path}
The LAST non-empty line of that file must be exactly this marker, with \
nothing after it: {sentinel}
Your reply to the caller must be a single line starting with "OK {step_id}" \
or "ERROR:". A DECISION: request is a final response like any other: write \
it to the file above and reply "OK {step_id}"."""

DECISION_RESOLVED_HEADER = """\
## Decisions resolved
Earlier attempts at this step raised decision request(s). All of them have
been answered. Every settled pair is listed -- also the earlier ones, so no
already-settled fork looks open again."""

DECISION_RESOLVED_PAIR = """\
### Request {n}
{request}

### Answer {n}
{answer}"""

DECISION_RESOLVED_FOOTER = ("Continue the step under these decisions. Do not "
                            "re-raise any fork settled above.")


def format_decisions(pairs: list[tuple[str, str]]) -> str:
    """The `## Decisions resolved` block for a form-(b) re-run: header, every
    (request, answer) pair in adjudication order, footer (§13.6)."""
    blocks = [DECISION_RESOLVED_HEADER]
    blocks += [DECISION_RESOLVED_PAIR.format(n=n, request=request.strip(),
                                             answer=answer.strip())
               for n, (request, answer) in enumerate(pairs, start=1)]
    blocks.append(DECISION_RESOLVED_FOOTER)
    return "\n\n".join(blocks)

REPLAN_PROMPT = """\
You are a continuation planner inside a running workflow. Based on the results
so far, produce the workflow XML for what should happen next. You plan; you do
not execute any of the work yourself.

## Contract (a validator rejects violations; you would then be asked to fix them)
- Output MUST be a single `<workflow name="..." version="2" max="N">` document
  with N <= {max_steps}.
- A step MAY take a role: either role="<name>" using ONLY these named
  definitions: {roles} — or an inline <role> child you author yourself. Give
  one only when WHO does the work changes the outcome; when mode= already
  fixes the discipline, omit it rather than inventing a filler persona.
- A step MAY set mode= (processing discipline). If used, it MUST be one of:
  {modes}. Set it where the discipline matters: `execute` for strict
  do-exactly-this operations, `survey` for fact collection, `debug` for
  diagnosis; steps without an obvious discipline need no mode.
- The XML format specification: read {spec_path} before writing.
- The document MUST NOT contain `<replan>` or `<param>` elements
  (this is a one-level continuation; variables are inherited).
- You may reference the existing variables below as {{name}} placeholders.{outputs_clause}{constraint_clause}

## Current variables (name: value)
{variables}

## Planning task
{task}

## Output contract
{output_contract}"""

# The backend constraint the pi Executor adds to the contract list above
# (design phase6-run-pi-design.md §10.2 point 3). Kept here, next to the
# prompt it is spliced into, rather than in executor.py: it is prompt text.
# It is passed in rather than derived here because REPLAN_PROMPT has no
# backend of its own -- `wfrun prompt` builds it for run-llm, where neither
# attribute is refused, and that path must stay byte-identical.
REPLAN_PI_CONSTRAINT = ('The document MUST NOT use `schema=` or '
                        '`on-error="debug"` on any step (this execution '
                        'backend supports neither; write a value to a file '
                        'and declare it in expect-file, and handle failures '
                        'with on-error="fail" / retry=N / on-error="ignore").')

REPLAN_OUTPUT_INLINE = ("Reply with ONLY the workflow XML. "
                        "No code fences, no commentary, no explanation.")
REPLAN_OUTPUT_FILE = ('Write ONLY the workflow XML (no code fences, no commentary) '
                      'to this file: {result_path}\n'
                      'Your reply to the caller must be a single line starting '
                      'with "OK" or "ERROR:".')


def load_rules(wf: model.Workflow, base_dir: str | Path) -> dict[str, str]:
    base_dir = Path(base_dir)
    cache = {}
    for rules in wf.rules.values():
        if rules.src:
            path = Path(rules.src)
            if not path.is_absolute():
                path = base_dir / path
            if not path.is_file():
                raise StepIOError(f"rules '{rules.id}': file not found: {path}")
            cache[rules.id] = path.read_text(encoding="utf-8").strip()
        else:
            cache[rules.id] = rules.text or ""
    return cache


def find_step(wf: model.Workflow, step_id: str) -> model.Step:
    for step in wf.iter_steps():
        if step.id == step_id:
            return step
    raise StepIOError(f"step '{step_id}' not found in workflow '{wf.name}'")


def resolve_role(node, agents_cache: dict[str, AgentDef]
                 ) -> tuple[str | None, AgentDef | None]:
    """The role body injected into the prompt: an inline <role> as-is, or the
    body of the named .claude/agents definition (returned for dispatch too).

    Role is optional: (None, None) when the node declares neither form. The
    caller then omits the <role> block — and, for a step, uses the role-less
    framework header."""
    if node.role_text:
        return node.role_text, None
    if not node.role:
        return None, None
    agent = agents_cache.get(node.role)
    if agent is None:
        raise StepIOError(
            f"step '{node.id}': role '{node.role}' not found in .claude/agents "
            "(project) or the user agents dir ($CLAUDE_CONFIG_DIR or ~/.claude)/agents")
    return agent.prompt, agent


def dispatch_for(node, agents_cache: dict[str, AgentDef]
                 ) -> tuple[str | None, str | None]:
    """(model, tools) for the claude call: step attributes win over the named
    role's frontmatter; an inline role has only the step attributes.
    (<replan> has no tools attribute — its tool set is fixed by the executor.)"""
    agent = agents_cache.get(node.role) if node.role else None
    return (node.model or (agent.model if agent else None),
            getattr(node, "tools", None) or (agent.tools if agent else None))


def _role_and_mode_parts(node, agents_cache: dict[str, AgentDef]) -> list[str]:
    """Framework header + <role> block when a role is declared (the header's
    role-less variant and no block when it is not), + mode declaration/body/
    _common when mode= is set."""
    role_body, _ = resolve_role(node, agents_cache)
    parts = [modes.meta_text(with_role=role_body is not None)]
    if role_body is not None:
        parts.append(f"<role>\n{role_body}\n</role>")
    mode = getattr(node, "mode", None)
    if mode:
        try:
            parts.append(modes.mode_block(mode))
        except modes.ModeError as e:
            raise StepIOError(f"step '{node.id}': {e}") from e
    return parts


def value_output_rule_applies(step: model.Step) -> bool:
    """Whether this step gets guardrails.VALUE_LINE_RULE (rule 6).

    Three conditions, all read off the declaration alone
    (xml-wf-decision-request.md §18.6): the step sets a variable at all, that
    variable is value-typed, and no `schema=` is in force. The schema exclusion
    is not an optimisation -- a schema forces a JSON final response, so a line
    template would be a second, contradictory shape in the same prompt, and
    unwrap_value's structured branch already lands a scalar without one.

    Injecting on a declared attribute does not reopen §12's all-steps rule: that
    rule is about rule 5, and rests on forks being *unforeseeable*, which an
    attribute already written in the XML is not.
    """
    return bool(step.output) and step.output_type != "file" and not step.schema


def build_step_prompt_parts(wf: model.Workflow, step: model.Step, variables: dict,
                            base_dir: str | Path, fix: str | None = None,
                            rules_cache: dict[str, str] | None = None,
                            agents_cache: dict[str, AgentDef] | None = None,
                            result_path: str | None = None,
                            decision: list[tuple[str, str]] | None = None,
                            sentinel: str | None = None
                            ) -> tuple[str, str]:
    """(system_text, user_text) for a step.

    system = framework header + role (when declared) + mode/_common + rules —
    the constraint layers, placed in the high-authority channel by run-cc.
    user = the interpolated task (+ fix, + resolved decisions), response
    protocol, guardrails, and — only where value_output_rule_applies — rule 6.
    run-llm joins the two (the Agent tool has no system-prompt input).

    `decision` is every settled (request body, answer body) pair of the
    current cycle, for a form-(b) re-run. It rides the USER channel next to
    `fix`, never the system one: an answer is task input, and promoting it
    would let it outrank the mode and rules layers
    (xml-wf-decision-request.md §13.6).
    """
    if rules_cache is None:
        rules_cache = load_rules(wf, base_dir)
    if agents_cache is None:
        agents_cache = discover_agents(base_dir)
    sys_parts = _role_and_mode_parts(step, agents_cache)
    for rid in step.rules:
        if rid not in rules_cache:
            raise StepIOError(f"step '{step.id}': rules id '{rid}' undefined")
        sys_parts.append(f'<rules id="{rid}">\n{rules_cache[rid]}\n</rules>')
    try:
        task = interpolate(step.task, variables)
    except InterpError as e:
        raise StepIOError(f"step '{step.id}': {e}") from e
    if fix:
        task += f"\n\n## Fix instructions for the previous failure\n{fix}"
    if decision:
        task += "\n\n" + format_decisions(decision)
    user_parts = [task]
    if result_path:
        user_parts.append(RESULT_PROTOCOL.format(
            result_path=result_path, step_id=step.id,
            sentinel=sentinel or sentinel_line(step.id)))
    user_parts.append(GUARDRAILS)
    if value_output_rule_applies(step):
        user_parts.append(VALUE_LINE_RULE)
    return "\n\n".join(sys_parts), "\n\n".join(user_parts)


def build_step_prompt(wf: model.Workflow, step: model.Step, variables: dict,
                      base_dir: str | Path, fix: str | None = None,
                      rules_cache: dict[str, str] | None = None,
                      agents_cache: dict[str, AgentDef] | None = None,
                      result_path: str | None = None,
                      decision: list[tuple[str, str]] | None = None,
                      sentinel: str | None = None) -> str:
    """Single combined prompt (run-llm / wfrun prompt): system part + user part."""
    system, user = build_step_prompt_parts(
        wf, step, variables, base_dir, fix=fix, rules_cache=rules_cache,
        agents_cache=agents_cache, result_path=result_path, decision=decision,
        sentinel=sentinel)
    return f"{system}\n\n{user}"


def default_spec_path() -> Path | None:
    """references/spec.md relative to the bundled skill layout, if present."""
    path = Path(__file__).resolve().parents[2] / "references" / "spec.md"
    return path if path.is_file() else None


def strip_fences(text: str) -> str:
    """Remove a wrapping markdown code fence if the model added one anyway."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    return text


def build_replan_prompt_parts(node, variables: dict,
                              agents_cache: dict[str, AgentDef],
                              fix: str | None = None,
                              result_path: str | None = None,
                              constraint: str | None = None) -> tuple[str, str]:
    """(system_text, user_text) for a replan builder call: role in the system
    part, the planning contract + variables + task in the user part.

    A replan carries no mode, so it gets no framework header and no _common —
    only the role. Role is optional: without one the system part is empty, and
    both backends then omit the append-system-prompt flag entirely.

    `constraint` is one extra contract bullet the caller knows about and this
    builder does not — today only the pi Executor's REPLAN_PI_CONSTRAINT
    (design §10.2 point 3). Omitted, the prompt is byte-identical to what it
    was before the parameter existed, which is what keeps the cc path and
    `wfrun prompt` (run-llm) unchanged."""
    try:
        task = interpolate(node.task, variables)
    except InterpError as e:
        raise StepIOError(f"replan '{node.id}': {e}") from e
    if fix:
        task += f"\n\n## Validation errors from your previous attempt\n{fix}"
    spec = default_spec_path()
    outputs_clause = ""
    if node.outputs:
        outputs_clause = ("\n- The continuation MUST define these output "
                          f"variables: {', '.join(node.outputs)}")
    role_body, _ = resolve_role(node, agents_cache)
    roles = ", ".join(sorted(agents_cache)) if agents_cache else "(none available)"
    user = REPLAN_PROMPT.format(
        max_steps=node.max_steps,
        roles=roles,
        modes=", ".join(m for m in modes.available_modes()
                        if m not in modes.MODE_ALIASES),
        spec_path=str(spec) if spec else "(spec file unavailable — follow the examples in the task)",
        outputs_clause=outputs_clause,
        constraint_clause=f"\n- {constraint}" if constraint else "",
        variables=json.dumps(variables, ensure_ascii=False, indent=2),
        task=task,
        output_contract=(REPLAN_OUTPUT_FILE.format(result_path=result_path)
                         if result_path else REPLAN_OUTPUT_INLINE),
    )
    system = "" if role_body is None else f"<role>\n{role_body}\n</role>"
    return system, user


def build_replan_prompt(node, variables: dict,
                        agents_cache: dict[str, AgentDef],
                        fix: str | None = None,
                        result_path: str | None = None) -> str:
    system, user = build_replan_prompt_parts(node, variables, agents_cache,
                                             fix=fix, result_path=result_path)
    return f"{system}\n\n{user}" if system else user


# What extract_value_line found, for the caller's report/event. Three values,
# not two: a transcribed placeholder is neither a value nor a plain absence, and
# collapsing it into either hides the one failure mode §12 measured twice
# (a model copying a template's own filler text).
VALUE_LINE_PRESENT = "present"
VALUE_LINE_ABSENT = "absent"
VALUE_LINE_PLACEHOLDER_MARK = "placeholder"


def extract_value_line(text: str) -> tuple[str | None, str]:
    """(value, marker) for guardrails.VALUE_LINE_RULE's line, or (None, ...).

    Syntactic only, which is what keeps it on the right side of §6's ban on
    value validation in code: the prefix must start a line and match exactly,
    and nothing here judges whether what follows *looks like* a value.

    The LAST match wins. Rule 6 asks for the line without fixing its position
    (so the run-llm sentinel can stay the final line), and "the final response
    is what counts" is the semantics everywhere else in this file -- a step that
    quotes the template while explaining itself and then writes the real line
    lands on the real one.

    A verbatim placeholder demotes to (None, "placeholder"). Comparing against
    the exact string this prompt asked for is self-identity, not a heuristic
    about the value; storing `<the value>` downstream would reproduce the silent
    contamination of §18.6 while *claiming* a value was extracted.
    """
    found = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(VALUE_LINE_PREFIX):
            found = stripped[len(VALUE_LINE_PREFIX):].strip()
    if found is None:
        return None, VALUE_LINE_ABSENT
    if found == VALUE_LINE_PLACEHOLDER:
        return None, VALUE_LINE_PLACEHOLDER_MARK
    return found, VALUE_LINE_PRESENT


def unwrap_value_marked(structured, text: str) -> tuple[object, str | None]:
    """unwrap_value plus what the value-line extractor saw (None when the
    structured path was taken and the line never applied)."""
    if isinstance(structured, dict) and len(structured) == 1:
        return next(iter(structured.values())), None
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False), None
    body = modes.strip_mode_line(text).strip()
    value, marker = extract_value_line(body)
    # fail-open: no usable line means the pre-rule-6 behaviour, never a failure.
    # Steps written before this rule existed (task bodies saying "return only
    # the path") keep working unchanged, and the deviation is reported rather
    # than swallowed (§18.6).
    return (value if value is not None else body), marker


def value_line_suffix(marker: str | None) -> str:
    """The report suffix naming what the VALUE: line did, or "" when it said
    nothing worth reporting.

    Content-free by construction: it names the shape the extractor saw and
    never the value, which is the same line _with_stray_warning walks ("line
    numbers and prefixes, never content") and is what keeps it compatible with
    run-llm's no-task-content design.
    """
    if marker == VALUE_LINE_ABSENT:
        return "; no value line"
    if marker == VALUE_LINE_PLACEHOLDER_MARK:
        return "; value line is the unreplaced placeholder"
    return ""


def unwrap_value(structured, text: str):
    """output-type=value extraction: single-property objects unwrap to the
    scalar; other structured results store as JSON text; plain text yields the
    VALUE: line when the step wrote one, else the whole body (after dropping
    the [Mode: ...] protocol line _common.md mandates)."""
    return unwrap_value_marked(structured, text)[0]


def _log_status(status: str) -> str:
    """steps.log entry status: aborted joins the pre-existing success/error
    (reliability-spec.md §4.2); `decision` joins them in turn
    (xml-wf-decision-request.md §8)."""
    return {"ok": "success"}.get(status, status)


def decisions_dir_for_result(result_path: str | Path) -> Path:
    """`decisions/` beside the directory holding the step's result file.

    run-llm keeps results in `<run>/steps/`, so this lands on
    `<run>/decisions/` — the same place run-cc writes them. It is kept
    outside `steps/`, where each attempt has its own result file, so the
    request a form-(b) re-run has to quote back has one place across
    attempts (xml-wf-decision-request.md §14.1).
    """
    return Path(result_path).parent.parent / "decisions"


def persist_decision_request(text: str, dec_dir: str | Path, prefix: str) -> str:
    """Copy a `DECISION:` payload into the decisions ledger. Returns its id."""
    dec_dir = Path(dec_dir)
    dec_dir.mkdir(parents=True, exist_ok=True)
    rid = decision_mod.allocate_request_id(dec_dir, prefix)
    (dec_dir / f"{rid}{decision_mod.REQUEST_SUFFIX}").write_text(
        text.strip(), encoding="utf-8")
    return rid


def _decision_message(text: str, dec_dir: str | Path, prefix: str) -> str:
    """The `record`/`wait` verdict line for a `DECISION:` response.

    Content-free by construction, like the guardrail/refusal messages beside
    it: the payload never enters the orchestrator's context, only its path
    does (run-llm.md's no-task-content design). Valid and malformed are told
    apart because they need different human actions -- answer it, or go read
    why it cannot be answered (xml-wf-decision-request.md §8).

    The cap is enforced here rather than by the orchestrator: counting settled
    rulings is a fact about the ledger, and run-llm's standing rule is that the
    LLM carries paths and verdicts while code does the arithmetic (§15.7). The
    orchestrator only has to follow the sentence it is handed.
    """
    body = modes.strip_mode_line(text)
    claimed, _preamble = decision_mod.claim_decision_body(body)
    # File the anchored slice, not the preamble: the request file is the
    # numbering authority the answer selects against (§1), so it has to be the
    # parseable payload and nothing else. The full response stays in the
    # result file for audit. A first-token body passes through unchanged,
    # malformed or not (D9).
    body = (claimed if claimed is not None else body).strip()
    rid = persist_decision_request(body, dec_dir, prefix)
    request = Path(dec_dir) / f"{rid}{decision_mod.REQUEST_SUFFIX}"
    _, errors = decision_mod.parse_payload(body)
    if errors:
        if decision_mod.looks_like_completion_report(body):
            # A claim about the payload's SHAPE, not its content -- the same
            # category as _with_stray_warning's line numbers and prefixes, so
            # the no-task-content firewall holds (§18.3).
            return ("decision: step wrapped a completion report in the "
                    "decision channel -- the payload declares `work-state:` "
                    "but names no fork (no `fork:` / `options:` / "
                    "`recommendation:`), so there is nothing to adjudicate; "
                    f"read it and decide by hand (request: {request})")
        return ("decision: step raised a decision request, but its payload is "
                f"malformed and cannot be answered as-is ({len(errors)} field "
                f"problem(s)); read it and decide by hand (request: {request})")
    answer = Path(dec_dir) / f"{rid}{decision_mod.ANSWER_SUFFIX}"
    capped = (decision_mod.llm_adjudications(dec_dir, prefix)
              >= model.DECISION_LLM_CAP)
    who = (" an llm decider has already settled "
           f"{model.DECISION_LLM_CAP} request(s) here, so this one is for a "
           "person to answer (add --decider human);" if capped else "")
    return (f"decision: step requested adjudication (request: {request});{who} "
            f"write the ruling to {answer}, then re-run record with "
            f"--answer {answer}")


def _with_stray_warning(message: str, text: str, result_path) -> str:
    """Append the D9 stray-token warning to an ok verdict, when one applies.

    Metadata only -- line numbers and prefixes, never content -- so it stays
    inside run-llm's no-task-content design while making the pass-through
    visible (D9 4-4: ERROR:/[BLOCKED: keep first-token classification, and a
    DECISION: line whose tail does not parse is ambiguous evidence; neither
    may fail a success, but neither may pass in silence either).
    """
    strays = decision_mod.stray_protocol_lines(modes.strip_mode_line(text))
    if not strays:
        return message
    detail = ", ".join(f"'{prefix}' at line {number}"
                       for number, prefix in strays[:5])
    return (f"{message}; warning: the response carries a line-anchored "
            f"protocol token it did not open with ({detail}); it was NOT "
            f"reclassified -- read {result_path} if this step should have "
            "stopped")


def _append_log(log_path, step: model.Step, status: str, result_path,
                deny_check: str | None = None):
    if not log_path:
        return
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "step": step.id,
             "status": _log_status(status),
             "output_var": step.output, "result_file": str(result_path)}
    if deny_check is not None:
        entry["deny_check"] = deny_check
    with Path(log_path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_attempt(attempts_path: str | Path, seq, status: str,
                   fields: dict | None = None):
    """Append one {seq, class, ended_at} entry to a JSON-array attempts file
    (reliability-spec.md §4.2/§5.1). Shared by run-llm's `record` (the B
    layer, via `_append_attempt` below) and the A layer's dispatch wrapper
    (`wfrun _wrapper`, §5.1), which is also what `wfrun dispatch` reads back
    to enforce the attempt cap (§5.1, F4/P5).
    `fields` adds to the entry (the B layer's attempt token)."""
    attempts_path = Path(attempts_path)
    try:
        attempts = json.loads(attempts_path.read_text(encoding="utf-8"))
        if not isinstance(attempts, list):
            attempts = []
    except (OSError, json.JSONDecodeError):
        attempts = []
    attempts.append({"seq": seq, "class": status, "ended_at": time.time(),
                     **(fields or {})})
    write_text_atomic(attempts_path,
                      json.dumps(attempts, ensure_ascii=False, indent=2))


def load_attempts(attempts_path: str | Path) -> list:
    try:
        attempts = json.loads(Path(attempts_path).read_text(encoding="utf-8"))
        return attempts if isinstance(attempts, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def update_attempt(attempts_path: str | Path, seq, fields: dict) -> bool:
    """Merge `fields` into the entry for attempt `seq`; False when the ledger
    has none. The A layer's `wait` records here what it learns after the
    wrapper wrote the entry -- a verdict its own checks changed, and the debug
    diagnosis -- because the ledger is all `dispatch` reads."""
    attempts = load_attempts(attempts_path)
    for entry in reversed(attempts):
        if isinstance(entry, dict) and entry.get("seq") == seq:
            entry.update(fields)
            write_text_atomic(attempts_path,
                              json.dumps(attempts, ensure_ascii=False, indent=2))
            return True
    return False


def record_aborted_attempt(step: model.Step, attempts_path: str | Path, seq,
                           log_path: str | Path | None, result_path,
                           fields: dict, deny_check: str | None = None) -> bool:
    """Record one aborted attempt to the run's step log and ledger
    (reliability-spec.md §5.4.3). The A layer's dispatch/wait in
    `wfrun.__main__` call it once they judge an attempt gone. The B layer's
    own aborted path (`_record_file`) does not come through here; its `record`
    uses this function for two cases only: an attempt whose result completed
    after its stop window closed, which is recorded aborted rather than
    adopted, and an attempt whose stop ended without a completion, which is
    recorded aborted whatever reply came with it, the refusal check's verdict
    going into the log as `deny_check`.

    Idempotent on `seq`: returns False and writes nothing when the ledger
    already carries an entry for it (any class) -- a settle re-evaluated
    after a crash between this function's own two writes must not
    double-record. The log line is written before the ledger entry, so a
    crash in between leaves at most one extra log line, never a missing
    one (`_count_step_executions`'s docstring already reads this file
    protectively).

    `fields` is merged into the ledger entry alongside the fixed
    {seq, class: "aborted", ended_at}. The A layer passes {"evidence":
    "run-lock"|"deadline", "detected_by": "dispatch"|"wait"}. The B layer's
    `record` passes {"token": <the attempt's token>} plus a ledger flag
    (`late_completion` or `stop_contradiction`: True) or, when the refusal
    check applies (Claude Code), its `deny_check` label.
    """
    attempts_path = Path(attempts_path)
    attempts = load_attempts(attempts_path)
    if any(isinstance(a, dict) and a.get("seq") == seq for a in attempts):
        return False
    _append_log(log_path, step, "aborted", result_path, deny_check=deny_check)
    attempts.append({"seq": seq, "class": "aborted", "ended_at": time.time(),
                     **fields})
    write_text_atomic(attempts_path,
                      json.dumps(attempts, ensure_ascii=False, indent=2))
    return True


def apply_result(step: model.Step, res, vars_path: str | Path,
                 log_path: str | Path | None = None,
                 result_path: str | Path = "<result>",
                 outputs_dir: str | Path | None = None,
                 base_dir: str | Path | None = None,
                 decisions_dir: str | Path | None = None,
                 decision_prefix: str | None = None) -> tuple[str, str]:
    """The shared second half of `record_result` (B layer) and `wfrun wait`
    (A layer, reliability-spec.md §5.1: "B層 record 相当を兼ねる"): given an
    ALREADY-CLASSIFIED `claude_cli.CliResult`, update vars.json and append
    the log. Only how each layer got to a CliResult differs -- B parses a
    sentinel-terminated result file itself; A already has one straight from
    `claude_cli.classify_result()`.

    `result_path` is used only for error-message pointers (never read), and
    `outputs_dir` only for `output-type="file"` steps (B passes the result
    file itself instead; A has no such file and writes one here, mirroring
    run-cc's `runs/<ts>/outputs/<id>.md`).

    `decisions_dir`/`decision_prefix` name where a `DECISION:` payload is
    filed and under which id (§14.1). The A layer passes its run dir and
    `<id>_cNN` explicitly, since only it knows the cycle; the B layer lets
    them default off the result path and the bare step id.

    `base_dir` resolves relative `expect-file` paths. The A layer MUST pass
    the same directory it gave the wrapper as cwd (the XML's parent):
    unlike the B layer -- where the orchestrator and the subagent share a
    cwd by construction -- an A-layer `wait` can be invoked from anywhere,
    so checking expect-file against the caller's cwd would report a
    correctly-written artifact as missing.

    Returns (status, message) like record_result -- including "decision",
    whose class `classify_result()` already assigned ("aborted" is decided
    before a CliResult exists at all, by the caller -- never returned here).
    """
    ok = res.ok
    message = "ok"
    value = None
    variables = None
    if ok and (step.expect_file or step.output):
        vars_path = Path(vars_path)
        try:
            variables = json.loads(vars_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _append_log(log_path, step, "error", result_path)
            return "error", f"error: cannot load vars file {vars_path}: {e}"

    if ok and step.expect_file:
        try:
            missing = _missing_expected(step.expect_file, variables, step.id,
                                        base_dir)
        except InterpError as e:
            ok = False
            message = f"error: step '{step.id}' expect-file: {e}"
        else:
            if missing:
                ok = False
                # run-cc's class for the same miss (Executor._exec_step):
                # retried, and still open to debug.
                res.error_class = "behavioral"
                message = "error: expect-file: not produced: " + ", ".join(missing)

    if ok and step.output:
        # None means the VALUE: line never applied (file-typed output); only
        # the plain-text branch can report a shape.
        value_marker = None
        if step.output_type == "file":
            value = str(result_path)
            if outputs_dir:
                out_path = Path(outputs_dir) / f"{step.id}.md"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(modes.strip_mode_line(res.text), encoding="utf-8")
                value = str(out_path)
        else:
            value, value_marker = unwrap_value_marked(
                res.structured, res.text)
        variables[step.output] = value
        vars_path.write_text(json.dumps(variables, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        message = f"ok (set {step.output}{value_line_suffix(value_marker)})"

    if not ok and message == "ok":
        # Content-hiding parity with record_result: guardrail/refusal carry
        # the step's own reported text in res.error/res.text, and denied the
        # refused command, none of which may reach the orchestrator's context
        # (run-llm.md's no-task-content design); point at the result file
        # instead. `error: denied` is also the literal run-llm.md keys its
        # no-retry rule on. Other classes' messages (classify_result()'s own
        # strings) are already generic/content-free.
        if res.error_class == "guardrail":
            message = f"error: step reported ERROR (details: {result_path})"
        elif res.error_class == "refusal":
            message = f"error: step blocked by a mode/rules constraint (details: {result_path})"
        elif res.error_class == "denied":
            message = f"error: denied (details: {result_path})"
        elif res.error_class == "decision":
            # res.error holds the whole payload body here, so this branch is
            # not optional content-hiding -- without it the `else` below would
            # spill the request into the orchestrator's context.
            message = _decision_message(
                res.text, decisions_dir or decisions_dir_for_result(result_path),
                decision_prefix or step.id)
        else:
            message = res.error or f"error: {res.error_class or 'failed'}"

    if ok:
        message = _with_stray_warning(message, res.text or "", result_path)
    status = "decision" if res.error_class == "decision" else ("ok" if ok else "error")
    _append_log(log_path, step, status, result_path)
    return status, message


def adjudicate_answer(step: model.Step, result_path: str | Path,
                      vars_path: str | Path, answer_file: str | Path,
                      log_path: str | Path | None = None,
                      decisions_dir: str | Path | None = None,
                      decision_prefix: str | None = None,
                      decider: str = model.DECIDER_HUMAN) -> tuple[str, str]:
    """Settle the step's open decision request (`record --answer`, §14.2).

    Returns ("ok", msg) for form (a) -- the payload's own output becomes the
    step's value and nothing re-runs -- or ("rerun", msg) for form (b), where
    the caller redoes the step from move 1 and the settled rulings are
    injected for it by `prompt`/`dispatch`. Everything that cannot be settled
    raises StepIOError, leaving the ledger untouched: a rejected answer must
    not half-apply.

    Unlike the batch path this evaluates (a)-eligibility once, at answer time,
    so `missing-file` and `missing-file-at-resume` collapse into the former
    here (§14.2 step 3) -- run-llm keeps no record of what existed when the
    step stopped, and both readings route to (b) anyway.

    `decider` is recorded, not inspected: run-llm settles human and delegated
    rulings through this one verb, so without the field the §7 cap would have
    no way to tell an unattended llm loop from a person answering every time
    (§15.7). The caller resolves it from the workflow, and `--decider human`
    overrides it for the fallback paths a person is asked to answer.
    """
    dec_dir = Path(decisions_dir or decisions_dir_for_result(result_path))
    # Search BOTH layers' namespaces unless the caller pinned one: an A-layer
    # request is filed under `<id>_cNN_dNN`, and a bare-`<id>` lookup would
    # leave it detected-but-unanswerable forever (§14.2, relaxed).
    if decision_prefix:
        rid = decision_mod.pending_request_id(dec_dir, decision_prefix)
        settled_ids = decision_mod.request_ids(dec_dir, decision_prefix)
    else:
        rid = decision_mod.pending_step_request_id(dec_dir, step.id)
        settled_ids = decision_mod.step_request_ids(dec_dir, step.id)
    if rid is None:
        settled = settled_ids
        raise StepIOError(
            f"step '{step.id}': no open decision request in {dec_dir}"
            + (f" ({len(settled)} already settled -- a recorded ruling is "
               "never replaced, since later steps may be built on it)"
               if settled else ""))

    request_file = dec_dir / f"{rid}{decision_mod.REQUEST_SUFFIX}"
    payload, errors = decision_mod.parse_payload(
        request_file.read_text(encoding="utf-8"))
    if errors:
        raise StepIOError(
            f"decision {rid}: the recorded payload is malformed "
            f"({'; '.join(errors)[:300]}); it cannot be answered as-is -- "
            f"read {request_file} and decide by hand")

    answer_file = Path(answer_file)
    try:
        answer_text = answer_file.read_text(encoding="utf-8")
    except OSError as e:
        raise StepIOError(f"decision {rid}: cannot read {answer_file}: {e}")
    answer, answer_errors = decision_mod.parse_answer(
        answer_text, len(payload.options))
    if answer_errors:
        raise StepIOError(
            f"decision {rid} ({answer_file}): {'; '.join(answer_errors)}; "
            f"the request lists {len(payload.options)} option(s) in {request_file}")

    b_reason = _decision_b_reason(step, payload, vars_path, result_path)
    if not b_reason:
        b_reason = decision_mod.answer_b_reason(answer, payload.recommendation)
    verdict = "b" if b_reason else "a"

    # Copy the ruling into the ledger for the same reason the request was
    # filed there (§14.1): the answer file belongs to whoever wrote it, and
    # answering a second request through the same path would otherwise
    # overwrite the first ruling that a later re-run still has to quote.
    filed_answer = dec_dir / f"{rid}{decision_mod.ANSWER_SUFFIX}"
    write_text_atomic(filed_answer, answer_text)
    write_text_atomic(decision_mod.verdict_marker(dec_dir, rid), json.dumps({
        "request_id": rid, "step": step.id,
        "answer_path": str(filed_answer.resolve()),
        "answer_source": str(answer_file.resolve()),
        "option": answer.option, "verdict": verdict, "b_reason": b_reason,
        "decider": decider,
    }, ensure_ascii=False, indent=2))

    chosen = "none" if answer.option is None else f"option {answer.option}"
    if verdict == "a":
        variables = json.loads(Path(vars_path).read_text(encoding="utf-8"))
        message = f"ok [decision {rid}: {chosen}, continues without re-running]"
        if step.output:
            variables[step.output] = payload.output
            Path(vars_path).write_text(
                json.dumps(variables, ensure_ascii=False, indent=2),
                encoding="utf-8")
            message = (f"ok (set {step.output}) [decision {rid}: {chosen}, "
                       "continues without re-running]")
        status = "ok"
    else:
        message = (f"re-run this step from move 1 [decision {rid}: {chosen}, "
                   f"{b_reason}]; the settled ruling(s) are injected for you")
        status = "rerun"

    # run-llm's only ledger. Without this the ruling would live nowhere:
    # there is no events.jsonl here, and P4's adjudication cap counts these.
    if log_path:
        with Path(log_path).open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "step": step.id,
                "status": f"decision-{verdict}", "request_id": rid,
                "request_file": str(request_file), "answer_file": str(answer_file),
                "option": answer.option, "b_reason": b_reason,
                "decider": decider,
            }, ensure_ascii=False) + "\n")
    return status, message


def _decision_b_reason(step: model.Step, payload, vars_path, result_path
                       ) -> str | None:
    """Why this ruling must re-run the step, or None if (a) is still open.

    Mirrors the batch predicate (§6) on the inputs run-llm actually has;
    expect-file resolves against the caller's cwd, this layer's standing
    "orchestrator cwd = subagent cwd" premise.
    """
    if not payload.work_complete:
        return decision_mod.B_REASON_WORK_STATE_STOPPED
    # Same order as the batch predicate, including its two guards: a step with
    # no `output=` has nowhere to put the value so its absence demotes nothing
    # (§18.5), and a value-typed output would be adopted verbatim from what the
    # step wrote before the ruling existed, so only file-typed steps keep (a)
    # (§18.2).
    if step.output and payload.output is None:
        return decision_mod.B_REASON_NO_OUTPUT
    if step.output and step.output_type != "file":
        return decision_mod.B_REASON_VALUE_OUTPUT
    if step.schema:
        return decision_mod.B_REASON_SCHEMA_STEP
    if not step.expect_file:
        return decision_mod.B_REASON_NO_EXPECT_FILE
    try:
        variables = json.loads(Path(vars_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise StepIOError(f"cannot load vars file {vars_path}: {e}")
    try:
        missing = _missing_expected(step.expect_file, variables, step.id, None)
    except InterpError as e:
        raise StepIOError(f"step '{step.id}' expect-file: {e}")
    return decision_mod.B_REASON_MISSING_FILE if missing else None


def _append_attempt(step_id: str, result_path, handle: dict, status: str):
    """B-layer (`record`) attempts file: the cycle's ledger next to the
    handle, keyed by the handle's own `attempt` and carrying its token
    (reliability-spec.md §4.2). Handle-bearing runs only — legacy callers get
    no attempts file, matching their no-handle, no-sentinel treatment
    throughout this module."""
    if is_attempt_handle(handle):
        path = attempt_ledger_path(Path(handle["result_path"]).parent, step_id,
                                   handle["cycle"])
        append_attempt(path, handle["attempt"], status, {"token": handle["token"]})
        return
    path = handle_path(step_id, result_path).with_name(f"{step_id}_attempts.json")
    append_attempt(path, handle.get("attempt"), status)


def _reply_claim(reply: str | None) -> str | None:
    """'ok' / 'error' / None (no usable claim) from the orchestrator-supplied
    --reply line. Only a clean prefix counts as a claim — anything else
    (garbled, empty, unrelated text) is treated the same as no reply at all
    for the liveness decision below (reliability-spec.md §4.2, F3)."""
    if not reply:
        return None
    head = reply.strip().upper()
    if head.startswith("OK"):
        return "ok"
    if head.startswith("ERROR"):
        return "error"
    return None


def record_result(step: model.Step, result_path: str | Path,
                  vars_path: str | Path, log_path: str | Path | None = None,
                  reply: str | None = None) -> tuple[str, str]:
    """Read a subagent's result file, update the vars file, append the log.

    Returns (status, message): status is "ok" / "error" / "aborted" /
    "decision" (reliability-spec.md §4.2's decision table plus
    xml-wf-decision-request.md §8; CLI exit codes 0/1/3/4 are
    __main__.cmd_record's job, not this function's). message never contains
    step output content — failures point at the result file instead of
    quoting it.

    `reply` is the single line the orchestrator received back from the
    subagent (the reply CHANNEL), independent of the result FILE this
    function otherwise reads — the two are cross-checked below. Passing
    None reproduces pre-Phase-2.2 behavior exactly for callers with no
    handle file (a "legacy" run: no `wfrun prompt --result` ever wrote one),
    with one exception made deliberately non-legacy: a genuinely missing
    result file used to report "error"/exit 1 for every caller; it now
    reports "aborted"/exit 3 unless `reply` claims "ok" — reply presence is
    the liveness signal that tells a dead/interrupted subagent (no reply at
    all) apart from one that finished but never touched the file (reply
    claims ok) (reliability-spec.md §1.3, §4.2).

    expect-file paths are checked here too (relative paths resolve against the
    orchestrator's cwd — where the subagent also ran).

    On Claude Code the step subagent's transcript is checked for refused tool
    calls first (ccdeny): one found makes the step `error: denied` whatever
    its result file says, and a transcript that cannot be resolved is
    reported as NO-TRANSCRIPT, never read as clean. The verdict goes into the
    steps.log entry as `deny_check`. An attempt whose stop record is not
    `done` (`wfrun stop`: confirmed or unconfirmed) never gets here: `wfrun
    record` records it `aborted` (or refuses, if it has a verdict already)
    without reading `reply` or `_record_file`'s decision table.
    """
    result_path = Path(result_path)
    handle = load_handle(step.id, result_path)
    if is_attempt_handle(handle):
        # `result_path` names the step; the attempt's own file is the one
        # its subagent was told to write.
        result_path = Path(handle["result_path"])
    check = ccdeny.check_delegation(handle)
    if check.verdict == ccdeny.DENIED:
        _append_log(log_path, step, "error", result_path, deny_check=check.label)
        _append_attempt(step.id, result_path, handle, "denied")
        return "error", f"error: denied (details: {check.transcript})"
    deny_check = None if check.verdict == ccdeny.NOT_APPLICABLE else check.label
    status, message = _record_file(step, result_path, vars_path, log_path,
                                   reply, handle, deny_check)
    if check.verdict == ccdeny.NO_TRANSCRIPT:
        # Its own line: a decision verdict ends in the answer path the
        # orchestrator copies into its next command.
        message = f"{message}\ndeny-check: {check.label}: {check.reason}"
    return status, message


def _record_file(step: model.Step, result_path: Path, vars_path: str | Path,
                 log_path: str | Path | None, reply: str | None,
                 handle: dict | None, deny_check: str | None) -> tuple[str, str]:
    """`record_result`'s decision table over the result file and the reply."""
    claim = _reply_claim(reply)

    if not result_path.is_file():
        status = "error" if claim == "ok" else "aborted"
        message = ("error: claimed-ok-but-no-result" if status == "error"
                   else "aborted: result file not found")
        _append_log(log_path, step, status, result_path, deny_check=deny_check)
        if handle is not None:
            _append_attempt(step.id, result_path, handle, status)
        return status, message

    raw = result_path.read_text(encoding="utf-8")
    text = modes.strip_mode_line(raw)

    if handle is not None:
        text, sentinel_ok = strip_sentinel_line(text, step.id, handle.get("sentinel"))
        if not sentinel_ok:
            status = "error" if claim == "ok" else "aborted"
            message = ("error: claimed-ok-but-no-result" if status == "error"
                       else "aborted: incomplete (no end marker)")
            _append_log(log_path, step, status, result_path, deny_check=deny_check)
            _append_attempt(step.id, result_path, handle, status)
            return status, message

    if text.strip() == "":
        _append_log(log_path, step, "error", result_path, deny_check=deny_check)
        if handle is not None:
            _append_attempt(step.id, result_path, handle, "error")
        return "error", "error: empty result"

    ok = True
    message = "ok"
    value = None
    variables = None
    is_decision = False
    if step.expect_file or step.output:
        vars_path = Path(vars_path)
        try:
            variables = json.loads(vars_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            status, message = "error", f"error: cannot load vars file {vars_path}: {e}"
            _append_log(log_path, step, status, result_path, deny_check=deny_check)
            if handle is not None:
                _append_attempt(step.id, result_path, handle, status)
            return status, message
    if text.lstrip().startswith("ERROR:"):
        ok = False
        message = f"error: step reported ERROR (details: {result_path})"
    elif modes.blocked_line(text) is not None:
        ok = False
        message = f"error: step blocked by a mode/rules constraint (details: {result_path})"
    elif decision_mod.claim_decision_body(
            modes.strip_mode_line(text))[0] is not None:
        # The second classification site (xml-wf-decision-request.md §3): this
        # path never reaches claude_cli.classify_result(), so the prefix is
        # recognized here too -- including the D9 preamble claim, which
        # _decision_message re-derives to file the anchored slice. Peer of the
        # two branches above, and likewise ahead of the expect-file/schema
        # checks below -- a decision response produces no step output to
        # validate.
        ok = False
        is_decision = True
        message = _decision_message(
            text, decisions_dir_for_result(result_path), step.id)
    else:
        if step.expect_file:
            try:
                # base_dir=None: cwd-relative, per this layer's documented
                # "orchestrator cwd = subagent cwd" premise (see the
                # docstring above and apply_result's base_dir note).
                missing = _missing_expected(step.expect_file, variables,
                                            step.id, None)
            except InterpError as e:
                status, message = "error", f"error: step '{step.id}' expect-file: {e}"
                _append_log(log_path, step, status, result_path, deny_check=deny_check)
                if handle is not None:
                    _append_attempt(step.id, result_path, handle, status)
                return status, message
            if missing:
                ok = False
                message = "error: expect-file: not produced: " + ", ".join(missing)
        if ok and step.output:
            value_marker = None  # see apply_result
            if step.output_type == "file":
                value = str(result_path)
            else:
                structured = None
                if step.schema:
                    try:
                        structured = json.loads(text)
                    except json.JSONDecodeError:
                        ok = False
                        message = (f"error: schema specified but result is not "
                                   f"valid JSON (details: {result_path})")
                if ok:
                    value, value_marker = unwrap_value_marked(
                        structured, text)

    if ok and claim == "error":
        # The file looks like a clean success, but the reply channel
        # explicitly claimed ERROR: do not silently trust the file over a
        # deliberate error claim (reliability-spec.md §4.2, row 6).
        ok = False
        message = "error: reply/file mismatch"

    if ok and step.output:
        variables[step.output] = value
        vars_path.write_text(json.dumps(variables, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        message = f"ok (set {step.output}{value_line_suffix(value_marker)})"

    if ok:
        message = _with_stray_warning(message, text, result_path)
    status = "decision" if is_decision else ("ok" if ok else "error")
    _append_log(log_path, step, status, result_path, deny_check=deny_check)
    if handle is not None:
        _append_attempt(step.id, result_path, handle, status)
    return status, message
