import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_executor import ExecutorTestCase  # noqa: E402

from wfrun import adjudicate as adjudicate_mod  # noqa: E402
from wfrun import decision as decision_mod  # noqa: E402
from wfrun import pi_cli, stepio  # noqa: E402
from wfrun.__main__ import AnswerError, _ingest_answers  # noqa: E402
from wfrun.claude_cli import (CliResult, classify_result, is_debuggable,  # noqa: E402
                              is_retryable)
from wfrun.executor import DecisionRequested, WorkflowFailure  # noqa: E402
from wfrun.state import load_events  # noqa: E402


def payload(work_state="complete", output="art.txt", options=("A -- loses rows",
                                                              "B -- loses columns"),
            recommendation="1", summary="which join", fork="two readings of 'merge'"):
    lines = [f"DECISION: {summary}", f"fork: {fork}", "options:"]
    lines += [f"  {i}. {opt}" for i, opt in enumerate(options, start=1)]
    lines.append(f"recommendation: {recommendation}")
    lines.append(f"work-state: {work_state}")
    if output is not None:
        lines.append(f"output: {output}")
    return "\n".join(lines)


class TestPayloadGrammar(unittest.TestCase):
    def test_valid(self):
        parsed, errors = decision_mod.parse_payload(payload())
        self.assertEqual(errors, [])
        self.assertEqual(parsed.work_state, "complete")
        self.assertEqual(parsed.recommendation, 1)
        self.assertEqual(parsed.output, "art.txt")
        self.assertEqual(len(parsed.options), 2)

    def test_wrapped_fork_and_option_lines_are_joined(self):
        text = ("DECISION: x\nfork: first line\nsecond line\noptions:\n"
                "  1. A\n     continued\n  2. B\nrecommendation: none\n"
                "work-state: stopped")
        parsed, errors = decision_mod.parse_payload(text)
        self.assertEqual(errors, [])
        self.assertEqual(parsed.fork, "first line\nsecond line")
        self.assertEqual(parsed.options[0], "A continued")
        self.assertIsNone(parsed.recommendation)

    def test_malformed_cases(self):
        cases = {
            "missing key": "DECISION: x\nfork: f\noptions:\n  1. A\n  2. B",
            "one option": payload(options=("only",)),
            "non-sequential": ("DECISION: x\nfork: f\noptions:\n  1. A\n  3. B\n"
                               "recommendation: none\nwork-state: stopped"),
            "bad work-state": payload(work_state="donezo"),
            "not a decision": "ERROR: nope",
        }
        for label, text in cases.items():
            with self.subTest(label):
                parsed, errors = decision_mod.parse_payload(text)
                self.assertIsNone(parsed)
                self.assertTrue(errors)


class TestAnswerGrammar(unittest.TestCase):
    def test_valid_number_and_none(self):
        answer, errors = decision_mod.parse_answer("option: 2\nbecause B is safer", 2)
        self.assertEqual((errors, answer.option), ([], 2))
        answer, errors = decision_mod.parse_answer("option: none\ndo C instead", 2)
        self.assertEqual((errors, answer.option), ([], None))

    def test_three_rejections(self):
        cases = {
            "no option line": "just prose about the fork",
            "out of range": "option: 5\nwhy",
            "none without text": "option: none\n   \n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                answer, errors = decision_mod.parse_answer(text, 2)
                self.assertIsNone(answer)
                self.assertTrue(errors)


class TestClassification(unittest.TestCase):
    def _stdout(self, text):
        return json.dumps({"result": text, "total_cost_usd": 0.01})

    def test_decision_class_is_neither_retryable_nor_debuggable(self):
        self.assertFalse(is_retryable("decision"))
        self.assertFalse(is_debuggable("decision"))

    def test_classify_result_assigns_decision(self):
        res = classify_result(0, self._stdout(payload()), "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "decision")

    def test_schema_step_still_classifies_as_decision(self):
        res = classify_result(0, self._stdout(payload()), "", schema='{"type":"object"}')
        self.assertEqual(res.error_class, "decision")

    def test_malformed_payload_keeps_the_decision_class(self):
        res = classify_result(0, self._stdout("DECISION: only a summary"), "")
        self.assertEqual(res.error_class, "decision")
        self.assertFalse(is_retryable(res.error_class))

    def test_pi_backend_classifies_the_same(self):
        stdout = json.dumps({
            "type": "turn_end",
            "message": {"stopReason": "stop",
                        "content": [{"type": "text", "text": payload()}],
                        "usage": {"cost": {"total": 0.0}}}})
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertEqual(res.error_class, "decision")

    def test_error_and_blocked_prefixes_still_win(self):
        for text, expected in (("ERROR: x", "guardrail"),
                               ("[BLOCKED: mode-rule x]", "refusal")):
            with self.subTest(expected):
                res = classify_result(0, self._stdout(text), "")
                self.assertEqual(res.error_class, expected)


class DecisionExecutorTestCase(ExecutorTestCase):
    def respond_decision(self, needle, *, then_ok=False, **payload_kwargs):
        state = {"n": 0}

        def predicate(prompt):
            if needle not in prompt:
                return False
            state["n"] += 1
            return not (then_ok and state["n"] > 1)

        self.fake.handlers.append(
            (predicate, CliResult(ok=True, text=payload(**payload_kwargs),
                                  cost_usd=0.02)))

    def artifact(self, name="art.txt"):
        path = Path(self.tmp.name) / name
        path.write_text("deliverable", encoding="utf-8")
        return path

    def decision_events(self, valid_only=True):
        return [e for e in load_events(self.run_dir)
                if e.get("kind") == "decision"
                and (e.get("valid") is not False or not valid_only)]

    def answer(self, run_dir, step_id, body):
        path = Path(self.tmp.name) / f"{step_id}_answer.md"
        path.write_text(body, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            return _ingest_answers(run_dir, [f"{step_id}={path}"],
                                   load_events(run_dir))


class TestBatchStop(DecisionExecutorTestCase):
    def test_valid_request_stops_and_records(self):
        self.artifact()
        self.respond_decision("DO-WORK")
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="2" expect-file="art.txt">'
            '<task>DO-WORK</task></step>'
            '<step id="s2" role="w"><task>later</task></step>'))
        with self.assertRaises(DecisionRequested) as ctx:
            ex.run()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(len(ctx.exception.requests), 1)

        record = self.decision_events()[0]
        self.assertEqual(record["request_id"], "s1_c01_d01")
        self.assertTrue(Path(record["request"]).is_file())
        self.assertTrue(record["a_eligible"])
        self.assertIsNone(record["b_reason"])
        self.assertTrue(Path(record["expect_files"][0]).is_absolute())
        snapshot = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["status"], "awaiting-decision")
        self.assertEqual(snapshot["decisions"], ["s1_c01_d01"])

    def test_malformed_payload_fails_without_retry(self):
        self.fake.handlers.append(
            (lambda p: "DO-WORK" in p,
             CliResult(ok=True, text="DECISION: no fields at all", cost_usd=0.02)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="2"><task>DO-WORK</task></step>'))
        with self.assertRaises(WorkflowFailure) as ctx:
            ex.run()
        self.assertIn("malformed", str(ctx.exception))
        self.assertEqual(len(self.fake.calls), 1)
        self.assertFalse(self.decision_events(valid_only=True))
        self.assertTrue(self.decision_events(valid_only=False))

    def test_on_error_ignore_does_not_absorb_a_malformed_payload(self):
        self.fake.handlers.append(
            (lambda p: "bad" in p,
             CliResult(ok=True, text="DECISION: no fields", cost_usd=0.02)))
        ex = self.execute(self.wrap(
            '<step id="bad" role="w" on-error="ignore"><task>bad</task></step>'))
        with self.assertRaises(WorkflowFailure):
            ex.run()

    def test_on_error_ignore_does_not_absorb_a_real_request(self):
        self.respond_decision("good", work_state="stopped", output=None)
        ex = self.execute(self.wrap(
            '<step id="good" role="w" on-error="ignore"><task>good</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()


class TestNoOutputDegrades(DecisionExecutorTestCase):
    def test_payload_parses_and_keeps_the_fork_answerable(self):
        parsed, errors = decision_mod.parse_payload(payload(output=None))
        self.assertEqual(errors, [])
        self.assertIsNone(parsed.output)
        self.assertTrue(parsed.work_complete)

    def test_batch_stops_with_a_ruling_still_possible(self):
        self.artifact()
        self.respond_decision("DO-WORK", output=None)
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()
        record = self.decision_events()[0]
        self.assertTrue(record["valid"])
        self.assertFalse(record["a_eligible"])
        self.assertEqual(record["b_reason"], decision_mod.B_REASON_NO_OUTPUT)

    def test_batch_answer_re_runs_instead_of_failing(self):
        self.artifact()
        self.respond_decision("DO-WORK", then_ok=True, output=None)
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()
        events = self.answer(self.run_dir, "s1", "option: 1\ngo with A")
        answer = [e for e in events if e.get("kind") == "answer"][0]
        self.assertEqual(answer["verdict"], "b")
        self.assertEqual(answer["b_reason"], decision_mod.B_REASON_NO_OUTPUT)
        resumed = self.execute(self.wrap(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>'), events=events)
        resumed.run()
        self.assertEqual(len(self.fake.calls), 2)

    def test_llm_decider_settles_it_in_process(self):
        self.artifact()
        self.respond_decision("DO-WORK", then_ok=True, output=None)
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>', extra='decider="llm"'),
            adjudicate=fake_decider(ruling(option=1)))
        ex.run()
        self.assertEqual(len(self.fake.calls), 2)
        answer = [e for e in load_events(self.run_dir)
                  if e.get("kind") == "answer"][0]
        self.assertEqual(answer["b_reason"], decision_mod.B_REASON_NO_OUTPUT)


class TestValueOutputDegrades(DecisionExecutorTestCase):
    VALUE_STEP = ('<step id="s1" role="w" expect-file="art.txt" output="v" '
                  'output-type="value"><task>DO-WORK</task></step>')

    def test_batch_demotes_a_value_typed_step(self):
        self.artifact()
        self.respond_decision("DO-WORK", output="the figure is undecided")
        ex = self.execute(self.wrap(self.VALUE_STEP))
        with self.assertRaises(DecisionRequested):
            ex.run()
        record = self.decision_events()[0]
        self.assertFalse(record["a_eligible"])
        self.assertEqual(record["b_reason"], decision_mod.B_REASON_VALUE_OUTPUT)

    def test_batch_agreeing_with_the_recommendation_still_re_runs(self):
        self.artifact()
        self.respond_decision("DO-WORK", then_ok=True,
                              output="the figure is undecided")
        ex = self.execute(self.wrap(self.VALUE_STEP))
        with self.assertRaises(DecisionRequested):
            ex.run()
        events = self.answer(self.run_dir, "s1", "option: 1\nagreed, A")
        answer = [e for e in events if e.get("kind") == "answer"][0]
        self.assertEqual(answer["verdict"], "b")
        self.assertEqual(answer["b_reason"], decision_mod.B_REASON_VALUE_OUTPUT)

    def test_the_file_typed_twin_still_reaches_form_a(self):
        self.artifact()
        self.respond_decision("DO-WORK")
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()
        record = self.decision_events()[0]
        self.assertTrue(record["a_eligible"])
        self.assertIsNone(record["b_reason"])

    def test_a_step_with_no_output_is_not_demoted_for_a_missing_value(self):
        self.artifact()
        self.respond_decision("DO-WORK", output=None)
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" expect-file="art.txt">'
            '<task>DO-WORK</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()
        record = self.decision_events()[0]
        self.assertTrue(record["a_eligible"])
        self.assertIsNone(record["b_reason"])


class TestBReasons(DecisionExecutorTestCase):
    def _reason_at_stop(self, step_xml, **payload_kwargs):
        self.respond_decision("DO-WORK", **payload_kwargs)
        ex = self.execute(self.wrap(step_xml))
        with self.assertRaises(DecisionRequested):
            ex.run()
        return self.decision_events()[0]["b_reason"]

    def test_work_state_stopped(self):
        self.assertEqual(
            self._reason_at_stop('<step id="s1" role="w" expect-file="art.txt">'
                                 '<task>DO-WORK</task></step>',
                                 work_state="stopped", output=None),
            decision_mod.B_REASON_WORK_STATE_STOPPED)

    def test_schema_step(self):
        self.artifact()
        self.assertEqual(
            self._reason_at_stop(
                '<step id="s1" role="w" expect-file="art.txt" '
                'schema=\'{"type":"object"}\'><task>DO-WORK</task></step>'),
            decision_mod.B_REASON_SCHEMA_STEP)

    def test_no_expect_file(self):
        self.assertEqual(
            self._reason_at_stop('<step id="s1" role="w"><task>DO-WORK</task></step>'),
            decision_mod.B_REASON_NO_EXPECT_FILE)

    def test_missing_file(self):
        self.assertEqual(
            self._reason_at_stop('<step id="s1" role="w" expect-file="art.txt">'
                                 '<task>DO-WORK</task></step>'),
            decision_mod.B_REASON_MISSING_FILE)

    def test_unlisted_option_is_decided_at_resume(self):
        self.artifact()
        self.respond_decision("DO-WORK", then_ok=True)
        ex = self.execute(self.wrap('<step id="s1" role="w" expect-file="art.txt" '
                                    'output="v">'
                                    '<task>DO-WORK</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()
        events = self.answer(self.run_dir, "s1", "option: none\ndo something else")
        answer_event = [e for e in events if e.get("kind") == "answer"][-1]
        self.assertEqual(answer_event["verdict"], "b")
        self.assertEqual(answer_event["b_reason"],
                         decision_mod.B_REASON_UNLISTED_OPTION)

    def test_missing_file_at_resume(self):
        artifact = self.artifact()
        self.respond_decision("DO-WORK", then_ok=True)
        ex = self.execute(self.wrap('<step id="s1" role="w" expect-file="art.txt" '
                                    'output="v">'
                                    '<task>DO-WORK</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()
        self.assertTrue(self.decision_events()[0]["a_eligible"])
        artifact.unlink()
        events = self.answer(self.run_dir, "s1", "option: 1\nkeep going")
        answer_event = [e for e in events if e.get("kind") == "answer"][-1]
        self.assertEqual(answer_event["verdict"], "b")
        self.assertEqual(answer_event["b_reason"],
                         decision_mod.B_REASON_MISSING_FILE_AT_RESUME)


class TestResumePaths(DecisionExecutorTestCase):
    def test_form_a_synthesizes_and_never_re_runs(self):
        self.artifact()
        self.respond_decision("DO-WORK")
        xml = self.wrap('<step id="s1" role="w" expect-file="art.txt" output="v" '
                        '><task>DO-WORK</task></step>'
                        '<step id="s2" role="w"><task>later</task></step>')
        ex = self.execute(xml)
        with self.assertRaises(DecisionRequested):
            ex.run()
        calls_before = len(self.fake.calls)

        events = self.answer(self.run_dir, "s1", "option: 1\nA is right")
        self.assertEqual([e for e in events if e.get("kind") == "answer"][-1]["verdict"], "a")

        ex2 = self.execute(xml, events=events)
        ex2.run()
        new = self.fake.calls[calls_before:]
        self.assertEqual(len(new), 1)
        self.assertIn("later", new[0]["prompt"])
        self.assertEqual(ex2.vars["v"], "art.txt")

    def test_batch_answer_against_the_recommendation_forces_a_re_run(self):
        self.artifact()
        self.respond_decision("DO-WORK", then_ok=True, recommendation="2")
        xml = self.wrap('<step id="s1" role="w" expect-file="art.txt" output="v" '
                        '><task>DO-WORK</task></step>')
        ex = self.execute(xml)
        with self.assertRaises(DecisionRequested):
            ex.run()
        events = self.answer(self.run_dir, "s1", "option: 1\nI disagree")
        answer_event = [e for e in events if e.get("kind") == "answer"][-1]
        self.assertEqual(answer_event["verdict"], "b")
        self.assertEqual(answer_event["b_reason"],
                         decision_mod.B_REASON_OPTION_NOT_RECOMMENDED)
        self.assertFalse(
            [e for e in events if e.get("via") == "decision-a"],
            "no synthetic success may be appended for an output: the ruling "
            "did not endorse")

    def test_form_b_re_runs_carrying_request_and_answer(self):
        self.respond_decision("DO-WORK", then_ok=True, work_state="stopped", output=None)
        xml = self.wrap('<step id="s1" role="w"><task>DO-WORK</task></step>')
        ex = self.execute(xml)
        with self.assertRaises(DecisionRequested):
            ex.run()
        calls_before = len(self.fake.calls)

        events = self.answer(self.run_dir, "s1", "option: 2\nB, because rows matter")
        ex2 = self.execute(xml, events=events)
        ex2.run()
        new = self.fake.calls[calls_before:]
        self.assertEqual(len(new), 1)
        prompt = new[0]["prompt"]
        self.assertIn("## Decisions resolved", prompt)
        self.assertIn("B, because rows matter", prompt)
        self.assertIn("two readings of 'merge'", prompt)
        self.assertNotIn("Decisions resolved", new[0]["system_prompt"])

    def test_second_fork_in_same_cycle_keeps_the_first_ruling_visible(self):
        state = {"n": 0}

        def predicate(prompt):
            if "DO-WORK" not in prompt:
                return False
            state["n"] += 1
            return state["n"] <= 2

        def result_for(prompt):
            fork = ("first fork" if state["n"] == 1 else "second fork")
            return CliResult(ok=True, text=payload(work_state="stopped",
                                                   output=None, fork=fork),
                             cost_usd=0.02)

        def fake_call(prompt, **kwargs):
            self.fake.calls.append({"prompt": prompt, **kwargs})
            if predicate(prompt):
                return result_for(prompt)
            return CliResult(ok=True, text="settled", cost_usd=0.01)

        xml = self.wrap('<step id="s1" role="w"><task>DO-WORK</task></step>')
        from wfrun import parser
        from wfrun.executor import Executor
        from wfrun.adp import Diagnosis

        def build(events=None):
            wf = parser.parse_string(xml, base_dir=self.tmp.name)
            return Executor(wf, {}, self.run_dir, base_dir=self.tmp.name,
                            replay_events=events, run_claude=fake_call,
                            ask_llm=lambda *a, **k: (True, "", 0.0),
                            diagnose=lambda *a, **k: Diagnosis("FAIL", "no"))

        with self.assertRaises(DecisionRequested):
            build().run()
        a1 = Path(self.tmp.name) / "a1.md"
        a1.write_text("option: 1\nfirst ruling", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            events = _ingest_answers(self.run_dir, [f"s1={a1}"],
                                     load_events(self.run_dir))
        with self.assertRaises(DecisionRequested):
            build(events).run()

        self.assertEqual(self.decision_events()[-1]["request_id"], "s1_c01_d02")
        a2 = Path(self.tmp.name) / "a2.md"
        a2.write_text("option: 2\nsecond ruling", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            events = _ingest_answers(self.run_dir, [f"s1={a2}"],
                                     load_events(self.run_dir))
        build(events).run()

        final_prompt = self.fake.calls[-1]["prompt"]
        for needle in ("first fork", "first ruling", "second fork",
                       "second ruling", "### Request 2"):
            self.assertIn(needle, final_prompt)

    def test_bare_resume_re_stops_at_no_cost_and_is_idempotent(self):
        self.respond_decision("DO-WORK", work_state="stopped", output=None)
        xml = self.wrap('<step id="s1" role="w"><task>DO-WORK</task></step>')
        ex = self.execute(xml)
        with self.assertRaises(DecisionRequested):
            ex.run()
        calls_before = len(self.fake.calls)
        events_before = len(load_events(self.run_dir))

        for _ in range(2):
            ex2 = self.execute(xml, events=load_events(self.run_dir))
            with self.assertRaises(DecisionRequested):
                ex2.run()
            self.assertEqual(len(self.fake.calls), calls_before)

        self.assertEqual(len(self.decision_events()), 1,
                         "a re-raise re-prints the same request; it may not "
                         "advance the ledger")
        appended = load_events(self.run_dir)[events_before:]
        self.assertTrue(all(e["kind"] == "run" for e in appended))

    def test_re_answering_an_answered_request_is_refused(self):
        self.artifact()
        self.respond_decision("DO-WORK", then_ok=True)
        ex = self.execute(self.wrap('<step id="s1" role="w" expect-file="art.txt">'
                                    '<task>DO-WORK</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()
        self.answer(self.run_dir, "s1", "option: 1\nfirst answer")
        with self.assertRaises(AnswerError) as ctx:
            self.answer(self.run_dir, "s1", "option: 2\nchanged my mind")
        self.assertIn("already answered", str(ctx.exception))

    def test_answer_parser_rejections_reach_the_cli_layer(self):
        self.respond_decision("DO-WORK", work_state="stopped", output=None)
        ex = self.execute(self.wrap('<step id="s1" role="w"><task>DO-WORK</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()
        for body in ("prose with no option line", "option: 9\nout of range",
                     "option: none\n"):
            with self.subTest(body):
                with self.assertRaises(AnswerError):
                    self.answer(self.run_dir, "s1", body)
        with self.assertRaises(AnswerError):
            self.answer(self.run_dir, "nosuch", "option: 1\nx")


class TestParallel(DecisionExecutorTestCase):
    def _parallel_xml(self):
        return self.wrap(
            '<parallel max-workers="2">'
            '<step id="p1" role="w"><task>alpha</task></step>'
            '<step id="p2" role="w"><task>beta</task></step>'
            '</parallel>')

    def test_all_siblings_are_collected(self):
        self.respond_decision("alpha", work_state="stopped", output=None)
        self.respond_decision("beta", work_state="stopped", output=None)
        ex = self.execute(self._parallel_xml())
        with self.assertRaises(DecisionRequested) as ctx:
            ex.run()
        self.assertEqual({r["key"] for r in ctx.exception.requests}, {"p1", "p2"})

    def test_partial_answer_advances_only_the_answered_sibling(self):
        self.respond_decision("alpha", then_ok=True, work_state="stopped", output=None)
        self.respond_decision("beta", work_state="stopped", output=None)
        xml = self._parallel_xml()
        ex = self.execute(xml)
        with self.assertRaises(DecisionRequested):
            ex.run()
        calls_before = len(self.fake.calls)

        events = self.answer(self.run_dir, "p1", "option: 1\ngo with A")
        ex2 = self.execute(xml, events=events)
        with self.assertRaises(DecisionRequested) as ctx:
            ex2.run()
        new = self.fake.calls[calls_before:]
        self.assertEqual(len(new), 1)
        self.assertIn("alpha", new[0]["prompt"])
        self.assertEqual([r["key"] for r in ctx.exception.requests], ["p2"])

    def test_partial_answer_form_a_synthesizes_without_any_call(self):
        self.artifact()
        self.respond_decision("alpha")
        self.respond_decision("beta")
        xml = self.wrap(
            '<parallel max-workers="2">'
            '<step id="p1" role="w" expect-file="art.txt" output="va" '
            '><task>alpha</task></step>'
            '<step id="p2" role="w" expect-file="art.txt" output="vb" '
            '><task>beta</task></step>'
            '</parallel>')
        ex = self.execute(xml)
        with self.assertRaises(DecisionRequested):
            ex.run()
        calls_before = len(self.fake.calls)

        events = self.answer(self.run_dir, "p1", "option: 1\ngo with A")
        ex2 = self.execute(xml, events=events)
        with self.assertRaises(DecisionRequested) as ctx:
            ex2.run()
        self.assertEqual(len(self.fake.calls), calls_before,
                         "neither the synthesized sibling nor the pending one "
                         "may spend a CLI call")
        self.assertEqual([r["key"] for r in ctx.exception.requests], ["p2"])
        self.assertEqual(ex2.vars["va"], "art.txt")

    def test_a_failing_sibling_outranks_a_decision(self):
        self.respond_decision("alpha", work_state="stopped", output=None)
        self.fake.handlers.append(
            (lambda p: "beta" in p, CliResult(ok=False, error="ERROR: died",
                                              error_class="guardrail", cost_usd=0)))
        ex = self.execute(self._parallel_xml())
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual([r["key"] for r in ex.decisions_raised], ["p1"])
        self.assertTrue(Path(self.decision_events()[0]["request"]).is_file())


class TestCycleIdentity(DecisionExecutorTestCase):
    def test_cycle_counts_visits_not_attempts(self):
        state = {"n": 0}

        def predicate(prompt):
            if "loop" not in prompt:
                return False
            state["n"] += 1
            return state["n"] >= 2

        self.fake.handlers.append(
            (predicate, CliResult(ok=True, text=payload(work_state="stopped",
                                                        output=None), cost_usd=0.02)))
        ex = self.execute(self.wrap(
            '<each range="3" as="k"><do>'
            '<step id="s1" role="w"><task>loop</task></step>'
            '</do></each>'))
        with self.assertRaises(DecisionRequested):
            ex.run()
        self.assertEqual(self.decision_events()[0]["request_id"], "s1_c02_d01")
        self.assertEqual(self.decision_events()[0]["cycle"], 2)


def ruling(verdict="settled", option=1, text="because", raw=None, cost=0.5):
    answer_text = (adjudicate_mod.render_answer(option, text)
                   if verdict == "settled" else None)
    return adjudicate_mod.Adjudication(
        verdict=verdict, answer_text=answer_text, reason=text, raw=raw,
        cost_usd=cost)


def fake_decider(*rulings, calls=None):
    queue = list(rulings)

    def _adjudicate(step_id, request_body, option_count, **kwargs):
        if calls is not None:
            calls.append({"step_id": step_id, "request": request_body,
                          "option_count": option_count, **kwargs})
        if not queue:
            raise AssertionError("the decider was called more times than the "
                                 "test provided rulings for")
        return queue.pop(0)

    return _adjudicate


class TestLlmAdjudication(DecisionExecutorTestCase):
    def llm(self, inner, *rulings, calls=None, extra='decider="llm"'):
        return self.execute(self.wrap(inner, extra=extra),
                            adjudicate=fake_decider(*rulings, calls=calls))

    def test_escalation_stops_for_a_human_without_re_running(self):
        calls = []
        self.respond_decision("DO-WORK")
        ex = self.llm('<step id="s1" role="w"><task>DO-WORK</task></step>',
                      ruling(verdict="escalate", text="irreversible: it emails"),
                      calls=calls)
        with self.assertRaises(DecisionRequested):
            ex.run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.fake.calls), 1)
        record = self.decision_events()[0]
        self.assertTrue(record["escalated"])
        self.assertIn("irreversible", record["adjudication_note"])
        self.assertFalse(Path(record["answer_path"]).exists())
        self.assertEqual(record["decider"], "human")

    def test_malformed_payload_never_reaches_the_decider(self):
        calls = []
        self.fake.handlers.append(
            (lambda p: "DO-WORK" in p,
             CliResult(ok=True, text="DECISION: no fields at all", cost_usd=0.02)))
        ex = self.llm('<step id="s1" role="w"><task>DO-WORK</task></step>',
                      calls=calls)
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(calls, [])

    def test_unusable_ruling_falls_back_and_keeps_the_evidence(self):
        self.respond_decision("DO-WORK")
        ex = self.llm('<step id="s1" role="w"><task>DO-WORK</task></step>',
                      ruling(verdict="failed", raw="option: 9\n\nbeyond the list",
                             text="'option: 9' is outside the 1..2 options"))
        with self.assertRaises(DecisionRequested):
            ex.run()
        record = self.decision_events()[0]
        self.assertEqual(record["decider"], "human")
        self.assertIn("outside", record["adjudication_error"])
        rejected = Path(record["adjudication_rejected"])
        self.assertEqual(rejected.name, "s1_c01_d01_llm-attempt01.md")
        self.assertIn("option: 9", rejected.read_text(encoding="utf-8"))
        self.assertFalse(Path(record["answer_path"]).exists())

    def test_cap_hands_the_third_fork_of_a_visit_to_a_human(self):
        calls = []
        self.fake.handlers.append(
            (lambda p: "DO-WORK" in p,
             CliResult(ok=True, text=payload(work_state="stopped", output=None),
                       cost_usd=0.02)))
        ex = self.llm('<step id="s1" role="w"><task>DO-WORK</task></step>',
                      ruling(option=1), ruling(option=2), calls=calls)
        with self.assertRaises(DecisionRequested):
            ex.run()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.fake.calls), 3)
        events = self.decision_events()
        self.assertEqual([e["decider"] for e in events],
                         ["llm", "llm", "human"])
        self.assertTrue(events[2]["cap_reached"])
        self.assertNotIn("adjudication_cost_usd", events[2])

    def test_adjudication_cost_reaches_the_budget_check(self):
        self.artifact()
        self.respond_decision("DO-WORK")
        ex = self.llm(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>'
            '<step id="s2" role="w"><task>later</task></step>',
            ruling(option=1, cost=0.5),
            extra='decider="llm" budget-usd="0.4"')
        with self.assertRaises(WorkflowFailure) as ctx:
            ex.run()
        self.assertIn("budget-usd", str(ctx.exception))
        self.assertIn("before 's2'", str(ctx.exception))
        self.assertGreaterEqual(ex.cost_usd, 0.5)
        self.assertEqual(self.decision_events()[0]["adjudication_cost_usd"], 0.5)

    def test_human_answers_never_spend_the_cap(self):
        from wfrun.executor import decision_tables
        events = [{"kind": "decision", "key": "s1", "cycle": 1, "seq": n,
                   "valid": True, "request_id": f"s1_c01_d{n:02d}",
                   "decider": "human"} for n in (1, 2, 3)]
        self.assertEqual(decision_tables(events)[3], {})
        self.respond_decision("DO-WORK")
        ex = self.execute(self.wrap(
            '<step id="s1" role="w"><task>DO-WORK</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()

    def test_form_a_continues_in_process_and_replays_as_a_hit(self):
        self.artifact()
        self.respond_decision("DO-WORK")
        ex = self.llm(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>',
            ruling(option=1))
        ex.run()
        self.assertEqual(ex.vars["v"], "art.txt")
        self.assertEqual(len(self.fake.calls), 1)
        success = [e for e in load_events(self.run_dir)
                   if e.get("kind") == "step" and e.get("status") == "success"]
        self.assertEqual(success[0]["via"], "decision-a")
        answer = [e for e in load_events(self.run_dir) if e.get("kind") == "answer"]
        self.assertEqual(answer[0]["decider"], "llm")
        self.assertEqual(answer[0]["verdict"], "a")
        self.assertTrue(Path(answer[0]["answer_path"]).read_text(
            encoding="utf-8").startswith("option: 1"))
        replayed = self.execute(self.wrap(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>', extra='decider="llm"'),
            events=load_events(self.run_dir))
        replayed.run()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(replayed.vars["v"], "art.txt")

    def test_form_b_re_runs_in_place_carrying_every_ruling(self):
        self.respond_decision("DO-WORK", then_ok=True,
                              work_state="stopped", output=None)
        ex = self.llm('<step id="s1" role="w" retry="1"><task>DO-WORK</task></step>',
                      ruling(option=2, text="take B"))
        ex.run()
        self.assertEqual(len(self.fake.calls), 2)
        self.assertIn("take B", self.fake.calls[1]["prompt"])
        self.assertIn("DECISION:", self.fake.calls[1]["prompt"])
        self.assertFalse([e for e in load_events(self.run_dir)
                          if e.get("status") == "attempt-failed"])

    def test_form_b_does_not_spend_the_retry_budget(self):
        seen = {"n": 0}

        def first_call_only(prompt):
            if "DO-WORK" not in prompt:
                return False
            seen["n"] += 1
            return seen["n"] == 1

        self.fake.handlers.append(
            (first_call_only,
             CliResult(ok=True, text=payload(work_state="stopped", output=None),
                       cost_usd=0.02)))
        self.fake.handlers.append(
            (lambda p: "DO-WORK" in p,
             CliResult(ok=False, error_class="behavioral", error="flaky",
                       cost_usd=0.01)))
        ex = self.llm('<step id="s1" role="w" retry="1"><task>DO-WORK</task></step>',
                      ruling(option=2))
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(len(self.fake.calls), 3,
                         "the decision, the granted re-run, and the retry "
                         "the re-run must not have eaten")


class FakeTextRunner:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def __call__(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if not self.queue:
            raise AssertionError("the text adjudicator was called more times "
                                 "than the test provided responses for")
        nxt = self.queue.pop(0)
        if isinstance(nxt, CliResult):
            return nxt
        return CliResult(ok=True, text=nxt, cost_usd=0.03)


class TestPiTextAdjudication(DecisionExecutorTestCase):
    def pi_run(self, inner, *responses, extra='decider="llm"'):
        self.runner = FakeTextRunner(*responses)
        return self.execute(
            self.wrap(inner, extra=extra),
            adjudicate=lambda *a, **k: adjudicate_mod.adjudicate_text(
                *a, runner=self.runner, **k))

    def test_settled_ruling_continues_the_run(self):
        self.artifact()
        self.respond_decision("DO-WORK")
        ex = self.pi_run(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>',
            "option: 1\n\ngo with the gross figure")
        ex.run()
        self.assertEqual(ex.vars["v"], "art.txt")
        self.assertEqual(len(self.fake.calls), 1)
        answer = [e for e in load_events(self.run_dir)
                  if e.get("kind") == "answer"][0]
        self.assertEqual(answer["decider"], "llm")
        self.assertTrue(Path(answer["answer_path"]).read_text(
            encoding="utf-8").startswith("option: 1"))

    def test_form_b_re_runs(self):
        self.respond_decision("DO-WORK", then_ok=True,
                              work_state="stopped", output=None)
        ex = self.pi_run('<step id="s1" role="w"><task>DO-WORK</task></step>',
                         "option: 2\n\ntake B")
        ex.run()
        self.assertEqual(len(self.fake.calls), 2)
        self.assertIn("take B", self.fake.calls[1]["prompt"])

    def test_escalate_line_stops_for_a_human(self):
        self.respond_decision("DO-WORK")
        ex = self.pi_run('<step id="s1" role="w"><task>DO-WORK</task></step>',
                         "escalate: outward-facing\n\nit bills a customer")
        with self.assertRaises(DecisionRequested):
            ex.run()
        record = self.decision_events()[0]
        self.assertTrue(record["escalated"])
        self.assertIn("outward-facing", record["adjudication_note"])
        self.assertIn("bills a customer", record["adjudication_note"])
        self.assertFalse(Path(record["answer_path"]).exists())

    def test_escalate_is_case_insensitive(self):
        ruling = adjudicate_mod.adjudicate_text(
            "s1", "req", 2, runner=FakeTextRunner("ESCALATE: uncertain"))
        self.assertEqual(ruling.verdict, "escalate")

    def test_unusable_rulings_fall_back_to_a_human(self):
        cases = {
            "preamble": "Here is my ruling.\n\noption: 1\nbecause",
            "out of range": "option: 9\n\nbeyond the list",
            "none without text": "option: none\n",
            "not a ruling": "I think option 1 is best.",
            "empty": "   ",
        }
        for label, text in cases.items():
            with self.subTest(label):
                ruling = adjudicate_mod.adjudicate_text(
                    "s1", "req", 2, runner=FakeTextRunner(text))
                self.assertEqual(ruling.verdict, "failed")
        classified = CliResult(ok=False, text="DECISION: quoted back",
                               cost_usd=0.01)
        classified.error_class = "decision"
        ruling = adjudicate_mod.adjudicate_text(
            "s1", "req", 2, runner=FakeTextRunner(classified))
        self.assertEqual(ruling.verdict, "failed")
        self.assertIn("decision", ruling.reason)

    def test_failed_ruling_keeps_the_evidence(self):
        self.respond_decision("DO-WORK")
        ex = self.pi_run('<step id="s1" role="w"><task>DO-WORK</task></step>',
                         "option: 9\n\nbeyond the list")
        with self.assertRaises(DecisionRequested):
            ex.run()
        record = self.decision_events()[0]
        self.assertEqual(record["decider"], "human")
        self.assertTrue(Path(record["adjudication_rejected"]).is_file())
        self.assertFalse(Path(record["answer_path"]).exists())

    def test_prompt_and_model_reach_the_runner(self):
        adjudicate_mod.adjudicate_text(
            "s1", "THE-REQUEST-BODY", 2,
            runner=(runner := FakeTextRunner("option: 1\n\nfine")),
            model="google/gemini-3.1-flash-lite")
        call = runner.calls[0]
        self.assertIn("THE-REQUEST-BODY", call["prompt"])
        self.assertNotIn("schema", call)
        self.assertEqual(call["model"], "google/gemini-3.1-flash-lite")
        self.assertEqual(call["tools"], adjudicate_mod.DECIDE_TOOLS)


class TestPiBackendWiring(unittest.TestCase):
    def test_pi_backend_gets_the_text_adjudicator(self):
        from wfrun.__main__ import _backend_executor_kwargs
        self.assertIs(_backend_executor_kwargs("cc")["adjudicate"],
                      adjudicate_mod.adjudicate)
        self.assertIs(_backend_executor_kwargs("pi")["adjudicate"],
                      adjudicate_mod.adjudicate_pi)


class TestDeciderLint(unittest.TestCase):
    def _lint(self, xml):
        from wfrun import lint as lint_mod
        from wfrun import parser
        wf = parser.parse_string(xml)
        return lint_mod.lint(wf, check_roles=False)

    def _codes(self, xml):
        return {f.code for f in self._lint(xml)}

    def test_llm_passes_lint_now_that_it_is_implemented(self):
        for xml in (
            '<workflow name="t" version="2" max="5" decider="llm">'
            '<step id="s1" tools="Read"><task>x</task></step></workflow>',
            '<workflow name="t" version="2" max="5">'
            '<step id="s1" tools="Read" decider="llm"><task>x</task></step></workflow>',
        ):
            with self.subTest(xml):
                codes = self._codes(xml)
                self.assertNotIn("decider-llm-unimplemented", codes)
                self.assertNotIn("decider-unknown", codes)

    def test_human_and_unset_pass(self):
        for xml in (
            '<workflow name="t" version="2" max="5" decider="human">'
            '<step id="s1" tools="Read"><task>x</task></step></workflow>',
            '<workflow name="t" version="2" max="5">'
            '<step id="s1" tools="Read"><task>x</task></step></workflow>',
        ):
            with self.subTest(xml):
                codes = self._codes(xml)
                self.assertNotIn("decider-llm-unimplemented", codes)
                self.assertNotIn("decider-unknown", codes)

    def test_pi_accepts_llm_adjudication(self):
        from wfrun import parser
        for xml in (
            '<workflow name="t" version="2" max="5" decider="llm">'
            '<step id="s1" tools="Read"><task>x</task></step></workflow>',
            '<workflow name="t" version="2" max="5">'
            '<step id="s1" tools="Read" decider="llm"><task>x</task></step>'
            '</workflow>',
            '<workflow name="t" version="2" max="5" decider="human">'
            '<step id="s1" tools="Read"><task>x</task></step></workflow>',
            '<workflow name="t" version="2" max="5">'
            '<step id="s1" tools="Read"><task>x</task></step></workflow>',
        ):
            with self.subTest(xml):
                self.assertEqual(
                    pi_cli.pi_compat_errors(parser.parse_string(xml)), [])

    def test_pi_still_refuses_schema_and_debug(self):
        from wfrun import parser
        errors = pi_cli.pi_compat_errors(parser.parse_string(
            '<workflow name="t" version="2" max="5" decider="llm">'
            '<step id="s1" tools="Read" schema=\'{"type":"object"}\' '
            'on-error="debug"><task>x</task></step></workflow>'))
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("schema=" in e for e in errors))
        self.assertTrue(any('on-error="debug"' in e for e in errors))

    def test_unknown_value_stays_decider_unknown(self):
        codes = self._codes(
            '<workflow name="t" version="2" max="5" decider="robot">'
            '<step id="s1" tools="Read"><task>x</task></step></workflow>')
        self.assertIn("decider-unknown", codes)
        self.assertNotIn("decider-llm-unimplemented", codes)


class TestVizDecider(unittest.TestCase):
    def test_start_node_carries_the_resolved_decider(self):
        from wfrun import parser, viz
        wf = parser.parse_string(
            '<workflow name="t" version="2" max="5">'
            '<step id="s1" tools="Read"><task>x</task></step></workflow>')
        self.assertIn("decider=human", viz.mermaid(wf))


class RunLlmAdjudicationTestCase(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from wfrun import parser
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._cwd = os.getcwd()
        os.chdir(self.root)
        (self.root / "steps").mkdir()
        self.vars = self.root / "vars.json"
        self.vars.write_text("{}", encoding="utf-8")
        self.log = self.root / "steps.log"
        self.result = self.root / "steps" / "s1_result.md"
        self.dec = self.root / "decisions"
        self.xml = (
            '<workflow name="t" version="2" max="9">'
            '<step id="s1" tools="Read" expect-file="art.txt" output="v" '
            '><task>t</task></step></workflow>')
        self.wf = parser.parse_string(self.xml)
        self.step = stepio.find_step(self.wf, "s1")

    def tearDown(self):
        import os
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def record(self, text):
        self.result.write_text(text, encoding="utf-8")
        return stepio.record_result(self.step, self.result, self.vars, self.log)

    def answer(self, body):
        path = self.root / "ans.md"
        path.write_text(body, encoding="utf-8")
        return stepio.adjudicate_answer(self.step, self.result, self.vars,
                                        path, self.log)


class TestRunLlmAdjudication(RunLlmAdjudicationTestCase):
    def test_request_survives_the_result_file_being_deleted(self):
        status, message = self.record(payload())
        self.assertEqual(status, "decision")
        filed = self.dec / "s1_d01_request.md"
        self.assertTrue(filed.is_file())
        self.assertIn(str(filed), message)
        self.result.unlink()
        self.assertIn("DECISION:", filed.read_text(encoding="utf-8"))

    def test_form_a_sets_the_var_without_re_running(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        self.record(payload())
        status, message = self.answer("option: 1\ngo with A")
        self.assertEqual(status, "ok")
        self.assertEqual(json.loads(self.vars.read_text(encoding="utf-8"))["v"],
                         "art.txt")
        self.assertIn("without re-running", message)

    def test_form_b_asks_for_a_re_run(self):
        self.record(payload(work_state="stopped", output=None))
        status, message = self.answer("option: 2\nB please")
        self.assertEqual(status, "rerun")
        self.assertIn(decision_mod.B_REASON_WORK_STATE_STOPPED, message)

    def test_complete_without_output_re_runs_instead_of_failing(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        status, _ = self.record(payload(output=None))
        self.assertEqual(status, "decision")
        status, message = self.answer("option: 1\ngo with A")
        self.assertEqual(status, "rerun")
        self.assertIn(decision_mod.B_REASON_NO_OUTPUT, message)

    def test_missing_artifact_collapses_to_missing_file(self):
        self.record(payload())
        status, message = self.answer("option: 1\nkeep going")
        self.assertEqual(status, "rerun")
        self.assertIn(decision_mod.B_REASON_MISSING_FILE, message)
        self.assertNotIn("at-resume", message)

    def test_unlisted_option_forces_a_re_run(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        self.record(payload())
        status, message = self.answer("option: none\ndo something else")
        self.assertEqual(status, "rerun")
        self.assertIn(decision_mod.B_REASON_UNLISTED_OPTION, message)

    def test_option_other_than_the_recommendation_forces_a_re_run(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        self.record(payload(recommendation="2"))
        status, message = self.answer("option: 1\nI disagree with the step")
        self.assertEqual(status, "rerun")
        self.assertIn(
            decision_mod.B_REASON_OPTION_NOT_RECOMMENDED, message,
            "output: describes only the step's own recommendation, so any "
            "other choice must re-run instead of substituting a value")
        self.assertEqual(json.loads(self.vars.read_text(encoding="utf-8")), {})

    def test_agreeing_with_the_recommendation_still_reaches_form_a(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        self.record(payload(recommendation="2"))
        status, message = self.answer("option: 2\nagreed")
        self.assertEqual(status, "ok")
        self.assertIn("without re-running", message)
        self.assertEqual(json.loads(self.vars.read_text(encoding="utf-8"))["v"],
                         "art.txt")

    def test_a_payload_recommending_none_never_reaches_form_a(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        self.record(payload(recommendation="none"))
        status, message = self.answer("option: 1\npick the first")
        self.assertEqual(status, "rerun")
        self.assertIn(decision_mod.B_REASON_OPTION_NOT_RECOMMENDED, message)

    def test_second_answer_is_refused(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        self.record(payload())
        self.answer("option: 1\nfirst")
        with self.assertRaises(stepio.StepIOError) as ctx:
            self.answer("option: 2\nsecond")
        self.assertIn("no open decision request", str(ctx.exception))

    def test_answer_without_a_request_is_refused(self):
        with self.assertRaises(stepio.StepIOError):
            self.answer("option: 1\nnothing to settle")

    def test_bad_answer_leaves_the_ledger_untouched(self):
        self.record(payload(work_state="stopped", output=None))
        for body in ("prose only", "option: 9\nout of range", "option: none\n"):
            with self.subTest(body):
                with self.assertRaises(stepio.StepIOError):
                    self.answer(body)
        self.assertIsNone(
            decision_mod.verdict_marker(self.dec, "s1_d01").exists() or None)
        self.assertEqual(self.answer("option: 1\nfine")[0], "rerun")

    def test_adjudication_lands_in_steps_log(self):
        self.record(payload(work_state="stopped", output=None))
        self.answer("option: 2\nB please")
        entries = [json.loads(line) for line in
                   self.log.read_text(encoding="utf-8").splitlines() if line.strip()]
        ruling = [e for e in entries if e.get("status", "").startswith("decision-")]
        self.assertEqual(len(ruling), 1)
        self.assertEqual(ruling[0]["option"], 2)
        self.assertEqual(ruling[0]["request_id"], "s1_d01")
        self.assertEqual(ruling[0]["b_reason"],
                         decision_mod.B_REASON_WORK_STATE_STOPPED)

    def test_settled_pairs_accumulate_for_the_re_run_prompt(self):
        self.record(payload(work_state="stopped", output=None, fork="first fork"))
        self.answer("option: 1\nfirst ruling")
        self.record(payload(work_state="stopped", output=None, fork="second fork"))
        self.answer("option: 2\nsecond ruling")
        pairs = decision_mod.settled_pairs(self.dec, "s1")
        self.assertEqual(len(pairs), 2)
        joined = "\n".join(r + a for r, a in pairs)
        for needle in ("first fork", "first ruling", "second fork", "second ruling"):
            self.assertIn(needle, joined)

    def test_allocation_skips_retired_numbers(self):
        self.record(payload(work_state="stopped", output=None))
        (self.dec / "s1_d01_request.md").unlink()
        self.assertEqual(decision_mod.allocate_request_id(self.dec, "s1"), "s1_d01")
        self.record(payload(work_state="stopped", output=None))
        self.record(payload(work_state="stopped", output=None))
        self.assertEqual(decision_mod.allocate_request_id(self.dec, "s1"), "s1_d03")

    def test_a_and_b_layer_prefixes_do_not_collide(self):
        self.record(payload(work_state="stopped", output=None))
        stepio.persist_decision_request(payload(), self.dec, "s1_c01")
        self.assertEqual(decision_mod.request_ids(self.dec, "s1"), ["s1_d01"])
        self.assertEqual(decision_mod.request_ids(self.dec, "s1_c01"),
                         ["s1_c01_d01"])

    def test_unreadable_settled_pair_raises_rather_than_dropping_a_ruling(self):
        self.record(payload(work_state="stopped", output=None))
        self.answer("option: 1\nfirst")
        (self.dec / "s1_d01_request.md").unlink()
        with self.assertRaises(decision_mod.DecisionError):
            decision_mod.settled_pairs(self.dec, "s1")

    def test_exit_codes(self):
        from wfrun.__main__ import RECORD_EXIT_CODES
        self.assertEqual(RECORD_EXIT_CODES["decision"], 4)
        self.assertEqual(RECORD_EXIT_CODES["rerun"], 5)

    def test_record_answer_settles_an_a_layer_request(self):
        stepio.persist_decision_request(
            payload(work_state="stopped", output=None), self.dec, "s1_c03")
        self.assertEqual(decision_mod.pending_step_request_id(self.dec, "s1"),
                         "s1_c03_d01")
        status, message = self.answer("option: 1\nsettle the A-layer request")
        self.assertEqual(status, "rerun")
        self.assertIn("s1_c03_d01", message)
        self.assertTrue(
            decision_mod.verdict_marker(self.dec, "s1_c03_d01").is_file())

    def test_both_layers_share_one_pending_queue(self):
        stepio.persist_decision_request(
            payload(work_state="stopped", output=None), self.dec, "s1")
        stepio.persist_decision_request(
            payload(work_state="stopped", output=None), self.dec, "s1_c01")
        self.assertEqual(decision_mod.step_request_ids(self.dec, "s1"),
                         ["s1_d01", "s1_c01_d01"])
        self.assertEqual(decision_mod.pending_step_request_id(self.dec, "s1"),
                         "s1_c01_d01")

    def test_a_layer_files_the_request_under_its_cycle(self):
        res = CliResult(ok=False, error_class="decision",
                        text=payload(work_state="stopped", output=None),
                        error="DECISION: ...")
        status, message = stepio.apply_result(
            self.step, res, self.vars, log_path=self.log,
            result_path=self.result, base_dir=self.root,
            decisions_dir=self.dec, decision_prefix="s1_c02")
        self.assertEqual(status, "decision")
        filed = self.dec / "s1_c02_d01_request.md"
        self.assertTrue(filed.is_file())
        self.assertIn(str(filed), message)
        self.assertEqual(decision_mod.request_ids(self.dec, "s1"), [])


class TestRunLlmValueOutput(RunLlmAdjudicationTestCase):
    def retype(self, attrs):
        from wfrun import parser
        wf = parser.parse_string(
            '<workflow name="t" version="2" max="9">'
            f'<step id="s1" tools="Read" expect-file="art.txt" {attrs}>'
            '<task>t</task></step></workflow>')
        self.step = stepio.find_step(wf, "s1")

    def test_value_typed_step_re_runs(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        self.retype('output="v" output-type="value"')
        self.record(payload(output="the figure is undecided"))
        status, message = self.answer("option: 1\ngo with A")
        self.assertEqual(status, "rerun")
        self.assertIn("re-run", message)
        entry = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["b_reason"], decision_mod.B_REASON_VALUE_OUTPUT)
        self.assertNotIn("v", json.loads(self.vars.read_text(encoding="utf-8")))

    def test_step_with_no_output_keeps_form_a(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        self.retype("")
        self.record(payload(output=None))
        status, _ = self.answer("option: 1\ngo with A")
        self.assertEqual(status, "ok",
                         "a step that declares no output= has nowhere to put "
                         "a value, so it is never demoted for missing one")
        entry = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertIsNone(entry["b_reason"])


class TestCompletionReportShape(unittest.TestCase):
    def test_detector_separates_the_two_shapes(self):
        report = ("DECISION: none -- task unambiguous\n"
                  "work-state: complete\noutput: art.txt")
        self.assertTrue(decision_mod.looks_like_completion_report(report))
        broken = "DECISION: x\nfork: f\nwork-state: complete\noutput: art.txt"
        self.assertFalse(decision_mod.looks_like_completion_report(broken))
        self.assertFalse(decision_mod.looks_like_completion_report(
            "DECISION: x\nfork: f\noptions:\n  1. A"))


WRAPPED_REPORT = ("DECISION: none -- task unambiguous\n"
                  "work-state: complete\noutput: art.txt")


class TestCompletionReportBatch(DecisionExecutorTestCase):
    def test_names_the_shape_and_marks_the_event(self):
        self.fake.handlers.append(
            (lambda p: "DO-WORK" in p,
             CliResult(ok=True, cost_usd=0.02, text=WRAPPED_REPORT)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="2"><task>DO-WORK</task></step>'))
        with self.assertRaises(WorkflowFailure) as ctx:
            ex.run()
        self.assertIn("wrapped a completion report", str(ctx.exception))
        self.assertEqual(len(self.fake.calls), 1)
        record = self.decision_events(valid_only=False)[0]
        self.assertFalse(record["valid"])
        self.assertTrue(record["completion_report_shape"])

    def test_an_ordinary_malformed_payload_is_not_marked(self):
        self.fake.handlers.append(
            (lambda p: "DO-WORK" in p,
             CliResult(ok=True, cost_usd=0.02,
                       text="DECISION: x\nfork: f\nwork-state: complete")))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w"><task>DO-WORK</task></step>'))
        with self.assertRaises(WorkflowFailure) as ctx:
            ex.run()
        self.assertIn("malformed", str(ctx.exception))
        self.assertNotIn("completion report", str(ctx.exception))
        self.assertFalse(self.decision_events(valid_only=False)[0]
                         ["completion_report_shape"])


class TestCompletionReportRunLlm(RunLlmAdjudicationTestCase):
    def test_record_names_the_shape_without_quoting_the_body(self):
        status, message = self.record(WRAPPED_REPORT)
        self.assertEqual(status, "decision")
        self.assertIn("wrapped a completion report", message)
        self.assertNotIn("unambiguous", message)

    def test_ordinary_malformed_keeps_the_generic_line(self):
        status, message = self.record("DECISION: x\nfork: f\nwork-state: complete")
        self.assertEqual(status, "decision")
        self.assertIn("malformed", message)
        self.assertNotIn("completion report", message)


class TestRecordVerdict(unittest.TestCase):
    def _record(self, tmp, text, step_xml='<step id="s1" role="w"><task>t</task></step>'):
        from wfrun import parser
        wf = parser.parse_string(f'<workflow name="t" version="2" max="5">{step_xml}</workflow>')
        step = stepio.find_step(wf, "s1")
        result = Path(tmp) / "s1_result.md"
        result.write_text(text, encoding="utf-8")
        vars_path = Path(tmp) / "vars.json"
        vars_path.write_text("{}", encoding="utf-8")
        return stepio.record_result(step, result, vars_path)

    def test_valid_and_malformed_both_report_decision(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            status, message = self._record(tmp, payload())
            self.assertEqual(status, "decision")
            self.assertIn("requested adjudication", message)

            status, message = self._record(tmp, "DECISION: nothing else")
            self.assertEqual(status, "decision")
            self.assertIn("malformed", message)
            self.assertNotIn("nothing else", message)

    def test_exit_code_is_four(self):
        from wfrun.__main__ import RECORD_EXIT_CODES
        self.assertEqual(RECORD_EXIT_CODES["decision"], 4)


class TestRunLlmDeciderLedger(RunLlmAdjudicationTestCase):
    def answer_as(self, body, decider):
        path = self.root / "ans.md"
        path.write_text(body, encoding="utf-8")
        return stepio.adjudicate_answer(self.step, self.result, self.vars,
                                        path, self.log, decider=decider)

    def marker(self, rid="s1_d01"):
        return json.loads((self.dec / f"{rid}_verdict.json")
                          .read_text(encoding="utf-8"))

    def test_the_ruling_records_who_made_it(self):
        self.record(payload(work_state="stopped", output=None))
        self.answer_as("option: 2\nB please", "llm")
        self.assertEqual(self.marker()["decider"], "llm")
        entries = [json.loads(line) for line
                   in self.log.read_text(encoding="utf-8").splitlines()]
        adjudications = [e for e in entries if e.get("status", "").startswith("decision-")]
        self.assertEqual(adjudications[-1]["decider"], "llm")

    def test_the_cap_sends_the_next_fork_to_a_person(self):
        for n in (1, 2):
            self.record(payload(work_state="stopped", output=None))
            self.answer_as(f"option: {n}\nkeep going", "llm")
        self.assertEqual(decision_mod.llm_adjudications(self.dec, "s1"), 2)
        _, message = self.record(payload(work_state="stopped", output=None))
        self.assertIn("--decider human", message)
        self.assertNotIn("which join", message)

    def test_human_rulings_do_not_spend_the_cap(self):
        for n in (1, 2):
            self.record(payload(work_state="stopped", output=None))
            self.answer_as(f"option: {n}\nkeep going", "human")
        self.assertEqual(decision_mod.llm_adjudications(self.dec, "s1"), 0)
        _, message = self.record(payload(work_state="stopped", output=None))
        self.assertNotIn("--decider human", message)

    def test_a_rejected_ruling_leaves_no_trace(self):
        self.record(payload(work_state="stopped", output=None))
        for body in ("no option line here", "option: 9\nout of range",
                     "option: none\n"):
            with self.subTest(body=body):
                with self.assertRaises(stepio.StepIOError):
                    self.answer_as(body, "llm")
        with self.assertRaises(stepio.StepIOError):
            stepio.adjudicate_answer(self.step, self.result, self.vars,
                                     self.root / "never-written.md", self.log,
                                     decider="llm")
        self.assertFalse((self.dec / "s1_d01_verdict.json").exists())
        self.assertEqual(json.loads(self.vars.read_text(encoding="utf-8")), {})
        self.assertEqual(decision_mod.llm_adjudications(self.dec, "s1"), 0)


def preambled(text, lines=("I examined both readings and cannot settle this alone.",
                           "Filing a decision request instead of picking one:")):
    return "\n".join([*lines, "", text])


class TestClaimDecisionBody(unittest.TestCase):
    def test_first_token_claims_even_malformed(self):
        body = "DECISION: only a summary"
        claimed, preamble = decision_mod.claim_decision_body(body)
        self.assertEqual(claimed, body)
        self.assertEqual(preamble, "")

    def test_preambled_complete_payload_is_claimed_from_its_anchor(self):
        claimed, preamble = decision_mod.claim_decision_body(preambled(payload()))
        self.assertIsNotNone(claimed)
        self.assertTrue(claimed.startswith("DECISION:"))
        parsed, errors = decision_mod.parse_payload(claimed)
        self.assertEqual(errors, [])
        self.assertEqual(parsed.output, "art.txt")
        self.assertEqual(len(preamble.splitlines()), 2)

    def test_mid_sentence_mention_is_not_claimed(self):
        claimed, _ = decision_mod.claim_decision_body(
            "The task asks about the DECISION: protocol; summarized it in doc.md.")
        self.assertIsNone(claimed)

    def test_line_anchored_but_incomplete_payload_is_not_claimed(self):
        claimed, _ = decision_mod.claim_decision_body(
            preambled("DECISION: raised, then resolved by myself below\nfork: f"))
        self.assertIsNone(claimed)

    def test_unparseable_first_anchor_falls_through_to_a_complete_one(self):
        body = "\n".join(["prose first",
                          "DECISION: incomplete early mention",
                          "prose in between",
                          "",
                          payload(summary="the real one")])
        claimed, _ = decision_mod.claim_decision_body(body)
        self.assertIsNotNone(claimed)
        self.assertTrue(claimed.startswith("DECISION: the real one"))

    def test_empty_and_plain_bodies_make_no_claim(self):
        for body in ("", "plain result text\nsecond line"):
            with self.subTest(body=body[:20]):
                self.assertEqual(decision_mod.claim_decision_body(body),
                                 (None, ""))


class TestStrayProtocolLines(unittest.TestCase):
    def test_line_anchored_tokens_are_reported_with_line_numbers(self):
        body = ("all done\n"
                "ERROR: retained log line\n"
                "  [BLOCKED: quoted refusal]\n"
                "DECISION: unparseable, no fields\n"
                "an ERROR: token mid-sentence does not count\n")
        self.assertEqual(decision_mod.stray_protocol_lines(body),
                         [(2, "ERROR:"), (3, "[BLOCKED:"), (4, "DECISION:")])

    def test_clean_body_has_none(self):
        self.assertEqual(decision_mod.stray_protocol_lines("plain result\n1. ok"),
                         [])


class TestPreambleClassification(unittest.TestCase):
    def _stdout(self, text):
        return json.dumps({"result": text, "total_cost_usd": 0.01})

    def test_cc_classifies_a_preambled_payload(self):
        res = classify_result(0, self._stdout(preambled(payload())), "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "decision")
        self.assertTrue(res.error.startswith("DECISION:"))

    def test_cc_leaves_a_preambled_incomplete_payload_ok(self):
        res = classify_result(
            0, self._stdout(preambled("DECISION: but no fields follow")), "")
        self.assertTrue(res.ok)

    def test_cc_leaves_a_preambled_error_token_ok(self):
        res = classify_result(0, self._stdout("prose first\n\nERROR: too late"), "")
        self.assertTrue(res.ok)

    def test_pi_classifies_a_preambled_payload(self):
        stdout = json.dumps({
            "type": "turn_end",
            "message": {"stopReason": "stop",
                        "content": [{"type": "text",
                                     "text": preambled(payload())}],
                        "usage": {"cost": {"total": 0.0}}}})
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertEqual(res.error_class, "decision")


class TestPreambleAnchoring(DecisionExecutorTestCase):
    def respond_text(self, needle, text):
        self.fake.handlers.append(
            (lambda p, n=needle: n in p,
             CliResult(ok=True, text=text, cost_usd=0.02)))

    def test_preambled_payload_stops_the_run_anchored(self):
        self.artifact()
        self.respond_text("DO-WORK", preambled(payload()))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" retry="2" expect-file="art.txt">'
            '<task>DO-WORK</task></step>'
            '<step id="s2" role="w"><task>later</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()
        self.assertEqual(len(self.fake.calls), 1)
        record = self.decision_events()[0]
        body = Path(record["request"]).read_text(encoding="utf-8")
        self.assertTrue(body.startswith("DECISION:"))
        self.assertEqual(record["preamble_lines"], 2)
        self.assertTrue(record["a_eligible"])

    def test_answer_after_a_preambled_stop_takes_form_a(self):
        self.artifact()
        self.respond_text("DO-WORK", preambled(payload()))
        xml = self.wrap('<step id="s1" role="w" expect-file="art.txt" output="v" '
                        '><task>DO-WORK</task></step>')
        ex = self.execute(xml)
        with self.assertRaises(DecisionRequested):
            ex.run()
        calls_before = len(self.fake.calls)
        events = self.answer(self.run_dir, "s1", "option: 1\nagreed")
        ex2 = self.execute(xml, events=events)
        ex2.run()
        self.assertEqual(len(self.fake.calls), calls_before)
        self.assertEqual(ex2.vars["v"], "art.txt")

    def test_stray_token_in_a_success_warns_without_failing(self):
        self.respond_text("DO-WORK",
                          "fine result\nERROR: quoted from the tool log\nend")
        ex = self.execute(self.wrap(
            '<step id="s1" role="w"><task>DO-WORK</task></step>'))
        ex.run()
        warnings = [e for e in load_events(self.run_dir)
                    if e.get("kind") == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["key"], "s1")
        self.assertIn("ERROR:", warnings[0]["warning"])
        self.assertEqual(len(ex.protocol_warnings), 1)

    def test_a_clean_success_emits_no_warning(self):
        ex = self.execute(self.wrap(
            '<step id="s1" role="w"><task>DO-WORK</task></step>'))
        ex.run()
        self.assertFalse([e for e in load_events(self.run_dir)
                          if e.get("kind") == "warning"])
        self.assertFalse(ex.protocol_warnings)

    def test_replacement_char_in_a_success_warns_without_failing(self):
        self.respond_text("DO-WORK", "caf� au lait")
        ex = self.execute(self.wrap(
            '<step id="s1" role="w"><task>DO-WORK</task></step>'))
        ex.run()
        warnings = [e for e in load_events(self.run_dir)
                    if e.get("kind") == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["key"], "s1")
        self.assertEqual(warnings[0]["surface"], "response")
        self.assertEqual(warnings[0]["replacement_chars"], 1)
        self.assertIn("U+FFFD", warnings[0]["warning"])
        self.assertEqual(len(ex.protocol_warnings), 1)


class TestRunLlmPreamble(RunLlmAdjudicationTestCase):
    def test_preambled_payload_is_filed_anchored_and_answerable(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        status, _ = self.record(preambled(payload()))
        self.assertEqual(status, "decision")
        filed = self.dec / "s1_d01_request.md"
        self.assertTrue(filed.read_text(encoding="utf-8").startswith("DECISION:"))
        status, message = self.answer("option: 1\nagreed")
        self.assertEqual(status, "ok")
        self.assertEqual(json.loads(self.vars.read_text(encoding="utf-8"))["v"],
                         "art.txt")

    def test_stray_token_warns_in_the_ok_message(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        status, message = self.record(
            "wrote art.txt\nERROR: quoted line\nDECISION: no fields follow")
        self.assertEqual(status, "ok")
        self.assertIn("warning", message)
        self.assertIn("ERROR:", message)


if __name__ == "__main__":
    unittest.main()
