"""Tests for wfrun ask's Pi backend (pi_cli.py; mode-orchestrator-runs/
phase5-item1-cc-inventory-design.md).

subprocess.run and shutil.which are monkeypatched; no real pi CLI is invoked
-- same pattern as test_claude_cli.py."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import pi_cli  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
