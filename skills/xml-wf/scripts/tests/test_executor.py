"""Executor tests with a fake claude runner (no API calls, no cost)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import parser  # noqa: E402
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
                permission_mode=None):
        wf = parser.parse_string(xml, base_dir=self.tmp.name)
        executor = Executor(
            wf, params or {}, self.run_dir, base_dir=self.tmp.name,
            permission_mode=permission_mode,
            replay_events=events,
            run_claude=self.fake,
            ask_llm=ask or fake_ask_factory([]),
            diagnose=diagnose or (lambda *a, **k: Diagnosis("FAIL", "no")),
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
        xml = self.wrap(
            '<replan id="r1" role="builder"><task>PLAN-ME</task></replan>'
            '<step id="tail" role="w"><task>end</task></step>')
        self.fake.handlers.append(
            (lambda p: "PLAN-ME" in p, CliResult(ok=True, text=child, cost_usd=0.01)))
        self.fake.handlers.append(
            (lambda p: "end" in p, CliResult(ok=False, error="ERROR: die", cost_usd=0)))
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
        self.assertIn("end", new[0]["prompt"])

    def test_budget_exhaustion(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="w"><task>a</task></step>'
            '<step id="s2" role="w"><task>b</task></step>',
            extra='budget-usd="0.005"'))
        with self.assertRaises(WorkflowFailure):
            ex.run()  # first step costs 0.01 > budget before second


if __name__ == "__main__":
    unittest.main()
