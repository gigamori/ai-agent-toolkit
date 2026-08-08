#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Unit tests: `.locks/progress.md.lock` is exempt from dead-lock sweeping.

Protocol v2 (taskflow-write-lock-v2-design.md §3.1 rule 2) gave `progress.md`
its own sidecar in the SAME `<project>/.locks/` directory that task-md sidecars
live in. Both dead-lock classifiers there key on "does a task md with this
basename still exist?", so without an explicit exemption `progress.md.lock`
would be reported as dead in every project, forever, the moment a rebuild ran.

The exemption exists in TWO separate implementations that must stay in
lockstep:
  - scripts/check_progress.py::check_orphan_lock  (report-only, check #9)
  - scripts/clean_locks.py::find_dead_locks       (the deleting sweep)

`clean_locks.py` deletes for real, and `_projects/` is typically gitignored, so
a false positive there is unrecoverable data loss of a LIVE lock. That is why
this is pinned rather than left to the two docstrings.

Not covered here, by design: a crash-orphaned `progress.md.lock` is reclaimed
by the next writer's stale-break (hooks/log_lock.py `_acquire_fd`), not by
either sweep. That path is covered in tests/test_log_lock.py (V2-4).

Run with:  uv run --script plugins/taskflow/tests/test_lock_sweep_exemption.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_progress as cp  # noqa: E402
import clean_locks as cl  # noqa: E402

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


TASK_BODY = "# Live Task\n\nbody\n"


def make_fixture(root: Path) -> Path:
    """A project whose `.locks/` holds one live, one dead, one exempt sidecar."""
    project_dir = root / "proj"
    todo = project_dir / "tasks" / "0_todo"
    todo.mkdir(parents=True)
    (todo / "2026-08-08_live.md").write_text(TASK_BODY, encoding="utf-8")

    locks = project_dir / ".locks"
    locks.mkdir()
    # live: its task md still exists.
    (locks / "2026-08-08_live.md.lock").write_text("", encoding="utf-8")
    # dead: the task md was deleted or renamed, so nothing resolves here again.
    (locks / "2026-08-08_gone.md.lock").write_text("", encoding="utf-8")
    # exempt: keyed on progress.md itself, never on a task md.
    (locks / "progress.md.lock").write_text("", encoding="utf-8")
    return project_dir


def test_check_progress_orphan_lock(root: Path) -> None:
    print("--- check_progress.check_orphan_lock: exempts progress.md.lock ---")
    project_dir = make_fixture(root)
    result = cp.Result()
    cp.check_orphan_lock(project_dir, result)

    findings = [f for f in result.findings if f.check == "orphan_lock"]
    messages = " | ".join(f.message for f in findings)

    check(len(findings) == 1,
          f"exactly one orphan_lock finding (got {len(findings)}: {messages})")
    check("2026-08-08_gone.md.lock" in messages,
          "the genuinely dead sidecar IS reported")
    check("progress.md.lock" not in messages,
          "progress.md.lock is NOT reported as dead")
    check("2026-08-08_live.md.lock" not in messages,
          "the live task's sidecar is NOT reported as dead")


def test_clean_locks_find_dead(root: Path) -> None:
    print("--- clean_locks.find_dead_locks: exempts progress.md.lock ---")
    project_dir = make_fixture(root)
    dead = cl.find_dead_locks(project_dir)
    names = sorted(p.name for p in dead)

    check(names == ["2026-08-08_gone.md.lock"],
          f"only the genuinely dead sidecar is swept (got {names})")
    check("progress.md.lock" not in names,
          "progress.md.lock would NOT be deleted by --apply")


def test_exemption_constant_is_shared_spelling() -> None:
    print("--- the two implementations agree on the exempt basename ---")
    check(cl.PROGRESS_LOCK_NAME == "progress.md.lock",
          f"clean_locks.PROGRESS_LOCK_NAME is the v2 rule-2 basename "
          f"(got {cl.PROGRESS_LOCK_NAME!r})")


def main() -> int:
    print("=== progress.md.lock dead-sweep exemption (protocol v2 rule 2) ===")
    with tempfile.TemporaryDirectory() as d1:
        test_check_progress_orphan_lock(Path(d1))
    with tempfile.TemporaryDirectory() as d2:
        test_clean_locks_find_dead(Path(d2))
    test_exemption_constant_is_shared_spelling()

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
