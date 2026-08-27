"""Deterministic workflow execution loop.

The orchestrator is this module: control flow, variable resolution, limits and
error policy are ordinary Python. LLMs appear in exactly three places — step
execution (claude -p subagent), ask= condition judgment, and the optional
debug diagnosis (adp.py).
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from glob import glob as _glob
from pathlib import Path

from . import adp, claude_cli, model, modelmap, modes, parser, stepio
from . import adjudicate as adjudicate_mod
from . import decision as decision_mod
from . import lint as lint_mod
from .agents import discover_agents
from .ccdirs import claude_config_dirs
from .interp import InterpError, interpolate, safe_eval
from .state import ReplayCursor, RunState


class WorkflowFailure(Exception):
    pass


class DecisionRequested(Exception):
    """One or more steps asked for adjudication and the run stopped for it.

    Deliberately NOT a WorkflowFailure subclass (xml-wf-decision-request.md
    §8): `cmd_run` reports a WorkflowFailure as FAILED and points at a plain
    `wfrun resume`, which would both mislabel this stop and hand back a resume
    command that cannot make progress. A separate type forces a separate
    report and exit code.

    `requests` holds one record per pending request -- more than one only via
    `<parallel>`, where every sibling is allowed to finish first (§9).
    """

    def __init__(self, requests: list[dict]):
        self.requests = requests
        ids = ", ".join(r["request_id"] for r in requests)
        super().__init__(f"decision requested: {ids}")


class _DecisionSettledA(Exception):
    """An llm decider settled a request as form (a), in-process (§15.1).

    Internal control flow, never seen outside this module: _handle_decision has
    already written the value and the synthetic success event, so the step is
    finished and _exec_step returns. A return value cannot say this -- that
    channel means "here is the failure message" -- and neither form may fall
    through to the attempt-failed event a decision must not write (§13.7).
    """


class _DecisionRerun(Exception):
    """Same, for form (b): re-run this step carrying the settled rulings.

    Carries every (request, answer) pair of this visit, not just the newest,
    for the reason §13.6 gives: a fresh subagent has no memory of the request
    it raised, and a re-run shown only the latest ruling can walk back into a
    fork already settled earlier in the cycle.
    """

    def __init__(self, context: list[tuple[str, str]]):
        self.context = context
        super().__init__("decision settled: re-run the step")


def decision_tables(events: list[dict]) -> tuple[dict, dict, dict, dict]:
    """(pending, answers, seq_high_water, llm_adjudications) rebuilt from a
    run's recorded events.

    pending: {(step id, cycle): decision event} for requests with no `answer`
    event yet -- the steps that must stop again without spending a CLI call.
    answers: {(step id, cycle): [answer events, adjudication order]} for every
    answered request, regardless of verdict. ALL of them, not the latest: a
    (b) re-run can raise a second fork in the same cycle, and a later re-run
    that carries only the newest ruling would let the agent walk back into
    the already-settled first fork -- the settled ones have to stay visible
    (§13.6). Form (a) normally never reaches the live path (its synthetic
    step event replays first), but if replay fell off earlier the step runs
    live anyway and is better off carrying the answers than not.

    seq_high_water: {(step id, cycle): highest seq already recorded}, so a (b)
    re-run that hits a *second* fork in the same cycle numbers it without
    colliding with the first.

    llm_adjudications: {(step id, cycle): how many requests this visit had
    adjudicated by an llm}, the §7 cap's ledger. There is no separate ledger
    by design: counting the recorded events makes the tally survive a process
    restart for free, on the same event-sourcing invariant the rest of resume
    rests on. Human answers are not counted -- the cap exists to stop an
    unattended llm loop, and a path where a person answers every time cannot
    run away (§7, §15.5). Requests that fell back to a human (escalation, cap
    reached, an adjudication that did not parse) record `decider="human"` and
    so drop out of the count here.

    Both keys are (step, cycle) rather than request_id because that is what
    `_exec_step` knows about itself. At most one request per step is ever
    pending (§13.1), so the collapse is lossless.
    """
    requests: dict[str, dict] = {}
    answered: set[str] = set()
    answers: dict[tuple[str, int], list[dict]] = {}
    seq_high_water: dict[tuple[str, int], int] = {}
    llm_adjudications: dict[tuple[str, int], int] = {}
    for event in events:
        kind = event.get("kind")
        if kind == "decision":
            key = (event["key"], event["cycle"])
            seq_high_water[key] = max(seq_high_water.get(key, 0), event["seq"])
            if event.get("decider") == model.DECIDER_LLM:
                llm_adjudications[key] = llm_adjudications.get(key, 0) + 1
            # A malformed payload is recorded too (so its seq is never reused
            # and the run's history shows it), but it can never be pending:
            # there is nothing well-formed to answer.
            if event.get("valid", True):
                requests[event["request_id"]] = event
        elif kind == "answer":
            answered.add(event["request_id"])
            # File order IS adjudication order: at most one request per step
            # is pending at a time (§13.1), so answers for the same (step,
            # cycle) can only have been appended one resume after another.
            answers.setdefault((event["key"], event["cycle"]), []).append(event)
    pending = {(e["key"], e["cycle"]): e
               for rid, e in requests.items() if rid not in answered}
    return pending, answers, seq_high_water, llm_adjudications


class Executor:
    def __init__(self, wf: model.Workflow, params: dict[str, str],
                 run_dir: str | Path, base_dir: str | Path = ".",
                 permission_mode: str | None = None,
                 replay_events: list[dict] | None = None,
                 run_claude=claude_cli.run_claude,
                 ask_llm=claude_cli.ask_llm,
                 diagnose=adp.diagnose,
                 adjudicate=adjudicate_mod.adjudicate,
                 model_runner: str = "cc",
                 inherit_model: str | None = None,
                 backend: str = "cc"):
        self.wf = wf
        self.base_dir = Path(base_dir)
        self.run_dir = Path(run_dir)
        self._check_base_dir()
        self.permission_mode = permission_mode
        self.state = RunState(self.run_dir)
        self.replay = ReplayCursor(replay_events or [])
        self._run_claude = run_claude
        self._ask_llm = ask_llm
        self._diagnose = diagnose
        self._adjudicate = adjudicate
        self._model_runner = model_runner
        self._inherit_model = inherit_model
        # Which execution facility this run is on ("cc" | "pi"), for the
        # checks a startup validator cannot reach: a replan continuation is
        # built mid-run, so its backend compatibility is only knowable from
        # here (design phase6-run-pi-design.md §10.2 point 1).
        #
        # NOT derived from `_model_runner`. That field's vocabulary is
        # "cc"/"llm" (which model_map table to resolve names through) and this
        # one's is "cc"/"pi" (which CLI runs the steps); they happen to be
        # correlated today, and folding one into the other would make any
        # future third combination a silent mis-dispatch rather than a new
        # argument.
        self.backend = backend

        self.vars: dict = {}
        self.step_count = 0
        self.cost_usd = 0.0
        self._attempt_seq: dict[str, int] = {}
        # Visits to each step node. Incremented at the top of _exec_step,
        # BEFORE the replay early-return, so a resumed run reconstructs the
        # same numbers (replay still calls _exec_step; it just returns early).
        # This is the `cycle` half of a decision request's identity (§13.1).
        self._cycle_seq: dict[str, int] = {}
        self._child_caps: list[tuple[int, int]] = []  # (count at start, cap)
        self._lock = threading.Lock()

        (self._pending_decisions, self._decision_answers,
         self._decision_seq,
         self._llm_adjudications) = decision_tables(replay_events or [])
        # Every request this run raised, for the report -- kept separately
        # from the exception because a `<parallel>` sibling failure outranks
        # a decision (§9) and the payloads must still be listed.
        self.decisions_raised: list[dict] = []
        # D9's warning face: stray line-anchored protocol tokens observed in
        # successful responses. Never affects classification; the report
        # prints these so a silent pass-through leaves a trace.
        self.protocol_warnings: list[str] = []

        self._resolve_params(params)
        self._rules_cache = self._load_rules()
        self._agents_cache = discover_agents(self.base_dir)
        try:  # a broken hand-edited model map must fail fast, not mid-run
            modelmap.load_map()
        except modelmap.ModelMapError as e:
            raise WorkflowFailure(str(e)) from e

    # ------------------------------------------------------------- setup ---
    def _check_base_dir(self):
        """Fail fast when the step subprocess cwd would be inside Claude's config tree.

        The claude CLI demands interactive write approval under its own config
        tree even with --permission-mode acceptEdits, so file-writing steps
        fail there in confusing ways (approval-denied errors, or claimed
        writes that never landed). Both `~/.claude` and, when
        `CLAUDE_CONFIG_DIR` is set, that env dir are protected (union, not
        env-only) — CC's config tree at runtime is the env dir when it is set,
        but a workflow started from a `~/.claude`-relative path is rejected
        too regardless (safety margin costs little, a false negative here
        reproduces the confusing approval failures this guard exists to
        avoid)."""
        base = self.base_dir.resolve()
        for protected in (d.resolve() for d in claude_config_dirs()):
            if base == protected or protected in base.parents:
                raise WorkflowFailure(
                    f"base dir {base} is inside Claude's config tree "
                    f"({protected}) — the claude CLI requires interactive "
                    "write approval there, so steps cannot write files; copy "
                    "the workflow to a normal project directory and run it "
                    "from there")

    def _resolve_params(self, params: dict[str, str]):
        declared = {p.name for p in self.wf.params}
        unknown = set(params) - declared
        if unknown:
            raise WorkflowFailure(f"unknown parameter(s): {', '.join(sorted(unknown))}")
        for p in self.wf.params:
            if p.name in params:
                self.vars[p.name] = params[p.name]
            elif p.default is not None:
                self.vars[p.name] = p.default
            elif p.required:
                raise WorkflowFailure(f"required parameter '{p.name}' not provided")

    def _load_rules(self) -> dict[str, str]:
        try:
            return stepio.load_rules(self.wf, self.base_dir)
        except stepio.StepIOError as e:
            raise WorkflowFailure(str(e)) from e

    # --------------------------------------------------------------- run ---
    def run(self) -> str:
        self.state.event("run", status="start", workflow=self.wf.name,
                         resume=self.replay.active)
        try:
            self._walk(self.wf.body)
        except DecisionRequested as e:
            ids = [r["request_id"] for r in e.requests]
            self._snapshot("awaiting-decision", decisions=ids)
            self.state.event("run", status="awaiting-decision", requests=ids)
            raise
        except WorkflowFailure as e:
            self._snapshot("failed", error=str(e))
            self.state.event("run", status="failed", error=str(e))
            raise
        self._snapshot("success")
        self.state.event("run", status="success")
        return "success"

    def _snapshot(self, status: str, error: str | None = None,
                  decisions: list[str] | None = None):
        self.state.snapshot(status=status, variables=self.vars,
                            step_count=self.step_count, cost_usd=self.cost_usd,
                            error=error, decisions=decisions)

    # ------------------------------------------------------------ walker ---
    def _walk(self, node):
        if isinstance(node, model.Seq):
            for child in node.children:
                self._walk(child)
        elif isinstance(node, model.Step):
            self._exec_step(node)
        elif isinstance(node, model.SetVar):
            self._exec_set(node)
        elif isinstance(node, model.If):
            self._exec_if(node)
        elif isinstance(node, model.While):
            self._exec_while(node)
        elif isinstance(node, model.Each):
            self._exec_each(node)
        elif isinstance(node, model.Parallel):
            self._exec_parallel(node)
        elif isinstance(node, model.Replan):
            self._exec_replan(node)
        else:
            raise WorkflowFailure(f"unknown node type {type(node).__name__}")

    # ----------------------------------------------------------- helpers ---
    def _interp(self, text: str, where: str) -> str:
        try:
            return interpolate(text, self.vars)
        except InterpError as e:
            raise WorkflowFailure(f"{where}: {e}") from e

    def _bump_limits(self, step_id: str):
        with self._lock:
            self.step_count += 1
            if self.step_count > self.wf.max:
                raise WorkflowFailure(
                    f"workflow max={self.wf.max} step executions exceeded at '{step_id}'")
            if self.wf.budget_usd is not None and self.cost_usd >= self.wf.budget_usd:
                raise WorkflowFailure(
                    f"budget-usd={self.wf.budget_usd} exhausted "
                    f"(spent {self.cost_usd:.4f}) before '{step_id}'")
            for start, cap in self._child_caps:
                if self.step_count - start > cap:
                    raise WorkflowFailure(
                        f"replan continuation step cap ({cap}) exceeded at '{step_id}'")

    def _add_cost(self, amount: float):
        with self._lock:
            self.cost_usd += amount

    def _build_prompt(self, step: model.Step, fix: str | None = None,
                      decision: tuple[str, str] | None = None
                      ) -> tuple[str, str]:
        """(system_text, user_text) — run-cc puts the constraint layers
        (role/mode/rules) in the system channel via --append-system-prompt."""
        try:
            return stepio.build_step_prompt_parts(
                self.wf, step, self.vars, self.base_dir,
                fix=fix, rules_cache=self._rules_cache,
                agents_cache=self._agents_cache, decision=decision)
        except stepio.StepIOError as e:
            raise WorkflowFailure(str(e)) from e

    # -------------------------------------------------------------- step ---
    def _exec_step(self, step: model.Step, replay_pool: dict | None = None):
        # Before everything, including the replay return: a resumed run must
        # arrive at the same cycle number it did originally (§13.1).
        with self._lock:
            cycle = self._cycle_seq[step.id] = self._cycle_seq.get(step.id, 0) + 1
        self._bump_limits(step.id)

        # (1) replay hit — including the synthetic success `resume --answer`
        # writes for a form-(a) decision, which is consumed right here and is
        # the whole reason (a) costs nothing (§13.5).
        if replay_pool is not None:
            replayed = replay_pool.pop(step.id, None)
        else:
            replayed = self.replay.take("step", step.id)
        if replayed is not None:
            with self._lock:
                if step.output:
                    self.vars[step.output] = replayed.get("output_value")
            self._add_cost(float(replayed.get("cost_usd") or 0.0))
            return

        # (2) an unanswered request for THIS visit — stop again without
        # spending a CLI call, and record nothing (§13.5). Re-raising is a
        # read-only operation: appending another `decision` event here would
        # make a bare `wfrun resume` non-idempotent and let the llm-adjudication
        # cap advance on nothing but a re-print.
        pending = self._pending_decisions.get((step.id, cycle))
        if pending is not None:
            with self._lock:
                self.decisions_raised.append(pending)
            raise DecisionRequested([pending])

        # (3) answered — form (b): re-run carrying every settled request and
        # answer of this cycle, not just the newest (§13.6).
        answer_events = self._decision_answers.get((step.id, cycle))
        decision_ctx = self._decision_context(answer_events) if answer_events else None
        system, prompt = self._build_prompt(step, decision=decision_ctx)
        dispatch_model, dispatch_tools = stepio.dispatch_for(step, self._agents_cache)
        dispatch_model = self._map_model(dispatch_model, step.id)
        # Least privilege: --permission-mode (e.g. acceptEdits) reaches only
        # steps whose tools can write; read-only steps run without it.
        permission = (self.permission_mode
                      if model.tools_can_write(dispatch_tools) else None)
        debug_used = False
        attempt = 0
        # Attempts spent re-running after an in-process ruling. Subtracted from
        # the retry test below so a settled fork does not eat the budget meant
        # for flaky failures -- the same treatment debug's one granted attempt
        # gets, and for the same reason: neither is a failed try (§15.1).
        decision_reruns = 0
        while True:
            attempt += 1
            with self._lock:
                self._attempt_seq[step.id] = self._attempt_seq.get(step.id, 0) + 1
                seq = self._attempt_seq[step.id]
            attempt_dir = self.run_dir / "steps" / f"{step.id}_{seq:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            (attempt_dir / "system.md").write_text(system, encoding="utf-8")
            (attempt_dir / "prompt.md").write_text(prompt, encoding="utf-8")

            res = self._run_claude(
                prompt, system_prompt=system, model=dispatch_model,
                effort=step.effort,
                tools=dispatch_tools, schema=step.schema, timeout=step.timeout,
                cwd=str(self.base_dir), permission_mode=permission)
            self._add_cost(res.cost_usd)
            # Before any branch: a lossy decode is worth a trace whether the
            # step passed, failed, or got reclassified below.
            self._warn_replacement_chars(step.id, res)
            if res.ok:
                # Mode/rules refusal (_meta protocol). run_claude also flags
                # this; checking here keeps the pipeline safe with any runner.
                blocked_line = modes.blocked_line(res.text)
                if blocked_line is not None:
                    res.ok = False
                    res.error_class = "refusal"
                    res.error = blocked_line[:500]
            if res.ok:
                claimed, _preamble = decision_mod.claim_decision_body(
                    modes.strip_mode_line(res.text))
                if claimed is not None:
                    # Same belt-and-braces as the refusal check above: both
                    # backends classify this already, but the executor must
                    # not depend on a particular runner having done so. The
                    # claim covers the D9 preamble shape too.
                    res.ok = False
                    res.error_class = "decision"
                    res.error = claimed.strip()
            if res.ok and step.expect_file:
                missing = self._missing_expected(step)
                if missing:
                    res.ok = False
                    # reliability-spec.md §3.1: expect-file failures are a
                    # CLI/model hiccup (the file didn't materialize), not a
                    # step-level refusal or permission problem -- retryable.
                    res.error_class = "behavioral"
                    res.error = ("expect-file: not produced: "
                                 + ", ".join(missing))

            (attempt_dir / "result.json").write_text(
                json.dumps(res.raw if res.raw is not None else
                           {"ok": res.ok, "error": res.error, "text": res.text},
                           ensure_ascii=False, indent=2), encoding="utf-8")
            if res.stderr:
                (attempt_dir / "stderr.log").write_text(res.stderr, encoding="utf-8")

            if res.ok:
                self._warn_stray_prefixes(step, res)
                self._finish_step(step, res, attempt)
                return

            if res.error_class == "decision":
                # Ahead of the retry/debug/ignore ladder on purpose (§13.7): a
                # well-formed request is not a failure, so `on-error="ignore"`
                # must not absorb it — that would silently drop the fork this
                # whole channel exists to surface. _handle_decision raises in
                # that case; a malformed payload IS a failure and comes back
                # as a message that falls through to the ladder below — where
                # the `decision` class keeps retry and debug out AND the
                # `ignore` branch refuses to absorb it (§19.2), so a malformed
                # fork fails the run whatever `on-error` says.
                #
                # An llm decider settles it in-process instead of stopping
                # (§15.1); both continuation forms leave through an exception
                # so neither reaches the attempt-failed event below.
                try:
                    res.error = self._handle_decision(step, res, cycle)
                except _DecisionSettledA:
                    return  # value and synthetic success already recorded
                except _DecisionRerun as settled:
                    decision_reruns += 1
                    # Kept in the local too, not just handed to this rebuild:
                    # every LATER rebuild of this visit (the debug-granted
                    # attempt below) reads decision_ctx, and a stale one would
                    # drop the very rulings the step was re-run to apply
                    # (§13.6, §19.1).
                    decision_ctx = settled.context
                    system, prompt = self._build_prompt(
                        step, decision=decision_ctx)
                    continue

            self.state.event("step", key=step.id, status="attempt-failed",
                             attempt=seq, error=(res.error or "")[:1000],
                             error_class=res.error_class,
                             cost_usd=res.cost_usd)
            if (attempt - decision_reruns <= step.retry
                    and claude_cli.is_retryable(res.error_class)):
                # deterministic retry, identical prompt — skipped for
                # error_class in NON_RETRYABLE_CLASSES (env/guardrail/
                # refusal/denied), where the same prompt would just repeat
                # the same outcome (reliability-spec.md §3.2)
                continue

            if (step.on_error == "debug" and not debug_used
                    and claude_cli.is_debuggable(res.error_class)):
                diagnosis = self._diagnose(step, f"{system}\n\n{prompt}", res,
                                           cwd=str(self.base_dir))
                self._add_cost(diagnosis.cost_usd)
                self.state.event("debug", key=step.id, action=diagnosis.action,
                                 reason=diagnosis.reason[:1000],
                                 cost_usd=diagnosis.cost_usd)
                if diagnosis.action == "RETRY":
                    debug_used = True
                    # decision_ctx is carried through: a (b) re-run that then
                    # fails for some other reason must not lose the answer it
                    # was re-run to apply.
                    system, prompt = self._build_prompt(
                        step, fix=diagnosis.fix_instruction,
                        decision=decision_ctx)
                    continue  # exactly one debug-granted attempt

            # `decision` is the one class `ignore` may not absorb (§19.2): a
            # malformed payload is a fork nobody could answer, and continuing
            # past it drops the fork exactly as silently as picking a branch by
            # hand -- the failure this whole channel exists to prevent. Every
            # other class is ignored as before.
            if step.on_error == "ignore" and res.error_class != "decision":
                self.state.event("step", key=step.id, status="failed-ignored",
                                 error=(res.error or "")[:1000])
                self._snapshot("running")
                return
            raise WorkflowFailure(f"step '{step.id}' failed: {res.error}")

    def _map_model(self, name: str | None, where: str) -> str | None:
        """Canonical name -> the model this runner actually dispatches
        (model_map.json, runner table selected at construction -- "cc" for
        run-cc, "llm" for run-pi, design phase6-run-pi-design.md §1). This
        one path also carries ask='s model resolution (_eval_cond) and
        replan's (_exec_replan), so making it variable here covers all
        three call sites. Mappings are recorded for audit.

        When `name` is None -- no step-level model= and no role-frontmatter
        model -- the backend CLI would otherwise pick for itself, silently
        and per-machine, and not necessarily from anything it calls a
        configured default (design phase6 review point 2, 2026-07-30 E2E: an
        inline-role step with no model landed on whichever provider happened
        to be enabled in the local pi config, bypassing even pi's own
        `defaultModel`, and not this session's model). `--inherit-model`
        supplies the session's own model for exactly this case, symmetric
        across both backends. It is a concrete model identifier already, not
        a canonical difficulty class, so it bypasses modelmap.resolve
        entirely and is used as-is."""
        if name is None:
            if self._inherit_model:
                self.state.event("model-map", key=where, canonical=None,
                                 resolved=self._inherit_model, source="inherit")
            return self._inherit_model
        resolved = modelmap.resolve(name, self._model_runner, allow_legacy=True)
        if resolved != name:
            self.state.event("model-map", key=where,
                             canonical=name, resolved=resolved)
        return resolved

    def model_inherit_warnings(self) -> list[str]:
        """One combined advisory line (not one per step) when
        --inherit-model was not given and at least one step/replan would be
        left to the backend CLI's own model choice (design phase6 review
        point 2). Called by the CLI layer once, right after construction and
        before .run(), so it surfaces at run start rather than being
        discovered only from a step's own result.json; empty when
        --inherit-model was given (nothing falls through in that case) or
        every step already resolves a model of its own."""
        if self._inherit_model:
            return []
        missing = [n.id for n in self.wf.iter_steps()
                  if not stepio.dispatch_for(n, self._agents_cache)[0]]
        if not missing:
            return []
        return ["no --inherit-model given; step(s) with no model= (no step "
                "attribute, no role-frontmatter default) will run on "
                "whatever model the backend CLI picks for itself -- not "
                "necessarily one it calls a default (measured under pi: an "
                "enabled provider, chosen over pi's own defaultModel) -- "
                "rather than this session's: " + ", ".join(missing)]

    def _expected_paths(self, step: model.Step) -> list[tuple[str, Path]]:
        """(as declared, resolved absolute) per expect-file entry.

        Comma-separated and {var}-interpolated; relative entries resolve
        against the XML dir, which is the step subprocess's cwd. The absolute
        half is what a decision event records, so that a later `resume` --
        whose own base_dir defaults to the caller's cwd, not the XML dir --
        re-checks the same files the original run meant (§13.2).
        """
        raw = self._interp(step.expect_file, f"step '{step.id}' expect-file")
        pairs = []
        for part in (p.strip() for p in raw.split(",")):
            if not part:
                continue
            path = Path(part)
            if not path.is_absolute():
                path = self.base_dir / path
            pairs.append((part, path))
        return pairs

    def _missing_expected(self, step: model.Step) -> list[str]:
        """expect-file paths (comma-separated, {var}-interpolated, relative to
        the XML dir = subprocess cwd) that do not exist after the response."""
        return [declared for declared, path in self._expected_paths(step)
                if not path.is_file()]

    def _warn_stray_prefixes(self, step: model.Step, res) -> None:
        """One warning when a success carries a line-anchored protocol token
        it did not open with (D9 rulings 4-2 residue and 4-4).

        Observability only, never reclassification: ERROR: and [BLOCKED: have
        no parseable structure to gate a mid-body match on -- relaxing them
        would turn real successes into false failures -- and a DECISION: line
        whose tail does not parse is ambiguous evidence. The success stands;
        the events stream and the run report get a trace instead of silence.
        """
        strays = decision_mod.stray_protocol_lines(
            modes.strip_mode_line(res.text or ""))
        if not strays:
            return
        detail = ", ".join(f"'{prefix}' at line {number}"
                           for number, prefix in strays[:5])
        warning = (f"step '{step.id}': a successful response carries a "
                   f"line-anchored protocol token it did not open with "
                   f"({detail}); prefixes bind at the start of the final "
                   f"response, so it was NOT reclassified -- read the "
                   f"response if this step should have stopped")
        self.state.event("warning", key=step.id, warning=warning,
                         lines=[[number, prefix] for number, prefix in strays])
        with self._lock:
            self.protocol_warnings.append(warning)

    def _warn_replacement_chars(self, key: str, res) -> None:
        """One warning when a response carries U+FFFD (§3.1 observability).

        The launchers decode child output as UTF-8 with errors="replace", so a
        byte the child emitted outside UTF-8 no longer kills the run -- it
        becomes a replacement character. That trade turns a loud crash into a
        quiet substitution, which is exactly the silent-failure shape this
        runner exists to remove, so the substitution gets a trace.

        Observability only, never reclassification: the JSON envelope is ASCII,
        so every field the classifier reads survives replacement intact and the
        step's verdict is unaffected. A model that types U+FFFD on purpose
        trips this too; a false warning costs a line, a missed corruption costs
        a wrong answer nobody questions.
        """
        for surface, text in (("response", res.text), ("stderr", res.stderr)):
            if not text or "�" not in text:
                continue
            count = text.count("�")
            warning = (f"step '{key}': {surface} carries {count} replacement "
                       f"character(s) (U+FFFD) -- the child emitted bytes that "
                       f"are not valid UTF-8 and they were substituted, not "
                       f"decoded; classification was unaffected but the text "
                       f"is lossy at those positions")
            self.state.event("warning", key=key, warning=warning,
                             surface=surface, replacement_chars=count)
            with self._lock:
                self.protocol_warnings.append(warning)

    # ---------------------------------------------------------- decision ---
    def _handle_decision(self, step: model.Step, res, cycle: int) -> str:
        """Persist a `DECISION:` response and route it (§1, §13.2, §15.1).

        Four ways out:
        - a malformed payload is a real failure: returns the message the caller
          feeds to the ordinary on-error ladder (no adjudicator is ever called
          for it -- §15.4);
        - `decider="human"`, an escalation, a cap already spent, or an
          adjudication that did not come back usable: raises DecisionRequested
          and the run stops for a person (§5 fail-closed);
        - an llm ruling that continues as form (a): records the value and the
          synthetic success, then raises _DecisionSettledA;
        - one that continues as form (b): raises _DecisionRerun carrying the
          rulings for the re-run's prompt.
        """
        full = modes.strip_mode_line(res.text).strip()
        claimed, preamble = decision_mod.claim_decision_body(full)
        # File the anchored slice: the request file is the numbering authority
        # the answer selects against (§1), so it must be the parseable payload
        # and nothing else; the full response stays in the attempt record. A
        # first-token body passes through whole, malformed or not, and a body
        # this method somehow got without a claim falls through whole to the
        # malformed path below rather than being guessed at (D9).
        body = (claimed if claimed is not None else full).strip()
        with self._lock:
            key = (step.id, cycle)
            seq = self._decision_seq[key] = self._decision_seq.get(key, 0) + 1
        rid = decision_mod.request_id(step.id, cycle, seq)
        request_file = decision_mod.request_path(self.run_dir, rid)
        request_file.parent.mkdir(parents=True, exist_ok=True)
        request_file.write_text(body, encoding="utf-8")

        payload, errors = decision_mod.parse_payload(body)
        if errors:
            # The shape marker rides on the event so the misuse rate can be
            # recounted from a run's log later (§11, §18.3). Nothing branches
            # on it -- the run still fails, as a malformed payload always has.
            misused = decision_mod.looks_like_completion_report(body)
            self.state.event("decision", key=step.id, request_id=rid,
                             cycle=cycle, seq=seq, valid=False,
                             request=str(request_file), errors=errors[:10],
                             completion_report_shape=misused,
                             cost_usd=res.cost_usd)
            if misused:
                return (f"step '{step.id}' wrapped a completion report in the "
                        "decision channel: the payload declares `work-state:` "
                        "but names no fork (no `fork:` / `options:` / "
                        "`recommendation:`). `DECISION:` is for a fork the step "
                        "may not settle alone; a step that finished reports "
                        "normally instead. Request: " + str(request_file))
            return (f"step '{step.id}' raised a decision request whose payload "
                    f"is malformed ({len(errors)} field problem(s)): "
                    f"{'; '.join(errors)[:400]}. It cannot be answered as-is; "
                    f"read it and decide by hand: {request_file}")

        b_reason, expect_files = self._decision_b_reason(step, payload)
        decider, decider_model = model.resolve_decider(self.wf, step)

        ruling = None
        extras: dict = {}
        if preamble.strip():
            # Audited, not filed: the deviation is worth a report line, but
            # the prose itself belongs to the attempt record, not the ledger.
            extras["preamble_lines"] = len(preamble.splitlines())
        if decider == model.DECIDER_LLM:
            with self._lock:
                spent = self._llm_adjudications.get((step.id, cycle), 0)
            if spent >= model.DECISION_LLM_CAP:
                # Falls back to a human rather than ruling again: the cap is
                # there to stop an unattended loop, and the honest report is
                # that this visit has already used its rulings (§7).
                extras["cap_reached"] = True
            else:
                ruling = self._adjudicate_request(step, rid, body, payload,
                                                  decider_model, extras)

        settled = ruling is not None and ruling.verdict == "settled"
        record = self.state.event(
            "decision", key=step.id, request_id=rid, cycle=cycle, seq=seq,
            valid=True, request=str(request_file),
            answer_path=str(decision_mod.answer_path(self.run_dir, rid)),
            # Who actually settled it, not who was declared: an escalation, a
            # spent cap and a failed adjudication all hand the fork to a human,
            # and the §7 tally counts this field (decision_tables).
            decider=(model.DECIDER_LLM if settled else model.DECIDER_HUMAN),
            work_state=payload.work_state,
            option_count=len(payload.options),
            recommendation=payload.recommendation, output=payload.output,
            a_eligible=b_reason is None, b_reason=b_reason,
            expect_files=expect_files, cost_usd=res.cost_usd, **extras)

        if not settled:
            with self._lock:
                self.decisions_raised.append(record)
            raise DecisionRequested([record])
        self._settle_in_process(step, rid, cycle, payload, ruling,
                                b_reason, res)

    def _adjudicate_request(self, step: model.Step, rid: str, body: str,
                            payload, decider_model: str | None,
                            extras: dict):
        """Call the llm decider for one request and record what it cost.

        Never raises on an unusable ruling: it returns one whose verdict says
        so, and the caller stops the run for a human. `extras` collects the
        fields that explain the fallback in the decision event -- the person
        reading the report has to know why the fork reached them (§15.2).
        """
        ruling = self._adjudicate(
            step.id, body, len(payload.options),
            model=decider_model, cwd=str(self.base_dir), timeout=step.timeout)
        self._add_cost(ruling.cost_usd)
        extras["adjudication_cost_usd"] = ruling.cost_usd
        extras["decider_model"] = decider_model
        if ruling.verdict == "escalate":
            extras["escalated"] = True
            extras["adjudication_note"] = ruling.reason[:1000]
        elif ruling.verdict != "settled":
            extras["adjudication_error"] = ruling.reason[:1000]
            if ruling.raw is not None:
                # Kept for audit, but NOT at the answer path: that one stays
                # empty so the human has the obvious place to write (§15.2).
                attempts = sorted(decision_mod.decisions_dir(self.run_dir)
                                  .glob(f"{rid}_llm-attempt*.md"))
                rejected = (decision_mod.decisions_dir(self.run_dir)
                            / f"{rid}_llm-attempt{len(attempts) + 1:02d}.md")
                rejected.write_text(ruling.raw, encoding="utf-8")
                extras["adjudication_rejected"] = str(rejected)
        return ruling

    def _settle_in_process(self, step: model.Step, rid: str, cycle: int,
                           payload, ruling, b_reason: str | None, res):
        """Apply a settled llm ruling without stopping the run (§15.1).

        Writes the same artifacts the human path writes -- the answer file at
        the same path, an `answer` event, and for form (a) the synthetic
        success event `resume --answer` would have appended -- so a later
        resume replays this run exactly as it replays a human-answered one.
        `missing-file-at-resume` cannot arise here: nothing happens between the
        stop and the ruling for a file to disappear in (§13.2).
        """
        answer_file = decision_mod.answer_path(self.run_dir, rid)
        answer_file.parent.mkdir(parents=True, exist_ok=True)
        answer_file.write_text(ruling.answer_text, encoding="utf-8")
        answer, errors = decision_mod.parse_answer(ruling.answer_text,
                                                   len(payload.options))
        if errors:  # adjudicate() already gated this; belt and braces
            raise WorkflowFailure(
                f"decision {rid}: the recorded ruling does not parse: "
                f"{'; '.join(errors)}")
        if not b_reason:
            b_reason = decision_mod.answer_b_reason(answer,
                                                    payload.recommendation)
        verdict = "b" if b_reason else "a"

        event = self.state.event(
            "answer", key=step.id, cycle=cycle, request_id=rid,
            request=str(decision_mod.request_path(self.run_dir, rid)),
            answer_path=str(answer_file), decider=model.DECIDER_LLM,
            option=answer.option, verdict=verdict, b_reason=b_reason,
            missing=[])
        key = (step.id, cycle)
        with self._lock:
            self._llm_adjudications[key] = self._llm_adjudications.get(key, 0) + 1
            # Feeds the re-run prompt. The construction-time table only holds
            # replayed answers, so without this a second fork in the same visit
            # would be re-run without the first ruling in front of it (§13.6).
            self._decision_answers.setdefault(key, []).append(event)

        if verdict == "b":
            raise _DecisionRerun(self._decision_context(
                self._decision_answers[key]))

        # Form (a): the payload's own output becomes the step's value, and the
        # success event is written in the shape `resume --answer` writes it, so
        # a resumed run consumes it as an ordinary replay hit (§13.4 step 3).
        if step.output:
            with self._lock:
                self.vars[step.output] = payload.output
        self.state.event("step", key=step.id, status="success",
                         via="decision-a", request_id=rid, attempts=1,
                         cost_usd=res.cost_usd, output_value=payload.output)
        self._snapshot("running")
        raise _DecisionSettledA

    def _decision_b_reason(self, step: model.Step, payload
                           ) -> tuple[str | None, list[str]]:
        """(why this must take form (b), recorded absolute expect-file paths).

        None means form (a) is still on the table — the answer selecting a
        listed option is the last condition, and only `resume` knows that
        (§6, §13.2). Evaluated HERE, at the moment the step stopped, because
        "did it finish writing before asking" is a fact about that instant;
        measuring it later would fold in whatever later steps wrote.
        """
        if not payload.work_complete:
            return decision_mod.B_REASON_WORK_STATE_STOPPED, []
        # Checked before the artifact conditions: without a value to adopt,
        # form (a) is impossible however well the expect-file check goes.
        # Only when the step HAS somewhere to put it, though -- a step with no
        # `output=` never reads the payload's value at all (form (a) sets a
        # variable only when one is declared), so demoting it for a missing
        # value would re-run a step to protect nothing (§18.5, A-1b).
        if step.output and payload.output is None:
            return decision_mod.B_REASON_NO_OUTPUT, []
        # A value-typed output is adopted verbatim into the variable, so form
        # (a) would hand downstream whatever the step wrote BEFORE the ruling
        # existed -- measured never to be the ruling's value (§18.2). Only
        # file-typed steps can take (a); everything else re-runs and produces
        # the value itself.
        if step.output and step.output_type != "file":
            return decision_mod.B_REASON_VALUE_OUTPUT, []
        if step.schema:
            return decision_mod.B_REASON_SCHEMA_STEP, []
        if not step.expect_file:
            return decision_mod.B_REASON_NO_EXPECT_FILE, []
        pairs = self._expected_paths(step)
        absolute = [str(path) for _, path in pairs]
        if not pairs:
            return decision_mod.B_REASON_NO_EXPECT_FILE, []
        if any(not path.is_file() for _, path in pairs):
            return decision_mod.B_REASON_MISSING_FILE, absolute
        return None, absolute

    def _decision_context(self, answer_events: list[dict]
                          ) -> list[tuple[str, str]]:
        """[(request body, answer body), ...] for a form-(b) re-run's prompt,
        one pair per settled decision of this cycle, in adjudication order
        (§13.6).

        A fresh subagent has no memory of the requests it raised, so the
        re-run has to carry every settled pair -- dropping the earlier ones
        would let it walk back into an already-settled fork. Unreadable files
        fail the run rather than quietly re-running without a ruling.
        """
        pairs = []
        for event in answer_events:
            try:
                request = Path(event["request"]).read_text(encoding="utf-8")
                reply = Path(event["answer_path"]).read_text(encoding="utf-8")
            except OSError as e:
                raise WorkflowFailure(
                    f"decision {event['request_id']}: cannot read the "
                    f"recorded request/answer needed to re-run the step: {e}") from e
            pairs.append((request, reply))
        return pairs

    def _finish_step(self, step: model.Step, res, attempts: int):
        output_value = None
        # None on the file path and on structured results, where rule 6's line
        # never applied; a shape token otherwise, so a run's own events can be
        # counted afterwards for how often the line was actually written
        # (xml-wf-decision-request.md §11, §18.6).
        value_line = None
        if step.output:
            if step.output_type == "file":
                path = self.run_dir / "outputs" / f"{step.id}.md"
                path.write_text(modes.strip_mode_line(res.text), encoding="utf-8")
                output_value = str(path)
            else:
                output_value, value_line = stepio.unwrap_value_marked(
                    res.structured, res.text)
            with self._lock:
                self.vars[step.output] = output_value
        self.state.event("step", key=step.id, status="success",
                         attempts=attempts, cost_usd=res.cost_usd,
                         output_var=step.output, output_value=output_value,
                         value_line=value_line)
        self._snapshot("running")

    # --------------------------------------------------------------- set ---
    def _exec_set(self, node: model.SetVar):
        if node.value is not None:
            value = self._interp(node.value, f"set '{node.var}'")
        else:
            try:
                value = safe_eval(node.expr, self.vars)
            except InterpError as e:
                raise WorkflowFailure(f"set '{node.var}': {e}") from e
        self.vars[node.var] = value
        self.state.event("set", var=node.var, value=str(value)[:1000])

    # -------------------------------------------------------- conditions ---
    def _eval_cond(self, test: str | None, ask: str | None, ask_model: str,
                   where: str) -> bool:
        if test is not None:
            try:
                value = bool(safe_eval(test, self.vars))
            except InterpError as e:
                raise WorkflowFailure(f"{where} test: {e}") from e
            self.state.event("test", where=where, expr=test, value=value)
            return value

        question = self._interp(ask, where)
        replayed = self.replay.take("cond", question)
        if replayed is not None:
            return bool(replayed["value"])
        answer, reason, cost = self._ask_llm(
            question, model=self._map_model(ask_model, where),
            cwd=str(self.base_dir))
        self._add_cost(cost)
        if answer is None:
            raise WorkflowFailure(f"{where} ask judgment failed: {reason}")
        self.state.event("cond", key=question, status="success",
                         value=answer, reason=reason[:1000], cost_usd=cost)
        return answer

    # ----------------------------------------------------------- control ---
    def _exec_if(self, node: model.If):
        if self._eval_cond(node.test, node.ask, node.ask_model, "if"):
            self._walk(node.then)
        elif node.else_ is not None:
            self._walk(node.else_)

    def _exec_while(self, node: model.While):
        for _ in range(node.max):
            if not self._eval_cond(node.test, node.ask, node.ask_model, "while"):
                return
            self._walk(node.body)
        self.state.event("while-max-reached", max=node.max)

    def _exec_each(self, node: model.Each):
        items = self._resolve_items(node)
        index_var = f"{node.as_}_index"
        shadowed = {k: self.vars[k] for k in (node.as_, index_var) if k in self.vars}
        try:
            for idx, item in enumerate(items):
                self.vars[node.as_] = item
                self.vars[index_var] = idx
                self._walk(node.body)
        finally:
            for k in (node.as_, index_var):
                self.vars.pop(k, None)
            self.vars.update(shadowed)

    def _resolve_items(self, node: model.Each) -> list:
        if node.range_ is not None:
            raw = self._interp(node.range_, "each range")
            try:
                return list(range(int(raw)))
            except ValueError:
                raise WorkflowFailure(f"each range: '{raw}' is not an integer")
        if node.glob is not None:
            pattern = self._interp(node.glob, "each glob")
            root = "" if Path(pattern).is_absolute() else str(self.base_dir) + "/"
            return sorted(_glob(root + pattern))
        raw = self._interp(node.items, "each items")
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            raise WorkflowFailure(f"each items: not a JSON array: {raw[:200]!r}")
        if not isinstance(items, list):
            raise WorkflowFailure("each items: JSON value is not an array")
        return items

    # ------------------------------------------------------------ replan ---
    def _exec_replan(self, node: model.Replan):
        self._bump_limits(node.id)

        replayed = self.replay.take("replan", node.id)
        if replayed is not None:
            self._add_cost(float(replayed.get("cost_usd") or 0.0))
            # Read the recorded text and parse it through the live branch's
            # path, NOT parse_file: the continuation is stored under
            # `<run dir>/replans/`, so parsing it by path would root the child
            # there and resolve a step's `schema="@rel/path.json"` against the
            # run dir instead of the XML dir the original run used -- a parse
            # error, or silently a different file. A resume must reconstruct
            # the same execution, so both branches resolve `@` against
            # self.base_dir (reliability-spec.md §14.1).
            xml_text = (self.run_dir / replayed["xml"]).read_text(
                encoding="utf-8")
            child = parser.parse_string(xml_text, base_dir=self.base_dir)
            self._run_child(node, child)
            return

        dispatch_model, _ = stepio.dispatch_for(node, self._agents_cache)
        dispatch_model = self._map_model(dispatch_model, node.id)
        fix = None
        attempt = 0
        while True:
            attempt += 1
            with self._lock:
                self._attempt_seq[node.id] = self._attempt_seq.get(node.id, 0) + 1
                seq = self._attempt_seq[node.id]
            attempt_dir = self.run_dir / "steps" / f"{node.id}_{seq:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            try:
                system, prompt = stepio.build_replan_prompt_parts(
                    node, self.vars, self._agents_cache, fix=fix,
                    constraint=self._replan_constraint())
            except stepio.StepIOError as e:
                raise WorkflowFailure(str(e)) from e
            (attempt_dir / "system.md").write_text(system, encoding="utf-8")
            (attempt_dir / "prompt.md").write_text(prompt, encoding="utf-8")

            res = self._run_claude(
                prompt, system_prompt=system, model=dispatch_model,
                effort=node.effort,
                tools="Read,Glob,Grep", schema=None, timeout=node.timeout,
                cwd=str(self.base_dir), permission_mode=None)  # read-only builder
            self._add_cost(res.cost_usd)
            self._warn_replacement_chars(node.id, res)
            (attempt_dir / "result.json").write_text(
                json.dumps(res.raw if res.raw is not None else
                           {"ok": res.ok, "error": res.error, "text": res.text},
                           ensure_ascii=False, indent=2), encoding="utf-8")

            errors, child, xml_text = self._validate_continuation(node, res)
            if not errors:
                xml_rel = f"replans/{node.id}_{seq:02d}.xml"
                (self.run_dir / "replans").mkdir(exist_ok=True)
                (self.run_dir / xml_rel).write_text(xml_text, encoding="utf-8")
                self.state.event("replan", key=node.id, status="success",
                                 xml=xml_rel, attempts=attempt, cost_usd=res.cost_usd)
                self._snapshot("running")
                self._run_child(node, child)
                return

            joined = "; ".join(errors)
            self.state.event("replan", key=node.id, status="attempt-failed",
                             attempt=seq, error=joined[:1000], cost_usd=res.cost_usd)
            if attempt <= node.retry:
                fix = "- " + "\n- ".join(errors)
                continue
            if node.on_error == "ignore":
                self.state.event("replan", key=node.id, status="failed-ignored",
                                 error=joined[:1000])
                self._snapshot("running")
                return
            raise WorkflowFailure(f"replan '{node.id}' failed: {joined}")

    def _replan_constraint(self) -> str | None:
        """The extra contract bullet the builder prompt gets on this backend.

        None on cc (and on the run-llm path, which never comes through here),
        so the prompt those see is byte-identical to the one they saw before
        this parameter existed (design §10.2 point 3)."""
        return stepio.REPLAN_PI_CONSTRAINT if self.backend == "pi" else None

    def _validate_continuation(self, node: model.Replan, res):
        """Parse + lint a generated continuation. Returns (errors, child, xml).

        Linted AS the live backend, and on pi also checked for the two
        attributes the startup fail-fast refuses (design §10.2 point 2).
        `pi_cli.pi_compat_errors` only ever sees the statically-declared
        steps, and `lint()` defaulted to backend="cc" here, so before this a
        continuation could carry `schema=` / `on-error="debug"` / a model name
        pi cannot resolve all the way to the point where the step launched and
        died. Everything found lands in the same `errors` list, which
        `_exec_replan` feeds back to the builder as `fix=`: the generator that
        wrote the violation is the one asked to correct it, and the run stops
        before any of the continuation runs.
        """
        if not res.ok:
            return [res.error or "claude call failed"], None, ""
        xml_text = stepio.strip_fences(res.text)
        try:
            child = parser.parse_string(xml_text, base_dir=self.base_dir)
        except parser.ParseError as e:
            return [str(e)], None, xml_text
        findings = lint_mod.lint(child, base_dir=self.base_dir, check_roles=True,
                                 as_child=True, defined_vars=set(self.vars),
                                 backend=self.backend)
        errors = [str(f) for f in findings if f.level == "error"]
        if self.backend == "pi":
            from . import pi_cli  # deferred: needs the pi CLI only on pi
            errors.extend(pi_cli.pi_continuation_errors(child))
        if child.max > node.max_steps:
            errors.append(f"workflow max={child.max} exceeds the allowed "
                          f"max-steps={node.max_steps}")
        return errors, child, xml_text

    def _warn_continuation_tool_widening(self, node: model.Replan,
                                         child: model.Workflow) -> None:
        """One warning per continuation step whose tools= carries an argument
        specifier pi cannot enforce (design §10.2, disposition (e)).

        `pi_tool_widening_notes` scans only the statically-declared steps and
        `run_pi` discards `_convert_tools`' per-call warnings on the grounds
        that the run-start advisory already covered them -- which leaves a
        continuation's `Bash(git:*)` widened to the whole `bash` tool with no
        notice anywhere. Same surface as the protocol warnings: an event, and
        a line in the run report.
        """
        if self.backend != "pi":
            return
        from . import pi_cli  # deferred: needs the pi CLI only on pi
        for note in pi_cli.pi_tool_widening_notes(child, self._agents_cache):
            warning = f"replan '{node.id}' continuation: {note}"
            self.state.event("warning", key=node.id, warning=warning)
            with self._lock:
                self.protocol_warnings.append(warning)

    def _run_child(self, node: model.Replan, child: model.Workflow):
        self._warn_continuation_tool_widening(node, child)
        saved_rules = self._rules_cache
        try:
            child_rules = stepio.load_rules(child, self.base_dir)
        except stepio.StepIOError as e:
            raise WorkflowFailure(str(e)) from e
        self._rules_cache = {**saved_rules, **child_rules}
        self._child_caps.append((self.step_count, min(child.max, node.max_steps)))
        try:
            self._walk(child.body)
        finally:
            self._child_caps.pop()
            self._rules_cache = saved_rules
        missing = [v for v in node.outputs if v not in self.vars]
        if missing:
            raise WorkflowFailure(
                f"replan '{node.id}': continuation did not define declared "
                f"output variable(s): {', '.join(missing)}")

    def _exec_parallel(self, node: model.Parallel):
        pool = self.replay.take_group("step", {s.id for s in node.children})
        errors = []
        decisions: list[dict] = []
        with ThreadPoolExecutor(max_workers=node.max_workers) as pool_exec:
            futures = {pool_exec.submit(self._exec_step, s, pool): s
                       for s in node.children}
            for future in as_completed(futures):
                try:
                    future.result()
                except WorkflowFailure as e:
                    errors.append(str(e))
                except DecisionRequested as e:
                    # Collected, not propagated mid-loop: running siblings are
                    # left to finish and every request gets listed (§9). A
                    # sibling that already stopped for an unanswered request
                    # arrives here too, at no cost.
                    decisions.extend(e.requests)
        if errors:
            # Failure outranks a decision (§9): the requests stay on disk and
            # in `decisions_raised` for the report, but there is no point
            # answering one while the run cannot get past the failure anyway.
            raise WorkflowFailure("; ".join(errors))
        if decisions:
            raise DecisionRequested(decisions)
