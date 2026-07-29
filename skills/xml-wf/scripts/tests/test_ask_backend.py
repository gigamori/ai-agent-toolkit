"""Tests for `wfrun ask --backend` selection (mode-orchestrator-runs/
phase5-item1-cc-inventory-design.md §2.3): env-based `auto` detection,
mismatch warnings, and modelmap table routing.

claude_cli.ask_llm / pi_cli.ask_llm_pi are monkeypatched; no real CLI is
invoked."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import claude_cli, modelmap, pi_cli  # noqa: E402
from wfrun.__main__ import main  # noqa: E402


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue().strip(), err.getvalue()


class DetectAskBackendTests(unittest.TestCase):
    def test_cc_when_session_id_set(self):
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "abc"}):
            from wfrun.__main__ import _detect_ask_backend
            self.assertEqual(_detect_ask_backend(), "cc")

    def test_pi_when_session_id_unset(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            from wfrun.__main__ import _detect_ask_backend
            self.assertEqual(_detect_ask_backend(), "pi")

    def test_pi_when_session_id_empty_string(self):
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": ""}):
            from wfrun.__main__ import _detect_ask_backend
            self.assertEqual(_detect_ask_backend(), "pi")


class CmdAskBackendRoutingTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict("os.environ", {}, clear=False)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()

    def _clear_session_id(self):
        import os
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def test_auto_resolves_to_cc_and_uses_cc_table(self):
        import os
        os.environ["CLAUDE_CODE_SESSION_ID"] = "abc"
        with mock.patch.object(claude_cli, "ask_llm",
                               return_value=(True, "yes", 0.01)) as ask_llm, \
             mock.patch.object(modelmap, "resolve",
                               wraps=modelmap.resolve) as resolve:
            code, out, err = run_cli(["ask", "is it ok?"])
        self.assertEqual(code, 0)
        ask_llm.assert_called_once()
        self.assertEqual(resolve.call_args[0][1], "cc")
        self.assertEqual(err, "")

    def test_auto_resolves_to_pi_and_uses_llm_table(self):
        self._clear_session_id()
        with mock.patch.object(pi_cli, "ask_llm_pi",
                               return_value=(False, "no", 0.0)) as ask_llm_pi, \
             mock.patch.object(modelmap, "resolve",
                               wraps=modelmap.resolve) as resolve:
            code, out, err = run_cli(["ask", "is it ok?"])
        self.assertEqual(code, 0)
        ask_llm_pi.assert_called_once()
        self.assertEqual(resolve.call_args[0][1], "llm")
        self.assertEqual(err, "")

    def test_explicit_backend_overrides_auto_no_warning_when_matching(self):
        import os
        os.environ["CLAUDE_CODE_SESSION_ID"] = "abc"
        with mock.patch.object(claude_cli, "ask_llm",
                               return_value=(True, "yes", 0.0)) as ask_llm:
            code, out, err = run_cli(["ask", "q?", "--backend", "cc"])
        ask_llm.assert_called_once()
        self.assertEqual(err, "")

    def test_explicit_backend_mismatch_warns_but_still_runs(self):
        import os
        os.environ["CLAUDE_CODE_SESSION_ID"] = "abc"  # environment looks like cc
        with mock.patch.object(pi_cli, "ask_llm_pi",
                               return_value=(True, "yes", 0.0)) as ask_llm_pi:
            code, out, err = run_cli(["ask", "q?", "--backend", "pi"])
        self.assertEqual(code, 0)
        ask_llm_pi.assert_called_once()
        self.assertIn("warning", err)
        self.assertIn("pi", err)

    def test_log_records_backend(self, ):
        self._clear_session_id()
        with mock.patch.object(pi_cli, "ask_llm_pi",
                               return_value=(True, "because", 0.0)):
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                log_path = str(Path(d) / "steps.log")
                code, out, err = run_cli(["ask", "q?", "--log", log_path])
                self.assertEqual(code, 0)
                lines = Path(log_path).read_text(encoding="utf-8").splitlines()
                entry = json.loads(lines[-1])
                self.assertEqual(entry["backend"], "pi")


if __name__ == "__main__":
    unittest.main()
