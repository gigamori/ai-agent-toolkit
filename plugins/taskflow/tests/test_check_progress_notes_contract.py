#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
from __future__ import annotations

import contextlib
import io
import json
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


def make_workspace(root: Path) -> tuple[Path, Path]:
    ws = root / "ws"
    project_dir = ws / "_projects" / "proj"
    notes = project_dir / "project-notes"
    for cat in ("specs", "investigations", "checks", "procedures", "backlog", "_archive"):
        (notes / cat).mkdir(parents=True)
    (notes / "index.md").write_text(
        "| File | Description | Tags | Updated |\n|---|---|---|---|\n", encoding="utf-8"
    )
    (ws / "src").mkdir()
    (ws / "src" / "mod.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    return ws, project_dir


def write_note(project_dir: Path, rel: str, frontmatter: str | None, body: str = "# T\n") -> Path:
    p = project_dir / "project-notes" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    text = body if frontmatter is None else f"---\n{frontmatter}\n---\n\n{body}"
    p.write_text(text, encoding="utf-8")
    return p


def register(project_dir: Path, rel: str) -> None:
    idx = project_dir / "project-notes" / "index.md"
    with idx.open("a", encoding="utf-8") as fh:
        fh.write(f"| {rel} | d | t | 2026-01-01 |\n")


def write_snapshot(ws: Path, key: str) -> Path:
    snap = ws / ".lsp-cache" / "snapshot.json"
    snap.parent.mkdir(parents=True)
    snap.write_text(json.dumps({
        "root": str(ws),
        "languages": {},
        "files": {key: {"lang": "python", "mtime": 0, "size": 0, "symbols": [
            {"name": "foo", "kind": 12},
            {"name": "run", "kind": 6, "container": "Klass"},
        ]}},
    }), encoding="utf-8")
    return snap


def findings_of(result: cp.Result, name: str) -> list[cp.Finding]:
    return [f for f in result.findings if f.check == name]


def test_nested_dir(root: Path) -> None:
    print("--- notes_nested_dir ---")
    ws, project_dir = make_workspace(root)
    notes = project_dir / "project-notes"
    (notes / "specs" / "run-2026").mkdir()
    (notes / "_archive" / "old-run").mkdir()
    (notes / ".qmd").mkdir()
    result = cp.Result()
    cp.check_notes_nested_dir(project_dir, result)
    hits = findings_of(result, "notes_nested_dir")
    check(len(hits) == 1, f"one nested-dir finding for specs/run-2026 (got {len(hits)})")
    if hits:
        check(hits[0].severity == "violation", f"severity is violation (got {hits[0].severity!r})")
        check("specs/run-2026" in hits[0].message.replace("\\", "/"),
              "message names the nested directory")
    check(not any("_archive" in f.path for f in hits),
          "a directory under _archive/ is not reported")
    check(not any(".qmd" in f.path for f in hits),
          "project-notes/.qmd/ (outside the 5 categories) is not reported")


def test_index_attachment_rows(root: Path) -> None:
    print("--- notes_index: attachment rows ---")
    ws, project_dir = make_workspace(root)
    notes = project_dir / "project-notes"
    (notes / "checks" / "setup.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    write_note(project_dir, "checks/c.md", "updated: 2026-01-01")
    for rel in ("checks/c.md", "checks/setup.sh", "checks/gone.md", "checks/gone.sh"):
        register(project_dir, rel)
    result = cp.Result()
    cp.check_notes_index_consistency(project_dir, result)
    missing = sorted(f.message for f in result.findings if f.check == "notes_index" and "does not exist" in f.message)
    check(len(missing) == 2 and all("gone." in m for m in missing),
          f"an existing non-.md attachment row is not drift; a vanished .md or attachment row is (got {missing})")
    check(not any("setup.sh" in f.message for f in result.findings),
          "the attachment is neither listed as missing nor as unregistered")


def test_summary_length_and_coverage(root: Path) -> None:
    print("--- notes_summary_length + coverage footer ---")
    ws, project_dir = make_workspace(root)
    write_note(project_dir, "specs/over.md", "summary: " + "あ" * 501)
    write_note(project_dir, "specs/edge.md", "summary: " + "a" * 500)
    write_note(project_dir, "specs/folded.md", "summary: >\n  first line\n  second line")
    write_note(project_dir, "specs/none.md", "updated: 2026-01-01")
    write_note(project_dir, "specs/nofm.md", None)
    write_note(project_dir, "specs/blank.md", "summary: ''")
    write_note(project_dir, "_archive/old.md", "updated: 2026-01-01")
    for rel in ("specs/over.md", "specs/edge.md", "specs/folded.md", "specs/none.md",
                "specs/nofm.md", "specs/blank.md", "_archive/old.md"):
        register(project_dir, rel)
    result = cp.Result()
    cp.check_notes_summary_length(project_dir, result)
    hits = findings_of(result, "notes_summary_length")
    check(len(hits) == 1, f"only the 501-code-point summary is flagged (got {len(hits)})")
    if hits:
        check(hits[0].path.endswith("over.md"), "flagged path is specs/over.md")
        check("501" in hits[0].message, "message carries the measured length")
    without, total = cp.notes_summary_coverage(project_dir)
    check(total == 6, f"coverage total excludes _archive/ (got {total})")
    check(without == 3, f"missing, no-frontmatter and blank summaries count as absent (got {without})")


def test_anchor_dangling(root: Path) -> None:
    print("--- notes_anchor_dangling ---")
    ws, project_dir = make_workspace(root)
    write_note(project_dir, "specs/ok-path.md", "anchors: [src/mod.py]")
    write_note(project_dir, "specs/missing-path.md", "anchors: [src/gone.py]")
    write_note(project_dir, "specs/backslash.md", "anchors: ['src\\mod.py']")
    write_note(project_dir, "specs/not-list.md", "anchors: src/mod.py")
    write_note(project_dir, "specs/sym.md", "anchors: [src/mod.py::foo, src/mod.py::Klass.run, src/mod.py::nope]")
    write_note(project_dir, "_archive/old.md", "anchors: [src/gone.py]")
    for rel in ("specs/ok-path.md", "specs/missing-path.md", "specs/backslash.md",
                "specs/not-list.md", "specs/sym.md", "_archive/old.md"):
        register(project_dir, rel)

    result = cp.Result()
    unchecked = cp.check_notes_anchor_dangling(project_dir, result, None)
    hits = findings_of(result, "notes_anchor_dangling")
    paths = {Path(f.path).name for f in hits}
    check("ok-path.md" not in paths, "an existing repo-relative path is not reported")
    check("backslash.md" not in paths, "a backslash path is normalised before the existence test")
    check("missing-path.md" in paths, "a missing path is reported")
    check("not-list.md" in paths, "a scalar anchors: value is reported")
    check("old.md" not in paths, "anchors under _archive/ are not checked")
    check(unchecked == 3, f"without a snapshot every path::symbol anchor is left unchecked (got {unchecked})")
    check(all(f.severity == cp.ANCHOR_DANGLING_SEVERITY for f in hits),
          "severity follows ANCHOR_DANGLING_SEVERITY")

    snap = write_snapshot(ws, "src\\mod.py")
    result = cp.Result()
    unchecked = cp.check_notes_anchor_dangling(project_dir, result, snap)
    hits = findings_of(result, "notes_anchor_dangling")
    sym_msgs = [f.message for f in hits if f.path.endswith("sym.md")]
    check(unchecked == 0, f"with a snapshot no symbol anchor is left unchecked (got {unchecked})")
    check(len(sym_msgs) == 1, f"only the unknown symbol is reported (got {len(sym_msgs)})")
    check(any("nope" in m for m in sym_msgs), "message names the unresolved symbol")
    check(not any("Klass.run" in m for m in sym_msgs), "Container.method resolves via the container field")


def test_main_footer_and_flag(root: Path) -> None:
    print("--- main(): footer lines and --lsp-snapshot ---")
    ws, project_dir = make_workspace(root)
    write_note(project_dir, "specs/a.md", "summary: s\nanchors: [src/mod.py::foo]")
    write_note(project_dir, "specs/b.md", "updated: 2026-01-01")
    register(project_dir, "specs/a.md")
    register(project_dir, "specs/b.md")
    (project_dir / "progress.md").write_text("# P\n", encoding="utf-8")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cp.main([str(project_dir)])
    out = buf.getvalue()
    check("notes without summary: 1 of 2" in out, "coverage footer printed with the OK report")
    check("anchor symbols unchecked: 1" in out, "unchecked-symbol footer printed when no snapshot exists")
    check(".lsp-cache" in out, "footer names the default snapshot location")

    snap = write_snapshot(ws, "src/mod.py")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc2 = cp.main([str(project_dir), "--lsp-snapshot", str(snap)])
    out2 = buf.getvalue()
    check("anchor symbols unchecked" not in out2, "no unchecked footer once a snapshot is given")
    check("notes without summary: 1 of 2" in out2, "coverage footer still printed")
    check(rc == rc2, f"footers never change the exit code (got {rc} vs {rc2})")

    write_note(project_dir, "specs/a.md", "summary: s\nanchors: [src/mod.py::zzz]")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc3 = cp.main([str(project_dir), "--lsp-snapshot", str(snap)])
    out3 = buf.getvalue()
    check(rc3 == 1, f"a dangling anchor drives exit 1 (got {rc3})")
    check(cp.SEVERITY_LABEL[cp.ANCHOR_DANGLING_SEVERITY] in out3,
          "the dangling finding prints under its severity label")


def main() -> int:
    print("=== check_progress.py notes data-contract checks (#14-#16 + footer) ===")
    for t in (test_nested_dir, test_index_attachment_rows, test_summary_length_and_coverage,
              test_anchor_dangling, test_main_footer_and_flag):
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
