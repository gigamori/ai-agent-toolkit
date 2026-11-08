import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import parser, stepio  # noqa: E402
from wfrun.__main__ import main  # noqa: E402

_NO_SESSION = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""})

_DENIAL_LINE = {"type": "user", "toolDenialKind": "permission-rule",
                "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "toolu_1",
                    "is_error": True, "content": "refused"}]}}


def setUpModule():
    _NO_SESSION.start()


def tearDownModule():
    _NO_SESSION.stop()


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
  <step id="s1" rules="r1" mode="execute" model="ultra" output="report_path">
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
        steps = self.dir / "run" / "steps"
        steps.mkdir(parents=True)
        prompt_file = str(steps / "s1_prompt.md")
        result_file = str(steps / "s1_result.md")
        code, out = self.run_cli(["prompt", self.xml, "s1", "--vars", self.vars_path,
                                  "--out", prompt_file, "--result", result_file])
        self.assertEqual(code, 0)
        self.assertIn("s1_prompt.md", out)
        self.assertIn("role=inline", out)
        self.assertIn("mode=execute", out)
        self.assertNotIn("data.csv", out)
        self.assertNotIn("ROLE-W", out)
        content = Path(prompt_file).read_text(encoding="utf-8")
        self.assertIn("Mode > Rules > Task > Role", content)
        self.assertIn("<role>\nROLE-W\n</role>", content)
        self.assertIn("mode:execute", content)
        self.assertIn("mode-output", content)
        self.assertIn("RULE-BODY", content)
        self.assertIn("Analyze data.csv", content)
        handle = json.loads((steps / "s1_handle.json").read_text(encoding="utf-8"))
        self.assertIn(handle["result_path"], content)
        self.assertIn("ERROR:", content)

    def test_prompt_dispatch_line_maps_model(self):
        from wfrun import modelmap
        map_path = self.dir / "mm.json"
        map_path.write_text(json.dumps({"llm": {"ultra": "gpt-5-high"}}), encoding="utf-8")
        old = modelmap.MAP_PATH
        modelmap.MAP_PATH = map_path
        try:
            code, out = self.run_cli(["prompt", self.xml, "s1",
                                      "--vars", self.vars_path,
                                      "--out", str(self.dir / "p.md")])
        finally:
            modelmap.MAP_PATH = old
        self.assertEqual(code, 0)
        self.assertIn("model=gpt-5-high (mapped from ultra)", out)

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
        self.assertEqual(out, "ok (set report_path)")
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
        self.assertNotIn("connection refused", out)
        self.assertNotIn("report_path", json.loads(Path(self.vars_path).read_text(encoding="utf-8")))

    def test_record_schema_violation(self):
        result = self.dir / "s2_result.md"
        result.write_text("not json", encoding="utf-8")
        code, _ = self.run_cli(["record", self.xml, "s2", "--result", str(result),
                                "--vars", self.vars_path])
        self.assertEqual(code, 1)

    def test_record_strips_mode_line(self):
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
        self.assertNotIn("Analyze", out)
        mmd = self.dir / "wf.mmd"
        code, out = self.run_cli(["viz", self.xml, "--out", str(mmd)])
        self.assertEqual(code, 0)
        self.assertEqual(out, str(mmd))
        self.assertIn("flowchart TD", mmd.read_text(encoding="utf-8"))

    def test_record_expect_file(self):
        expected = self.dir / "made.txt"
        Path(self.vars_path).write_text(json.dumps({"expected": str(expected)}), encoding="utf-8")
        result = self.dir / "s3_result.md"
        result.write_text("OK wrote it")
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
        self.assertNotIn("generate-target-artifacts", out)
        self.assertNotIn("report_path", json.loads(Path(self.vars_path).read_text(encoding="utf-8")))


class TestRunLlmProtocol(unittest.TestCase):
    XML = """
<workflow name="t" version="2" max="10">
  <step id="s1" output="report_path"><role>W</role><task>write it</task></step>
  <step id="s2" output="count" output-type="value"><role>W</role><task>count</task></step>
  <replan id="r1" max-steps="5"><role>W</role><task>plan it</task></replan>
  <replan id="r2" max-steps="5"><task>plan it role-less</task></replan>
</workflow>
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.xml = str(self.dir / "wf.xml")
        Path(self.xml).write_text(self.XML, encoding="utf-8")
        self.vars_path = str(self.dir / "vars.json")
        Path(self.vars_path).write_text("{}", encoding="utf-8")
        self.steps = self.dir / "run" / "steps"
        self.steps.mkdir(parents=True)
        self.result = self.steps / "s1_result.md"
        self.handle = self.steps / "s1_handle.json"
        self.prompt_file = str(self.steps / "s1_prompt.md")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue().strip()

    def dispatch(self, step_id="s1"):
        prompt_file = str(self.steps / f"{step_id}_prompt.md")
        result_file = str(self.steps / f"{step_id}_result.md")
        code, _ = self.run_cli(
            ["prompt", self.xml, step_id, "--vars", self.vars_path,
             "--out", prompt_file, "--result", result_file])
        self.assertEqual(code, 0)
        return prompt_file, self.attempt(step_id)

    def attempt(self, step_id="s1"):
        return json.loads((self.steps / f"{step_id}_handle.json").read_text(encoding="utf-8"))

    def write_result(self, body, step_id="s1", sentinel=True):
        handle = self.attempt(step_id)
        text = f"{body}\n{handle['sentinel']}" if sentinel else body
        Path(handle["result_path"]).write_text(text, encoding="utf-8")

    def test_prompt_writes_handle_json(self):
        _, handle = self.dispatch()
        self.assertEqual(handle["step_id"], "s1")
        self.assertEqual((handle["cycle"], handle["attempt"]), (1, 1))
        self.assertEqual(handle["result_base"], str(self.result))
        self.assertNotEqual(handle["result_path"], str(self.result))
        self.assertTrue(handle["sentinel"].startswith("[[WFRUN-END step=s1 attempt=c01a01-"),
                        handle["sentinel"])
        self.assertIsInstance(handle["dispatched_at"], (int, float))
        self.assertIsInstance(handle["timeout"], int)

    def test_prompt_writes_sentinel_and_reply_instructions(self):
        prompt_file, handle = self.dispatch()
        content = Path(prompt_file).read_text(encoding="utf-8")
        self.assertIn(handle["sentinel"], content)
        self.assertIn('OK s1', content)

    def test_prompt_leaves_a_file_at_the_step_path_alone(self):
        self.result.write_text("stale leftover from a prior attempt",
                               encoding="utf-8")
        self.dispatch()
        self.assertEqual(self.result.read_text(encoding="utf-8"),
                         "stale leftover from a prior attempt")
        code, out = self.run_cli(["poll", str(self.handle)])
        self.assertEqual((code, out), (10, "running"),
                         "only the attempt's own file can complete the attempt")

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

    def test_prompt_role_less_replan_has_no_leading_blank_lines(self):
        prompt_file = str(self.dir / "r2_prompt.md")
        result_file = str(self.dir / "r2_result.md")
        code, _ = self.run_cli(
            ["prompt", self.xml, "r2", "--vars", self.vars_path,
             "--out", prompt_file, "--result", result_file])
        self.assertEqual(code, 0)
        content = Path(prompt_file).read_text(encoding="utf-8")
        self.assertFalse(content.startswith("\n"))
        self.assertTrue(content.startswith("You are a continuation planner"))

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
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 3)
        self.assertEqual(out, "aborted: result file not found")

    def test_row3_missing_sentinel_no_reply_is_aborted(self):
        self.dispatch()
        self.write_result("a partial answer with no end marker", sentinel=False)
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 3)
        self.assertEqual(out, "aborted: incomplete (no end marker)")

    def test_row3_missing_sentinel_reply_claims_ok_is_error(self):
        self.dispatch()
        self.write_result("looks done but forgot the marker", sentinel=False)
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path, "--reply", "OK s1"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "error: claimed-ok-but-no-result")

    def test_row3_no_handle_sentinel_like_text_is_not_stripped(self):
        self.result.write_text("real content\n[[WFRUN-END step=s1]]",
                               encoding="utf-8")
        code, out = self.run_cli(["record", self.xml, "s2", "--result", str(self.result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 0)
        value = json.loads(Path(self.vars_path).read_text(encoding="utf-8"))["count"]
        self.assertIn("WFRUN-END", value,
                      "with no handle the sentinel check is skipped "
                      "entirely, so marker-looking text is ordinary output")

    def test_row4_empty_body_is_error(self):
        self.dispatch()
        self.write_result("")
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 1)
        self.assertEqual(out, "error: empty result")

    def test_row5_error_prefix_current_behavior(self):
        self.dispatch()
        self.write_result("ERROR: boom")
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 1)
        self.assertIn("error", out)
        self.assertNotIn("boom", out)

    def test_row6_reply_claims_error_but_file_is_clean_is_mismatch(self):
        self.dispatch()
        self.write_result("a perfectly good report")
        code, out = self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                                  "--vars", self.vars_path, "--reply", "ERROR: something"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "error: reply/file mismatch")

    def test_row7_success_strips_sentinel_from_extracted_value(self):
        self.dispatch(step_id="s2")
        self.write_result("42", step_id="s2")
        code, out = self.run_cli(["record", self.xml, "s2",
                                  "--result", str(self.steps / "s2_result.md"),
                                  "--vars", self.vars_path])
        self.assertEqual(code, 0)
        value = json.loads(Path(self.vars_path).read_text(encoding="utf-8"))["count"]
        self.assertEqual(value, "42")
        self.assertNotIn("WFRUN-END", value)

    def test_row7_success_with_reply_ok_matches(self):
        self.dispatch()
        self.write_result("done")
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
        self.dispatch()
        self.write_result("done")
        self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                     "--vars", self.vars_path])
        attempts = json.loads((self.steps / "s1_c01_attempts.json").read_text(encoding="utf-8"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["seq"], 1)
        self.assertEqual(attempts[0]["class"], "ok")
        self.assertIn("ended_at", attempts[0])

    def test_no_attempts_json_without_handle(self):
        self.result.write_text("done", encoding="utf-8")
        self.run_cli(["record", self.xml, "s1", "--result", str(self.result),
                     "--vars", self.vars_path])
        self.assertFalse((self.steps / "s1_attempts.json").is_file())
        self.assertFalse((self.steps / "s1_c01_attempts.json").is_file())

    def test_poll_done(self):
        self.dispatch()
        self.write_result("done")
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
        self.write_result("partial, still writing...", sentinel=False)
        code, out = self.run_cli(["poll", str(self.handle)])
        self.assertEqual(code, 10)
        self.assertEqual(out, "running")


class TestRecordDenyCheck(unittest.TestCase):
    XML = """
<workflow name="t" version="2" max="10">
  <step id="s1" output="report_path" retry="1"><role>W</role><task>write it</task></step>
</workflow>
"""
    SESSION = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.xml = str(self.dir / "wf.xml")
        Path(self.xml).write_text(self.XML, encoding="utf-8")
        self.vars_path = str(self.dir / "vars.json")
        Path(self.vars_path).write_text("{}", encoding="utf-8")
        self.steps = self.dir / "run" / "steps"
        self.steps.mkdir(parents=True)
        self.result = self.steps / "s1_result.md"
        self.log = self.dir / "run" / "steps.log"
        config = self.dir / "config"
        self.subagents = config / "projects" / "proj" / self.SESSION / "subagents"
        env = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": self.SESSION,
                                           "CLAUDE_CONFIG_DIR": str(config)})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue().strip()

    def dispatch(self):
        code, out = self.run_cli(
            ["prompt", self.xml, "s1", "--vars", self.vars_path,
             "--out", str(self.steps / "s1_prompt.md"), "--result", str(self.result)])
        self.assertEqual(code, 0)
        return self.attempt()["delegation"], out

    def attempt(self):
        return json.loads((self.steps / "s1_handle.json").read_text(encoding="utf-8"))

    def subagent(self, description, lines):
        self.subagents.mkdir(parents=True, exist_ok=True)
        (self.subagents / "agent-a1.meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "description": description}),
            encoding="utf-8")
        (self.subagents / "agent-a1.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    def finish(self, body="wrote it"):
        handle = self.attempt()
        Path(handle["result_path"]).write_text(f"{body}\n{handle['sentinel']}\n",
                                               encoding="utf-8")

    def record(self, reply="OK s1"):
        argv = ["record", self.xml, "s1", "--result", str(self.result),
                "--vars", self.vars_path, "--log", str(self.log)]
        if reply:
            argv += ["--reply", reply]
        return self.run_cli(argv)

    def log_entry(self):
        lines = [l for l in self.log.read_text(encoding="utf-8").splitlines() if l.strip()]
        return json.loads(lines[-1])

    def test_prompt_issues_a_delegation_description(self):
        delegation, out = self.dispatch()
        self.assertTrue(delegation.startswith("wfrun s1 c01 a01 "), delegation)
        self.assertIn(f"description={delegation}", out)

    def test_each_attempt_gets_its_own_description(self):
        first, _ = self.dispatch()
        self.finish("ERROR: failed")
        self.record(reply=None)
        second, _ = self.dispatch()
        self.assertNotEqual(first, second)

    def test_denial_in_the_subagent_overrides_an_ok_result(self):
        delegation, _ = self.dispatch()
        self.subagent(delegation, [_DENIAL_LINE])
        self.finish()
        code, out = self.record()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("error: denied (details: "), out)
        self.assertEqual(self.log_entry()["deny_check"], "DENIED 1")
        attempts = json.loads((self.steps / "s1_c01_attempts.json").read_text(encoding="utf-8"))
        self.assertEqual(attempts[-1]["class"], "denied")

    def test_denial_overrides_a_missing_result_file(self):
        delegation, _ = self.dispatch()
        self.subagent(delegation, [_DENIAL_LINE])
        code, out = self.record(reply=None)
        self.assertEqual(code, 1,
                         "an aborted attempt may be run again; a refused one never")
        self.assertTrue(out.startswith("error: denied"), out)

    def test_clean_transcript_keeps_the_verdict(self):
        delegation, _ = self.dispatch()
        self.subagent(delegation, [{"type": "assistant", "message": {"content": []}}])
        self.finish()
        code, out = self.record()
        self.assertEqual(code, 0, out)
        self.assertNotIn("deny-check", out)
        self.assertEqual(self.log_entry()["deny_check"], "CLEAN")

    def test_unresolved_transcript_keeps_the_status_and_says_so(self):
        self.dispatch()
        self.finish()
        code, out = self.record()
        self.assertEqual(code, 0, out)
        self.assertIn("deny-check: NO-TRANSCRIPT", out)
        self.assertEqual(self.log_entry()["deny_check"], "NO-TRANSCRIPT")

    def test_unresolved_check_leaves_the_verdict_line_intact(self):
        self.dispatch()
        self.finish(
            "DECISION: which join\nfork: two readings of merge\noptions:\n"
            "  1. A -- loses rows\n  2. B -- loses columns\n"
            "recommendation: 1\nwork-state: stopped")
        code, out = self.record()
        self.assertEqual(code, 4, out)
        verdict, note = out.splitlines()
        self.assertNotIn("deny-check", verdict,
                         "the decision verdict ends in the answer path the "
                         "orchestrator copies; nothing may follow it")
        self.assertTrue(note.startswith("deny-check: NO-TRANSCRIPT"), note)

    def test_without_a_session_the_check_is_skipped(self):
        delegation, _ = self.dispatch()
        self.subagent(delegation, [_DENIAL_LINE])
        self.finish()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            code, out = self.record()
        self.assertEqual(code, 0,
                         "outside Claude Code no permission layer can have refused a call")
        self.assertNotIn("deny_check", self.log_entry())


class TestModeHelpers(unittest.TestCase):
    def test_strip_mode_line(self):
        from wfrun.modes import strip_mode_line
        self.assertEqual(strip_mode_line("[Mode: execute]\npath.txt"), "path.txt")
        self.assertEqual(strip_mode_line("no mode line"), "no mode line")
        self.assertEqual(strip_mode_line("body\n[Mode: x]\n"), "body\n[Mode: x]\n")

    def test_blocked_line(self):
        from wfrun.modes import blocked_line
        self.assertEqual(blocked_line("[BLOCKED: mode-rule x] reason\nrest"),
                         "[BLOCKED: mode-rule x] reason")
        self.assertIsNotNone(blocked_line("[Mode: survey]\n[BLOCKED: rules r1] no"))
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


class TestReplanBuilderPrompt(unittest.TestCase):
    def prompt(self, retry_attr):
        wf = parser.parse_string(
            '<workflow name="t" version="2" max="10">'
            f'<replan id="r1" max-steps="5"{retry_attr}><task>plan it</task></replan>'
            '</workflow>')
        return stepio.build_replan_prompt(next(wf.iter_steps()), {}, {})

    def test_builder_prompt_at_retry_0_promises_no_fix(self):
        for retry_attr in ("", ' retry="0"'):
            with self.subTest(retry_attr=retry_attr):
                text = self.prompt(retry_attr)
                self.assertNotIn(
                    "you would then be asked to fix them", text,
                    "retry=0 never re-asks the builder, so the prompt must not say it will")
                self.assertIn("you would not be asked to fix them", text)

    def test_builder_prompt_states_the_retry_bound(self):
        text = self.prompt(' retry="2"')
        self.assertIn("you would then be asked to fix them, at most 2 times in all", text)
        self.assertNotIn("you would not be asked to fix them", text)
        self.assertIn("at most once in all", self.prompt(' retry="1"'))

    def test_builder_prompt_requires_a_task_child(self):
        text = self.prompt("")
        self.assertIn("non-empty `<task>` child", text,
                      "a step without a `<task>` child fails validation")


if __name__ == "__main__":
    unittest.main()
