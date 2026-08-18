"""Rule 6 — the VALUE: line protocol (xml-wf-decision-request.md §18.6).

What these tests pin is the *contract*, not the wording: the wording is an eval
subject and will move, so assertions compare against the guardrails constants
rather than quoting them. What must not move is which steps are asked for the
line, how the line is read back, that a missing line degrades to the pre-rule-6
behaviour instead of failing, and that a transcribed placeholder is not mistaken
for a value.

The predecessor of this rule stated the requirement as prose and was measured at
0 bare values in 28 samples across three substrates, then reverted; the labeled
line exists because every steering this prompt has actually achieved has been a
labeled line. That history is why the "no line" path here is a *reported*
fallback and not a failure -- fail-open keeps workflows written before the rule
working unchanged.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import model, stepio  # noqa: E402
from wfrun.guardrails import (GUARDRAILS, VALUE_LINE_PLACEHOLDER,  # noqa: E402
                              VALUE_LINE_PREFIX, VALUE_LINE_RULE)


def step(**kw) -> model.Step:
    kw.setdefault("id", "s1")
    kw.setdefault("task", "Count the rows.")
    return model.Step(**kw)


class TestRuleApplies(unittest.TestCase):
    """The declaration alone decides — nothing is inferred from the task."""

    def test_value_typed_output_var_applies(self):
        self.assertTrue(stepio.value_output_rule_applies(
            step(output="n", output_type="value")))

    def test_file_typed_does_not_apply(self):
        self.assertFalse(stepio.value_output_rule_applies(
            step(output="n", output_type="file")))

    def test_default_output_type_does_not_apply(self):
        # DEFAULT_OUTPUT_TYPE is "file", so `output=` without `output-type=`
        # is file-typed and must not be asked for a value line.
        self.assertEqual(model.DEFAULT_OUTPUT_TYPE, "file")
        self.assertFalse(stepio.value_output_rule_applies(step(output="n")))

    def test_no_output_var_does_not_apply(self):
        # Nothing consumes this step's response as a value; the line would name
        # a variable that does not exist.
        self.assertFalse(stepio.value_output_rule_applies(
            step(output=None, output_type="value")))

    def test_schema_excludes(self):
        # A schema forces a JSON final response: a line template would be a
        # second, contradictory shape in the same prompt.
        self.assertFalse(stepio.value_output_rule_applies(
            step(output="n", output_type="value", schema='{"type":"object"}')))


class TestPromptInjection(unittest.TestCase):
    def build(self, st, **kw):
        wf = model.Workflow(name="t", version="2", max=1)
        return stepio.build_step_prompt_parts(
            wf, st, {}, base_dir=".", agents_cache={}, **kw)

    def test_value_step_gets_the_rule(self):
        _, user = self.build(step(output="n", output_type="value"))
        self.assertIn(VALUE_LINE_RULE, user)

    def test_file_step_prompt_ends_at_guardrails(self):
        """The regression guarantee the cheap A/B rests on: a step that is not
        value-typed receives exactly the old prompt, so arms that declare no
        `output=` cannot be diluted by construction."""
        _, user = self.build(step(output="report", output_type="file"))
        self.assertTrue(user.endswith(GUARDRAILS))
        self.assertNotIn(VALUE_LINE_RULE, user)

    def test_no_output_step_prompt_ends_at_guardrails(self):
        _, user = self.build(step())
        self.assertTrue(user.endswith(GUARDRAILS))

    def test_schema_step_does_not_get_the_rule(self):
        _, user = self.build(
            step(output="n", output_type="value", schema='{"type":"object"}'))
        self.assertNotIn(VALUE_LINE_RULE, user)

    def test_rule_follows_guardrails(self):
        """Rule 6 reads as a continuation of the numbered block, so it comes
        after it — and it refers to rule 4, which is inside it."""
        _, user = self.build(step(output="n", output_type="value"))
        self.assertLess(user.index(GUARDRAILS), user.index(VALUE_LINE_RULE))

    def test_guardrails_constant_is_not_mutated(self):
        """Rule 6 is appended, never spliced into GUARDRAILS — that is what
        keeps §18.4a's "restore guardrails.py alone" A/B recipe usable."""
        self.assertNotIn(VALUE_LINE_RULE, GUARDRAILS)
        # Line-anchored: a rule numbered 6 growing inside GUARDRAILS would
        # collide, but a mere "§18.6" in a value or comment must not trip this.
        self.assertNotIn("\n6. ", GUARDRAILS)

    def test_rule_asks_for_no_position(self):
        """It must not demand the line be last: run-llm's result file needs the
        sentinel there (stepio.sentinel_line), and a position demand would put
        the two requirements in conflict."""
        self.assertNotIn("end with", VALUE_LINE_RULE.lower())
        self.assertNotIn("last line", VALUE_LINE_RULE.lower())


class TestExtraction(unittest.TestCase):
    def test_plain_line(self):
        self.assertEqual(stepio.extract_value_line(f"{VALUE_LINE_PREFIX} 375"),
                         ("375", stepio.VALUE_LINE_PRESENT))

    def test_line_after_prose(self):
        text = f"I read orders.csv and summed the column.\n{VALUE_LINE_PREFIX} 375"
        self.assertEqual(stepio.extract_value_line(text),
                         ("375", stepio.VALUE_LINE_PRESENT))

    def test_line_before_prose(self):
        """No position is demanded, so a line that is not last still counts."""
        text = f"{VALUE_LINE_PREFIX} 375\nWrote breakdown.md as well."
        self.assertEqual(stepio.extract_value_line(text),
                         ("375", stepio.VALUE_LINE_PRESENT))

    def test_last_match_wins(self):
        """A step that quotes the template while explaining itself and then
        writes the real line lands on the real one."""
        text = (f"I will report it as `{VALUE_LINE_PREFIX} <n>`.\n"
                f"{VALUE_LINE_PREFIX} 375")
        self.assertEqual(stepio.extract_value_line(text),
                         ("375", stepio.VALUE_LINE_PRESENT))

    def test_absent(self):
        self.assertEqual(stepio.extract_value_line("The total is 375."),
                         (None, stepio.VALUE_LINE_ABSENT))

    def test_placeholder_is_not_a_value(self):
        """The §12-measured failure: copying the template's own filler. Storing
        it would claim an extraction succeeded while contaminating the variable
        exactly as §18.6 describes."""
        text = f"{VALUE_LINE_PREFIX} {VALUE_LINE_PLACEHOLDER}"
        self.assertEqual(stepio.extract_value_line(text),
                         (None, stepio.VALUE_LINE_PLACEHOLDER_MARK))

    def test_prefix_must_start_the_line(self):
        """Syntactic anchoring — the same discipline ERROR: keeps. A mention
        mid-sentence is not a protocol line."""
        self.assertEqual(
            stepio.extract_value_line(f"the answer ({VALUE_LINE_PREFIX} 375)"),
            (None, stepio.VALUE_LINE_ABSENT))

    def test_case_sensitive(self):
        self.assertEqual(stepio.extract_value_line("value: 375"),
                         (None, stepio.VALUE_LINE_ABSENT))

    def test_empty_value_is_not_a_placeholder(self):
        self.assertEqual(stepio.extract_value_line(f"{VALUE_LINE_PREFIX}"),
                         ("", stepio.VALUE_LINE_PRESENT))


class TestUnwrap(unittest.TestCase):
    """unwrap_value is what actually reaches the variable at all three call
    sites, so the fallback contract is pinned here rather than on the extractor
    alone."""

    def test_line_wins_over_body(self):
        text = f"Wrote breakdown.md.\n{VALUE_LINE_PREFIX} 375"
        value, marker = stepio.unwrap_value_marked(None, text)
        self.assertEqual(value, "375")
        self.assertEqual(marker, stepio.VALUE_LINE_PRESENT)

    def test_fail_open_keeps_the_whole_body(self):
        """Pre-rule-6 behaviour, unchanged, for a step that writes no line —
        including every workflow authored before this rule existed."""
        text = "output/line_count.txt"
        value, marker = stepio.unwrap_value_marked(None, text)
        self.assertEqual(value, "output/line_count.txt")
        self.assertEqual(marker, stepio.VALUE_LINE_ABSENT)

    def test_placeholder_falls_open_too(self):
        text = f"{VALUE_LINE_PREFIX} {VALUE_LINE_PLACEHOLDER}"
        value, marker = stepio.unwrap_value_marked(None, text)
        self.assertEqual(value, text)
        self.assertEqual(marker, stepio.VALUE_LINE_PLACEHOLDER_MARK)

    def test_structured_path_reports_no_marker(self):
        """A schema step never saw the rule, so there is nothing to report."""
        value, marker = stepio.unwrap_value_marked({"line_count": 3}, "")
        self.assertEqual(value, 3)
        self.assertIsNone(marker)

    def test_mode_line_stripped_before_matching(self):
        text = f"[Mode: execute]\n{VALUE_LINE_PREFIX} 375"
        self.assertEqual(stepio.unwrap_value(None, text), "375")

    def test_unwrap_value_signature_unchanged(self):
        self.assertEqual(stepio.unwrap_value(None, f"{VALUE_LINE_PREFIX} 7"), "7")


class TestReportSuffix(unittest.TestCase):
    """Content-free by construction: the suffix names a shape, never a value
    (the run-llm firewall line _with_stray_warning already walks)."""

    def test_present_is_silent(self):
        self.assertEqual(stepio.value_line_suffix(stepio.VALUE_LINE_PRESENT), "")

    def test_no_marker_is_silent(self):
        self.assertEqual(stepio.value_line_suffix(None), "")

    def test_absent_and_placeholder_are_named_distinctly(self):
        absent = stepio.value_line_suffix(stepio.VALUE_LINE_ABSENT)
        placeholder = stepio.value_line_suffix(stepio.VALUE_LINE_PLACEHOLDER_MARK)
        self.assertTrue(absent and placeholder)
        self.assertNotEqual(absent, placeholder)

    def test_suffix_carries_no_value_text(self):
        for marker in (stepio.VALUE_LINE_ABSENT,
                       stepio.VALUE_LINE_PLACEHOLDER_MARK):
            self.assertNotIn("375", stepio.value_line_suffix(marker))


class TestSentinelOrder(unittest.TestCase):
    """run-llm reads the result file mode-line first, then this step's
    sentinel, then the value — the sentinel must not end up inside the value
    and must not hide the line."""

    def test_sentinel_stripped_then_line_extracted(self):
        raw = (f"Wrote the file.\n{VALUE_LINE_PREFIX} 375\n"
               f"{stepio.sentinel_line('v3')}\n")
        body, present = stepio.strip_sentinel_line(raw, "v3")
        self.assertTrue(present)
        value, marker = stepio.unwrap_value_marked(None, body)
        self.assertEqual(value, "375")
        self.assertEqual(marker, stepio.VALUE_LINE_PRESENT)

    def test_sentinel_is_not_mistaken_for_the_line(self):
        value, marker = stepio.unwrap_value_marked(
            None, f"{stepio.sentinel_line('v3')}")
        self.assertEqual(marker, stepio.VALUE_LINE_ABSENT)


class TestBatchWiring(unittest.TestCase):
    """The batch facet (run-cc and run-pi share this path: pi differs only in
    which launcher Executor is handed, not in how the value is unwrapped)."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_executor import ExecutorTestCase  # noqa: E402
        self.case = ExecutorTestCase("run")
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def run_wf(self, response):
        from wfrun.claude_cli import CliResult
        from wfrun.state import load_events
        ex = self.case.execute(self.case.wrap(
            '<step id="s1" role="w" output="v" output-type="value">'
            '<task>count</task></step>'))
        self.case.fake.handlers.append(
            (lambda _p: True, CliResult(ok=True, text=response, cost_usd=0.01)))
        ex.run()
        events = load_events(ex.run_dir)
        step_ev = [e for e in events if e.get("kind") == "step"][0]
        return ex, step_ev

    def test_line_extracted_and_event_marks_present(self):
        ex, ev = self.run_wf(f"Counted them.\n{VALUE_LINE_PREFIX} 42")
        self.assertEqual(ex.vars["v"], "42")
        self.assertEqual(ev["value_line"], stepio.VALUE_LINE_PRESENT)

    def test_absent_line_is_fail_open_and_marked(self):
        ex, ev = self.run_wf("The count is 42.")
        self.assertEqual(ex.vars["v"], "The count is 42.")
        self.assertEqual(ev["value_line"], stepio.VALUE_LINE_ABSENT)

    def test_placeholder_marked(self):
        ex, ev = self.run_wf(f"{VALUE_LINE_PREFIX} {VALUE_LINE_PLACEHOLDER}")
        self.assertEqual(ev["value_line"], stepio.VALUE_LINE_PLACEHOLDER_MARK)

    def test_file_typed_step_reports_no_marker(self):
        from wfrun.claude_cli import CliResult
        from wfrun.state import load_events
        ex = self.case.execute(self.case.wrap(
            '<step id="s1" role="w" output="p"><task>write</task></step>'))
        self.case.fake.handlers.append(
            (lambda _p: True, CliResult(ok=True, text="wrote it", cost_usd=0.01)))
        ex.run()
        events = load_events(ex.run_dir)
        step_ev = [e for e in events if e.get("kind") == "step"][0]
        self.assertIsNone(step_ev["value_line"])


class TestReplayDoesNotReExtract(unittest.TestCase):
    """A resumed run must adopt the value recorded at original run time, not
    re-derive it: _exec_step returns from the replay branch before
    _finish_step, so extraction cannot run a second time. Pinned because a
    future refactor that routed replay through _finish_step would silently
    change resumed values."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_executor import ExecutorTestCase  # noqa: E402
        self.case = ExecutorTestCase("run")
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def test_recorded_output_value_is_adopted_verbatim(self):
        recorded = [{"kind": "step", "key": "s1", "status": "success",
                     "output_var": "v",
                     # A body that WOULD extract differently if re-run through
                     # the extractor: replay must not touch it.
                     "output_value": f"prose\n{VALUE_LINE_PREFIX} 42",
                     "cost_usd": 0.0}]
        ex = self.case.execute(self.case.wrap(
            '<step id="s1" role="w" output="v" output-type="value">'
            '<task>count</task></step>'), events=recorded)
        ex.run()
        self.assertEqual(ex.vars["v"], f"prose\n{VALUE_LINE_PREFIX} 42")
        self.assertEqual(self.case.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
