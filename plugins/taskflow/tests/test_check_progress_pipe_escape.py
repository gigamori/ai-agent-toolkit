#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
r"""Unit tests for checks #6 and #2 — pipe-escape in table-row parsing.

Both of check_progress.py's markdown-table parsers must split a row on
*unescaped* "|" only and then unescape each cell. This file pins that rule on
both sides: check #6 (summary_h1) via parse_progress_table_rows, and check #2
(notes index) via parse_notes_index_rows.

== check #6 / parse_progress_table_rows ==

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

== check #2 / parse_notes_index_rows ==

parse_notes_index_rows carried the same naive `stripped.split("|")` long after
its sibling was fixed, so a project-notes/index.md row whose Description or
Tags cell contains an escaped pipe split into 5 or 6 cells instead of 4, with
the pipe character itself consumed as a delimiter. It stayed latent because
check_notes_index_consistency (and view_progress.py::build_notes_summary) read
only cells[0], which is left of the damage.

  T4  the two real escaped-pipe rows of
      _projects/harness-taskflow/project-notes/index.md (verbatim, inlined —
      the test never reads the live gitignored file) each parse into exactly
      4 cells with the literal "|" restored.
  T5  cells[0] is still the plain note rel path for those rows — the field
      today's consumers actually read must not regress.
  T6  check_notes_index_consistency reports no findings for an index whose
      rows carry escaped pipes (no false positive at the consumer).

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

# The two rows of _projects/harness-taskflow/project-notes/index.md that the
# naive parser mis-split (lines 5 and 27 as of 2026-08-19), copied verbatim and
# frozen here. Do NOT read the live file: _projects/ is gitignored and edited
# by ordinary work, so it is not a stable fixture.
#   line 5  — two escaped pipes in Description (`fullUuid\|\|shortId`): the
#             naive split produced 6 cells.
#   line 27 — one escaped pipe in Description (`wiki:on\|off`): 5 cells.
NOTES_ROW_2_PIPES = (
    r"| procedures/sibling-handoff-taskflow-round-binding-kanban-2026-08-09.md "
    r"| pi-studio(P型) 宛: 1タスクに同一sidの[s:]行が複数付く/他プロジェクトのsidも入る"
    r"影響の確認依頼。unassigned一覧の件は裁定済で対象外と明記。**報告済: non-applicable**"
    r"(`kanban-data-provider.ts:loadTasks` に既存 dedupe あり＝`fullUuid\|\|shortId` "
    r"キー・最新タイムスタンプ勝ち、`referencedUuids` は Set、`(r{N})` は自由文字列扱い) "
    r"| taskflow, sibling-handoff, pi-studio, kanban, round-binding, reported, non-applicable "
    r"| 2026-08-11 |"
)
NOTES_ROW_1_PIPE = (
    r"| investigations/llm-wiki-integration-survey.md "
    r"| taskflow×llm-wiki併用調査+Phase1設計: pjスコープ解決は実装済・session-aware化・"
    r"`wiki:on\|off`トグル・併存ガイド注入・Phase2不採用 "
    r"| llm-wiki, pj, integration, hook, wiki-toggle, design | 2026-07-03 |"
)
NOTES_INDEX_MD = (
    "# project-notes index\n"
    "\n"
    "| File | Description | Tags | Updated |\n"
    "|------|-------------|------|---------|\n"
    f"{NOTES_ROW_2_PIPES}\n"
    f"{NOTES_ROW_1_PIPE}\n"
)
NOTES_ROW_FILES = (
    "procedures/sibling-handoff-taskflow-round-binding-kanban-2026-08-09.md",
    "investigations/llm-wiki-integration-survey.md",
)


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


def make_project_with_notes_index(root: Path) -> Path:
    """Materialize a project whose project-notes/index.md is NOTES_INDEX_MD and
    whose note files are exactly the ones that index registers."""
    project_dir = root / "proj"
    notes_dir = project_dir / "project-notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "index.md").write_text(NOTES_INDEX_MD, encoding="utf-8")
    for rel in NOTES_ROW_FILES:
        note = notes_dir / rel
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("# note\n\nBody.\n", encoding="utf-8")
    return project_dir


def test_notes_index_row_arity_with_escaped_pipes() -> None:
    print("--- T4: notes index rows with escaped pipes parse into 4 cells ---")
    rows = cp.parse_notes_index_rows(NOTES_INDEX_MD)
    check(len(rows) == 2, f"exactly two data rows parsed (got {len(rows)})")
    if len(rows) != 2:
        return

    two, one = rows[0]["cells"], rows[1]["cells"]
    check(len(two) == 4, f"the 2-escaped-pipe row splits into 4 cells (got {len(two)}: {two!r})")
    check(len(one) == 4, f"the 1-escaped-pipe row splits into 4 cells (got {len(one)}: {one!r})")
    check(
        "`fullUuid||shortId`" in " ".join(two),
        "the 2-escaped-pipe row restores the literal pipes (fullUuid||shortId)",
    )
    check(
        "`wiki:on|off`" in " ".join(one),
        "the 1-escaped-pipe row restores the literal pipe (wiki:on|off)",
    )
    check(
        all("\\" not in c for c in two + one),
        "no backslash survives in any cell (neither an escape nor its remnant)",
    )


def test_notes_index_file_cell_unaffected() -> None:
    print("--- T5: cells[0] still yields the plain note rel path ---")
    rows = cp.parse_notes_index_rows(NOTES_INDEX_MD)
    files = tuple(r["file"] for r in rows)
    check(
        files == NOTES_ROW_FILES,
        f"the File cell of each row is the note rel path (got {files!r})",
    )


def test_notes_index_no_false_positive(root: Path) -> None:
    print("--- T6: check_notes_index_consistency is clean on escaped-pipe rows ---")
    project_dir = make_project_with_notes_index(root)
    result = cp.Result()
    cp.check_notes_index_consistency(project_dir, result)
    findings = [f for f in result.findings if f.check == "notes_index"]
    check(
        not findings,
        f"no notes_index findings (got {[f.message for f in findings]!r})",
    )


def main() -> int:
    print("=== check_progress.py checks #6 / #2 pipe-escape regression tests ===")
    with tempfile.TemporaryDirectory() as d1:
        test_row_parses_back_with_pipe_restored(Path(d1))
    with tempfile.TemporaryDirectory() as d2:
        test_no_false_positive(Path(d2))
    with tempfile.TemporaryDirectory() as d3:
        test_real_drift_still_caught(Path(d3))
    test_notes_index_row_arity_with_escaped_pipes()
    test_notes_index_file_cell_unaffected()
    with tempfile.TemporaryDirectory() as d4:
        test_notes_index_no_false_positive(Path(d4))

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
