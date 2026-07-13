#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Unit tests for generate_kanban.py's LOG_ENTRY_RE (@log line parsing).

Regression guard for the timestamp-timezone-consistency fix
(_projects/harness-taskflow/tasks/0_todo/2026-07-14_log-timestamp-timezone-consistency.md,
item #6): the regex must keep matching pre-fix entries (no offset, `T`
timestamp or bare date) AND new offset-aware entries produced by
`tstamp.now_iso()` (`+HH:MM` or `Z`), so channel A/B's timestamp format
change never silently drops entries from the kanban session list again.

stdlib only. Run with:  uv run python plugins/taskflow/tests/test_log_entry_regex.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Import the module under test from scripts/ (sibling of tests/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import generate_kanban as gk  # noqa: E402

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


CASES = [
    # (label, line, expected groups or None if no match)
    ("legacy T-timestamp, no offset",
     "- 2026-07-13T22:52:03 [s:2053faf1]: Moved from 0_todo to 1_in_progress",
     ("2026-07-13T22:52:03", "2053faf1")),
    ("bare date, no time component (SKILL.md transition-line format)",
     "- 2026-07-13 [s:abcd1234]: approved → 2_done",
     ("2026-07-13", "abcd1234")),
    ("new offset-aware timestamp (+HH:MM)",
     "- 2026-07-14T01:17:28+09:00 [s:bf72387a]: 起票",
     ("2026-07-14T01:17:28+09:00", "bf72387a")),
    ("new offset-aware timestamp, negative offset",
     "- 2026-07-14T01:17:28-05:00 [s:bf72387a]: note",
     ("2026-07-14T01:17:28-05:00", "bf72387a")),
    ("new offset-aware timestamp, Z (UTC)",
     "- 2026-07-14T01:17:28Z [s:bf72387a]: note",
     ("2026-07-14T01:17:28Z", "bf72387a")),
]


def test_formats_match() -> None:
    for label, line, expected in CASES:
        m = gk.LOG_ENTRY_RE.search(line)
        check(m is not None, f"{label}: matches")
        if m is not None and expected is not None:
            check(m.group(1) == expected[0], f"{label}: timestamp group == {expected[0]!r}")
            check(m.group(2) == expected[1], f"{label}: sid group == {expected[1]!r}")


def test_extract_sessions_mixed_log() -> None:
    """A single @log block mixing pre-fix and post-fix entries must yield all of them."""
    content = (
        "<!-- @log:begin -->\n"
        "- 2026-07-13T13:45:45 [s:907c0329]: legacy narrative entry\n"
        "- 2026-07-13: started → 1_in_progress\n"
        "- 2026-07-13T22:52:03 [s:2053faf1]: legacy transition applied\n"
        "- 2026-07-14T01:17:28+09:00 [s:bf72387a]: new offset-aware entry\n"
        "<!-- @log:end -->\n"
    )
    refs = gk.extract_sessions(content)
    sids = [r.short_id for r in refs]
    check(sids == ["907c0329", "2053faf1", "bf72387a"],
          f"extract_sessions finds all sid-tagged entries (offset + legacy), got {sids}")


def main() -> int:
    test_formats_match()
    test_extract_sessions_mixed_log()

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
