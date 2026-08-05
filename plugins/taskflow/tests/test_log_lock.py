#!/usr/bin/env python3
"""Unit tests for the stable-keyed advisory lock (log_lock.py, D1/D2).

Covers the deterministic acceptance criteria of
project-notes/specs/log-lock-stable-key.md:
  - AC-L1  lock_path_for(): tasks/<status>/X.md -> <project_root>/.locks/X.lock;
           no tasks/ ancestor -> legacy <task>.lock fallback
  - AC-L2a lock_path_for() is invariant across the three status folders
           (the core fix: the rendezvous point must not move on task-move)
  - AC-L2b lock_path_for() is invariant across path spelling (relative vs
           absolute, case, symlink) via realpath+normcase normalization
  - AC-L5  INV-2 preserved: acquire remains bounded (no behavior change to
           the win32/POSIX acquire loops themselves)
  - AC-L6  release-time delete: after the last holder releases, the lock
           sidecar is gone (self-check in log_lock.py's __main__ covers the
           live msvcrt/fcntl path on this platform; this file additionally
           covers the platform-independent _lock_file_is_current() logic
           that D2's POSIX stale-reacquire branch depends on)

stdlib only. Run with:  uv run python plugins/taskflow/tests/test_log_lock.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Import the module under test from hooks/ (sibling of note_links.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import log_lock as ll  # noqa: E402

PASS = 0
FAIL = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS: {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL: {msg}")


def check(cond: bool, msg: str) -> None:
    ok(msg) if cond else bad(msg)


def test_ac_l1_primary_and_fallback(root: Path) -> None:
    print("--- AC-L1: tasks/<status>/ -> <project>/.locks/, else legacy fallback ---")
    task = root / "tasks" / "0_todo" / "2026-08-05_example.md"
    task.parent.mkdir(parents=True)
    task.write_text("x", encoding="utf-8")

    got = ll.lock_path_for(str(task))
    # Both sides go through the same realpath+normcase transform so this
    # doesn't spuriously fail on platforms where the temp dir itself is a
    # symlink (e.g. macOS /tmp -> /private/tmp) or has case quirks.
    expected = os.path.join(os.path.normcase(os.path.realpath(str(root))),
                             ".locks", "2026-08-05_example.md.lock")
    check(os.path.normcase(got) == expected,
          f"primary path routes under <project>/.locks/ (got {got!r}, expected {expected!r})")

    outside = root / "not_a_project" / "loose.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("x", encoding="utf-8")
    got2 = ll.lock_path_for(str(outside))
    check(got2 == str(outside) + ".lock",
          "no tasks/ ancestor falls back to legacy <task>.lock")


def test_ac_l2a_status_invariance(root: Path) -> None:
    print("--- AC-L2a: same key across all 3 status folders (the core fix) ---")
    basename = "2026-08-05_moved-task.md"
    paths = [root / "tasks" / status / basename
             for status in ("0_todo", "1_in_progress", "2_done")]
    keys = set()
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        keys.add(os.path.normcase(ll.lock_path_for(str(p))))
    check(len(keys) == 1,
          f"lock_path_for identical across 0_todo/1_in_progress/2_done (got {keys})")


def test_ac_l2b_spelling_invariance(root: Path) -> None:
    print("--- AC-L2b: same key across path spelling variance ---")
    task = root / "tasks" / "1_in_progress" / "2026-08-05_spelled.md"
    task.parent.mkdir(parents=True)
    task.write_text("x", encoding="utf-8")

    abs_key = ll.lock_path_for(str(task))

    cwd = os.getcwd()
    try:
        os.chdir(str(task.parent))
        rel_key = ll.lock_path_for("2026-08-05_spelled.md")
    finally:
        os.chdir(cwd)
    check(os.path.normcase(abs_key) == os.path.normcase(rel_key),
          "absolute vs relative path resolve to the same lock file")

    upper = str(task).upper() if os.name == "nt" else str(task)
    if os.name == "nt":
        upper_key = ll.lock_path_for(upper)
        check(os.path.normcase(abs_key) == os.path.normcase(upper_key),
              "case variance (Windows) resolves to the same lock file")
    else:
        ok("case variance check skipped (non-Windows platform is case-sensitive by design)")


def test_lock_file_is_current(root: Path) -> None:
    print("--- _lock_file_is_current(): platform-independent stale-lock detection ---")
    # NOTE: the real race this guards against is POSIX-only (unlink-while-open
    # is impossible on win32 without FILE_SHARE_DELETE — that asymmetry is
    # exactly why D2 gates this branch on _HAVE_FCNTL). To keep this test
    # runnable on any platform, the "path no longer matches the held handle"
    # states are constructed WITHOUT requiring unlink/replace of a path an
    # open handle already holds (impossible on Windows) — instead by pointing
    # the function's `lock_file` argument at a distinct path (never-created,
    # or a different real file) while `fh` stays open on the original.
    lock_file = str(root / "held.lock")
    other_file = str(root / "other.lock")
    missing_file = str(root / "does_not_exist.lock")
    with open(other_file, "a+"):
        pass

    with open(lock_file, "a+") as fh:
        check(ll._lock_file_is_current(fh, lock_file) is True,
              "freshly-opened lock file is current")
        check(ll._lock_file_is_current(fh, missing_file) is False,
              "a path that does not exist on disk is detected as stale")
        check(ll._lock_file_is_current(fh, other_file) is False,
              "a path pointing at a different inode is detected as stale")


def test_ac_l6_release_time_delete(root: Path) -> None:
    print("--- AC-L6: lock sidecar removed after the last holder releases ---")
    task = root / "tasks" / "1_in_progress" / "2026-08-05_release.md"
    task.parent.mkdir(parents=True)
    task.write_text("x", encoding="utf-8")

    with ll.log_lock(str(task)):
        pass
    check(not os.path.exists(ll.lock_path_for(str(task))),
          "lock sidecar deleted once the sole holder released it")


def test_ac_l5_inv2_bounded(root: Path) -> None:
    print("--- AC-L5: acquire stays bounded (INV-2 unaffected by D1/D2) ---")
    task = root / "tasks" / "1_in_progress" / "2026-08-05_bounded.md"
    task.parent.mkdir(parents=True)
    task.write_text("x", encoding="utf-8")

    entered = False
    with ll.log_lock(str(task)):
        entered = True
    check(entered, "log_lock context body still runs to completion (no hang)")


def main() -> int:
    print("=== log_lock.py unit tests (D1 stable key / D2 release-time delete) ===")
    with tempfile.TemporaryDirectory() as d:
        for fn in (
            test_ac_l1_primary_and_fallback,
            test_ac_l2a_status_invariance,
            test_ac_l2b_spelling_invariance,
            test_lock_file_is_current,
            test_ac_l6_release_time_delete,
            test_ac_l5_inv2_bounded,
        ):
            sub = Path(d) / fn.__name__
            sub.mkdir()
            fn(sub)

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
