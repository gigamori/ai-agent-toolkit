"""wfrun CLI: validate / run / resume / plan / viz, plus the helpers an LLM
orchestrator calls: interp / eval / ask (which keep interpolation, condition
evaluation and ask judgment deterministic even when an LLM drives the control
flow), run-llm's prompt / record / decide / poll / stop / pids / judge (a
step's prompt, and the records of its attempts and decisions), and dispatch /
wait (the claude CLI path, not part of run-llm)."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import adjudicate as adjudicate_mod
from . import adp, ccdeny, claude_cli
from . import decision as decision_mod
from . import interp as interp_mod
from . import lint as lint_mod
from . import llmgate, model, modelmap, parser, runlock, stepio, viz
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
                             as_child=args.as_child, defined_vars=defined,
                             backend=_resolve_backend(args.backend, quiet=True))
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


def _load_validated(path: str, no_role_check: bool,
                    backend: str = "cc") -> model.Workflow:
    wf = parser.parse_file(path)
    findings = lint_mod.lint(wf, base_dir=Path(path).parent,
                             check_roles=not no_role_check, backend=backend)
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
    adjudicate=/model_runner=/backend=) for a resolved backend ("cc" or "pi"
    -- never "auto"). Shared by cmd_run and cmd_resume so a resumed run always
    reconstructs the same execution facility it started with (design §1, §3.3).

    `adjudicate=` splits by backend for the same reason `run_claude=` does: the
    cc adjudicator forces structured output, which pi has no equivalent for, so
    the pi one takes the ruling as §13.3 text instead. Both land in the same
    parser (xml-wf-decision-request.md §17.1).

    `backend=` is passed as the plain name on top of the injected callables:
    the checks a replan continuation needs (design §10.2) are about which
    facility the run is on, not about which function object executes a step,
    and `model_runner=` cannot stand in for it -- that one's vocabulary is
    "cc"/"llm". Resume goes through here too, so it inherits the backend
    recorded in backend.json like everything else in this dict."""
    if backend == "cc":
        return dict(run_claude=claude_cli.run_claude, ask_llm=claude_cli.ask_llm,
                    diagnose=adp.diagnose,
                    adjudicate=adjudicate_mod.adjudicate, model_runner="cc",
                    backend="cc")
    from . import pi_cli  # deferred: needs the pi CLI only when backend=="pi"
    return dict(run_claude=pi_cli.run_pi, ask_llm=pi_cli.ask_llm_pi,
                diagnose=pi_cli.diagnose_stub_pi,
                adjudicate=adjudicate_mod.adjudicate_pi, model_runner="llm",
                backend="pi")


# A run dir belongs to one live `run`/`resume` process at a time: the
# process holds <run-dir>/run.lock from before its first write until after
# its last, and the OS drops the lock if the process dies. A second process
# is refused before it writes or launches anything, so it can never replay
# up to a step the first one is still executing and run that step again.
RUN_DIR_LOCK_NAME = "run.lock"
EXIT_RUN_UNTOUCHED = 2  # rejected before anything ran; the run dir is as it was


def _lock_run_dir(run_dir: Path):
    """Take the run-dir lock without waiting. Returns the handle, or one of
    the `_acquire_with_retry` outcomes; `_LOCK_CONTENDED` means the caller
    must refuse. Where locking is unsupported or fails for another reason
    the run proceeds unguarded, as `dispatch` does, but says so."""
    lock_path = run_dir / RUN_DIR_LOCK_NAME
    outcome = _acquire_with_retry(lock_path, 0)
    if outcome is _LOCK_CONTENDED:
        print(f"error: run dir {run_dir} is in use by another wfrun process "
              f"(it holds {lock_path}) -- wait for that process to finish, or "
              f"stop it, then retry", file=sys.stderr)
    elif outcome is _LOCK_UNSUPPORTED:
        print(f"note: {lock_path}: file locking is not supported here, so a "
              f"concurrent run or resume of this run dir is not refused",
              file=sys.stderr)
    elif outcome is _LOCK_UNAVAILABLE:
        print(f"warning: could not take {lock_path}, so a concurrent run or "
              f"resume of this run dir is not refused", file=sys.stderr)
    return outcome


def cmd_run(args) -> int:
    # Backend first: validation's model-name checks are backend-specific, so a
    # pi run must be linted as pi (an unavailable model is an error there).
    backend = _resolve_backend(args.backend)
    wf = _load_validated(args.workflow, args.no_role_check, backend)

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
    lock = _lock_run_dir(run_dir)
    if lock is _LOCK_CONTENDED:
        return EXIT_RUN_UNTOUCHED
    try:
        return _run_in_run_dir(args, wf, backend, params, base_dir, run_dir)
    finally:
        _release_lock(lock)


def _run_in_run_dir(args, wf, backend, params, base_dir, run_dir) -> int:
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
    rulings: list[tuple] = []
    seen_steps: set[str] = set()

    for spec in specs:
        if "=" not in spec:
            raise AnswerError(f"--answer expects STEP_ID=PATH, got '{spec}'")
        step_id, _, raw_path = spec.partition("=")
        step_id = step_id.strip()
        if step_id in seen_steps:
            raise AnswerError(
                f"--answer {step_id}=...: given more than once. A request takes "
                "exactly one answer; pass a single --answer for this step.")
        seen_steps.add(step_id)
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
        rulings.append((step_id, request, answer_file, answer, verdict,
                        b_reason, missing))

    # Nothing is recorded until every --answer has passed, so a rejected batch
    # leaves the run dir exactly as it was.
    state = RunState(run_dir)
    appended: list[dict] = []
    for step_id, request, answer_file, answer, verdict, b_reason, missing in rulings:
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
    lock = _lock_run_dir(run_dir)
    if lock is _LOCK_CONTENDED:
        return EXIT_RUN_UNTOUCHED
    try:
        return _resume_in_run_dir(args, run_dir, wf_path, params_path)
    finally:
        _release_lock(lock)


def _resume_in_run_dir(args, run_dir, wf_path, params_path) -> int:
    wf = parser.parse_file(wf_path)
    params = json.loads(params_path.read_text(encoding="utf-8")) if params_path.is_file() else {}
    events = load_events(run_dir)
    try:
        events = _ingest_answers(run_dir, args.answer, events)
    except AnswerError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_RUN_UNTOUCHED
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
    tmp/runs/phase5-item1-cc-inventory-design.md §2.3 -- this
    is deliberately NOT left to the calling orchestrator's own judgment: a
    forgotten/wrong flag would otherwise fail silently (ask would just run
    against whichever CLI happens to be on PATH)."""
    return "cc" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "pi"


def _resolve_backend(explicit: str, quiet: bool = False) -> str:
    """`--backend {auto,cc,pi}` resolution shared by `run`, `ask` and `validate`
    (tmp/runs/phase6-run-pi-design.md §3.1: "same determinant,
    same default" as `ask --backend`, _detect_ask_backend promoted to
    shared use). auto detects from CLAUDE_CODE_SESSION_ID; an explicit
    cc/pi that disagrees with the detected environment is honored but
    warned about -- a forgotten/wrong flag would otherwise silently
    dispatch against the wrong CLI. quiet suppresses that warning for
    `validate`, which dispatches nothing and would otherwise print it twice on
    the `run` path (validate-then-execute shares this resolution)."""
    detected = _detect_ask_backend()
    backend = detected if explicit == "auto" else explicit
    if explicit != "auto" and explicit != detected and not quiet:
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

    try:  # "cc" table for the claude CLI, "llm" table for everything else.
        # allow_legacy mirrors the ask-model attribute path (_map_model): the
        # flag's default is a tier, so this is a tier-vocabulary interface.
        ask_model = modelmap.resolve(args.model, "cc" if backend == "cc" else "llm",
                                     allow_legacy=True)
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
    # tmp/runs/phase5-item1-cc-inventory-design.md §3.2).
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
    elif args.new_cycle or args.redispatch:
        print("error: --new-cycle and --redispatch need --result: they choose "
              "the next attempt, and only --result has attempts", file=sys.stderr)
        return 2
    wf = parser.parse_file(args.workflow)
    variables = _load_vars(args.vars)
    base_dir = Path(args.workflow).resolve().parent
    agents_cache = discover_agents(base_dir)
    try:
        step = stepio.find_step(wf, args.step_id)
    except stepio.StepIOError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    is_replan = isinstance(step, model.Replan)

    # Admission comes before any write: a refused call leaves every file of
    # the run as it was, earlier attempts' results included.
    attempt = None
    if args.result:
        now = time.time()
        attempt = (llmgate.admit_replan(step, args.result, args.fix, now)
                   if is_replan else _admit_step_attempt(wf, step, args, now))
        if isinstance(attempt, str):
            print(f"error: {attempt}", file=sys.stderr)
            return 1

    try:
        if is_replan:
            prompt = stepio.build_replan_prompt(
                step, variables, agents_cache, fix=args.fix,
                result_path=attempt["output"] if attempt else None)
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
                result_path=attempt["result_path"] if attempt else None,
                decision=settled or None,
                sentinel=attempt["sentinel"] if attempt else None)
    except (stepio.StepIOError, decision_mod.DecisionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    # model/tools are the RESOLVED dispatch values (step attrs > role frontmatter).
    dispatch_model, dispatch_tools = stepio.dispatch_for(step, agents_cache)
    try:  # the dispatch line carries the runner-resolved name ("llm" table)
        resolved = modelmap.resolve(dispatch_model, "llm", allow_legacy=True)
    except modelmap.ModelMapError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if attempt is not None and is_replan:
        llmgate.record_replan(args.result, attempt)
    elif attempt is not None:
        # The attempt's own copy of its prompt, then the step's pointer to
        # the attempt: `record`/`poll` read the attempt through the pointer.
        # Sentinel/handle machinery is step-only: a <replan>'s own XML
        # well-formedness already proves completeness (§0, F1).
        Path(attempt["prompt_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(attempt["prompt_path"]).write_text(prompt, encoding="utf-8")
        stepio.write_text_atomic(stepio.handle_path(step.id, args.result),
                                 json.dumps(attempt, ensure_ascii=False, indent=2))
    out.write_text(prompt, encoding="utf-8")

    # Print only control-plane facts — never the prompt content.
    kind = "replan" if is_replan else "step"
    facts = [kind] + [x for x in (model.role_label(step),) if x]
    if getattr(step, "mode", None):
        facts.append(f"mode={step.mode}")
    if resolved:
        facts.append(f"model={resolved}"
                     + ("" if resolved == dispatch_model
                        else f" (mapped from {dispatch_model})"))
    if step.effort:
        facts.append(f"effort={step.effort}")
    if dispatch_tools and not is_replan:
        facts.append(f"tools={dispatch_tools}")
    if attempt is not None:
        facts.append("attempt=" + llmgate.attempt_label(attempt["cycle"],
                                                        attempt["attempt"]))
        facts.append(f"result={attempt['output' if is_replan else 'result_path']}")
        facts.append(f"timeout={attempt['timeout']}")
        if not is_replan:
            facts.append(f"session-dir={llmgate.files_of(attempt)['session']}")
            facts.append(f"description={attempt['delegation']}")
    print(f"{out} ({', '.join(facts)})")
    return 0


def _admit_step_attempt(wf, step: model.Step, args, now: float) -> dict | str:
    """The handle of the next run-llm attempt at `step`, or why none may
    start. Writes nothing, except a ledger flag when the gate finds that a
    stopped attempt completed after its stop was confirmed.

    Liveness first, across cycles: an attempt with no recorded verdict
    blocks every new attempt, `--new-cycle` included, and so does an aborted
    one: it goes on only through its stop record and its judge
    (`_after_abort`). Then the step's open decision, the debug diagnosis the
    ladder is waiting for, and last the ladder and budgets shared with
    `dispatch` (`_admission_refusal`)."""
    result_base = Path(args.result)
    steps_dir = result_base.parent
    run_dir = steps_dir.parent
    pointer_file = stepio.handle_path(step.id, result_base)
    pointer = None
    if pointer_file.exists():
        pointer = llmgate.read_json(pointer_file)
        if not stepio.is_attempt_handle(pointer):
            return (f"step '{step.id}': {pointer_file} is not a per-attempt "
                    "handle -- it was written by an older wfrun or is "
                    "unreadable; start a new run directory")
    if args.redispatch and pointer is None:
        return (f"step '{step.id}': --redispatch replaces an aborted attempt, "
                "and the step has no attempt yet")

    latest, timeout, supersedes, record = None, step.timeout, None, None
    if pointer is not None:
        latest = pointer["cycle"]
        label = llmgate.attempt_label(pointer["cycle"], pointer["attempt"])
        entry = llmgate.ledger_entry(steps_dir, step.id, pointer["cycle"],
                                     pointer["attempt"])
        if entry is None:
            return (f"step '{step.id}': attempt {label} has no recorded "
                    "verdict -- poll it and record it once it is done; past "
                    "its deadline, follow the abort procedure")
        if entry.get("class") == "aborted":
            granted = _after_abort(step, pointer, entry, label, args)
            if isinstance(granted, str):
                return granted
            timeout, record = granted
            if not args.new_cycle:
                supersedes = {"cycle": pointer["cycle"],
                              "attempt": pointer["attempt"]}
        elif args.redispatch:
            return (f"step '{step.id}': --redispatch applies only after an "
                    f"aborted attempt, and attempt {label} ended "
                    f"{entry.get('class')}")

    pending = decision_mod.pending_request_id(
        decision_mod.decisions_dir(run_dir), step.id)
    if pending is not None:
        return (f"step '{step.id}': decision {pending} has no ruling yet -- "
                "settle it with record --answer, which says whether to re-run")

    if latest is None:
        cycle, attempts = 1, []
    else:
        cycle, attempts = _next_cycle(latest, stepio.load_attempts(
            stepio.attempt_ledger_path(steps_dir, step.id, latest)),
            args.new_cycle)
    debug = _debug_pending(step, cycle, attempts, args.fix)
    if debug:
        return debug
    refusal = _admission_refusal(wf, step, run_dir, cycle, attempts, args.fix,
                                 decision_prefix=step.id)
    if refusal:
        return refusal
    return llmgate.new_attempt(step.id, result_base, cycle, len(attempts) + 1,
                               timeout, now, supersedes, record)


def _after_abort(step: model.Step, pointer: dict, entry: dict, label: str,
                 args) -> tuple[int, str | None] | str:
    """(timeout, judge record) for the attempt that follows an aborted one,
    or why there is none. Every way on needs a confirmed stop and no sign
    that the attempt kept writing; the only way on is a restart of the same
    cycle, which also needs a transcript with no refused call and the
    judge's record."""
    stop = llmgate.load_stop(pointer)
    if stop is None:
        return (f"step '{step.id}': attempt {label} was aborted and has no "
                "stop record -- run `wfrun stop` on its handle first")
    if stop.get("verdict") != llmgate.STOP_CONFIRMED:
        reasons = "; ".join(stop.get("reasons", [])) or stop.get("verdict")
        return (f"step '{step.id}': attempt {label} was aborted and its stop "
                f"is unconfirmed ({reasons}); it is never run again -- stop "
                "and report")
    if (entry.get(llmgate.FLAG_LATE_COMPLETION)
            or entry.get(llmgate.FLAG_STOP_CONTRADICTION)
            or llmgate.is_complete(pointer)):
        llmgate.flag_attempt(pointer, llmgate.FLAG_STOP_CONTRADICTION)
        return (f"step '{step.id}': attempt {label} completed after its stop "
                "was confirmed, so its stop evidence cannot be trusted -- "
                "stop and report")
    if args.new_cycle:
        return (f"step '{step.id}': attempt {label} was aborted; --new-cycle "
                "does not move past an aborted attempt -- run `wfrun judge`, "
                "delegate the judge, and pass its reply with --redispatch; or "
                "stop and report")
    if not args.redispatch:
        return (f"step '{step.id}': attempt {label} was aborted -- run "
                "`wfrun judge`, delegate the judge, and pass its reply with "
                "--redispatch; or stop and report")
    # A refused call is never re-run, and a transcript that cannot be found
    # cannot be judged (Claude Code only: Pi has no permission layer).
    check = ccdeny.check_delegation(pointer)
    if check.verdict == ccdeny.DENIED:
        return (f"step '{step.id}': a tool call of attempt {label} was "
                "refused, and a refused call is never re-run -- stop and report")
    if check.verdict == ccdeny.NO_TRANSCRIPT:
        return (f"step '{step.id}': the transcript of attempt {label} cannot "
                f"be found ({check.reason}) -- stop and report")
    return llmgate.check_judge(pointer, args.redispatch)


def _debug_pending(step: model.Step, cycle: int, attempts: list,
                   fix: str | None) -> str | None:
    """Why a re-run must wait for the debug diagnosis: the ladder has handed
    the cycle's last failed attempt to debug and no verdict is recorded yet.
    run-llm delegates that diagnosis, where `wait` runs it
    in-process."""
    view = _ladder_view(attempts)
    if fix or not view:
        return None
    last = view[-1]
    if last.get("debug") is not None:
        return None
    if not _debug_is_next(step, last.get("class"), last.get("seq"), attempts):
        return None
    return (f"step '{step.id}' cycle {cycle}: attempt {last.get('seq')} failed "
            "with no retry left and on-error=\"debug\" -- delegate the debug "
            "diagnosis, pass its reply to record --debug-reply, and do what "
            "that prints")


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
    elif args.debug_reply is not None:
        code, message = _record_debug(step, args)
        print(message)
        return code
    else:
        refusal = (_recorded_already(step, args.result)
                   or _completed_after_stop(step, args.result, args.log))
        if refusal:
            print(refusal)
            return RECORD_REFUSED
        stopped = _stopped_unfinished(step, args.result)
        if stopped is not None:
            print(_record_stopped(step, stopped, args.log))
            return RECORD_EXIT_CODES["aborted"]
        status, message = stepio.record_result(step, args.result, args.vars,
                                               args.log, reply=args.reply)
    print(message)
    return RECORD_EXIT_CODES[status]


# The record of a decision request's one llm-decider delegation, next to the
# request in `decisions/`.
DECIDER_RECORD_SUFFIX = "_decider.json"


def cmd_decide(args) -> int:
    """Issue the one llm-decider delegation of a step's open decision request:
    record it, then print the paths and description to delegate with. A
    request is delegated once. Whether that delegation replied, ruled, or
    vanished, a second one is refused: the request goes to a person."""
    wf = parser.parse_file(args.workflow)
    try:
        step = stepio.find_step(wf, args.step_id)
    except stepio.StepIOError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    dec_dir = stepio.decisions_dir_for_result(args.result)
    rid = decision_mod.pending_request_id(dec_dir, step.id)
    if rid is None:
        print(f"error: step '{step.id}': no open decision request in {dec_dir}",
              file=sys.stderr)
        return 2
    decider, decider_model = model.resolve_decider(wf, step)
    if decider != model.DECIDER_LLM:
        print(f"error: step '{step.id}': the decider is {decider}: present "
              f"decision {rid} to a person; nothing is delegated", file=sys.stderr)
        return 2
    if decision_mod.llm_adjudications(dec_dir, step.id) >= model.DECISION_LLM_CAP:
        print(f"error: step '{step.id}': an llm decider has already settled "
              f"{model.DECISION_LLM_CAP} request(s) here, so decision {rid} is "
              "for a person (record --answer ... --decider human)",
              file=sys.stderr)
        return 1
    record_path = dec_dir / f"{rid}{DECIDER_RECORD_SUFFIX}"
    if record_path.exists():
        print(f"error: decision {rid} was already delegated to the llm decider "
              f"({record_path}); a request is not delegated again -- present it "
              "to a person and record their ruling with --decider human",
              file=sys.stderr)
        return 1
    try:
        resolved = modelmap.resolve(decider_model, "llm")
    except modelmap.ModelMapError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    record = {
        "request_id": rid, "step_id": step.id, "issued_at": time.time(),
        "request_path": str(dec_dir / f"{rid}{decision_mod.REQUEST_SUFFIX}"),
        "answer_path": str(dec_dir / f"{rid}{decision_mod.ANSWER_SUFFIX}"),
        "model": resolved,
        "description": (f"wfrun-decider {step.id} {rid} "
                        f"{secrets.token_hex(stepio.DELEGATION_TOKEN_BYTES)}"),
    }
    llmgate.write_json(record_path, record)
    print(f"{record['request_path']} (decider, model={resolved}, "
          f"answer={record['answer_path']}, description={record['description']})")
    return 0


# `record` verdicts that change nothing: a second record of one attempt, a
# late result that is not adopted, a debug reply that cannot be recorded.
# Same code as an unusable --answer.
RECORD_REFUSED = 2

_DEBUG_REPLY_RE = re.compile(r"\s*(RETRY|FAIL)\b", re.IGNORECASE)


def _recorded_already(step: model.Step, result_base: str) -> str | None:
    """Why `record` must not record the attempt `result_base`'s handle names:
    it already has a verdict. A result that completed after an aborted
    verdict is flagged on the way out -- it is the one sign that something
    kept writing after the attempt was judged over -- and never adopted."""
    handle = stepio.load_handle(step.id, result_base)
    if not stepio.is_attempt_handle(handle):
        return None
    steps_dir = Path(handle["result_path"]).parent
    entry = llmgate.ledger_entry(steps_dir, step.id, handle["cycle"],
                                 handle["attempt"])
    if entry is None:
        return None
    label = llmgate.attempt_label(handle["cycle"], handle["attempt"])
    if entry.get("class") == "aborted" and llmgate.is_complete(handle):
        stop = llmgate.load_stop(handle)
        confirmed = stop is not None and stop.get("verdict") == llmgate.STOP_CONFIRMED
        llmgate.flag_attempt(handle, llmgate.FLAG_STOP_CONTRADICTION if confirmed
                             else llmgate.FLAG_LATE_COMPLETION)
        return (f"error: attempt {label} of step '{step.id}' was recorded "
                "aborted and its result completed afterwards; that completion "
                "is not adopted -- stop and report")
    return (f"error: attempt {label} of step '{step.id}' is already recorded "
            f"({entry.get('class')}); nothing recorded")


def _completed_after_stop(step: model.Step, result_base: str,
                          log_path: str | None) -> str | None:
    """Why `record` must not adopt the attempt's result: its stop window
    closed without a completion (the stop record says confirmed or
    unconfirmed), and the result completed afterwards. The attempt is
    recorded aborted instead, flagged the way `_recorded_already` flags a
    completion after an aborted verdict: after a confirmed stop it
    contradicts the stop evidence."""
    handle = stepio.load_handle(step.id, result_base)
    if not stepio.is_attempt_handle(handle):
        return None
    stop = llmgate.load_stop(handle)
    if stop is None or stop.get("verdict") == llmgate.STOP_DONE:
        return None
    if not llmgate.is_complete(handle):
        return None
    flag = (llmgate.FLAG_STOP_CONTRADICTION
            if stop.get("verdict") == llmgate.STOP_CONFIRMED
            else llmgate.FLAG_LATE_COMPLETION)
    stepio.record_aborted_attempt(
        step, stepio.attempt_ledger_path(Path(handle["result_path"]).parent,
                                         step.id, handle["cycle"]),
        handle["attempt"], log_path, handle["result_path"],
        {"token": handle["token"], flag: True})
    label = llmgate.attempt_label(handle["cycle"], handle["attempt"])
    return (f"error: attempt {label} of step '{step.id}' completed after its "
            f"stop was observed ({stop.get('verdict')}); that completion is "
            "not adopted and the attempt is recorded aborted -- stop and report")


STOPPED_RECORD_MESSAGE = ("aborted: stopped before its result completed -- a "
                          "reply is not read after a stop")


def _stopped_unfinished(step: model.Step, result_base: str) -> dict | None:
    """The handle of the attempt `result_base` names when its stop was
    observed and its result is still incomplete: the stop settles it, so
    `record` reads neither a reply nor the refusal check for its class."""
    handle = stepio.load_handle(step.id, result_base)
    if not stepio.is_attempt_handle(handle):
        return None
    stop = llmgate.load_stop(handle)
    if stop is None or stop.get("verdict") == llmgate.STOP_DONE:
        return None
    if llmgate.is_complete(handle):
        return None
    return handle


def _record_stopped(step: model.Step, handle: dict,
                    log_path: str | None) -> str:
    """Record the attempt `handle` names aborted and say so. A stop that cut
    a running call can leave it recorded as refused: the refusal check still
    runs, but its verdict is only logged and printed -- a stop is not a
    denial, and the stop's own record decides whether the attempt may run
    again."""
    check = ccdeny.check_delegation(handle)
    fields = {"token": handle["token"]}
    deny_check = None
    if check.verdict != ccdeny.NOT_APPLICABLE:
        deny_check = check.label
        fields["deny_check"] = deny_check
    stepio.record_aborted_attempt(
        step, stepio.attempt_ledger_path(Path(handle["result_path"]).parent,
                                         step.id, handle["cycle"]),
        handle["attempt"], log_path, handle["result_path"], fields,
        deny_check=deny_check)
    lines = [STOPPED_RECORD_MESSAGE]
    if check.verdict == ccdeny.DENIED:
        lines.append(f"deny-check: {check.label} (details: {check.transcript})")
    elif check.verdict == ccdeny.NO_TRANSCRIPT:
        lines.append(f"deny-check: {check.label}: {check.reason}")
    return "\n".join(lines)


def _record_debug(step: model.Step, args) -> tuple[int, str]:
    """Attach the delegated debug diagnosis's verdict to the attempt it
    diagnosed, the way `wait` attaches its in-process one, so
    the shared ladder admits `--fix` exactly once and only after RETRY. One
    verdict per failed attempt: a second reply records nothing."""
    handle = stepio.load_handle(step.id, args.result)
    if not stepio.is_attempt_handle(handle):
        return RECORD_REFUSED, (f"error: step '{step.id}' has no attempt to "
                                "attach a debug verdict to")
    steps_dir = Path(handle["result_path"]).parent
    ledger = stepio.attempt_ledger_path(steps_dir, step.id, handle["cycle"])
    attempts = stepio.load_attempts(ledger)
    seq = handle["attempt"]
    label = llmgate.attempt_label(handle["cycle"], seq)
    entry = next((a for a in attempts
                  if isinstance(a, dict) and a.get("seq") == seq), None)
    if entry is None:
        return RECORD_REFUSED, (f"error: attempt {label} of step '{step.id}' "
                                "has no recorded verdict yet -- record it first")
    if entry.get("debug") is not None:
        return RECORD_REFUSED, (f"error: attempt {label} already carries the "
                                f"debug verdict {entry['debug']}; a diagnosis "
                                "is delegated once per failed attempt -- "
                                "nothing recorded")
    if not _debug_is_next(step, entry.get("class"), seq, attempts):
        return RECORD_REFUSED, (f"error: the ladder does not hand attempt "
                                f"{label} ({entry.get('class')}) to debug -- "
                                "nothing recorded")
    match = _DEBUG_REPLY_RE.match(args.debug_reply or "")
    if match is None:
        return RECORD_REFUSED, ("error: the debug reply is neither RETRY nor "
                                "FAIL -- nothing recorded; stop and report")
    if match.group(1).upper() == DEBUG_RETRY:
        fix = _fix_path(steps_dir.parent, step.id)
        try:
            written = fix.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            written = ""
        if not written:
            return RECORD_REFUSED, (f"error: debug answered RETRY but wrote no "
                                    f"fix to {fix} -- nothing recorded; stop "
                                    "and report")
        stepio.update_attempt(ledger, seq, {"debug": DEBUG_RETRY})
        return 0, f'debug: RETRY -- redo from move 1 with --fix "$(cat {fix})"'
    stepio.update_attempt(ledger, seq, {"debug": DEBUG_FAIL})
    earlier = [a for a in _ladder_view(attempts) if a.get("seq", seq) < seq]
    return 1, ("debug: FAIL -- "
               + _on_error_action(step, {**entry, "debug": DEBUG_FAIL}, earlier))


# How often `poll --wait` looks at the attempt's result file. It is a
# background wake-up for a deadline, not a busy loop.
POLL_WAIT_INTERVAL = 5.0


def _poll_verdict(handle: dict) -> tuple[int, str]:
    if llmgate.is_complete(handle):
        return 0, "done"
    # No kill authority here (run-llm limitation, reliability-spec.md §4.3):
    # this only ever declares a verdict, never terminates the subagent.
    if time.time() - handle["dispatched_at"] > handle["timeout"]:
        return 11, "deadline-exceeded"
    return 10, "running"


def cmd_poll(args) -> int:
    """done / running / deadline-exceeded for the attempt a handle names:
    only that attempt's own file ending in its own sentinel is done.
    `--wait` blocks until the verdict is not `running`; it reads the result
    file and the clock and writes nothing, so a background wake-up at the
    deadline never classifies or records the attempt."""
    handle_file = Path(args.handle)
    try:
        handle = json.loads(handle_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot load handle file {handle_file}: {e}", file=sys.stderr)
        return 2
    while True:
        code, verdict = _poll_verdict(handle)
        if code != 10 or not args.wait:
            print(verdict)
            return code
        time.sleep(POLL_WAIT_INTERVAL)


# `stop`'s verdicts: `done` shares poll's 0 (record it now: the completion is
# adopted); the other two continue poll's numbering, so no exit code means
# two things.
STOP_EXIT_CODES = {llmgate.STOP_DONE: 0, llmgate.STOP_CONFIRMED: 12,
                   llmgate.STOP_UNCONFIRMED: 13}


def _attempt_handle(path: str) -> dict | None:
    handle = llmgate.read_json(path)
    return handle if stepio.is_attempt_handle(handle) else None


def cmd_stop(args) -> int:
    """Observe a run-llm attempt after its stop request, for at most a
    minute, and write its stop record: `done` (the result completed inside
    the window -- record it), `confirmed`, or `unconfirmed` (record the
    attempt, then stop and report). An attempt is observed once; asking
    again prints the first record's verdict."""
    handle = _attempt_handle(args.handle)
    if handle is None:
        print(f"error: {args.handle} is not a run-llm attempt handle",
              file=sys.stderr)
        return 2
    if args.facet == llmgate.FACET_CC and not args.task_stop:
        print("error: --facet cc needs --task-stop: what TaskStop reported",
              file=sys.stderr)
        return 2
    if args.facet != llmgate.FACET_CC and args.task_stop:
        print(f"error: --task-stop belongs to --facet {llmgate.FACET_CC} only",
              file=sys.stderr)
        return 2
    if not 0 < args.observe <= llmgate.STOP_OBSERVE_MAX:
        print(f"error: --observe takes 1 to {llmgate.STOP_OBSERVE_MAX} seconds",
              file=sys.stderr)
        return 2
    label = llmgate.attempt_label(handle["cycle"], handle["attempt"])
    entry = llmgate.ledger_entry(Path(handle["result_path"]).parent,
                                 handle["step_id"], handle["cycle"],
                                 handle["attempt"])
    if entry is not None and entry.get("class") != "aborted":
        print(f"error: attempt {label} is already recorded {entry.get('class')}; "
              "there is nothing to stop", file=sys.stderr)
        return 2
    record = llmgate.observe_stop(handle, args.facet, args.task_stop, args.observe)
    where = llmgate.files_of(handle)["stop"]
    verdict = record["verdict"]
    if verdict == llmgate.STOP_DONE:
        print("done")
    elif verdict == llmgate.STOP_CONFIRMED:
        print(f"confirmed ({record['facet']}, cause={record['cause']}; "
              f"record: {where})")
    else:
        print(f"unconfirmed: {'; '.join(record['reasons'])} -- record the "
              f"attempt, then stop and report (record: {where})")
    return STOP_EXIT_CODES[verdict]


def cmd_pids(args) -> int:
    """Record the processes a Pi CLI attempt was launched as, once, at
    launch: the set its stop is later judged against."""
    handle = _attempt_handle(args.handle)
    if handle is None:
        print(f"error: {args.handle} is not a run-llm attempt handle",
              file=sys.stderr)
        return 2
    if any(pid <= 0 for pid in args.pids):
        print("error: a PID is a positive integer", file=sys.stderr)
        return 2
    recorded = llmgate.record_pids(handle, args.pids)
    if isinstance(recorded, str):
        print(f"error: {recorded}", file=sys.stderr)
        return 1
    label = llmgate.attempt_label(handle["cycle"], handle["attempt"])
    print(f"recorded {len(recorded['processes'])} process(es) of attempt {label}: "
          + ", ".join(f"{p['pid']} ({p['image']})" for p in recorded["processes"]))
    return 0


def cmd_judge(args) -> int:
    """Issue the one judge of an aborted run-llm attempt whose stop is
    confirmed: write its evidence manifest, prompt and handle, and print the
    paths and dispatch facts to delegate it with. Prints no content."""
    wf = parser.parse_file(args.workflow)
    try:
        step = stepio.find_step(wf, args.step_id)
    except stepio.StepIOError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    handle = stepio.load_handle(step.id, args.result)
    if isinstance(step, model.Replan) or not stepio.is_attempt_handle(handle):
        print(f"error: step '{step.id}' has no run-llm attempt to judge",
              file=sys.stderr)
        return 2
    try:
        resolved = modelmap.resolve(llmgate.JUDGE_MODEL, "llm")
    except modelmap.ModelMapError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    refusal = _judge_refusal(wf, step, handle)
    if refusal is None:
        issued = llmgate.issue_judge(handle, llmgate.load_stop(handle))
        refusal = issued if isinstance(issued, str) else None
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 1
    judge = issued["judge"]
    print(f"{judge['prompt_path']} (judge, model={resolved}, "
          f"tools={','.join(llmgate.JUDGE_TOOLS)}, record={judge['record_path']}, "
          f"handle={judge['handle_path']}, timeout={judge['timeout']}, "
          f"description={judge['description']})")
    return 0


def _judge_refusal(wf: model.Workflow, step: model.Step, handle: dict
                   ) -> str | None:
    """Why no judge may be spent on `handle`'s attempt: it is not an aborted
    attempt with a confirmed stop and a still-incomplete result, a tool call
    of it was refused, its transcript cannot be found, or the restart a judge
    could allow would be refused anyway (an open decision, the budgets)."""
    steps_dir = Path(handle["result_path"]).parent
    label = llmgate.attempt_label(handle["cycle"], handle["attempt"])
    entry = llmgate.ledger_entry(steps_dir, step.id, handle["cycle"],
                                 handle["attempt"])
    if entry is None:
        return (f"attempt {label} of step '{step.id}' has no recorded verdict "
                "-- record it first; a judge weighs only an aborted attempt")
    if entry.get("class") != "aborted":
        return (f"attempt {label} of step '{step.id}' ended {entry.get('class')}; "
                "a judge weighs only an aborted attempt")
    if (entry.get(llmgate.FLAG_LATE_COMPLETION)
            or entry.get(llmgate.FLAG_STOP_CONTRADICTION)):
        return (f"attempt {label} completed after its stop or its verdict, so "
                "its stop evidence cannot be trusted -- stop and report")
    stop = llmgate.load_stop(handle)
    if stop is None or stop.get("verdict") != llmgate.STOP_CONFIRMED:
        return (f"the stop of attempt {label} is not confirmed; it is never "
                "run again -- stop and report")
    if llmgate.is_complete(handle):
        llmgate.flag_attempt(handle, llmgate.FLAG_STOP_CONTRADICTION)
        return (f"attempt {label} completed after its stop was confirmed, so "
                "its stop evidence cannot be trusted -- stop and report")
    check = ccdeny.check_delegation(handle)
    if check.verdict == ccdeny.DENIED:
        return (f"a tool call of attempt {label} was refused, and a refused "
                "call is never re-run -- stop and report")
    if check.verdict == ccdeny.NO_TRANSCRIPT:
        return (f"the transcript of attempt {label} cannot be found "
                f"({check.reason}), so no judge could read it -- stop and report")
    pending = decision_mod.pending_request_id(
        decision_mod.decisions_dir(steps_dir.parent), step.id)
    if pending is not None:
        return (f"step '{step.id}': decision {pending} has no ruling yet -- "
                "settle it with record --answer, which says whether to re-run")
    attempts = stepio.load_attempts(
        stepio.attempt_ledger_path(steps_dir, step.id, handle["cycle"]))
    return _admission_refusal(wf, step, steps_dir.parent, handle["cycle"],
                              attempts, None, decision_prefix=step.id)


# ===== claude CLI path, not part of run-llm (reliability-spec.md §5): dispatch
# a detached wrapper that self-enforces the step timeout with kill-tree
# cleanup, poll its exit.json via `wait`. `_wrapper` is launched only by
# `dispatch` -- never invoke it directly. =====

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
# so `wait` has a path out of "running" forever, matching what `poll`
# already provides for run-llm -- without it, a wrapper that never
# writes exit.json reproduces the exact P1 incident (passive wait, no
# liveness signal) this whole spec exists to close.
WAIT_ABORT_MARGIN = 30

# Liveness machinery: how long dispatch waits to acquire a contended
# per-step state lock before refusing (bounded, since dispatch is a
# one-shot call with no external retry loop); how long a freshly launched
# wrapper retries for the run lock during admission (absorbs the
# probe-vs-admission race that state-lock serialization is meant to close,
# plus any other transient holder); how long a wrapper still holding the
# run lock past its own deadline is tolerated as merely late (margin=30s
# alone is too tight for the per-wrapper capability probe plus process
# startup) before `wait` gives up and calls it gone.
STATE_LOCK_WAIT = 5.0
WRAPPER_ADMIT_WAIT = 30.0
RUN_LOCK_RETRY = 2.0
OVERDUE_GRACE = 600
_LOCK_POLL_INTERVAL = 0.1

# The debug verdict `wait` records on the attempt it diagnosed. `dispatch`
# admits --fix only right after DEBUG_RETRY: run-cc grants one fixed re-run
# per visit, and a fix file left from an earlier attempt or cycle must never
# be applied.
DEBUG_RETRY = "RETRY"
DEBUG_FAIL = "FAIL"


def _check_base_dir(base_dir: Path) -> str | None:
    """Same guard as Executor._check_base_dir (executor.py), for `dispatch`/`wait`
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
        "debug_system": steps_dir / f"{stem}_debug_system.md",
        # Step-level, not per-cycle: the liveness/admission machinery
        # tracks whether ANY wrapper for this step id is alive, independent
        # of which cycle it was dispatched for.
        "run_lock": steps_dir / f"{step_id}_run.lock",
        "marker": steps_dir / f"{step_id}_run.json",
        "state_lock": steps_dir / f"{step_id}_state.lock",
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
    return _next_cycle(latest, stepio.load_attempts(
        _a_layer_paths(run_dir, step_id, latest)["attempts"]), force_new)


def _next_cycle(latest: int, attempts: list, force_new: bool) -> tuple[int, list]:
    """(cycle, attempts-so-far-in-that-cycle) after cycle `latest`, whose
    ledger is `attempts`: a new cycle after a success or on `--new-cycle`,
    else the same one. Shared by `dispatch`/`wait` and run-llm (see
    `_resolve_cycle`)."""
    last_class = attempts[-1].get("class") if attempts else None
    if force_new or last_class == "ok":
        return latest + 1, []
    return latest, attempts


def _admission_refusal(wf: model.Workflow, step: model.Step, run_dir: Path,
                       cycle: int, attempts: list, fix: str | None,
                       decision_prefix: str | None = None) -> str | None:
    """Why run-cc's ladder and the run's budgets refuse the next attempt of
    `step` in `cycle` (its ledger so far is `attempts`), or None. Shared by
    `dispatch` and run-llm's `prompt --result`, so the two count one set of
    budgets one way. `decision_prefix` is the id prefix the caller files
    decision requests under (`dispatch`'s `<id>_cNN` by default; run-llm's
    bare step id)."""
    # The one re-dispatch after an abort is a cycle-wide budget of its
    # own, judged before the total-attempt cap so a run already spent
    # on aborts is told to stop, not pointed at --new-cycle (a
    # deviation from the total cap's own precedence, made for message
    # accuracy only -- either order refuses the same calls). Counted by
    # distinct seq, not raw entry count: the time-deadline fallback can
    # in principle record the same seq twice (no lock evidence to key
    # idempotency on), and that must not cost a second redispatch.
    aborted_count = len({a.get("seq") for a in attempts
                        if a.get("class") == "aborted"})
    if aborted_count > DISPATCH_ABORTED_REDISPATCH:
        return (f"step '{step.id}' cycle {cycle}: "
                f"{aborted_count} attempts aborted; the one re-dispatch "
                "after an abort is spent -- stop and report")

    # The cap is per cycle (one step-node visit), matching run-cc's
    # per-visit retry budget; wf.max below is the run-wide backstop.
    decision_attempts = sum(1 for a in attempts if a.get("class") == "decision")
    cap = (step.retry + 1 + DISPATCH_DEBUG_GRANTS + DISPATCH_ABORTED_REDISPATCH
           + DISPATCH_DECISION_GRANTS * decision_attempts)
    if len(attempts) >= cap:
        return (f"dispatch cap exceeded for step '{step.id}' "
                f"(cycle {cycle}): {len(attempts)} attempts already recorded, "
                f"cap is {cap} (retry={step.retry} + 1 initial + "
                f"{DISPATCH_DEBUG_GRANTS} debug + "
                f"{DISPATCH_ABORTED_REDISPATCH} aborted-redispatch"
                + (f" + {DISPATCH_DECISION_GRANTS}x{decision_attempts} decision"
                   if decision_attempts else "") + "). "
                f"If this is a new <while>/<each> iteration rather than a "
                f"retry, pass --new-cycle")

    # What may follow a failed attempt is run-cc's ladder, not the
    # orchestrator's generic retry: a class never re-run as-is (a refused
    # call's re-run could reach the refused effect another way), a spent
    # budget, the one fixed re-run, a fork awaiting its ruling. Enforced
    # here, not left to prompt. The ladder view drops aborted attempts:
    # an aborted attempt never finished, so there is nothing in it for
    # the ladder to judge.
    refusal = _dispatch_refusal(step, run_dir, cycle, _ladder_view(attempts),
                                fix, decision_prefix)
    if refusal:
        return refusal

    log_path = run_dir / "steps.log"
    executed = _count_step_executions(log_path)
    if executed >= wf.max:
        return (f"workflow max={wf.max} step executions reached "
                f"({executed} recorded in {log_path})")
    return None


def _count_step_executions(log_path: Path) -> int:
    """steps.log entries that are step executions, for the `wf.max` cap.

    Only entries carrying a "step" key count: `wfrun ask` writes its
    judgment records to the same file by run-llm.md's convention
    (`ask ... --log steps.log`), and an ask is a condition evaluation, not
    a step execution -- counting raw lines let a branch-heavy workflow hit
    "max reached" long before run-cc would.

    Retry attempts DO each count here, unlike run-cc (whose `_bump_limits`
    fires once per step-node visit, outside the retry loop). That is
    deliberate: when an LLM drives the run there is no in-process step
    counter, so this log is the only runaway backstop, and every attempt is a
    real model call (a subagent or a `pi -p` process under run-llm, a
    `claude -p` wrapper under `dispatch`). Erring toward
    counting them keeps the cap's protective meaning.
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


def _fix_path(run_dir: str | Path, step_id: str) -> Path:
    """Where `wait` writes the fix instruction of a debug RETRY."""
    return Path(run_dir) / "steps" / f"{step_id}_fix.md"


def _ladder_view(attempts: list) -> list:
    """`attempts` with every aborted entry dropped: an aborted attempt is
    not part of run-cc's ladder (it never finished, so there is nothing to
    judge), so every ladder function below reads this instead of the raw
    ledger -- otherwise an aborted-then-redispatched cycle would count a
    vanished wrapper as a real failure against the retry budget, or as the
    "last attempt" a debug fix or --fix applies to."""
    return [a for a in attempts if isinstance(a, dict) and a.get("class") != "aborted"]


def _retry_left(step: model.Step, error_class: str | None, failed: int,
                earlier: list) -> bool:
    """run-cc's retry test after the `failed`-th attempt of a cycle fails
    (Executor._exec_step): a class an identical re-run can clear, and budget
    left. Attempts re-run after a ruling do not spend the budget."""
    reruns = sum(1 for a in earlier if a.get("class") == "decision")
    return claude_cli.is_retryable(error_class) and failed - reruns <= step.retry


def _debug_is_next(step: model.Step, error_class: str | None, seq: int,
                   attempts: list) -> bool:
    """Whether run-cc's ladder hands failed attempt `seq` to debug: no
    identical retry left, `on-error="debug"`, a class debug takes, and no
    fixed re-run granted earlier in the cycle.

    `attempts` is the raw ledger; the failure count and "earlier" list are
    both taken from the ladder view so an aborted attempt elsewhere in the
    cycle never inflates them -- unlike `_plain_block`
    below, this function is handed `seq` (a raw ledger position) rather
    than an already-filtered list, so the view-relative position has to be
    recomputed here."""
    if step.on_error != "debug" or not claude_cli.is_debuggable(error_class):
        return False
    view = _ladder_view(attempts)
    position = sum(1 for a in view if a.get("seq", seq) <= seq)
    earlier = [a for a in view if a.get("seq", seq) < seq]
    if any(a.get("debug") == DEBUG_RETRY for a in earlier):
        return False
    return not _retry_left(step, error_class, position, earlier)


def _on_error_action(step: model.Step, last: dict, earlier: list) -> str:
    """What `on-error` leaves once run-cc's ladder has nothing more to run
    after the cycle's last attempt."""
    if step.on_error == "ignore":
        return ('on-error="ignore": move on; dispatching this step again in a '
                'later <while>/<each> iteration takes --new-cycle')
    if step.on_error != "debug":
        return 'on-error="fail": stop and report'
    cls = last.get("class")
    if last.get("debug") == DEBUG_FAIL:
        why = "debug found no fix"
    elif not claude_cli.is_debuggable(cls):
        why = f"debug does not take {cls}"
    elif any(a.get("debug") == DEBUG_RETRY for a in earlier):
        why = "the one fixed re-run is spent"
    else:
        why = f"no debug verdict is recorded for attempt {last.get('seq')}"
    return f'on-error="debug", but {why}: stop and report'


def _plain_block(step: model.Step, run_dir: Path, cycle: int,
                 attempts: list, decision_prefix: str | None = None
                 ) -> tuple[str, str] | None:
    """(why, what to do instead) when run-cc's ladder would not re-run the
    cycle's last attempt with the step's own prompt; None when it would."""
    if not attempts:
        return None
    last, earlier = attempts[-1], attempts[:-1]
    cls = last.get("class")
    if last.get("debug") == DEBUG_RETRY:
        return ("debug granted a fixed re-run",
                f'dispatch with --fix "$(cat {_fix_path(run_dir, step.id)})"')
    if cls == "decision":
        # Form (b) re-runs once the ruling is filed; before that, a re-run
        # executes the step a second time while the fork is still open.
        pending = decision_mod.pending_request_id(
            decision_mod.decisions_dir(run_dir),
            decision_prefix or f"{step.id}_c{cycle:02d}")
        if pending is None:
            return None
        return (f"decision {pending} has no ruling yet",
                "settle it with record --answer, which says whether to re-run")
    if not claude_cli.is_retryable(cls):
        note = " (a refused tool call)" if cls == "denied" else ""
        return (f"its last attempt ended {cls}{note}, which is never re-run as-is",
                _on_error_action(step, last, earlier))
    if _retry_left(step, cls, len(attempts), earlier):
        return None
    return (f"its retries are spent (retry={step.retry})",
            _on_error_action(step, last, earlier))


def _dispatch_refusal(step: model.Step, run_dir: Path, cycle: int,
                      attempts: list, fix: str | None,
                      decision_prefix: str | None = None) -> str | None:
    """Why run-cc's ladder (Executor._exec_step) would not make this dispatch,
    ending in the one thing to do instead; None when it would.

    --fix is admitted only right after the debug RETRY `wait` recorded, so
    the one fixed re-run cannot repeat and a stale fix file never applies.
    `attempts` here is always the ladder view (`_ladder_view`, called by
    every caller before passing it in): an aborted attempt IS recorded to
    the ledger, but it is not part of the ladder, so re-dispatching after
    one is judged as if it had never happened.
    """
    where = f"step '{step.id}' cycle {cycle}"
    if fix:
        if attempts and attempts[-1].get("debug") == DEBUG_RETRY:
            return None
        blocked = _plain_block(step, run_dir, cycle, attempts, decision_prefix)
        action = "dispatch without --fix" if blocked is None else blocked[1]
        return (f"{where}: no debug RETRY is recorded for its last attempt, "
                f"so --fix has nothing to apply -- {action}")
    blocked = _plain_block(step, run_dir, cycle, attempts, decision_prefix)
    if blocked is None:
        return None
    why, action = blocked
    return f"{where}: {why} -- {action}"


# ----- Liveness --------------------------------------------------------
# Whether a step's most recently dispatched attempt is still running, is
# overdue, or has vanished, decided from strongest evidence to weakest: the
# run lock (a wrapper holds it only while it is actually admitted and
# running, and only ever releases it after its own final write -- see
# `cmd_run_wrapper`), then the ledger/exit.json it leaves behind, and only
# as a last resort the wall-clock deadline. PID liveness is never checked
# (a PID can be reused; the lock cannot lie about who holds it).

_LOCK_CONTENDED = object()  # never acquired: another holder has it
_LOCK_UNSUPPORTED = object()  # locking is not implemented for this path/FS
_LOCK_UNAVAILABLE = object()  # the lock call raised for some other reason


def _acquire_with_retry(path: Path, budget: float):
    """`runlock.try_acquire` in a bounded poll loop. Returns a
    `runlock.LockHandle` on success; otherwise one of three distinct
    outcomes, because callers treat them differently:
    - `_LOCK_CONTENDED`: `budget` seconds passed while another holder kept
      it.
    - `_LOCK_UNSUPPORTED`: the lock call itself raised an errno that means
      locking is not implemented here at all (`runlock.is_unsupported`) --
      every caller falls open on this.
    - `_LOCK_UNAVAILABLE`: the lock call raised anything else (permission,
      a transient filesystem error) -- callers decide per role whether that
      means fail-open or fail-closed.
    `budget<=0` tries exactly once, non-blocking."""
    deadline = time.monotonic() + budget
    while True:
        try:
            handle = runlock.try_acquire(str(path))
        except OSError as e:
            return _LOCK_UNSUPPORTED if runlock.is_unsupported(e) else _LOCK_UNAVAILABLE
        if handle is not None:
            return handle
        if time.monotonic() >= deadline:
            return _LOCK_CONTENDED
        time.sleep(min(_LOCK_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))


_LOCK_OUTCOMES = (_LOCK_CONTENDED, _LOCK_UNSUPPORTED, _LOCK_UNAVAILABLE, None)


def _release_lock(outcome) -> None:
    if outcome not in _LOCK_OUTCOMES:
        runlock.release(outcome)


def _read_json_tolerant(path: str | Path) -> dict | None:
    """Same tolerant-read policy as `_read_exit_json`: a run-lock marker or
    wait record that does not exist yet, or is caught mid-write by another
    process's atomic replace, reads as None rather than raising."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_json_retry(path: str | Path, budget: float) -> dict | None:
    """`_read_json_tolerant` in a bounded poll loop: the first readable
    result, or None once `budget` seconds pass without one."""
    deadline = time.monotonic() + budget
    while True:
        data = _read_json_tolerant(path)
        if data is not None or time.monotonic() >= deadline:
            return data
        time.sleep(min(_LOCK_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))


class _AttemptState:
    __slots__ = ("kind", "evidence", "marker", "entry", "probed")

    def __init__(self, kind: str, evidence: str | None = None,
                marker: dict | None = None, entry: dict | None = None,
                probed: bool = False):
        self.kind = kind  # NONE / DONE / DONE_ABORTED / DONE_STUCK /
                          # RUNNING / OVERDUE / GONE
        self.evidence = evidence  # GONE only: "run-lock" | "deadline"
        self.marker = marker  # only ever the CURRENT token's marker
        self.entry = entry  # DONE/DONE_ABORTED/DONE_STUCK: the matched
                            # ledger entry, when found via the ledger
                            # rather than exit.json
        self.probed = probed  # whether the run lock was actually probed
                              # before this state was decided -- false only
                              # for a DONE reached via exit.json alone,
                              # before any probe happens at all


def _attempt_state(handle: dict, attempts_path: Path, exit_path: Path,
                   lock_paths: dict, now: float) -> _AttemptState:
    """NONE / DONE / DONE_ABORTED / DONE_STUCK / RUNNING / OVERDUE / GONE
    for `handle`'s most recent attempt -- shared by `cmd_dispatch` and
    `cmd_wait` so the two commands can never disagree about the same
    attempt. Caller holds the per-step state lock for the duration of this
    call (or has decided to proceed fail-open without one) so the
    marker-then-probe read below cannot interleave with a wrapper's own
    admission.

    Read order, and why it cannot be reordered:
    1. exit.json alone decides DONE first: reading it is always safe (a
       wrapper writes it at most once, atomically, and only ever as its
       very last act before releasing the run lock).
    2. The marker is read BEFORE the run lock is probed: a wrapper that
       wins the lock during the probe call itself must still be seen as
       this attempt's holder, or dispatch could launch a second one.
    3. HELD is the strongest signal there is: while the run lock is held,
       the ledger is never opened at all. A wrapper appends to it (via a
       non-atomic-looking but still `os.replace`-based write) only while
       holding the run lock, and admission (which would let a NEW wrapper
       start doing the same) requires the same state lock this call's
       caller already holds -- so once HELD is observed, no concurrent
       writer to the ledger can possibly exist, but reading it any FURTHER
       while genuinely still held risks catching that write mid-replace on
       Windows and misreading a real attempt as absent.
    4. Only once the probe is NOT held is the ledger consulted, together
       with a fresh re-read of exit.json (a wrapper may have finished in
       the gap between step 1 and the probe).

    The one state that can be reached WITHOUT ever probing is `DONE` via
    exit.json alone (step 1): `probed` on the returned state is False only
    there, so `cmd_dispatch` can still probe before treating it as settled
    -- a narrow window exists between a wrapper writing exit.json and
    releasing the run lock, and a HELD lock there still means "do not
    start a second one yet", not "already gone".
    """
    attempt = handle.get("attempt")
    token = handle.get("token")

    if _read_exit_json(exit_path) is not None:
        return _AttemptState("DONE")

    marker = _read_json_tolerant(lock_paths["marker"])
    marker_matches = (token is not None and isinstance(marker, dict)
                      and marker.get("token") == token)
    matched_marker = marker if marker_matches else None
    probe_result = runlock.probe(str(lock_paths["run_lock"]))

    deadline = None
    if handle.get("started_at") is not None and handle.get("timeout") is not None:
        deadline = handle["started_at"] + handle["timeout"] + WAIT_ABORT_MARGIN
    overdue = deadline is not None and now >= deadline

    if probe_result == runlock.HELD:
        return _AttemptState("OVERDUE" if overdue else "RUNNING",
                             marker=matched_marker, probed=True)

    # Not held: no wrapper can be actively writing to the ledger right now
    # (see the docstring), so it is safe to open it.
    if _read_exit_json(exit_path) is not None:
        return _AttemptState("DONE", probed=True)
    entry = None
    if attempt is not None:
        entry = next((a for a in stepio.load_attempts(attempts_path)
                     if isinstance(a, dict) and a.get("seq") == attempt), None)
    if entry is not None:
        if entry.get("class") == "aborted":
            return _AttemptState("DONE_ABORTED", entry=entry, probed=True)
        if probe_result == runlock.FREE and marker_matches:
            # The ledger already carries this attempt's final (non-aborted)
            # class, but exit.json never appeared, and the wrapper that
            # wrote the ledger entry has provably released the run lock:
            # not merely slow, genuinely stuck.
            return _AttemptState("DONE_STUCK", marker=matched_marker,
                                 entry=entry, probed=True)
        return _AttemptState("DONE", entry=entry, probed=True)

    if attempt is None:
        return _AttemptState("NONE", probed=True)

    if probe_result == runlock.FREE and marker_matches:
        return _AttemptState("GONE", evidence="run-lock",
                             marker=matched_marker, probed=True)

    if overdue:
        return _AttemptState("GONE", evidence="deadline",
                             marker=matched_marker, probed=True)

    return _AttemptState("RUNNING", marker=matched_marker, probed=True)


def _attempt_pid(state: _AttemptState, handle: dict):
    """The most trustworthy pid to name in a message: the marker's (the
    lock holder itself), when one matched this attempt's own token, else
    the handle's own launch pid (which, behind a venv shim, can be a
    different process than the wrapper)."""
    if state.marker:
        return state.marker.get("pid", handle.get("wrapper_pid"))
    return handle.get("wrapper_pid")


def _unrecorded_completion(handle: dict, exit_path: Path, wait_path: Path) -> bool:
    """An attempt whose exit.json already exists but whose wait.json never
    recorded it must block dispatch -- otherwise a finished-but-unrecorded
    result gets silently discarded (a new cycle opens, or dispatch's own
    stale-file cleanup deletes it) before its vars.json/log side effects
    ever ran. Scoped to token-bearing handles only: a legacy handle (no
    token) predates this guard and keeps the old behavior of trusting the
    ledger alone."""
    if not handle.get("token"):
        return False
    if _read_exit_json(exit_path) is None:
        return False
    wait_record = _read_json_tolerant(wait_path)
    return not (isinstance(wait_record, dict)
               and wait_record.get("attempt") == handle.get("attempt"))


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
    lock_paths = _a_layer_paths(run_dir, args.step_id, 1)

    # Serializes this call against any other dispatch/wait/admission for the
    # same step id: without it, two concurrent dispatches could both see the
    # same vanished attempt as gone and each launch a replacement for it.
    state_lock = _acquire_with_retry(lock_paths["state_lock"], STATE_LOCK_WAIT)
    if state_lock is _LOCK_CONTENDED:
        print(f"error: step '{args.step_id}': another dispatch or wait is "
              "working on it -- run this again after that call returns",
              file=sys.stderr)
        return 1
    if state_lock in (_LOCK_UNSUPPORTED, _LOCK_UNAVAILABLE):
        print(f"warning: step '{args.step_id}': the per-step state lock is "
              "unavailable on this filesystem; proceeding without it",
              file=sys.stderr)
    try:
        # Whether the previous dispatch's wrapper is still running, overdue,
        # or gone -- checked on the LATEST cycle regardless of --new-cycle:
        # a live wrapper must block a new cycle exactly as it blocks a
        # retry within the same one.
        latest = _latest_cycle(run_dir / "steps", args.step_id)
        if latest is not None:
            latest_paths = _a_layer_paths(run_dir, args.step_id, latest)
            handle = _read_json_tolerant(latest_paths["handle"])
            if handle is not None:
                now = time.time()
                state = _attempt_state(handle, latest_paths["attempts"],
                                       latest_paths["exit"], lock_paths, now)
                pid = _attempt_pid(state, handle)
                if state.kind == "RUNNING":
                    print(f"error: step '{args.step_id}': attempt "
                          f"{handle.get('attempt')} is still running "
                          f"(wrapper pid {pid}) -- call wait on "
                          f"{latest_paths['handle']} instead", file=sys.stderr)
                    return 1
                if state.kind == "OVERDUE":
                    print(f"error: step '{args.step_id}': attempt "
                          f"{handle.get('attempt')} still holds the step's "
                          f"run lock past its deadline (pid {pid}) -- stop "
                          "and report", file=sys.stderr)
                    return 1
                if state.kind == "GONE":
                    stepio.record_aborted_attempt(
                        step, latest_paths["attempts"], handle.get("attempt"),
                        run_dir / "steps.log", latest_paths["result"],
                        {"evidence": state.evidence, "detected_by": "dispatch"})
                if state.kind == "DONE" and not state.probed:
                    # exit.json alone decided DONE, before any probe (see
                    # `_attempt_state`'s docstring): a narrow window exists
                    # between a wrapper writing exit.json and releasing the
                    # run lock, and HELD there still means "do not start a
                    # second one yet".
                    if runlock.probe(str(lock_paths["run_lock"])) == runlock.HELD:
                        print(f"error: step '{args.step_id}': attempt "
                              f"{handle.get('attempt')} is still running "
                              f"(wrapper pid {handle.get('wrapper_pid')}) -- "
                              f"call wait on {latest_paths['handle']} "
                              "instead", file=sys.stderr)
                        return 1
                # A finished attempt `wait` never got to record must block
                # dispatch too, whatever the ledger/lock state says --
                # otherwise its result is silently discarded (a new cycle
                # opens over it, or the stale-file cleanup below deletes
                # it) before vars.json/the log ever saw it.
                if _unrecorded_completion(handle, latest_paths["exit"],
                                          latest_paths["wait"]):
                    print(f"error: step '{args.step_id}': attempt "
                          f"{handle.get('attempt')} finished but wait has "
                          f"not recorded it -- run wait on "
                          f"{latest_paths['handle']} first; if wait cannot "
                          "record it, stop and report", file=sys.stderr)
                    return 1
                # Ledger says ok, exit.json never appeared, wrapper is gone:
                # `wait` cannot apply the result, and a new cycle (or the
                # stale-file cleanup) would discard it. Refused for ok only;
                # a non-ok class is also what a failed launch leaves behind
                # (cleanup removed exit.json, the handle was restored), and
                # the ladder judges that one.
                if (state.kind == "DONE_STUCK"
                        and state.entry.get("class") == "ok"):
                    print(f"error: step '{args.step_id}': attempt "
                          f"{handle.get('attempt')} recorded ok but left no "
                          "exit.json, so its result was never applied -- "
                          "stop and report", file=sys.stderr)
                    return 1
            elif runlock.probe(str(lock_paths["run_lock"])) == runlock.HELD:
                # An unreadable handle is not proof that no wrapper is
                # alive: the run lock still says so.
                print(f"error: step '{args.step_id}': its latest attempt is "
                      f"still running (handle {latest_paths['handle']} is "
                      "unreadable right now) -- call wait on "
                      f"{latest_paths['handle']} instead", file=sys.stderr)
                return 1

        cycle, attempts = _resolve_cycle(run_dir, args.step_id, args.new_cycle)
        paths = _a_layer_paths(run_dir, args.step_id, cycle)

        refusal = _admission_refusal(wf, step, run_dir, cycle, attempts, args.fix)
        if refusal:
            print(f"error: {refusal}", file=sys.stderr)
            return 1

        try:
            # Same auto-enumeration as run-llm's, keyed per cycle since
            # `dispatch` has one (§14.3).
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
            dispatch_model = modelmap.resolve(dispatch_model, "cc", allow_legacy=True)
        except modelmap.ModelMapError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        # Least privilege, same rule as run-cc: --permission-mode reaches only
        # steps whose resolved tools can write.
        permission = (args.permission_mode
                      if model.tools_can_write(dispatch_tools) else None)

        for p in (paths["exit"], paths["result"], paths["wait"]):
            # Stale-read prevention (run-llm's `prompt --result` needs no
            # deletion: each attempt has its own result file and a keyed
            # sentinel): a leftover exit/result from a prior attempt must never
            # be mistaken for this one's, and a leftover wait record must not
            # make this attempt look finished.
            # Safe here: a still-live prior attempt already refused above,
            # and a gone one was just settled to the ledger/log, never to
            # these files.
            if p.is_file():
                p.unlink()
        paths["system"].write_text(system, encoding="utf-8")
        paths["prompt"].write_text(prompt, encoding="utf-8")

        seq = len(attempts) + 1
        token = secrets.token_hex(16)
        wrapper_cmd = [sys.executable, "-m", "wfrun", "_wrapper",
                      "--system-file", str(paths["system"]),
                      "--prompt-file", str(paths["prompt"]),
                      "--exit-file", str(paths["exit"]),
                      "--result-file", str(paths["result"]),
                      "--attempts-file", str(paths["attempts"]),
                      "--seq", str(seq), "--cwd", str(base_dir),
                      "--timeout", str(step.timeout),
                      "--handle-file", str(paths["handle"]), "--token", token]
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

        # Written before Popen, token included, so the wrapper's own
        # admission (which reads --handle-file under its own state-lock
        # hold) always finds this attempt's token there; wrapper_pid is
        # filled in below once Popen returns it. Atomic, and restored (or
        # removed, if this cycle had none before) if the launch itself never
        # happens.
        started_at = time.time()
        prior_handle = paths["handle"].read_bytes() if paths["handle"].is_file() else None
        handle_fields = {
            "wrapper_pid": None, "attempt": seq, "cycle": cycle,
            "started_at": started_at, "timeout": step.timeout, "token": token,
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
        }
        stepio.write_text_atomic(paths["handle"], json.dumps(
            handle_fields, ensure_ascii=False, indent=2))
        try:
            wrapper_proc = subprocess.Popen(
                wrapper_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, **popen_kwargs)
        except OSError as e:
            if prior_handle is not None:
                paths["handle"].write_bytes(prior_handle)
            else:
                paths["handle"].unlink(missing_ok=True)
            print(f"error: failed to launch wrapper for step "
                  f"'{args.step_id}': {e}", file=sys.stderr)
            return 2

        handle_fields["wrapper_pid"] = wrapper_proc.pid
        stepio.write_text_atomic(paths["handle"], json.dumps(
            handle_fields, ensure_ascii=False, indent=2))

        print(f"{paths['handle']} (dispatched, pid={wrapper_proc.pid}, "
              f"cycle={cycle}, seq={seq})")
        return 0
    finally:
        _release_lock(state_lock)


def cmd_run_wrapper(args) -> int:
    """Internal: launched detached by `cmd_dispatch` only.

    Admission runs first: take the run lock, confirm this is still the
    current attempt (token matches the handle, seq not already in the
    ledger), and write a marker carrying this attempt's own pid and token
    -- all under the per-step state lock, so it cannot interleave with an
    observer's marker-then-probe read (`_attempt_state`). Only once
    admitted does this run the actual `claude -p` call with kill_tree=True
    (reliability-spec.md §5.1) and persist exit.json / result.json / an
    attempts.json entry -- never touches vars.json or steps.log (that is
    `wait`'s job).

    Fail-closed by default: an admission step that cannot prove
    exclusivity exits without writing anything, rather than risk running
    alongside another holder. The one exception is a lock call that raises
    an errno meaning locking itself is not implemented here at all
    (`runlock.is_unsupported`) -- there, this falls open like every other
    participant, since no amount of retrying would ever succeed.
    """
    # Not yet under the state lock, so this read can land inside dispatch's
    # atomic rewrite of the same handle (a transient PermissionError on
    # Windows); retry briefly instead of dropping an attempt nobody will
    # ever record.
    handle = _read_json_retry(args.handle_file, RUN_LOCK_RETRY)
    if handle is None:
        return 1

    lock_paths = _a_layer_paths(Path(handle["run_dir"]), handle["step_id"],
                                handle.get("cycle", 1))

    state_lock = _acquire_with_retry(lock_paths["state_lock"], WRAPPER_ADMIT_WAIT)
    if state_lock in (_LOCK_CONTENDED, _LOCK_UNAVAILABLE):
        return 1

    run_lock = _LOCK_CONTENDED
    admitted = False
    try:
        # A short bounded retry, not a single non-blocking try: it absorbs
        # the run lock's own probe-vs-admission race and any other
        # transient holder, without ever blocking as long as a live
        # wrapper would.
        run_lock = _acquire_with_retry(lock_paths["run_lock"], RUN_LOCK_RETRY)
        if run_lock is not _LOCK_UNAVAILABLE and run_lock is not _LOCK_CONTENDED:
            fresh_handle = _read_json_tolerant(args.handle_file)
            already_recorded = any(
                isinstance(a, dict) and a.get("seq") == args.seq
                for a in stepio.load_attempts(args.attempts_file))
            # A token mismatch means a newer dispatch has replaced this
            # attempt; an already-recorded seq means this admission is a
            # duplicate. Either way: touch nothing, leave no trace.
            if (fresh_handle is not None
                    and fresh_handle.get("token") == args.token
                    and not already_recorded):
                stepio.write_text_atomic(lock_paths["marker"], json.dumps({
                    "token": args.token, "cycle": handle.get("cycle", 1),
                    "attempt": args.seq, "pid": os.getpid(),
                    "acquired_at": time.time(),
                }, ensure_ascii=False))
                admitted = True
    finally:
        _release_lock(state_lock)

    if not admitted:
        _release_lock(run_lock)
        return 1

    try:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")

        launched = claude_cli._launch(
            prompt, system_prompt_file=args.system_file, model=args.model,
            effort=args.effort, tools=args.tools, schema=args.schema,
            timeout=args.timeout, cwd=args.cwd,
            permission_mode=args.permission_mode, kill_tree=True)

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

        # Order matters: exit.json is the completion authority `wait` polls
        # for, so everything it will read must already be in place before it
        # appears. Both writes are atomic (stepio.write_text_atomic) so
        # `wait` can never observe a half-written file.
        stepio.write_text_atomic(args.result_file, raw_stdout)
        stepio.append_attempt(args.attempts_file, args.seq, status)
        stepio.write_text_atomic(args.exit_file,
                                 json.dumps(exit_data, ensure_ascii=False, indent=2))
        return 0
    finally:
        # Released after exit.json is written on the success path (this is
        # exactly the evidence `_attempt_state` relies on to tell a
        # finished attempt from a merely-vanished one); released here
        # regardless on any exception, so a wrapper that dies mid-launch
        # still frees the lock for the observer to see FREE.
        _release_lock(run_lock)


def _settle_gone_wait(handle: dict, attempts_path: Path, steps_log: Path,
                      state: _AttemptState) -> int:
    """Record a gone attempt as aborted for `cmd_wait` (caller holds the
    per-step state lock) and return `wait`'s exit code."""
    wf = parser.parse_file(handle["xml"])
    try:
        step = stepio.find_step(wf, handle.get("step_id"))
    except stepio.StepIOError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    attempt = handle.get("attempt")
    stepio.record_aborted_attempt(
        step, attempts_path, attempt, steps_log,
        Path(handle["result_path"]),
        {"evidence": state.evidence, "detected_by": "wait"})
    print(f"aborted: attempt {attempt} is gone (evidence="
          f"{state.evidence}, last known wrapper pid "
          f"{_attempt_pid(state, handle)})", file=sys.stderr)
    return 3


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
    # running within its own timeout. OVERDUE_GRACE gives a wrapper that
    # still holds the run lock past that deadline extra room -- HELD is
    # positive evidence it is alive, merely late.
    abort_at = handle["started_at"] + handle["timeout"] + WAIT_ABORT_MARGIN
    overdue_deadline = abort_at + OVERDUE_GRACE

    attempt = handle.get("attempt")
    run_dir = Path(handle["run_dir"])
    step_id = handle.get("step_id")
    paths = _a_layer_paths(run_dir, step_id, handle.get("cycle", 1))
    attempts_path = paths["attempts"]
    # An abort always settles to the run's own steps.log, regardless of
    # what (if anything) this call's own --log points at: that is the one
    # file the dispatch cap (`_count_step_executions`) is guaranteed to
    # read, and the ledger is the idempotency key either detector shares.
    steps_log = run_dir / "steps.log"

    while True:
        exit_data = _read_exit_json(exit_path)
        if exit_data is not None:
            return _finish_wait(handle, args, exit_data)

        now = time.time()
        lock = _acquire_with_retry(paths["state_lock"], 0.0)
        state = None
        if lock is not _LOCK_CONTENDED:
            try:
                state = _attempt_state(handle, attempts_path, exit_path, paths, now)
                if state.kind == "GONE":
                    # Settled under the same hold as the evaluation: once
                    # the lock is released, a late wrapper could be admitted
                    # and run an attempt already reported as aborted.
                    return _settle_gone_wait(handle, attempts_path, steps_log,
                                             state)
            finally:
                _release_lock(lock)

        if state is not None and state.kind == "DONE_ABORTED":
            # Already settled by an earlier wait/dispatch call: replay the
            # verdict, not a fresh liveness evaluation (idempotent, like
            # `_finish_wait`'s own wait.json replay above).
            print(f"aborted: attempt {attempt} already recorded "
                  f"({state.entry.get('evidence', 'unknown')})", file=sys.stderr)
            return 3

        if state is not None and state.kind == "DONE_STUCK":
            # The ledger already carries this attempt's final (non-aborted)
            # class, but exit.json never appeared, and the wrapper that
            # wrote it has provably released the run lock: not merely
            # slow, gone. Nothing to settle (it is already recorded);
            # nothing to gain by waiting further.
            print(f"aborted: attempt {attempt} recorded "
                  f"{state.entry.get('class')} but left no exit.json",
                  file=sys.stderr)
            return 3

        # A ledger entry that is not provably stuck (the run lock's probe
        # came back UNKNOWN, or the marker did not match -- most likely a
        # legacy, token-less handle) is treated exactly like RUNNING below,
        # including the overdue-grace cap, rather than guessed at -- but it
        # must still hit that cap, or a legacy handle whose exit.json never
        # arrives would report "running" forever (the exact P1 incident
        # WAIT_ABORT_MARGIN exists to close). `unproven` carries the entry
        # through so the cap's own message can still name its class.
        unproven = None
        if state is not None and state.kind == "DONE":
            if _read_exit_json(exit_path) is not None:
                continue  # the loop head will pick it up via _finish_wait
            unproven = state.entry
            state = _AttemptState("RUNNING")

        state_lock_stuck = lock is _LOCK_CONTENDED
        overdue = state is not None and state.kind == "OVERDUE"
        if (state_lock_stuck or overdue
                or unproven is not None) and now >= overdue_deadline:
            # A state lock that stays contended on every poll, or a ledger
            # entry that never gets an exit.json to confirm it, must still
            # hit a time upper bound, exactly like a wrapper that stays
            # overdue -- all three would otherwise keep `wait` returning
            # "running" forever.
            if state_lock_stuck:
                print(f"aborted: attempt {attempt} -- the step's state lock "
                      "stayed contended past the overdue grace window; stop "
                      "and report", file=sys.stderr)
            elif unproven is not None:
                print(f"aborted: attempt {attempt} recorded "
                      f"{unproven.get('class')} but left no exit.json past "
                      "the overdue grace window -- stop and report",
                      file=sys.stderr)
            else:
                print(f"aborted: attempt {attempt} (wrapper pid "
                      f"{_attempt_pid(state, handle)}) is still running "
                      "past its deadline -- stop and report", file=sys.stderr)
            return 3

        if now >= call_deadline:
            if overdue:
                print(f"running: attempt {attempt} is past its own deadline "
                      f"but still holds the run lock (wrapper pid "
                      f"{_attempt_pid(state, handle)})", file=sys.stderr)
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
        # `dispatch`/`wait` know their cycle, so their requests are filed per
        # cycle.
        decisions_dir=decision_mod.decisions_dir(handle["run_dir"]),
        decision_prefix=f"{step.id}_c{handle.get('cycle', 1):02d}")
    print(message)
    seq = handle.get("attempt", 1)
    attempts_path = _a_layer_paths(Path(handle["run_dir"]), step.id,
                                   handle.get("cycle", 1))["attempts"]
    if status == "error" and res.ok:
        # The wrapper recorded ok from the CLI's verdict alone, and this
        # command's own checks (expect-file, the vars file) failed the step.
        # Left ok, the next dispatch would read the cycle as finished and
        # start a new one with a fresh retry budget.
        stepio.update_attempt(attempts_path, seq, {
            "class": res.error_class or "error", "reclassified_from": "ok"})
    stepio.write_text_atomic(wait_record, json.dumps(
        {"attempt": handle.get("attempt"), "status": status, "message": message},
        ensure_ascii=False, indent=2))

    if status == "error" and _debug_is_next(step, res.error_class, seq,
                                            stepio.load_attempts(attempts_path)):
        system = Path(handle.get("system_path", "")).read_text(encoding="utf-8") \
            if handle.get("system_path") else ""
        prompt_text = Path(handle.get("prompt_path", "")).read_text(encoding="utf-8") \
            if handle.get("prompt_path") else ""
        debug_system_path = _a_layer_paths(
            Path(handle["run_dir"]), step.id, handle.get("cycle", 1))["debug_system"]
        diagnosis = adp.diagnose(step, f"{system}\n\n{prompt_text}", res,
                                 role_file=debug_system_path,
                                 cwd=str(Path(handle["xml"]).resolve().parent))
        fix_path = _fix_path(handle["run_dir"], step.id)
        if diagnosis.action == "RETRY" and diagnosis.fix_instruction:
            fix_path.write_text(diagnosis.fix_instruction, encoding="utf-8")
            verdict = DEBUG_RETRY
            print(f"debug: RETRY -- fix written to {fix_path}; "
                  f"re-dispatch with --fix \"$(cat {fix_path})\"", file=sys.stderr)
        else:
            verdict = DEBUG_FAIL
            print(f"debug: FAIL -- {diagnosis.reason}", file=sys.stderr)
        stepio.update_attempt(attempts_path, seq, {"debug": verdict})

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
    resolved_decider_model = modelmap.resolve(decider_model, "llm")
    print(f"decider:  {decider}"
          + (f" (model={resolved_decider_model})" if decider == "llm" else ""))
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
    p_val.add_argument("--backend", choices=("auto", "cc", "pi"), default="auto",
                       help="which facility the workflow targets; only the "
                            "model-name checks read it (pi verifies names "
                            "against `pi --list-models`, cc checks none)")
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
                          help="admit the next attempt (exit 1 when it may not "
                               "start) and append the file-based response "
                               "protocol pointing at the attempt's own result "
                               "file, derived from this path (a step's: "
                               "<id>_cNN_aNN_result.md); a step's handle "
                               "<id>_handle.json is written beside it")
    p_prompt.add_argument("--fix", help="debug-granted fix instruction to append")
    p_prompt.add_argument("--new-cycle", action="store_true",
                          help="with --result: the next attempt opens a new "
                               "cycle (a new <while>/<each> iteration after a "
                               "failure the workflow tolerated); refused after "
                               "an aborted attempt, which goes on only "
                               "through stop, judge and --redispatch")
    p_prompt.add_argument("--redispatch", metavar="JUDGE_REPLY",
                          help="with --result: run an aborted attempt's step "
                               "again; the judge's one-line reply "
                               "('allow <record>')")
    p_prompt.set_defaults(func=cmd_prompt)

    p_rec = sub.add_parser(
        "record", help="record a step's result file: update vars, append log, "
                       "print the verdict, never step content: ok(0) / "
                       "error(1) / aborted(3) / decision(4) / rerun(5); a "
                       "refused record exits 2")
    p_rec.add_argument("workflow")
    p_rec.add_argument("step_id")
    p_rec.add_argument("--result", required=True, metavar="RESULT_FILE")
    p_rec.add_argument("--vars", required=True, metavar="VARS_JSON")
    p_rec.add_argument("--log", metavar="LOG_FILE")
    p_rec.add_argument("--reply", metavar="LINE",
                       help="the single reply line the orchestrator received "
                            "back from the subagent (optional; enables the "
                            "claimed-ok-but-no-result / reply-file-mismatch "
                            "checks; not read for an attempt whose stop is "
                            "recorded as confirmed or unconfirmed)")
    p_rec.add_argument("--answer", metavar="ANSWER_FILE",
                       help="settle this step's open decision request with "
                            "this ruling file (first line 'option: <N|none>'). "
                            "Exits 0 when the step continues without "
                            "re-running, 5 when it must be re-run from move 1")
    p_rec.add_argument("--debug-reply", metavar="LINE",
                       help="the one-line reply of the debug diagnosis "
                            "delegated for the step's last failed attempt "
                            "(RETRY or FAIL); recorded on that attempt")
    p_rec.add_argument("--decider", choices=list(model.DECIDER_VALUES),
                       help="who wrote the --answer file. Defaults to the "
                            "workflow's own decider; pass 'human' when a "
                            "person answered a request that fell back to "
                            "them, so it does not spend the llm cap")
    p_rec.set_defaults(func=cmd_record)

    p_decide = sub.add_parser(
        "decide", help="run-llm: record and print the one llm-decider "
                       "delegation of a step's open decision request "
                       "(paths only; a request is delegated once)")
    p_decide.add_argument("workflow")
    p_decide.add_argument("step_id")
    p_decide.add_argument("--result", required=True, metavar="RESULT_FILE",
                          help="the step's --result path, as given to prompt")
    p_decide.set_defaults(func=cmd_decide)

    p_poll = sub.add_parser(
        "poll", help="check a delegated attempt's handle (or a judge's): "
                     "done(0) / running(10) / deadline-exceeded(11); returns "
                     "at once unless --wait")
    p_poll.add_argument("handle", metavar="HANDLE_FILE",
                        help="the steps/<id>_handle.json written by "
                             "'wfrun prompt --result', or the judge handle "
                             "that 'wfrun judge' prints")
    p_poll.add_argument("--wait", action="store_true",
                        help="block until the attempt is done or past its "
                             "deadline; writes nothing")
    p_poll.set_defaults(func=cmd_poll)

    p_stop = sub.add_parser(
        "stop", help="run-llm: after a stop request, observe an attempt for "
                     "at most a minute and record whether its stop is "
                     "confirmed: done(0) / confirmed(12) / unconfirmed(13)")
    p_stop.add_argument("handle", metavar="HANDLE_FILE",
                        help="the step's handle, as 'wfrun prompt --result' "
                             "wrote it")
    p_stop.add_argument("--facet", required=True, choices=list(llmgate.FACETS),
                        help="how the attempt was delegated: the Claude Code "
                             "Agent tool, or a Pi CLI process")
    p_stop.add_argument("--task-stop", choices=list(llmgate.TASK_STOP_RESULTS),
                        help="cc only: what TaskStop reported -- stopped; "
                             "completed (the task had already ended); failed "
                             "(the call errored or could not be made)")
    p_stop.add_argument("--observe", type=int, default=llmgate.STOP_OBSERVE_MAX,
                        metavar="SECONDS",
                        help=f"observation window, 1 to "
                             f"{llmgate.STOP_OBSERVE_MAX} (default)")
    p_stop.set_defaults(func=cmd_stop)

    p_pids = sub.add_parser(
        "pids", help="run-llm, Pi CLI: record the processes an attempt was "
                     "launched as (PID, image, creation time), once, at launch")
    p_pids.add_argument("handle", metavar="HANDLE_FILE",
                        help="the step's handle, as 'wfrun prompt --result' "
                             "wrote it")
    p_pids.add_argument("pids", nargs="+", type=int, metavar="PID",
                        help="Windows PIDs: the launching shell and the CLI's "
                             "own process")
    p_pids.set_defaults(func=cmd_pids)

    p_judge = sub.add_parser(
        "judge", help="run-llm: issue the one judge of an aborted attempt whose "
                      "stop is confirmed (paths only; one judge per attempt)")
    p_judge.add_argument("workflow")
    p_judge.add_argument("step_id")
    p_judge.add_argument("--result", required=True, metavar="RESULT_FILE",
                         help="the step's --result path, as given to prompt")
    p_judge.set_defaults(func=cmd_judge)

    p_dispatch = sub.add_parser(
        "dispatch", help="claude CLI path, not part of run-llm "
                         "(reliability-spec.md §5): launch a step's claude -p "
                         "call in a detached, self-timing-out wrapper and "
                         "return immediately")
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
    p_wrapper.add_argument("--handle-file", required=True,
                           help="the handle dispatch wrote, for admission "
                                "(run lock + marker) before launching")
    p_wrapper.add_argument("--token", required=True,
                           help="this attempt's token, checked against the "
                                "handle's own during admission")
    p_wrapper.set_defaults(func=cmd_run_wrapper)

    p_wait = sub.add_parser(
        "wait", help="claude CLI path, not part of run-llm "
                     "(reliability-spec.md §5): poll a dispatched step's "
                     "handle up to --max seconds; on completion, update "
                     "vars/log like record")
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
