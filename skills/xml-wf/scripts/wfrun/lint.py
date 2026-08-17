"""Static validation (`wfrun validate`).

Parse errors are structural and raised by parser.py; lint assumes a parsed
Workflow and checks the semantic layer: id uniqueness, variable flow, named
role existence, mode existence, rules resolution, expression syntax.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import interp, model, modelmap, modes
from .agents import discover_agents


# Modes whose discipline is non-writing (mode-output goes to task-named paths;
# target sources stay untouched). Write-capable tools= on such a step usually
# means the tool grant, not the mode text, is what actually limits the agent.
NON_WRITING_MODES = {"survey", "plan", "review", "review-dev"}


@dataclass
class Finding:
    level: str  # "error" | "warn"
    code: str
    message: str

    def __str__(self):
        return f"[{self.level.upper()}] {self.code}: {self.message}"


class _VarState:
    """Tracks definitely-defined and maybe-defined variable names."""

    def __init__(self, defined=(), maybe=()):
        self.defined = set(defined)
        self.maybe = set(maybe)

    def copy(self):
        return _VarState(self.defined, self.maybe)

    def define(self, name):
        self.defined.add(name)
        self.maybe.discard(name)


def _lint_pi_models(wf: model.Workflow, steps, backend: str) -> list[Finding]:
    """Model names, checked only when `backend == "pi"`.

    Nothing is checked on cc/llm by design: those runs stay inside the canonical
    difficulty vocabulary, which model_map binds to claude CLI names, so there
    is no catalog a name could be missing from. pi is the opposite case — its
    models are not in that vocabulary, `decider-model=` is allowed to name one
    directly (spec.md, run-pi.md § decider-model), and an unresolvable name is
    otherwise discovered only when the process launches.

    The vocabulary still governs `model=`, so a non-canonical name there stays a
    warning even when it resolves. Names are deduplicated: with a workflow-level
    `decider="llm"` every step would otherwise report the same adjudicator.
    """
    if backend != "pi":
        return []
    from . import pi_cli  # deferred: needs the pi CLI only for this check

    findings: list[Finding] = []
    checked: dict[str, str] = {}  # name as pi receives it -> first site

    def note(where: str, name: str | None) -> None:
        if name and name not in checked:
            checked[name] = where

    for step in steps:
        if step.model:
            if step.model not in modelmap.CANONICAL_MODELS:
                findings.append(Finding(
                    "warn", "model-not-canonical",
                    f"step '{step.id}': model='{step.model}' is not a canonical "
                    f"difficulty name ({'/'.join(modelmap.CANONICAL_MODELS)}); "
                    "it bypasses model_map.json"))
            note(f"step '{step.id}' model=", modelmap.resolve(step.model, "llm"))
        decider, decider_model = model.resolve_decider(wf, step)
        if decider == "llm":
            note(f"step '{step.id}' decider-model=",
                 modelmap.resolve(decider_model, "llm"))

    if not checked:
        return findings

    catalog = pi_cli.list_available_models()
    if catalog is None:
        findings.append(Finding(
            "warn", "pi-model-unverified",
            "pi's model catalog could not be read (`--list-models`), so "
            f"{len(checked)} model name(s) went unverified — that is not a pass"))
        return findings
    for name, where in checked.items():
        if not pi_cli.model_is_resolvable(name, catalog):
            findings.append(Finding(
                "error", "pi-model-unavailable",
                f"{where} '{name}' matches no model pi currently offers, so the "
                "call fails when the process launches; `--list-models` shows "
                "what is available"))
    return findings


def lint(wf: model.Workflow, base_dir: str | Path = ".",
         check_roles: bool = True, as_child: bool = False,
         defined_vars: set[str] | None = None,
         backend: str = "cc") -> list[Finding]:
    """as_child: validate a replan-generated continuation — <replan> (recursion)
    and <param> are forbidden. defined_vars: names to treat as already defined
    (a child inherits the parent's live variable store).

    backend: which facility the workflow is headed for ("cc" | "pi"). Only the
    model-name checks read it — see `_lint_pi_models`."""
    base_dir = Path(base_dir)
    findings: list[Finding] = []
    err = lambda code, msg: findings.append(Finding("error", code, msg))
    warn = lambda code, msg: findings.append(Finding("warn", code, msg))

    try:
        modelmap.load_map()
    except modelmap.ModelMapError as e:
        err("model-map-invalid", str(e))

    if as_child:
        if any(isinstance(n, model.Replan) for n in wf.iter_steps()):
            err("replan-forbidden",
                "a replan-generated continuation must not contain <replan> "
                "(recursion depth is limited to 1)")
        if wf.params:
            err("param-forbidden",
                "a replan-generated continuation must not declare <param> "
                "(variables are inherited from the parent run)")

    # --- step id uniqueness -------------------------------------------------
    seen_ids: set[str] = set()
    steps = list(wf.iter_steps())
    for step in steps:
        if step.id in seen_ids:
            err("duplicate-id", f"step id '{step.id}' is used more than once")
        seen_ids.add(step.id)

    # --- max sanity ---------------------------------------------------------
    if wf.max < len(steps):
        warn("max-too-small",
             f"workflow max={wf.max} is below the static step count ({len(steps)}); "
             "the run may abort before completing")

    # --- rules --------------------------------------------------------------
    for rules in wf.rules.values():
        if rules.src:
            src = (base_dir / rules.src) if not Path(rules.src).is_absolute() else Path(rules.src)
            if not src.is_file():
                err("rules-src-missing", f"rules '{rules.id}': file not found: {src}")
    for step in steps:
        for rid in getattr(step, "rules", []):
            if rid not in wf.rules:
                err("rules-undefined", f"step '{step.id}': rules id '{rid}' is not defined")

    # --- named roles ----------------------------------------------------------
    if check_roles:
        agents = discover_agents(base_dir)
        for step in steps:
            if step.role and step.role not in agents:
                err("role-missing",
                    f"step '{step.id}': role '{step.role}' not found in "
                    f"{base_dir / '.claude/agents'} or the user agents dir "
                    "($CLAUDE_CONFIG_DIR or ~/.claude)/agents")

    # --- modes ----------------------------------------------------------------
    for step in steps:
        mode = getattr(step, "mode", None)
        if mode and modes.mode_file(mode) is None:
            err("mode-unknown",
                f"step '{step.id}': mode '{mode}' is not a known execution mode "
                f"(available: {', '.join(modes.available_modes())})")

    # --- decider ---------------------------------------------------------------
    # `decider="llm"` is implemented (§15) and valid XML under run-cc and
    # run-llm, so lint only checks the vocabulary. The backend that cannot
    # support it refuses at startup instead: adjudication runs on the claude
    # CLI whichever backend executes the steps, so pi_cli.pi_compat_errors
    # rejects it there, beside the schema= / on-error="debug" fail-fasts it
    # shares a cause with (§4, §15.8).
    valid_deciders = "/".join(model.DECIDER_VALUES)
    if wf.decider is not None and wf.decider not in model.DECIDER_VALUES:
        err("decider-unknown",
            f"workflow decider='{wf.decider}' is not one of {valid_deciders}")
    for step in steps:
        decider = getattr(step, "decider", None)
        if decider is not None and decider not in model.DECIDER_VALUES:
            err("decider-unknown",
                f"step '{step.id}': decider='{decider}' is not one of {valid_deciders}")
        if getattr(step, "decider_model", None) and not decider and not wf.decider:
            warn("decider-model-unused",
                 f"step '{step.id}': decider-model= is set but the decider "
                 f"resolves to '{model.DEFAULT_DECIDER}', which never calls a model")
        if isinstance(step, model.Step) and step.schema:
            warn("decision-schema",
                 f"step '{step.id}': schema= forces structured output, so this "
                 "step may be unable to return a plain-text 'DECISION:' request "
                 "when it hits a fork; and a decision on a schema= step can "
                 "only ever continue by re-running it")

    # --- misc step warnings ---------------------------------------------------
    for step in steps:
        if step.on_error == "ignore":
            warn("on-error-ignore",
                 f"step '{step.id}': failures will be silently ignored")
        if (isinstance(step, model.Step)
                and not step.role and not step.tools):
            warn("tools-not-inherited",
                 f"step '{step.id}': no named role= to inherit tools from and no "
                 "tools= of its own, so it runs with the CLI's default tool "
                 "permissions; set tools= for least privilege")
        if (isinstance(step, model.Step)
                and step.output_type == "value" and not step.output):
            warn("value-without-output",
                 f"step '{step.id}': output-type=value without output= has no effect")
        if (isinstance(step, model.Step) and step.mode in NON_WRITING_MODES
                and step.tools and model.tools_can_write(step.tools)):
            warn("mode-write-tools",
                 f"step '{step.id}': mode '{step.mode}' is a non-writing "
                 f"discipline but tools=\"{step.tools}\" grants write-capable "
                 "tools; the mode text is a probabilistic constraint — restrict "
                 "tools= for a deterministic one")

    findings.extend(_lint_pi_models(wf, steps, backend))

    # --- variable flow --------------------------------------------------------
    state = _VarState(defined_vars or ())
    for param in wf.params:
        if param.required or param.default is not None:
            state.define(param.name)
        else:
            state.maybe.add(param.name)
            warn("param-optional-no-default",
                 f"param '{param.name}' is optional without default; references "
                 "fail when it is not provided")

    def check_refs(text: str, where: str, expr: bool = False):
        if expr:
            msg = interp.check_expr_syntax(text)
            if msg:
                err("bad-expr", f"{where}: {msg}")
        for name in sorted(interp.find_refs(text)):
            if name in state.defined:
                continue
            if name in state.maybe:
                warn("var-maybe-undefined",
                     f"{where}: '{name}' may be undefined on some paths")
            else:
                err("var-undefined", f"{where}: reference to undefined variable '{name}'")

    def walk(node):
        nonlocal state
        if isinstance(node, model.Seq):
            for child in node.children:
                walk(child)
        elif isinstance(node, model.SetVar):
            check_refs(node.value if node.value is not None else node.expr,
                       f"set '{node.var}'", expr=node.expr is not None)
            state.define(node.var)
        elif isinstance(node, model.Step):
            check_refs(node.task, f"step '{node.id}' task")
            if node.expect_file:
                # checked before the output variable exists (executor order)
                check_refs(node.expect_file, f"step '{node.id}' expect-file")
            if node.output:
                state.define(node.output)
        elif isinstance(node, model.Replan):
            check_refs(node.task, f"replan '{node.id}' task")
            for name in node.outputs:
                if name in state.defined:
                    warn("replan-shadows-var",
                         f"replan '{node.id}': declared output '{name}' overwrites "
                         "an existing variable")
                state.define(name)
        elif isinstance(node, model.If):
            cond = node.test if node.test is not None else node.ask
            check_refs(cond, "if condition", expr=node.test is not None)
            before = state.copy()
            walk(node.then)
            after_then = state
            state = before.copy()
            if node.else_ is not None:
                walk(node.else_)
            after_else = state
            merged = _VarState(
                after_then.defined & after_else.defined,
                (after_then.defined | after_else.defined |
                 after_then.maybe | after_else.maybe),
            )
            merged.maybe -= merged.defined
            state = merged
        elif isinstance(node, model.While):
            cond = node.test if node.test is not None else node.ask
            check_refs(cond, "while condition", expr=node.test is not None)
            before = state.copy()
            walk(node.body)
            after = state
            state = _VarState(before.defined,
                              before.maybe | (after.defined | after.maybe) - before.defined)
        elif isinstance(node, model.Each):
            source = node.items or node.glob or node.range_
            check_refs(source, "each source")
            before = state.copy()
            state.define(node.as_)
            state.define(f"{node.as_}_index")
            walk(node.body)
            after = state
            leaked = (after.defined | after.maybe) - before.defined
            leaked -= {node.as_, f"{node.as_}_index"}
            state = _VarState(before.defined, before.maybe | leaked)
        elif isinstance(node, model.Parallel):
            outputs = [s.output for s in node.children if s.output]
            for name in sorted({n for n in outputs if outputs.count(n) > 1}):
                err("parallel-output-conflict",
                    f"parallel: output variable '{name}' written by multiple steps")
            for step in node.children:
                check_refs(step.task, f"parallel step '{step.id}' task")
                if step.expect_file:
                    check_refs(step.expect_file,
                               f"parallel step '{step.id}' expect-file")
            for step in node.children:
                if step.output:
                    state.define(step.output)

    walk(wf.body)
    return findings


def has_errors(findings: list[Finding]) -> bool:
    return any(f.level == "error" for f in findings)
