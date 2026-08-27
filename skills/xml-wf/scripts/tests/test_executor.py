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
        agents_dir = Path(self.tmp.name) / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        for name in ("w", "builder"):
            (agents_dir / f"{name}.md").write_text(
                f"---\nname: {name}\ndescription: test\n---\nROLE-BODY-{name}",
                encoding="utf-8")
        (agents_dir / "coder.md").write_text(
            "---\nname: coder\ndescription: test\nmodel: basic\ntools: Read\n---\n"
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
            adjudicate=adjudicate or _no_adjudicator,
            model_runner=model_runner,
            inherit_model=inherit_model,
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
        p1 = Path(ex.vars["p1"])
        self.assertTrue(p1.is_file())
        self.assertIn("echo:", p1.read_text(encoding="utf-8"))
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
        self.assertIn("Prompt axes in this step:", system)
        self.assertIn("Mode > Rules > Task > Role", system)
        self.assertIn("<role>\nROLE-BODY-w\n</role>", system)
        self.assertIn('<rules id="r1">', system)
        self.assertIn("RULE-BODY", system)
        self.assertLess(system.index("<role>"), system.index('<rules id="r1">'))
        prompt = call["prompt"]
        self.assertIn("ERROR:", prompt)
        self.assertNotIn("<role>", prompt)

    def test_mode_injected_after_role(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" mode="execute"><task>x</task></step>'))
        ex.run()
        system = self.fake.calls[0]["system_prompt"]
        self.assertIn("mode:execute", system)
        self.assertIn("mode-output", system)
        self.assertNotIn("[Mode: current_mode]", system)
        self.assertLess(system.index("</role>"), system.index("mode:execute"))

    def test_role_less_step_drops_role_block_and_role_axis(self):
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
        self.assertIn("mode:execute", system)
        self.assertIn("mode-output", system)
        self.assertIn('<rules id="r1">', system)
        self.assertIn("RULE-BODY", system)

    def test_mode_alias_reads_target_file(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" mode="implement"><task>x</task></step>'))
        ex.run()
        system = self.fake.calls[0]["system_prompt"]
        self.assertIn("mode:implement", system)
        self.assertIn("deviate-from-plan", system)

    def test_inline_role_injected(self):
        ex = self.execute(self.wrap(
            '<step id="s1"><role>INLINE-PERSONA</role><task>x</task></step>'))
        ex.run()
        call = self.fake.calls[0]
        self.assertIn("<role>\nINLINE-PERSONA\n</role>", call["system_prompt"])
        self.assertIsNone(call["model"])
        self.assertIsNone(call["tools"])

    def test_step_flags_forwarded(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="coder" model="ultra" effort="high" tools="Read,Write">'
            '<task>x</task></step>'))
        ex.run()
        call = self.fake.calls[0]
        self.assertNotIn("agent", call)
        self.assertEqual(call["model"], "opus")
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
            ex.run()
        self.assertIn("expect-file", str(ctx.exception))
        (Path(self.tmp.name) / "out.txt").write_text("data", encoding="utf-8")
        self.execute(xml).run()

    def test_model_map_applied_and_recorded(self):
        from wfrun import modelmap
        map_path = Path(self.tmp.name) / "mm.json"
        map_path.write_text(json.dumps({"cc": {"ultra": "mapped-ultra"}}), encoding="utf-8")
        old = modelmap.MAP_PATH
        modelmap.MAP_PATH = map_path
        try:
            ex = self.execute(self.wrap(
                '<step id="s1" role="w" model="ultra"><task>x</task></step>'))
            ex.run()
        finally:
            modelmap.MAP_PATH = old
        self.assertEqual(self.fake.calls[0]["model"], "mapped-ultra")
        mapped = [e for e in load_events(self.run_dir) if e["kind"] == "model-map"]
        self.assertEqual((mapped[0]["canonical"], mapped[0]["resolved"]),
                         ("ultra", "mapped-ultra"))

    def test_model_runner_selects_the_llm_table(self):
        from wfrun import modelmap
        map_path = Path(self.tmp.name) / "mm.json"
        map_path.write_text(
            json.dumps({"cc": {"ultra": "cc-ultra"}, "llm": {"ultra": "llm-ultra"}}),
            encoding="utf-8")
        old = modelmap.MAP_PATH
        modelmap.MAP_PATH = map_path
        try:
            ex = self.execute(self.wrap(
                '<step id="s1" role="w" model="ultra"><task>x</task></step>'),
                model_runner="llm")
            ex.run()
        finally:
            modelmap.MAP_PATH = old
        self.assertEqual(self.fake.calls[0]["model"], "llm-ultra")

    def test_inherit_model_used_when_step_has_no_model(self):
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
        ex = self.execute(self.wrap(
            '<step id="s1" model="ultra"><role>W</role><task>x</task></step>'),
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
            '<step id="s2" model="ultra"><role>W</role><task>y</task></step>'))
        warnings = ex.model_inherit_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("s1", warnings[0])
        self.assertNotIn("s2", warnings[0])
        self.assertIn("--inherit-model", warnings[0])

    def test_model_inherit_warnings_empty_when_every_step_has_a_model(self):
        ex = self.execute(self.wrap(
            '<step id="s1" model="ultra"><role>W</role><task>x</task></step>'))
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
        self.assertEqual(call["model"], "haiku")
        self.assertEqual(call["tools"], "Read")

    def test_missing_named_role_fails(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="ghost"><task>x</task></step>'))
        with self.assertRaises(WorkflowFailure):
            ex.run()

    def test_base_dir_under_claude_config_rejected(self):
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
            self.execute(xml)
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
        self.assertIn("in_a.txt", self.fake.calls[0]["prompt"])
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
        self.assertEqual(len(self.fake.calls), 2)

    def test_guardrail_skips_deterministic_retry_but_allows_debug(self):
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
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(diagnoses, ["s1"])

    def test_refusal_skips_debug(self):
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
        self.assertEqual(diagnoses, [],
                         "a refusal is the model applying a rule it was "
                         "given; debugging it would argue with the rule")

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
        self.assertEqual(diagnoses, [],
                         "a denial is a permission fact of the environment, "
                         "not something a diagnosis can fix")

    def test_transient_retries_but_skips_debug(self):
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
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(diagnoses, [],
                         "transient is an upstream hiccup; handing it to "
                         "debug is the retry-storm path")

    def test_env_skips_retry_and_goes_straight_to_on_error(self):
        self.fake.handlers.append(
            (lambda p: "guarded" in p,
             CliResult(ok=False, error="claude CLI not found on PATH",
                      error_class="env", cost_usd=0)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="3"><task>guarded</task></step>'))
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(len(self.fake.calls), 1,
                         "an env failure does not consume retry: the next "
                         "attempt would fail identically")

    def test_timeout_consumes_retry(self):
        self.fake.handlers.append(
            (lambda p: "flaky" in p,
             CliResult(ok=False, error="timeout after 60s",
                      error_class="timeout", cost_usd=0)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="2"><task>flaky</task></step>'))
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(len(self.fake.calls), 3)

    def test_empty_body_via_fake_run_claude_is_behavioral_and_retries(self):
        self.fake.handlers.append(
            (lambda p: "flaky" in p,
             CliResult(ok=False, error="empty result",
                      error_class="behavioral", cost_usd=0)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="1"><task>flaky</task></step>'))
        with self.assertRaises(WorkflowFailure) as ctx:
            ex.run()
        self.assertIn("empty result", str(ctx.exception))
        self.assertEqual(len(self.fake.calls), 2)

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

        self.fake.handlers.clear()
        events = load_events(self.run_dir)
        ex2 = self.execute(xml, events=events)
        ex2.run()
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
        ask2 = fake_ask_factory([])
        ex2 = self.execute(xml, ask=ask2, events=load_events(self.run_dir))
        ex2.run()
        self.assertEqual(ask2.calls, [])

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
        self.assertEqual(len(self.fake.calls), 3)
        self.assertIn("child work", self.fake.calls[1]["prompt"])
        self.assertIn("after echo:", self.fake.calls[2]["prompt"])
        saved = (self.run_dir / "replans" / "r1_01.xml").read_text(encoding="utf-8")
        self.assertTrue(saved.startswith("<workflow"))
        self.assertIn("MUST NOT contain", self.fake.calls[0]["prompt"])
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
        self._replan_builder_returns_child()
        ex = self.execute(self.wrap(
            '<replan id="r1" role="builder"><task>PLAN-ME</task></replan>'))
        ex.run()
        self.assertEqual(self.fake.calls[0]["system_prompt"],
                         "<role>\nROLE-BODY-builder\n</role>")

    def test_role_less_replan_sends_empty_system(self):
        self._replan_builder_returns_child()
        ex = self.execute(self.wrap(
            '<replan id="r1"><task>PLAN-ME</task></replan>'))
        ex.run()
        self.assertEqual(self.fake.calls[0]["system_prompt"], "")
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
        self.assertIn("replan-forbidden", self.fake.calls[1]["prompt"])
        self.assertEqual(len(self.fake.calls), 3)

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
        self.assertEqual(len(new), 1)
        self.assertIn("TAIL-ME", new[0]["prompt"])

    def test_replan_resume_parses_continuation_against_the_base_dir(self):
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
        self.assertEqual(self.fake.calls[1]["schema"], schema_text)
        first_calls = len(self.fake.calls)

        events = [e for e in load_events(self.run_dir) if e["kind"] == "replan"]
        ex2 = self.execute(xml, events=events)
        ex2.run()
        new = self.fake.calls[first_calls:]
        self.assertEqual(len(new), 1)
        self.assertIn("child work", new[0]["prompt"])
        self.assertEqual(new[0]["schema"], schema_text)

    def test_budget_exhaustion(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="w"><task>a</task></step>'
            '<step id="s2" role="w"><task>b</task></step>',
            extra='budget-usd="0.005"'))
        with self.assertRaises(WorkflowFailure):
            ex.run()


class TestReplanContinuationUnderPiBackend(ExecutorTestCase):
    CHILD_OK = ('<workflow name="c" version="2" max="5">'
                '<step id="c1" role="w"><task>child work</task></step>'
                '</workflow>')
    REPLAN = '<replan id="r1" role="builder"><task>PLAN-ME</task></replan>'
    REPLAN_RETRY = ('<replan id="r1" role="builder" retry="1">'
                    '<task>PLAN-ME</task></replan>')

    def _builder_returns(self, first, second=None):
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
        retry_prompt = self.fake.calls[1]["prompt"]
        self.assertIn("schema= is not allowed on this backend", retry_prompt)
        self.assertIn("expect-file instead", retry_prompt)
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
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w" on-error="debug" '
                 'schema=\'{"type":"object"}\'><task>fine here</task>'
                 '</step></workflow>')
        self._builder_returns(child)
        ex = self.execute(self.wrap(self.REPLAN), backend="cc")
        ex.run()
        self.assertEqual(self._replan_errors(), [])

    def test_pi_model_check_reaches_the_continuation(self):
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
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w" tools="Bash(git:*)">'
                 '<task>child work</task></step></workflow>')
        self._builder_returns(child)
        ex = self.execute(self.wrap(self.REPLAN), backend="cc")
        ex.run()
        self.assertEqual(ex.protocol_warnings, [])


class TestDecisionContextSurvivesDebugRetry(ExecutorTestCase):
    STOPPED_PAYLOAD = ("DECISION: which join\n"
                       "fork: two readings of 'merge'\n"
                       "options:\n"
                       "  1. A -- loses rows\n"
                       "  2. B -- loses columns\n"
                       "recommendation: 1\n"
                       "work-state: stopped")

    def respond_in_order(self, needle, *results):
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

        self.assertEqual(len(self.fake.calls), 3)
        debug_prompt = self.fake.calls[2]["prompt"]
        self.assertIn("## Fix instructions for the previous failure",
                      debug_prompt)
        self.assertIn("add --force", debug_prompt)
        self.assertIn("## Decisions resolved", debug_prompt)
        self.assertIn("option: 1", debug_prompt)
        self.assertIn("two readings of 'merge'", debug_prompt)


class TestIgnoreDoesNotAbsorbAMalformedDecision(ExecutorTestCase):
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
        message = str(ctx.exception)
        self.assertIn("s1", message)
        request = self.run_dir / "decisions" / "s1_c01_d01_request.md"
        self.assertTrue(request.is_file())
        self.assertIn(str(request), message)
        self.assertEqual(len(self.fake.calls), 1)

    def test_ordinary_failure_on_the_same_step_is_still_ignored(self):
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
