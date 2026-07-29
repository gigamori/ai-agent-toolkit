"""Tests for wfrun ask's Pi backend (pi_cli.py; mode-orchestrator-runs/
phase5-item1-cc-inventory-design.md).

subprocess.run and shutil.which are monkeypatched; no real pi CLI is invoked
-- same pattern as test_claude_cli.py."""
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


class AskLlmPiTests(unittest.TestCase):
    def setUp(self):
        pi_cli._resolution_cache.clear()
        self.which_patch = mock.patch.object(
            pi_cli.shutil, "which", return_value="/usr/local/bin/pi")
        self.which_patch.start()

    def tearDown(self):
        self.which_patch.stop()
        pi_cli._resolution_cache.clear()

    def test_not_on_path_fails_without_subprocess_call(self):
        with mock.patch.object(pi_cli.shutil, "which", return_value=None):
            with mock.patch.object(pi_cli.subprocess, "run") as run:
                answer, reason, cost = pi_cli.ask_llm_pi("q?")
        run.assert_not_called()
        self.assertIsNone(answer)
        self.assertEqual(cost, 0.0)
        self.assertIn("not found", reason)

    def test_success_path_pure_json(self):
        stdout = '{"answer": true, "reason": "because"}'
        with mock.patch.object(pi_cli.subprocess, "run",
                               return_value=_fake_completed(0, stdout)):
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertTrue(answer)
        self.assertEqual(reason, "because")
        self.assertEqual(cost, 0.0)

    def test_success_path_json_wrapped_in_code_fence(self):
        stdout = 'Sure, here you go:\n```json\n{"answer": false, "reason": "nope"}\n```\n'
        with mock.patch.object(pi_cli.subprocess, "run",
                               return_value=_fake_completed(0, stdout)):
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertFalse(answer)
        self.assertEqual(reason, "nope")

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

    def test_prompt_file_is_removed_after_call(self):
        written_paths = []
        real_run = pi_cli.subprocess.run

        def _capture_and_run(cmd, **kwargs):
            # cmd[0] is the pi binary, cmd[1] is "@<tmpfile>"
            arg = cmd[1]
            self.assertTrue(arg.startswith("@"))
            path = arg[1:]
            written_paths.append(path)
            self.assertTrue(Path(path).is_file())
            return _fake_completed(0, '{"answer": true, "reason": "x"}')

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=_capture_and_run):
            pi_cli.ask_llm_pi("q?")
        self.assertEqual(len(written_paths), 1)
        self.assertFalse(Path(written_paths[0]).exists())


if __name__ == "__main__":
    unittest.main()
