#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Unit tests for the context-side Completed-row cap.

The invariant under test: progress.md always holds EVERY task in tasks/2_done/,
and only view_progress.py bounds how many Completed rows reach an agent's
context. This file replaces test_rebuild_done_limit.py, which asserted the
opposite (a file-side cap) and was removed together with that behavior.

  V1  rebuild_progress.render_table_region() renders every done row and emits
      no footnote.
  V2  rebuild_progress.main() writes a progress.md whose Completed row count
      equals the tasks/2_done/ file count.
  V3  view_progress truncates to the newest N rows at the default limit and
      does NOT renumber the `#` column.
  V4  the view passes free-text sections, the TODO / In Progress tables and the
      @table markers through unchanged.
  V5  when the row count is at or below the limit, the view is byte-identical
      to progress.md.
  V6  --all and --limit 0 emit every row with no footnote.
  V7  TASKFLOW_CONTEXT_DONE_ROWS_MAX overrides the default; --limit overrides
      the env var.
  V8  a progress.md with no @table region / no Completed section / an empty
      Completed table passes through unchanged with exit 0.
  V9  a missing progress.md exits 1; a missing project dir exits 2.
  V10 check_progress.py reports no findings on an untruncated progress.md.
  V11 the view never emits the retired env name or the retired file-side
      footnote string.

rebuild_progress.py and check_progress.py both have a PEP723 header declaring
`pyyaml` as a dependency, so this test also declares it and must be run via:

    uv run --script plugins/taskflow/tests/test_progress_view_cap.py

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
import view_progress as vp  # noqa: E402

PASS = 0
FAIL = 0

RETIRED_ENV = "TASKFLOW_DONE_ROWS_MAX"
RETIRED_FOOTNOTE = "Showing the latest"


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


def make_task_file(dir_path: Path, date: str, slug: str, title: str) -> Path:
    content = (
        f"---\npriority: MID\ncreated: {date}\nupdated: {date}\n---\n\n"
        f"# {title}\n\nBody.\n"
    )
    path = dir_path / f"{date}_{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


def make_project_with_done(root: Path, count: int, extras: bool = False) -> Path:
    """A project with `count` done tasks. With extras=True it also gets one
    0_todo and one 1_in_progress task, so the view's passthrough of the other
    two tables is observable."""
    project_dir = root / "proj"
    done_dir = project_dir / "tasks" / "2_done"
    done_dir.mkdir(parents=True)
    for i in range(1, count + 1):
        date = f"2026-06-{i:02d}"
        make_task_file(done_dir, date, "done", f"Done Task {date}")
    if extras:
        todo_dir = project_dir / "tasks" / "0_todo"
        todo_dir.mkdir(parents=True)
        make_task_file(todo_dir, "2026-07-01", "todo", "Pending Task")
        wip_dir = project_dir / "tasks" / "1_in_progress"
        wip_dir.mkdir(parents=True)
        make_task_file(wip_dir, "2026-07-02", "wip", "Active Task")
    return project_dir


def build(project_dir: Path) -> str:
    """Run rebuild_progress.main() quietly; return progress.md's text."""
    with contextlib.redirect_stdout(io.StringIO()):
        rc = rp.main([str(project_dir)])
    assert rc == 0, f"rebuild_progress.main() returned {rc}"
    return (project_dir / "progress.md").read_text(encoding="utf-8")


def view(project_dir: Path, *args: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = vp.main([str(project_dir), *args])
            except SystemExit as exc:  # argparse rejects bad flag combinations
                rc = int(exc.code or 0)
    return rc, buf.getvalue()


def completed_rows(text: str) -> list[str]:
    return [
        ln for ln in text.splitlines() if ln.startswith("|") and "@tasks/2_done/" in ln
    ]


# --------------------------------------------------------------------------- V1


def test_rebuild_renders_all(root: Path) -> None:
    print("--- V1: render_table_region renders every done row, no footnote ---")
    by_status = {"0_todo": [], "1_in_progress": [], "2_done": make_done_rows(12)}
    region = rp.render_table_region(by_status)

    check(region.count("@tasks/2_done/") == 12, "all 12 Completed rows rendered")
    check("2026-06-01" in region, "oldest row (06-01) is present")
    check("2026-06-12" in region, "newest row (06-12) is present")
    check(RETIRED_FOOTNOTE not in region, "no file-side footnote is emitted")
    check(
        not hasattr(rp, "DONE_ROWS_MAX_DEFAULT")
        and not hasattr(rp, "ENV_DONE_ROWS_MAX")
        and not hasattr(rp, "resolve_done_limit"),
        "the file-side cap symbols are gone from rebuild_progress",
    )


# --------------------------------------------------------------------------- V2


def test_rebuild_file_is_complete(root: Path) -> None:
    print("--- V2: progress.md row count == tasks/2_done file count ---")
    project_dir = make_project_with_done(root, 23)
    text = build(project_dir)
    files = list((project_dir / "tasks" / "2_done").glob("*.md"))

    check(len(files) == 23, f"fixture has 23 done files (got {len(files)})")
    check(
        len(completed_rows(text)) == len(files),
        f"progress.md has {len(files)} Completed rows",
    )
    check(RETIRED_FOOTNOTE not in text, "progress.md carries no truncation footnote")


# --------------------------------------------------------------------------- V3


def test_view_truncates_without_renumbering(root: Path) -> None:
    print("--- V3: view truncates to the newest N and keeps the original # ---")
    project_dir = make_project_with_done(root, 12)
    build(project_dir)
    rc, out = view(project_dir)
    rows = completed_rows(out)

    check(rc == 0, f"view exits 0 (got {rc})")
    check(len(rows) == 10, f"default limit keeps 10 rows (got {len(rows)})")
    check("2026-06-01" not in out, "oldest row (06-01) is dropped")
    check("2026-06-03" in out, "first kept row (06-03) is present")
    check("2026-06-12" in out, "newest row (06-12) is present")
    check(rows[0].startswith("| 3 |"), f"first kept row keeps # = 3 (got {rows[0][:6]!r})")
    check(rows[-1].startswith("| 12 |"), "last row keeps # = 12")
    check(
        "_[context view] Latest 10 of 12 completed rows; "
        "2 older rows dropped." in out,
        "footnote reports latest 10 of 12 with 2 omitted",
    )
    check("proj/progress.md" in out, "footnote names the source file relatively")
    footnote = [ln for ln in out.splitlines() if ln.startswith("_[context view]")]
    check(len(footnote) == 1, "exactly one footnote line")
    check(
        not footnote[0].startswith("|") and "@tasks/" not in footnote[0],
        "footnote is invisible to check_progress's row parser",
    )


# --------------------------------------------------------------------------- V4


def test_view_passthrough(root: Path) -> None:
    print("--- V4: free text, TODO / In Progress tables and markers survive ---")
    project_dir = make_project_with_done(root, 12, extras=True)
    text = build(project_dir)
    _, out = view(project_dir)

    for marker in (vp.TABLE_BEGIN, vp.TABLE_END):
        check(out.count(marker) == 1, f"{marker} appears exactly once")
    for heading in ("## Architecture", "## Key Decisions & Policies", "## Open Issues"):
        check(heading in out, f"free-text section {heading!r} survives")
    check("@tasks/0_todo/2026-07-01_todo.md" in out, "TODO row survives untouched")
    check(
        "@tasks/1_in_progress/2026-07-02_wip.md" in out,
        "In Progress row survives untouched",
    )
    check(
        text.split(vp.TABLE_BEGIN)[0] == out.split(vp.TABLE_BEGIN)[0],
        "everything before @table:begin is byte-identical",
    )
    check(
        text.split("## Completed")[0].split(vp.TABLE_BEGIN)[1]
        == out.split("## Completed")[0].split(vp.TABLE_BEGIN)[1],
        "the TODO + In Progress region is byte-identical",
    )


# --------------------------------------------------------------------------- V5


def test_view_identity_under_limit(root: Path) -> None:
    print("--- V5: at or below the limit the view is byte-identical ---")
    for count in (3, 10):
        with tempfile.TemporaryDirectory() as d:
            project_dir = make_project_with_done(Path(d), count, extras=True)
            text = build(project_dir)
            rc, out = view(project_dir)
            check(rc == 0, f"{count} rows: view exits 0")
            check(out == text, f"{count} rows: stdout is byte-identical to progress.md")
            check("context view" not in out, f"{count} rows: no footnote")


# --------------------------------------------------------------------------- V6


def test_view_unlimited(root: Path) -> None:
    print("--- V6: --all and --limit 0 emit every row ---")
    project_dir = make_project_with_done(root, 12)
    text = build(project_dir)
    for flags in (["--all"], ["--limit", "0"]):
        rc, out = view(project_dir, *flags)
        label = " ".join(flags)
        check(rc == 0, f"{label}: exits 0")
        check(len(completed_rows(out)) == 12, f"{label}: all 12 rows emitted")
        check("context view" not in out, f"{label}: no footnote")
        check(out == text, f"{label}: byte-identical to progress.md")

    rc, _ = view(project_dir, "--all", "--limit", "5")
    check(rc == 2, f"--all with --limit is rejected with exit 2 (got {rc})")


# --------------------------------------------------------------------------- V7


def test_view_env_precedence(root: Path) -> None:
    print("--- V7: env overrides the default, --limit overrides env ---")
    project_dir = make_project_with_done(root, 12)
    build(project_dir)
    old = os.environ.get(vp.ENV_DONE_ROWS_MAX)
    try:
        os.environ[vp.ENV_DONE_ROWS_MAX] = "2"
        _, out_env = view(project_dir)
        check(len(completed_rows(out_env)) == 2, "env limit of 2 is honored")
        _, out_cli = view(project_dir, "--limit", "7")
        check(len(completed_rows(out_cli)) == 7, "--limit 7 overrides the env var")
        os.environ[vp.ENV_DONE_ROWS_MAX] = "not-a-number"
        _, out_bad = view(project_dir)
        check(
            len(completed_rows(out_bad)) == vp.DONE_ROWS_MAX_DEFAULT,
            "a non-numeric env value falls back to the default without crashing",
        )
    finally:
        if old is None:
            os.environ.pop(vp.ENV_DONE_ROWS_MAX, None)
        else:
            os.environ[vp.ENV_DONE_ROWS_MAX] = old


# --------------------------------------------------------------------------- V8


def test_view_degenerate_inputs(root: Path) -> None:
    print("--- V8: degenerate progress.md shapes pass through unchanged ---")
    header = (
        "| # | Priority | Task | Completed | Link |\n"
        "|---|----------|------|---------|------|\n"
    )
    cases = {
        "no @table region": "# Progress: proj\n\n## Architecture\n\nfree text only.\n",
        "no Completed section": (
            f"# Progress: proj\n\n{vp.TABLE_BEGIN}\n## TODO\n\n"
            f"{header}{vp.TABLE_END}\n"
        ),
        "empty Completed table": (
            f"# Progress: proj\n\n{vp.TABLE_BEGIN}\n## Completed\n\n"
            f"{header}{vp.TABLE_END}\n"
        ),
        "no trailing newline": (
            f"# Progress: proj\n\n{vp.TABLE_BEGIN}\n## Completed\n\n"
            f"{header}{vp.TABLE_END}"
        ),
    }
    for label, content in cases.items():
        with tempfile.TemporaryDirectory() as d:
            project_dir = Path(d) / "proj"
            project_dir.mkdir()
            (project_dir / "progress.md").write_text(content, encoding="utf-8")
            rc, out = view(project_dir, "--limit", "1")
            check(rc == 0, f"{label}: exits 0")
            check(out == content, f"{label}: stdout is byte-identical to the input")


# --------------------------------------------------------------------------- V9


def test_view_exit_codes(root: Path) -> None:
    print("--- V9: missing progress.md exits 1, missing project dir exits 2 ---")
    empty = root / "empty"
    empty.mkdir()
    rc_missing_file, _ = view(empty)
    check(rc_missing_file == 1, f"missing progress.md exits 1 (got {rc_missing_file})")

    rc_missing_dir, _ = view(root / "does-not-exist")
    check(rc_missing_dir == 2, f"missing project dir exits 2 (got {rc_missing_dir})")


# --------------------------------------------------------------------------- V10


def test_check_progress_clean(root: Path) -> None:
    print("--- V10: check_progress finds nothing on an untruncated progress.md ---")
    project_dir = make_project_with_done(root, 12)
    build(project_dir)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cp.main([str(project_dir)])
    out = buf.getvalue()
    check(rc == 0, f"check_progress.main() returns 0 (got {rc})")
    check(
        "OK: no drift, no violations, no stale tasks." in out,
        "check_progress reports no findings",
    )


# --------------------------------------------------------------------------- V11


def test_no_retired_strings(root: Path) -> None:
    print("--- V11: the view never emits retired cap strings ---")
    project_dir = make_project_with_done(root, 12)
    text = build(project_dir)
    _, out = view(project_dir)

    check(RETIRED_ENV not in out, "view output has no retired env name")
    check(RETIRED_FOOTNOTE not in out, "view output has no retired footnote string")
    check(RETIRED_ENV not in text, "progress.md has no retired env name")
    check(RETIRED_FOOTNOTE not in text, "progress.md has no retired footnote string")


def main() -> int:
    print("=== context-side Completed-row cap unit tests ===")
    tests = (
        test_rebuild_renders_all,
        test_rebuild_file_is_complete,
        test_view_truncates_without_renumbering,
        test_view_passthrough,
        test_view_identity_under_limit,
        test_view_unlimited,
        test_view_env_precedence,
        test_view_degenerate_inputs,
        test_view_exit_codes,
        test_check_progress_clean,
        test_no_retired_strings,
    )
    for fn in tests:
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
