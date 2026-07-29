"""Tests for hooks/mode_inject.py slug detection boundaries.

Covers the Phase 4 defect-2 hardening (mode-orchestrator-runs/
phase4-defects-2-4-design.md): backtick-span masking and the
system-notification gate, plus regressions for normal invocation.

The hook is exercised as a black box: stdin JSON in, stdout JSON out --
the same way Claude Code runs it.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "mode_inject.py"


def run_hook(prompt):
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": prompt}).encode("utf-8"),
        capture_output=True, timeout=30)
    if not proc.stdout.strip():
        return proc.returncode, None
    return proc.returncode, json.loads(proc.stdout.decode("utf-8"))


def run_hook_stderr(prompt):
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": prompt}).encode("utf-8"),
        capture_output=True, timeout=30)
    return proc.stderr.decode("utf-8")


def context_of(out):
    return out["hookSpecificOutput"]["additionalContext"]


class NormalInvocationTests(unittest.TestCase):
    def test_bare_mode_slug_injects(self):
        code, out = run_hook("mode:survey investigate the build failure")
        self.assertEqual(code, 0)
        self.assertIsNotNone(out)
        self.assertIn("mode: survey", context_of(out))

    def test_mode_slug_at_end_of_prompt_injects(self):
        # Real usage writes the slug anywhere, including trailing position.
        code, out = run_hook("compatibilityを検証して mode:survey")
        self.assertIsNotNone(out)
        self.assertIn("mode: survey", context_of(out))

    def test_no_slug_no_output(self):
        code, out = run_hook("just a normal prompt")
        self.assertEqual(code, 0)
        self.assertIsNone(out)


class BacktickMaskTests(unittest.TestCase):
    def test_backticked_mode_is_a_mention_not_an_invocation(self):
        code, out = run_hook("the turn stopped because of `mode:execute` rules")
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_bare_slug_outside_span_still_invokes(self):
        # Masking must not eat legitimate slugs elsewhere in the prompt.
        code, out = run_hook("see `mode:execute` for contrast; mode:survey now")
        self.assertIsNotNone(out)
        self.assertIn("mode: survey", context_of(out))

    def test_backticked_role_is_masked_too(self):
        code, out = run_hook("it printed `role:senior engineer` verbatim")
        self.assertIsNone(out)

    def test_mask_is_single_line(self):
        # An unpaired backtick on one line must not swallow the next line.
        code, out = run_hook("odd ` tick here\nmode:survey do the thing")
        self.assertIsNotNone(out)
        self.assertIn("mode: survey", context_of(out))


class NotificationGateTests(unittest.TestCase):
    # The measured injection vector (Phase 4 run ...13, turn 05): a subagent
    # completion notice, relayed as a user turn, quoting mode:execute in its
    # result text.
    def test_task_notification_never_injects(self):
        prompt = ("<task-notification>\n<task-id>abc</task-id>\n<result>"
                  "stopped per mode:execute's ban on ambiguity</result>\n"
                  "</task-notification>")
        code, out = run_hook(prompt)
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_system_notification_header_never_injects(self):
        prompt = ("[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
                  "Agent finished. It said: use mode:execute next time.")
        code, out = run_hook(prompt)
        self.assertIsNone(out)

    def test_notification_skip_is_visible_on_stderr(self):
        # A silent skip would leave "why didn't my mode fire" undiagnosable
        # for a hand-typed prompt that mentions a marker string.
        stderr = run_hook_stderr("<task-notification> mode:survey")
        self.assertIn("role-mode: skipped", stderr)
        self.assertIn("task-notification", stderr)


if __name__ == "__main__":
    unittest.main()
