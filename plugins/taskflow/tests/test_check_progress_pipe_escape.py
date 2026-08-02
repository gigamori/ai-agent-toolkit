#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
r"""Unit tests for check #6 (summary_h1) with a pipe character in the task H1.

Regression for a pre-existing bug surfaced by the 2026-08-03 context-side
Completed-row cap fix (see
_projects/harness-taskflow/project-notes/specs/context-side-done-rows-cap.md):
it had been hidden by the old file-side cap dropping the affected row, and only
became visible once progress.md started listing every Completed task again.

rebuild_progress.py::escape_cell escapes a literal "|" in a table cell as "\|"
so it is not mistaken for a column separator. check_progress.py's
parse_progress_table_rows used to split each row on every "|" character
unconditionally, which:
  1. split an escaped pipe into two spurious extra cells, and
  2. discarded the "|" character itself (consumed as the split delimiter),
so a task whose H1 contains "|" (e.g. "wiki:on|off") always false-positived as
summary_h1 drift, no matter how fresh the table was.

  T1  a row built by rebuild_progress.py for a task whose H1 contains "|"
      parses back into exactly 5 cells with the pipe restored.
  T2  check_progress.py reports no findings for that project (no false
      positive from the pipe).
  T3  the check still catches REAL drift on a pipe-bearing H1 (a stale row
      after the task's H1 changes) — the fix must not neuter the check.

check_progress.py and rebuild_progress.py both have a PEP723 header declaring
`pyyaml` as a dependency, so this test also declares it and must be run via:

    uv run --script plugins/taskflow/tests/test_check_progress_pipe_escape.py

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
import rebuild_progress as rp  # noqa: E402

PASS = 0
FAIL = 0

PIPE_H1 = "llm-wiki x taskflow Phase 1: session-aware pj + `wiki:on|off` toggle"


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


def make_project_with_pipe_task(root: Path, h1: str) -> Path:
    project_dir = root / "proj"
    done_dir = project_dir / "tasks" / "2_done"
    done_dir.mkdir(parents=True)
    content = f"---\npriority: MID\ncreated: 2026-07-03\nupdated: 2026-07-03\n---\n\n# {h1}\n\nBody.\n"
    (done_dir / "2026-07-03_pipe-task.md").write_text(content, encoding="utf-8")
    return project_dir


def build(project_dir: Path) -> str:
    with contextlib.redirect_stdout(io.StringIO()):
        rc = rp.main([str(project_dir)])
    assert rc == 0, f"rebuild_progress.main() returned {rc}"
    return (project_dir / "progress.md").read_text(encoding="utf-8")


def test_row_parses_back_with_pipe_restored(root: Path) -> None:
    print("--- T1: a rebuilt row with an escaped pipe parses into 5 cells ---")
    project_dir = make_project_with_pipe_task(root, PIPE_H1)
    text = build(project_dir)

    check("\\|" in text, "the rebuilt table escapes the pipe as \\| on disk")

    rows = cp.parse_progress_table_rows(text)
    check(len(rows) == 1, f"exactly one row parsed (got {len(rows)})")
    if not rows:
        return
    cells = rows[0]["cells"]
    check(len(cells) == 5, f"row splits into exactly 5 cells (got {len(cells)}: {cells!r})")
    check(PIPE_H1 in " ".join(cells), "the unescaped H1 (with a literal |) is present in the cells")


def test_no_false_positive(root: Path) -> None:
    print("--- T2: check_progress reports no findings for a pipe-bearing H1 ---")
    project_dir = make_project_with_pipe_task(root, PIPE_H1)
    build(project_dir)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cp.main([str(project_dir)])
    out = buf.getvalue()
    check(rc == 0, f"check_progress.main() returns 0 (got {rc})")
    check(
        "OK: no drift, no violations, no stale tasks." in out,
        "no summary_h1 false positive from the escaped pipe",
    )


def test_real_drift_still_caught(root: Path) -> None:
    print("--- T3: a genuinely stale pipe-bearing row is still flagged ---")
    project_dir = make_project_with_pipe_task(root, PIPE_H1)
    build(project_dir)

    # Change the task's H1 without rebuilding — progress.md's row is now stale.
    task_path = project_dir / "tasks" / "2_done" / "2026-07-03_pipe-task.md"
    stale_content = task_path.read_text(encoding="utf-8").replace(
        PIPE_H1, "a completely different title with|a pipe too"
    )
    task_path.write_text(stale_content, encoding="utf-8")

    result = cp.Result()
    cp.check_progress_summary_h1_sync(project_dir, result)
    h1_findings = [f for f in result.findings if f.check == "summary_h1"]
    check(len(h1_findings) == 1, f"exactly one summary_h1 finding (got {len(h1_findings)})")


def main() -> int:
    print("=== check_progress.py check #6 pipe-escape regression tests ===")
    with tempfile.TemporaryDirectory() as d1:
        test_row_parses_back_with_pipe_restored(Path(d1))
    with tempfile.TemporaryDirectory() as d2:
        test_no_false_positive(Path(d2))
    with tempfile.TemporaryDirectory() as d3:
        test_real_drift_still_caught(Path(d3))

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
