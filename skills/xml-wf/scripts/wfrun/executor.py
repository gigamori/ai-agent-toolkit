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
from . import lint as lint_mod
from .agents import discover_agents
from .ccdirs import claude_config_dirs
from .interp import InterpError, interpolate, safe_eval
from .state import ReplayCursor, RunState


class WorkflowFailure(Exception):
    pass


class Executor:
    def __init__(self, wf: model.Workflow, params: dict[str, str],
                 run_dir: str | Path, base_dir: str | Path = ".",
                 permission_mode: str | None = None,
                 replay_events: list[dict] | None = None,
                 run_claude=claude_cli.run_claude,
                 ask_llm=claude_cli.ask_llm,
                 diagnose=adp.diagnose):
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

        self.vars: dict = {}
        self.step_count = 0
        self.cost_usd = 0.0
        self._attempt_seq: dict[str, int] = {}
        self._child_caps: list[tuple[int, int]] = []  # (count at start, cap)
        self._lock = threading.Lock()

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
        except WorkflowFailure as e:
            self._snapshot("failed", error=str(e))
            self.state.event("run", status="failed", error=str(e))
            raise
        self._snapshot("success")
        self.state.event("run", status="success")
        return "success"

    def _snapshot(self, status: str, error: str | None = None):
        self.state.snapshot(status=status, variables=self.vars,
                            step_count=self.step_count, cost_usd=self.cost_usd,
                            error=error)

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

    def _build_prompt(self, step: model.Step, fix: str | None = None
                      ) -> tuple[str, str]:
        """(system_text, user_text) — run-cc puts the constraint layers
        (role/mode/rules) in the system channel via --append-system-prompt."""
        try:
            return stepio.build_step_prompt_parts(
                self.wf, step, self.vars, self.base_dir,
                fix=fix, rules_cache=self._rules_cache,
                agents_cache=self._agents_cache)
        except stepio.StepIOError as e:
            raise WorkflowFailure(str(e)) from e

    # -------------------------------------------------------------- step ---
    def _exec_step(self, step: model.Step, replay_pool: dict | None = None):
        self._bump_limits(step.id)

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

        system, prompt = self._build_prompt(step)
        dispatch_model, dispatch_tools = stepio.dispatch_for(step, self._agents_cache)
        dispatch_model = self._map_model(dispatch_model, step.id)
        # Least privilege: --permission-mode (e.g. acceptEdits) reaches only
        # steps whose tools can write; read-only steps run without it.
        permission = (self.permission_mode
                      if model.tools_can_write(dispatch_tools) else None)
        debug_used = False
        attempt = 0
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
            if res.ok:
                # Mode/rules refusal (_meta protocol). run_claude also flags
                # this; checking here keeps the pipeline safe with any runner.
                blocked_line = modes.blocked_line(res.text)
                if blocked_line is not None:
                    res.ok = False
                    res.error_class = "refusal"
                    res.error = blocked_line[:500]
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
                self._finish_step(step, res, attempt)
                return

            self.state.event("step", key=step.id, status="attempt-failed",
                             attempt=seq, error=(res.error or "")[:1000],
                             error_class=res.error_class,
                             cost_usd=res.cost_usd)
            if attempt <= step.retry and claude_cli.is_retryable(res.error_class):
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
                    system, prompt = self._build_prompt(
                        step, fix=diagnosis.fix_instruction)
                    continue  # exactly one debug-granted attempt

            if step.on_error == "ignore":
                self.state.event("step", key=step.id, status="failed-ignored",
                                 error=(res.error or "")[:1000])
                self._snapshot("running")
                return
            raise WorkflowFailure(f"step '{step.id}' failed: {res.error}")

    def _map_model(self, name: str | None, where: str) -> str | None:
        """Canonical name -> the model this runner actually dispatches
        (model_map.json, "cc" table). Mappings are recorded for audit."""
        resolved = modelmap.resolve(name, "cc")
        if resolved != name:
            self.state.event("model-map", key=where,
                             canonical=name, resolved=resolved)
        return resolved

    def _missing_expected(self, step: model.Step) -> list[str]:
        """expect-file paths (comma-separated, {var}-interpolated, relative to
        the XML dir = subprocess cwd) that do not exist after the response."""
        raw = self._interp(step.expect_file, f"step '{step.id}' expect-file")
        missing = []
        for part in (p.strip() for p in raw.split(",")):
            if not part:
                continue
            path = Path(part)
            if not path.is_absolute():
                path = self.base_dir / path
            if not path.is_file():
                missing.append(part)
        return missing

    def _finish_step(self, step: model.Step, res, attempts: int):
        output_value = None
        if step.output:
            if step.output_type == "file":
                path = self.run_dir / "outputs" / f"{step.id}.md"
                path.write_text(modes.strip_mode_line(res.text), encoding="utf-8")
                output_value = str(path)
            else:
                output_value = stepio.unwrap_value(res.structured, res.text)
            with self._lock:
                self.vars[step.output] = output_value
        self.state.event("step", key=step.id, status="success",
                         attempts=attempts, cost_usd=res.cost_usd,
                         output_var=step.output, output_value=output_value)
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
            child = parser.parse_file(self.run_dir / replayed["xml"])
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
                    node, self.vars, self._agents_cache, fix=fix)
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

    def _validate_continuation(self, node: model.Replan, res):
        """Parse + lint a generated continuation. Returns (errors, child, xml)."""
        if not res.ok:
            return [res.error or "claude call failed"], None, ""
        xml_text = stepio.strip_fences(res.text)
        try:
            child = parser.parse_string(xml_text, base_dir=self.base_dir)
        except parser.ParseError as e:
            return [str(e)], None, xml_text
        findings = lint_mod.lint(child, base_dir=self.base_dir, check_roles=True,
                                 as_child=True, defined_vars=set(self.vars))
        errors = [str(f) for f in findings if f.level == "error"]
        if child.max > node.max_steps:
            errors.append(f"workflow max={child.max} exceeds the allowed "
                          f"max-steps={node.max_steps}")
        return errors, child, xml_text

    def _run_child(self, node: model.Replan, child: model.Workflow):
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
        with ThreadPoolExecutor(max_workers=node.max_workers) as pool_exec:
            futures = {pool_exec.submit(self._exec_step, s, pool): s
                       for s in node.children}
            for future in as_completed(futures):
                try:
                    future.result()
                except WorkflowFailure as e:
                    errors.append(str(e))
        if errors:
            raise WorkflowFailure("; ".join(errors))
