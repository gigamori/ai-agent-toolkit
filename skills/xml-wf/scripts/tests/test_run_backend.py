"""Tests for `wfrun run --backend` (mode-orchestrator-runs/
phase6-run-pi-design.md §3.1, §3.3): auto/explicit/mismatch resolution
(mirrors `ask --backend`, tested in test_ask_backend.py), the pi-backend
startup fail-fast wired into cmd_run (pi_cli.pi_compat_errors), and
resume's backend inheritance via backend.json.

claude_cli.run_claude / pi_cli.run_pi are monkeypatched; no real CLI is
invoked."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import claude_cli, pi_cli  # noqa: E402
from wfrun.__main__ import main  # noqa: E402
from wfrun.claude_cli import CliResult  # noqa: E402


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue().strip(), err.getvalue()


MINIMAL_XML = """\
<workflow name="t" version="2" max="5">
  <step id="s1"><role>W</role><task>do it</task></step>
</workflow>
"""

# schema= alone is enough to trip pi_compat_errors; no output= needed, so
# a CC-backend run of this XML doesn't have to exercise stepio.unwrap_value.
SCHEMA_XML = """\
<workflow name="t" version="2" max="5">
  <step id="s1" schema='{"type":"object","properties":{"n":{"type":"integer"}},"required":["n"]}'>
    <role>W</role><task>count</task>
  </step>
</workflow>
"""

DEBUG_XML = """\
<workflow name="t" version="2" max="5">
  <step id="s1" on-error="debug"><role>W</role><task>do it</task></step>
</workflow>
"""


class RunBackendTestCase(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict("os.environ", {}, clear=False)
        self.env_patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def _clear_session_id(self):
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def _write(self, name: str, content: str) -> str:
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return str(path)


class BackendResolutionTests(RunBackendTestCase):
    """`--backend` auto detection / explicit override / mismatch warning --
    same shape as `ask --backend` (test_ask_backend.py)."""

    def test_auto_resolves_to_cc_and_uses_cc_table(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "abc"
        xml = self._write("wf.xml", MINIMAL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)) as run_claude:
            code, out, err = run_cli(["run", xml, "--run-dir", str(run_dir)])
        self.assertEqual(code, 0)
        run_claude.assert_called_once()
        self.assertEqual(
            json.loads((run_dir / "backend.json").read_text(encoding="utf-8")),
            {"backend": "cc"})

    def test_auto_resolves_to_pi_and_uses_pi_functions(self):
        self._clear_session_id()
        xml = self._write("wf.xml", MINIMAL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                pi_cli, "run_pi",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)) as run_pi:
            code, out, err = run_cli(["run", xml, "--run-dir", str(run_dir)])
        self.assertEqual(code, 0)
        run_pi.assert_called_once()
        self.assertEqual(
            json.loads((run_dir / "backend.json").read_text(encoding="utf-8")),
            {"backend": "pi"})

    def test_explicit_backend_overrides_auto_no_warning_when_matching(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "abc"
        xml = self._write("wf.xml", MINIMAL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)) as run_claude:
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir), "--backend", "cc"])
        self.assertEqual(code, 0)
        run_claude.assert_called_once()
        # unlike `ask`, `run` also emits lint findings to stderr (unrelated
        # to backend selection) -- only the backend-mismatch warning itself
        # must be absent here.
        self.assertNotIn("warning: --backend", err)

    def test_explicit_backend_mismatch_warns_but_still_runs(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "abc"  # environment looks like cc
        xml = self._write("wf.xml", MINIMAL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                pi_cli, "run_pi",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)) as run_pi:
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir), "--backend", "pi"])
        self.assertEqual(code, 0)
        run_pi.assert_called_once()
        self.assertIn("warning", err)
        self.assertIn("pi", err)


class PiBackendFailFastCliTests(RunBackendTestCase):
    """cmd_run wiring for pi_cli.pi_compat_errors (design §2.2, §2.3):
    rejected before run_dir gets any artifact and before run_pi is ever
    called -- message-content fidelity itself is covered directly against
    pi_compat_errors() in test_pi_cli.py."""

    def test_schema_workflow_rejected_before_any_run_dir_artifact(self):
        xml = self._write("wf.xml", SCHEMA_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(pi_cli, "run_pi") as run_pi:
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir), "--backend", "pi"])
        self.assertEqual(code, 1)
        self.assertIn("error: step 's1' declares schema=", err)
        self.assertIn('"Replacing schema="', err)
        run_pi.assert_not_called()
        self.assertFalse(run_dir.exists())

    def test_on_error_debug_workflow_rejected_with_three_hints(self):
        xml = self._write("wf.xml", DEBUG_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(pi_cli, "run_pi") as run_pi:
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir), "--backend", "pi"])
        self.assertEqual(code, 1)
        self.assertIn('on-error="debug"', err)
        self.assertIn('on-error="fail"', err)
        self.assertIn("retry=N", err)
        self.assertIn('on-error="ignore"', err)
        run_pi.assert_not_called()
        self.assertFalse(run_dir.exists())

    def test_schema_workflow_is_unaffected_under_cc_backend(self):
        # The fail-fast is pi-specific: claude enforces schema= itself.
        xml = self._write("wf.xml", SCHEMA_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=True, text="3", cost_usd=0.0)) as run_claude:
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir), "--backend", "cc"])
        self.assertEqual(code, 0)
        run_claude.assert_called_once()


class ResumeBackendInheritanceTests(RunBackendTestCase):
    """resume reads backend.json rather than re-detecting (design §3.3):
    an environment change between run and resume must not switch the
    execution facility mid-run."""

    def test_resume_reuses_pi_backend_regardless_of_current_environment(self):
        xml = self._write("wf.xml", MINIMAL_XML)
        run_dir = self.dir / "runs" / "r1"
        # Original run: explicit pi backend, step fails so a resume applies.
        with mock.patch.object(
                pi_cli, "run_pi",
                return_value=CliResult(ok=False, error="ERROR: die",
                                       error_class="guardrail", cost_usd=0.0)):
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir), "--backend", "pi"])
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads((run_dir / "backend.json").read_text(encoding="utf-8")),
            {"backend": "pi"})

        # Resume, with the environment now looking like Claude Code: the
        # recorded backend ("pi") must still be the one used, not "cc".
        os.environ["CLAUDE_CODE_SESSION_ID"] = "abc"
        with mock.patch.object(
                pi_cli, "run_pi",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)) as run_pi, \
             mock.patch.object(claude_cli, "run_claude") as run_claude:
            code, out, err = run_cli(["resume", str(run_dir)])
        self.assertEqual(code, 0)
        run_pi.assert_called_once()
        run_claude.assert_not_called()

    def test_resume_without_backend_json_defaults_to_cc(self):
        xml = self._write("wf.xml", MINIMAL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=False, error="ERROR: die",
                                       error_class="guardrail", cost_usd=0.0)):
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir), "--backend", "cc"])
        self.assertEqual(code, 1)
        (run_dir / "backend.json").unlink()  # simulate a pre-existing run dir

        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)) as run_claude, \
             mock.patch.object(pi_cli, "run_pi") as run_pi:
            code, out, err = run_cli(["resume", str(run_dir)])
        self.assertEqual(code, 0)
        run_claude.assert_called_once()
        run_pi.assert_not_called()


if __name__ == "__main__":
    unittest.main()
