#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
r"""Unit tests for scripts/check_progress.py check #11: notes index row arity.

Covers
_projects/harness-taskflow/project-notes/specs/review-2026-08-19-fixes.md §8 A-4
(the enforcement gap §4.9 residual 2 records: F-4 fixed the reminder TEXT, i.e.
likelihood steering, but nothing mechanically rejected a malformed row, so
`/progress check` stayed silent on one).

check_notes_index_consistency now reports a `notes_index_arity` violation for
any project-notes/index.md DATA row whose effective column count is not 4
(File | Description | Tags | Updated).

  A1  a 3-column row IS reported (the real defect class: a missing `Updated`).
  A2  a well-formed 4-column row is NOT reported.
  A3  a 4-column row whose Description contains an escaped pipe (`\|`) is NOT
      reported. This is the A-3 -> A-4 ordering constraint's own regression
      pin: before A-3 taught parse_notes_index_rows to split on unescaped "|"
      only, this row parsed into 5 cells and an arity check would have
      misreported this repo's own healthy index from its first run. The row is
      the verbatim `investigations/llm-wiki-integration-survey.md` row of
      _projects/harness-taskflow/project-notes/index.md (inlined — the test
      never reads the live gitignored file).
  A4  a 4-column row with ONE trailing space after the closing "|" is NOT
      reported. str.strip("|") is a character strip and cannot remove the
      closing pipe when whitespace follows it, so such a row parses into 5
      cells with an empty last one while rendering identically; the predicate
      drops at most one trailing empty cell before counting.
  A5  a 5-column row IS reported (over-arity, not just under-arity).
  A6  header ("| File | ... |") and separator ("|---|---|---|---|") rows are
      never reported — parse_notes_index_rows drops them before the check sees
      them, so no arity-side exclusion logic exists or is needed.
  A7  the finding's labels: check == "notes_index_arity", severity ==
      "violation", path == the index.md path, message names the row's File cell
      and the observed column count.
  A8  end-to-end through check_progress.main(): a malformed row drives a
      non-zero exit and prints the check name and the VIOLATION label; a clean
      index drives a zero exit.

check_progress.py has a PEP723 header declaring `pyyaml` as a dependency, so
this test also declares it and must be run via:

    uv run --script plugins/taskflow/tests/test_check_progress_notes_arity.py

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

HEADER = "| File | Description | Tags | Updated |"
SEPARATOR = "|------|-------------|------|---------|"

ROW_4 = "| specs/good.md | A well-formed row. | spec, ok | 2026-08-19 |"
ROW_3 = "| investigations/missing-updated.md | No Updated cell. | stop-hook, procedure |"
ROW_5 = "| specs/too-wide.md | Five | cells | here | 2026-08-19 |"
# One trailing space after the closing pipe: invisible in the rendered table.
ROW_4_TRAILING_SPACE = ROW_4 + " "

# Verbatim from _projects/harness-taskflow/project-notes/index.md — the row
# whose Description carries an escaped pipe (`wiki:on\|off`). Under the naive
# split this parsed into 5 cells.
ROW_4_ESCAPED_PIPE = (
    r"| investigations/llm-wiki-integration-survey.md "
    r"| taskflow×llm-wiki併用調査+Phase1設計: pjスコープ解決は実装済・session-aware化・"
    r"`wiki:on\|off`トグル・併存ガイド注入・Phase2不採用 "
    r"| llm-wiki, pj, integration, hook, wiki-toggle, design | 2026-07-03 |"
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


def make_project(root: Path, rows: list[str]) -> Path:
    """A project whose project-notes/index.md holds `rows`, with every listed
    note file actually present (so the only findings possible from check #2 are
    arity ones — no unregistered / missing noise)."""
    project_dir = root / "proj"
    notes_dir = project_dir / "project-notes"
    notes_dir.mkdir(parents=True)
    body = "\n".join([HEADER, SEPARATOR, *rows]) + "\n"
    (notes_dir / "index.md").write_text(
        "# project-notes index\n\n" + body, encoding="utf-8"
    )
    for row in rows:
        rel = row.strip("|").split("|")[0].strip()
        if not rel.endswith(".md"):
            continue
        target = notes_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# note\n\nBody.\n", encoding="utf-8")
    return project_dir


def arity_findings(project_dir: Path) -> list[cp.Finding]:
    result = cp.Result()
    cp.check_notes_index_consistency(project_dir, result)
    return [f for f in result.findings if f.check == "notes_index_arity"]


def other_findings(project_dir: Path) -> list[cp.Finding]:
    result = cp.Result()
    cp.check_notes_index_consistency(project_dir, result)
    return [f for f in result.findings if f.check != "notes_index_arity"]


def test_three_column_row_reported(root: Path) -> None:
    print("--- A1: a 3-column row is reported ---")
    project_dir = make_project(root, [ROW_4, ROW_3])
    found = arity_findings(project_dir)
    check(len(found) == 1, f"exactly one arity finding (got {len(found)}: "
                           f"{[f.message for f in found]})")
    if found:
        check("investigations/missing-updated.md" in found[0].message,
              "the finding names the offending row's File cell")
        check("3 columns" in found[0].message,
              f"the finding names the observed column count (got {found[0].message!r})")


def test_four_column_row_not_reported(root: Path) -> None:
    print("--- A2: a well-formed 4-column row is not reported ---")
    project_dir = make_project(root, [ROW_4])
    found = arity_findings(project_dir)
    check(len(found) == 0, f"no arity finding (got {[f.message for f in found]})")
    check(len(other_findings(project_dir)) == 0,
          "and no other notes_index finding either (fixture is clean)")


def test_escaped_pipe_row_not_reported(root: Path) -> None:
    print("--- A3: a 4-column row with an escaped pipe is not reported ---")
    project_dir = make_project(root, [ROW_4_ESCAPED_PIPE])
    rows = cp.parse_notes_index_rows(
        (project_dir / "project-notes" / "index.md").read_text(encoding="utf-8")
    )
    check(len(rows) == 1, f"exactly one data row parsed (got {len(rows)})")
    if rows:
        check(len(rows[0]["cells"]) == 4,
              f"the escaped-pipe row parses into 4 cells (got "
              f"{len(rows[0]['cells'])}) — A-3 is in effect")
    found = arity_findings(project_dir)
    check(len(found) == 0,
          f"no arity finding for the escaped-pipe row (got "
          f"{[f.message for f in found]})")


def test_trailing_space_row_not_reported(root: Path) -> None:
    print("--- A4: a 4-column row with a trailing space is not reported ---")
    project_dir = make_project(root, [ROW_4_TRAILING_SPACE])
    rows = cp.parse_notes_index_rows(
        (project_dir / "project-notes" / "index.md").read_text(encoding="utf-8")
    )
    check(len(rows) == 1 and len(rows[0]["cells"]) == 5,
          "the parser still yields 5 cells for it (the artefact is real, and "
          f"is NOT fixed in parse_notes_index_rows): got "
          f"{[len(r['cells']) for r in rows]}")
    found = arity_findings(project_dir)
    check(len(found) == 0,
          f"no arity finding (the predicate drops one trailing empty cell): "
          f"got {[f.message for f in found]}")


def test_five_column_row_reported(root: Path) -> None:
    print("--- A5: a 5-column row is reported ---")
    project_dir = make_project(root, [ROW_5])
    found = arity_findings(project_dir)
    check(len(found) == 1, f"exactly one arity finding (got {len(found)})")
    if found:
        check("5 columns" in found[0].message,
              f"the finding names 5 columns (got {found[0].message!r})")


def test_header_and_separator_excluded(root: Path) -> None:
    print("--- A6: header and separator rows are never reported ---")
    project_dir = make_project(root, [ROW_4])
    # The fixture already contains both; a second separator with a different
    # dash count pins that the exclusion is not arity-shaped.
    index_md = project_dir / "project-notes" / "index.md"
    index_md.write_text(
        index_md.read_text(encoding="utf-8") + "|---|---|---|\n", encoding="utf-8"
    )
    found = arity_findings(project_dir)
    check(len(found) == 0,
          f"no arity finding from header/separator rows (got "
          f"{[f.message for f in found]})")


def test_finding_labels(root: Path) -> None:
    print("--- A7: finding check / severity / path labels ---")
    project_dir = make_project(root, [ROW_3])
    found = arity_findings(project_dir)
    check(len(found) == 1, f"exactly one arity finding (got {len(found)})")
    if not found:
        return
    f = found[0]
    check(f.check == "notes_index_arity", f"check is 'notes_index_arity' (got {f.check!r})")
    check(f.severity == "violation", f"severity is 'violation' (got {f.severity!r})")
    check(f.severity in cp.SEVERITY_ORDER,
          "severity is one of the module's declared severities (sorts and "
          "labels correctly)")
    expected_path = str(project_dir / "project-notes" / "index.md")
    check(f.path == expected_path, f"path is the index.md path (got {f.path!r})")


def test_main_end_to_end(root: Path) -> None:
    print("--- A8: main() exit code and stdout ---")
    bad_dir = make_project(root / "bad", [ROW_3])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cp.main([str(bad_dir)])
    out = buf.getvalue()
    check(rc == 1, f"main() returns 1 on a malformed index (got {rc})")
    check("notes_index_arity" in out, "stdout mentions the notes_index_arity check")
    check("VIOLATION" in out, "stdout prints the VIOLATION severity label")

    good_dir = make_project(root / "good", [ROW_4, ROW_4_ESCAPED_PIPE,
                                            ROW_4_TRAILING_SPACE])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cp.main([str(good_dir)])
    out = buf.getvalue()
    check(rc == 0, f"main() returns 0 on a clean index (got {rc})")
    check("OK: no drift, no violations, no stale tasks." in out,
          "stdout prints the OK message")


def main() -> int:
    print("=== check_progress.py check #11 (notes index arity) unit tests ===")
    tests = (
        test_three_column_row_reported,
        test_four_column_row_not_reported,
        test_escaped_pipe_row_not_reported,
        test_trailing_space_row_not_reported,
        test_five_column_row_reported,
        test_header_and_separator_excluded,
        test_finding_labels,
        test_main_end_to_end,
    )
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
