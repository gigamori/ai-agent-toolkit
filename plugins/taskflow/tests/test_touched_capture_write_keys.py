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


def test_single_keys() -> None:
    """Each entry of `WRITE_PATH_KEYS` is returned verbatim, un-normalized."""
    print("--- one write key at a time, returned verbatim ---")
    got = tc.extract_paths({"file_path": "/r/a.md"})
    check(got == ["/r/a.md"],
          f"W1 file_path -> ['/r/a.md'] (got {got!r})")
    got = tc.extract_paths({"notebook_path": "/r/n.ipynb"})
    check(got == ["/r/n.ipynb"],
          f"W2 notebook_path -> ['/r/n.ipynb'] (got {got!r})")


def test_non_vacuity_controls() -> None:
    """Also pins that a non-dict `tool_input` returns [] instead of raising."""
    print("--- nothing to extract -> [] ---")
    got = tc.extract_paths({})
    check(got == [],
          f"W3a empty tool_input -> [] (got {got!r})")
    got = tc.extract_paths("not a dict")  # type: ignore[arg-type]
    check(got == [],
          f"W3b non-dict tool_input -> [] (isinstance guard) (got {got!r})")


def test_both_keys_in_one_input() -> None:
    """The defect this catches -- a loop that stops at the first hit -- is invisible when
    each key is tested alone."""
    print("--- both write keys present in one tool_input ---")
    got = tc.extract_paths({"file_path": "/r/a.md",
                            "notebook_path": "/r/n.ipynb"})
    check(got == ["/r/a.md", "/r/n.ipynb"],
          f"W4 both keys -> both recorded in WRITE_PATH_KEYS order "
          f"(got {got!r}, keys={tc.WRITE_PATH_KEYS!r})")


def main() -> int:
    print("=== touched_capture.py tool_input write-key extraction ===")
    test_single_keys()
    test_non_vacuity_controls()
    test_both_keys_in_one_input()
    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
