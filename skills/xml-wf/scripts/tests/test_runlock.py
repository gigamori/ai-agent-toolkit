import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])

HOLD_SECONDS = 120
SPAWN_DEADLINE_S = 30
KILL_REAP_TIMEOUT_S = 10
FREE_DEADLINE_S = 10
POLL_INTERVAL_S = 0.05

HOLDER_SCRIPT = f"""
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from wfrun import runlock

held = runlock.acquire(sys.argv[2])
Path(sys.argv[3]).write_text("held", encoding="utf-8")
time.sleep({HOLD_SECONDS})
"""


class RunLockProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_probe_missing_file_is_unknown_and_creates_nothing(self):
        from wfrun import runlock
        path = self.dir / "s1_run.lock"
        result = runlock.probe(str(path))
        self.assertEqual(result, runlock.UNKNOWN,
            "a run lock path that was never created must probe as "
            "unknown, not free -- free means a wrapper once held it and "
            "let go, unknown means no wrapper has ever touched it")
        self.assertFalse(path.exists(),
            "probing must never create the lock file itself")

    def test_probe_sees_held_then_free_across_handles_same_process(self):
        from wfrun import runlock
        path = self.dir / "s1_run.lock"
        held = runlock.acquire(str(path))
        try:
            result = runlock.probe(str(path))
            self.assertEqual(result, runlock.HELD,
                "a second handle in this same process must see the lock "
                "as held while the first handle still holds it")
        finally:
            runlock.release(held)
        self.assertEqual(runlock.probe(str(path)), runlock.FREE,
            "once the holder releases, a fresh probe must see free")

    def test_try_acquire_fails_while_held_and_succeeds_after_release(self):
        from wfrun import runlock
        path = self.dir / "s1_run.lock"
        held = runlock.acquire(str(path))
        try:
            self.assertIsNone(runlock.try_acquire(str(path)),
                "a non-blocking acquire while the lock is already held "
                "must return none rather than block or raise")
        finally:
            runlock.release(held)
        second = runlock.try_acquire(str(path))
        self.assertIsNotNone(second,
            "once the first holder releases, a non-blocking acquire must "
            "succeed")
        runlock.release(second)

    def test_probe_sees_another_process_hold_then_release_by_being_killed(self):
        from wfrun import runlock
        path = self.dir / "s1_run.lock"
        ready = self.dir / "ready.txt"
        script = self.dir / "_holder.py"
        script.write_text(HOLDER_SCRIPT, encoding="utf-8")
        holder = subprocess.Popen(
            [sys.executable, str(script), REPO_SCRIPTS_DIR, str(path), str(ready)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + SPAWN_DEADLINE_S
            while not ready.exists():
                if holder.poll() is not None:
                    self.fail("the holder exited before taking the lock; "
                              f"returncode={holder.returncode} "
                              f"stderr={holder.stderr.read()!r}")
                if time.monotonic() > deadline:
                    self.fail("the holder never reported holding the lock")
                time.sleep(POLL_INTERVAL_S)
            self.assertEqual(runlock.probe(str(path)), runlock.HELD,
                "a lock held by another live process must probe as held")
            holder.kill()
            holder.wait(timeout=KILL_REAP_TIMEOUT_S)
            free_deadline = time.monotonic() + FREE_DEADLINE_S
            result = runlock.probe(str(path))
            while result != runlock.FREE and time.monotonic() < free_deadline:
                time.sleep(POLL_INTERVAL_S)
                result = runlock.probe(str(path))
            self.assertEqual(result, runlock.FREE,
                "the OS must release a killed process's lock within a bounded time")
        finally:
            holder.kill()
            holder.wait(timeout=KILL_REAP_TIMEOUT_S)
            holder.stderr.close()


if __name__ == "__main__":
    unittest.main()
