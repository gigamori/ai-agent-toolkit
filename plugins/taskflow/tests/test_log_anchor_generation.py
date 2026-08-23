#!/usr/bin/env python3
"""T-D3-1 — unit tests for hooks/session_progress_capture.py::append_auto_binding
`@log` block GENERATION (project-notes/specs/capture-detection-gaps.md §4.2, D3).

Before D3, a task md carrying NEITHER `<!-- @log:begin -->` NOR
`<!-- @log:end -->` could never be bound: `repair_log_markers` only handles the
"exactly one begin, no end" damage shape and returns None otherwise, so
`append_auto_binding` returned False and the touched task was dropped silently.
D3 makes that case GENERATE the block and bind:

    \\n<!-- @log:begin -->\\n- {iso_ts} [s:{sid8}]: {note}\\n<!-- @log:end -->\\n

inserted immediately before `<!-- @notes:begin -->` when one exists, otherwise
at EOF, with a newline supplied first when the preceding content does not end
with one.

Covered here:
  - no markers at all, no `@notes`      -> block generated at EOF, line inside
  - a `@notes` block exists             -> new `@log` block precedes NOTES_BEGIN
  - file does not end with a newline    -> no `...text<!-- @log:begin -->` splice
  - second call on the generated block  -> appends into it, no 2nd block
  - the half-damaged path (begin present / end missing) is UNCHANGED
  - ambiguous residue (two begins, no end) still fails (T-D3-2 covers the
    hook-level reporting of that case)

ABSOLUTE SAFETY (`e2e_state_dir_sandbox`): every
fixture lives inside a `tempfile.TemporaryDirectory()` under a real
`tasks/<status>/` layout (so `log_lock` keys its sidecar inside the tempdir
too). This test NEVER calls `main()`, NEVER reads stdin, and NEVER calls
`_cleanup_stale_markers` — so the real `_projects/_state/` sweep can never
fire. A file-count check on the real `_state/` dir brackets the whole run.

Run with:  uv run --no-project python plugins/taskflow/tests/test_log_anchor_generation.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import session_progress_capture as spc  # noqa: E402
from note_links import NOTES_BEGIN, NOTES_END  # noqa: E402

PASS = 0
FAIL = 0

SID8 = "d3anchor"
TS = "2026-08-08T12:00:00+09:00"

REAL_STATE_DIR = (Path(__file__).resolve().parent.parent.parent.parent
                  / "_projects" / "_state")


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


def make_task(d: Path, name: str, body: str) -> Path:
    """Write `body` verbatim (no trailing-newline normalization) under a real
    tasks/<status>/ layout inside the tempdir."""
    tasks_dir = d / "tasks" / "1_in_progress"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    p = tasks_dir / name
    p.write_text(body, encoding="utf-8", newline="")
    return p


def block_of(content: str) -> str | None:
    m = spc._LOG_BLOCK_RE.search(content)
    return m.group(1) if m else None


_HEAD = (
    "---\n"
    "priority: HIGH\n"
    "created: 2026-08-08\n"
    "updated: 2026-08-08\n"
    "---\n"
    "\n"
    "# D3 anchor-generation task\n"
    "\n"
    "## Next Steps\n"
    "- (none)\n"
)


def test_no_markers_at_all(d: Path) -> None:
    print("--- T-D3-1a: no @log markers at all -> block generated at EOF ---")
    p = make_task(d, "2026-08-08_no-markers.md", _HEAD)
    before = spc.log_block_has_sid(str(p), SID8)
    rc = spc.append_auto_binding(str(p), SID8, TS)
    after = spc.log_block_has_sid(str(p), SID8)
    content = p.read_text(encoding="utf-8")

    check(before is False, "precondition: no [s:] before the call")
    check(rc is True, "append_auto_binding returns True when it generates the block")
    check(after is True, "log_block_has_sid is True after generation")
    check(content.count("<!-- @log:begin -->") == 1,
          f"exactly one @log:begin generated (got "
          f"{content.count('<!-- @log:begin -->')})")
    check(content.count("<!-- @log:end -->") == 1,
          f"exactly one @log:end generated (got "
          f"{content.count('<!-- @log:end -->')})")
    blk = block_of(content)
    check(blk is not None and f"[s:{SID8}]" in blk,
          f"the [s:{SID8}] line landed INSIDE the generated block: {blk!r}")
    check(blk is not None
          and f"- {TS} [s:{SID8}]: (auto) touched; summary pending\n" in blk,
          f"generated line has the standard default form: {blk!r}")
    check(content.startswith(_HEAD),
          "pre-existing body content is preserved byte-for-byte ahead of the block")
    check(content.endswith("<!-- @log:end -->\n"),
          f"block sits at EOF and is newline-terminated: {content[-40:]!r}")


def test_notes_block_present(d: Path) -> None:
    print("--- T-D3-1b: a @notes block exists -> new @log block precedes NOTES_BEGIN ---")
    notes = (f"\n{NOTES_BEGIN}\n"
             f"<!-- auto-managed by taskflow note-link; do not hand-edit -->\n"
             f"- project-notes/specs/pre-existing.md\n"
             f"{NOTES_END}\n")
    p = make_task(d, "2026-08-08_with-notes.md", _HEAD + notes)
    rc = spc.append_auto_binding(str(p), SID8, TS)
    content = p.read_text(encoding="utf-8")

    check(rc is True, "append_auto_binding returns True with a @notes block present")
    i_begin = content.find("<!-- @log:begin -->")
    i_end = content.find("<!-- @log:end -->")
    i_notes = content.find(NOTES_BEGIN)
    check(i_begin != -1 and i_end != -1 and i_notes != -1,
          "all three markers present after generation")
    check(0 <= i_begin < i_end < i_notes,
          f"@log block is inserted BEFORE {NOTES_BEGIN} "
          f"(begin={i_begin} end={i_end} notes={i_notes})")
    blk = block_of(content)
    check(blk is not None and f"[s:{SID8}]" in blk,
          f"the [s:{SID8}] line landed inside the generated block: {blk!r}")
    check(notes.strip() in content.replace("\r", ""),
          "the pre-existing @notes block survives intact")
    check("- project-notes/specs/pre-existing.md" in content,
          "the pre-existing note link is untouched")
    check(content.count(NOTES_BEGIN) == 1,
          "no duplicate @notes:begin introduced")


def test_no_trailing_newline(d: Path) -> None:
    print("--- T-D3-1c: file does not end with a newline -> newline supplied first ---")
    tail = "Some closing prose with no trailing newline"
    p = make_task(d, "2026-08-08_no-eol.md", _HEAD + tail)
    rc = spc.append_auto_binding(str(p), SID8, TS)
    content = p.read_text(encoding="utf-8")

    check(rc is True, "append_auto_binding returns True on a non-newline-terminated file")
    check(f"{tail}<!-- @log:begin -->" not in content,
          "no `...text<!-- @log:begin -->` splice (a newline was supplied)")
    check(f"{tail}\n" in content, "the unterminated final line got its newline")
    check(re.search(r"\n<!--\s*@log:begin\s*-->", content) is not None,
          "@log:begin starts on its own line")
    blk = block_of(content)
    check(blk is not None and f"[s:{SID8}]" in blk,
          f"the [s:{SID8}] line landed inside the generated block: {blk!r}")


def test_second_call_reuses_generated_block(d: Path) -> None:
    print("--- T-D3-1d: a second bind appends INTO the generated block (no 2nd block) ---")
    p = make_task(d, "2026-08-08_twice.md", _HEAD)
    spc.append_auto_binding(str(p), SID8, TS)
    rc2 = spc.append_auto_binding(str(p), "othersid", TS, "second binding")
    content = p.read_text(encoding="utf-8")

    check(rc2 is True, "second append_auto_binding returns True")
    check(content.count("<!-- @log:begin -->") == 1,
          f"still exactly one @log:begin (got {content.count('<!-- @log:begin -->')})")
    blk = block_of(content)
    check(blk is not None and f"[s:{SID8}]" in blk and "[s:othersid]" in blk,
          f"both lines live inside the single block: {blk!r}")


def test_half_damage_path_unchanged(d: Path) -> None:
    print("--- regression: half damage (begin present / end missing) still repairs ---")
    p = make_task(d, "2026-08-08_half.md",
                  _HEAD + "\n<!-- @log:begin -->\n- 2026-08-08: created\n")
    rc = spc.append_auto_binding(str(p), SID8, TS)
    content = p.read_text(encoding="utf-8")

    check(rc is True, "half-damaged file is still repaired + bound (repair path)")
    check(content.count("<!-- @log:begin -->") == 1,
          "repair did not add a second @log:begin (D3 branch did not fire)")
    check(content.count("<!-- @log:end -->") == 1, "exactly one @log:end after repair")
    blk = block_of(content)
    check(blk is not None and "- 2026-08-08: created" in blk
          and f"[s:{SID8}]" in blk,
          f"pre-existing log line and the new line share the repaired block: {blk!r}")


def test_ambiguous_damage_still_fails(d: Path) -> None:
    print("--- regression: ambiguous residue (two @log:begin, no end) still fails ---")
    body = (_HEAD + "\n<!-- @log:begin -->\n- 2026-08-08: a\n"
            "\n<!-- @log:begin -->\n- 2026-08-08: b\n")
    p = make_task(d, "2026-08-08_ambiguous.md", body)
    rc = spc.append_auto_binding(str(p), SID8, TS)
    content = p.read_text(encoding="utf-8")

    check(rc is False, "append_auto_binding returns False on ambiguous damage")
    check(content == body, "the ambiguous file is left byte-for-byte untouched")
    check(spc.log_block_has_sid(str(p), SID8) is False, "no [s:] was written")


def real_state_count() -> int:
    try:
        return len(list(REAL_STATE_DIR.iterdir()))
    except OSError:
        return -1


def main() -> int:
    print("=== T-D3-1: append_auto_binding @log block generation (D3 §4.2) ===")
    before_count = real_state_count()
    print(f"  real _projects/_state/ file count (before): {before_count}")

    with tempfile.TemporaryDirectory() as d:
        test_no_markers_at_all(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_notes_block_present(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_no_trailing_newline(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_second_call_reuses_generated_block(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_half_damage_path_unchanged(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_ambiguous_damage_still_fails(Path(d))

    print("--- ABSOLUTE SAFETY: real _projects/_state/ unchanged ---")
    after_count = real_state_count()
    print(f"  real _projects/_state/ file count (after):  {after_count}")
    check(before_count == after_count,
          f"real _state/ file count unchanged ({before_count} -> {after_count})")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
