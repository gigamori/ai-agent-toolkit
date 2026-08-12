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

from . import model, modes
from . import decision as decision_mod
from .agents import AgentDef, discover_agents
from .guardrails import GUARDRAILS
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


def sentinel_line(step_id: str) -> str:
    """The completion marker a run-llm step must write as the LAST non-empty
    line of its result file (reliability-spec.md §4.1). Absence of this line
    is what lets `record`/`poll` tell "still writing" or "died mid-write"
    apart from "wrote a genuinely short/empty answer" -- the well-formedness
    of a <replan> continuation's own XML already proves completeness, so
    replans are deliberately exempted (reliability-spec.md §0, §4.1)."""
    return f"[[WFRUN-END step={step_id}]]"


def strip_sentinel_line(text: str, step_id: str) -> tuple[str, bool]:
    """Drop this step's trailing sentinel line, if the last non-empty line
    of `text` is exactly `sentinel_line(step_id)`. Returns (text, present).
    A sentinel for a *different* step id counts as absent -- record/poll
    only ever look for their own step's marker.

    Call this (and modes.strip_mode_line) before any value extraction or
    file-content check: the sentinel is protocol furniture, not part of the
    step's actual output (reliability-spec.md §4.2)."""
    lines = text.splitlines()
    idx = len(lines) - 1
    while idx >= 0 and lines[idx].strip() == "":
        idx -= 1
    if idx >= 0 and lines[idx].strip() == sentinel_line(step_id):
        return "\n".join(lines[:idx]), True
    return text, False


def handle_path(step_id: str, result_path: str | Path) -> Path:
    """Sibling `<id>_handle.json` next to a step's result file (written by
    `wfrun prompt --result`, read by `wfrun record`/`wfrun poll`;
    reliability-spec.md §4.1/§4.3). Its absence means a pre-Phase-2.2
    ("legacy") caller: no sentinel/aborted machinery applies then."""
    return Path(result_path).parent / f"{step_id}_handle.json"


def load_handle(step_id: str, result_path: str | Path) -> dict | None:
    path = handle_path(step_id, result_path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


RESULT_PROTOCOL = """\
## Response protocol
Write your full final response to this file: {result_path}
The LAST non-empty line of that file must be exactly this marker, with \
nothing after it: {sentinel}
Your reply to the caller must be a single line starting with "OK {step_id}" \
or "ERROR:"."""

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
- You may reference the existing variables below as {{name}} placeholders.{outputs_clause}

## Current variables (name: value)
{variables}

## Planning task
{task}

## Output contract
{output_contract}"""

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


def build_step_prompt_parts(wf: model.Workflow, step: model.Step, variables: dict,
                            base_dir: str | Path, fix: str | None = None,
                            rules_cache: dict[str, str] | None = None,
                            agents_cache: dict[str, AgentDef] | None = None,
                            result_path: str | None = None,
                            decision: list[tuple[str, str]] | None = None
                            ) -> tuple[str, str]:
    """(system_text, user_text) for a step.

    system = framework header + role (when declared) + mode/_common + rules —
    the constraint layers, placed in the high-authority channel by run-cc.
    user = the interpolated task (+ fix, + resolved decisions), response
    protocol, and guardrails. run-llm joins the two (the Agent tool has no
    system-prompt input).

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
            sentinel=sentinel_line(step.id)))
    user_parts.append(GUARDRAILS)
    return "\n\n".join(sys_parts), "\n\n".join(user_parts)


def build_step_prompt(wf: model.Workflow, step: model.Step, variables: dict,
                      base_dir: str | Path, fix: str | None = None,
                      rules_cache: dict[str, str] | None = None,
                      agents_cache: dict[str, AgentDef] | None = None,
                      result_path: str | None = None) -> str:
    """Single combined prompt (run-llm / wfrun prompt): system part + user part."""
    system, user = build_step_prompt_parts(
        wf, step, variables, base_dir, fix=fix, rules_cache=rules_cache,
        agents_cache=agents_cache, result_path=result_path)
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
                              result_path: str | None = None) -> tuple[str, str]:
    """(system_text, user_text) for a replan builder call: role in the system
    part, the planning contract + variables + task in the user part.

    A replan carries no mode, so it gets no framework header and no _common —
    only the role. Role is optional: without one the system part is empty, and
    both backends then omit the append-system-prompt flag entirely."""
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


def unwrap_value(structured, text: str):
    """output-type=value extraction: single-property objects unwrap to the
    scalar; other structured results store as JSON text; plain text strips
    (after dropping the [Mode: ...] protocol line _common.md mandates)."""
    if isinstance(structured, dict) and len(structured) == 1:
        return next(iter(structured.values()))
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return modes.strip_mode_line(text).strip()


def _log_status(status: str) -> str:
    """steps.log entry status: aborted joins the pre-existing success/error
    (reliability-spec.md §4.2); `decision` joins them in turn
    (xml-wf-decision-request.md §8)."""
    return {"ok": "success"}.get(status, status)


def _decision_message(text: str, result_path) -> str:
    """The `record`/`wait` verdict line for a `DECISION:` response.

    Content-free by construction, like the guardrail/refusal messages beside
    it: the payload never enters the orchestrator's context, only its path
    does (run-llm.md's no-task-content design). Valid and malformed are told
    apart because they need different human actions -- answer it, or go read
    why it cannot be answered (xml-wf-decision-request.md §8).
    """
    _, errors = decision_mod.parse_payload(modes.strip_mode_line(text))
    if errors:
        return ("decision: step raised a decision request, but its payload is "
                f"malformed and cannot be answered as-is ({len(errors)} field "
                f"problem(s)); read it and decide by hand (request: {result_path})")
    return f"decision: step requested adjudication (request: {result_path})"


def _append_log(log_path, step: model.Step, status: str, result_path):
    if not log_path:
        return
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "step": step.id,
             "status": _log_status(status),
             "output_var": step.output, "result_file": str(result_path)}
    with Path(log_path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_attempt(attempts_path: str | Path, seq, status: str):
    """Append one {seq, class, ended_at} entry to a JSON-array attempts file
    (reliability-spec.md §4.2/§5.1). Shared by both run-llm layers: the B
    layer's `record` (via `_append_attempt` below) and the A layer's
    dispatch wrapper (`wfrun _wrapper`, §5.1), which is also what
    `wfrun dispatch` reads back to enforce the attempt cap (§5.1, F4/P5)."""
    attempts_path = Path(attempts_path)
    try:
        attempts = json.loads(attempts_path.read_text(encoding="utf-8"))
        if not isinstance(attempts, list):
            attempts = []
    except (OSError, json.JSONDecodeError):
        attempts = []
    attempts.append({"seq": seq, "class": status, "ended_at": time.time()})
    write_text_atomic(attempts_path,
                      json.dumps(attempts, ensure_ascii=False, indent=2))


def load_attempts(attempts_path: str | Path) -> list:
    try:
        attempts = json.loads(Path(attempts_path).read_text(encoding="utf-8"))
        return attempts if isinstance(attempts, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def apply_result(step: model.Step, res, vars_path: str | Path,
                 log_path: str | Path | None = None,
                 result_path: str | Path = "<result>",
                 outputs_dir: str | Path | None = None,
                 base_dir: str | Path | None = None) -> tuple[str, str]:
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
                message = "error: expect-file: not produced: " + ", ".join(missing)

    if ok and step.output:
        if step.output_type == "file":
            value = str(result_path)
            if outputs_dir:
                out_path = Path(outputs_dir) / f"{step.id}.md"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(modes.strip_mode_line(res.text), encoding="utf-8")
                value = str(out_path)
        else:
            value = unwrap_value(res.structured, res.text)
        variables[step.output] = value
        vars_path.write_text(json.dumps(variables, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        message = f"ok (set {step.output})"

    if not ok and message == "ok":
        # Content-hiding parity with record_result: guardrail/refusal carry
        # the step's own reported text in res.error/res.text, which must
        # never reach the orchestrator's context (run-llm.md's no-task-
        # content design); point at the result file instead. Other classes'
        # messages (claude_cli.classify_result()'s own strings) are already
        # generic/content-free.
        if res.error_class == "guardrail":
            message = f"error: step reported ERROR (details: {result_path})"
        elif res.error_class == "refusal":
            message = f"error: step blocked by a mode/rules constraint (details: {result_path})"
        elif res.error_class == "decision":
            # res.error holds the whole payload body here, so this branch is
            # not optional content-hiding -- without it the `else` below would
            # spill the request into the orchestrator's context.
            message = _decision_message(res.text, result_path)
        else:
            message = res.error or f"error: {res.error_class or 'failed'}"

    status = "decision" if res.error_class == "decision" else ("ok" if ok else "error")
    _append_log(log_path, step, status, result_path)
    return status, message


def _append_attempt(step_id: str, result_path, handle: dict, status: str):
    """B-layer (`record`) attempts file: steps/<id>_attempts.json next to
    the handle, keyed by the handle's own recorded `attempt` (reliability-
    spec.md §4.2). Handle-bearing runs only — legacy callers get no attempts
    file, matching their no-handle, no-sentinel treatment throughout this
    module."""
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
    """
    result_path = Path(result_path)
    claim = _reply_claim(reply)
    handle = load_handle(step.id, result_path)

    if not result_path.is_file():
        status = "error" if claim == "ok" else "aborted"
        message = ("error: claimed-ok-but-no-result" if status == "error"
                   else "aborted: result file not found")
        _append_log(log_path, step, status, result_path)
        if handle is not None:
            _append_attempt(step.id, result_path, handle, status)
        return status, message

    raw = result_path.read_text(encoding="utf-8")
    text = modes.strip_mode_line(raw)

    if handle is not None:
        text, sentinel_ok = strip_sentinel_line(text, step.id)
        if not sentinel_ok:
            status = "error" if claim == "ok" else "aborted"
            message = ("error: claimed-ok-but-no-result" if status == "error"
                       else "aborted: incomplete (no end marker)")
            _append_log(log_path, step, status, result_path)
            _append_attempt(step.id, result_path, handle, status)
            return status, message

    if text.strip() == "":
        _append_log(log_path, step, "error", result_path)
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
            _append_log(log_path, step, status, result_path)
            if handle is not None:
                _append_attempt(step.id, result_path, handle, status)
            return status, message
    if text.lstrip().startswith("ERROR:"):
        ok = False
        message = f"error: step reported ERROR (details: {result_path})"
    elif modes.blocked_line(text) is not None:
        ok = False
        message = f"error: step blocked by a mode/rules constraint (details: {result_path})"
    elif decision_mod.starts_with_decision(text):
        # The second classification site (xml-wf-decision-request.md §3): this
        # path never reaches claude_cli.classify_result(), so the prefix is
        # recognized here too. Peer of the two branches above, and likewise
        # ahead of the expect-file/schema checks below -- a decision response
        # produces no step output to validate.
        ok = False
        is_decision = True
        message = _decision_message(text, result_path)
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
                _append_log(log_path, step, status, result_path)
                if handle is not None:
                    _append_attempt(step.id, result_path, handle, status)
                return status, message
            if missing:
                ok = False
                message = "error: expect-file: not produced: " + ", ".join(missing)
        if ok and step.output:
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
                    value = unwrap_value(structured, text)

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
        message = f"ok (set {step.output})"

    status = "decision" if is_decision else ("ok" if ok else "error")
    _append_log(log_path, step, status, result_path)
    if handle is not None:
        _append_attempt(step.id, result_path, handle, status)
    return status, message
