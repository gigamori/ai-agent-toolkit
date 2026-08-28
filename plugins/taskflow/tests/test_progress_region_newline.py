#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_progress as cp  # noqa: E402
import rebuild_progress as rp  # noqa: E402

PASS = 0
FAIL = 0

FREE_TEXT = "# Progress: fixture\n\n## Key Decisions\n\n- keep me\n\n"
TABLE_BODY = (
    "## TODO\n\n| # | Priority | Task | Created | Link |\n"
    "|---|----------|------|---------|------|\n\n"
    "## In Progress\n\n| # | Priority | Task | Updated | Link |\n"
    "|---|----------|------|---------|------|\n\n"
    "## Completed\n\n| # | Priority | Task | Completed | Link |\n"
    "|---|----------|------|---------|------|"
)
TASK_MD = "---\npriority: HIGH\nupdated: 2026-08-27\n---\n\n# Sample done task\n"


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


def region_block() -> str:
    return f"{rp.TABLE_BEGIN}\n{TABLE_BODY}\n{rp.TABLE_END}"


def make_project(root: Path, regions: int, newline: str) -> Path:
    project = root / "fixture"
    for status in rp.TASK_STATUSES:
        (project / "tasks" / status).mkdir(parents=True)
    (project / "tasks" / "2_done" / "2026-08-27_sample.md").write_bytes(
        TASK_MD.replace("\n", newline).encode("utf-8")
    )
    text = FREE_TEXT + "\n\n".join(region_block() for _ in range(regions)) + "\n"
    (project / "progress.md").write_bytes(text.replace("\n", newline).encode("utf-8"))
    return project


def rebuild(project: Path) -> None:
    progress = rp.ensure_progress_md(project)
    rp.write_region(progress, rp.render_table_region(rp.gather_tasks(project)))


def read_bytes(project: Path) -> bytes:
    return (project / "progress.md").read_bytes()


def test_collapse_duplicate_regions(root: Path) -> None:
    content = FREE_TEXT + "\n\n".join(region_block() for _ in range(3)) + "\n"
    out = rp.replace_or_append_region(content, "NEW BODY")
    check(
        out.count(rp.TABLE_BEGIN) == 1,
        f"3 regions collapse to 1 begin marker (got {out.count(rp.TABLE_BEGIN)})",
    )
    check(
        out.count(rp.TABLE_END) == 1,
        f"3 regions collapse to 1 end marker (got {out.count(rp.TABLE_END)})",
    )
    check(out.count("NEW BODY") == 1, "collapsed file holds the new region body once")
    check("- keep me" in out, "collapsing duplicate regions keeps free text")


def test_append_when_no_region(root: Path) -> None:
    out = rp.replace_or_append_region(FREE_TEXT, "NEW BODY")
    check(out.count(rp.TABLE_BEGIN) == 1, "a file with no region gains exactly one")
    check("- keep me" in out, "appending a region keeps free text")


def test_single_region_is_replaced(root: Path) -> None:
    content = FREE_TEXT + region_block() + "\n"
    out = rp.replace_or_append_region(content, "NEW BODY")
    check(out.count(rp.TABLE_BEGIN) == 1, "a single region stays single")
    check("## Completed" not in out, "a single region is replaced, not appended to")


def test_rebuild_writes_lf(root: Path) -> None:
    project = make_project(root, regions=1, newline="\n")
    rebuild(project)
    check(
        b"\r\n" not in read_bytes(project),
        "rebuild writes progress.md with LF endings only",
    )


def test_rebuild_normalizes_crlf_file(root: Path) -> None:
    project = make_project(root, regions=1, newline="\r\n")
    rebuild(project)
    raw = read_bytes(project)
    check(b"\r\n" not in raw, "rebuild rewrites a CRLF progress.md as LF")
    check(
        raw.decode("utf-8").count(rp.TABLE_BEGIN) == 1,
        "rebuilding a CRLF progress.md does not add a region",
    )


def test_rebuild_collapses_on_disk(root: Path) -> None:
    project = make_project(root, regions=4, newline="\r\n")
    rebuild(project)
    text = read_bytes(project).decode("utf-8")
    check(
        text.count(rp.TABLE_BEGIN) == 1,
        f"rebuild collapses 4 on-disk regions to 1 (got {text.count(rp.TABLE_BEGIN)})",
    )
    check(
        text.count("@tasks/2_done/2026-08-27_sample.md") == 1,
        "a collapsed file lists each done task once",
    )
    check("- keep me" in text, "collapsing on disk keeps free text")


def test_scaffold_writes_lf(root: Path) -> None:
    project = root / "scaffold"
    project.mkdir()
    rp.ensure_progress_md(project)
    check(
        b"\r\n" not in (project / "progress.md").read_bytes(),
        "the progress.md scaffold is written with LF endings",
    )


def test_h1_repair_writes_lf(root: Path) -> None:
    project = root / "noh1"
    project.mkdir()
    (project / "progress.md").write_bytes("## Open Issues\r\n\r\n- x\r\n".encode("utf-8"))
    rp.ensure_progress_md(project)
    raw = (project / "progress.md").read_bytes()
    check(b"\r\n" not in raw, "the H1 repair rewrites progress.md with LF endings")
    check(raw.decode("utf-8").startswith("# Progress: noh1"), "the H1 repair adds an H1")


def test_check_flags_duplicate_region(root: Path) -> None:
    project = make_project(root, regions=2, newline="\n")
    result = cp.Result()
    cp.check_duplicate_table_region(project, result)
    hits = [f for f in result.findings if f.check == "duplicate_table_region"]
    check(len(hits) == 1, f"2 regions raise one duplicate_table_region finding (got {len(hits)})")
    check(
        "2" in hits[0].message if hits else False,
        "the duplicate_table_region message states how many regions were found",
    )


def test_check_clean_on_single_region(root: Path) -> None:
    project = make_project(root, regions=1, newline="\n")
    result = cp.Result()
    cp.check_duplicate_table_region(project, result)
    check(not result.findings, "a single region raises no duplicate_table_region finding")


def main() -> int:
    tests = (
        test_collapse_duplicate_regions,
        test_append_when_no_region,
        test_single_region_is_replaced,
        test_rebuild_writes_lf,
        test_rebuild_normalizes_crlf_file,
        test_rebuild_collapses_on_disk,
        test_scaffold_writes_lf,
        test_h1_repair_writes_lf,
        test_check_flags_duplicate_region,
        test_check_clean_on_single_region,
    )
    for fn in tests:
        print(fn.__name__)
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
