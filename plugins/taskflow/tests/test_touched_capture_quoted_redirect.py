#!/usr/bin/env python3
"""Unit tests for touched_capture.py's quote-aware redirection scan.

Regression guard for the defect recorded as A-6 in
_projects/harness-taskflow/project-notes/specs/review-2026-08-19-fixes.md §8
(as amended by that spec's design review, findings F-2/F-3/F-4/F-12):

    _REDIRECT_RE = re.compile(r'\\d?>>?\\s*(?!&)("[^"]*"|\\'[^\\']*\\'|[^\\s|&;<>()]+)')

treated a `>` INSIDE a quoted string as a shell redirection. Observed live in a
`.touched` ledger: `echo "real _state: $BEFORE -> $AFTER"` recorded the literal
token `$AFTER` as a written path, and `/dev/null` was recorded from another
command. Nothing downstream breaks (neither resolves to a task md nor to
`project-notes/`, so `resolve_touched_tasks` and `_scan_note_writes` both drop
them) but `.touched` is append-only and its raw line count is the round cursor,
so every spurious line shifts the round slice. This is a ledger-integrity fix.

The replacement is a three-state character scan (`outside` / `single` /
`double`) per review F-4, not a "skip redirect extraction when the command
contains a quote" shortcut. T8 is the case that separates the two: the
degenerate implementation passes every other case in the list and fails T8.

Sandbox (review F-2): §8's claim that `e2e_state_dir_sandbox` is non-applicable
"because this hook resolves no `_projects` path and writes nothing" is FALSE —
touched_capture.py defines PROGRESS_ROOT/STATE_DIR from `os.getcwd()` at module
scope and `main()` appends to `<STATE_DIR>/<session_id>.touched`. The
conclusion survives only for this test's exact shape: every case below calls
`extract_bash_paths` directly and NEVER `main()`, so no `_projects` path is
resolved and nothing is written. Do not exercise these cases end-to-end through
the hook, and do not extend test_touched_capture.sh's `[main]` sections.

stdlib only. Run with:
    uv run --no-project python plugins/taskflow/tests/test_touched_capture_quoted_redirect.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Import the module under test from hooks/ (sibling of tests/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import touched_capture as tc  # noqa: E402

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


# (id, command, expected extract_bash_paths() result)
#
# The expectation is the FULL list, not a membership test: a quote-tracking
# scan is judged by what it does NOT record as much as by what it does.
CASES = [
    # --- the field observation, verbatim -----------------------------------
    ("T1", 'echo "real _state: $BEFORE -> $AFTER"', []),
    # --- reduced forms of the same defect ----------------------------------
    ("T2", 'echo "a -> b"', []),
    ("T3", "echo 'x > y'", []),
    # --- real redirections must still be captured --------------------------
    ("T4", "cmd > out.txt", ["out.txt"]),
    ("T5", "echo hi >> log.md", ["log.md"]),
    ("T6", "cmd 2> err.log", ["err.log"]),
    # A quoted TARGET is fine — it is the quoting of the OPERATOR that
    # suppresses a redirection, never the quoting of what follows it.
    ("T7", '2>> "log f.txt"', ["log f.txt"]),
    # --- the discriminating case (review F-3) ------------------------------
    # The only case that separates a real quote-aware scan from the degenerate
    # "if the command contains a quote, skip redirect extraction" shortcut:
    # the quoted `>` must be ignored AND the unquoted one still captured.
    ("T8", 'echo "a > b" > out.txt', ["out.txt"]),
    # --- quote-state desynchronization (review F-3) ------------------------
    # An apostrophe INSIDE a double-quoted string must not toggle single-quote
    # state; doing so desynchronizes the rest of the line.
    ("T9", 'echo "it\'s > here"', []),
    # T10 is T9's consequence: a scanner that desynchronizes on the apostrophe
    # silently DROPS this real redirection instead of merely over-recording.
    ("T10", 'echo "it\'s fine" > out.txt', ["out.txt"]),
    # --- fd duplication: existing behaviour, must not change ---------------
    ("T11", "cmd >&2", []),
    ("T12", "cmd 2>&1", []),
    # `&>` (redirect both fds to a file) is NOT fd duplication and is captured
    # today; a rewrite must not lose it silently (review F-3).
    ("T13", "cmd &> all.log", ["all.log"]),
    # --- null sinks, excluded (review F-3 recommendation) ------------------
    # `/dev/null` is an exact POSIX path; `NUL` is a Windows device name and is
    # case-insensitive by platform convention, so both spellings are pinned.
    ("T14", "cmd > /dev/null", []),
    ("T15", "cmd 2>> /dev/null", []),
    ("T16", "cmd > NUL", []),
    ("T17", "cmd > nul", []),
    # A path that merely CONTAINS a null-sink spelling is a real file.
    ("T18", "cmd > logs/nul.txt", ["logs/nul.txt"]),
    # --- verb path unchanged (out of A-6's scope, pinned as regression) ----
    ("T19", "rm z.md", ["z.md"]),
    ("T20", "echo x | tee t.md", ["t.md"]),
    ("T21", "echo x && mv c.md d.md", ["c.md", "d.md"]),
    # A quoted `>` must not break the verb path either.
    ("T22", 'echo "a > b" && rm z.md', ["z.md"]),
    # --- R-1 regression: an unpaired apostrophe must not outlive its line ---
    # The quote-aware scan (A-6) introduced a LOSS of capture the old regex did
    # not have: one contraction in a comment left the scan in `single` for the
    # rest of a multi-line command, silently dropping every later redirect.
    # Measured on the real hook before the newline reset: all four returned [].
    # `.touched` is the sole input to task resolution, so a drop is worse than
    # the false positive A-6 removed. (review-2026-08-19-fixes.md §8 A-6;
    # implementation review R-1.)
    ("T23", "# don't do this\nls > out.txt", ["out.txt"]),
    ("T24", "git commit -m x # it's fine\nls > o.txt", ["o.txt"]),
    ("T25", "echo don't\nfoo > b.txt", ["b.txt"]),
    # Two apostrophes on one line already re-synced by parity; pinned so the
    # reset is not mistaken for the only thing making this case work.
    ("T26", "# don't won't\nls > out.txt", ["out.txt"]),
    # The reset must not resurrect a quoted `>`: still no capture on the SAME
    # line, which is what A-6 exists to guarantee.
    ("T27", 'echo "a -> b"\nls > c.txt', ["c.txt"]),
    ("T28", 'echo "a -> b"', []),
]


def test_cases() -> None:
    print("--- extract_bash_paths() over the quoted-redirect case set ---")
    for cid, cmd, expected in CASES:
        got = tc.extract_bash_paths(cmd)
        check(got == expected, f"{cid} {cmd!r} -> {expected!r} (got {got!r})")


def main() -> int:
    print("=== touched_capture.py quote-aware redirection tests ===")
    test_cases()
    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
