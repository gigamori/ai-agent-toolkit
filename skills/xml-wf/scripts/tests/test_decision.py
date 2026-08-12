"""`DECISION:` channel tests (xml-wf-decision-request.md P1).

Three layers, all with a fake runner (no API calls, no cost):
  - the payload/answer grammar (decision.py)
  - classification on all three sites that assign error_class (§3)
  - the batch stop/resume machinery: form (a) vs (b), the six (b) reasons, the
    no-cost re-stop, partial-answer <parallel>, and failure-outranks-decision

The resume tests drive `__main__._ingest_answers` rather than re-implementing
adjudication, since the ordering it enforces (answer event, then the synthetic
success, then the Executor) is itself the thing under test (§13.4).
"""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# `unittest discover -s tests` puts this directory on the path itself, but
# `python -m unittest tests.test_decision` does not — and the harness below is
# imported from a sibling module either way.
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
        # The hazard §3 exists to close: with schema= set, a later check would
        # call this `behavioral` and retry a step that may already have written
        # its deliverable.
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
    """Adds decision-shaped fake responses to the shared executor harness."""

    def respond_decision(self, needle, *, then_ok=False, **payload_kwargs):
        """Answer prompts containing `needle` with a DECISION: payload.

        With then_ok, only the FIRST such prompt gets it — which is what a
        form-(b) re-run needs: the step is expected to settle once it has been
        told what was decided.
        """
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
        """Write an answer file and run it through the real ingestion path."""
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
        # retry=2 must not have fired, and the downstream step never ran
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(len(ctx.exception.requests), 1)

        record = self.decision_events()[0]
        self.assertEqual(record["request_id"], "s1_c01_d01")
        self.assertTrue(Path(record["request"]).is_file())
        self.assertTrue(record["a_eligible"])
        self.assertIsNone(record["b_reason"])
        # the recorded expect-file path is absolute, so a resume from another
        # cwd re-checks the same file (§13.2)
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
        self.assertEqual(len(self.fake.calls), 1)  # class blocks the retry
        self.assertFalse(self.decision_events(valid_only=True))
        self.assertTrue(self.decision_events(valid_only=False))

    def test_on_error_ignore_absorbs_a_malformed_payload(self):
        self.fake.handlers.append(
            (lambda p: "bad" in p,
             CliResult(ok=True, text="DECISION: no fields", cost_usd=0.02)))
        ex = self.execute(self.wrap(
            '<step id="bad" role="w" on-error="ignore"><task>bad</task></step>'))
        ex.run()  # malformed IS a failure, so ignore may swallow it

    def test_on_error_ignore_does_not_absorb_a_real_request(self):
        self.respond_decision("good", work_state="stopped", output=None)
        ex = self.execute(self.wrap(
            '<step id="good" role="w" on-error="ignore"><task>good</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()  # a well-formed request is not a failure to ignore (§13.7)


class TestNoOutputDegrades(DecisionExecutorTestCase):
    """`work-state: complete` with no `output:` is answerable, not malformed.

    It used to be a payload error, which took the whole run down (error_class
    `decision` takes neither retry nor debug) over a slip an eval measured at
    3 of 6 `complete` samples. The fork was well posed every time; only the
    value to adopt was missing, so it degrades to a (b) re-run (§1, §6).
    """

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
            ex.run()  # a decision stop, NOT a WorkflowFailure
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
        # and the re-run really happens rather than the run having died
        resumed = self.execute(self.wrap(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>'), events=events)
        resumed.run()
        self.assertEqual(len(self.fake.calls), 2)

    def test_llm_decider_settles_it_in_process(self):
        """The degradation reaches the llm path too: the adjudicator is called
        (the payload is answerable) and the run continues via form (b)."""
        self.artifact()
        self.respond_decision("DO-WORK", then_ok=True, output=None)
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>', extra='decider="llm"'),
            adjudicate=fake_decider(ruling(option=1)))
        ex.run()
        self.assertEqual(len(self.fake.calls), 2)  # form (b) re-run happened
        answer = [e for e in load_events(self.run_dir)
                  if e.get("kind") == "answer"][0]
        self.assertEqual(answer["b_reason"], decision_mod.B_REASON_NO_OUTPUT)


class TestBReasons(DecisionExecutorTestCase):
    """All six (b) reasons, the vocabulary §6 fixes."""

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
                                    'output="v" output-type="value">'
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
                                    'output="v" output-type="value">'
                                    '<task>DO-WORK</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()
        self.assertTrue(self.decision_events()[0]["a_eligible"])
        artifact.unlink()  # the human tidied up while the run was stopped
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
                        'output-type="value"><task>DO-WORK</task></step>'
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
        self.assertEqual(len(new), 1)              # only s2 ran
        self.assertIn("later", new[0]["prompt"])
        self.assertEqual(ex2.vars["v"], "art.txt")  # taken from the payload

    def test_batch_answer_against_the_recommendation_forces_a_re_run(self):
        # Same rule on the batch path: _ingest_answers must not synthesize a
        # success from an `output:` the ruling did not endorse.
        self.artifact()
        self.respond_decision("DO-WORK", then_ok=True, recommendation="2")
        xml = self.wrap('<step id="s1" role="w" expect-file="art.txt" output="v" '
                        'output-type="value"><task>DO-WORK</task></step>')
        ex = self.execute(xml)
        with self.assertRaises(DecisionRequested):
            ex.run()
        events = self.answer(self.run_dir, "s1", "option: 1\nI disagree")
        answer_event = [e for e in events if e.get("kind") == "answer"][-1]
        self.assertEqual(answer_event["verdict"], "b")
        self.assertEqual(answer_event["b_reason"],
                         decision_mod.B_REASON_OPTION_NOT_RECOMMENDED)
        # and no synthetic success was appended, so the step must run live
        self.assertFalse([e for e in events if e.get("via") == "decision-a"])

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
        self.assertIn("B, because rows matter", prompt)   # the answer
        self.assertIn("two readings of 'merge'", prompt)  # the original request
        # the answer rides the user channel, never the constraint layers
        self.assertNotIn("Decisions resolved", new[0]["system_prompt"])

    def test_second_fork_in_same_cycle_keeps_the_first_ruling_visible(self):
        # d01 answered -> (b) re-run raises a SECOND fork (d02) -> answered.
        # The next re-run must carry BOTH settled pairs; dropping d01 would
        # let the agent walk back into the settled fork (§13.6).
        state = {"n": 0}

        def predicate(prompt):
            if "DO-WORK" not in prompt:
                return False
            state["n"] += 1
            return state["n"] <= 2  # attempts 1 and 2 raise; attempt 3 settles

        def result_for(prompt):
            fork = ("first fork" if state["n"] == 1 else "second fork")
            return CliResult(ok=True, text=payload(work_state="stopped",
                                                   output=None, fork=fork),
                             cost_usd=0.02)

        # FakeClaude returns a fixed CliResult per handler, so route through a
        # tiny stateful wrapper instead of two static handlers.
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
            build().run()                                  # raises d01
        a1 = Path(self.tmp.name) / "a1.md"
        a1.write_text("option: 1\nfirst ruling", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            events = _ingest_answers(self.run_dir, [f"s1={a1}"],
                                     load_events(self.run_dir))
        with self.assertRaises(DecisionRequested):
            build(events).run()                            # re-run raises d02

        self.assertEqual(self.decision_events()[-1]["request_id"], "s1_c01_d02")
        a2 = Path(self.tmp.name) / "a2.md"
        a2.write_text("option: 2\nsecond ruling", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            events = _ingest_answers(self.run_dir, [f"s1={a2}"],
                                     load_events(self.run_dir))
        build(events).run()                                # settles

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

        for _ in range(2):  # repeated bare resumes must all be free and inert
            ex2 = self.execute(xml, events=load_events(self.run_dir))
            with self.assertRaises(DecisionRequested):
                ex2.run()
            self.assertEqual(len(self.fake.calls), calls_before)

        # no new decision event: re-raising must not let the ledger advance on
        # nothing but a re-print (§13.5)
        self.assertEqual(len(self.decision_events()), 1)
        # only the run-level start/awaiting-decision pairs were appended
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
        # an unknown step id is refused too, rather than silently ignored
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
        # p1 re-ran (form b); p2 re-stopped without spending anything
        new = self.fake.calls[calls_before:]
        self.assertEqual(len(new), 1)
        self.assertIn("alpha", new[0]["prompt"])
        self.assertEqual([r["key"] for r in ctx.exception.requests], ["p2"])

    def test_partial_answer_form_a_synthesizes_without_any_call(self):
        # The (a) variant of partial answer goes through a DIFFERENT mechanism
        # from (b): the synthetic success is consumed by take_group as a
        # PARTIAL group (which disables further replay), while the unanswered
        # sibling falls through to the free pending re-raise. Neither side may
        # spend a CLI call.
        self.artifact()
        self.respond_decision("alpha")
        self.respond_decision("beta")
        xml = self.wrap(
            '<parallel max-workers="2">'
            '<step id="p1" role="w" expect-file="art.txt" output="va" '
            'output-type="value"><task>alpha</task></step>'
            '<step id="p2" role="w" expect-file="art.txt" output="vb" '
            'output-type="value"><task>beta</task></step>'
            '</parallel>')
        ex = self.execute(xml)
        with self.assertRaises(DecisionRequested):
            ex.run()
        calls_before = len(self.fake.calls)

        events = self.answer(self.run_dir, "p1", "option: 1\ngo with A")
        ex2 = self.execute(xml, events=events)
        with self.assertRaises(DecisionRequested) as ctx:
            ex2.run()
        self.assertEqual(len(self.fake.calls), calls_before)  # zero CLI spend
        self.assertEqual([r["key"] for r in ctx.exception.requests], ["p2"])
        self.assertEqual(ex2.vars["va"], "art.txt")  # synthesized from payload

    def test_a_failing_sibling_outranks_a_decision(self):
        self.respond_decision("alpha", work_state="stopped", output=None)
        self.fake.handlers.append(
            (lambda p: "beta" in p, CliResult(ok=False, error="ERROR: died",
                                              error_class="guardrail", cost_usd=0)))
        ex = self.execute(self._parallel_xml())
        with self.assertRaises(WorkflowFailure):
            ex.run()
        # the request is still saved and still listed for the report (§9)
        self.assertEqual([r["key"] for r in ex.decisions_raised], ["p1"])
        self.assertTrue(Path(self.decision_events()[0]["request"]).is_file())


class TestCycleIdentity(DecisionExecutorTestCase):
    def test_cycle_counts_visits_not_attempts(self):
        # iteration 1 succeeds, iteration 2 raises: the request must be tagged
        # c02, which is only true if the counter runs per step-node visit
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
    """One Adjudication, in the shape adjudicate() returns (§15.2)."""
    answer_text = (adjudicate_mod.render_answer(option, text)
                   if verdict == "settled" else None)
    return adjudicate_mod.Adjudication(
        verdict=verdict, answer_text=answer_text, reason=text, raw=raw,
        cost_usd=cost)


def fake_decider(*rulings, calls=None):
    """An adjudicate() stand-in handing out `rulings` in order, no API call."""
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
    """`decider="llm"`: the in-process path (§15.1) and its three fallbacks."""

    def llm(self, inner, *rulings, calls=None, extra='decider="llm"'):
        return self.execute(self.wrap(inner, extra=extra),
                            adjudicate=fake_decider(*rulings, calls=calls))

    def test_escalation_stops_for_a_human_without_re_running(self):
        """T1: the clause in §5 routes the fork to a person, and the step is
        not re-run on the way."""
        calls = []
        self.respond_decision("DO-WORK")
        ex = self.llm('<step id="s1" role="w"><task>DO-WORK</task></step>',
                      ruling(verdict="escalate", text="irreversible: it emails"),
                      calls=calls)
        with self.assertRaises(DecisionRequested):
            ex.run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.fake.calls), 1)  # no form-(b) re-run
        record = self.decision_events()[0]
        self.assertTrue(record["escalated"])
        self.assertIn("irreversible", record["adjudication_note"])
        # the answer path stays empty: it is where the human writes (§15.2)
        self.assertFalse(Path(record["answer_path"]).exists())
        # and the fallback is not counted against the cap (§7)
        self.assertEqual(record["decider"], "human")

    def test_malformed_payload_never_reaches_the_decider(self):
        """T2: an unanswerable payload stops the run before adjudication."""
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
        """T2b: a ruling the shared parser rejects is no ruling at all."""
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
        """T3: two rulings per step visit, then the run stops (§7)."""
        calls = []
        self.fake.handlers.append(
            (lambda p: "DO-WORK" in p,
             CliResult(ok=True, text=payload(work_state="stopped", output=None),
                       cost_usd=0.02)))
        ex = self.llm('<step id="s1" role="w"><task>DO-WORK</task></step>',
                      ruling(option=1), ruling(option=2), calls=calls)
        with self.assertRaises(DecisionRequested):
            ex.run()
        self.assertEqual(len(calls), 2)          # the cap, not one more
        self.assertEqual(len(self.fake.calls), 3)  # two re-runs, then the stop
        events = self.decision_events()
        self.assertEqual([e["decider"] for e in events],
                         ["llm", "llm", "human"])
        self.assertTrue(events[2]["cap_reached"])
        self.assertNotIn("adjudication_cost_usd", events[2])

    def test_adjudication_cost_reaches_the_budget_check(self):
        """T4: the ruling's cost is workflow cost, so budget-usd sees it."""
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
        """T5: the tally counts llm rulings only, so a human-answered run can
        stop and be answered any number of times (§7)."""
        from wfrun.executor import decision_tables
        events = [{"kind": "decision", "key": "s1", "cycle": 1, "seq": n,
                   "valid": True, "request_id": f"s1_c01_d{n:02d}",
                   "decider": "human"} for n in (1, 2, 3)]
        self.assertEqual(decision_tables(events)[3], {})
        # and a human-decider run does not call an adjudicator at all: the
        # harness's default raises if it is reached
        self.respond_decision("DO-WORK")
        ex = self.execute(self.wrap(
            '<step id="s1" role="w"><task>DO-WORK</task></step>'))
        with self.assertRaises(DecisionRequested):
            ex.run()

    def test_form_a_continues_in_process_and_replays_as_a_hit(self):
        """T8: the ruling is applied without stopping, in the shape a resumed
        run consumes as an ordinary replay hit (§15.1)."""
        self.artifact()
        self.respond_decision("DO-WORK")
        ex = self.llm(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>',
            ruling(option=1))  # matches the payload's recommendation
        ex.run()
        self.assertEqual(ex.vars["v"], "art.txt")
        self.assertEqual(len(self.fake.calls), 1)  # (a) never re-runs
        success = [e for e in load_events(self.run_dir)
                   if e.get("kind") == "step" and e.get("status") == "success"]
        self.assertEqual(success[0]["via"], "decision-a")
        answer = [e for e in load_events(self.run_dir) if e.get("kind") == "answer"]
        self.assertEqual(answer[0]["decider"], "llm")
        self.assertEqual(answer[0]["verdict"], "a")
        # the ruling is on disk at the human's path, in the human's format
        self.assertTrue(Path(answer[0]["answer_path"]).read_text(
            encoding="utf-8").startswith("option: 1"))
        # replaying those events re-runs nothing
        replayed = self.execute(self.wrap(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>', extra='decider="llm"'),
            events=load_events(self.run_dir))
        replayed.run()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(replayed.vars["v"], "art.txt")

    def test_form_b_re_runs_in_place_carrying_every_ruling(self):
        """T9: the re-run happens inside the same visit, is not a failed
        attempt, and shows the step every fork already settled (§13.6)."""
        self.respond_decision("DO-WORK", then_ok=True,
                              work_state="stopped", output=None)
        ex = self.llm('<step id="s1" role="w" retry="1"><task>DO-WORK</task></step>',
                      ruling(option=2, text="take B"))
        ex.run()
        self.assertEqual(len(self.fake.calls), 2)
        self.assertIn("take B", self.fake.calls[1]["prompt"])
        self.assertIn("DECISION:", self.fake.calls[1]["prompt"])
        # a settled fork is not a failure (§13.7)
        self.assertFalse([e for e in load_events(self.run_dir)
                          if e.get("status") == "attempt-failed"])

    def test_form_b_does_not_spend_the_retry_budget(self):
        """The re-run is granted, like debug's one attempt -- a later genuine
        failure still gets the retry the step declared (§15.1)."""
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
        # decision + the re-run + the retry the re-run must not have eaten
        self.assertEqual(len(self.fake.calls), 3)


class TestDeciderLint(unittest.TestCase):
    def _lint(self, xml):
        from wfrun import lint as lint_mod
        from wfrun import parser
        wf = parser.parse_string(xml)
        return lint_mod.lint(wf, check_roles=False)

    def _codes(self, xml):
        return {f.code for f in self._lint(xml)}

    def test_llm_passes_lint_now_that_it_is_implemented(self):
        """T7: the transitional rejection is gone; the backend that cannot run
        it refuses at startup instead (§15.8)."""
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

    def test_pi_refuses_llm_adjudication_at_both_levels(self):
        """T6: the pi backend rejects it before any process starts -- and the
        workflow-level attribute names no step, so a step-only scan would miss
        it (§15.8)."""
        from wfrun import parser
        for xml, expected in (
            ('<workflow name="t" version="2" max="5" decider="llm">'
             '<step id="s1" tools="Read"><task>x</task></step></workflow>',
             "this workflow"),
            ('<workflow name="t" version="2" max="5">'
             '<step id="s1" tools="Read" decider="llm"><task>x</task></step>'
             '</workflow>', "step 's1'"),
        ):
            with self.subTest(xml):
                errors = pi_cli.pi_compat_errors(parser.parse_string(xml))
                self.assertEqual(len(errors), 1)
                self.assertIn(expected, errors[0])
                self.assertIn('decider="human"', errors[0])
                self.assertIn("--backend cc", errors[0])

    def test_pi_accepts_human_adjudication(self):
        from wfrun import parser
        for xml in (
            '<workflow name="t" version="2" max="5" decider="human">'
            '<step id="s1" tools="Read"><task>x</task></step></workflow>',
            '<workflow name="t" version="2" max="5">'
            '<step id="s1" tools="Read"><task>x</task></step></workflow>',
        ):
            with self.subTest(xml):
                self.assertEqual(
                    pi_cli.pi_compat_errors(parser.parse_string(xml)), [])

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
        # resolved from the default, not just echoed from an attribute (§11)
        self.assertIn("decider=human", viz.mermaid(wf))


class RunLlmAdjudicationTestCase(unittest.TestCase):
    """Fixture for the run-llm layer: a workflow, a result file and the
    `decisions/` ledger, with the cwd where an orchestrator would stand."""

    def setUp(self):
        import os
        import tempfile
        from wfrun import parser
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # This layer resolves expect-file against the caller's cwd — its
        # documented "orchestrator cwd = subagent cwd" premise — so the test
        # has to stand where a real run-llm orchestrator stands.
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
            'output-type="value"><task>t</task></step></workflow>')
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
    """The run-llm (a)/(b) path (§14): `decisions/` is the ledger, and every
    enumeration is derived from it rather than carried by the orchestrator."""

    def test_request_survives_the_result_file_being_deleted(self):
        status, message = self.record(payload())
        self.assertEqual(status, "decision")
        filed = self.dec / "s1_d01_request.md"
        self.assertTrue(filed.is_file())
        self.assertIn(str(filed), message)
        # `prompt --result` clears the result file before every attempt; the
        # filed request must be untouched by that (§14.1)
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
        """The run-llm mirror of the batch degradation (§6): answerable, not
        malformed, and it lands in form (b) under no-output."""
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        status, _ = self.record(payload(output=None))
        self.assertEqual(status, "decision")
        status, message = self.answer("option: 1\ngo with A")
        self.assertEqual(status, "rerun")
        self.assertIn(decision_mod.B_REASON_NO_OUTPUT, message)

    def test_missing_artifact_collapses_to_missing_file(self):
        # No art.txt on disk: run-llm cannot tell "never written" from
        # "written then removed", so both read as missing-file (§14.2)
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
        # Measured 2026-08-13: with recommendation 2 / output 300 and a ruling
        # of option 1 (375), form (a) applied 300 — the ruling was silently
        # ignored. `output:` only describes the step's own recommendation, so
        # any other choice has to re-run rather than substitute a value.
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        self.record(payload(recommendation="2"))
        status, message = self.answer("option: 1\nI disagree with the step")
        self.assertEqual(status, "rerun")
        self.assertIn(decision_mod.B_REASON_OPTION_NOT_RECOMMENDED, message)
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
        # still open, so a good answer afterwards still works
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
        # Deliberately reuses ONE answer path for both rulings: the ledger
        # must not depend on a file the answerer may overwrite.
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
        # highest-plus-one, so a removed file never causes id reuse
        self.assertEqual(decision_mod.allocate_request_id(self.dec, "s1"), "s1_d03")

    def test_a_and_b_layer_prefixes_do_not_collide(self):
        self.record(payload(work_state="stopped", output=None))
        stepio.persist_decision_request(payload(), self.dec, "s1_c01")
        self.assertEqual(decision_mod.request_ids(self.dec, "s1"), ["s1_d01"])
        self.assertEqual(decision_mod.request_ids(self.dec, "s1_c01"),
                         ["s1_c01_d01"])

    def test_unreadable_settled_pair_raises_rather_than_dropping_a_ruling(self):
        # The verdict marker is what makes a ruling settled, so losing the
        # request must surface — not make the whole ruling vanish from the
        # re-run prompt, which is the failure R2 closed on the batch side.
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
        # The A layer files under <id>_cNN_dNN and `record --answer` is the
        # only settling verb, so a bare-<id> lookup would leave every A-layer
        # request detected-but-unanswerable.
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
        # newest-unsettled across both namespaces
        self.assertEqual(decision_mod.pending_step_request_id(self.dec, "s1"),
                         "s1_c01_d01")

    def test_a_layer_files_the_request_under_its_cycle(self):
        # `wait` goes through apply_result, which the A layer hands its run dir
        # and `<id>_cNN` because only it knows the cycle.
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
        # and it stays separate from the B-layer namespace
        self.assertEqual(decision_mod.request_ids(self.dec, "s1"), [])


class TestRecordVerdict(unittest.TestCase):
    """`record`'s verdict for the run-llm path — the second classification
    site, which never reaches classify_result()."""

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
            # never quotes the payload back at the orchestrator
            self.assertNotIn("nothing else", message)

    def test_exit_code_is_four(self):
        from wfrun.__main__ import RECORD_EXIT_CODES
        self.assertEqual(RECORD_EXIT_CODES["decision"], 4)


class TestRunLlmDeciderLedger(RunLlmAdjudicationTestCase):
    """run-llm's half of the llm decider (§15.7): who ruled is recorded, the
    cap is counted from the ledger, and nothing half-applies."""

    def answer_as(self, body, decider):
        path = self.root / "ans.md"
        path.write_text(body, encoding="utf-8")
        return stepio.adjudicate_answer(self.step, self.result, self.vars,
                                        path, self.log, decider=decider)

    def marker(self, rid="s1_d01"):
        return json.loads((self.dec / f"{rid}_verdict.json")
                          .read_text(encoding="utf-8"))

    def test_the_ruling_records_who_made_it(self):
        """T10 (first half): without this field human and delegated rulings
        are indistinguishable, and the cap has nothing to count."""
        self.record(payload(work_state="stopped", output=None))
        self.answer_as("option: 2\nB please", "llm")
        self.assertEqual(self.marker()["decider"], "llm")
        entries = [json.loads(line) for line
                   in self.log.read_text(encoding="utf-8").splitlines()]
        adjudications = [e for e in entries if e.get("status", "").startswith("decision-")]
        self.assertEqual(adjudications[-1]["decider"], "llm")

    def test_the_cap_sends_the_next_fork_to_a_person(self):
        """T10 (second half): two llm rulings on this step, then the verdict
        message stops asking a subagent (§7, §15.7)."""
        for n in (1, 2):
            self.record(payload(work_state="stopped", output=None))
            self.answer_as(f"option: {n}\nkeep going", "llm")
        self.assertEqual(decision_mod.llm_adjudications(self.dec, "s1"), 2)
        _, message = self.record(payload(work_state="stopped", output=None))
        self.assertIn("--decider human", message)
        # still content-free: the cap sentence names no part of the payload
        self.assertNotIn("which join", message)

    def test_human_rulings_do_not_spend_the_cap(self):
        """T11: the fallback path a person is asked to answer must not count
        against the budget it is the fallback for."""
        for n in (1, 2):
            self.record(payload(work_state="stopped", output=None))
            self.answer_as(f"option: {n}\nkeep going", "human")
        self.assertEqual(decision_mod.llm_adjudications(self.dec, "s1"), 0)
        _, message = self.record(payload(work_state="stopped", output=None))
        self.assertNotIn("--decider human", message)

    def test_a_rejected_ruling_leaves_no_trace(self):
        """T12: the three rejections and a missing answer file share one exit,
        and neither writes a half-settled ledger (§15.4, §15.7)."""
        self.record(payload(work_state="stopped", output=None))
        for body in ("no option line here", "option: 9\nout of range",
                     "option: none\n"):
            with self.subTest(body=body):
                with self.assertRaises(stepio.StepIOError):
                    self.answer_as(body, "llm")
        # the delegation failing outright lands in the same place
        with self.assertRaises(stepio.StepIOError):
            stepio.adjudicate_answer(self.step, self.result, self.vars,
                                     self.root / "never-written.md", self.log,
                                     decider="llm")
        self.assertFalse((self.dec / "s1_d01_verdict.json").exists())
        self.assertEqual(json.loads(self.vars.read_text(encoding="utf-8")), {})
        self.assertEqual(decision_mod.llm_adjudications(self.dec, "s1"), 0)


def preambled(text, lines=("I examined both readings and cannot settle this alone.",
                           "Filing a decision request instead of picking one:")):
    """A payload the model wrapped in preamble prose -- the D9 shape."""
    return "\n".join([*lines, "", text])


class TestClaimDecisionBody(unittest.TestCase):
    """D9 grammar: first-token anchoring stays primary; the claim extends only
    to a line-anchored `DECISION:` line whose tail parses as a COMPLETE
    payload, so a mere mention still cannot be caught."""

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
    """The warning face (D9 4-2 residue / 4-4): line-anchored tokens inside a
    body that classified as none of them, reported but never reclassified."""

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
    """D9 wiring on the classification sites, plus the 4-4 contract: ERROR: and
    [BLOCKED: keep first-token anchoring (no parse gate exists for them, and a
    mid-body match would turn a real success into a failure)."""

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
    """D9 on the batch path: a preambled payload stops the run like a clean
    one, the filed request is anchored, and the answer machinery runs on it
    unchanged; a stray token in a success warns without reclassifying."""

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
        self.assertTrue(body.startswith("DECISION:"))  # preamble not filed
        self.assertEqual(record["preamble_lines"], 2)  # ...but audited
        self.assertTrue(record["a_eligible"])

    def test_answer_after_a_preambled_stop_takes_form_a(self):
        self.artifact()
        self.respond_text("DO-WORK", preambled(payload()))
        xml = self.wrap('<step id="s1" role="w" expect-file="art.txt" output="v" '
                        'output-type="value"><task>DO-WORK</task></step>')
        ex = self.execute(xml)
        with self.assertRaises(DecisionRequested):
            ex.run()
        calls_before = len(self.fake.calls)
        events = self.answer(self.run_dir, "s1", "option: 1\nagreed")
        ex2 = self.execute(xml, events=events)
        ex2.run()
        self.assertEqual(len(self.fake.calls), calls_before)  # no re-run
        self.assertEqual(ex2.vars["v"], "art.txt")

    def test_stray_token_in_a_success_warns_without_failing(self):
        self.respond_text("DO-WORK",
                          "fine result\nERROR: quoted from the tool log\nend")
        ex = self.execute(self.wrap(
            '<step id="s1" role="w"><task>DO-WORK</task></step>'))
        ex.run()  # still a success: no reclassification (4-4)
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


class TestRunLlmPreamble(RunLlmAdjudicationTestCase):
    """D9 on the run-llm path: record files the anchored payload and the
    request stays answerable; a stray token rides the ok message as a warning."""

    def test_preambled_payload_is_filed_anchored_and_answerable(self):
        (self.root / "art.txt").write_text("deliverable", encoding="utf-8")
        status, _ = self.record(preambled(payload()))
        self.assertEqual(status, "decision")
        filed = self.dec / "s1_d01_request.md"
        self.assertTrue(filed.read_text(encoding="utf-8").startswith("DECISION:"))
        status, message = self.answer("option: 1\nagreed")
        self.assertEqual(status, "ok")  # form (a) rode the anchored request
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
