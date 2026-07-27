"""Tests for the LLM-orchestrator helper subcommands (interp / eval)."""
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun.__main__ import main  # noqa: E402


class TestCliHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vars_path = str(Path(self.tmp.name) / "vars.json")
        Path(self.vars_path).write_text(
            json.dumps({"n": "3", "status": "ok", "path": "output/a.csv"}),
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue().strip()

    def test_interp(self):
        code, out = self.run_cli(["interp", "read {path} ({n} rows)", "--vars", self.vars_path])
        self.assertEqual(code, 0)
        self.assertEqual(out, "read output/a.csv (3 rows)")

    def test_interp_undefined_var(self):
        code, _ = self.run_cli(["interp", "{missing}", "--vars", self.vars_path])
        self.assertEqual(code, 2)

    def test_eval_true_false(self):
        code, out = self.run_cli(["eval", "int({n}) > 2", "--vars", self.vars_path])
        self.assertEqual((code, out), (0, "true"))
        code, out = self.run_cli(["eval", "'{status}' == 'ng'", "--vars", self.vars_path])
        self.assertEqual((code, out), (0, "false"))

    def test_eval_rejects_unsafe(self):
        code, _ = self.run_cli(["eval", "open('/etc/passwd')", "--vars", self.vars_path])
        self.assertEqual(code, 2)


class TestPromptRecord(unittest.TestCase):
    XML = """
<workflow name="t" version="2" max="10">
  <rules id="r1">RULE-BODY</rules>
  <step id="s1" rules="r1" mode="execute" model="opus" output="report_path">
    <role>ROLE-W</role>
    <task>Analyze {src} and write output/report.md.</task>
  </step>
  <step id="s2" output="count" output-type="value"
        schema='{"type":"object","properties":{"count":{"type":"integer"}},"required":["count"]}'>
    <role>ROLE-W</role>
    <task>Count it.</task>
  </step>
  <step id="s3" expect-file="{expected}">
    <role>ROLE-W</role>
    <task>Write the agreed file.</task>
  </step>
</workflow>
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.xml = str(self.dir / "wf.xml")
        Path(self.xml).write_text(self.XML, encoding="utf-8")
        self.vars_path = str(self.dir / "vars.json")
        Path(self.vars_path).write_text(json.dumps({"src": "data.csv"}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue().strip()

    def test_prompt_builds_file_and_prints_only_pointer(self):
        prompt_file = str(self.dir / "s1_prompt.md")
        result_file = str(self.dir / "s1_result.md")
        code, out = self.run_cli(["prompt", self.xml, "s1", "--vars", self.vars_path,
                                  "--out", prompt_file, "--result", result_file])
        self.assertEqual(code, 0)
        # stdout carries pointer + dispatch facts only, no task content
        self.assertIn("s1_prompt.md", out)
        self.assertIn("role=inline", out)
        self.assertIn("mode=execute", out)
        self.assertNotIn("data.csv", out)
        self.assertNotIn("ROLE-W", out)
        content = Path(prompt_file).read_text(encoding="utf-8")
        self.assertIn("Mode > Rules > Task > Role", content)  # _meta precedence
        self.assertIn("<role>\nROLE-W\n</role>", content)   # role injected
        self.assertIn("mode:execute", content)              # mode declaration
        self.assertIn("mode-output", content)               # _common.md rules
        self.assertIn("RULE-BODY", content)                 # rules injected
        self.assertIn("Analyze data.csv", content)          # interpolated task
        self.assertIn(result_file, content)                 # response protocol
        self.assertIn("ERROR:", content)                    # guardrails

    def test_prompt_dispatch_line_maps_model(self):
        from wfrun import modelmap
        map_path = self.dir / "mm.json"
        map_path.write_text(json.dumps({"llm": {"opus": "gpt-5-high"}}), encoding="utf-8")
        old = modelmap.MAP_PATH
        modelmap.MAP_PATH = map_path
        try:
            code, out = self.run_cli(["prompt", self.xml, "s1",
                                      "--vars", self.vars_path,
                                      "--out", str(self.dir / "p.md")])
        finally:
            modelmap.MAP_PATH = old
        self.assertEqual(code, 0)
        # runner-resolved name with the canonical origin for audit
        self.assertIn("model=gpt-5-high (mapped from opus)", out)

    def test_prompt_undefined_var(self):
        Path(self.vars_path).write_text("{}", encoding="utf-8")
        code, _ = self.run_cli(["prompt", self.xml, "s1", "--vars", self.vars_path,
                                "--out", str(self.dir / "p.md")])
        self.assertEqual(code, 2)

    def test_record_file_output(self):
        result = self.dir / "s1_result.md"
        result.write_text("a long report body", encoding="utf-8")
        log = self.dir / "steps.log"
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(result),
                                  "--vars", self.vars_path, "--log", str(log)])
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok (set report_path)")       # no payload in stdout
        variables = json.loads(Path(self.vars_path).read_text(encoding="utf-8"))
        self.assertEqual(variables["report_path"], str(result))
        self.assertIn('"status": "success"', log.read_text(encoding="utf-8"))

    def test_record_value_with_schema_unwrap(self):
        result = self.dir / "s2_result.md"
        result.write_text('{"count": 7}', encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s2", "--result", str(result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(Path(self.vars_path).read_text(encoding="utf-8"))["count"], 7)

    def test_record_error_result(self):
        result = self.dir / "s1_result.md"
        result.write_text("ERROR: connection refused", encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 1)
        self.assertIn("error", out)
        self.assertNotIn("connection refused", out)         # details stay in the file
        self.assertNotIn("report_path", json.loads(Path(self.vars_path).read_text(encoding="utf-8")))

    def test_record_schema_violation(self):
        result = self.dir / "s2_result.md"
        result.write_text("not json", encoding="utf-8")
        code, _ = self.run_cli(["record", self.xml, "s2", "--result", str(result),
                                "--vars", self.vars_path])
        self.assertEqual(code, 1)

    def test_record_strips_mode_line(self):
        # _common.md mandates a leading [Mode: x] line; it must not hide the
        # ERROR: protocol nor leak into extracted values.
        result = self.dir / "s1_result.md"
        result.write_text("[Mode: execute]\nERROR: connection refused", encoding="utf-8")
        code, _ = self.run_cli(["record", self.xml, "s1", "--result", str(result),
                                "--vars", self.vars_path])
        self.assertEqual(code, 1)

        result2 = self.dir / "s2_result.md"
        result2.write_text('[Mode: execute]\n{"count": 7}', encoding="utf-8")
        code, _ = self.run_cli(["record", self.xml, "s2", "--result", str(result2),
                                "--vars", self.vars_path])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(Path(self.vars_path).read_text(encoding="utf-8"))["count"], 7)


    def test_viz_stdout_and_file(self):
        code, out = self.run_cli(["viz", self.xml])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("flowchart TD"))
        self.assertNotIn("Analyze", out)  # no task bodies in the diagram
        mmd = self.dir / "wf.mmd"
        code, out = self.run_cli(["viz", self.xml, "--out", str(mmd)])
        self.assertEqual(code, 0)
        self.assertEqual(out, str(mmd))
        self.assertIn("flowchart TD", mmd.read_text(encoding="utf-8"))

    def test_record_expect_file(self):
        expected = self.dir / "made.txt"
        Path(self.vars_path).write_text(json.dumps({"expected": str(expected)}), encoding="utf-8")
        result = self.dir / "s3_result.md"
        result.write_text("OK wrote it")  # compliant-looking, but no artifact
        code, out = self.run_cli(["record", self.xml, "s3", "--result", str(result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 1)
        self.assertIn("expect-file", out)
        expected.write_text("data", encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s3", "--result", str(result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 0)

    def test_record_blocked_result(self):
        result = self.dir / "s1_result.md"
        result.write_text("[BLOCKED: mode-rule generate-target-artifacts] cannot", encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 1)
        self.assertIn("blocked", out)
        self.assertNotIn("generate-target-artifacts", out)  # details stay in file
        self.assertNotIn("report_path", json.loads(Path(self.vars_path).read_text(encoding="utf-8")))


class TestRunLlmProtocol(unittest.TestCase):
    """Phase 2.2 (reliability-spec.md §4): sentinel, handle.json, the
    record decision table (§4.2), and poll (§4.3)."""

    XML = """
<workflow name="t" version="2" max="10">
  <step id="s1" output="report_path"><role>W</role><task>write it</task></step>
  <step id="s2" output="count" output-type="value"><role>W</role><task>count</task></step>
  <replan id="r1" max-steps="5"><role>W</role><task>plan it</task></replan>
</workflow>
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.xml = str(self.dir / "wf.xml")
        Path(self.xml).write_text(self.XML, encoding="utf-8")
        self.vars_path = str(self.dir / "vars.json")
        Path(self.vars_path).write_text("{}", encoding="utf-8")
        self.result = self.dir / "s1_result.md"
        self.handle = self.dir / "s1_handle.json"
        self.prompt_file = str(self.dir / "s1_prompt.md")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue().strip()

    def dispatch(self, step_id="s1", attempt=1):
        prompt_file = str(self.dir / f"{step_id}_prompt.md")
        result_file = str(self.dir / f"{step_id}_result.md")
        code, _ = self.run_cli(
            ["prompt", self.xml, step_id, "--vars", self.vars_path,
             "--out", prompt_file, "--result", result_file, "--attempt", str(attempt)])
        self.assertEqual(code, 0)
        return prompt_file, result_file

    # ---- prompt: handle.json / sentinel / stale-result deletion ----

    def test_prompt_writes_handle_json(self):
        prompt_file, result_file = self.dispatch(attempt=2)
        handle = json.loads((self.dir / "s1_handle.json").read_text(encoding="utf-8"))
        self.assertEqual(handle["step_id"], "s1")
        self.assertEqual(handle["attempt"], 2)
        self.assertEqual(handle["result_path"], result_file)
        self.assertEqual(handle["sentinel"], "[[WFRUN-END step=s1]]")
        self.assertIsInstance(handle["dispatched_at"], (int, float))
        self.assertIsInstance(handle["timeout"], int)

    def test_prompt_writes_sentinel_and_reply_instructions(self):
        prompt_file, _ = self.dispatch()
        content = Path(prompt_file).read_text(encoding="utf-8")
        self.assertIn("[[WFRUN-END step=s1]]", content)
        self.assertIn('OK s1', content)

    def test_prompt_deletes_stale_result_file(self):
        result_file = self.dir / "s1_result.md"
        result_file.write_text("stale leftover from a prior attempt",
                               encoding="utf-8")
        self.dispatch()
        self.assertFalse(result_file.is_file())

    def test_prompt_replan_has_no_handle_no_sentinel(self):
        prompt_file = str(self.dir / "r1_prompt.md")
        result_file = str(self.dir / "r1_result.md")
        code, _ = self.run_cli(
            ["prompt", self.xml, "r1", "--vars", self.vars_path,
             "--out", prompt_file, "--result", result_file])
        self.assertEqual(code, 0)
        self.assertFalse((self.dir / "r1_handle.json").is_file())
        content = Path(prompt_file).read_text(encoding="utf-8")
        self.assertNotIn("WFRUN-END", content)

    # ---- record decision table (reliability-spec.md §4.2) ----

    def test_row1_missing_file_reply_claims_ok_is_error(self):
        self.dispatch()
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path, "--reply", "OK s1"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "error: claimed-ok-but-no-result")

    def test_row2_missing_file_no_reply_is_aborted(self):
        self.dispatch()
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 3)
        self.assertEqual(out, "aborted: result file not found")

    def test_row2_legacy_missing_file_no_reply_is_also_aborted(self):
        # No handle.json (no prompt --result ever ran): rows 1-2 are not
        # handle-gated, only the sentinel check (row 3) is.
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 3)
        self.assertEqual(out, "aborted: result file not found")

    def test_row3_missing_sentinel_no_reply_is_aborted(self):
        self.dispatch()
        self.result.write_text("a partial answer with no end marker",
                               encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 3)
        self.assertEqual(out, "aborted: incomplete (no end marker)")

    def test_row3_missing_sentinel_reply_claims_ok_is_error(self):
        self.dispatch()
        self.result.write_text("looks done but forgot the marker",
                               encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path, "--reply", "OK s1"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "error: claimed-ok-but-no-result")

    def test_row3_no_handle_sentinel_like_text_is_not_stripped(self):
        # Legacy (no handle): sentinel checking is skipped entirely, so a
        # line that merely looks like the marker is part of the output.
        self.result.write_text("real content\n[[WFRUN-END step=s1]]",
                               encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s2", "--result", str(self.result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 0)
        value = json.loads(Path(self.vars_path).read_text(encoding="utf-8"))["count"]
        self.assertIn("WFRUN-END", value)

    def test_row4_empty_body_is_error(self):
        self.dispatch()
        self.result.write_text("\n" + "[[WFRUN-END step=s1]]", encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 1)
        self.assertEqual(out, "error: empty result")

    def test_row5_error_prefix_current_behavior(self):
        self.dispatch()
        self.result.write_text("ERROR: boom\n[[WFRUN-END step=s1]]", encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 1)
        self.assertIn("error", out)
        self.assertNotIn("boom", out)  # details stay in the file

    def test_row6_reply_claims_error_but_file_is_clean_is_mismatch(self):
        self.dispatch()
        self.result.write_text("a perfectly good report\n[[WFRUN-END step=s1]]",
                               encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path, "--reply", "ERROR: something"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "error: reply/file mismatch")

    def test_row7_success_strips_sentinel_from_extracted_value(self):
        self.dispatch(step_id="s2")
        result2 = self.dir / "s2_result.md"
        result2.write_text("42\n[[WFRUN-END step=s2]]", encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s2", "--result", str(result2),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 0)
        value = json.loads(Path(self.vars_path).read_text(encoding="utf-8"))["count"]
        self.assertEqual(value, "42")
        self.assertNotIn("WFRUN-END", value)

    def test_row7_success_with_reply_ok_matches(self):
        self.dispatch()
        self.result.write_text("done\n[[WFRUN-END step=s1]]", encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path, "--reply", "OK s1"])
        self.assertEqual(code, 0)

    def test_aborted_status_written_to_log(self):
        self.dispatch()
        log = self.dir / "steps.log"
        self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                     "--vars", self.vars_path, "--log", str(log)])
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        self.assertEqual(entry["status"], "aborted")

    def test_attempts_json_written_when_handle_present(self):
        self.dispatch(attempt=3)
        self.result.write_text("done\n[[WFRUN-END step=s1]]", encoding="utf-8")
        self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                     "--vars", self.vars_path])
        attempts = json.loads((self.dir / "s1_attempts.json").read_text(encoding="utf-8"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["seq"], 3)
        self.assertEqual(attempts[0]["class"], "ok")
        self.assertIn("ended_at", attempts[0])

    def test_no_attempts_json_without_handle(self):
        self.result.write_text("done", encoding="utf-8")
        self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                     "--vars", self.vars_path])
        self.assertFalse((self.dir / "s1_attempts.json").is_file())

    # ---- poll (reliability-spec.md §4.3) ----

    def test_poll_done(self):
        self.dispatch()
        self.result.write_text("done\n[[WFRUN-END step=s1]]", encoding="utf-8")
        code, out = self.run_cli(["poll", str(self.handle)])
        self.assertEqual(code, 0)
        self.assertEqual(out, "done")

    def test_poll_running(self):
        self.dispatch()
        code, out = self.run_cli(["poll", str(self.handle)])
        self.assertEqual(code, 10)
        self.assertEqual(out, "running")

    def test_poll_deadline_exceeded(self):
        self.handle.write_text(json.dumps({
            "step_id": "s1", "attempt": 1,
            "dispatched_at": time.time() - 9999, "timeout": 1,
            "result_path": str(self.result), "sentinel": "[[WFRUN-END step=s1]]",
        }), encoding="utf-8")
        code, out = self.run_cli(["poll", str(self.handle)])
        self.assertEqual(code, 11)
        self.assertEqual(out, "deadline-exceeded")

    def test_poll_partial_file_without_sentinel_is_running_or_deadline(self):
        self.dispatch()
        self.result.write_text("partial, still writing...", encoding="utf-8")
        code, out = self.run_cli(["poll", str(self.handle)])
        self.assertEqual(code, 10)
        self.assertEqual(out, "running")


class TestModeHelpers(unittest.TestCase):
    def test_strip_mode_line(self):
        from wfrun.modes import strip_mode_line
        self.assertEqual(strip_mode_line("[Mode: execute]\npath.txt"), "path.txt")
        self.assertEqual(strip_mode_line("no mode line"), "no mode line")
        # only one leading line is stripped; mid-text brackets stay intact
        self.assertEqual(strip_mode_line("body\n[Mode: x]\n"), "body\n[Mode: x]\n")

    def test_blocked_line(self):
        from wfrun.modes import blocked_line
        self.assertEqual(blocked_line("[BLOCKED: mode-rule x] reason\nrest"),
                         "[BLOCKED: mode-rule x] reason")
        # tolerates a legacy leading [Mode:] line
        self.assertIsNotNone(blocked_line("[Mode: survey]\n[BLOCKED: rules r1] no"))
        # first-line anchored: mid-text mentions do not trip it
        self.assertIsNone(blocked_line("report mentions [BLOCKED: x] token"))
        self.assertIsNone(blocked_line("ordinary output"))

    def test_cli_result_error_detection_behind_mode_line(self):
        from wfrun.claude_cli import strip_mode_line
        self.assertTrue(strip_mode_line("[Mode: debug]\nERROR: y")
                        .lstrip().startswith("ERROR:"))

    def test_unwrap_value_strips_mode_line(self):
        from wfrun.stepio import unwrap_value
        self.assertEqual(unwrap_value(None, "[Mode: execute]\noutput/a.csv"),
                         "output/a.csv")


if __name__ == "__main__":
    unittest.main()
