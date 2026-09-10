#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
from __future__ import annotations

import contextlib
import datetime
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import backfill_notes_updated as bf  # noqa: E402

PASS = 0
FAIL = 0

STAMP = datetime.datetime(2026, 3, 4, 12, 0, 0).timestamp()
DATE = "2026-03-04"


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


def make_project(root: Path) -> Path:
    project_dir = root / "_projects" / "proj"
    notes = project_dir / "project-notes"
    files = {
        "specs/has.md": b"---\ncreated: 2026-01-01\nupdated: 2026-01-02\n---\n\n# A\n",
        "specs/missing.md": b"---\ncreated: 2026-01-01\ntags: [x]\n---\n\n# B\n",
        "specs/nofm.md": b"# C\n\nbody\n",
        "specs/crlf.md": b"# D\r\n\r\nbody\r\n",
        "specs/crlf-fm.md": b"---\r\ncreated: 2026-01-01\r\n---\r\n\r\n# E\r\n",
        "_archive/old.md": b"# F\n",
    }
    for rel, data in files.items():
        p = notes / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (STAMP, STAMP))
    return project_dir


def run(project_dir: Path, *flags: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = bf.main([str(project_dir), *flags])
    return rc, buf.getvalue()


def read(project_dir: Path, rel: str) -> bytes:
    return (project_dir / "project-notes" / rel).read_bytes()


def test_dry_run_changes_nothing(root: Path) -> None:
    print("--- dry-run ---")
    project_dir = make_project(root)
    before = {rel: read(project_dir, rel) for rel in ("specs/missing.md", "specs/nofm.md")}
    rc, out = run(project_dir)
    check(rc == 0, f"dry-run exits 0 (got {rc})")
    check("DRY-RUN: updated_added=2 frontmatter_added=2 untouched=1 unreadable=0" in out,
          "summary counts one untouched, two appended, two new blocks, archive skipped")
    check(f"+updated: {DATE}" in out, "diff shows the mtime date being added")
    check(all(read(project_dir, r) == b for r, b in before.items()),
          "dry-run leaves every file byte-identical")


def test_apply_and_idempotence(root: Path) -> None:
    print("--- apply ---")
    project_dir = make_project(root)
    rc, out = run(project_dir, "--apply")
    check(rc == 0, f"apply exits 0 (got {rc})")
    check(read(project_dir, "specs/has.md").endswith(b"updated: 2026-01-02\n---\n\n# A\n"),
          "an existing updated: value is untouched")
    check(read(project_dir, "specs/missing.md")
          == f"---\ncreated: 2026-01-01\ntags: [x]\nupdated: {DATE}\n---\n\n# B\n".encode(),
          "updated: is appended as the last frontmatter line")
    check(read(project_dir, "specs/nofm.md") == f"---\nupdated: {DATE}\n---\n\n# C\n\nbody\n".encode(),
          "a note without frontmatter gets a block holding only updated:")
    check(read(project_dir, "specs/crlf.md") == f"---\r\nupdated: {DATE}\r\n---\r\n\r\n# D\r\n\r\nbody\r\n".encode(),
          "a CRLF note gets a CRLF block and stays CRLF")
    check(read(project_dir, "specs/crlf-fm.md") == f"---\r\ncreated: 2026-01-01\r\nupdated: {DATE}\r\n---\r\n\r\n# E\r\n".encode(),
          "a CRLF frontmatter gets a CRLF updated: line")
    check(read(project_dir, "_archive/old.md") == b"# F\n", "_archive/ is never touched")
    rc2, out2 = run(project_dir, "--apply")
    check("APPLIED: updated_added=0 frontmatter_added=0 untouched=5 unreadable=0" in out2,
          "a second apply changes nothing")


def test_missing_project(root: Path) -> None:
    print("--- bad argument ---")
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = bf.main([str(root / "nope")])
    check(rc == 2, f"a project without project-notes/ exits 2 (got {rc})")


def main() -> int:
    print("=== backfill_notes_updated.py unit tests ===")
    for t in (test_dry_run_changes_nothing, test_apply_and_idempotence, test_missing_project):
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
