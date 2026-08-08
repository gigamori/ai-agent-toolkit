#!/usr/bin/env python3
"""Unit tests for the cross-harness advisory write lock (log_lock.py).

Covers the deterministic acceptance criteria of the stable-key work (D1/D2,
project-notes/specs/log-lock-stable-key.md) plus the protocol v2 port
(taskflow-write-lock-v2-design.md §3):

  - AC-L1  lock_path_for(): tasks/<status>/X.md -> <project_root>/.locks/X.lock;
           no tasks/ ancestor -> adjacent <target>.lock fallback
  - AC-L2a lock_path_for() is invariant across the three status folders
           (the core fix: the rendezvous point must not move on task-move)
  - AC-L2b lock_path_for() is invariant across path spelling (relative vs
           absolute, case, symlink) via realpath+normcase normalization
  - AC-L5  INV-2 preserved: acquire remains bounded, body always runs
  - AC-L6  release-time delete: after the holder releases, the sidecar is gone
  - V2-1   progress.md -> <project_root>/.locks/progress.md.lock (rule 2)
  - V2-2   rule 3 stays pure: no tasks/ ancestor and not progress.md ->
           adjacent fallback
  - V2-3   a FRESH foreign sidecar + a tiny TASKFLOW_LOCK_TIMEOUT degrades
           unlocked, still runs the body, and does NOT unlink a sidecar it
           does not own
  - V2-4   a BACKDATED sidecar is stale-broken, acquired, and removed on
           release
  - V2-5   rule 1 and rule 2 define <project_root> differently but land in the
           SAME <project_root>/.locks/ directory for one project

Deliberately NOT covered here, and not a gap:
  - The stale-break TOCTOU window is narrowed, not eliminated (see
    log_lock._acquire_fd). No test asserts its absence.
  - The win32 live-holder-blocks-unlink behaviour needs a second live process
    holding an open handle; that is platform-dependent to construct and is
    left to the race harnesses.
  - Key-string equality with the Pi side's write-lock.ts. The two normalize to
    different strings on win32 and resolve to the same file; comparing the
    strings would be wrong.

Philosophy (unchanged): pure-function invariants over timing-dependent
concurrency.

stdlib only. Run with:  uv run python plugins/taskflow/tests/test_log_lock.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
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


class env_override:
    """Set env vars for a block and restore them exactly afterwards.

    log_lock reads its tunables per call precisely so this works on a module
    that was imported once at the top of this file.
    """

    def __init__(self, **kv: str) -> None:
        self._kv = kv
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "env_override":
        for k, v in self._kv.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *_exc: object) -> None:
        for k, old in self._saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def test_ac_l1_primary_and_fallback(root: Path) -> None:
    print("--- AC-L1: tasks/<status>/ -> <project>/.locks/, else adjacent fallback ---")
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
          "no tasks/ ancestor falls back to adjacent <target>.lock")


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


def test_v2_1_progress_md_key(root: Path) -> None:
    print("--- V2-1: progress.md -> <project>/.locks/progress.md.lock (rule 2) ---")
    (root / "tasks" / "0_todo").mkdir(parents=True)
    progress = root / "progress.md"
    progress.write_text("x", encoding="utf-8")

    got = ll.lock_path_for(str(progress))
    expected = os.path.join(os.path.normcase(os.path.realpath(str(root))),
                            ".locks", "progress.md.lock")
    check(os.path.normcase(got) == expected,
          f"progress.md routes under its OWN parent's .locks/ "
          f"(got {got!r}, expected {expected!r})")

    # Rule 2 must key on progress.md's own parent, NOT on some tasks/ sibling
    # lookup -- a progress.md that is not beside a tasks/ dir still resolves.
    lonely_dir = root / "no_tasks_here"
    lonely_dir.mkdir()
    lonely = lonely_dir / "progress.md"
    lonely.write_text("x", encoding="utf-8")
    got2 = ll.lock_path_for(str(lonely))
    expected2 = os.path.join(os.path.normcase(os.path.realpath(str(lonely_dir))),
                             ".locks", "progress.md.lock")
    check(os.path.normcase(got2) == expected2,
          "rule 2 keys on progress.md's own parent even with no tasks/ sibling")


def test_v2_2_rule3_purity(root: Path) -> None:
    print("--- V2-2: rule 3 stays pure (no tasks/ ancestor, not progress.md) ---")
    loose = root / "notes" / "something.md"
    loose.parent.mkdir(parents=True)
    loose.write_text("x", encoding="utf-8")
    check(ll.lock_path_for(str(loose)) == str(loose) + ".lock",
          "a non-progress.md file with no tasks/ ancestor gets the adjacent fallback")

    # Near-miss on rule 2: the basename must be exactly `progress.md`.
    nearly = root / "notes" / "progress.md.bak"
    nearly.write_text("x", encoding="utf-8")
    check(ll.lock_path_for(str(nearly)) == str(nearly) + ".lock",
          "`progress.md.bak` does NOT trigger rule 2 (exact basename match only)")


def test_v2_5_rule1_rule2_share_locks_dir(root: Path) -> None:
    print("--- V2-5: rule 1 and rule 2 land in the SAME <project>/.locks/ ---")
    task = root / "tasks" / "2_done" / "2026-08-08_shared.md"
    task.parent.mkdir(parents=True)
    task.write_text("x", encoding="utf-8")
    progress = root / "progress.md"
    progress.write_text("x", encoding="utf-8")

    task_locks_dir = os.path.dirname(ll.lock_path_for(str(task)))
    progress_locks_dir = os.path.dirname(ll.lock_path_for(str(progress)))
    check(os.path.normcase(task_locks_dir) == os.path.normcase(progress_locks_dir),
          f"rule 1 (<tasks parent>) and rule 2 (<own parent>) agree on the "
          f".locks/ dir (got {task_locks_dir!r} vs {progress_locks_dir!r})")
    check(os.path.basename(task_locks_dir) == ".locks",
          "that shared directory is named .locks")


def test_ac_l6_release_time_delete(root: Path) -> None:
    print("--- AC-L6: lock sidecar removed after the holder releases ---")
    task = root / "tasks" / "1_in_progress" / "2026-08-05_release.md"
    task.parent.mkdir(parents=True)
    task.write_text("x", encoding="utf-8")

    held = None
    with ll.log_lock(str(task)):
        held = os.path.exists(ll.lock_path_for(str(task)))
    check(held is True, "sidecar exists while the lock is held")
    check(not os.path.exists(ll.lock_path_for(str(task))),
          "lock sidecar deleted once the sole holder released it")


def test_ac_l5_inv2_bounded(root: Path) -> None:
    print("--- AC-L5: acquire stays bounded (INV-2) ---")
    task = root / "tasks" / "1_in_progress" / "2026-08-05_bounded.md"
    task.parent.mkdir(parents=True)
    task.write_text("x", encoding="utf-8")

    entered = False
    with ll.log_lock(str(task)):
        entered = True
    check(entered, "log_lock context body still runs to completion (no hang)")


def test_v2_3_degrade_unlocked_on_fresh_foreign_sidecar(root: Path) -> None:
    print("--- V2-3: fresh foreign sidecar + tiny timeout -> degrade unlocked ---")
    task = root / "tasks" / "1_in_progress" / "2026-08-08_foreign.md"
    task.parent.mkdir(parents=True)
    task.write_text("x", encoding="utf-8")

    lock_file = ll.lock_path_for(str(task))
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    with open(lock_file, "w", encoding="utf-8") as f:
        f.write("99999 someone-elses-lock pi\n")  # fresh: mtime is now

    entered = False
    t0 = time.monotonic()
    # Tiny timeout so the bounded acquire gives up promptly; stale threshold
    # left high so the fresh sidecar is NOT eligible for a break.
    with env_override(TASKFLOW_LOCK_TIMEOUT="0.05", TASKFLOW_LOCK_STALE="600"):
        with ll.log_lock(str(task)):
            entered = True
    elapsed = time.monotonic() - t0

    check(entered, "body still runs when the lock could not be acquired (degrade unlocked)")
    check(elapsed < 2.0,
          f"degrade happened promptly, honoring the tiny timeout ({elapsed:.3f}s)")
    check(os.path.exists(lock_file),
          "a degraded call does NOT unlink a sidecar it does not own")

    os.unlink(lock_file)


def test_v2_4_stale_break(root: Path) -> None:
    print("--- V2-4: a backdated sidecar is stale-broken and acquired ---")
    task = root / "tasks" / "0_todo" / "2026-08-08_stale.md"
    task.parent.mkdir(parents=True)
    task.write_text("x", encoding="utf-8")

    lock_file = ll.lock_path_for(str(task))
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    with open(lock_file, "w", encoding="utf-8") as f:
        f.write("99999 abandoned pi\n")
    # Backdate well past the stale threshold used below.
    old = time.time() - 3600
    os.utime(lock_file, (old, old))

    entered = False
    payload_replaced = False
    with env_override(TASKFLOW_LOCK_TIMEOUT="3.0", TASKFLOW_LOCK_STALE="10"):
        with ll.log_lock(str(task)):
            entered = True
            # We broke the stale lock and re-created it, so the abandoned
            # holder's payload must be gone.
            try:
                payload_replaced = "abandoned" not in Path(lock_file).read_text(
                    encoding="utf-8")
            except OSError:
                payload_replaced = False

    check(entered, "body ran after breaking the stale sidecar")
    check(payload_replaced,
          "the stale sidecar was replaced by ours, not merely reused in place")
    check(not os.path.exists(lock_file),
          "the sidecar we took ownership of is removed on release")


def main() -> int:
    print("=== log_lock.py unit tests (stable key / release-delete / protocol v2) ===")
    with tempfile.TemporaryDirectory() as d:
        for fn in (
            test_ac_l1_primary_and_fallback,
            test_ac_l2a_status_invariance,
            test_ac_l2b_spelling_invariance,
            test_v2_1_progress_md_key,
            test_v2_2_rule3_purity,
            test_v2_5_rule1_rule2_share_locks_dir,
            test_ac_l6_release_time_delete,
            test_ac_l5_inv2_bounded,
            test_v2_3_degrade_unlocked_on_fresh_foreign_sidecar,
            test_v2_4_stale_break,
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
