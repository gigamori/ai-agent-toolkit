"""Step prompt assembly and result recording, shared by the deterministic
executor and the LLM-orchestrator helper commands (wfrun prompt / record).

In run-llm mode these functions are the content firewall: task bodies and
step outputs flow XML -> prompt file -> subagent -> result file -> vars.json
entirely inside Python, so the orchestrating LLM's context carries only step
ids, file paths, and ok/error signals.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import model, modes
from .agents import AgentDef, discover_agents
from .guardrails import GUARDRAILS
from .interp import InterpError, interpolate


class StepIOError(Exception):
    pass


RESULT_PROTOCOL = """\
## Response protocol
Write your full final response to this file: {result_path}
Your reply to the caller must be a single line starting with "OK" or "ERROR:"."""

REPLAN_PROMPT = """\
You are a continuation planner inside a running workflow. Based on the results
so far, produce the workflow XML for what should happen next. You plan; you do
not execute any of the work yourself.

## Contract (a validator rejects violations; you would then be asked to fix them)
- Output MUST be a single `<workflow name="..." version="2" max="N">` document
  with N <= {max_steps}.
- Every step needs a role: either role="<name>" using ONLY these named
  definitions: {roles} — or an inline <role> child you author yourself.
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
                 ) -> tuple[str, AgentDef | None]:
    """The role body injected into the prompt: an inline <role> as-is, or the
    body of the named .claude/agents definition (returned for dispatch too)."""
    if node.role_text:
        return node.role_text, None
    agent = agents_cache.get(node.role)
    if agent is None:
        raise StepIOError(
            f"step '{node.id}': role '{node.role}' not found in .claude/agents "
            "(project) or ~/.claude/agents")
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
    """_meta + <role> block (+ mode declaration/body/_common when mode= is set)."""
    role_body, _ = resolve_role(node, agents_cache)
    parts = [modes.meta_text(), f"<role>\n{role_body}\n</role>"]
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
                            result_path: str | None = None) -> tuple[str, str]:
    """(system_text, user_text) for a step.

    system = framework (_meta) + role + mode/_common + rules — the constraint
    layers, placed in the high-authority channel by run-cc.
    user = the interpolated task (+ fix), response protocol, and guardrails.
    run-llm joins the two (the Agent tool has no system-prompt input).
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
    user_parts = [task]
    if result_path:
        user_parts.append(RESULT_PROTOCOL.format(result_path=result_path))
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
    part, the planning contract + variables + task in the user part."""
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
        spec_path=str(spec) if spec else "(spec file unavailable — follow the examples in the task)",
        outputs_clause=outputs_clause,
        variables=json.dumps(variables, ensure_ascii=False, indent=2),
        task=task,
        output_contract=(REPLAN_OUTPUT_FILE.format(result_path=result_path)
                         if result_path else REPLAN_OUTPUT_INLINE),
    )
    return f"<role>\n{role_body}\n</role>", user


def build_replan_prompt(node, variables: dict,
                        agents_cache: dict[str, AgentDef],
                        fix: str | None = None,
                        result_path: str | None = None) -> str:
    system, user = build_replan_prompt_parts(node, variables, agents_cache,
                                             fix=fix, result_path=result_path)
    return f"{system}\n\n{user}"


def unwrap_value(structured, text: str):
    """output-type=value extraction: single-property objects unwrap to the
    scalar; other structured results store as JSON text; plain text strips
    (after dropping the [Mode: ...] protocol line _common.md mandates)."""
    if isinstance(structured, dict) and len(structured) == 1:
        return next(iter(structured.values()))
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return modes.strip_mode_line(text).strip()


def record_result(step: model.Step, result_path: str | Path,
                  vars_path: str | Path, log_path: str | Path | None = None
                  ) -> tuple[bool, str]:
    """Read a subagent's result file, update the vars file, append the log.

    Returns (ok, message). message never contains step output content —
    failures point at the result file instead of quoting it.

    expect-file paths are checked here too (relative paths resolve against the
    orchestrator's cwd — where the subagent also ran).
    """
    result_path = Path(result_path)
    if not result_path.is_file():
        return False, f"error: result file not found: {result_path}"
    raw = result_path.read_text(encoding="utf-8")
    text = modes.strip_mode_line(raw)

    ok = True
    message = "ok"
    value = None
    variables = None
    if step.expect_file or step.output:
        vars_path = Path(vars_path)
        try:
            variables = json.loads(vars_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return False, f"error: cannot load vars file {vars_path}: {e}"
    if text.lstrip().startswith("ERROR:"):
        ok = False
        message = f"error: step reported ERROR (details: {result_path})"
    elif modes.blocked_line(raw) is not None:
        ok = False
        message = f"error: step blocked by a mode/rules constraint (details: {result_path})"
    else:
        if step.expect_file:
            try:
                paths = interpolate(step.expect_file, variables)
            except InterpError as e:
                return False, f"error: step '{step.id}' expect-file: {e}"
            missing = [p.strip() for p in paths.split(",")
                       if p.strip() and not Path(p.strip()).is_file()]
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

    if ok and step.output:
        variables[step.output] = value
        vars_path.write_text(json.dumps(variables, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        message = f"ok (set {step.output})"

    if log_path:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "step": step.id,
                 "status": "success" if ok else "error",
                 "output_var": step.output, "result_file": str(result_path)}
        with Path(log_path).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return ok, message
