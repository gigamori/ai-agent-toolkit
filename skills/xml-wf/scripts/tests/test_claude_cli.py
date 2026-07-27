"""Tests for the launch-path fix (reliability-spec.md §13): executable
resolution, shim loud-failure, and the `transient` error_class.

subprocess.run and shutil.which are monkeypatched; no real claude CLI is
invoked (that is covered separately by a real-CLI E2E, run manually in an
isolated worktree per reliability-spec.md §13.6)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import claude_cli  # noqa: E402


def _fake_completed(returncode=0, stdout="", stderr=""):
    proc = mock.Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class ResolutionChainTests(unittest.TestCase):
    def setUp(self):
        claude_cli._resolution_cache.clear()

    def tearDown(self):
        claude_cli._resolution_cache.clear()

    def test_not_on_path_is_env(self):
        with mock.patch.object(claude_cli.shutil, "which", return_value=None):
            path, via_shim = claude_cli._resolve_claude_bin()
        self.assertIsNone(path)
        self.assertFalse(via_shim)

    def test_posix_script_used_directly(self):
        with mock.patch.object(claude_cli.shutil, "which",
                               return_value="/usr/local/bin/claude"):
            path, via_shim = claude_cli._resolve_claude_bin()
        self.assertEqual(path, "/usr/local/bin/claude")
        self.assertFalse(via_shim)

    def test_native_exe_used_directly(self):
        with mock.patch.object(claude_cli.shutil, "which",
                               return_value=r"C:\tools\claude.exe"):
            path, via_shim = claude_cli._resolve_claude_bin()
        self.assertEqual(path, r"C:\tools\claude.exe")
        self.assertFalse(via_shim)

    def test_cmd_shim_with_real_sibling_exe_resolves_to_exe(self):
        with tempfile.TemporaryDirectory() as d:
            npm_dir = Path(d)
            shim = npm_dir / "claude.cmd"
            shim.write_text("@echo off")
            sibling_dir = (npm_dir / "node_modules" / "@anthropic-ai"
                           / "claude-code" / "bin")
            sibling_dir.mkdir(parents=True)
            sibling = sibling_dir / "claude.exe"
            sibling.write_bytes(b"x" * 5000)  # above the 4096-byte stub threshold

            with mock.patch.object(claude_cli.shutil, "which", return_value=str(shim)):
                path, via_shim = claude_cli._resolve_claude_bin()
            self.assertEqual(path, str(sibling))
            self.assertFalse(via_shim)

    def test_cmd_shim_with_stub_sibling_falls_back_to_shim(self):
        with tempfile.TemporaryDirectory() as d:
            npm_dir = Path(d)
            shim = npm_dir / "claude.cmd"
            shim.write_text("@echo off")
            sibling_dir = (npm_dir / "node_modules" / "@anthropic-ai"
                           / "claude-code" / "bin")
            sibling_dir.mkdir(parents=True)
            sibling = sibling_dir / "claude.exe"
            sibling.write_bytes(b"x" * 500)  # below the 4096-byte stub threshold

            with mock.patch.object(claude_cli.shutil, "which", return_value=str(shim)):
                path, via_shim = claude_cli._resolve_claude_bin()
            self.assertEqual(path, str(shim))
            self.assertTrue(via_shim)

    def test_cmd_shim_with_no_sibling_falls_back_to_shim(self):
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "claude.cmd"
            shim.write_text("@echo off")
            with mock.patch.object(claude_cli.shutil, "which", return_value=str(shim)):
                path, via_shim = claude_cli._resolve_claude_bin()
            self.assertEqual(path, str(shim))
            self.assertTrue(via_shim)

    def test_resolution_is_cached(self):
        with mock.patch.object(claude_cli.shutil, "which",
                               return_value="/usr/local/bin/claude") as which:
            claude_cli._resolve_claude_bin()
            claude_cli._resolve_claude_bin()
        which.assert_called_once()


class RunClaudeLaunchTests(unittest.TestCase):
    """Exercises run_claude() with subprocess.run mocked out."""

    def setUp(self):
        claude_cli._resolution_cache.clear()
        self.which_patch = mock.patch.object(
            claude_cli.shutil, "which", return_value="/usr/local/bin/claude")
        self.which_patch.start()

    def tearDown(self):
        self.which_patch.stop()
        claude_cli._resolution_cache.clear()

    def test_cli_not_found_is_env(self):
        claude_cli._resolution_cache.clear()
        with mock.patch.object(claude_cli.shutil, "which", return_value=None):
            res = claude_cli.run_claude("hi")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")

    def test_success_path_still_works(self):
        stdout = json.dumps({"result": "4", "is_error": False, "num_turns": 1})
        with mock.patch.object(claude_cli.subprocess, "run",
                               return_value=_fake_completed(0, stdout)):
            res = claude_cli.run_claude("2+2?")
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "4")

    def test_system_prompt_uses_file_flag_and_cleans_up(self):
        stdout = json.dumps({"result": "ok", "is_error": False})
        captured_cmd = {}
        written_content = {}

        def fake_run(cmd, **kwargs):
            captured_cmd["cmd"] = cmd
            idx = cmd.index("--append-system-prompt-file")
            path = cmd[idx + 1]
            written_content["text"] = Path(path).read_text(encoding="utf-8")
            written_content["existed_during_call"] = os.path.isfile(path)
            return _fake_completed(0, stdout)

        with mock.patch.object(claude_cli, "_supports_system_prompt_file",
                               return_value=True), \
             mock.patch.object(claude_cli.subprocess, "run", side_effect=fake_run):
            res = claude_cli.run_claude("task", system_prompt="<role>x</role>")

        self.assertTrue(res.ok)
        self.assertNotIn("--append-system-prompt", captured_cmd["cmd"])
        self.assertIn("--append-system-prompt-file", captured_cmd["cmd"])
        self.assertEqual(written_content["text"], "<role>x</role>")
        self.assertTrue(written_content["existed_during_call"])
        # cleaned up after the call
        idx = captured_cmd["cmd"].index("--append-system-prompt-file")
        self.assertFalse(os.path.isfile(captured_cmd["cmd"][idx + 1]))

    def test_shim_with_hostile_system_prompt_and_no_file_support_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "claude.cmd"
            shim.write_text("@echo off")
            claude_cli._resolution_cache.clear()
            with mock.patch.object(claude_cli.shutil, "which", return_value=str(shim)), \
                 mock.patch.object(claude_cli, "_supports_system_prompt_file",
                                   return_value=False), \
                 mock.patch.object(claude_cli.subprocess, "run") as run:
                res = claude_cli.run_claude(
                    "task", system_prompt="line1 & line2 | line3")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")
        run.assert_not_called()  # loud failure: never launches

    def test_shim_with_benign_system_prompt_and_no_file_support_launches_inline(self):
        stdout = json.dumps({"result": "ok", "is_error": False})
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "claude.cmd"
            shim.write_text("@echo off")
            claude_cli._resolution_cache.clear()
            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                return _fake_completed(0, stdout)

            with mock.patch.object(claude_cli.shutil, "which", return_value=str(shim)), \
                 mock.patch.object(claude_cli, "_supports_system_prompt_file",
                                   return_value=False), \
                 mock.patch.object(claude_cli.subprocess, "run", side_effect=fake_run):
                res = claude_cli.run_claude(
                    "task", system_prompt="plain text, no metachars at all")
        self.assertTrue(res.ok)
        self.assertIn("--append-system-prompt", captured["cmd"])

    def test_shim_with_hostile_schema_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "claude.cmd"
            shim.write_text("@echo off")
            claude_cli._resolution_cache.clear()
            with mock.patch.object(claude_cli.shutil, "which", return_value=str(shim)), \
                 mock.patch.object(claude_cli.subprocess, "run") as run:
                res = claude_cli.run_claude(
                    "task", schema='{"pattern": "a|b"}')
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")
        run.assert_not_called()

    def test_hostile_schema_refusal_leaks_no_temp_file(self):
        # Regression: the schema refusal must not strand a system-prompt
        # temp file. All rejection checks run before the file is created.
        before = set(Path(tempfile.gettempdir()).glob("wfrun-sysprompt-*"))
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "claude.cmd"
            shim.write_text("@echo off")
            claude_cli._resolution_cache.clear()
            with mock.patch.object(claude_cli.shutil, "which", return_value=str(shim)), \
                 mock.patch.object(claude_cli, "_supports_system_prompt_file",
                                   return_value=True), \
                 mock.patch.object(claude_cli.subprocess, "run") as run:
                res = claude_cli.run_claude(
                    "task", system_prompt="<role>x</role>",
                    schema='{"pattern": "a|b"}')
        self.assertFalse(res.ok)
        run.assert_not_called()
        after = set(Path(tempfile.gettempdir()).glob("wfrun-sysprompt-*"))
        self.assertEqual(before, after)

    def test_temp_file_removed_when_subprocess_raises(self):
        before = set(Path(tempfile.gettempdir()).glob("wfrun-sysprompt-*"))
        with mock.patch.object(claude_cli, "_supports_system_prompt_file",
                               return_value=True), \
             mock.patch.object(claude_cli.subprocess, "run",
                               side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                claude_cli.run_claude("task", system_prompt="<role>x</role>")
        after = set(Path(tempfile.gettempdir()).glob("wfrun-sysprompt-*"))
        self.assertEqual(before, after)

    def test_non_ascii_system_prompt_written_as_utf8(self):
        # Role/rules bodies in this repo are routinely Japanese; the platform
        # default encoding (cp932) would corrupt them silently.
        captured = {}

        def fake_run(cmd, **kwargs):
            idx = cmd.index("--append-system-prompt-file")
            captured["bytes"] = Path(cmd[idx + 1]).read_bytes()
            return _fake_completed(0, json.dumps({"result": "ok", "is_error": False}))

        with mock.patch.object(claude_cli, "_supports_system_prompt_file",
                               return_value=True), \
             mock.patch.object(claude_cli.subprocess, "run", side_effect=fake_run):
            res = claude_cli.run_claude("task", system_prompt="日本語ルール")
        self.assertTrue(res.ok)
        self.assertEqual(captured["bytes"].decode("utf-8"), "日本語ルール")

    def test_timeout_is_classified(self):
        with mock.patch.object(
                claude_cli.subprocess, "run",
                side_effect=claude_cli.subprocess.TimeoutExpired(cmd="claude", timeout=5)):
            res = claude_cli.run_claude("hi", timeout=5)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "timeout")

    def test_nonzero_exit_with_valid_json_is_parsed_not_discarded(self):
        # This is the O2a regression case (reliability-spec.md §13.5): a
        # non-zero exit still carries a fully-formed error JSON on stdout.
        stdout = json.dumps({
            "is_error": True, "result": "There's an issue with the model",
            "terminal_reason": "api_error", "api_error_status": 404,
        })
        with mock.patch.object(claude_cli.subprocess, "run",
                               return_value=_fake_completed(1, stdout)):
            res = claude_cli.run_claude("hi", model="bogus")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")  # 404 is not retryable
        self.assertIn("status=404", res.error)

    def test_transient_status_classified_as_transient(self):
        stdout = json.dumps({
            "is_error": True, "result": "overloaded",
            "terminal_reason": "api_error", "api_error_status": 529,
        })
        with mock.patch.object(claude_cli.subprocess, "run",
                               return_value=_fake_completed(1, stdout)):
            res = claude_cli.run_claude("hi")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "transient")

    def test_missing_api_error_status_fails_closed_to_env(self):
        stdout = json.dumps({
            "is_error": True, "result": "??",
            "terminal_reason": "api_error",
        })
        with mock.patch.object(claude_cli.subprocess, "run",
                               return_value=_fake_completed(1, stdout)):
            res = claude_cli.run_claude("hi")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")  # fail-closed, not transient

    def test_unparseable_stdout_is_env(self):
        with mock.patch.object(claude_cli.subprocess, "run",
                               return_value=_fake_completed(1, "not json", "boom")):
            res = claude_cli.run_claude("hi")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")

    def test_is_error_with_non_api_error_terminal_reason_is_behavioral(self):
        # Any other is_error terminal_reason (budget_exhausted,
        # structured_output_retry_exhausted, tool_deferred_unavailable,
        # turn_setup_failed, ...) is a CLI/model hiccup, not further
        # classified -- treated as retryable `behavioral` (§3.1).
        stdout = json.dumps({
            "is_error": True, "result": "x", "terminal_reason": "budget_exhausted",
        })
        with mock.patch.object(claude_cli.subprocess, "run",
                               return_value=_fake_completed(0, stdout)):
            res = claude_cli.run_claude("hi")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")

    def test_subtype_success_with_is_error_true_is_still_an_error(self):
        # Regression for reliability-spec.md §13.9.1: subtype must not be
        # trusted as a success signal.
        stdout = json.dumps({
            "is_error": True, "subtype": "success", "result": "x",
            "terminal_reason": "api_error", "api_error_status": 404,
        })
        with mock.patch.object(claude_cli.subprocess, "run",
                               return_value=_fake_completed(1, stdout)):
            res = claude_cli.run_claude("hi")
        self.assertFalse(res.ok)


class ClassifyResultTests(unittest.TestCase):
    """Phase 2.1 (reliability-spec.md §3.1): the full error_class table,
    exercised directly against classify_result() rather than through
    run_claude()'s subprocess plumbing."""

    def test_guardrail(self):
        stdout = json.dumps({"result": "ERROR: boom", "is_error": False})
        res = claude_cli.classify_result(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "guardrail")

    def test_refusal(self):
        stdout = json.dumps({
            "result": "[BLOCKED: mode-rule x] reason", "is_error": False})
        res = claude_cli.classify_result(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "refusal")

    def test_empty_body_is_behavioral(self):
        stdout = json.dumps({"result": "", "is_error": False})
        res = claude_cli.classify_result(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")
        self.assertEqual(res.error, "empty result")

    def test_empty_body_after_mode_line_strip_is_behavioral(self):
        stdout = json.dumps({"result": "[Mode: execute]\n", "is_error": False})
        res = claude_cli.classify_result(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")

    def test_empty_body_with_structured_output_is_not_an_error(self):
        # structured is present -> the empty text body is fine, the answer
        # lives in structured_output.
        stdout = json.dumps({"result": "", "is_error": False,
                             "structured_output": {"count": 1}})
        res = claude_cli.classify_result(0, stdout, "", schema="{}")
        self.assertTrue(res.ok)

    def test_schema_violation_is_behavioral(self):
        stdout = json.dumps({"result": "not json, no structured output",
                             "is_error": False})
        res = claude_cli.classify_result(0, stdout, "", schema='{"type":"object"}')
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")

    def test_denied_independent_of_is_error(self):
        # Phase 0 O1: observed with returncode=0, is_error=False.
        stdout = json.dumps({
            "result": "waiting for permission", "is_error": False,
            "permission_denials": [{"tool_name": "Write", "tool_use_id": "t1"}],
        })
        res = claude_cli.classify_result(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "denied")
        self.assertIn("Write", res.error)

    def test_denied_takes_priority_over_is_error(self):
        stdout = json.dumps({
            "result": "x", "is_error": True, "terminal_reason": "api_error",
            "api_error_status": 429,
            "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t1"}],
        })
        res = claude_cli.classify_result(0, stdout, "")
        self.assertEqual(res.error_class, "denied")

    def test_success_path(self):
        stdout = json.dumps({"result": "4", "is_error": False})
        res = claude_cli.classify_result(0, stdout, "")
        self.assertTrue(res.ok)
        self.assertIsNone(res.error_class)


class RetryableDebuggablePredicateTests(unittest.TestCase):
    def test_retryable_classes(self):
        for cls in ("timeout", "behavioral", "transient", None):
            self.assertTrue(claude_cli.is_retryable(cls), cls)

    def test_non_retryable_classes(self):
        for cls in ("env", "guardrail", "refusal", "denied"):
            self.assertFalse(claude_cli.is_retryable(cls), cls)

    def test_debuggable_classes(self):
        for cls in ("env", "timeout", "behavioral", "guardrail", None):
            self.assertTrue(claude_cli.is_debuggable(cls), cls)

    def test_non_debuggable_classes(self):
        for cls in ("refusal", "denied", "transient"):
            self.assertFalse(claude_cli.is_debuggable(cls), cls)


class SupportsSystemPromptFileTests(unittest.TestCase):
    def setUp(self):
        claude_cli._resolution_cache.clear()

    def tearDown(self):
        claude_cli._resolution_cache.clear()

    def test_unknown_option_means_unsupported(self):
        with mock.patch.object(
                claude_cli.subprocess, "run",
                return_value=_fake_completed(1, "", "error: unknown option "
                                                    "'--append-system-prompt-file'")):
            self.assertFalse(claude_cli._supports_system_prompt_file("claude"))

    def test_missing_file_error_means_supported(self):
        with mock.patch.object(
                claude_cli.subprocess, "run",
                return_value=_fake_completed(1, "", "Error: Append system "
                                                    "prompt file not found: x")):
            self.assertTrue(claude_cli._supports_system_prompt_file("claude"))

    def test_indeterminate_probe_fails_closed(self):
        # Neither verdict message -> do not assume support.
        with mock.patch.object(
                claude_cli.subprocess, "run",
                return_value=_fake_completed(1, "", "some unrelated crash")):
            self.assertFalse(claude_cli._supports_system_prompt_file("claude"))

    def test_probe_failure_fails_closed(self):
        with mock.patch.object(claude_cli.subprocess, "run",
                               side_effect=OSError("boom")):
            self.assertFalse(claude_cli._supports_system_prompt_file("claude"))

    def test_probe_result_is_cached(self):
        with mock.patch.object(
                claude_cli.subprocess, "run",
                return_value=_fake_completed(1, "", "not found")) as run:
            claude_cli._supports_system_prompt_file("claude")
            claude_cli._supports_system_prompt_file("claude")
        run.assert_called_once()

    def test_probe_cache_is_per_executable(self):
        with mock.patch.object(
                claude_cli.subprocess, "run",
                return_value=_fake_completed(1, "", "not found")) as run:
            claude_cli._supports_system_prompt_file("/a/claude")
            claude_cli._supports_system_prompt_file("/b/claude")
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
