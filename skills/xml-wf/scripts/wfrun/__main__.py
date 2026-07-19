"""wfrun CLI: validate / run / resume / plan, plus LLM-orchestrator helpers
(interp / eval / ask) that keep interpolation, condition evaluation and ask
judgment deterministic even when an LLM drives the control flow."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from . import interp as interp_mod
from . import lint as lint_mod
from . import model, modelmap, parser, stepio, viz
from .executor import Executor, WorkflowFailure
from .state import load_events


def _parse_params(pairs: list[str]) -> dict[str, str]:
    params = {}
    for pair in pairs:
        if "=" not in pair:
            sys.exit(f"error: -p expects key=value, got '{pair}'")
        key, _, value = pair.partition("=")
        params[key] = value
    return params


def cmd_validate(args) -> int:
    try:
        wf = parser.parse_file(args.workflow)
    except parser.ParseError as e:
        if args.json:
            print(json.dumps({"errors": [str(e)], "warnings": []}, ensure_ascii=False))
        else:
            print(f"[ERROR] parse: {e}")
        return 1
    defined = set(_load_vars(args.defined_vars)) if args.defined_vars else None
    findings = lint_mod.lint(wf, base_dir=Path(args.workflow).parent,
                             check_roles=not args.no_role_check,
                             as_child=args.as_child, defined_vars=defined)
    if args.json:
        print(json.dumps({
            "errors": [str(f) for f in findings if f.level == "error"],
            "warnings": [str(f) for f in findings if f.level == "warn"],
        }, ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f)
        if not findings:
            print(f"OK: {args.workflow} ({len(list(wf.iter_steps()))} steps)")
    return 1 if lint_mod.has_errors(findings) else 0


def _load_validated(path: str, no_role_check: bool) -> model.Workflow:
    wf = parser.parse_file(path)
    findings = lint_mod.lint(wf, base_dir=Path(path).parent,
                             check_roles=not no_role_check)
    for f in findings:
        print(f, file=sys.stderr)
    if lint_mod.has_errors(findings):
        sys.exit("error: validation failed; fix the workflow first")
    return wf


def _report(executor: Executor, status: str):
    print(f"status:      {status}")
    print(f"run dir:     {executor.run_dir}")
    print(f"steps run:   {executor.step_count}")
    print(f"cost (usd):  {executor.cost_usd:.4f}")
    outputs = sorted((executor.run_dir / "outputs").glob("*"))
    if outputs:
        print("outputs:")
        for p in outputs:
            print(f"  {p}")


def cmd_run(args) -> int:
    wf = _load_validated(args.workflow, args.no_role_check)
    params = _parse_params(args.param)
    base_dir = Path(args.workflow).resolve().parent

    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_dir = Path(args.runs_root) / f"{wf.name}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(args.workflow, run_dir / "workflow.xml")
    (run_dir / "params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        executor = Executor(wf, params, run_dir, base_dir=base_dir,
                            permission_mode=args.permission_mode)
    except WorkflowFailure as e:
        sys.exit(f"error: {e}")
    try:
        executor.run()
    except WorkflowFailure as e:
        _report(executor, "FAILED")
        print(f"error: {e}", file=sys.stderr)
        print(f"resume with: wfrun resume {run_dir}", file=sys.stderr)
        return 1
    _report(executor, "SUCCESS")
    return 0


def cmd_resume(args) -> int:
    run_dir = Path(args.run_dir)
    wf_path = run_dir / "workflow.xml"
    params_path = run_dir / "params.json"
    if not wf_path.is_file():
        sys.exit(f"error: {wf_path} not found (not a run directory?)")
    wf = parser.parse_file(wf_path)
    params = json.loads(params_path.read_text(encoding="utf-8")) if params_path.is_file() else {}
    events = load_events(run_dir)
    base_dir = Path(args.base_dir).resolve() if args.base_dir else Path.cwd()

    try:
        executor = Executor(wf, params, run_dir, base_dir=base_dir,
                            permission_mode=args.permission_mode,
                            replay_events=events)
    except WorkflowFailure as e:
        sys.exit(f"error: {e}")
    try:
        executor.run()
    except WorkflowFailure as e:
        _report(executor, "FAILED")
        print(f"error: {e}", file=sys.stderr)
        return 1
    _report(executor, "SUCCESS")
    return 0


def _tree_lines(node, depth=0):
    pad = "  " * depth
    if isinstance(node, model.Seq):
        for child in node.children:
            yield from _tree_lines(child, depth)
    elif isinstance(node, model.Step):
        extras = [f"role={node.role}" if node.role else "role=inline"]
        if node.mode:
            extras.append(f"mode={node.mode}")
        if node.model:
            extras.append(node.model)
        if node.retry:
            extras.append(f"retry={node.retry}")
        if node.on_error != model.DEFAULT_ON_ERROR:
            extras.append(f"on-error={node.on_error}")
        if node.output:
            extras.append(f"-> {node.output}" +
                          ("" if node.output_type == model.DEFAULT_OUTPUT_TYPE
                           else f" ({node.output_type})"))
        yield f"{pad}step {node.id} ({', '.join(extras)})"
    elif isinstance(node, model.Replan):
        extras = [f"role={node.role}" if node.role else "role=inline",
                  f"max-steps={node.max_steps}"]
        if node.retry:
            extras.append(f"retry={node.retry}")
        if node.on_error != model.DEFAULT_ON_ERROR:
            extras.append(f"on-error={node.on_error}")
        if node.outputs:
            extras.append(f"-> {', '.join(node.outputs)}")
        yield f"{pad}replan {node.id} ({', '.join(extras)})"
    elif isinstance(node, model.SetVar):
        yield f"{pad}set {node.var}"
    elif isinstance(node, model.If):
        cond = node.test if node.test is not None else f"ask: {node.ask}"
        yield f"{pad}if {cond}"
        yield from _tree_lines(node.then, depth + 1)
        if node.else_ is not None:
            yield f"{pad}else"
            yield from _tree_lines(node.else_, depth + 1)
    elif isinstance(node, model.While):
        cond = node.test if node.test is not None else f"ask: {node.ask}"
        yield f"{pad}while {cond} (max {node.max})"
        yield from _tree_lines(node.body, depth + 1)
    elif isinstance(node, model.Each):
        source = node.items or node.glob or f"range {node.range_}"
        yield f"{pad}each {source} as {node.as_}"
        yield from _tree_lines(node.body, depth + 1)
    elif isinstance(node, model.Parallel):
        yield f"{pad}parallel (max-workers {node.max_workers})"
        for child in node.children:
            yield from _tree_lines(child, depth + 1)


def _load_vars(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"error: cannot load vars file {path}: {e}")
    if not isinstance(data, dict):
        sys.exit(f"error: vars file {path} must contain a JSON object")
    return data


def cmd_interp(args) -> int:
    variables = _load_vars(args.vars)
    try:
        print(interp_mod.interpolate(args.text, variables))
    except interp_mod.InterpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_eval(args) -> int:
    variables = _load_vars(args.vars)
    try:
        value = bool(interp_mod.safe_eval(args.expr, variables))
    except interp_mod.InterpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("true" if value else "false")
    return 0


def cmd_ask(args) -> int:
    from .claude_cli import ask_llm  # deferred: needs claude CLI only here
    question = args.question
    if args.vars:
        try:
            question = interp_mod.interpolate(question, _load_vars(args.vars))
        except interp_mod.InterpError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    try:  # wfrun ask always dispatches through the claude CLI -> "cc" table
        ask_model = modelmap.resolve(args.model, "cc")
    except modelmap.ModelMapError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    answer, reason, cost = ask_llm(question, model=ask_model, cwd=args.base_dir)
    if answer is None:
        print(f"error: ask judgment failed: {reason}", file=sys.stderr)
        return 2
    payload = {"answer": answer, "reason": reason, "cost_usd": round(cost, 6)}
    if args.log:
        with Path(args.log).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "ask", "question": question, **payload},
                               ensure_ascii=False) + "\n")
    if args.quiet:
        print("true" if answer else "false")
    else:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


# run-llm's no-read firewall is prompt-level unless a PreToolUse hook backs it
# (references/run-llm.md, "Enforcement boundaries"). The recommended snippet
# carries this marker so its presence is checkable.
LLM_GUARD_MARKER = "xml-wf-llm-guard"


def _warn_if_no_llm_guard():
    for settings in (Path(".claude/settings.json"),
                     Path(".claude/settings.local.json"),
                     Path.home() / ".claude" / "settings.json"):
        try:
            if LLM_GUARD_MARKER in settings.read_text(encoding="utf-8"):
                return
        except OSError:
            continue
    print(f"note: no '{LLM_GUARD_MARKER}' hook in .claude settings — the "
          "run-llm content firewall is prompt-level only "
          "(see references/run-llm.md, Enforcement boundaries)", file=sys.stderr)


def cmd_prompt(args) -> int:
    from .agents import discover_agents
    if args.result:  # run-llm signature: file-based response protocol
        _warn_if_no_llm_guard()
    wf = parser.parse_file(args.workflow)
    variables = _load_vars(args.vars)
    base_dir = Path(args.workflow).resolve().parent
    agents_cache = discover_agents(base_dir)
    try:
        step = stepio.find_step(wf, args.step_id)
        if isinstance(step, model.Replan):
            prompt = stepio.build_replan_prompt(
                step, variables, agents_cache,
                fix=args.fix, result_path=args.result)
        else:
            prompt = stepio.build_step_prompt(
                wf, step, variables, base_dir=base_dir,
                fix=args.fix, agents_cache=agents_cache,
                result_path=args.result)
    except stepio.StepIOError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(prompt, encoding="utf-8")
    # Print only control-plane facts — never the prompt content.
    # model/tools are the RESOLVED dispatch values (step attrs > role frontmatter).
    kind = "replan" if isinstance(step, model.Replan) else "step"
    dispatch_model, dispatch_tools = stepio.dispatch_for(step, agents_cache)
    try:  # the dispatch line carries the runner-resolved name ("llm" table)
        resolved = modelmap.resolve(dispatch_model, "llm")
    except modelmap.ModelMapError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    facts = [kind, f"role={step.role}" if step.role else "role=inline"]
    if getattr(step, "mode", None):
        facts.append(f"mode={step.mode}")
    if resolved:
        facts.append(f"model={resolved}"
                     + ("" if resolved == dispatch_model
                        else f" (mapped from {dispatch_model})"))
    if step.effort:
        facts.append(f"effort={step.effort}")
    if dispatch_tools and not isinstance(step, model.Replan):
        facts.append(f"tools={dispatch_tools}")
    print(f"{out} ({', '.join(facts)})")
    return 0


def cmd_record(args) -> int:
    wf = parser.parse_file(args.workflow)
    try:
        step = stepio.find_step(wf, args.step_id)
    except stepio.StepIOError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    ok, message = stepio.record_result(step, args.result, args.vars, args.log)
    print(message)
    return 0 if ok else 1


def cmd_plan(args) -> int:
    wf = parser.parse_file(args.workflow)
    steps = list(wf.iter_steps())
    print(f"workflow: {wf.name} (max={wf.max}"
          + (f", budget-usd={wf.budget_usd}" if wf.budget_usd else "") + ")")
    for p in wf.params:
        flag = "required" if p.required else f"default={p.default!r}"
        print(f"param:    {p.name} ({flag})")
    print(f"steps:    {len(steps)} static definitions")
    print()
    for line in _tree_lines(wf.body):
        print(line)
    return 0


def cmd_viz(args) -> int:
    wf = parser.parse_file(args.workflow)
    text = viz.mermaid(wf)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(out)
    else:
        print(text, end="")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wfrun",
                                 description="Deterministic XML workflow runner "
                                             "(steps run as isolated claude -p subagents)")
    sub = ap.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="static validation (lint)")
    p_val.add_argument("workflow")
    p_val.add_argument("--json", action="store_true")
    p_val.add_argument("--no-role-check", action="store_true",
                       help="skip existence checks for named roles (.claude/agents)")
    p_val.add_argument("--as-child", action="store_true",
                       help="validate a replan-generated continuation "
                            "(forbids <replan> and <param>)")
    p_val.add_argument("--defined-vars", metavar="VARS_JSON",
                       help="treat this vars file's keys as already defined")
    p_val.set_defaults(func=cmd_validate)

    p_run = sub.add_parser("run", help="validate then execute a workflow")
    p_run.add_argument("workflow")
    p_run.add_argument("-p", "--param", action="append", default=[],
                       metavar="KEY=VALUE")
    p_run.add_argument("--run-dir", help="explicit run directory")
    p_run.add_argument("--runs-root", default="runs")
    p_run.add_argument("--permission-mode",
                       help="forwarded to every claude -p call (e.g. acceptEdits)")
    p_run.add_argument("--no-role-check", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_res = sub.add_parser("resume", help="resume a failed run (skips recorded successes)")
    p_res.add_argument("run_dir")
    p_res.add_argument("--base-dir", help="project dir for agents/rules (default: cwd)")
    p_res.add_argument("--permission-mode")
    p_res.set_defaults(func=cmd_resume)

    p_plan = sub.add_parser("plan", help="print the step tree without executing")
    p_plan.add_argument("workflow")
    p_plan.set_defaults(func=cmd_plan)

    p_viz = sub.add_parser(
        "viz", help="render the control flow as a mermaid flowchart "
                    "(control-plane labels only, no task bodies)")
    p_viz.add_argument("workflow")
    p_viz.add_argument("--out", metavar="FILE",
                       help="write to this file instead of stdout")
    p_viz.set_defaults(func=cmd_viz)

    p_interp = sub.add_parser(
        "interp", help="interpolate {var} references in text against a vars JSON file")
    p_interp.add_argument("text")
    p_interp.add_argument("--vars", required=True, metavar="VARS_JSON")
    p_interp.set_defaults(func=cmd_interp)

    p_eval = sub.add_parser(
        "eval", help="evaluate a test= expression against a vars JSON file (prints true/false)")
    p_eval.add_argument("expr")
    p_eval.add_argument("--vars", required=True, metavar="VARS_JSON")
    p_eval.set_defaults(func=cmd_eval)

    p_ask = sub.add_parser(
        "ask", help="LLM condition judgment with forced structured output (prints JSON)")
    p_ask.add_argument("question")
    p_ask.add_argument("--vars", metavar="VARS_JSON",
                       help="optional vars file for {var} interpolation in the question")
    p_ask.add_argument("--model", default=model.DEFAULT_ASK_MODEL)
    p_ask.add_argument("--base-dir", help="cwd for the judgment agent (file reads)")
    p_ask.add_argument("--quiet", action="store_true",
                       help="print only true/false (keeps reason out of the caller)")
    p_ask.add_argument("--log", metavar="FILE",
                       help="append the full judgment JSON to this file")
    p_ask.set_defaults(func=cmd_ask)

    p_prompt = sub.add_parser(
        "prompt", help="assemble a step's full prompt into a file (LLM orchestrator: "
                       "task content never passes through the caller)")
    p_prompt.add_argument("workflow")
    p_prompt.add_argument("step_id")
    p_prompt.add_argument("--vars", required=True, metavar="VARS_JSON")
    p_prompt.add_argument("--out", required=True, metavar="PROMPT_FILE")
    p_prompt.add_argument("--result", metavar="RESULT_FILE",
                          help="append the file-based response protocol pointing here")
    p_prompt.add_argument("--fix", help="debug-granted fix instruction to append")
    p_prompt.set_defaults(func=cmd_prompt)

    p_rec = sub.add_parser(
        "record", help="record a step's result file: update vars, append log, "
                       "print only ok/error")
    p_rec.add_argument("workflow")
    p_rec.add_argument("step_id")
    p_rec.add_argument("--result", required=True, metavar="RESULT_FILE")
    p_rec.add_argument("--vars", required=True, metavar="VARS_JSON")
    p_rec.add_argument("--log", metavar="LOG_FILE")
    p_rec.set_defaults(func=cmd_record)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except parser.ParseError as e:
        print(f"[ERROR] parse: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
