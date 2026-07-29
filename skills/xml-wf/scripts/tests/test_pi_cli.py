"""Tests for wfrun ask's Pi backend (pi_cli.py; mode-orchestrator-runs/
phase5-item1-cc-inventory-design.md) and for run-pi (pi_cli.py;
mode-orchestrator-runs/phase6-run-pi-design.md).

subprocess.run and shutil.which are monkeypatched; no real pi CLI is invoked
-- same pattern as test_claude_cli.py."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import parser, pi_cli  # noqa: E402
from wfrun.executor import WorkflowFailure  # noqa: E402


def _fake_completed(returncode=0, stdout="", stderr=""):
    proc = mock.Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class ResolvePiLauncherTests(unittest.TestCase):
    def setUp(self):
        pi_cli._resolution_cache.clear()

    def tearDown(self):
        pi_cli._resolution_cache.clear()

    def test_not_on_path_is_none(self):
        with mock.patch.object(pi_cli.shutil, "which", return_value=None):
            self.assertIsNone(pi_cli.resolve_pi_launcher())

    def test_real_executable_launches_directly(self):
        with mock.patch.object(pi_cli.shutil, "which",
                               return_value="/usr/local/bin/pi"):
            self.assertEqual(pi_cli.resolve_pi_launcher(), ["/usr/local/bin/pi"])

    def test_windows_shim_is_bypassed_via_node(self):
        shim = r"C:\npm\pi.CMD"
        entry = os.path.join(r"C:\npm", *pi_cli._NPM_ENTRY)

        def fake_which(name):
            return {"pi": shim, "node": r"C:\nodejs\node.exe"}.get(name)

        with mock.patch.object(pi_cli.shutil, "which", side_effect=fake_which), \
             mock.patch.object(pi_cli.os.path, "isfile", return_value=True):
            self.assertEqual(pi_cli.resolve_pi_launcher(),
                             [r"C:\nodejs\node.exe", entry])

    def test_shim_without_resolvable_entry_fails_closed(self):
        # Refusing beats launching through the shim: it silently truncates a
        # multi-line prompt at the first newline (measured).
        def fake_which(name):
            return {"pi": r"C:\npm\pi.CMD", "node": r"C:\nodejs\node.exe"}.get(name)

        with mock.patch.object(pi_cli.shutil, "which", side_effect=fake_which), \
             mock.patch.object(pi_cli.os.path, "isfile", return_value=False):
            self.assertIsNone(pi_cli.resolve_pi_launcher())

    def test_shim_without_node_fails_closed(self):
        def fake_which(name):
            return {"pi": r"C:\npm\pi.CMD"}.get(name)

        with mock.patch.object(pi_cli.shutil, "which", side_effect=fake_which), \
             mock.patch.object(pi_cli.os.path, "isfile", return_value=True):
            self.assertIsNone(pi_cli.resolve_pi_launcher())


class AskLlmPiTests(unittest.TestCase):
    def setUp(self):
        pi_cli._resolution_cache.clear()
        self.which_patch = mock.patch.object(
            pi_cli.shutil, "which", return_value="/usr/local/bin/pi")
        self.which_patch.start()

    def tearDown(self):
        self.which_patch.stop()
        pi_cli._resolution_cache.clear()

    def test_unlaunchable_fails_without_subprocess_call(self):
        pi_cli._resolution_cache.clear()
        with mock.patch.object(pi_cli.shutil, "which", return_value=None):
            with mock.patch.object(pi_cli.subprocess, "run") as run:
                answer, reason, cost = pi_cli.ask_llm_pi("q?")
        run.assert_not_called()
        self.assertIsNone(answer)
        self.assertEqual(cost, 0.0)
        self.assertIn("not launchable", reason)

    def test_success_path_pure_json(self):
        stdout = '{"answer": true, "reason": "because"}'
        with mock.patch.object(pi_cli.subprocess, "run",
                               return_value=_fake_completed(0, stdout)):
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertTrue(answer)
        self.assertEqual(reason, "because")
        self.assertEqual(cost, 0.0)

    def test_success_path_json_wrapped_in_code_fence(self):
        # This is the shape the model actually returns (measured), not a
        # hypothetical -- the brace-extraction pass is load bearing.
        stdout = '```json\n{"answer": false, "reason": "nope"}\n```\n'
        with mock.patch.object(pi_cli.subprocess, "run",
                               return_value=_fake_completed(0, stdout)):
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertFalse(answer)
        self.assertEqual(reason, "nope")

    def test_prompt_is_a_positional_argument_not_an_at_file(self):
        # `@file` attaches the file as content to reason about, not as the
        # turn's instruction -- a probe using it got a prompt-injection
        # refusal. The prompt must ride on argv.
        captured = {}

        def capture(cmd, **kwargs):
            captured["cmd"] = cmd
            return _fake_completed(0, '{"answer": true, "reason": "x"}')

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            pi_cli.ask_llm_pi("is the sky blue?")
        cmd = captured["cmd"]
        self.assertFalse(any(str(a).startswith("@") for a in cmd),
                         f"no argv entry may use @file include syntax: {cmd}")
        self.assertIn("is the sky blue?", cmd[-1])
        self.assertEqual(cmd[-2], "haiku")

    def test_first_attempt_malformed_second_succeeds(self):
        bad = _fake_completed(0, "not json at all")
        good = _fake_completed(0, '{"answer": true, "reason": "ok"}')
        with mock.patch.object(pi_cli.subprocess, "run",
                               side_effect=[bad, good]) as run:
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertEqual(run.call_count, 2)
        self.assertTrue(answer)
        self.assertEqual(reason, "ok")

    def test_always_malformed_gives_up_after_two_attempts(self):
        bad = _fake_completed(0, "still not json")
        with mock.patch.object(pi_cli.subprocess, "run",
                               side_effect=[bad, bad]) as run:
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(answer)

    def test_timeout_reports_failure(self):
        with mock.patch.object(
                pi_cli.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="pi", timeout=300)):
            answer, reason, cost = pi_cli.ask_llm_pi("q?", timeout=300)
        self.assertIsNone(answer)
        self.assertIn("timeout", reason)

    def test_nonzero_exit_is_treated_as_failure_and_retried(self):
        fail = _fake_completed(1, "", "some pi error")
        with mock.patch.object(pi_cli.subprocess, "run",
                               side_effect=[fail, fail]) as run:
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(answer)

    def test_stdin_is_closed(self):
        # An open stdin pipe makes `pi -p` block forever before dispatch.
        captured = {}

        def capture(cmd, **kwargs):
            captured.update(kwargs)
            return _fake_completed(0, '{"answer": true, "reason": "x"}')

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            pi_cli.ask_llm_pi("q?")
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)


def _turn_end(**message) -> str:
    """One JSONL line: a turn_end event wrapping the given message fields."""
    return json.dumps({"type": "turn_end", "message": message})


def _jsonl(*lines: str) -> str:
    return "\n".join(lines) + "\n"


class ConvertToolsTests(unittest.TestCase):
    """_convert_tools(): TOOL_NAME_MAP (design phase6-run-pi-design.md §4.2)."""

    def test_glob_converts_to_find(self):
        converted, error = pi_cli._convert_tools("Glob")
        self.assertIsNone(error)
        self.assertEqual(converted, "find")

    def test_multiple_names_all_converted(self):
        converted, error = pi_cli._convert_tools("Read,Write,Glob")
        self.assertIsNone(error)
        self.assertEqual(converted, "read,write,find")

    def test_unconvertible_name_is_rejected(self):
        converted, error = pi_cli._convert_tools("Read,MultiEdit")
        self.assertIsNone(converted)
        self.assertIn("MultiEdit", error)

    def test_unknown_name_is_rejected(self):
        converted, error = pi_cli._convert_tools("Frobnicate")
        self.assertIsNone(converted)
        self.assertIn("Frobnicate", error)

    def test_empty_after_conversion_is_rejected(self):
        converted, error = pi_cli._convert_tools(" , ,")
        self.assertIsNone(converted)
        self.assertIn("empty", error)


class ClassifyResultPiTests(unittest.TestCase):
    """classify_result_pi(): the full classification table (design §4.1)."""

    def test_missing_turn_end_is_behavioral(self):
        stdout = _jsonl(json.dumps({"type": "session"}),
                        json.dumps({"type": "agent_start"}))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")
        self.assertEqual(res.raw["last_event_type"], "agent_start")

    def test_no_events_at_all_is_behavioral(self):
        res = pi_cli.classify_result_pi(0, "", "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")
        self.assertIsNone(res.raw["last_event_type"])

    def test_stop_reason_error_is_behavioral(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": ""}],
            stopReason="error", errorMessage="invalid api key"))
        res = pi_cli.classify_result_pi(1, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")
        self.assertIn("invalid api key", res.error)

    def test_stop_reason_aborted_is_behavioral(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": "partial"}], stopReason="aborted"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")

    def test_error_prefix_is_guardrail(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": "ERROR: boom"}], stopReason="stop"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "guardrail")
        self.assertEqual(res.error, "ERROR: boom")

    def test_blocked_is_refusal(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text",
                     "text": "[BLOCKED: mode-rule x] reason"}],
            stopReason="stop"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "refusal")

    def test_empty_body_is_behavioral(self):
        stdout = _jsonl(_turn_end(content=[{"type": "text", "text": ""}],
                                  stopReason="stop"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")
        self.assertEqual(res.error, "empty result")

    def test_success_path_text_and_cost(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": "4"}], stopReason="stop",
            usage={"input": 10, "output": 2, "cacheRead": 0, "cacheWrite": 0,
                  "totalTokens": 12, "cost": {"total": 0.00123875}},
            model="pi-claude-agent-sdk/haiku"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "4")
        self.assertEqual(res.cost_usd, 0.00123875)

    def test_multiple_text_blocks_are_concatenated(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"}],
            stopReason="stop"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "hello world")

    def test_raw_reduction_excludes_thinking_blocks_and_full_jsonl(self):
        stdout = _jsonl(
            json.dumps({"type": "message_start"}),
            _turn_end(
                content=[{"type": "thinking", "thinking": "pondering..."},
                        {"type": "text", "text": "answer"}],
                stopReason="stop", errorMessage=None,
                usage={"cost": {"total": 0.0}},
                model="pi-claude-agent-sdk/sonnet", provider="anthropic"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertTrue(res.ok)
        self.assertEqual(
            res.raw,
            {"content": [{"type": "text", "text": "answer"}],
             "stopReason": "stop", "errorMessage": None,
             "usage": {"cost": {"total": 0.0}},
             "model": "pi-claude-agent-sdk/sonnet", "provider": "anthropic"})
        # the full JSONL (message_start line, thinking block) is not in raw
        self.assertNotIn("message_start", json.dumps(res.raw))
        self.assertNotIn("pondering", json.dumps(res.raw))

    def test_stray_non_json_line_is_tolerated(self):
        stdout = "not json at all\n" + _turn_end(
            content=[{"type": "text", "text": "ok"}], stopReason="stop")
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "ok")


class RunPiTests(unittest.TestCase):
    """run_pi(): design phase6-run-pi-design.md §4."""

    def setUp(self):
        pi_cli._resolution_cache.clear()
        self.which_patch = mock.patch.object(
            pi_cli.shutil, "which", return_value="/usr/local/bin/pi")
        self.which_patch.start()

    def tearDown(self):
        self.which_patch.stop()
        pi_cli._resolution_cache.clear()

    def _ok_completed(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": "done"}], stopReason="stop"))
        return _fake_completed(0, stdout)

    def test_schema_is_rejected_without_launching(self):
        with mock.patch.object(pi_cli.subprocess, "run") as run:
            res = pi_cli.run_pi("hi", schema='{"type": "object"}')
        run.assert_not_called()
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")
        self.assertIn("schema=", res.error)

    def test_unlaunchable_fails_without_subprocess_call(self):
        pi_cli._resolution_cache.clear()
        with mock.patch.object(pi_cli.shutil, "which", return_value=None):
            with mock.patch.object(pi_cli.subprocess, "run") as run:
                res = pi_cli.run_pi("hi")
        run.assert_not_called()
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")
        self.assertIn("not launchable", res.error)

    def test_no_session_and_no_skills_always_in_argv(self):
        captured = {}

        def capture(cmd, **kwargs):
            captured["cmd"] = cmd
            return self._ok_completed()

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            pi_cli.run_pi("do the thing")
        self.assertIn("--no-session", captured["cmd"])
        self.assertIn("--no-skills", captured["cmd"])
        self.assertNotIn("--no-extensions", captured["cmd"])

    def test_effort_passed_through_unconverted(self):
        captured = {}

        def capture(cmd, **kwargs):
            captured["cmd"] = cmd
            return self._ok_completed()

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            pi_cli.run_pi("task", effort="xhigh")
        cmd = captured["cmd"]
        idx = cmd.index("--thinking")
        self.assertEqual(cmd[idx + 1], "xhigh")  # forwarded verbatim, no mapping

    def test_tools_converted_to_pi_names_in_argv(self):
        captured = {}

        def capture(cmd, **kwargs):
            captured["cmd"] = cmd
            return self._ok_completed()

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            pi_cli.run_pi("task", tools="Read,Glob")
        cmd = captured["cmd"]
        idx = cmd.index("--tools")
        self.assertEqual(cmd[idx + 1], "read,find")

    def test_unconvertible_tools_rejected_without_launching(self):
        with mock.patch.object(pi_cli.subprocess, "run") as run:
            res = pi_cli.run_pi("task", tools="Read,NotebookEdit")
        run.assert_not_called()
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")
        self.assertIn("NotebookEdit", res.error)

    def test_no_tools_means_no_tools_flag(self):
        captured = {}

        def capture(cmd, **kwargs):
            captured["cmd"] = cmd
            return self._ok_completed()

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            pi_cli.run_pi("task")  # tools=None: unrestricted, same as run_claude
        self.assertNotIn("--tools", captured["cmd"])

    def test_prompt_is_positional_not_at_file(self):
        captured = {}

        def capture(cmd, **kwargs):
            captured["cmd"] = cmd
            return self._ok_completed()

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            pi_cli.run_pi("do the thing please")
        cmd = captured["cmd"]
        self.assertFalse(any(str(a).startswith("@") for a in cmd))
        self.assertIn("do the thing please", cmd)

    def test_permission_mode_is_silently_ignored(self):
        captured = {}

        def capture(cmd, **kwargs):
            captured["cmd"] = cmd
            return self._ok_completed()

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            res = pi_cli.run_pi("task", permission_mode="acceptEdits")
        self.assertTrue(res.ok)
        self.assertFalse(any("permission" in str(a).lower()
                            for a in captured["cmd"]))

    def test_system_prompt_written_to_temp_file_and_cleaned_up(self):
        written = {}

        def capture(cmd, **kwargs):
            idx = cmd.index("--append-system-prompt")
            path = cmd[idx + 1]
            written["text"] = Path(path).read_text(encoding="utf-8")
            written["existed_during_call"] = os.path.isfile(path)
            written["path"] = path
            return self._ok_completed()

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            pi_cli.run_pi("task", system_prompt="<role>x</role>")
        self.assertEqual(written["text"], "<role>x</role>")
        self.assertTrue(written["existed_during_call"])
        self.assertFalse(os.path.isfile(written["path"]))  # cleaned up after

    def test_stdin_is_closed(self):
        captured = {}

        def capture(cmd, **kwargs):
            captured.update(kwargs)
            return self._ok_completed()

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            pi_cli.run_pi("task")
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)

    def test_timeout_is_classified(self):
        with mock.patch.object(
                pi_cli.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="pi", timeout=5)):
            res = pi_cli.run_pi("task", timeout=5)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "timeout")

    def test_kill_tree_uses_tree_kill_helper_with_empty_stdin_prompt(self):
        captured = {}

        def fake_tree_kill(cmd, prompt, timeout, cwd):
            captured["cmd"] = cmd
            captured["prompt"] = prompt
            return self._ok_completed()

        with mock.patch.object(pi_cli.claude_cli, "_run_with_tree_kill",
                               side_effect=fake_tree_kill), \
             mock.patch.object(pi_cli.subprocess, "run") as plain_run:
            res = pi_cli.run_pi("task", kill_tree=True)
        plain_run.assert_not_called()
        self.assertTrue(res.ok)
        # the real prompt already rides on argv; nothing is fed over stdin
        self.assertEqual(captured["prompt"], "")
        self.assertIn("task", captured["cmd"])

    def test_success_end_to_end_through_classify_result_pi(self):
        with mock.patch.object(pi_cli.subprocess, "run",
                               return_value=self._ok_completed()):
            res = pi_cli.run_pi("task")
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "done")


class DiagnoseStubPiTests(unittest.TestCase):
    """diagnose_stub_pi(): the second line of defense behind the startup
    fail-fast (design §1, §2.3) -- verifies it fails loudly if ever reached."""

    def test_raises_workflow_failure(self):
        with self.assertRaises(WorkflowFailure):
            pi_cli.diagnose_stub_pi(step=None, prompt="p", failure=None, cwd=None)

    def test_raises_even_without_cwd_kwarg(self):
        with self.assertRaises(WorkflowFailure):
            pi_cli.diagnose_stub_pi(None, "p", None)


class PiCompatErrorsTests(unittest.TestCase):
    """pi_compat_errors(): startup fail-fast (design §2.2, §2.3)."""

    def _wf(self, body: str):
        xml = f'<workflow name="t" version="2" max="10">{body}</workflow>'
        return parser.parse_string(xml)

    def test_compatible_workflow_has_no_errors(self):
        wf = self._wf('<step id="s1"><role>W</role><task>x</task></step>')
        self.assertEqual(pi_cli.pi_compat_errors(wf), [])

    def test_schema_step_rejected_with_build_guidance(self):
        wf = self._wf(
            '<step id="s1" output="n" output-type="value" '
            'schema=\'{"type":"object","properties":{"n":{"type":"integer"}},'
            '"required":["n"]}\'>'
            '<role>W</role><task>count</task></step>')
        errors = pi_cli.pi_compat_errors(wf)
        self.assertEqual(len(errors), 1)
        expected = (
            "error: step 's1' declares schema=, which the pi backend cannot enforce\n"
            "       (no forced-structured-output equivalent exists).\n"
            "       Rebuild this workflow as pi-compatible: run the skill in build mode\n"
            "       on this XML and ask for a pi-compatible version. The conversion\n"
            "       rules are in references/run-pi.md, \"Replacing schema=\".")
        self.assertEqual(errors[0], expected)

    def test_on_error_debug_step_rejected_with_three_hints(self):
        wf = self._wf(
            '<step id="s1" on-error="debug"><role>W</role><task>x</task></step>')
        errors = pi_cli.pi_compat_errors(wf)
        self.assertEqual(len(errors), 1)
        expected = (
            "error: step 's1' uses on-error=\"debug\", which the pi backend does not\n"
            "       support (debug diagnosis has no pi implementation).\n"
            "       Rebuild this workflow as pi-compatible. Replacement hints:\n"
            "       - on-error=\"fail\" (default) — stop the run and let resume handle it\n"
            "       - retry=N — for steps that fail transiently, a plain retry often\n"
            "         covers what a debug-retry cycle did\n"
            "       - on-error=\"ignore\" + a follow-up verification step — when the run\n"
            "         should continue and the failure needs recording instead of fixing\n"
            "       See references/run-pi.md, \"Replacing on-error=debug\".")
        self.assertEqual(errors[0], expected)
        # the three replacement hints, explicitly
        self.assertIn('on-error="fail"', errors[0])
        self.assertIn("retry=N", errors[0])
        self.assertIn('on-error="ignore"', errors[0])

    def test_replan_on_error_debug_is_not_flagged(self):
        # <replan> cannot carry schema= at all (parser.py's _REPLAN_ATTRS
        # excludes it), and its on-error="debug" is already inert under
        # every backend (_exec_replan never calls self._diagnose) -- not a
        # pi-specific gap this check needs to close.
        wf = self._wf(
            '<replan id="r1" on-error="debug"><role>W</role><task>x</task></replan>')
        self.assertEqual(pi_cli.pi_compat_errors(wf), [])

    def test_multiple_violations_combine_in_step_order(self):
        wf = self._wf(
            '<step id="s1" schema=\'{"type":"object"}\'>'
            '<role>W</role><task>a</task></step>'
            '<step id="s2" on-error="debug"><role>W</role><task>b</task></step>')
        errors = pi_cli.pi_compat_errors(wf)
        self.assertEqual(len(errors), 2)
        self.assertIn("step 's1'", errors[0])
        self.assertIn("step 's2'", errors[1])


if __name__ == "__main__":
    unittest.main()
