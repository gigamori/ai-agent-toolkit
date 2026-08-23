#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import generate_kanban as gk  # noqa: E402

PASS = 0
FAIL = 0

HEADER = "| Project | Description | Target |"
SEP = "|---|---|---|"

CASES: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("C01 header row is not a project",
     HEADER, []),
    ("C02 separator |---|---|---|",
     SEP, []),
    ("C03 separator with padding (session_init.py bootstrap header)",
     "|---------|-------------|--------|", []),
    ("C04 separator with alignment colons",
     "|:---|---:|:---:|", []),
    ("C05 plain row",
     "| alpha | an alpha project | alpha/ |", [("alpha", "an alpha project")]),
    ("C06 R2: description containing --- is kept",
     "| alpha | before --- after | alpha/ |", [("alpha", "before --- after")]),
    ("C07 R2: description that is only a dash run is kept",
     "| alpha | --- | alpha/ |", [("alpha", "---")]),
    ("C08 R1: cell 0 that is entirely a link yields the label",
     "| [ alpha ](alpha/index.md) | d | t |", [("alpha", "d")]),
    ("C09 R1: link label is stripped of surrounding space",
     "| [alpha](alpha/index.md) | d | t |", [("alpha", "d")]),
    ("C10 R1: a cell that merely contains a link is NOT unwrapped",
     "| see [alpha](alpha/index.md) | d | t |", [("see [alpha](alpha/index.md)", "d")]),
    ("C11 non-table lines are ignored",
     "# Projects\n\nsome prose\n", []),
    ("C12 row with only one cell yields an empty description",
     "| alpha |", [("alpha", "")]),
    ("C13 empty cell 0 is dropped",
     "|  | d | t |", []),
    ("C14 rule 2: ||alpha| has an empty cell 0 and is dropped",
     "|| alpha | d |", []),
    ("C15 missing trailing pipe still parses",
     "| alpha | d", [("alpha", "d")]),
    ("C16 empty file",
     "", []),
    ("C17 duplicate names are both returned (dedup is load_projects' job)",
     f"{HEADER}\n{SEP}\n| alpha | first | t |\n| alpha | second | t |",
     [("alpha", "first"), ("alpha", "second")]),
    ("C18 a link in the Description column is NOT unwrapped",
     "| alpha | [d](d.md) | t |", [("alpha", "[d](d.md)")]),
    ("C19 R4: the shape of the live _projects/index.md is unchanged",
     f"# Projects\n\n{HEADER}\n{SEP}\n| alpha | a desc | alpha/ |\n| beta | b desc | beta/ |",
     [("alpha", "a desc"), ("beta", "b desc")]),
]


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


def test_cases(tmp: Path) -> None:
    for case_id, content, expected in CASES:
        path = tmp / "index.md"
        path.write_text(content, encoding="utf-8")
        actual = gk.parse_index(path)
        check(actual == expected, f"{case_id} -> {expected!r}" if actual == expected
              else f"{case_id}: expected {expected!r}, got {actual!r}")


def test_missing_file(tmp: Path) -> None:
    check(gk.parse_index(tmp / "does-not-exist.md") == [],
          "C20 missing file yields no projects")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="taskflow-parse-index-"))
    try:
        print("parse_index case table")
        test_cases(tmp)
        test_missing_file(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
