"""Executor tests with a fake claude runner (no API calls, no cost)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import adjudicate as adjudicate_mod  # noqa: E402
from wfrun import modes, parser, pi_cli, stepio  # noqa: E402
from wfrun.adp import Diagnosis  # noqa: E402
from wfrun.claude_cli import CliResult  # noqa: E402
from wfrun.executor import Executor, WorkflowFailure  # noqa: E402
from wfrun.state import load_events  # noqa: E402


class FakeClaude:
    """Scriptable stand-in for claude_cli.run_claude.

    handlers: list of (predicate(prompt), CliResult) tried in order, else echo.
    """

    def __init__(self):
        self.calls = []
        self.handlers = []

    def __call__(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        for predicate, result in self.handlers:
            if predicate(prompt):
                return result
        return CliResult(ok=True, text=f"echo:{prompt.splitlines()[0][:60]}",
                         cost_usd=0.01)

    def fail_times(self, needle, times, then_ok=True):
        """First `times` calls whose prompt contains needle fail; later succeed."""
        counter = {"n": 0}

        def predicate(prompt):
            if needle in prompt:
                counter["n"] += 1
                return counter["n"] <= times
            return False

        self.handlers.append(
            (predicate, CliResult(ok=False, error="ERROR: boom", cost_usd=0.01)))
        if then_ok:
            self.handlers.append(
                (lambda p: needle in p, CliResult(ok=True, text="recovered", cost_usd=0.01)))


def fake_ask_factory(answers):
    calls = []

    def fake_ask(question, **kwargs):
        calls.append(question)
        return answers.pop(0), "because", 0.001

    fake_ask.calls = calls
    return fake_ask


def _no_adjudicator(*args, **kwargs):
    raise AssertionError("the llm decider was called; this test did not "
                         "declare decider='llm'")


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run"
        self.fake = FakeClaude()
        # Named roles resolve to .claude/agents definitions whose body is
        # injected into every step prompt.
        agents_dir = Path(self.tmp.name) / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        for name in ("w", "builder"):
            (agents_dir / f"{name}.md").write_text(
                f"---\nname: {name}\ndescription: test\n---\nROLE-BODY-{name}",
                encoding="utf-8")
        (agents_dir / "coder.md").write_text(
            "---\nname: coder\ndescription: test\nmodel: haiku\ntools: Read\n---\n"
            "ROLE-BODY-coder", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def execute(self, xml, params=None, ask=None, diagnose=None, events=None,
                permission_mode=None, model_runner="cc", inherit_model=None,
                adjudicate=None, backend="cc"):
        wf = parser.parse_string(xml, base_dir=self.tmp.name)
        executor = Executor(
            wf, params or {}, self.run_dir, base_dir=self.tmp.name,
            permission_mode=permission_mode,
            replay_events=events,
            run_claude=self.fake,
            ask_llm=ask or fake_ask_factory([]),
            diagnose=diagnose or (lambda *a, **k: Diagnosis("FAIL", "no")),
            # Default raises rather than returning a ruling: a test that did
            # not ask for llm adjudication must never reach one silently.
            adjudicate=adjudicate or _no_adjudicator,
            model_runner=model_runner,
            inherit_model=inherit_model,
            # "cc"/"pi" -- which execution facility the run is on, kept apart
            # from model_runner's "cc"/"llm" on purpose. run_claude= stays the
            # fake either way: these tests exercise the executor's own
            # backend-conditional checks, not pi_cli's launcher.
            backend=backend,
        )
        return executor

    def wrap(self, inner, max_="20", extra=""):
        return f'<workflow name="t" version="2" max="{max_}" {extra}>{inner}</workflow>'


class TestBasics(ExecutorTestCase):
    def test_seq_outputs_and_interpolation(self):
        ex = self.execute(self.wrap('''
            <step id="s1" role="w" output="p1"><task>make data</task></step>
            <step id="s2" role="w" output="v" output-type="value"><task>use {p1}</task></step>
        '''))
        ex.run()
        # s1 output-type=file -> path variable, file exists with response text
        p1 = Path(ex.vars["p1"])
        self.assertTrue(p1.is_file())
        self.assertIn("echo:", p1.read_text(encoding="utf-8"))
        # s2 saw the interpolated path in its prompt
        self.assertIn(str(p1), self.fake.calls[1]["prompt"])
        self.assertTrue(str(ex.vars["v"]).startswith("echo:"))

    def test_guardrails_rules_and_role_injected(self):
        ex = self.execute(self.wrap('''
            <rules id="r1">RULE-BODY</rules>
            <step id="s1" role="w" rules="r1"><task>x</task></step>
        '''))
        ex.run()
        call = self.fake.calls[0]
        system = call["system_prompt"]
        # constraint layers travel in the system channel
        self.assertIn("Prompt axes in this step:", system)   # _meta header
        self.assertIn("Mode > Rules > Task > Role", system)  # precedence
        self.assertIn("<role>\nROLE-BODY-w\n</role>", system)
        self.assertIn('<rules id="r1">', system)
        self.assertIn("RULE-BODY", system)
        self.assertLess(system.index("<role>"), system.index('<rules id="r1">'))
        # task + guardrails stay in the user prompt
        prompt = call["prompt"]
        self.assertIn("ERROR:", prompt)  # guardrails text
        self.assertNotIn("<role>", prompt)

    def test_mode_injected_after_role(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" mode="execute"><task>x</task></step>'))
        ex.run()
        system = self.fake.calls[0]["system_prompt"]
        self.assertIn("mode:execute", system)
        self.assertIn("mode-output", system)               # _common.md injected
        self.assertNotIn("[Mode: current_mode]", system)   # legacy mandate gone
        self.assertLess(system.index("</role>"), system.index("mode:execute"))

    def test_role_less_step_drops_role_block_and_role_axis(self):
        """No role= and no inline <role>: the header loses its Role axis and no
        <role> block is injected — the other constraint layers are unchanged."""
        ex = self.execute(self.wrap(
            '<rules id="r1">RULE-BODY</rules>'
            '<step id="s1" mode="execute" rules="r1"><task>x</task></step>'))
        ex.run()
        system = self.fake.calls[0]["system_prompt"]
        self.assertIn("Prompt axes in this step:", system)
        self.assertIn("Precedence: Mode > Rules > Task.", system)
        self.assertNotIn("Mode > Rules > Task > Role", system)
        self.assertNotIn("Role: WHO you are", system)
        self.assertNotIn("<role>", system)
        # mode and rules still travel exactly as before
        self.assertIn("mode:execute", system)
        self.assertIn("mode-output", system)
        self.assertIn('<rules id="r1">', system)
        self.assertIn("RULE-BODY", system)

    def test_mode_alias_reads_target_file(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" mode="implement"><task>x</task></step>'))
        ex.run()
        system = self.fake.calls[0]["system_prompt"]
        self.assertIn("mode:implement", system)        # chosen name preserved
        self.assertIn("deviate-from-plan", system)     # execute.md body

    def test_inline_role_injected(self):
        ex = self.execute(self.wrap(
            '<step id="s1"><role>INLINE-PERSONA</role><task>x</task></step>'))
        ex.run()
        call = self.fake.calls[0]
        self.assertIn("<role>\nINLINE-PERSONA\n</role>", call["system_prompt"])
        self.assertIsNone(call["model"])   # no frontmatter to inherit
        self.assertIsNone(call["tools"])

    def test_step_flags_forwarded(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="coder" model="opus" effort="high" tools="Read,Write">'
            '<task>x</task></step>'))
        ex.run()
        call = self.fake.calls[0]
        self.assertNotIn("agent", call)              # role body travels in-prompt
        self.assertEqual(call["model"], "opus")      # step attr wins over frontmatter
        self.assertEqual(call["effort"], "high")
        self.assertEqual(call["tools"], "Read,Write")
        self.assertIn("ROLE-BODY-coder", call["system_prompt"])

    def test_blocked_response_is_error(self):
        self.fake.handlers.append(
            (lambda p: "guarded" in p,
             CliResult(ok=True, text="[BLOCKED: mode-rule generate-target-artifacts]"
                                     " survey may not write sources", cost_usd=0.01)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" mode="survey"><task>guarded</task></step>'))
        with self.assertRaises(WorkflowFailure) as ctx:
            ex.run()
        self.assertIn("[BLOCKED: mode-rule", str(ctx.exception))

    def test_blocked_skips_deterministic_retry(self):
        # An identical prompt would be refused identically: no retry burn.
        self.fake.handlers.append(
            (lambda p: "guarded" in p,
             CliResult(ok=True, text="[BLOCKED: mode-rule x] no", cost_usd=0.01)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" mode="survey" retry="3"><task>guarded</task></step>'))
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(len(self.fake.calls), 1)

    def test_expect_file_verified(self):
        xml = self.wrap(
            '<step id="s1" role="w" expect-file="out.txt"><task>make it</task></step>')
        ex = self.execute(xml)
        with self.assertRaises(WorkflowFailure) as ctx:
            ex.run()  # fake claude never writes files
        self.assertIn("expect-file", str(ctx.exception))
        # once the artifact exists (relative to the XML dir), the step passes
        (Path(self.tmp.name) / "out.txt").write_text("data", encoding="utf-8")
        self.execute(xml).run()

    def test_model_map_applied_and_recorded(self):
        from wfrun import modelmap
        map_path = Path(self.tmp.name) / "mm.json"
        map_path.write_text(json.dumps({"cc": {"opus": "mapped-opus"}}), encoding="utf-8")
        old = modelmap.MAP_PATH
        modelmap.MAP_PATH = map_path
        try:
            ex = self.execute(self.wrap(
                '<step id="s1" role="w" model="opus"><task>x</task></step>'))
            ex.run()
        finally:
            modelmap.MAP_PATH = old
        self.assertEqual(self.fake.calls[0]["model"], "mapped-opus")
        mapped = [e for e in load_events(self.run_dir) if e["kind"] == "model-map"]
        self.assertEqual((mapped[0]["canonical"], mapped[0]["resolved"]),
                         ("opus", "mapped-opus"))

    def test_model_runner_selects_the_llm_table(self):
        # design phase6-run-pi-design.md §1: _map_model's runner table is
        # selectable at construction ("llm" for run-pi) rather than
        # hardcoded to "cc" -- this one path also carries ask=/replan
        # model resolution, but a step model= is the simplest probe.
        from wfrun import modelmap
        map_path = Path(self.tmp.name) / "mm.json"
        map_path.write_text(
            json.dumps({"cc": {"opus": "cc-opus"}, "llm": {"opus": "llm-opus"}}),
            encoding="utf-8")
        old = modelmap.MAP_PATH
        modelmap.MAP_PATH = map_path
        try:
            ex = self.execute(self.wrap(
                '<step id="s1" role="w" model="opus"><task>x</task></step>'),
                model_runner="llm")
            ex.run()
        finally:
            modelmap.MAP_PATH = old
        self.assertEqual(self.fake.calls[0]["model"], "llm-opus")

    def test_inherit_model_used_when_step_has_no_model(self):
        # design phase6 review point 2, 2026-07-30: a step with no model=
        # and no role-frontmatter default must run on --inherit-model's
        # value (the invoking skill session's own model), not the backend
        # CLI's own configured default.
        ex = self.execute(self.wrap(
            '<step id="s1"><role>W</role><task>x</task></step>'),
            inherit_model="session-model")
        ex.run()
        self.assertEqual(self.fake.calls[0]["model"], "session-model")
        mapped = [e for e in load_events(self.run_dir) if e["kind"] == "model-map"]
        self.assertEqual((mapped[0]["canonical"], mapped[0]["resolved"],
                          mapped[0]["source"]),
                         (None, "session-model", "inherit"))

    def test_inherit_model_ignored_when_step_has_its_own_model(self):
        # A step's own model= (or role-frontmatter default) always wins;
        # --inherit-model only fills the gap when neither is present.
        ex = self.execute(self.wrap(
            '<step id="s1" model="opus"><role>W</role><task>x</task></step>'),
            inherit_model="session-model")
        ex.run()
        self.assertNotEqual(self.fake.calls[0]["model"], "session-model")

    def test_model_inherit_warnings_empty_when_inherit_model_given(self):
        ex = self.execute(self.wrap(
            '<step id="s1"><role>W</role><task>x</task></step>'),
            inherit_model="session-model")
        self.assertEqual(ex.model_inherit_warnings(), [])

    def test_model_inherit_warnings_lists_steps_with_no_model(self):
        ex = self.execute(self.wrap(
            '<step id="s1"><role>W</role><task>x</task></step>'
            '<step id="s2" model="opus"><role>W</role><task>y</task></step>'))
        warnings = ex.model_inherit_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("s1", warnings[0])
        self.assertNotIn("s2", warnings[0])
        self.assertIn("--inherit-model", warnings[0])

    def test_model_inherit_warnings_empty_when_every_step_has_a_model(self):
        ex = self.execute(self.wrap(
            '<step id="s1" model="opus"><role>W</role><task>x</task></step>'))
        self.assertEqual(ex.model_inherit_warnings(), [])

    def test_permission_mode_reaches_only_write_capable_steps(self):
        ex = self.execute(self.wrap(
            '<step id="ro" role="w" tools="Read,Grep"><task>look</task></step>'
            '<step id="rw" role="w" tools="Read,Write"><task>write</task></step>'
            '<step id="un" role="w"><task>unrestricted</task></step>'),
            permission_mode="acceptEdits")
        ex.run()
        self.assertEqual([c["permission_mode"] for c in self.fake.calls],
                         [None, "acceptEdits", "acceptEdits"])

    def test_role_frontmatter_dispatch_defaults(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="coder"><task>x</task></step>'))
        ex.run()
        call = self.fake.calls[0]
        self.assertEqual(call["model"], "haiku")     # from coder.md frontmatter
        self.assertEqual(call["tools"], "Read")

    def test_missing_named_role_fails(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="ghost"><task>x</task></step>'))
        with self.assertRaises(WorkflowFailure):
            ex.run()

    def test_base_dir_under_claude_config_rejected(self):
        # ~/.claude is a protected tree: child claude processes cannot get
        # write approval there, so the executor must fail fast at init.
        wf = parser.parse_string(self.wrap(
            '<step id="s1" role="w"><task>x</task></step>'))
        for base in (Path.home() / ".claude" / "skills" / "somewhere",
                     Path.home() / ".claude"):
            with self.assertRaises(WorkflowFailure, msg=base):
                Executor(wf, {}, self.run_dir, base_dir=base,
                         run_claude=self.fake)

    def test_set_value_and_expr(self):
        ex = self.execute(self.wrap('''
            <set var="n" value="1"/>
            <set var="n" expr="{n} + 41"/>
        '''))
        ex.run()
        self.assertEqual(ex.vars["n"], 42)

    def test_param_resolution(self):
        xml = self.wrap('<step id="s1" role="w"><task>{env}</task></step>')
        xml = xml.replace(">", '><param name="env" required="true"/>', 1)
        with self.assertRaises(WorkflowFailure):
            self.execute(xml)  # required missing
        ex = self.execute(xml, params={"env": "prod"})
        ex.run()
        self.assertIn("prod", self.fake.calls[0]["prompt"])

    def test_unknown_param_rejected(self):
        with self.assertRaises(WorkflowFailure):
            self.execute(self.wrap('<step id="s1" role="w"><task>x</task></step>'),
                         params={"nope": "1"})


class TestControlFlow(ExecutorTestCase):
    def test_if_test_branches(self):
        ex = self.execute(self.wrap('''
            <set var="n" value="5"/>
            <if test="int({n}) > 3">
              <then><step id="yes" role="w"><task>yes</task></step></then>
              <else><step id="no" role="w"><task>no</task></step></else>
            </if>
        '''))
        ex.run()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIn("yes", self.fake.calls[0]["prompt"])

    def test_if_ask_uses_llm(self):
        ask = fake_ask_factory([False])
        ex = self.execute(self.wrap('''
            <if ask="is it good?">
              <then><step id="yes" role="w"><task>yes</task></step></then>
              <else><step id="no" role="w"><task>no</task></step></else>
            </if>
        '''), ask=ask)
        ex.run()
        self.assertEqual(ask.calls, ["is it good?"])
        self.assertIn("no", self.fake.calls[0]["prompt"])

    def test_while_and_max(self):
        ex = self.execute(self.wrap('''
            <set var="n" value="0"/>
            <while test="int({n}) &lt; 3" max="10">
              <do><set var="n" expr="{n} + 1"/></do>
            </while>
        '''))
        ex.run()
        self.assertEqual(ex.vars["n"], 3)
        # max cap
        ex2 = self.execute(self.wrap('''
            <set var="n" value="0"/>
            <while test="1 == 1" max="4">
              <do><set var="n" expr="{n} + 1"/></do>
            </while>
        '''))
        ex2.run()
        self.assertEqual(ex2.vars["n"], 4)
        self.assertIn("while-max-reached",
                      [e["kind"] for e in load_events(self.run_dir)])

    def test_each_items_and_scope(self):
        ex = self.execute(self.wrap('''
            <each items='["a", "b"]' as="f">
              <do><step id="s" role="w"><task>proc {f} #{f_index}</task></step></do>
            </each>
        '''))
        ex.run()
        self.assertIn("proc a #0", self.fake.calls[0]["prompt"])
        self.assertIn("proc b #1", self.fake.calls[1]["prompt"])
        self.assertNotIn("f", ex.vars)

    def test_each_range_and_glob(self):
        (Path(self.tmp.name) / "in_b.txt").write_text("b", encoding="utf-8")
        (Path(self.tmp.name) / "in_a.txt").write_text("a", encoding="utf-8")
        ex = self.execute(self.wrap('''
            <each glob="in_*.txt" as="f">
              <do><step id="s" role="w"><task>read {f}</task></step></do>
            </each>
            <each range="2" as="i">
              <do><set var="last" value="{i}"/></do>
            </each>
        '''))
        ex.run()
        self.assertIn("in_a.txt", self.fake.calls[0]["prompt"])  # sorted order
        self.assertIn("in_b.txt", self.fake.calls[1]["prompt"])
        self.assertEqual(ex.vars["last"], "1")

    def test_step_max_enforced(self):
        ex = self.execute(self.wrap('''
            <each range="5" as="i">
              <do><step id="s" role="w"><task>{i}</task></step></do>
            </each>
        ''', max_="3"))
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(len(self.fake.calls), 3)

    def test_parallel(self):
        ex = self.execute(self.wrap('''
            <parallel max-workers="2">
              <step id="a" role="w" output="oa"><task>A</task></step>
              <step id="b" role="w" output="ob"><task>B</task></step>
            </parallel>
            <step id="c" role="w"><task>join {oa} {ob}</task></step>
        '''))
        ex.run()
        self.assertEqual(len(self.fake.calls), 3)
        self.assertIn("join", self.fake.calls[2]["prompt"])


class TestErrorsAndResume(ExecutorTestCase):
    def test_retry_recovers(self):
        self.fake.fail_times("flaky", times=1)
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="1" output="o" output-type="value">'
            '<task>flaky</task></step>'))
        ex.run()
        self.assertEqual(ex.vars["o"], "recovered")
        self.assertEqual(len(self.fake.calls), 2)

    def test_on_error_fail_default(self):
        self.fake.handlers.append(
            (lambda p: "boom" in p, CliResult(ok=False, error="ERROR: x", cost_usd=0)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w"><task>boom</task></step>'))
        with self.assertRaises(WorkflowFailure):
            ex.run()

    def test_on_error_ignore(self):
        self.fake.handlers.append(
            (lambda p: "boom" in p, CliResult(ok=False, error="ERROR: x", cost_usd=0)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" on-error="ignore"><task>boom</task></step>'
            '<step id="s2" role="w"><task>after</task></step>'))
        ex.run()
        self.assertEqual(len(self.fake.calls), 2)

    def test_debug_retry_once(self):
        self.fake.fail_times("fragile", times=1)
        diagnoses = []

        def fake_diag(step, prompt, failure, **kwargs):
            diagnoses.append(step.id)
            return Diagnosis("RETRY", "transient", fix_instruction="add --force")

        ex = self.execute(self.wrap(
            '<step id="s1" role="w" on-error="debug"><task>fragile</task></step>'),
            diagnose=fake_diag)
        ex.run()
        self.assertEqual(diagnoses, ["s1"])
        self.assertIn("add --force", self.fake.calls[1]["prompt"])

    def test_debug_retry_fails_again_is_fatal(self):
        self.fake.fail_times("fragile", times=99, then_ok=False)

        def fake_diag(step, prompt, failure, **kwargs):
            return Diagnosis("RETRY", "try", fix_instruction="fix")

        ex = self.execute(self.wrap(
            '<step id="s1" role="w" on-error="debug"><task>fragile</task></step>'),
            diagnose=fake_diag)
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(len(self.fake.calls), 2)  # original + one debug retry

    def test_guardrail_skips_deterministic_retry_but_allows_debug(self):
        # reliability-spec.md §3.1/§3.2: guardrail (ERROR:) is not retried
        # identically, but on-error="debug" may still fire.
        self.fake.handlers.append(
            (lambda p: "guarded" in p,
             CliResult(ok=False, error="ERROR: boom", error_class="guardrail",
                      cost_usd=0.01)))
        diagnoses = []

        def fake_diag(step, prompt, failure, **kwargs):
            diagnoses.append(step.id)
            return Diagnosis("FAIL", "no fix")

        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="3" on-error="debug">'
            '<task>guarded</task></step>'), diagnose=fake_diag)
        with self.assertRaises(WorkflowFailure):
            ex.run()
        # 1 initial attempt only -- retry was skipped (not 4)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(diagnoses, ["s1"])  # debug still ran

    def test_refusal_skips_debug(self):
        # reliability-spec.md §3.1/§3.2: [BLOCKED:] refusal is neither
        # retried nor handed to debug (current behavior, made explicit).
        self.fake.handlers.append(
            (lambda p: "guarded" in p,
             CliResult(ok=True, text="[BLOCKED: mode-rule x] no", cost_usd=0.01)))
        diagnoses = []

        def fake_diag(step, prompt, failure, **kwargs):
            diagnoses.append(step.id)
            return Diagnosis("RETRY", "try", fix_instruction="fix")

        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="3" on-error="debug">'
            '<task>guarded</task></step>'), diagnose=fake_diag)
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(diagnoses, [])  # debug never invoked

    def test_denied_skips_retry_and_debug(self):
        self.fake.handlers.append(
            (lambda p: "guarded" in p,
             CliResult(ok=False, error="claude reported permission_denials for: Write",
                      error_class="denied", cost_usd=0.01)))
        diagnoses = []

        def fake_diag(step, prompt, failure, **kwargs):
            diagnoses.append(step.id)
            return Diagnosis("RETRY", "try", fix_instruction="fix")

        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="3" on-error="debug">'
            '<task>guarded</task></step>'), diagnose=fake_diag)
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(diagnoses, [])

    def test_transient_retries_but_skips_debug(self):
        # reliability-spec.md §13.5: transient is retried (it's a
        # technical/upstream hiccup) but never handed to debug -- treating
        # it as a fixable "failed" is the P3/C3 retry-storm mechanism.
        self.fake.handlers.append(
            (lambda p: "flaky" in p,
             CliResult(ok=False, error="claude reported api_error (status=529)",
                      error_class="transient", cost_usd=0.01)))
        diagnoses = []

        def fake_diag(step, prompt, failure, **kwargs):
            diagnoses.append(step.id)
            return Diagnosis("RETRY", "try", fix_instruction="fix")

        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="2" on-error="debug">'
            '<task>flaky</task></step>'), diagnose=fake_diag)
        with self.assertRaises(WorkflowFailure):
            ex.run()
        # 1 initial + 2 retries consumed (all failed identically) = 3 calls
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(diagnoses, [])  # debug never invoked

    def test_env_skips_retry_and_goes_straight_to_on_error(self):
        # reliability-spec.md §3.3: env -> retry not consumed, straight to
        # on-error (here: fail, the default).
        self.fake.handlers.append(
            (lambda p: "guarded" in p,
             CliResult(ok=False, error="claude CLI not found on PATH",
                      error_class="env", cost_usd=0)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="3"><task>guarded</task></step>'))
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(len(self.fake.calls), 1)

    def test_timeout_consumes_retry(self):
        self.fake.handlers.append(
            (lambda p: "flaky" in p,
             CliResult(ok=False, error="timeout after 60s",
                      error_class="timeout", cost_usd=0)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="2"><task>flaky</task></step>'))
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(len(self.fake.calls), 3)  # 1 initial + 2 retries

    def test_empty_body_via_fake_run_claude_is_behavioral_and_retries(self):
        # End-to-end through the executor with a fake run_claude, matching
        # reliability-spec.md §3.3's literal test list ("空本文->behavioral
        # 失敗"). The classification itself is unit-tested directly against
        # classify_result() in test_claude_cli.py.
        self.fake.handlers.append(
            (lambda p: "flaky" in p,
             CliResult(ok=False, error="empty result",
                      error_class="behavioral", cost_usd=0)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="1"><task>flaky</task></step>'))
        with self.assertRaises(WorkflowFailure) as ctx:
            ex.run()
        self.assertIn("empty result", str(ctx.exception))
        self.assertEqual(len(self.fake.calls), 2)  # 1 initial + 1 retry (behavioral retries)

    def test_resume_skips_recorded_successes(self):
        xml = self.wrap('''
            <step id="s1" role="w" output="p"><task>first</task></step>
            <step id="s2" role="w"><task>second uses {p}</task></step>
        ''')
        self.fake.handlers.append(
            (lambda p: "second" in p, CliResult(ok=False, error="ERROR: die", cost_usd=0)))
        ex = self.execute(xml)
        with self.assertRaises(WorkflowFailure):
            ex.run()
        first_calls = len(self.fake.calls)

        # resume: fix the failure, replay events
        self.fake.handlers.clear()
        events = load_events(self.run_dir)
        ex2 = self.execute(xml, events=events)
        ex2.run()
        # s1 not re-executed; s2 re-executed with restored {p}
        new_calls = self.fake.calls[first_calls:]
        self.assertEqual(len(new_calls), 1)
        self.assertIn(str(ex.vars["p"]), new_calls[0]["prompt"])

    def test_resume_replays_ask_verdicts(self):
        xml = self.wrap('''
            <if ask="good?">
              <then><step id="s1" role="w"><task>ok</task></step></then>
            </if>
            <step id="s2" role="w"><task>tail</task></step>
        ''')
        self.fake.handlers.append(
            (lambda p: "tail" in p, CliResult(ok=False, error="ERROR: die", cost_usd=0)))
        ask = fake_ask_factory([True])
        ex = self.execute(xml, ask=ask)
        with self.assertRaises(WorkflowFailure):
            ex.run()

        self.fake.handlers.clear()
        ask2 = fake_ask_factory([])  # would raise if consulted
        ex2 = self.execute(xml, ask=ask2, events=load_events(self.run_dir))
        ex2.run()
        self.assertEqual(ask2.calls, [])  # verdict replayed, not re-asked

    def test_replan_generates_validates_and_runs_child(self):
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w" output="extra" output-type="value">'
                 '<task>child work</task></step></workflow>')
        self.fake.handlers.append(
            (lambda p: "PLAN-ME" in p,
             CliResult(ok=True, text=f"```xml\n{child}\n```", cost_usd=0.02)))
        ex = self.execute(self.wrap(
            '<replan id="r1" role="builder" outputs="extra"><task>PLAN-ME</task></replan>'
            '<step id="tail" role="w"><task>after {extra}</task></step>'))
        ex.run()
        # builder call, child step, tail step
        self.assertEqual(len(self.fake.calls), 3)
        self.assertIn("child work", self.fake.calls[1]["prompt"])
        self.assertIn("after echo:", self.fake.calls[2]["prompt"])
        # generated XML persisted (fences stripped)
        saved = (self.run_dir / "replans" / "r1_01.xml").read_text(encoding="utf-8")
        self.assertTrue(saved.startswith("<workflow"))
        # builder prompt carries the contract, not code
        self.assertIn("MUST NOT contain", self.fake.calls[0]["prompt"])
        # ... and the execution-mode vocabulary (aliases excluded) for
        # continuation steps
        self.assertIn(", ".join(m for m in modes.available_modes()
                                if m not in modes.MODE_ALIASES),
                      self.fake.calls[0]["prompt"])
        self.assertNotIn("implement", self.fake.calls[0]["prompt"])

    def _replan_builder_returns_child(self):
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w"><task>child work</task></step></workflow>')
        self.fake.handlers.append(
            (lambda p: "PLAN-ME" in p,
             CliResult(ok=True, text=f"```xml\n{child}\n```", cost_usd=0.02)))

    def test_replan_with_role_sends_role_only_system(self):
        """A replan gets no _meta and no _common — only the role."""
        self._replan_builder_returns_child()
        ex = self.execute(self.wrap(
            '<replan id="r1" role="builder"><task>PLAN-ME</task></replan>'))
        ex.run()
        self.assertEqual(self.fake.calls[0]["system_prompt"],
                         "<role>\nROLE-BODY-builder\n</role>")

    def test_role_less_replan_sends_empty_system(self):
        """Without a role there is nothing to put in the system channel, so it
        is empty and both backends then omit the append-system-prompt flag."""
        self._replan_builder_returns_child()
        ex = self.execute(self.wrap(
            '<replan id="r1"><task>PLAN-ME</task></replan>'))
        ex.run()
        self.assertEqual(self.fake.calls[0]["system_prompt"], "")
        # no framework header sneaks in: a replan declares no mode, so a Mode
        # axis would be left dangling
        self.assertNotIn("Prompt axes", self.fake.calls[0]["prompt"])

    def test_replan_retry_with_lint_feedback(self):
        bad = '<workflow name="c" version="2" max="5"><replan id="x" role="builder"><task>t</task></replan></workflow>'
        good = ('<workflow name="c" version="2" max="5">'
                '<step id="c1" role="w"><task>ok</task></step></workflow>')
        state = {"n": 0}

        def handler(prompt):
            if "PLAN-ME" not in prompt:
                return False
            state["n"] += 1
            return state["n"] == 1

        self.fake.handlers.append((handler, CliResult(ok=True, text=bad, cost_usd=0.01)))
        self.fake.handlers.append(
            (lambda p: "PLAN-ME" in p, CliResult(ok=True, text=good, cost_usd=0.01)))
        ex = self.execute(self.wrap(
            '<replan id="r1" role="builder" retry="1"><task>PLAN-ME</task></replan>'))
        ex.run()
        # second builder prompt contains the validator feedback
        self.assertIn("replan-forbidden", self.fake.calls[1]["prompt"])
        self.assertEqual(len(self.fake.calls), 3)  # builder x2 + child step

    def test_replan_child_step_cap(self):
        child = ('<workflow name="c" version="2" max="5"><each range="3" as="i">'
                 '<do><step id="c1" role="w"><task>item {i}</task></step></do>'
                 '</each></workflow>')
        self.fake.handlers.append(
            (lambda p: "PLAN-ME" in p, CliResult(ok=True, text=child, cost_usd=0.01)))
        ex = self.execute(self.wrap(
            '<replan id="r1" role="builder" max-steps="1"><task>PLAN-ME</task></replan>'))
        with self.assertRaises(WorkflowFailure):
            ex.run()

    def test_replan_missing_declared_output(self):
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w"><task>no output</task></step></workflow>')
        self.fake.handlers.append(
            (lambda p: "PLAN-ME" in p, CliResult(ok=True, text=child, cost_usd=0.01)))
        ex = self.execute(self.wrap(
            '<replan id="r1" role="builder" outputs="promised"><task>PLAN-ME</task></replan>'))
        with self.assertRaises(WorkflowFailure) as ctx:
            ex.run()
        self.assertIn("promised", str(ctx.exception))

    def test_replan_resume_skips_regeneration(self):
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w" output="extra" output-type="value">'
                 '<task>child work</task></step></workflow>')
        # TAIL-ME, not "end": needles are matched against the WHOLE prompt,
        # which carries the injected guardrails, and "end" is a substring of
        # the decision protocol's "recommendation:" key.
        xml = self.wrap(
            '<replan id="r1" role="builder"><task>PLAN-ME</task></replan>'
            '<step id="tail" role="w"><task>TAIL-ME</task></step>')
        self.fake.handlers.append(
            (lambda p: "PLAN-ME" in p, CliResult(ok=True, text=child, cost_usd=0.01)))
        self.fake.handlers.append(
            (lambda p: "TAIL-ME" in p, CliResult(ok=False, error="ERROR: die", cost_usd=0)))
        ex = self.execute(xml)
        with self.assertRaises(WorkflowFailure):
            ex.run()
        first_calls = len(self.fake.calls)

        self.fake.handlers.clear()
        ex2 = self.execute(xml, events=load_events(self.run_dir))
        ex2.run()
        new = self.fake.calls[first_calls:]
        # neither the builder nor the child step re-ran; only the tail did
        self.assertEqual(len(new), 1)
        self.assertIn("TAIL-ME", new[0]["prompt"])

    def test_replan_resume_parses_continuation_against_the_base_dir(self):
        """Replay resolves `@`-relative paths exactly as the live run did.

        The recorded continuation sits under `<run dir>/replans/`, so parsing
        it by path would root the child there and a continuation step's
        `schema="@rel/s.json"` would resolve against the run dir instead of the
        XML dir -- a parse error, or a different file (reliability-spec.md
        §14.1).
        """
        schema_text = '{"type": "object"}'
        schema_dir = Path(self.tmp.name) / "rel"
        schema_dir.mkdir()
        (schema_dir / "s.json").write_text(schema_text, encoding="utf-8")
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w" schema="@rel/s.json">'
                 '<task>child work</task></step></workflow>')
        self.fake.handlers.append(
            (lambda p: "PLAN-ME" in p, CliResult(ok=True, text=child, cost_usd=0.01)))
        xml = self.wrap(
            '<replan id="r1" role="builder"><task>PLAN-ME</task></replan>')
        ex = self.execute(xml)
        ex.run()
        # live: builder call, then the child step carrying the resolved schema
        self.assertEqual(self.fake.calls[1]["schema"], schema_text)
        first_calls = len(self.fake.calls)

        # resume with the recorded replan success only: the continuation is
        # replayed from disk and its child step then runs live again
        events = [e for e in load_events(self.run_dir) if e["kind"] == "replan"]
        ex2 = self.execute(xml, events=events)
        ex2.run()
        new = self.fake.calls[first_calls:]
        self.assertEqual(len(new), 1)  # the builder was not re-run
        self.assertIn("child work", new[0]["prompt"])
        self.assertEqual(new[0]["schema"], schema_text)

    def test_budget_exhaustion(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="w"><task>a</task></step>'
            '<step id="s2" role="w"><task>b</task></step>',
            extra='budget-usd="0.005"'))
        with self.assertRaises(WorkflowFailure):
            ex.run()  # first step costs 0.01 > budget before second


class TestReplanContinuationUnderPiBackend(ExecutorTestCase):
    """The pi backend's fail-fast, extended to replan continuations
    (phase6-run-pi-design.md §10).

    `pi_cli.pi_compat_errors` runs once at startup over the statically
    declared steps, so a continuation built mid-run never reached it: its
    `schema=` was refused only when `run_pi` was already launching the step,
    half-way through the run. These tests pin the three places §10.2 closes
    that -- the executor knowing its backend, the continuation validated as
    that backend, and the builder prompt told about it up front -- plus the
    tools-widening warning of disposition (e).

    `run_claude=` stays the fake in all of them: what is under test is the
    executor's backend-conditional logic, not pi_cli's launcher.
    """

    CHILD_OK = ('<workflow name="c" version="2" max="5">'
                '<step id="c1" role="w"><task>child work</task></step>'
                '</workflow>')
    REPLAN = '<replan id="r1" role="builder"><task>PLAN-ME</task></replan>'
    REPLAN_RETRY = ('<replan id="r1" role="builder" retry="1">'
                    '<task>PLAN-ME</task></replan>')

    def _builder_returns(self, first, second=None):
        """Script the builder call by attempt: `first` for attempt 1, then
        `second` for every later one (same shape as
        test_replan_retry_with_lint_feedback's handler pair)."""
        state = {"n": 0}

        def once(prompt):
            if "PLAN-ME" not in prompt:
                return False
            state["n"] += 1
            return state["n"] == 1

        self.fake.handlers.append(
            (once, CliResult(ok=True, text=first, cost_usd=0.01)))
        if second is not None:
            self.fake.handlers.append(
                (lambda p: "PLAN-ME" in p,
                 CliResult(ok=True, text=second, cost_usd=0.01)))

    def _replan_errors(self):
        return [e["error"] for e in load_events(self.run_dir)
                if e["kind"] == "replan" and e["status"] == "attempt-failed"]

    # --- (a) the violation is caught, and the builder is told in its own terms
    def test_schema_in_continuation_is_refused_and_fed_back_to_the_builder(self):
        bad = ('<workflow name="c" version="2" max="5">'
               '<step id="c1" role="w" schema=\'{"type":"object"}\'>'
               '<task>bad</task></step></workflow>')
        self._builder_returns(bad, self.CHILD_OK)
        ex = self.execute(self.wrap(self.REPLAN_RETRY), backend="pi")
        ex.run()

        errors = self._replan_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("schema= is not allowed on this backend", errors[0])
        # and it reached the next attempt as fix= feedback -- the point of
        # merging it into the same list the lint findings ride (§10.2 point 2)
        retry_prompt = self.fake.calls[1]["prompt"]
        self.assertIn("schema= is not allowed on this backend", retry_prompt)
        self.assertIn("expect-file instead", retry_prompt)
        # disposition (d): the startup text addresses a human and points at
        # build mode, which is the wrong audience for a regeneration hint
        self.assertNotIn("run the skill in build mode", retry_prompt)

    def test_on_error_debug_in_continuation_is_refused_the_same_way(self):
        bad = ('<workflow name="c" version="2" max="5">'
               '<step id="c1" role="w" on-error="debug"><task>bad</task>'
               '</step></workflow>')
        self._builder_returns(bad, self.CHILD_OK)
        ex = self.execute(self.wrap(self.REPLAN_RETRY), backend="pi")
        ex.run()

        errors = self._replan_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn('on-error="debug" is not allowed on this backend',
                      errors[0])
        self.assertIn('on-error="debug" is not allowed on this backend',
                      self.fake.calls[1]["prompt"])

    def test_neither_attribute_is_refused_under_cc(self):
        """The same continuation is valid on cc: claude enforces schema= and
        adp.diagnose exists there."""
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w" on-error="debug" '
                 'schema=\'{"type":"object"}\'><task>fine here</task>'
                 '</step></workflow>')
        self._builder_returns(child)
        ex = self.execute(self.wrap(self.REPLAN), backend="cc")
        ex.run()
        self.assertEqual(self._replan_errors(), [])

    def test_pi_model_check_reaches_the_continuation(self):
        """The other half of passing the live backend to lint(): an
        unresolvable model name on a continuation step is an error on pi,
        where before it was linted as cc and never checked at all."""
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w" model="nowhere-9000">'
                 '<task>child work</task></step></workflow>')
        self._builder_returns(child)
        ex = self.execute(self.wrap(self.REPLAN), backend="pi")
        with mock.patch.object(pi_cli, "list_available_models",
                               return_value=[("anthropic", "claude-sonnet-4")]):
            with self.assertRaises(WorkflowFailure) as ctx:
                ex.run()
        self.assertIn("pi-model-unavailable", str(ctx.exception))

    # --- (b) the builder is told before it writes anything ------------------
    def test_builder_prompt_carries_the_constraint_under_pi(self):
        self._builder_returns(self.CHILD_OK)
        ex = self.execute(self.wrap(self.REPLAN), backend="pi")
        ex.run()
        self.assertIn(stepio.REPLAN_PI_CONSTRAINT, self.fake.calls[0]["prompt"])

    def test_builder_prompt_has_no_constraint_line_under_cc(self):
        self._builder_returns(self.CHILD_OK)
        ex = self.execute(self.wrap(self.REPLAN), backend="cc")
        ex.run()
        self.assertNotIn(stepio.REPLAN_PI_CONSTRAINT,
                         self.fake.calls[0]["prompt"])
        self.assertNotIn("MUST NOT use `schema=`", self.fake.calls[0]["prompt"])

    # --- (e) the widening a continuation step gets is not silent ------------
    def test_continuation_tool_specifier_warns_under_pi(self):
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w" tools="Bash(git:*)">'
                 '<task>child work</task></step></workflow>')
        self._builder_returns(child)
        ex = self.execute(self.wrap(self.REPLAN), backend="pi")
        ex.run()
        self.assertEqual(len(ex.protocol_warnings), 1)
        warning = ex.protocol_warnings[0]
        self.assertIn("r1", warning)
        self.assertIn("c1", warning)
        self.assertIn("Bash(git:*)", warning)
        events = [e for e in load_events(self.run_dir) if e["kind"] == "warning"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["key"], "r1")

    def test_continuation_tool_specifier_is_silent_under_cc(self):
        """cc honors the specifier, so there is nothing to warn about."""
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w" tools="Bash(git:*)">'
                 '<task>child work</task></step></workflow>')
        self._builder_returns(child)
        ex = self.execute(self.wrap(self.REPLAN), backend="cc")
        ex.run()
        self.assertEqual(ex.protocol_warnings, [])


class TestDecisionContextSurvivesDebugRetry(ExecutorTestCase):
    """A ruling settled in-process stays in front of every later rebuild of
    the same visit (xml-wf-decision-request.md §13.6, defect §19.1).

    The form-(b) re-run is the step's second chance to apply a ruling. When
    that re-run fails for an unrelated reason and `on-error="debug"` grants
    one more attempt, the debug rebuild has to carry the same rulings: a
    prompt without them lets the fresh subagent walk back into a fork that
    was already settled -- the exact accident §13.6 exists to prevent.
    """

    # work-state: stopped forces continuation form (b) on its own, so the
    # step needs no expect-file or output= for the ruling to re-run it (§6).
    STOPPED_PAYLOAD = ("DECISION: which join\n"
                       "fork: two readings of 'merge'\n"
                       "options:\n"
                       "  1. A -- loses rows\n"
                       "  2. B -- loses columns\n"
                       "recommendation: 1\n"
                       "work-state: stopped")

    def respond_in_order(self, needle, *results):
        """Answer prompts containing `needle` with `results`, one per call."""
        state = {"n": 0}
        for index, result in enumerate(results):
            def predicate(prompt, index=index):
                if needle not in prompt or state["n"] != index:
                    return False
                state["n"] += 1
                return True
            self.fake.handlers.append((predicate, result))

    def test_debug_retry_after_a_form_b_re_run_still_carries_the_ruling(self):
        self.respond_in_order(
            "DO-WORK",
            CliResult(ok=True, text=self.STOPPED_PAYLOAD, cost_usd=0.02),
            CliResult(ok=False, error="the file never appeared",
                      error_class="behavioral", cost_usd=0.01),
            CliResult(ok=True, text="done", cost_usd=0.01))

        def fake_diag(step, prompt, failure, **kwargs):
            return Diagnosis("RETRY", "transient", fix_instruction="add --force")

        ruling = adjudicate_mod.Adjudication(
            verdict="settled",
            answer_text=adjudicate_mod.render_answer(1, "go with A"),
            reason="go with A", cost_usd=0.5)
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="0" on-error="debug">'
            '<task>DO-WORK</task></step>', extra='decider="llm"'),
            diagnose=fake_diag,
            adjudicate=lambda *a, **k: ruling)
        ex.run()

        self.assertEqual(len(self.fake.calls), 3)  # request, (b) re-run, debug
        debug_prompt = self.fake.calls[2]["prompt"]
        self.assertIn("## Fix instructions for the previous failure",
                      debug_prompt)
        self.assertIn("add --force", debug_prompt)
        self.assertIn("## Decisions resolved", debug_prompt)
        self.assertIn("option: 1", debug_prompt)
        self.assertIn("two readings of 'merge'", debug_prompt)


class TestIgnoreDoesNotAbsorbAMalformedDecision(ExecutorTestCase):
    """`on-error="ignore"` may not swallow a malformed `DECISION:` payload
    (xml-wf-decision-request.md §19.2; §1 and §15.4-1 state the fail-closed
    rule this restores).

    A well-formed request already stops the run through DecisionRequested no
    matter what `on-error` says, so the malformed side was the only hole: the
    ladder's `ignore` branch recorded `failed-ignored` for every class,
    including `decision`, and the fork the channel exists to surface was
    dropped as silently as picking a branch by hand.
    """

    # Opens with the prefix, so the channel is claimed and the class is
    # `decision` (§1) -- but `options:`, `recommendation:` and `work-state:`
    # are missing, so it can never be answered.
    MALFORMED_PAYLOAD = ("DECISION: which join\n"
                         "fork: two readings of 'merge'\n")

    def test_malformed_decision_fails_an_ignore_step(self):
        self.fake.handlers.append(
            (lambda p: "DO-WORK" in p,
             CliResult(ok=True, text=self.MALFORMED_PAYLOAD, cost_usd=0.02)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" on-error="ignore"><task>DO-WORK</task></step>'
            '<step id="s2" role="w"><task>AFTER-ME</task></step>'))
        with self.assertRaises(WorkflowFailure) as ctx:
            ex.run()
        # The report has to point at the payload a human must read (§1).
        message = str(ctx.exception)
        self.assertIn("s1", message)
        request = self.run_dir / "decisions" / "s1_c01_d01_request.md"
        self.assertTrue(request.is_file())
        self.assertIn(str(request), message)
        # The run stopped there: no retry, no debug, and no later step.
        self.assertEqual(len(self.fake.calls), 1)

    def test_ordinary_failure_on_the_same_step_is_still_ignored(self):
        """Control arm: the guard is narrow -- only the `decision` class."""
        self.fake.handlers.append(
            (lambda p: "DO-WORK" in p,
             CliResult(ok=False, error="the file never appeared",
                       error_class="behavioral", cost_usd=0.01)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" on-error="ignore"><task>DO-WORK</task></step>'
            '<step id="s2" role="w"><task>AFTER-ME</task></step>'))
        ex.run()
        self.assertEqual(len(self.fake.calls), 2)
        self.assertIn("AFTER-ME", self.fake.calls[1]["prompt"])
        ignored = [e for e in load_events(self.run_dir)
                   if e.get("kind") == "step"
                   and e.get("status") == "failed-ignored"]
        self.assertEqual([e["key"] for e in ignored], ["s1"])


if __name__ == "__main__":
    unittest.main()
