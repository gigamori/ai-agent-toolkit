import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import claude_cli  # noqa: E402
from wfrun.__main__ import main  # noqa: E402
from wfrun.claude_cli import CliResult  # noqa: E402


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue().strip(), err.getvalue()


NO_MODEL_XML = """\
<workflow name="t" version="2" max="5">
  <step id="s1"><role>W</role><task>do it</task></step>
</workflow>
"""

HAS_MODEL_XML = """\
<workflow name="t" version="2" max="5">
  <step id="s1" model="opus"><role>W</role><task>do it</task></step>
</workflow>
"""


class RunInheritModelTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, content: str) -> str:
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return str(path)


class InheritModelPersistenceTests(RunInheritModelTestCase):
    def test_inherit_model_json_written_when_given(self):
        xml = self._write("wf.xml", NO_MODEL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)):
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir),
                 "--backend", "cc", "--inherit-model", "session-model"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads((run_dir / "inherit_model.json").read_text(encoding="utf-8")),
            {"inherit_model": "session-model"})

    def test_inherit_model_json_written_as_null_when_not_given(self):
        xml = self._write("wf.xml", HAS_MODEL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)):
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir), "--backend", "cc"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads((run_dir / "inherit_model.json").read_text(encoding="utf-8")),
            {"inherit_model": None})


class InheritModelFallbackWarningTests(RunInheritModelTestCase):
    def test_warns_when_step_has_no_model_and_no_inherit_given(self):
        xml = self._write("wf.xml", NO_MODEL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)):
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir), "--backend", "cc"])
        self.assertEqual(code, 0)
        self.assertIn("note:", err)
        self.assertIn("--inherit-model", err)
        self.assertIn("s1", err)

    def test_no_warning_when_inherit_model_given(self):
        xml = self._write("wf.xml", NO_MODEL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)):
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir),
                 "--backend", "cc", "--inherit-model", "session-model"])
        self.assertEqual(code, 0)
        self.assertNotIn("--inherit-model given", err)

    def test_no_warning_when_step_has_its_own_model(self):
        xml = self._write("wf.xml", HAS_MODEL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)):
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir), "--backend", "cc"])
        self.assertEqual(code, 0)
        self.assertNotIn("note:", err)


class ResumeInheritModelTests(RunInheritModelTestCase):
    def test_resume_reuses_inherit_model_from_the_original_run(self):
        xml = self._write("wf.xml", NO_MODEL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=False, error="ERROR: die",
                                       error_class="guardrail", cost_usd=0.0)):
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir),
                 "--backend", "cc", "--inherit-model", "session-model"])
        self.assertEqual(code, 1, "the original run must fail so a resume applies")

        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)) as run_claude:
            code, out, err = run_cli(["resume", str(run_dir)])
        self.assertEqual(code, 0)
        run_claude.assert_called_once()
        self.assertEqual(run_claude.call_args.kwargs["model"], "session-model")

    def test_resume_without_inherit_model_json_defaults_to_none(self):
        xml = self._write("wf.xml", HAS_MODEL_XML)
        run_dir = self.dir / "runs" / "r1"
        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=False, error="ERROR: die",
                                       error_class="guardrail", cost_usd=0.0)):
            code, out, err = run_cli(
                ["run", xml, "--run-dir", str(run_dir), "--backend", "cc"])
        self.assertEqual(code, 1, "the original run must fail so a resume applies")
        (run_dir / "inherit_model.json").unlink()

        with mock.patch.object(
                claude_cli, "run_claude",
                return_value=CliResult(ok=True, text="ok", cost_usd=0.0)) as run_claude:
            code, out, err = run_cli(["resume", str(run_dir)])
        self.assertEqual(code, 0)
        run_claude.assert_called_once()


if __name__ == "__main__":
    unittest.main()
