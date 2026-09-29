import errno
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import claude_cli, pi_cli, runlock  # noqa: E402
from wfrun.__main__ import main  # noqa: E402
from wfrun.claude_cli import CliResult  # noqa: E402

REPO_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_IN_USE = 2

BACKENDS = ("cc", "pi")
LOCK_NAME = "run.lock"
OLD_WORKFLOW = "<old/>"

CLI_TIMEOUT_S = 120
HOLDER_READY_TIMEOUT_S = 30
HOLDER_EXIT_TIMEOUT_S = 30

MINIMAL_XML = """\
<workflow name="t" version="2" max="5">
  <step id="s1"><role>W</role><task>do it</task></step>
</workflow>
"""

OK_RESULT = CliResult(ok=True, text="ok", cost_usd=0.0)
FAILED_RESULT = CliResult(ok=False, error="ERROR: die", error_class="guardrail",
                          cost_usd=0.0)

if os.name == "nt":
    CHILD_PATH = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
else:
    CHILD_PATH = os.pathsep.join(["/usr/bin", "/bin"])

HOLDER_SCRIPT = """
import sys
from wfrun import runlock

handle = runlock.try_acquire(sys.argv[1])
print("held" if handle is not None else "contended", flush=True)
sys.stdin.read()
if handle is not None:
    runlock.release(handle)
"""

HOOK_SCRIPT = """
import os

from wfrun import claude_cli, pi_cli
from wfrun.claude_cli import CliResult


def _standin(*args, **kwargs):
    with open(os.environ["WFRUN_RDL_EVIDENCE"], "a", encoding="utf-8") as f:
        f.write("launched\\n")
    return CliResult(ok=False, error="ERROR: stand-in", error_class="guardrail",
                     cost_usd=0.0)


claude_cli.run_claude = _standin
pi_cli.run_pi = _standin
"""


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue().strip(), err.getvalue()


def backend_patch(backend, **kwargs):
    if backend == "pi":
        return mock.patch.object(pi_cli, "run_pi", **kwargs)
    return mock.patch.object(claude_cli, "run_claude", **kwargs)


class RunDirLockTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env_patch = mock.patch.dict("os.environ", {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.dir = Path(self.tmp.name)
        self.xml = self.dir / "wf.xml"
        self.xml.write_text(MINIMAL_XML, encoding="utf-8")

    def run_dir_for(self, backend):
        return self.dir / "runs" / backend

    def seed_failed_run(self, backend):
        run_dir = self.run_dir_for(backend)
        with backend_patch(backend, return_value=FAILED_RESULT):
            code, out, err = run_cli(["run", str(self.xml), "--run-dir", str(run_dir),
                                      "--backend", backend])
        self.assertEqual(code, EXIT_FAILED, err)
        return run_dir

    def hold_lock(self, run_dir):
        run_dir.mkdir(parents=True, exist_ok=True)
        handle = runlock.try_acquire(run_dir / LOCK_NAME)
        self.assertIsNotNone(handle)
        self.addCleanup(runlock.release, handle)
        return handle

    def events_bytes(self, run_dir):
        path = run_dir / "events.jsonl"
        return path.read_bytes() if path.exists() else b""


class RefusalTests(RunDirLockTestCase):
    def test_resume_is_refused_while_another_holder_has_the_run_dir_lock(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                run_dir = self.seed_failed_run(backend)
                self.hold_lock(run_dir)
                before = self.events_bytes(run_dir)
                with mock.patch.object(claude_cli, "run_claude",
                                       return_value=FAILED_RESULT) as run_claude, \
                     mock.patch.object(pi_cli, "run_pi",
                                       return_value=FAILED_RESULT) as run_pi:
                    code, out, err = run_cli(["resume", str(run_dir)])
                self.assertEqual(code, EXIT_IN_USE, err)
                self.assertIn(LOCK_NAME, err)
                run_claude.assert_not_called()
                run_pi.assert_not_called()
                self.assertEqual(self.events_bytes(run_dir), before)

    def test_run_into_a_locked_run_dir_is_refused_before_writing_anything(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                run_dir = self.run_dir_for(backend)
                self.hold_lock(run_dir)
                (run_dir / "workflow.xml").write_text(OLD_WORKFLOW, encoding="utf-8")
                with mock.patch.object(claude_cli, "run_claude",
                                       return_value=FAILED_RESULT) as run_claude, \
                     mock.patch.object(pi_cli, "run_pi",
                                       return_value=FAILED_RESULT) as run_pi:
                    code, out, err = run_cli(["run", str(self.xml), "--run-dir",
                                              str(run_dir), "--backend", backend])
                self.assertEqual(code, EXIT_IN_USE, err)
                self.assertIn(LOCK_NAME, err)
                run_claude.assert_not_called()
                run_pi.assert_not_called()
                self.assertEqual((run_dir / "workflow.xml").read_text(encoding="utf-8"),
                                 OLD_WORKFLOW)
                self.assertFalse((run_dir / "backend.json").exists())
                self.assertFalse((run_dir / "events.jsonl").exists())

    def test_a_refused_resume_does_not_ingest_its_answers(self):
        run_dir = self.seed_failed_run("cc")
        self.hold_lock(run_dir)
        answer = self.dir / "answer.md"
        answer.write_text("option: 1\n", encoding="utf-8")
        with mock.patch("wfrun.__main__._ingest_answers") as ingest, \
             backend_patch("cc", return_value=FAILED_RESULT) as run_claude:
            code, out, err = run_cli(["resume", str(run_dir), "--answer",
                                      f"s1={answer}"])
        self.assertEqual(code, EXIT_IN_USE, err)
        ingest.assert_not_called()
        run_claude.assert_not_called()


class HoldAndReleaseTests(RunDirLockTestCase):
    def probing_result(self, run_dir, seen, result):
        def side_effect(*args, **kwargs):
            seen.append(runlock.probe(run_dir / LOCK_NAME))
            return result
        return side_effect

    def test_run_holds_the_lock_while_steps_execute_and_releases_it_after(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                run_dir = self.run_dir_for(backend)
                seen = []
                with backend_patch(backend, side_effect=self.probing_result(
                        run_dir, seen, OK_RESULT)):
                    code, out, err = run_cli(["run", str(self.xml), "--run-dir",
                                              str(run_dir), "--backend", backend])
                self.assertEqual(code, EXIT_OK, err)
                self.assertEqual(seen, [runlock.HELD])
                self.assertEqual(runlock.probe(run_dir / LOCK_NAME), runlock.FREE)

    def test_resume_holds_the_lock_while_steps_execute_and_releases_it_after(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                run_dir = self.seed_failed_run(backend)
                seen = []
                with backend_patch(backend, side_effect=self.probing_result(
                        run_dir, seen, OK_RESULT)):
                    code, out, err = run_cli(["resume", str(run_dir)])
                self.assertEqual(code, EXIT_OK, err)
                self.assertEqual(seen, [runlock.HELD])
                self.assertEqual(runlock.probe(run_dir / LOCK_NAME), runlock.FREE)

    def test_a_failed_run_and_a_failed_resume_both_release_the_lock(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                run_dir = self.seed_failed_run(backend)
                self.assertEqual(runlock.probe(run_dir / LOCK_NAME), runlock.FREE)
                with backend_patch(backend, return_value=FAILED_RESULT):
                    code, out, err = run_cli(["resume", str(run_dir)])
                self.assertEqual(code, EXIT_FAILED, err)
                self.assertEqual(runlock.probe(run_dir / LOCK_NAME), runlock.FREE)


class FailOpenTests(RunDirLockTestCase):
    def test_locking_that_is_unsupported_here_falls_open_with_a_note(self):
        run_dir = self.run_dir_for("cc")
        with mock.patch.object(runlock, "try_acquire",
                               side_effect=OSError(errno.ENOSYS, "not implemented")), \
             backend_patch("cc", return_value=OK_RESULT) as run_claude:
            code, out, err = run_cli(["run", str(self.xml), "--run-dir", str(run_dir),
                                      "--backend", "cc"])
        self.assertEqual(code, EXIT_OK, err)
        run_claude.assert_called_once()
        self.assertIn(f"note: {run_dir / LOCK_NAME}", err)

    def test_a_lock_that_cannot_be_taken_for_another_reason_falls_open_with_a_warning(self):
        run_dir = self.seed_failed_run("cc")
        with mock.patch.object(runlock, "try_acquire",
                               side_effect=OSError(errno.EIO, "io error")), \
             backend_patch("cc", return_value=OK_RESULT) as run_claude:
            code, out, err = run_cli(["resume", str(run_dir)])
        self.assertEqual(code, EXIT_OK, err)
        run_claude.assert_called_once()
        self.assertIn(f"warning: could not take {run_dir / LOCK_NAME}", err)


class CrossProcessRefusalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.xml = self.work / "wf.xml"
        self.xml.write_text(MINIMAL_XML, encoding="utf-8")
        self.run_dir = self.work / "run"
        self.lock_path = self.run_dir / LOCK_NAME
        self.evidence = self.work / "evidence.txt"
        hook_dir = self.work / "hooks"
        hook_dir.mkdir()
        (hook_dir / "sitecustomize.py").write_text(HOOK_SCRIPT, encoding="utf-8")
        self.assertIsNone(
            shutil.which("claude", path=CHILD_PATH),
            "the children's PATH must not resolve a real claude, whatever the hook does")
        kept = {k: v for k, v in os.environ.items()
                if k.upper() not in ("PATH", "PYTHONPATH")
                and not k.upper().startswith(("ANTHROPIC", "CLAUDE"))}
        self.env = dict(
            kept, PATH=CHILD_PATH, PYTHONUTF8="1",
            PYTHONPATH=os.pathsep.join([str(hook_dir), REPO_SCRIPTS_DIR]),
            WFRUN_RDL_EVIDENCE=str(self.evidence))

    def wfrun(self, *argv):
        proc = subprocess.run(
            [sys.executable, "-m", "wfrun", *argv], env=self.env, cwd=self.work,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=CLI_TIMEOUT_S)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def launches(self):
        if not self.evidence.exists():
            return 0
        return len(self.evidence.read_text(encoding="utf-8").splitlines())

    def start_holder(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER_SCRIPT, str(self.lock_path)], env=self.env,
            cwd=self.work, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8")
        self.addCleanup(self.stop_holder, holder)
        return holder

    def stop_holder(self, holder):
        if holder.poll() is None:
            holder.stdin.close()
            try:
                holder.wait(timeout=HOLDER_EXIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=HOLDER_EXIT_TIMEOUT_S)
        holder.stdout.close()

    def test_a_resume_in_another_process_is_refused_until_the_holder_is_gone(self):
        code, out, err = self.wfrun("run", str(self.xml), "--run-dir", str(self.run_dir),
                                    "--backend", "cc")
        self.assertEqual(code, EXIT_FAILED, err)
        launched_by_run = self.launches()
        self.assertGreater(launched_by_run, 0)

        holder = self.start_holder()
        self.assertEqual(holder.stdout.readline().strip(), "held")
        events_before = (self.run_dir / "events.jsonl").read_bytes()

        code, out, err = self.wfrun("resume", str(self.run_dir))
        self.assertEqual(code, EXIT_IN_USE, err)
        self.assertIn(LOCK_NAME, err)
        self.assertEqual(self.launches(), launched_by_run,
                         "a resume refused for a live run dir must not launch any step")
        self.assertEqual((self.run_dir / "events.jsonl").read_bytes(), events_before)

        self.stop_holder(holder)
        code, out, err = self.wfrun("resume", str(self.run_dir))
        self.assertEqual(code, EXIT_FAILED, err)
        self.assertGreater(self.launches(), launched_by_run,
                           "once the holder is gone the same resume must run the step")


if __name__ == "__main__":
    unittest.main()
