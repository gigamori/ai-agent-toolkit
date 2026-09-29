import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])

STEP_ID = "s1"
STEP_TIMEOUT_S = 600
STANDIN_SLEEP_S = 120

SPAWN_DEADLINE_S = 60
DUPLICATE_LAUNCH_WINDOW_S = 5
CLI_TIMEOUT_S = 180
WAIT_RUNNING_S = 2
WAIT_ABORT_S = 10
WAIT_OK_S = 60
LOCK_FREE_DEADLINE_S = 10
PROCESS_GONE_S = 20
POLL_INTERVAL_S = 0.05

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ABORTED = 3
EXIT_RUNNING = 10

MODE_SLEEP = "sleep"
MODE_OK = "ok"

if os.name == "nt":
    CHILD_PATH = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
else:
    CHILD_PATH = os.pathsep.join(["/usr/bin", "/bin"])

STANDIN_SCRIPT = f"""
import json
import os
import sys
import time

piddir = sys.argv[1]
target = os.path.join(piddir, "standin-%d.pid" % os.getpid())
with open(target + ".tmp", "w") as f:
    f.write(str(os.getpid()))
os.replace(target + ".tmp", target)
sys.stdin.read()
if os.environ["WFRUN_IT_MODE"] == "{MODE_OK}":
    print(json.dumps({{"result": "Counted the rows.\\nVALUE: 42",
                      "is_error": False, "permission_denials": []}}))
else:
    time.sleep({STANDIN_SLEEP_S})
"""

HOOK_SCRIPT = """
import os
import sys

import wfrun
from wfrun import claude_cli

claude_cli._resolve_claude_bin = lambda: (sys.executable, False)
claude_cli._supports_system_prompt_file = lambda *a, **k: True
claude_cli.PRINT_ARGS = (os.environ["WFRUN_IT_STANDIN"],
                         os.environ["WFRUN_IT_PIDDIR"])
with open(os.environ["WFRUN_IT_EVIDENCE"], "a", encoding="utf-8") as f:
    f.write("%d\\t%s\\n" % (os.getpid(), wfrun.__file__))
"""

WORKFLOW_XML = f"""
<workflow name="it" version="2" max="10">
  <step id="{STEP_ID}" role="w" retry="0" timeout="{STEP_TIMEOUT_S}"
        output="answer" output-type="value">
    <task>answer it</task>
  </step>
</workflow>
"""

AGENT_FILE = "---\nname: w\ndescription: test\ntools: Read\n---\nROLE-BODY"


if os.name == "nt":
    _SYNCHRONIZE = 0x00100000
    _PROCESS_TERMINATE = 0x0001
    _WAIT_OBJECT_0 = 0x00000000
    _TERMINATED_BY_TEST = 1
    _MS_PER_S = 1000

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    _kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    _kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    _kernel32.TerminateProcess.restype = ctypes.c_int
    _kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    _kernel32.CloseHandle.restype = ctypes.c_int


class _WindowsWatch:
    def __init__(self, pid, label):
        self.pid = pid
        self.label = label
        self._handle = _kernel32.OpenProcess(
            _SYNCHRONIZE | _PROCESS_TERMINATE, False, pid)
        if not self._handle:
            raise OSError(ctypes.get_last_error(), f"OpenProcess failed for pid {pid}")

    def wait_gone(self, seconds):
        waited = _kernel32.WaitForSingleObject(self._handle, int(seconds * _MS_PER_S))
        return waited == _WAIT_OBJECT_0

    def kill(self):
        if not self.wait_gone(0):
            _kernel32.TerminateProcess(self._handle, _TERMINATED_BY_TEST)

    def close(self):
        _kernel32.CloseHandle(self._handle)


class _PosixWatch:
    def __init__(self, pid, label):
        self.pid = pid
        self.label = label
        if not self._alive():
            raise OSError(0, f"pid {pid} is not alive")

    def _alive(self):
        proc_stat = Path(f"/proc/{self.pid}/stat")
        if proc_stat.parent.parent.is_dir():
            try:
                stat = proc_stat.read_text()
            except (FileNotFoundError, ProcessLookupError):
                return False
            return stat.rsplit(")", 1)[1].split()[0] != "Z"
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def wait_gone(self, seconds):
        deadline = time.monotonic() + seconds
        while self._alive():
            if time.monotonic() > deadline:
                return False
            time.sleep(POLL_INTERVAL_S)
        return True

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self):
        pass


_Watch = _WindowsWatch if os.name == "nt" else _PosixWatch


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _poll(predicate, seconds):
    deadline = time.monotonic() + seconds
    while True:
        if predicate():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(POLL_INTERVAL_S)


class DispatchWaitProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wfrun-it-")
        self.addCleanup(self.tmp.cleanup)
        self.watches = {}
        self.standin_pids = []
        self.addCleanup(self.reap)

        self.work = Path(self.tmp.name)
        self.piddir = self.work / "pids"
        self.piddir.mkdir()
        self.run_dir = self.work / "run"
        self.xml = self.work / "wf.xml"
        self.xml.write_text(WORKFLOW_XML, encoding="utf-8")
        self.vars_path = self.work / "vars.json"
        self.vars_path.write_text("{}", encoding="utf-8")
        agents_dir = self.work / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "w.md").write_text(AGENT_FILE, encoding="utf-8")
        hook_dir = self.work / "hooks"
        hook_dir.mkdir()
        (hook_dir / "sitecustomize.py").write_text(HOOK_SCRIPT, encoding="utf-8")
        standin = self.work / "standin.py"
        standin.write_text(STANDIN_SCRIPT, encoding="utf-8")
        self.evidence = self.work / "evidence.txt"

        self.assertIsNone(
            shutil.which("claude", path=CHILD_PATH),
            "the children's PATH must not resolve a real claude, whatever the hook does")
        kept = {k: v for k, v in os.environ.items()
                if k.upper() not in ("PATH", "PYTHONPATH")
                and not k.upper().startswith(("ANTHROPIC", "CLAUDE"))}
        self.env = dict(
            kept, PATH=CHILD_PATH, PYTHONUTF8="1",
            PYTHONPATH=os.pathsep.join([str(hook_dir), REPO_SCRIPTS_DIR]),
            WFRUN_IT_STANDIN=str(standin), WFRUN_IT_PIDDIR=str(self.piddir),
            WFRUN_IT_EVIDENCE=str(self.evidence), WFRUN_IT_MODE=MODE_SLEEP)

    def watch(self, pid, label, required=True):
        if pid in self.watches:
            return self.watches[pid]
        try:
            watch = _Watch(pid, label)
        except OSError as e:
            if required:
                self.fail(f"could not hold a process handle on {label} (pid {pid}): {e}")
            return None
        self.watches[pid] = watch
        return watch

    def reap(self):
        for watch in self.watches.values():
            watch.kill()
        survivors = [f"{w.label} (pid {w.pid})" for w in self.watches.values()
                     if not w.wait_gone(PROCESS_GONE_S)]
        for watch in self.watches.values():
            watch.close()
        self.assertEqual(survivors, [],
                         "processes started by this test survived their kill")

    def assert_all_gone(self):
        survivors = [f"{w.label} (pid {w.pid})" for w in self.watches.values()
                     if not w.wait_gone(PROCESS_GONE_S)]
        self.assertEqual(survivors, [],
                         "every process this test started must be gone by the end")

    def wfrun(self, *argv):
        proc = subprocess.run(
            [sys.executable, "-m", "wfrun", *argv], env=self.env, cwd=self.work,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=CLI_TIMEOUT_S)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def dispatch(self, *extra):
        code, out, err = self.wfrun(
            "dispatch", str(self.xml), STEP_ID, "--vars", str(self.vars_path),
            "--run-dir", str(self.run_dir), *extra)
        launched = re.search(r"pid=(\d+)", out)
        if launched:
            self.watch(int(launched.group(1)), "dispatch launch", required=False)
        return code, out, err

    def wait(self, seconds, cycle=1):
        return self.wfrun(
            "wait", str(self.step_file("handle", cycle)), "--max", str(seconds),
            "--vars", str(self.vars_path), "--log", str(self.run_dir / "steps.log"))

    def step_file(self, kind, cycle=1):
        return self.run_dir / "steps" / f"{STEP_ID}_c{cycle:02d}_{kind}.json"

    def read_handle(self, cycle=1):
        return _read_json(self.step_file("handle", cycle)) or {}

    def read_ledger(self, cycle=1):
        return _read_json(self.step_file("attempts", cycle)) or []

    def read_marker(self):
        return _read_json(self.run_dir / "steps" / f"{STEP_ID}_run.json") or {}

    def lock_path(self):
        return str(self.run_dir / "steps" / f"{STEP_ID}_run.lock")

    def logged_statuses(self):
        try:
            lines = (self.run_dir / "steps.log").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries = [json.loads(line) for line in lines if line.strip()]
        return [e["status"] for e in entries if e.get("step") == STEP_ID]

    def describe(self):
        steps_dir = self.run_dir / "steps"
        listing = sorted(p.name for p in steps_dir.iterdir()) if steps_dir.is_dir() else []
        return (f"steps dir: {listing}; stand-in pid files: "
                f"{sorted(p.name for p in self.piddir.glob('standin-*.pid'))}")

    def standin_count(self):
        for pid_file in sorted(self.piddir.glob("standin-*.pid"),
                               key=lambda p: p.stat().st_mtime):
            pid = int(pid_file.read_text())
            if pid not in self.standin_pids:
                self.standin_pids.append(pid)
                self.watch(pid, f"stand-in {len(self.standin_pids)}", required=False)
        return len(self.standin_pids)

    def standin_appears(self, count, seconds):
        return _poll(lambda: self.standin_count() >= count, seconds)

    def await_standins(self, count):
        self.assertTrue(
            self.standin_appears(count, SPAWN_DEADLINE_S),
            f"stand-in number {count} never started; {self.describe()}")

    def assert_hook_loaded_from_scripts_dir(self):
        lines = self.evidence.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines, "the sitecustomize hook never ran in any child")
        root = Path(REPO_SCRIPTS_DIR).resolve()
        for line in lines:
            pid, loaded_from = line.split("\t", 1)
            self.assertIn(root, Path(loaded_from).resolve().parents,
                          f"pid {pid} imported wfrun from {loaded_from}, not from {root}")

    def assert_marker_matches_handle(self):
        handle = self.read_handle()
        marker = self.read_marker()
        self.assertTrue(handle.get("token"),
                        "the handle must carry the current attempt's token")
        self.assertEqual(marker.get("token"), handle.get("token"),
                         "the run marker must carry the current attempt's token")
        return marker

    def kill_admitted_wrapper(self, runlock, attempt):
        marker = self.assert_marker_matches_handle()
        self.assertEqual(marker.get("attempt"), attempt)
        self.assertEqual(runlock.probe(self.lock_path()), runlock.HELD,
                         "an admitted, running wrapper must hold the run lock")
        wrapper = self.watch(marker["pid"], f"wrapper of attempt {attempt}")
        wrapper.kill()
        freed = _poll(lambda: runlock.probe(self.lock_path()) == runlock.FREE,
                      LOCK_FREE_DEADLINE_S)
        self.assertTrue(
            freed, f"the run lock stayed held {LOCK_FREE_DEADLINE_S}s after its wrapper was killed")
        if os.name == "nt" or sys.platform.startswith("linux"):
            standin = self.watches.get(self.standin_pids[attempt - 1])
            self.assertIsNotNone(standin, "the stand-in must be under watch")
            self.assertTrue(standin.wait_gone(PROCESS_GONE_S),
                            "the stand-in must die with its killed wrapper")

    def test_live_attempt_refuses_redispatch_and_killed_attempts_abort_once_each(self):
        code, out, err = self.dispatch()
        self.assertEqual(code, EXIT_OK, f"first dispatch: {out} {err}")
        self.await_standins(1)
        self.assert_hook_loaded_from_scripts_dir()
        first_handle = self.read_handle()

        code, out, err = self.dispatch()
        self.assertFalse(
            self.standin_appears(2, DUPLICATE_LAUNCH_WINDOW_S),
            "a second stand-in started: dispatch launched a duplicate of a live attempt")
        self.assertEqual(code, EXIT_REFUSED, f"second dispatch: {out} {err}")
        self.assertIn("wait", err, "the refusal must point the caller at wait")
        self.assertEqual(self.read_handle().get("attempt"), 1)
        self.assertEqual(self.read_handle().get("token"), first_handle.get("token"))
        self.assertEqual(self.read_ledger(), [])
        self.assert_marker_matches_handle()

        code, out, err = self.wait(WAIT_RUNNING_S)
        self.assertEqual(code, EXIT_RUNNING, f"wait on a live attempt: {out} {err}")

        from wfrun import runlock
        self.kill_admitted_wrapper(runlock, attempt=1)
        code, out, err = self.wait(WAIT_ABORT_S)
        self.assertEqual(
            code, EXIT_ABORTED,
            f"wait must call a killed attempt aborted long before its deadline: {out} {err}")
        self.assertIn("run-lock", err)
        ledger = self.read_ledger()
        self.assertEqual([e.get("class") for e in ledger], ["aborted"])
        self.assertEqual(ledger[0].get("evidence"), "run-lock")
        self.assertEqual(self.logged_statuses(), ["aborted"])

        code, out, err = self.dispatch()
        self.assertEqual(code, EXIT_OK, f"the one re-dispatch after an abort: {out} {err}")
        self.assertIn("seq=2", out)
        self.await_standins(2)
        second_handle = self.read_handle()
        self.assertNotEqual(second_handle.get("token"), first_handle.get("token"))
        self.assertEqual(second_handle.get("attempt"), 2)
        self.kill_admitted_wrapper(runlock, attempt=2)
        code, out, err = self.wait(WAIT_ABORT_S)
        self.assertEqual(code, EXIT_ABORTED, f"second abort: {out} {err}")
        self.assertEqual([e.get("seq") for e in self.read_ledger()], [1, 2])
        self.assertEqual(self.logged_statuses(), ["aborted", "aborted"])

        code, out, err = self.dispatch()
        self.assertFalse(
            self.standin_appears(3, DUPLICATE_LAUNCH_WINDOW_S),
            "a third stand-in started: the one re-dispatch after an abort was exceeded")
        self.assertEqual(code, EXIT_REFUSED, f"third dispatch: {out} {err}")
        self.assertIn("stop and report", err)
        self.assertEqual(self.read_handle().get("attempt"), 2)
        self.assertEqual(len(self.read_ledger()), 2)
        self.assert_all_gone()

    def test_a_normal_finish_is_never_recorded_as_aborted(self):
        self.env["WFRUN_IT_MODE"] = MODE_OK
        code, out, err = self.dispatch()
        self.assertEqual(code, EXIT_OK, f"dispatch: {out} {err}")
        self.await_standins(1)
        self.assert_hook_loaded_from_scripts_dir()

        code, out, err = self.wait(WAIT_OK_S)
        self.assertEqual(code, EXIT_OK, f"wait on a normal finish: {out} {err}")
        self.assertEqual([e.get("class") for e in self.read_ledger()], ["ok"])
        self.assertNotIn("aborted", self.logged_statuses())
        variables = json.loads(self.vars_path.read_text(encoding="utf-8"))
        self.assertEqual(variables.get("answer"), "42")

        self.assert_marker_matches_handle()
        from wfrun import runlock
        self.assertTrue(
            _poll(lambda: runlock.probe(self.lock_path()) == runlock.FREE,
                  LOCK_FREE_DEADLINE_S),
            "a wrapper that finished normally must release the run lock")

        code, out, err = self.dispatch()
        self.assertEqual(code, EXIT_OK, f"dispatch after a finished step: {out} {err}")
        self.assertIn("cycle=2", out)
        self.await_standins(2)
        code, out, err = self.wait(WAIT_OK_S, cycle=2)
        self.assertEqual(code, EXIT_OK, f"wait on cycle 2: {out} {err}")
        self.assertEqual([e.get("class") for e in self.read_ledger(cycle=2)], ["ok"])
        self.assertNotIn("aborted", self.logged_statuses())
        self.assert_all_gone()


if __name__ == "__main__":
    unittest.main()
