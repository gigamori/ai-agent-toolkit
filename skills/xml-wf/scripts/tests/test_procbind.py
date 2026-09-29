import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])

STANDIN_SECONDS_BEFORE_FOLLOW_UP = 5
SPAWN_DEADLINE_S = 30
PARENT_REAP_TIMEOUT_S = 10
GONE_TIMEOUT_S = STANDIN_SECONDS_BEFORE_FOLLOW_UP + 10
LEFTOVER_SLEEP_S = 60
LEFTOVER_GRACE_S = 1

ARM_RUN_CC = "run_cc"
ARM_TREE_KILL = "tree_kill"
ARM_PI = "pi"

STANDIN_SCRIPT = f"""
import os
import sys
import time

base = sys.argv[1]
with open(base + ".pid.tmp", "w") as f:
    f.write(str(os.getpid()))
os.replace(base + ".pid.tmp", base + ".pid")
time.sleep({STANDIN_SECONDS_BEFORE_FOLLOW_UP})
with open(base + ".after", "w") as f:
    f.write("x")
"""

PARENT_SCRIPT = f"""
import sys

repo_scripts, arm, standin, base = sys.argv[1:5]
sys.path.insert(0, repo_scripts)

from wfrun import claude_cli, pi_cli

claude_cli._resolve_claude_bin = lambda: (sys.executable, False)
claude_cli.PRINT_ARGS = (standin, base)
pi_cli.resolve_pi_launcher = lambda: [sys.executable, standin, base]

if arm == "{ARM_RUN_CC}":
    claude_cli.run_claude("prompt", timeout=600)
elif arm == "{ARM_TREE_KILL}":
    claude_cli.run_claude("prompt", timeout=600, kill_tree=True)
else:
    pi_cli.run_pi("prompt", timeout=600)
"""

LEFTOVER_LAUNCHER_SCRIPT = f"""
import os
import subprocess
import sys

base = sys.argv[1]
leftover = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep({LEFTOVER_SLEEP_S})"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL)
with open(base + ".pid.tmp", "w") as f:
    f.write(str(leftover.pid))
os.replace(base + ".pid.tmp", base + ".pid")
"""


if os.name == "nt":
    import ctypes

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


class _WindowsProcessWatch:
    def __init__(self, pid):
        self._handle = _kernel32.OpenProcess(
            _SYNCHRONIZE | _PROCESS_TERMINATE, False, pid)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def wait_gone(self, seconds):
        waited = _kernel32.WaitForSingleObject(self._handle, int(seconds * _MS_PER_S))
        return waited == _WAIT_OBJECT_0

    def close(self):
        if not self.wait_gone(0):
            _kernel32.TerminateProcess(self._handle, _TERMINATED_BY_TEST)
            self.wait_gone(PARENT_REAP_TIMEOUT_S)
        _kernel32.CloseHandle(self._handle)


class _LinuxProcessWatch:
    def __init__(self, pid):
        self._pid = pid

    def _alive(self):
        try:
            stat = Path(f"/proc/{self._pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return False
        return stat.rsplit(")", 1)[1].split()[0] != "Z"

    def wait_gone(self, seconds):
        deadline = time.monotonic() + seconds
        while self._alive():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)
        return True

    def close(self):
        if self._alive():
            os.kill(self._pid, signal.SIGKILL)


def _wait_for_pid_file(test, pid_file, owner, label):
    deadline = time.monotonic() + SPAWN_DEADLINE_S
    while not pid_file.exists():
        if owner.poll() is not None and not pid_file.exists():
            out, err = owner.communicate()
            test.fail(f"{label}: launcher exited before the pid file appeared; "
                      f"returncode={owner.returncode} stdout={out!r} stderr={err!r}")
        if time.monotonic() > deadline:
            owner.kill()
            owner.communicate()
            test.fail(f"{label}: the pid file never appeared")
        time.sleep(0.05)
    return int(pid_file.read_text())


class _ParentDeathScenario:
    watch_class = None

    def _assert_standin_dies_with_parent(self, arm):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            base = work / "standin"
            standin_path = work / "_standin.py"
            standin_path.write_text(STANDIN_SCRIPT, encoding="utf-8")
            parent_path = work / "_parent.py"
            parent_path.write_text(PARENT_SCRIPT, encoding="utf-8")
            parent = subprocess.Popen(
                [sys.executable, str(parent_path), REPO_SCRIPTS_DIR, arm,
                 str(standin_path), str(base)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            pid = _wait_for_pid_file(self, Path(f"{base}.pid"), parent, arm)
            standin = self.watch_class(pid)
            try:
                parent.kill()
                parent.communicate(timeout=PARENT_REAP_TIMEOUT_S)
                gone = standin.wait_gone(GONE_TIMEOUT_S)
            finally:
                standin.close()
            self.assertFalse(
                Path(f"{base}.after").exists(),
                f"{arm}: the CLI stand-in kept working after its parent was killed")
            self.assertTrue(
                gone, f"{arm}: the CLI stand-in outlived its killed parent")

    def test_run_cc_launch_dies_with_its_killed_parent(self):
        self._assert_standin_dies_with_parent(ARM_RUN_CC)

    def test_tree_kill_launch_dies_with_its_killed_parent(self):
        self._assert_standin_dies_with_parent(ARM_TREE_KILL)

    def test_pi_launch_dies_with_its_killed_parent(self):
        self._assert_standin_dies_with_parent(ARM_PI)


@unittest.skipUnless(os.name == "nt", "Job Object binding is Windows-only")
class WindowsParentDeathTests(_ParentDeathScenario, unittest.TestCase):
    watch_class = _WindowsProcessWatch


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "PR_SET_PDEATHSIG binding is Linux-only")
class LinuxParentDeathTests(_ParentDeathScenario, unittest.TestCase):
    watch_class = _LinuxProcessWatch


@unittest.skipUnless(os.name == "nt", "Job Object binding is Windows-only")
class WindowsBindingReleaseTests(unittest.TestCase):
    def test_a_leftover_grandchild_survives_a_normal_exit_of_the_bound_child(self):
        from wfrun import procbind

        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            base = work / "leftover"
            launcher_path = work / "_launcher.py"
            launcher_path.write_text(LEFTOVER_LAUNCHER_SCRIPT, encoding="utf-8")
            launcher = procbind.popen_bound(
                [sys.executable, str(launcher_path), str(base)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            pid = _wait_for_pid_file(self, Path(f"{base}.pid"), launcher,
                                     "leftover launcher")
            leftover = _WindowsProcessWatch(pid)
            try:
                launcher.communicate(timeout=PARENT_REAP_TIMEOUT_S)
                survived = not leftover.wait_gone(LEFTOVER_GRACE_S)
            finally:
                leftover.close()
            self.assertEqual(launcher.returncode, 0)
            self.assertTrue(
                survived,
                "the binding is for a parent that dies mid-call; once the bound "
                "child has exited normally, what it left running must not be killed")


if __name__ == "__main__":
    unittest.main()
