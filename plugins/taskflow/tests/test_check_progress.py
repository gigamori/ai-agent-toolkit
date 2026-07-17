#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Unit tests for scripts/check_progress.py check #10: check_duplicate_basename.

Covers project-notes/specs/review-2026-07-17-fixes.md P3-1 (F4):
  - check_duplicate_basename() walks the WHOLE tasks/ tree with os.walk (not
    just the 3 status folders) so a duplicate basename in a stray subdir
    (e.g. a hand-made tasks/_archive/) is still caught — this must mirror
    hooks/session_progress_capture.py::_task_basename_index's walk range.
  - a duplicate is reported as a `violation` Finding listing all colliding
    paths.
  - a clean fixture (no duplicate basenames) produces no findings from this
    check.
  - running the check through check_progress.main() end-to-end drives a
    non-zero process exit when a duplicate is present, and a zero exit /
    "OK: ..." message on a clean fixture.

check_progress.py has a PEP723 header declaring `pyyaml` as a dependency, so
this test also declares it and must be run via:

    uv run --script plugins/taskflow/tests/test_check_progress.py

(plain `uv run python <path>` will fail with `ModuleNotFoundError: yaml` in
this repo's ambient uv environment, which has no project-level pyyaml dep.)

Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_progress as cp  # noqa: E402

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


TASK_BODY = "# Dup Task\n\nSome content, no frontmatter.\n"


def make_duplicate_fixture(root: Path) -> Path:
    """project/tasks/0_todo/2026-01-01_dup.md and project/tasks/_archive/2026-01-01_dup.md
    (a stray subdir NOT one of the 3 status folders) share the same basename."""
    project_dir = root / "proj"
    todo = project_dir / "tasks" / "0_todo"
    archive = project_dir / "tasks" / "_archive"
    todo.mkdir(parents=True)
    archive.mkdir(parents=True)
    (todo / "2026-01-01_dup.md").write_text(TASK_BODY, encoding="utf-8")
    (archive / "2026-01-01_dup.md").write_text(TASK_BODY, encoding="utf-8")
    return project_dir


def make_clean_fixture(root: Path) -> Path:
    """project/tasks/0_todo with two DISTINCT basenames — no duplication."""
    project_dir = root / "proj"
    todo = project_dir / "tasks" / "0_todo"
    todo.mkdir(parents=True)
    (todo / "2026-01-01_dup.md").write_text(TASK_BODY, encoding="utf-8")
    (todo / "2026-01-02_other.md").write_text(TASK_BODY, encoding="utf-8")
    return project_dir


def test_duplicate_direct(root: Path) -> None:
    print("--- check_duplicate_basename: duplicate across stray subdir ---")
    project_dir = make_duplicate_fixture(root)
    result = cp.Result()
    cp.check_duplicate_basename(project_dir, result)

    dup_findings = [f for f in result.findings if f.check == "duplicate_basename"]
    check(len(dup_findings) == 1,
          f"exactly one duplicate_basename finding (got {len(dup_findings)})")
    if not dup_findings:
        return
    f = dup_findings[0]
    check(f.severity == "violation", f"severity is 'violation' (got {f.severity!r})")
    check("tasks/0_todo/2026-01-01_dup.md" in f.message,
          "message lists the 0_todo/ location")
    check("tasks/_archive/2026-01-01_dup.md" in f.message,
          "message lists the stray _archive/ location (whole-tree walk)")
    check("2026-01-01_dup.md" in f.message, "message names the colliding basename")


def test_clean_direct(root: Path) -> None:
    print("--- check_duplicate_basename: clean fixture (distinct basenames) ---")
    project_dir = make_clean_fixture(root)
    result = cp.Result()
    cp.check_duplicate_basename(project_dir, result)
    check(len(result.findings) == 0,
          f"no findings for distinct basenames (got {len(result.findings)})")


def test_main_duplicate_nonzero_exit(root: Path) -> None:
    print("--- main(): duplicate fixture drives non-zero exit ---")
    project_dir = make_duplicate_fixture(root)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cp.main([str(project_dir)])
    out = buf.getvalue()
    check(rc == 1, f"main() returns 1 on a duplicate fixture (got {rc})")
    check("duplicate_basename" in out, "stdout mentions the duplicate_basename check")
    check("VIOLATION" in out, "stdout prints the VIOLATION severity label")


def test_main_clean_zero_exit(root: Path) -> None:
    print("--- main(): clean fixture drives zero exit ---")
    project_dir = make_clean_fixture(root)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cp.main([str(project_dir)])
    out = buf.getvalue()
    check(rc == 0, f"main() returns 0 on a clean fixture (got {rc})")
    check("OK: no drift, no violations, no stale tasks." in out,
          "stdout prints the OK message")


def main() -> int:
    print("=== check_progress.py check #10 (duplicate_basename) unit tests ===")
    with tempfile.TemporaryDirectory() as d1:
        test_duplicate_direct(Path(d1))
    with tempfile.TemporaryDirectory() as d2:
        test_clean_direct(Path(d2))
    with tempfile.TemporaryDirectory() as d3:
        test_main_duplicate_nonzero_exit(Path(d3))
    with tempfile.TemporaryDirectory() as d4:
        test_main_clean_zero_exit(Path(d4))

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
