"""wfrun CLI: validate / run / resume / plan, plus LLM-orchestrator helpers
(interp / eval / ask) that keep interpolation, condition evaluation and ask
judgment deterministic even when an LLM drives the control flow."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import adjudicate as adjudicate_mod
from . import adp, claude_cli
from . import decision as decision_mod
from . import interp as interp_mod
from . import lint as lint_mod
from . import model, modelmap, modes, parser, stepio, viz
from .ccdirs import claude_config_dirs
from .executor import DecisionRequested, Executor, WorkflowFailure, decision_tables
from .state import RunState, load_events


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
    if executor.protocol_warnings:
        # D9's warning face: stray protocol tokens inside successful
        # responses. The run stands; the trace is the point.
        print("warnings:")
        for warning in executor.protocol_warnings:
            print(f"  {warning}")


def _request_options(path: str) -> list[str]:
    """The request's own numbered options, re-read from the payload file.

    Read back rather than carried on the event on purpose: the payload is the
    authority for the numbering the answer will use, so the report must not be
    able to print a different list from the one the human opens
    (xml-wf-decision-request.md §1)."""
    try:
        payload, _ = decision_mod.parse_payload(
            Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []
    return decision_mod.render_options(payload) if payload else []


def _report_decisions(records: list[dict], run_dir: Path, *, on_hold: bool = False):
    """Print the raised decision requests.

    For a batch run this output IS the interface (xml-wf-decision-request.md
    §2, §8): `wfrun run` is its own process with no live turn to ask in, so
    everything the human needs to answer — payload path, the numbered options,
    where to write, and the exact resume command — has to be on stdout before
    the process exits.
    """
    if not records:
        return
    print()
    print(f"decision requests: {len(records)}")
    for record in records:
        print(f"  step '{record['key']}' ({record['request_id']})")
        print(f"    request: {record['request']}")
        if record.get("preamble_lines"):
            # D9: the payload arrived below preamble prose; the request file
            # holds only the anchored payload, the full response stays in the
            # step's attempt record.
            print(f"    note: the response put {record['preamble_lines']} "
                  "line(s) of prose above the payload (protocol deviation; "
                  "not filed)")
        for line in _request_options(record["request"]):
            print(f"    {line}")
        print(f"    answer file: {record['answer_path']}")
    if on_hold:
        print()
        print("these are on hold: a step in the same <parallel> failed outright, "
              "and that has to be fixed before any answer can help. The payloads "
              "above are saved and will still be pending after the fix.")
        return
    print()
    # ASCII only, deliberately: this block IS the interface for a stopped run,
    # and stdout here is whatever console the human ran wfrun on -- a cp932
    # terminal turns one stray em-dash into a UnicodeEncodeError that hides
    # the instructions entirely.
    print("answer format (the first line is parsed; the rest is free text):")
    print("  option: <a number from the list above, or 'none'>")
    print("  <why, or what to do instead: required when the option is 'none'>")
    print()
    flags = " ".join(f"--answer {r['key']}={r['answer_path']}" for r in records)
    print(f"then: wfrun resume {run_dir} {flags}")
    print("if the fork invalidates the rest of the workflow, do not resume: "
          "split or rebuild the XML instead")


def _backend_executor_kwargs(backend: str) -> dict:
    """The Executor constructor kwargs (run_claude=/ask_llm=/diagnose=/
    adjudicate=/model_runner=) for a resolved backend ("cc" or "pi" -- never
    "auto"). Shared by cmd_run and cmd_resume so a resumed run always
    reconstructs the same execution facility it started with (design §1, §3.3).

    `adjudicate=` splits by backend for the same reason `run_claude=` does: the
    cc adjudicator forces structured output, which pi has no equivalent for, so
    the pi one takes the ruling as §13.3 text instead. Both land in the same
    parser (xml-wf-decision-request.md §17.1)."""
    if backend == "cc":
        return dict(run_claude=claude_cli.run_claude, ask_llm=claude_cli.ask_llm,
                    diagnose=adp.diagnose,
                    adjudicate=adjudicate_mod.adjudicate, model_runner="cc")
    from . import pi_cli  # deferred: needs the pi CLI only when backend=="pi"
    return dict(run_claude=pi_cli.run_pi, ask_llm=pi_cli.ask_llm_pi,
                diagnose=pi_cli.diagnose_stub_pi,
                adjudicate=adjudicate_mod.adjudicate_pi, model_runner="llm")


def cmd_run(args) -> int:
    wf = _load_validated(args.workflow, args.no_role_check)
    backend = _resolve_backend(args.backend)

    if backend == "pi":
        from . import pi_cli  # deferred: needs the pi CLI only for this check
        violations = pi_cli.pi_compat_errors(wf)
        if violations:
            for msg in violations:
                print(msg, file=sys.stderr)
            return 1

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
    # Recorded so `resume` inherits the same backend rather than
    # re-detecting it -- a switch mid-run would break event consistency
    # (design §3.3).
    (run_dir / "backend.json").write_text(
        json.dumps({"backend": backend}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    # Same reasoning for --inherit-model (design phase6 review point 2):
    # always written (never absent going forward, unlike backend.json's
    # pre-existing-run gap) so a step that executes only after a resume
    # still gets the model the original run was given, not a re-detected
    # one. `None` here means "none was given" -- distinct from the model
    # attribute being merely absent on a particular step.
    (run_dir / "inherit_model.json").write_text(
        json.dumps({"inherit_model": args.inherit_model}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    try:
        executor = Executor(wf, params, run_dir, base_dir=base_dir,
                            permission_mode=args.permission_mode,
                            inherit_model=args.inherit_model,
                            **_backend_executor_kwargs(backend))
    except WorkflowFailure as e:
        sys.exit(f"error: {e}")
    for msg in executor.model_inherit_warnings():
        print(f"note: {msg}", file=sys.stderr)
    if backend == "pi":
        from . import pi_cli  # deferred: pi-only advisory
        for msg in pi_cli.pi_tool_widening_notes(wf, executor._agents_cache):
            print(f"note: {msg}", file=sys.stderr)
    try:
        executor.run()
    except DecisionRequested:
        _report(executor, "AWAITING-DECISION")
        _report_decisions(executor.decisions_raised, run_dir)
        return 4
    except WorkflowFailure as e:
        _report(executor, "FAILED")
        print(f"error: {e}", file=sys.stderr)
        print(f"resume with: wfrun resume {run_dir}", file=sys.stderr)
        _report_decisions(executor.decisions_raised, run_dir, on_hold=True)
        return 1
    _report(executor, "SUCCESS")
    return 0


class AnswerError(Exception):
    """A `--answer` that cannot be honored. Always raised before the Executor
    exists, so a rejected answer leaves the run exactly as it was."""


def _ingest_answers(run_dir: Path, specs: list[str],
                    events: list[dict]) -> list[dict]:
    """Adjudicate `--answer` flags and return the extended event list (§13.4).

    Order is load-bearing: every append happens here, before `Executor` is
    constructed, because the synthetic success event a form-(a) answer writes
    has to already be in the list the ReplayCursor is built from — that event
    IS what makes (a) cost nothing.
    """
    if not specs:
        return events
    pending, _, _, _ = decision_tables(events)
    by_step = {step: event for (step, _cycle), event in pending.items()}
    answered_steps = {e["key"] for e in events if e.get("kind") == "answer"}
    state = RunState(run_dir)
    appended: list[dict] = []

    for spec in specs:
        if "=" not in spec:
            raise AnswerError(f"--answer expects STEP_ID=PATH, got '{spec}'")
        step_id, _, raw_path = spec.partition("=")
        step_id = step_id.strip()
        request = by_step.get(step_id)
        if request is None:
            if step_id in answered_steps:
                raise AnswerError(
                    f"--answer {step_id}=...: that request was already "
                    "answered. A recorded answer is never replaced; later "
                    "steps may already have been built on it. Resume without "
                    "--answer for this step; if the answer was wrong, start a "
                    "new run.")
            known = ", ".join(sorted(by_step)) or "(none)"
            raise AnswerError(
                f"--answer {step_id}=...: no decision request is pending for "
                f"step '{step_id}'. Pending: {known}")

        answer_file = Path(raw_path.strip())
        try:
            text = answer_file.read_text(encoding="utf-8")
        except OSError as e:
            raise AnswerError(f"--answer {step_id}: cannot read {answer_file}: {e}")
        option_count = int(request.get("option_count") or 0)
        answer, errors = decision_mod.parse_answer(text, option_count)
        if errors:
            raise AnswerError(
                f"--answer {step_id} ({answer_file}): {'; '.join(errors)}\n"
                f"       the request and its {option_count} option(s) are in "
                f"{request['request']}")

        # A reason recorded when the step stopped still stands; the answer can
        # only add one. Then, and only then, is the artifact's existence
        # re-checked — it was true when the run stopped, but the human has had
        # the filesystem to themselves since (§13.2).
        b_reason = request.get("b_reason")
        if not b_reason:
            # One shared rule for all three adjudication sites
            # (decision.answer_b_reason): form (a) adopts the payload's
            # `output:`, which only describes what the step would produce
            # under its own recommendation, so any other ruling has to re-run
            # the step rather than silently substitute a value nobody chose.
            b_reason = decision_mod.answer_b_reason(
                answer, request.get("recommendation"))
        missing: list[str] = []
        if not b_reason:
            missing = [p for p in request.get("expect_files") or []
                       if not Path(p).is_file()]
            if missing:
                b_reason = decision_mod.B_REASON_MISSING_FILE_AT_RESUME
        verdict = "b" if b_reason else "a"

        appended.append(state.event(
            "answer", key=step_id, cycle=request["cycle"],
            request_id=request["request_id"], request=request["request"],
            # Always human: `resume --answer` IS the human path. An llm
            # decider settles in-process (§15.1) and never arrives here, so
            # recording the workflow's declared decider would mislabel a
            # person's ruling -- and, through decision_tables, spend the §7
            # cap that human answers are defined not to consume.
            answer_path=str(answer_file), decider=model.DECIDER_HUMAN,
            option=answer.option, verdict=verdict, b_reason=b_reason,
            missing=missing))
        if verdict == "a":
            appended.append(state.event(
                "step", key=step_id, status="success", via="decision-a",
                request_id=request["request_id"], attempts=1,
                cost_usd=float(request.get("cost_usd") or 0.0),
                output_value=request.get("output")))

        chosen = "none" if answer.option is None else f"option {answer.option}"
        note = f" [{b_reason}]" if b_reason else ""
        print(f"answer: {step_id} -> {chosen}; continues as form "
              f"({verdict}){note}")
    return events + appended


def cmd_resume(args) -> int:
    run_dir = Path(args.run_dir)
    wf_path = run_dir / "workflow.xml"
    params_path = run_dir / "params.json"
    if not wf_path.is_file():
        sys.exit(f"error: {wf_path} not found (not a run directory?)")
    wf = parser.parse_file(wf_path)
    params = json.loads(params_path.read_text(encoding="utf-8")) if params_path.is_file() else {}
    events = load_events(run_dir)
    try:
        events = _ingest_answers(run_dir, args.answer, events)
    except AnswerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    base_dir = Path(args.base_dir).resolve() if args.base_dir else Path.cwd()

    # Backend is inherited from the original run, never re-detected: a
    # resume whose environment now looks different must not switch
    # execution facilities mid-run (design §3.3). A run predating backend
    # tracking (no backend.json) defaults to "cc", the only backend that
    # existed before this file existed.
    backend_path = run_dir / "backend.json"
    backend = (json.loads(backend_path.read_text(encoding="utf-8"))["backend"]
              if backend_path.is_file() else "cc")

    # --inherit-model is inherited the same way, for the same reason (design
    # phase6 review point 2): no CLI override on resume, always the value
    # the original run was given. A run predating this file (no
    # inherit_model.json) falls back to None -- "none was given" -- same as
    # backend.json's pre-existing-run fallback above.
    inherit_model_path = run_dir / "inherit_model.json"
    inherit_model = (json.loads(inherit_model_path.read_text(encoding="utf-8"))["inherit_model"]
                     if inherit_model_path.is_file() else None)

    try:
        executor = Executor(wf, params, run_dir, base_dir=base_dir,
                            permission_mode=args.permission_mode,
                            replay_events=events,
                            inherit_model=inherit_model,
                            **_backend_executor_kwargs(backend))
    except WorkflowFailure as e:
        sys.exit(f"error: {e}")
    try:
        executor.run()
    except DecisionRequested:
        _report(executor, "AWAITING-DECISION")
        _report_decisions(executor.decisions_raised, run_dir)
        return 4
    except WorkflowFailure as e:
        _report(executor, "FAILED")
        print(f"error: {e}", file=sys.stderr)
        _report_decisions(executor.decisions_raised, run_dir, on_hold=True)
        return 1
    _report(executor, "SUCCESS")
    return 0


def _tree_lines(node, depth=0):
    pad = "  " * depth
    if isinstance(node, model.Seq):
        for child in node.children:
            yield from _tree_lines(child, depth)
    elif isinstance(node, model.Step):
        extras = [x for x in (model.role_label(node),) if x]
        if node.mode:
            extras.append(f"mode={node.mode}")
        if node.model:
            extras.append(node.model)
        if node.retry:
            extras.append(f"retry={node.retry}")
        if node.on_error != model.DEFAULT_ON_ERROR:
            extras.append(f"on-error={node.on_error}")
        if node.decider:  # only when it overrides the workflow-level setting
            extras.append(f"decider={node.decider}")
        if node.decider_model:
            # Shown for the same reason the workflow-level line shows it: plan
            # output is the run-llm orchestrator's only view of the workflow,
            # and it cannot name the model for a delegated adjudication it
            # cannot see (§11, §15.7).
            extras.append(f"decider-model={node.decider_model}")
        if node.output:
            extras.append(f"-> {node.output}" +
                          ("" if node.output_type == model.DEFAULT_OUTPUT_TYPE
                           else f" ({node.output_type})"))
        yield f"{pad}step {node.id} ({', '.join(extras)})"
    elif isinstance(node, model.Replan):
        extras = [x for x in (model.role_label(node),) if x]
        extras.append(f"max-steps={node.max_steps}")
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


def _detect_ask_backend() -> str:
    """Deterministic `--backend auto` resolution: the same env signal
    mode-orchestrator's Step -1 uses for harness detection (non-empty
    CLAUDE_CODE_SESSION_ID => Claude Code => "cc"; empty => "pi"). See
    mode-orchestrator-runs/phase5-item1-cc-inventory-design.md §2.3 -- this
    is deliberately NOT left to the calling orchestrator's own judgment: a
    forgotten/wrong flag would otherwise fail silently (ask would just run
    against whichever CLI happens to be on PATH)."""
    return "cc" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "pi"


def _resolve_backend(explicit: str) -> str:
    """`--backend {auto,cc,pi}` resolution shared by `run` and `ask`
    (mode-orchestrator-runs/phase6-run-pi-design.md §3.1: "same determinant,
    same default" as `ask --backend`, _detect_ask_backend promoted to
    shared use). auto detects from CLAUDE_CODE_SESSION_ID; an explicit
    cc/pi that disagrees with the detected environment is honored but
    warned about -- a forgotten/wrong flag would otherwise silently
    dispatch against the wrong CLI."""
    detected = _detect_ask_backend()
    backend = detected if explicit == "auto" else explicit
    if explicit != "auto" and explicit != detected:
        print(f"warning: --backend {explicit} given but the environment "
              f"looks like '{detected}' (CLAUDE_CODE_SESSION_ID is "
              f"{'set' if detected == 'cc' else 'unset'}); proceeding with "
              f"the explicit --backend {explicit} anyway", file=sys.stderr)
    return backend


def cmd_ask(args) -> int:
    question = args.question
    if args.vars:
        try:
            question = interp_mod.interpolate(question, _load_vars(args.vars))
        except interp_mod.InterpError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    backend = _resolve_backend(args.backend)

    try:  # "cc" table for the claude CLI, "llm" table for everything else
        ask_model = modelmap.resolve(args.model, "cc" if backend == "cc" else "llm")
    except modelmap.ModelMapError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if backend == "cc":
        from .claude_cli import ask_llm  # deferred: needs claude CLI only here
        answer, reason, cost = ask_llm(question, model=ask_model, cwd=args.base_dir)
    else:
        from .pi_cli import ask_llm_pi  # deferred: needs pi CLI only here
        answer, reason, cost = ask_llm_pi(question, model=ask_model, cwd=args.base_dir)

    if answer is None:
        print(f"error: ask judgment failed: {reason}", file=sys.stderr)
        return 2
    payload = {"answer": answer, "reason": reason, "cost_usd": round(cost, 6)}
    if args.log:
        with Path(args.log).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "ask", "backend": backend, "question": question,
                               **payload}, ensure_ascii=False) + "\n")
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
    # The hook is a Claude Code PreToolUse mechanism; only under CC is
    # "is it configured" even a meaningful question (see
    # mode-orchestrator-runs/phase5-item1-cc-inventory-design.md §3.2).
    # An unset CLAUDE_CODE_SESSION_ID means "not CC" -- not "Pi": the same
    # signal is unset when wfrun is run from a plain terminal.
    if not os.environ.get("CLAUDE_CODE_SESSION_ID"):
        print("note: not running under Claude Code — the run-llm content "
              "firewall is prompt-level only (no mechanical backstop "
              "available; see references/run-llm.md, Enforcement "
              "boundaries)", file=sys.stderr)
        return
    user_settings = [d / "settings.json" for d in claude_config_dirs()]
    for settings in (Path(".claude/settings.json"),
                     Path(".claude/settings.local.json"),
                     *user_settings):
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
            # Settled rulings are enumerated from the decisions ledger, never
            # passed in: the orchestrator carries no memory of them, so it
            # cannot drop one and let the step re-open a settled fork
            # (xml-wf-decision-request.md §14.3). Only the run-llm signature
            # (--result given) has a ledger to read.
            settled = (decision_mod.settled_pairs(
                stepio.decisions_dir_for_result(args.result), step.id)
                if args.result else None)
            prompt = stepio.build_step_prompt(
                wf, step, variables, base_dir=base_dir,
                fix=args.fix, agents_cache=agents_cache,
                result_path=args.result, decision=settled or None)
    except (stepio.StepIOError, decision_mod.DecisionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.result:
        result_file = Path(args.result)
        if result_file.is_file():
            # Stale-read prevention (reliability-spec.md §4.1, F6): a
            # leftover result file from a prior attempt must never be
            # mistaken for this attempt's output by `record`/`poll`.
            result_file.unlink()
        if not isinstance(step, model.Replan):
            # Sentinel/handle machinery is step-only: a <replan>'s own XML
            # well-formedness already proves completeness (§0, F1).
            (result_file.parent).mkdir(parents=True, exist_ok=True)
            stepio.handle_path(step.id, args.result).write_text(json.dumps({
                "step_id": step.id, "attempt": args.attempt,
                "dispatched_at": time.time(), "timeout": step.timeout,
                "result_path": str(args.result),
                "sentinel": stepio.sentinel_line(step.id),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
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
    facts = [kind] + [x for x in (model.role_label(step),) if x]
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


# `decision` (4) is a verdict, not a failure: run-llm's orchestrator decides
# whether to redo a step from the exit code, and returning 1 for a decision
# would have it re-dispatch a step that is waiting on a ruling — executing it
# twice (xml-wf-decision-request.md §8). `rerun` (5) is the answered-form-(b)
# verdict: settled, and the step must run again from move 1 (§14.2).
RECORD_EXIT_CODES = {"ok": 0, "error": 1, "aborted": 3, "decision": 4, "rerun": 5}


def cmd_record(args) -> int:
    wf = parser.parse_file(args.workflow)
    try:
        step = stepio.find_step(wf, args.step_id)
    except stepio.StepIOError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.answer:
        # Adjudication, not recording: the result file is not re-read (the
        # request was filed out of its way when the decision was detected).
        #
        # Who ruled is resolved here, from the workflow -- the orchestrator is
        # not asked to remember it (§14's no-new-memory invariant). --decider
        # human overrides it for the paths that hand the fork back to a person
        # (escalation, a spent cap, a ruling that did not parse), so those
        # answers do not spend the llm cap they are the fallback for (§7).
        declared, _ = model.resolve_decider(wf, step)
        try:
            status, message = stepio.adjudicate_answer(
                step, args.result, args.vars, args.answer, args.log,
                decider=(model.DECIDER_HUMAN if args.decider == "human"
                         else declared))
        except stepio.StepIOError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    else:
        status, message = stepio.record_result(step, args.result, args.vars,
                                               args.log, reply=args.reply)
    print(message)
    return RECORD_EXIT_CODES[status]


def cmd_poll(args) -> int:
    handle_file = Path(args.handle)
    try:
        handle = json.loads(handle_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot load handle file {handle_file}: {e}", file=sys.stderr)
        return 2
    result_path = Path(handle["result_path"])
    if result_path.is_file():
        raw = result_path.read_text(encoding="utf-8")
        text = modes.strip_mode_line(raw)
        _, sentinel_ok = stepio.strip_sentinel_line(text, handle["step_id"])
        if sentinel_ok:
            print("done")
            return 0
    # No kill authority here (B-layer limitation, reliability-spec.md §4.3):
    # this only ever declares a verdict, never terminates the subagent.
    if time.time() - handle["dispatched_at"] > handle["timeout"]:
        print("deadline-exceeded")
        return 11
    print("running")
    return 10


# ===== A layer (reliability-spec.md §5): dispatch a detached wrapper that
# self-enforces the step timeout with kill-tree cleanup, poll its exit.json
# via `wait`. `_wrapper` is launched only by `dispatch` -- never invoke it
# directly. =====

# Attempt-cap enforcement (F4/P5): one dispatch call = one attempts.json
# entry, whatever kind (deterministic retry, the one debug-granted retry, or
# the one aborted-redispatch) -- a single flat cap covers all three without
# separate counters. Exceeding it is exactly the unbounded-relaunch pattern
# that produced the P3/C3 retry-storm incident this spec responds to.
DISPATCH_DEBUG_GRANTS = 1
DISPATCH_ABORTED_REDISPATCH = 1
# Extra cap headroom per decision already raised in this cycle: the request
# attempt itself, plus the one re-run its answer grants. The cap is a runaway
# backstop, and a loop that needs a human keystroke every turn is not a
# runaway -- without this, a legitimately-answered step would hit "cap
# exceeded" while a decision-free runaway still stops at the same count
# (xml-wf-decision-request.md §13.8).
DISPATCH_DECISION_GRANTS = 2

WAIT_POLL_INTERVAL = 1.0
# Grace beyond the wrapper's own step.timeout before `wait` gives up on ever
# seeing exit.json and calls it aborted (wrapper process itself vanished --
# killed externally, crashed, host issue). Not in reliability-spec.md §5.1's
# literal 3-outcome list for `wait` (ok/error/running); added deliberately
# so this layer has a path out of "running" forever, matching what `poll`
# already provides for the B layer -- without it, a wrapper that never
# writes exit.json reproduces the exact P1 incident (passive wait, no
# liveness signal) this whole spec exists to close.
WAIT_ABORT_MARGIN = 30


def _check_base_dir(base_dir: Path) -> str | None:
    """Same guard as Executor._check_base_dir (executor.py), for the A-layer
    commands below, which don't go through Executor. Returns an error
    message, or None if the directory is fine. Protects both `~/.claude` and
    the `CLAUDE_CONFIG_DIR` dir when set (union) — see executor.py's
    docstring for why union rather than env-only."""
    base = base_dir.resolve()
    for protected in (d.resolve() for d in claude_config_dirs()):
        if base == protected or protected in base.parents:
            return (f"base dir {base} is inside Claude's config tree "
                   f"({protected}) — the claude CLI requires interactive "
                   "write approval there, so steps cannot write files; copy "
                   "the workflow to a normal project directory and run it "
                   "from there")
    return None


def _a_layer_paths(run_dir: Path, step_id: str, cycle: int) -> dict:
    """Per-CYCLE artifact paths for one step.

    A "cycle" is one step-node visit -- run-cc's unit for the retry budget
    (`Executor._exec_step` gives every visit a fresh `attempt` counter).
    `<while>`/`<each>` legitimately visit the same step id repeatedly, so
    keying these files on step id alone made iteration 2 inherit iteration
    1's attempt ledger (tripping the dispatch cap) and overwrite its
    prompt/result (destroying the audit trail). The `_cNN_` segment gives
    each visit its own ledger and its own record, mirroring run-cc's
    per-attempt `steps/<id>_<nn>/` directories.
    """
    steps_dir = run_dir / "steps"
    stem = f"{step_id}_c{cycle:02d}"
    return {
        "run_dir": run_dir, "steps_dir": steps_dir, "cycle": cycle,
        "system": steps_dir / f"{stem}_system.md",
        "prompt": steps_dir / f"{stem}_prompt.md",
        "handle": steps_dir / f"{stem}_handle.json",
        "exit": steps_dir / f"{stem}_exit.json",
        "result": steps_dir / f"{stem}_result.json",
        "attempts": steps_dir / f"{stem}_attempts.json",
        "wait": steps_dir / f"{stem}_wait.json",
    }


def _latest_cycle(steps_dir: Path, step_id: str) -> int | None:
    """Highest cycle number already dispatched for this step, or None.

    Keyed off the handle file because `dispatch` writes it synchronously --
    attempts.json only appears once a wrapper finishes, so a wrapper that
    died would otherwise make its cycle invisible and silently restart the
    ledger.
    """
    pattern = re.compile(rf"{re.escape(step_id)}_c(\d+)_handle\.json\Z")
    cycles = []
    try:
        names = [p.name for p in steps_dir.glob(f"{step_id}_c*_handle.json")]
    except OSError:
        return None
    for name in names:
        m = pattern.match(name)
        if m:
            cycles.append(int(m.group(1)))
    return max(cycles) if cycles else None


def _resolve_cycle(run_dir: Path, step_id: str, force_new: bool
                   ) -> tuple[int, list]:
    """(cycle, attempts-so-far-in-that-cycle) for a dispatch about to run.

    A dispatch continues the current cycle (retry / debug-granted retry /
    aborted redispatch -- all capped together) unless it starts a new one:

    - `force_new` (`--new-cycle`): the orchestrator declares it has accepted
      the previous outcome and moved on. Needed for a loop whose body step
      ended in a failure the workflow chose to tolerate (`on-error="ignore"`)
      -- the ledger cannot infer acceptance from a failed outcome.
    - the previous cycle's last attempt succeeded: nobody retries a success,
      so a further dispatch of the same step can only be the next loop
      iteration. Inferred automatically so the common case costs the
      orchestrator nothing.

    Resetting the ledger is safe because it is not the global runaway
    backstop -- `wf.max` (counted from steps.log, and never reset) is.
    """
    latest = _latest_cycle(run_dir / "steps", step_id)
    if latest is None:
        return 1, []
    attempts = stepio.load_attempts(
        _a_layer_paths(run_dir, step_id, latest)["attempts"])
    last_class = attempts[-1].get("class") if attempts else None
    if force_new or last_class == "ok":
        return latest + 1, []
    return latest, attempts


def _count_step_executions(log_path: Path) -> int:
    """steps.log entries that are step executions, for the `wf.max` cap.

    Only entries carrying a "step" key count: `wfrun ask` writes its
    judgment records to the same file by run-llm.md's convention
    (`ask ... --log steps.log`), and an ask is a condition evaluation, not
    a step execution -- counting raw lines let a branch-heavy workflow hit
    "max reached" long before run-cc would.

    Retry attempts DO each count here, unlike run-cc (whose `_bump_limits`
    fires once per step-node visit, outside the retry loop). That is
    deliberate: in run-llm there is no in-process step counter, so this log
    is the only runaway backstop, and every attempt is a real `claude -p`
    call. Erring toward counting them keeps the cap's protective meaning.
    """
    count = 0
    try:
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and "step" in entry:
                    count += 1
    except OSError:
        return 0
    return count


def cmd_dispatch(args) -> int:
    from .agents import discover_agents
    wf = parser.parse_file(args.workflow)
    try:
        step = stepio.find_step(wf, args.step_id)
    except stepio.StepIOError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if isinstance(step, model.Replan):
        print("error: dispatch does not support <replan> nodes "
              "(reliability-spec.md §0: replans are exempt from the "
              "sentinel/handle machinery)", file=sys.stderr)
        return 2
    variables = _load_vars(args.vars)
    base_dir = Path(args.workflow).resolve().parent
    base_dir_error = _check_base_dir(base_dir)
    if base_dir_error:
        print(f"error: {base_dir_error}", file=sys.stderr)
        return 2
    agents_cache = discover_agents(base_dir)

    run_dir = Path(args.run_dir)
    (run_dir / "steps").mkdir(parents=True, exist_ok=True)
    cycle, attempts = _resolve_cycle(run_dir, args.step_id, args.new_cycle)
    paths = _a_layer_paths(run_dir, args.step_id, cycle)

    # The cap is per cycle (one step-node visit), matching run-cc's
    # per-visit retry budget; wf.max below is the run-wide backstop.
    decision_attempts = sum(1 for a in attempts if a.get("class") == "decision")
    cap = (step.retry + 1 + DISPATCH_DEBUG_GRANTS + DISPATCH_ABORTED_REDISPATCH
           + DISPATCH_DECISION_GRANTS * decision_attempts)
    if len(attempts) >= cap:
        print(f"error: dispatch cap exceeded for step '{args.step_id}' "
              f"(cycle {cycle}): {len(attempts)} attempts already recorded, "
              f"cap is {cap} (retry={step.retry} + 1 initial + "
              f"{DISPATCH_DEBUG_GRANTS} debug + "
              f"{DISPATCH_ABORTED_REDISPATCH} aborted-redispatch"
              + (f" + {DISPATCH_DECISION_GRANTS}x{decision_attempts} decision"
                 if decision_attempts else "") + "). "
              f"If this is a new <while>/<each> iteration rather than a "
              f"retry, pass --new-cycle", file=sys.stderr)
        return 1

    log_path = run_dir / "steps.log"
    executed = _count_step_executions(log_path)
    if executed >= wf.max:
        print(f"error: workflow max={wf.max} step executions reached "
              f"({executed} recorded in {log_path})", file=sys.stderr)
        return 1

    try:
        # Same auto-enumeration as the B layer, keyed per cycle since the A
        # layer has one (§14.3).
        settled = decision_mod.settled_pairs(
            decision_mod.decisions_dir(run_dir), f"{args.step_id}_c{cycle:02d}")
        system, prompt = stepio.build_step_prompt_parts(
            wf, step, variables, base_dir, fix=args.fix,
            agents_cache=agents_cache, decision=settled or None)
    except (stepio.StepIOError, decision_mod.DecisionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    dispatch_model, dispatch_tools = stepio.dispatch_for(step, agents_cache)
    try:  # a broken hand-edited model map is a startup error, not silently ignored
        dispatch_model = modelmap.resolve(dispatch_model, "cc")
    except modelmap.ModelMapError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    # Least privilege, same rule as run-cc: --permission-mode reaches only
    # steps whose resolved tools can write.
    permission = (args.permission_mode
                  if model.tools_can_write(dispatch_tools) else None)

    for p in (paths["exit"], paths["result"], paths["wait"]):
        # Stale-read prevention (same principle as B-layer's `prompt
        # --result`, reliability-spec.md §4.1 F6): a leftover exit/result
        # from a prior attempt must never be mistaken for this one's, and a
        # leftover wait record must not make this attempt look finished.
        if p.is_file():
            p.unlink()
    paths["system"].write_text(system, encoding="utf-8")
    paths["prompt"].write_text(prompt, encoding="utf-8")

    seq = len(attempts) + 1
    wrapper_cmd = [sys.executable, "-m", "wfrun", "_wrapper",
                  "--system-file", str(paths["system"]),
                  "--prompt-file", str(paths["prompt"]),
                  "--exit-file", str(paths["exit"]),
                  "--result-file", str(paths["result"]),
                  "--attempts-file", str(paths["attempts"]),
                  "--seq", str(seq), "--cwd", str(base_dir),
                  "--timeout", str(step.timeout)]
    if dispatch_model:
        wrapper_cmd += ["--model", dispatch_model]
    if step.effort:
        wrapper_cmd += ["--effort", step.effort]
    if dispatch_tools:
        wrapper_cmd += ["--tools", dispatch_tools]
    if step.schema:
        wrapper_cmd += ["--schema", step.schema]
    if permission:
        wrapper_cmd += ["--permission-mode", permission]

    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    else:
        popen_kwargs["start_new_session"] = True
    wrapper_proc = subprocess.Popen(
        wrapper_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, **popen_kwargs)

    started_at = time.time()
    paths["handle"].write_text(json.dumps({
        "wrapper_pid": wrapper_proc.pid, "attempt": seq, "cycle": cycle,
        "started_at": started_at, "timeout": step.timeout,
        # Not in reliability-spec.md §5.1's literal handle field list
        # ({wrapper_pid, attempt, started_at, timeout}), but `wait`'s own
        # signature (<handle> --max --vars --log, no xml/step-id) has
        # nowhere else to get the step definition it needs for its
        # record-equivalent job -- a genuine spec gap, resolved here by
        # widening the handle rather than the CLI surface.
        "xml": str(args.workflow), "step_id": args.step_id,
        "run_dir": str(run_dir), "exit_path": str(paths["exit"]),
        "result_path": str(paths["result"]),
        "system_path": str(paths["system"]), "prompt_path": str(paths["prompt"]),
        # Carried explicitly rather than re-derived from step_id: the
        # per-cycle stem means `wait` cannot reconstruct it from the id alone.
        "wait_path": str(paths["wait"]),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{paths['handle']} (dispatched, pid={wrapper_proc.pid}, "
          f"cycle={cycle}, seq={seq})")
    return 0


def cmd_run_wrapper(args) -> int:
    """Internal: launched detached by `cmd_dispatch` only. Runs the actual
    `claude -p` call with kill_tree=True (reliability-spec.md §5.1) and
    persists exit.json / result.json / an attempts.json entry -- never
    touches vars.json or steps.log (that is `wait`'s job)."""
    system = Path(args.system_file).read_text(encoding="utf-8")
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")

    launched = claude_cli._launch(
        prompt, system_prompt=system, model=args.model, effort=args.effort,
        tools=args.tools, schema=args.schema, timeout=args.timeout,
        cwd=args.cwd, permission_mode=args.permission_mode, kill_tree=True)

    exit_data = {"ended_at": time.time()}
    if isinstance(launched, claude_cli.CliResult):
        # Rejected before/without producing real output (env: resolution
        # failure or a shim loud-failure refusal; timeout: the claude
        # process itself never returned). There is no real stdout to
        # persist, so result.json stays empty and `wait` must NOT try to
        # re-derive a classification from it (empty stdout always reads as
        # `env` via classify_result, which would silently discard a real
        # `timeout`) -- exit_data carries the true classification directly.
        exit_data.update(returncode=launched.exit_code, stderr=launched.stderr,
                         early_class=launched.error_class, early_error=launched.error)
        raw_stdout = ""
        status = launched.error_class or "error"
    else:
        returncode, raw_stdout, stderr = launched
        exit_data.update(returncode=returncode, stderr=stderr)
        res = claude_cli.classify_result(returncode, raw_stdout, stderr, schema=args.schema)
        status = "ok" if res.ok else (res.error_class or "error")

    # Order matters: exit.json is the completion authority `wait` polls for,
    # so everything it will read must already be in place before it appears.
    # Both writes are atomic (stepio.write_text_atomic) so `wait` can never
    # observe a half-written file.
    stepio.write_text_atomic(args.result_file, raw_stdout)
    stepio.append_attempt(args.attempts_file, args.seq, status)
    stepio.write_text_atomic(args.exit_file,
                             json.dumps(exit_data, ensure_ascii=False, indent=2))
    return 0


def cmd_wait(args) -> int:
    handle_file = Path(args.handle)
    try:
        handle = json.loads(handle_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot load handle file {handle_file}: {e}", file=sys.stderr)
        return 2
    exit_path = Path(handle["exit_path"])
    call_deadline = time.time() + args.max
    # See WAIT_ABORT_MARGIN: a wrapper that never appears is a different
    # failure mode (the process vanished) from one still legitimately
    # running within its own timeout.
    abort_at = handle["started_at"] + handle["timeout"] + WAIT_ABORT_MARGIN

    while True:
        exit_data = _read_exit_json(exit_path)
        if exit_data is not None:
            return _finish_wait(handle, args, exit_data)
        now = time.time()
        if now >= abort_at:
            print("aborted: wrapper produced no exit.json past its deadline",
                  file=sys.stderr)
            return 3
        if now >= call_deadline:
            print("running")
            return 10
        time.sleep(min(WAIT_POLL_INTERVAL, max(0.0, call_deadline - now)))


def _read_exit_json(exit_path: Path) -> dict | None:
    """The wrapper's exit.json once it is readable, else None ("not yet").

    The wrapper writes it atomically, so a torn read should not happen; a
    decode failure is still treated as "keep polling" rather than an
    exception, because crashing the orchestrator's completion check is a
    far worse failure mode than one extra poll -- and if it never becomes
    readable, the caller's abort deadline still terminates the wait.
    """
    try:
        return json.loads(exit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _finish_wait(handle: dict, args, exit_data: dict) -> int:
    wait_record = Path(handle["wait_path"])
    prior = _read_exit_json(wait_record)  # tolerant read; None when absent
    if isinstance(prior, dict) and prior.get("attempt") == handle.get("attempt"):
        # Already finished this exact attempt: replay the verdict without
        # re-appending to steps.log (which would inflate the wf.max count)
        # and without re-running the debug diagnosis (a real LLM call).
        # `wait` is expected to be called repeatedly -- the orchestrator
        # loops on "running" -- so a completed handle WILL be re-polled.
        print(prior.get("message", "ok"))
        return RECORD_EXIT_CODES.get(prior.get("status", "ok"), 1)

    wf = parser.parse_file(handle["xml"])
    try:
        step = stepio.find_step(wf, handle["step_id"])
    except stepio.StepIOError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    result_path = Path(handle["result_path"])

    if exit_data.get("early_class"):
        res = claude_cli.CliResult(ok=False, error_class=exit_data["early_class"],
                                   error=exit_data.get("early_error"),
                                   exit_code=exit_data.get("returncode", -1),
                                   stderr=exit_data.get("stderr", ""))
    else:
        raw_stdout = result_path.read_text(encoding="utf-8")
        res = claude_cli.classify_result(exit_data["returncode"], raw_stdout,
                                         exit_data.get("stderr", ""), schema=step.schema)

    outputs_dir = Path(handle["run_dir"]) / "outputs"
    status, message = stepio.apply_result(
        step, res, args.vars, log_path=args.log,
        result_path=result_path, outputs_dir=outputs_dir,
        # expect-file must be checked against the directory the wrapper ran
        # the step in (the XML's parent), not wherever `wait` was invoked.
        base_dir=Path(handle["xml"]).resolve().parent,
        # The A layer knows its cycle, so its requests are filed per cycle.
        decisions_dir=decision_mod.decisions_dir(handle["run_dir"]),
        decision_prefix=f"{step.id}_c{handle.get('cycle', 1):02d}")
    print(message)
    stepio.write_text_atomic(wait_record, json.dumps(
        {"attempt": handle.get("attempt"), "status": status, "message": message},
        ensure_ascii=False, indent=2))

    if status == "error" and step.on_error == "debug" and claude_cli.is_debuggable(res.error_class):
        system = Path(handle.get("system_path", "")).read_text(encoding="utf-8") \
            if handle.get("system_path") else ""
        prompt_text = Path(handle.get("prompt_path", "")).read_text(encoding="utf-8") \
            if handle.get("prompt_path") else ""
        diagnosis = adp.diagnose(step, f"{system}\n\n{prompt_text}", res,
                                 cwd=str(Path(handle["xml"]).resolve().parent))
        fix_path = Path(handle["run_dir"]) / "steps" / f"{step.id}_fix.md"
        if diagnosis.action == "RETRY" and diagnosis.fix_instruction:
            fix_path.write_text(diagnosis.fix_instruction, encoding="utf-8")
            print(f"debug: RETRY -- fix written to {fix_path}; "
                  f"re-dispatch with --fix \"$(cat {fix_path})\"", file=sys.stderr)
        else:
            print(f"debug: FAIL -- {diagnosis.reason}", file=sys.stderr)

    return RECORD_EXIT_CODES[status]


def cmd_plan(args) -> int:
    wf = parser.parse_file(args.workflow)
    steps = list(wf.iter_steps())
    print(f"workflow: {wf.name} (max={wf.max}"
          + (f", budget-usd={wf.budget_usd}" if wf.budget_usd else "") + ")")
    # Resolved, not raw: this output is the run-llm orchestrator's only view of
    # the workflow (run-llm.md), so a decision verdict arriving there is
    # unreadable without knowing which policy is in force
    # (xml-wf-decision-request.md §11).
    decider, decider_model = model.resolve_decider(wf)
    print(f"decider:  {decider}"
          + (f" (model={decider_model})" if decider == "llm" else ""))
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
    p_run.add_argument("--backend", choices=("auto", "cc", "pi"), default="auto",
                       help="dispatch CLI; auto detects from CLAUDE_CODE_SESSION_ID "
                            "(default: auto)")
    p_run.add_argument("--inherit-model",
                       help="model identifier for steps with no model= of their "
                            "own (no step attribute, no role-frontmatter default); "
                            "pass the invoking session's own model. Without it, "
                            "such steps are left to whatever model the backend "
                            "CLI picks for itself, which is not necessarily one "
                            "it calls a default")
    p_run.set_defaults(func=cmd_run)

    p_res = sub.add_parser("resume", help="resume a failed run (skips recorded successes)")
    p_res.add_argument("run_dir")
    p_res.add_argument("--base-dir", help="project dir for agents/rules (default: cwd)")
    p_res.add_argument("--permission-mode")
    p_res.add_argument("--answer", action="append", default=[],
                       metavar="STEP_ID=PATH",
                       help="settle a pending decision request: the file's "
                            "first line must be 'option: <N|none>'. Repeat for "
                            "several. Without it a run that stopped for a "
                            "decision just re-prints the request and stops "
                            "again, at no cost")
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
    p_ask.add_argument("--backend", choices=("auto", "cc", "pi"), default="auto",
                       help="dispatch CLI; auto detects from CLAUDE_CODE_SESSION_ID "
                            "(default: auto)")
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
    p_prompt.add_argument("--attempt", type=int, default=1,
                          help="attempt sequence number recorded in the "
                               "step's handle.json (audit only)")
    p_prompt.set_defaults(func=cmd_prompt)

    p_rec = sub.add_parser(
        "record", help="record a step's result file: update vars, append log, "
                       "print only ok/error/aborted")
    p_rec.add_argument("workflow")
    p_rec.add_argument("step_id")
    p_rec.add_argument("--result", required=True, metavar="RESULT_FILE")
    p_rec.add_argument("--vars", required=True, metavar="VARS_JSON")
    p_rec.add_argument("--log", metavar="LOG_FILE")
    p_rec.add_argument("--reply", metavar="LINE",
                       help="the single reply line the orchestrator received "
                            "back from the subagent (optional; enables the "
                            "claimed-ok-but-no-result / reply-file-mismatch "
                            "checks when a handle.json exists for this step)")
    p_rec.add_argument("--answer", metavar="ANSWER_FILE",
                       help="settle this step's open decision request with "
                            "this ruling file (first line 'option: <N|none>'). "
                            "Exits 0 when the step continues without "
                            "re-running, 5 when it must be re-run from move 1")
    p_rec.add_argument("--decider", choices=list(model.DECIDER_VALUES),
                       help="who wrote the --answer file. Defaults to the "
                            "workflow's own decider; pass 'human' when a "
                            "person answered a request that fell back to "
                            "them, so it does not spend the llm cap")
    p_rec.set_defaults(func=cmd_record)

    p_poll = sub.add_parser(
        "poll", help="check a dispatched step's handle without blocking: "
                     "done(0) / running(10) / deadline-exceeded(11)")
    p_poll.add_argument("handle", metavar="HANDLE_FILE",
                        help="the steps/<id>_handle.json written by "
                             "'wfrun prompt --result'")
    p_poll.set_defaults(func=cmd_poll)

    p_dispatch = sub.add_parser(
        "dispatch", help="A-layer (reliability-spec.md §5): launch a step's "
                         "claude -p call in a detached, self-timing-out "
                         "wrapper and return immediately")
    p_dispatch.add_argument("workflow")
    p_dispatch.add_argument("step_id")
    p_dispatch.add_argument("--vars", required=True, metavar="VARS_JSON")
    p_dispatch.add_argument("--run-dir", required=True, metavar="DIR")
    p_dispatch.add_argument("--permission-mode")
    p_dispatch.add_argument("--fix", help="debug-granted fix instruction to append")
    p_dispatch.add_argument("--new-cycle", action="store_true",
                            help="start a fresh attempt budget: this dispatch "
                                 "is a new <while>/<each> iteration, not a "
                                 "retry. Only needed when the previous "
                                 "iteration ended in a failure the workflow "
                                 "tolerated (on-error=ignore); a new cycle "
                                 "after a success is detected automatically")
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_wrapper = sub.add_parser(
        "_wrapper", help=argparse.SUPPRESS)  # internal: launched by dispatch only
    p_wrapper.add_argument("--system-file", required=True)
    p_wrapper.add_argument("--prompt-file", required=True)
    p_wrapper.add_argument("--exit-file", required=True)
    p_wrapper.add_argument("--result-file", required=True)
    p_wrapper.add_argument("--attempts-file", required=True)
    p_wrapper.add_argument("--seq", type=int, required=True)
    p_wrapper.add_argument("--cwd", required=True)
    p_wrapper.add_argument("--timeout", type=int, required=True)
    p_wrapper.add_argument("--model")
    p_wrapper.add_argument("--effort")
    p_wrapper.add_argument("--tools")
    p_wrapper.add_argument("--schema")
    p_wrapper.add_argument("--permission-mode")
    p_wrapper.set_defaults(func=cmd_run_wrapper)

    p_wait = sub.add_parser(
        "wait", help="A-layer: poll a dispatched step's handle up to --max "
                     "seconds; on completion, update vars/log like record")
    p_wait.add_argument("handle", metavar="HANDLE_FILE")
    p_wait.add_argument("--max", type=float, default=60,
                        help="seconds to watch for exit.json before "
                             "reporting 'running' (call again after; keep "
                             "this <= 550 so the call itself fits a single "
                             "CC Bash tool invocation)")
    p_wait.add_argument("--vars", required=True, metavar="VARS_JSON")
    p_wait.add_argument("--log", metavar="LOG_FILE")
    p_wait.set_defaults(func=cmd_wait)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except parser.ParseError as e:
        print(f"[ERROR] parse: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
