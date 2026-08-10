"""Tests for hooks/mode_inject.py slug detection boundaries.

Covers the Phase 4 defect-2 hardening (mode-orchestrator-runs/
phase4-defects-2-4-design.md): backtick-span masking and the
system-notification gate, plus regressions for normal invocation.

Also covers the 2026-07-30 role-less `_meta.md` split (_projects/harness-modes/
project-notes/specs/meta-role-less-variant-plan.md): which of the two
framework-header variants (`_meta.md` role-less vs `_meta_role.md`) is
selected, keyed off whether a role ends up present after masking/escaping.

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


class MetaVariantTests(unittest.TestCase):
    """Which of _meta.md (role-less) / _meta_role.md gets injected."""

    def test_mode_only_gets_role_less_meta(self):
        code, out = run_hook("mode:ask what time is it")
        ctx = context_of(out)
        self.assertIn("Mode = HOW you process", ctx)
        self.assertNotIn("Two response axes", ctx)
        self.assertNotIn("Role:", ctx)

    def test_role_only_gets_role_meta(self):
        code, out = run_hook("role:senior engineer hello")
        ctx = context_of(out)
        self.assertIn("Two response axes", ctx)
        self.assertIn("- Role:", ctx)
        self.assertIn("role: senior engineer", ctx)
        # role-only carries no mode: no mode line, no _common.md rules.
        self.assertNotIn("mode:", ctx)

    def test_both_slugs_get_role_meta(self):
        code, out = run_hook("mode:ask role:senior engineer hello")
        ctx = context_of(out)
        self.assertIn("Two response axes", ctx)
        self.assertIn("role: senior engineer", ctx)
        self.assertIn("mode: ask", ctx)

    def test_alias_mode_gets_role_less_meta(self):
        code, out = run_hook("mode:verify check this")
        ctx = context_of(out)
        self.assertIn("Mode = HOW you process", ctx)
        self.assertIn("mode: verify", ctx)  # chosen alias preserved

    def test_empty_quoted_role_gets_role_less_meta(self):
        # role:"" is treated as no role -- meta selection must follow that,
        # not the raw presence of a `role:` token.
        code, out = run_hook('role:"" mode:ask hi')
        ctx = context_of(out)
        self.assertIn("Mode = HOW you process", ctx)
        self.assertNotIn("Two response axes", ctx)
        self.assertNotIn("role:", ctx)

    def test_backticked_role_gets_role_less_meta(self):
        # The role is masked out entirely, so only mode: fires -- the meta
        # selection must follow the post-mask role state, not raw text
        # containing "role:".
        code, out = run_hook("`role:x` mode:ask hi")
        ctx = context_of(out)
        self.assertIn("Mode = HOW you process", ctx)
        self.assertNotIn("Two response axes", ctx)


class SuffixTests(unittest.TestCase):
    """`mode:<name>/<seg>...` subagent-delegation suffix (2026-08-09)."""

    def test_suffix_echoes_and_injects_subagent_file(self):
        code, out = run_hook("mode:survey/subagent look into the failure")
        self.assertIsNotNone(out)
        ctx = context_of(out)
        self.assertIn("mode: survey/subagent", ctx)
        self.assertIn("SUBAGENT DELEGATION", ctx)

    def test_no_suffix_does_not_inject_subagent_file(self):
        # Non-regression: a bare mode slug must not pull in _subagent.md.
        code, out = run_hook("mode:survey investigate the build failure")
        ctx = context_of(out)
        self.assertIn("mode: survey", ctx)
        self.assertNotIn("SUBAGENT DELEGATION", ctx)

    def test_multi_segment_suffix_echoed_verbatim(self):
        code, out = run_hook("mode:execute/subagent/opus apply the fix")
        ctx = context_of(out)
        self.assertIn("mode: execute/subagent/opus", ctx)
        self.assertIn("SUBAGENT DELEGATION", ctx)

    def test_model_only_suffix_still_delegates(self):
        # A model name with no literal "subagent" segment still implies
        # delegation.
        code, out = run_hook("mode:execute/opus apply the fix")
        ctx = context_of(out)
        self.assertIn("mode: execute/opus", ctx)
        self.assertIn("SUBAGENT DELEGATION", ctx)

    def test_alias_mode_with_suffix_resolves_and_echoes_alias(self):
        code, out = run_hook("mode:verify/subagent check this")
        ctx = context_of(out)
        # File lookup resolves the alias (debug.md); the displayed line
        # keeps the user's chosen alias plus the suffix.
        self.assertIn("mode: verify/subagent", ctx)
        self.assertIn("SUBAGENT DELEGATION", ctx)

    def test_unresolvable_mode_with_suffix_drops_both(self):
        code, out = run_hook("mode:nonexistent/subagent do something")
        self.assertIsNone(out)

    def test_backticked_suffixed_slug_never_invokes(self):
        code, out = run_hook("it stopped because of `mode:survey/subagent` rules")
        self.assertIsNone(out)

    def test_role_and_suffixed_mode_together(self):
        code, out = run_hook('role:"senior engineer" mode:survey/subagent hello')
        ctx = context_of(out)
        self.assertIn("Two response axes", ctx)
        self.assertIn("role: senior engineer", ctx)
        self.assertIn("mode: survey/subagent", ctx)
        self.assertIn("SUBAGENT DELEGATION", ctx)

    def test_nomode_suppresses_suffixed_slug(self):
        code, out = run_hook("nomode mode:survey/subagent do the thing")
        self.assertIsNone(out)

    def test_suffixed_slug_at_end_of_prompt_injects(self):
        code, out = run_hook("investigate the build failure mode:survey/subagent")
        ctx = context_of(out)
        self.assertIn("mode: survey/subagent", ctx)
        self.assertIn("SUBAGENT DELEGATION", ctx)


if __name__ == "__main__":
    unittest.main()
