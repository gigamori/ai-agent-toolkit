#!/usr/bin/env python3
"""Unit tests for hooks/session_progress_capture.py::_apply_capture — the D-7
deterministic note-path guard (project-notes/specs/capture-context-abs-path.md
Q6 / AC-9, review finding F-R2-1/F-R2-2).

Background: a capture subagent's OUTPUT contract requires `note_links[].note`
to be project-relative (starting with `project-notes/`) — but that contract
is enforced only by the agent prompt (agents/progress-capture.md), which is
non-deterministic (§10.1). Before D-7, a subagent that violated this contract
with an absolute or otherwise off-contract path could still reach
`append_note_link()` and get burned into a task's `@notes` block permanently,
via the *legacy* apply path where `items=None` bypasses the F7a membership
set entirely (session_progress_capture.py `_apply_capture`, `items=None` ->
`note_set is None` -> membership check skipped).

D-7 adds `if not note_rel.startswith('project-notes/'): continue` right after
`note_rel` is computed, so the reject is unconditional — independent of
whether `items`/`note_set` is present. This file pins:
  - T-5a: an absolute note path is REJECTED under `items=None` (legacy
    fail-open path) — the exact hole D-7 closes.
  - T-5b: an absolute note path is REJECTED even when the offending value
    itself is included in a request-time `items['notes']` set (proves the
    guard fires before/independent of membership, not merely because
    membership would have caught it anyway).
  - T-5c: a normal project-relative note (unchanged shape) still applies
    correctly (no regression to the AC-4 output contract's happy path).

Fixture tasks live in a `tempfile.TemporaryDirectory()`; this file never
touches real `_projects/_state/` or the real repo tree.

Run:  uv run --no-project python plugins/taskflow/tests/test_apply_capture_note_guard.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import session_progress_capture as spc  # noqa: E402
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


def make_task(root: Path, name: str = "task1.md") -> Path:
    task_dir = root / "tasks" / "1_in_progress"
    task_dir.mkdir(parents=True, exist_ok=True)
    task_path = task_dir / name
    task_path.write_text(TASK_TEMPLATE, encoding="utf-8")
    return task_path


def test_absolute_note_rejected_under_legacy_fail_open(root: Path) -> None:
    print("--- T-5a: absolute note path rejected, items=None (legacy fail-open path) ---")
    task_path = make_task(root)
    current_index = {"task1.md": str(task_path)}
    sidecar = {
        "confirmed": [],
        "note_links": [
            {"note": "C:/other-repo/project-notes/specs/evil.md", "task": "task1.md"},
        ],
        "proposals": [],
    }
    summaries, links, proposals, link_skipped, membership_skipped = spc._apply_capture(
        sidecar, current_index, "harness-taskflow", str(root), "abcd1234", "2026-07-30T01:00:00+09:00",
        items=None,
    )
    check(links == [], f"no note_link applied (got {links})")
    content = task_path.read_text(encoding="utf-8")
    check(nl.NOTES_BEGIN not in content, "@notes block was NOT created on the task file")
    check("evil.md" not in content, "the off-contract note path never appears in the task file")


def test_absolute_note_rejected_even_when_in_membership_set(root: Path) -> None:
    print("--- T-5b: absolute note path rejected even if present in items['notes'] ---")
    task_path = make_task(root, "task2.md")
    current_index = {"task2.md": str(task_path)}
    evil_note = "C:/other-repo/project-notes/specs/evil.md"
    sidecar = {
        "confirmed": [],
        "note_links": [{"note": evil_note, "task": "task2.md"}],
        "proposals": [],
    }
    # items explicitly ADMITS the offending value into the request-time closed
    # set — if the guard only worked "because membership would reject it
    # anyway", this call would apply the link. It must still be rejected.
    items = {"tasks": ["task2.md"], "notes": [evil_note]}
    summaries, links, proposals, link_skipped, membership_skipped = spc._apply_capture(
        sidecar, current_index, "harness-taskflow", str(root), "abcd1234", "2026-07-30T01:00:00+09:00",
        items=items,
    )
    check(links == [], f"no note_link applied even though evil_note is in items['notes'] (got {links})")
    check(membership_skipped == [], f"guard fires before membership check, not via it (got {membership_skipped})")
    content = task_path.read_text(encoding="utf-8")
    check(nl.NOTES_BEGIN not in content, "@notes block was NOT created on the task file")


def test_normal_project_relative_note_still_applies(root: Path) -> None:
    print("--- T-5c: regression - normal project-relative note still applies ---")
    task_path = make_task(root, "task3.md")
    current_index = {"task3.md": str(task_path)}
    good_note = "project-notes/specs/foo.md"
    sidecar = {
        "confirmed": [],
        "note_links": [{"note": good_note, "task": "task3.md"}],
        "proposals": [],
    }
    summaries, links, proposals, link_skipped, membership_skipped = spc._apply_capture(
        sidecar, current_index, "harness-taskflow", str(root), "abcd1234", "2026-07-30T01:00:00+09:00",
        items=None,
    )
    check(links == [(good_note, "task3.md")], f"note_link applied for a normal project-relative note (got {links})")
    content = task_path.read_text(encoding="utf-8")
    check(good_note in content, "note rel is recorded in the task's @notes block")


def main() -> int:
    print("=== session_progress_capture.py _apply_capture D-7 note-path guard tests ===")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        test_absolute_note_rejected_under_legacy_fail_open(root)
        test_absolute_note_rejected_even_when_in_membership_set(root)
        test_normal_project_relative_note_still_applies(root)

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
