#!/usr/bin/env python3
"""Unit tests for the note↔task link data layer (note_links.py, Phase A).

Covers the deterministic acceptance criteria of
project-notes/specs/note-task-link.md:
  - AC-3  idempotent: same note appended twice → single entry, no duplicate
  - AC-4  pure-reference filter: a note never linked → resolve returns []
  - AC-5  exclusion: index.md / _archive/ are not note deliverables (handoff is)
  - AC-7  stale-skip: a deleted/renamed note is dropped from the reverse index
  - AC-2 (deterministic part): after establish, the same note resolves to the
          same owning task with no judgment
Plus structural guards: the `@notes` block is created after `@log:end` and the
`@log` block stays intact (the §4.1 placement gate).

stdlib only. Run with:  uv run python plugins/taskflow/tests/test_note_links.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# Import the module under test from hooks/ (sibling of log_lock.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import note_links as nl  # noqa: E402

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


TASK_TEMPLATE = """\
---
priority: MID
---

# Test task

## Next Steps

<!-- @log:begin -->
- 2026-07-01T00:00:00 [s:abcd1234]: created
<!-- @log:end -->
"""

_LOG_BLOCK_RE = re.compile(r"<!--\s*@log:begin\s*-->(.*?)<!--\s*@log:end\s*-->", re.DOTALL)


def make_project(root: Path) -> tuple[Path, Path]:
    """Create a minimal project: one in-progress task + one note + index.md.
    Returns (task_path, note_path)."""
    task_dir = root / "tasks" / "1_in_progress"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "task1.md"
    task_path.write_text(TASK_TEMPLATE, encoding="utf-8")

    notes_dir = root / "project-notes" / "specs"
    notes_dir.mkdir(parents=True)
    note_path = notes_dir / "foo.md"
    note_path.write_text("# foo spec\n", encoding="utf-8")

    (root / "project-notes" / "index.md").write_text("# index\n", encoding="utf-8")
    return task_path, note_path


def test_establish_and_placement(root: Path) -> None:
    print("--- establish + §4.1 placement ---")
    task_path, _ = make_project(root)
    note_rel = "project-notes/specs/foo.md"

    res = nl.append_note_link(str(task_path), note_rel)
    check(res is True, "append_note_link returns True on first establish")

    content = task_path.read_text(encoding="utf-8")
    check(nl.NOTES_BEGIN in content and nl.NOTES_END in content,
          "@notes block created")
    check(note_rel in nl.parse_note_links(content),
          "note rel recorded in @notes block")

    # Placement: @notes block must sit AFTER @log:end (§4.1).
    log_end = content.index("<!-- @log:end -->")
    notes_begin = content.index(nl.NOTES_BEGIN)
    check(notes_begin > log_end, "@notes block is placed after @log:end")

    # @log block stays intact and still parseable.
    log_m = _LOG_BLOCK_RE.search(content)
    check(bool(log_m) and "[s:abcd1234]" in log_m.group(1),
          "@log block remains intact after @notes insertion")


def test_idempotent_ac3(root: Path) -> None:
    print("--- AC-3 idempotent ---")
    task_path, _ = make_project(root)
    note_rel = "project-notes/specs/foo.md"

    nl.append_note_link(str(task_path), note_rel)
    res2 = nl.append_note_link(str(task_path), note_rel)
    check(res2 is True, "second append of same note returns True (no-op success)")

    links = nl.parse_note_links(task_path.read_text(encoding="utf-8"))
    check(links.count(note_rel) == 1, "no duplicate entry after double append")
    check(len(links) == 1, "exactly one link recorded")


def test_union_distinct(root: Path) -> None:
    print("--- union of distinct notes ---")
    task_path, _ = make_project(root)
    (root / "project-notes" / "specs" / "bar.md").write_text("# bar\n", encoding="utf-8")

    nl.append_note_link(str(task_path), "project-notes/specs/foo.md")
    nl.append_note_link(str(task_path), "project-notes/specs/bar.md")
    links = nl.parse_note_links(task_path.read_text(encoding="utf-8"))
    check(set(links) == {"project-notes/specs/foo.md", "project-notes/specs/bar.md"},
          "two distinct notes both recorded in one block")


def test_resolve_ac2(root: Path) -> None:
    print("--- AC-2 deterministic resolve ---")
    task_path, _ = make_project(root)
    note_rel = "project-notes/specs/foo.md"
    nl.append_note_link(str(task_path), note_rel)

    owners = nl.resolve_note_owner(note_rel, str(root))
    check(owners == [str(task_path)],
          "established note resolves deterministically to its owning task")


def test_pure_reference_ac4(root: Path) -> None:
    print("--- AC-4 pure-reference filter ---")
    task_path, _ = make_project(root)
    # A real note that no task links → must not resolve.
    guide = root / "project-notes" / "procedures" / "guide.md"
    guide.parent.mkdir(parents=True)
    guide.write_text("# authoring guide\n", encoding="utf-8")

    idx = nl.build_reverse_index(str(root))
    check("project-notes/procedures/guide.md" not in idx,
          "unlinked note has no reverse-index entry")
    owners = nl.resolve_note_owner("project-notes/procedures/guide.md", str(root))
    check(owners == [], "pure-reference note resolves to [] (AC-4)")


def test_stale_skip_ac7(root: Path) -> None:
    print("--- AC-7 stale-skip ---")
    task_path, note_path = make_project(root)
    note_rel = "project-notes/specs/foo.md"
    nl.append_note_link(str(task_path), note_rel)

    # Sanity: resolves before deletion.
    check(nl.resolve_note_owner(note_rel, str(root)) == [str(task_path)],
          "resolves before note deletion")

    note_path.unlink()  # simulate rename/delete
    idx = nl.build_reverse_index(str(root))
    check(note_rel not in idx, "deleted note dropped from reverse index")
    check(nl.resolve_note_owner(note_rel, str(root)) == [],
          "deleted note resolves to [] (stale-skip)")


def test_exclusions_ac5() -> None:
    print("--- AC-5 exclusion predicate ---")
    check(nl.is_note_deliverable("project-notes/specs/foo.md") is True,
          "specs note is a deliverable")
    check(nl.is_note_deliverable("project-notes/checks/handoff-x.md") is True,
          "handoff (checks/) is a deliverable (§5)")
    check(nl.is_note_deliverable("project-notes/index.md") is False,
          "project-notes/index.md excluded (registry)")
    check(nl.is_note_deliverable("project-notes/_archive/old.md") is False,
          "_archive/ excluded (non-authoritative)")
    check(nl.is_note_deliverable("tasks/0_todo/x.md") is False,
          "task md is not a note deliverable")
    check(nl.is_note_deliverable("project-notes/specs/foo.txt") is False,
          "non-.md excluded")


def test_no_anchor_fails(root: Path) -> None:
    print("--- no @log:end anchor → graceful False ---")
    task_dir = root / "tasks" / "0_todo"
    task_dir.mkdir(parents=True)
    bare = task_dir / "bare.md"
    bare.write_text("# bare task\n\nno log block here\n", encoding="utf-8")
    res = nl.append_note_link(str(bare), "project-notes/specs/foo.md")
    check(res is False, "append returns False when no @log:end anchor exists")
    check(nl.NOTES_BEGIN not in bare.read_text(encoding="utf-8"),
          "no @notes block written without an anchor")


def test_spec41_entry_literal_pin(root: Path) -> None:
    """PIN TEST — SPEC note-task-link.md §4.1 boundary contract.

    The `@notes` entry line and the auto-managed comment are a CROSS-HARNESS
    literal: the Pi taskflow extension parses what this writes and vice versa.
    Pi once deviated by writing `- <rel> [s:<sid8>] <ts>`, which made the two
    implementations unable to read each other. §4.1 now pins the bare form, and
    the Pi side carries the mirror of this test ("writes the SPEC §4.1 bare
    form" in binding.test.ts).

    Asserting the exact bytes is the point. If a future change wants to add a
    session id or a timestamp to an entry, it must change the SPEC and BOTH
    implementations together — this test failing is that conversation starting,
    not a test to relax.
    """
    print("--- §4.1 entry-literal pin (cross-harness boundary contract) ---")
    task_path, _ = make_project(root)

    # 1. Block creation from scratch.
    nl.append_note_link(str(task_path), "project-notes/specs/foo.md")
    content = task_path.read_text(encoding="utf-8")

    check(nl._AUTO_COMMENT
          == "<!-- auto-managed by taskflow note-link; do not hand-edit -->",
          f"auto-comment literal is byte-exact (got {nl._AUTO_COMMENT!r})")

    _, _, after_begin = content.partition(nl.NOTES_BEGIN + "\n")
    block_inner, _, _ = after_begin.partition(nl.NOTES_END)
    lines = block_inner.splitlines()
    check(lines == [nl._AUTO_COMMENT, "- project-notes/specs/foo.md"],
          f"fresh block is exactly [auto-comment, bare entry] (got {lines!r})")

    # 2. Append into an existing block.
    nl.append_note_link(str(task_path), "project-notes/checks/bar.md")
    content = task_path.read_text(encoding="utf-8")
    _, _, after_begin = content.partition(nl.NOTES_BEGIN + "\n")
    block_inner, _, _ = after_begin.partition(nl.NOTES_END)
    lines = block_inner.splitlines()
    check(lines == [nl._AUTO_COMMENT,
                    "- project-notes/specs/foo.md",
                    "- project-notes/checks/bar.md"],
          f"appended entry is the bare form, order preserved (got {lines!r})")

    # 3. Nothing resembling the retired Pi form leaked in.
    entry_lines = [ln for ln in lines if ln.startswith("- ")]
    check(all(re.fullmatch(r"- \S+", ln) for ln in entry_lines),
          f"every entry is exactly `- <note_rel>`: no [s:<sid8>], no timestamp "
          f"(got {entry_lines!r})")


def main() -> int:
    print("=== note_links.py unit tests (Phase A) ===")
    with tempfile.TemporaryDirectory() as d:
        for fn in (
            test_establish_and_placement,
            test_idempotent_ac3,
            test_union_distinct,
            test_resolve_ac2,
            test_pure_reference_ac4,
            test_stale_skip_ac7,
            test_no_anchor_fails,
            test_spec41_entry_literal_pin,
        ):
            sub = Path(d) / fn.__name__
            sub.mkdir()
            fn(sub)
        test_exclusions_ac5()

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
