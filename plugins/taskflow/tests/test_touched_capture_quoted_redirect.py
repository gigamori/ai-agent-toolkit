#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

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


CASES = [
    ("T1", 'echo "real _state: $BEFORE -> $AFTER"', []),
    ("T2", 'echo "a -> b"', []),
    ("T3", "echo 'x > y'", []),
    ("T4", "cmd > out.txt", ["out.txt"]),
    ("T5", "echo hi >> log.md", ["log.md"]),
    ("T6", "cmd 2> err.log", ["err.log"]),
    ("T7", '2>> "log f.txt"', ["log f.txt"]),
    ("T8", 'echo "a > b" > out.txt', ["out.txt"]),
    ("T9", 'echo "it\'s > here"', []),
    ("T10", 'echo "it\'s fine" > out.txt', ["out.txt"]),
    ("T11", "cmd >&2", []),
    ("T12", "cmd 2>&1", []),
    ("T13", "cmd &> all.log", ["all.log"]),
    ("T14", "cmd > /dev/null", []),
    ("T15", "cmd 2>> /dev/null", []),
    ("T16", "cmd > NUL", []),
    ("T17", "cmd > nul", []),
    ("T18", "cmd > logs/nul.txt", ["logs/nul.txt"]),
    ("T19", "rm z.md", ["z.md"]),
    ("T20", "echo x | tee t.md", ["t.md"]),
    ("T21", "echo x && mv c.md d.md", ["c.md", "d.md"]),
    ("T22", 'echo "a > b" && rm z.md', ["z.md"]),
    ("T29", "cp x.md y.md", ["x.md", "y.md"]),
    ("T30", "echo x | tee -a t2.md", ["t2.md"]),
    ("T23", "# don't do this\nls > out.txt", ["out.txt"]),
    ("T24", "git commit -m x # it's fine\nls > o.txt", ["o.txt"]),
    ("T25", "echo don't\nfoo > b.txt", ["b.txt"]),
    ("T26", "# don't won't\nls > out.txt", ["out.txt"]),
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
