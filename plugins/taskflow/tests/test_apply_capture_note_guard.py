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

F-2 (review-2026-08-19-fixes.md §2) adds a SECOND reject branch after the
prefix one: the prefix is not the bound it was taken for, because
`project-notes/../../../secrets/x.md` satisfies it and still leaves the project
root. Containment is now a named predicate (`note_links.is_contained_note_rel`)
enforced at three sites; this file pins the D-7 site:
  - T-6a: a `..` traversal is REJECTED under `items=None`.
  - T-6b: it is REJECTED even when admitted into `items['notes']`, and the skip
    is NOT attributed to the membership check.
  - T-6c: the reject is logged with its own distinct reason string, so the two
    invariants are distinguishable on stderr.
  - T-6d: the backslash spelling of the same payload (folded to `/` by
    `normalize_note_rel`, which is what makes it satisfy the prefix) is rejected.
  - T-6e: the minimal single-`..` escape is rejected — the rule is "no `..`",
    not "no deep escape".

D2 (capture-detection-gaps.md §3.3): `_apply_capture` now takes the resolved
`project_roots` map instead of a single `project_root`, its `current_index` is
the QUALIFIED `{"<project>/<basename>": path}` union, and the values it returns
name tasks by that qualified key. The D-7 guard itself is unchanged — it is
applied against the ENTRY's own project root — so the three rejects below still
pin exactly the same behavior.

Fixture tasks live in a `tempfile.TemporaryDirectory()`; this file never
touches real `_projects/_state/` or the real repo tree.

Run:  uv run --no-project python plugins/taskflow/tests/test_apply_capture_note_guard.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import io
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
    current_index = {"harness-taskflow/task1.md": str(task_path)}
    sidecar = {
        "confirmed": [],
        "note_links": [
            {"note": "C:/other-repo/project-notes/specs/evil.md", "task": "task1.md"},
        ],
        "proposals": [],
    }
    summaries, links, proposals, link_skipped, membership_skipped = spc._apply_capture(
        sidecar, current_index, "harness-taskflow", {"harness-taskflow": str(root)},
        "abcd1234", "2026-07-30T01:00:00+09:00",
        items=None,
    )
    check(links == [], f"no note_link applied (got {links})")
    content = task_path.read_text(encoding="utf-8")
    check(nl.NOTES_BEGIN not in content, "@notes block was NOT created on the task file")
    check("evil.md" not in content, "the off-contract note path never appears in the task file")


def test_absolute_note_rejected_even_when_in_membership_set(root: Path) -> None:
    print("--- T-5b: absolute note path rejected even if present in items['notes'] ---")
    task_path = make_task(root, "task2.md")
    current_index = {"harness-taskflow/task2.md": str(task_path)}
    evil_note = "C:/other-repo/project-notes/specs/evil.md"
    sidecar = {
        "confirmed": [],
        "note_links": [{"note": evil_note, "task": "task2.md"}],
        "proposals": [],
    }
    # items explicitly ADMITS the offending value into the request-time closed
    # set — if the guard only worked "because membership would reject it
    # anyway", this call would apply the link. It must still be rejected.
    items = {"tasks": ["harness-taskflow/task2.md"], "notes": [evil_note]}
    summaries, links, proposals, link_skipped, membership_skipped = spc._apply_capture(
        sidecar, current_index, "harness-taskflow", {"harness-taskflow": str(root)},
        "abcd1234", "2026-07-30T01:00:00+09:00",
        items=items,
    )
    check(links == [], f"no note_link applied even though evil_note is in items['notes'] (got {links})")
    check(membership_skipped == [], f"guard fires before membership check, not via it (got {membership_skipped})")
    content = task_path.read_text(encoding="utf-8")
    check(nl.NOTES_BEGIN not in content, "@notes block was NOT created on the task file")


def test_reject_is_logged_to_stderr(root: Path) -> None:
    print("--- T-5d: D-7 reject is reported to stderr, not silent (review F-I1) ---")
    # Before this fix, the D-7 guard's `continue` had no observability at
    # all: unlike the pre-existing membership_skipped path, a reject firing
    # ahead of membership left zero trace anywhere. That silence is exactly
    # the failure class this task exists to eliminate (2026-07-28 incident).
    task_path = make_task(root, "task4.md")
    current_index = {"harness-taskflow/task4.md": str(task_path)}
    evil_note = "C:/other-repo/project-notes/specs/evil.md"
    sidecar = {
        "confirmed": [],
        "note_links": [{"note": evil_note, "task": "task4.md"}],
        "proposals": [],
    }
    stderr_buf = io.StringIO()
    with contextlib.redirect_stderr(stderr_buf):
        spc._apply_capture(
            sidecar, current_index, "harness-taskflow", {"harness-taskflow": str(root)},
        "abcd1234", "2026-07-30T01:00:00+09:00",
            items=None,
        )
    out = stderr_buf.getvalue()
    check("note-path-reject" in out, f"reject is logged to stderr: {ascii(out)}")
    check(evil_note in out, f"logged line names the offending note path: {ascii(out)}")
    check("[s:abcd1234]" in out, f"logged line carries the session tag: {ascii(out)}")


# A rel that satisfies the D-7 prefix and STILL leaves the project root: the
# `..` segments sit AFTER `project-notes/`, so `startswith('project-notes/')`
# is True and the traversal survives every other gate (review F-2, §2.1).
TRAVERSAL_NOTE = "project-notes/../../../secrets/x.md"


def test_traversal_note_rejected_under_legacy_fail_open(root: Path) -> None:
    print("--- T-6a: `..` traversal rejected, items=None (legacy fail-open path) ---")
    task_path = make_task(root, "task5.md")
    current_index = {"harness-taskflow/task5.md": str(task_path)}
    sidecar = {
        "confirmed": [],
        "note_links": [{"note": TRAVERSAL_NOTE, "task": "task5.md"}],
        "proposals": [],
    }
    summaries, links, proposals, link_skipped, membership_skipped = spc._apply_capture(
        sidecar, current_index, "harness-taskflow", {"harness-taskflow": str(root)},
        "abcd1234", "2026-08-19T01:00:00+09:00",
        items=None,
    )
    check(links == [], f"no note_link applied for a traversal rel (got {links})")
    content = task_path.read_text(encoding="utf-8")
    check(nl.NOTES_BEGIN not in content, "@notes block was NOT created on the task file")
    check("secrets" not in content, "the escaping note path never appears in the task file")


def test_traversal_note_rejected_even_when_in_membership_set(root: Path) -> None:
    print("--- T-6b: `..` traversal rejected even if present in items['notes'] ---")
    task_path = make_task(root, "task6.md")
    current_index = {"harness-taskflow/task6.md": str(task_path)}
    sidecar = {
        "confirmed": [],
        "note_links": [{"note": TRAVERSAL_NOTE, "task": "task6.md"}],
        "proposals": [],
    }
    # The containment reject must fire BEFORE the membership check, exactly as
    # the prefix reject does: admitting the value into the round's closed set
    # must not buy it a pass, and the skip must not be attributed to membership.
    items = {"tasks": ["harness-taskflow/task6.md"], "notes": [TRAVERSAL_NOTE]}
    summaries, links, proposals, link_skipped, membership_skipped = spc._apply_capture(
        sidecar, current_index, "harness-taskflow", {"harness-taskflow": str(root)},
        "abcd1234", "2026-08-19T01:00:00+09:00",
        items=items,
    )
    check(links == [],
          f"no note_link applied even though the traversal is in items['notes'] (got {links})")
    check(membership_skipped == [],
          f"containment reject fires before membership check, not via it (got {membership_skipped})")
    content = task_path.read_text(encoding="utf-8")
    check(nl.NOTES_BEGIN not in content, "@notes block was NOT created on the task file")


def test_traversal_reject_is_logged_to_stderr(root: Path) -> None:
    print("--- T-6c: the containment reject is reported to stderr with its own reason ---")
    # The second reject branch must be exactly as loud as the first one, and it
    # must name WHICH invariant failed — an operator seeing `note-path-reject`
    # has to be able to tell a wrong-prefix path from an escaping one.
    task_path = make_task(root, "task7.md")
    current_index = {"harness-taskflow/task7.md": str(task_path)}
    sidecar = {
        "confirmed": [],
        "note_links": [{"note": TRAVERSAL_NOTE, "task": "task7.md"}],
        "proposals": [],
    }
    stderr_buf = io.StringIO()
    with contextlib.redirect_stderr(stderr_buf):
        spc._apply_capture(
            sidecar, current_index, "harness-taskflow", {"harness-taskflow": str(root)},
            "abcd1234", "2026-08-19T01:00:00+09:00",
            items=None,
        )
    out = stderr_buf.getvalue()
    check("note-path-reject" in out, f"reject is logged to stderr: {ascii(out)}")
    check("escapes the project root" in out,
          f"logged line names the containment invariant, not the prefix one: {ascii(out)}")
    check(TRAVERSAL_NOTE in out, f"logged line names the offending note path: {ascii(out)}")
    check("[s:abcd1234]" in out, f"logged line carries the session tag: {ascii(out)}")


def test_traversal_backslash_spelling_rejected(root: Path) -> None:
    print("--- T-6d: the backslash spelling of the same payload is rejected ---")
    # `normalize_note_rel` folds `\` to `/`, which TURNS this into a rel that
    # satisfies the `project-notes/` prefix. It is therefore a second real
    # spelling of the same reachable payload, not a variant that dies earlier.
    task_path = make_task(root, "task8.md")
    current_index = {"harness-taskflow/task8.md": str(task_path)}
    backslash_note = "project-notes\\..\\..\\secrets\\x.md"
    sidecar = {
        "confirmed": [],
        "note_links": [{"note": backslash_note, "task": "task8.md"}],
        "proposals": [],
    }
    summaries, links, proposals, link_skipped, membership_skipped = spc._apply_capture(
        sidecar, current_index, "harness-taskflow", {"harness-taskflow": str(root)},
        "abcd1234", "2026-08-19T01:00:00+09:00",
        items=None,
    )
    check(links == [], f"no note_link applied for the backslash spelling (got {links})")
    content = task_path.read_text(encoding="utf-8")
    check(nl.NOTES_BEGIN not in content, "@notes block was NOT created on the task file")
    check("secrets" not in content, "the escaping note path never appears in the task file")


def test_minimal_single_dotdot_escape_rejected(root: Path) -> None:
    print("--- T-6e: the minimal single-`..` escape is rejected too ---")
    # Still inside `_projects/<project>/` after resolution — it only leaves
    # `project-notes/`. The rule is "no `..`", not "no DEEP escape"; pinning
    # this prevents a later weakening into a depth heuristic.
    task_path = make_task(root, "task9.md")
    current_index = {"harness-taskflow/task9.md": str(task_path)}
    shallow_note = "project-notes/../tasks/1_in_progress/task9.md"
    sidecar = {
        "confirmed": [],
        "note_links": [{"note": shallow_note, "task": "task9.md"}],
        "proposals": [],
    }
    summaries, links, proposals, link_skipped, membership_skipped = spc._apply_capture(
        sidecar, current_index, "harness-taskflow", {"harness-taskflow": str(root)},
        "abcd1234", "2026-08-19T01:00:00+09:00",
        items=None,
    )
    check(links == [], f"no note_link applied for a single-segment escape (got {links})")
    content = task_path.read_text(encoding="utf-8")
    check(nl.NOTES_BEGIN not in content, "@notes block was NOT created on the task file")


def test_normal_project_relative_note_still_applies(root: Path) -> None:
    print("--- T-5c: regression - normal project-relative note still applies ---")
    task_path = make_task(root, "task3.md")
    current_index = {"harness-taskflow/task3.md": str(task_path)}
    good_note = "project-notes/specs/foo.md"
    sidecar = {
        "confirmed": [],
        "note_links": [{"note": good_note, "task": "task3.md"}],
        "proposals": [],
    }
    summaries, links, proposals, link_skipped, membership_skipped = spc._apply_capture(
        sidecar, current_index, "harness-taskflow", {"harness-taskflow": str(root)},
        "abcd1234", "2026-07-30T01:00:00+09:00",
        items=None,
    )
    check(links == [(good_note, "harness-taskflow/task3.md")],
          f"note_link applied for a normal project-relative note, qualified task key (got {links})")
    content = task_path.read_text(encoding="utf-8")
    check(good_note in content, "note rel is recorded in the task's @notes block")


def main() -> int:
    print("=== session_progress_capture.py _apply_capture D-7 note-path guard tests ===")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        test_absolute_note_rejected_under_legacy_fail_open(root)
        test_absolute_note_rejected_even_when_in_membership_set(root)
        test_reject_is_logged_to_stderr(root)
        test_traversal_note_rejected_under_legacy_fail_open(root)
        test_traversal_note_rejected_even_when_in_membership_set(root)
        test_traversal_reject_is_logged_to_stderr(root)
        test_traversal_backslash_spelling_rejected(root)
        test_minimal_single_dotdot_escape_rejected(root)
        test_normal_project_relative_note_still_applies(root)

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
