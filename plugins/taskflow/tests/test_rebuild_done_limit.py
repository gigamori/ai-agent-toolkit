#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Unit tests for scripts/rebuild_progress.py's Completed-table row cap.

Covers project-notes/specs/done-table-row-cap.md:
  - render_table_region() caps the Completed section to the N most recent
    rows (by original ascending order) and appends a footnote reporting the
    omitted count when the cap is active (T1).
  - When the Completed row count is at or below the cap, no footnote is
    added and all rows are shown (T2).
  - done_limit <= 0 means unlimited (T3).
  - TASKFLOW_DONE_ROWS_MAX env var is honored when --done-rows-max is not
    passed on the CLI (T4).
  - Capping the Completed table does not introduce false positives in
    check_progress.py, since checks #1/#6 only validate rows present in the
    table (forward direction only) (T5).

rebuild_progress.py and check_progress.py both have a PEP723 header
declaring `pyyaml` as a dependency, so this test also declares it and must
be run via:

    uv run --script plugins/taskflow/tests/test_rebuild_done_limit.py

Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_progress as cp  # noqa: E402
import rebuild_progress as rp  # noqa: E402

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


def make_done_rows(count: int) -> list[rp.TaskRow]:
    """count TaskRow entries with ascending n / date, as gather_tasks would
    produce for files named 2026-06-01_done.md .. 2026-06-{count:02d}_done.md."""
    rows = []
    for i in range(1, count + 1):
        date = f"2026-06-{i:02d}"
        rows.append(
            rp.TaskRow(
                n=i,
                priority="MID",
                h1=f"Done Task {date}",
                date=date,
                link=f"@tasks/2_done/{date}_done.md",
            )
        )
    return rows


def make_task_file(dir_path: Path, date: str, slug: str) -> Path:
    content = (
        f"---\npriority: MID\ncreated: {date}\nupdated: {date}\n---\n\n"
        f"# Done Task {date}\n\nBody.\n"
    )
    path = dir_path / f"{date}_{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


def make_project_with_done(root: Path, count: int) -> Path:
    project_dir = root / "proj"
    done_dir = project_dir / "tasks" / "2_done"
    done_dir.mkdir(parents=True)
    for i in range(1, count + 1):
        make_task_file(done_dir, f"2026-06-{i:02d}", "done")
    return project_dir


def test_render_capped(root: Path) -> None:
    print("--- render_table_region: 12 done rows, default cap 10 ---")
    by_status = {"0_todo": [], "1_in_progress": [], "2_done": make_done_rows(12)}
    region = rp.render_table_region(by_status, rp.DONE_ROWS_MAX_DEFAULT)

    check(region.count("@tasks/2_done/") == 10, "exactly 10 Completed rows rendered")
    check("2026-06-01" not in region, "oldest omitted row (06-01) is not in the region")
    check("2026-06-03" in region, "first shown row (06-03, n=3) is in the region")
    check("2026-06-12" in region, "newest row (06-12, n=12) is in the region")
    check(
        "Showing the latest 10 of 12 completed tasks — 2 older entries omitted"
        in region,
        "footnote reports latest 10 of 12, 2 omitted",
    )
    check("tasks/2_done/" in region.rsplit("Showing", 1)[-1], "footnote points at tasks/2_done/")


def test_render_under_limit(root: Path) -> None:
    print("--- render_table_region: 3 done rows, cap 10 (no-op) ---")
    by_status = {"0_todo": [], "1_in_progress": [], "2_done": make_done_rows(3)}
    region = rp.render_table_region(by_status, 10)

    check(region.count("@tasks/2_done/") == 3, "all 3 Completed rows rendered")
    check("Showing the latest" not in region, "no footnote when under the cap")


def test_render_unlimited(root: Path) -> None:
    print("--- render_table_region: 12 done rows, done_limit=0 (unlimited) ---")
    by_status = {"0_todo": [], "1_in_progress": [], "2_done": make_done_rows(12)}
    region = rp.render_table_region(by_status, 0)

    check(region.count("@tasks/2_done/") == 12, "all 12 Completed rows rendered")
    check("Showing the latest" not in region, "no footnote when done_limit is unlimited")


def test_env_override(root: Path) -> None:
    print("--- main(): TASKFLOW_DONE_ROWS_MAX env overrides the default cap ---")
    project_dir = make_project_with_done(root, 12)
    old_env = os.environ.get(rp.ENV_DONE_ROWS_MAX)
    os.environ[rp.ENV_DONE_ROWS_MAX] = "2"
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = rp.main([str(project_dir)])
        out = buf.getvalue()
    finally:
        if old_env is None:
            os.environ.pop(rp.ENV_DONE_ROWS_MAX, None)
        else:
            os.environ[rp.ENV_DONE_ROWS_MAX] = old_env

    check(rc == 0, f"main() returns 0 (got {rc})")
    check("Completed: 12 (showing latest 2)" in out, "stdout summary reflects env cap of 2")
    progress_text = (project_dir / "progress.md").read_text(encoding="utf-8")
    check(
        progress_text.count("@tasks/2_done/") == 2,
        "progress.md Completed section has exactly 2 rows under env cap",
    )
    check(
        "Showing the latest 2 of 12 completed tasks — 10 older entries omitted"
        in progress_text,
        "progress.md footnote reflects env cap of 2",
    )


def test_check_progress_no_false_positive_on_capped(root: Path) -> None:
    print("--- check_progress.py: 0 findings on a capped progress.md (T5 / D5) ---")
    project_dir = make_project_with_done(root, 12)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = rp.main([str(project_dir)])  # default cap (10) — 2 rows omitted
    check(rc == 0, "rebuild_progress.main() returns 0")

    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = cp.main([str(project_dir)])
    out2 = buf2.getvalue()
    check(rc2 == 0, f"check_progress.main() returns 0 on a capped progress.md (got {rc2})")
    check(
        "OK: no drift, no violations, no stale tasks." in out2,
        "check_progress reports no findings for the 2 rows omitted by the cap",
    )


def main() -> int:
    print("=== rebuild_progress.py Completed-table row cap unit tests ===")
    with tempfile.TemporaryDirectory() as d1:
        test_render_capped(Path(d1))
    with tempfile.TemporaryDirectory() as d2:
        test_render_under_limit(Path(d2))
    with tempfile.TemporaryDirectory() as d3:
        test_render_unlimited(Path(d3))
    with tempfile.TemporaryDirectory() as d4:
        test_env_override(Path(d4))
    with tempfile.TemporaryDirectory() as d5:
        test_check_progress_no_false_positive_on_capped(Path(d5))

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
