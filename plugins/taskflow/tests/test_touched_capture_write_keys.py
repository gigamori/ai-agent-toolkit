#!/usr/bin/env python3
"""Unit tests for touched_capture.py's `tool_input` write-key extraction layer
-- `extract_paths` over `WRITE_PATH_KEYS`, both in touched_capture.py.

Scope is the function, not a provenance list: any future test of how a
`tool_input` dict is turned into raw write targets belongs here, including a
third `WRITE_PATH_KEYS` entry. `command` handling is deliberately NOT tested
here -- `extract_paths` delegates it to `extract_bash_paths`, whose own case
tables live in test_touched_capture_quoted_redirect.py and
test_touched_capture_bash_scope.py.

Origin: the consolidation of 2026-08-20
(mode-orchestrator-runs/2026-08-20_test-touched-capture-sh-state-hazard/,
plan §2.2/§3). W2 is a direct port of the retired test_touched_capture.sh's
`notebook` check (its A2) and W1 of its `write` check (its A1); the .sh was the
only place in the repo that called `extract_paths` at all, so without this file
its deletion would be a net coverage loss rather than a consolidation.

Why the unit level is not redundant with the end-to-end tests: through `main()`
every extracted value passes touched_capture.py's `normalize_path(p, cwd)`,
where `main()` sets `cwd = STATE_ROOT`, and which only strips a cwd prefix. An
`extract_paths` that mangled a path bearing no relation to the state root would
still satisfy every existing `main()`-level assertion, because those all feed
in-workspace paths. W1/W2 deliberately use `/r/...` -- unrelated to any cwd --
which is what makes them discriminating.

Sandbox (plugins/taskflow/CLAUDE.md `e2e_state_dir_sandbox`, same conclusion
and same reason as test_touched_capture_quoted_redirect.py): importing
touched_capture runs `_find_state_root(os.getcwd()) or os.getcwd()` at module
scope, so the import alone walks the cwd's ancestors looking for
`_projects/_state` -- read-only `isdir` probes, but run from inside the repo
they do resolve to the real one. The conclusion survives only for this file's
exact shape: every check below calls `extract_paths` directly and NEVER
`main()`, so nothing is written wherever STATE_DIR landed, and this file needs
no temp workspace and no real-state bracket. Do not add a case here that drives
the hook end to end -- those belong in test_touched_capture_ledger_append.py.

stdlib only. Run with:
    uv run --no-project python plugins/taskflow/tests/test_touched_capture_write_keys.py
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


def test_single_keys() -> None:
    """W1/W2: each entry of WRITE_PATH_KEYS is returned verbatim, un-normalized.

    W2 is the assertion that existed only in the retired .sh: nothing else in
    the repo exercised the SECOND key.
    """
    print("--- W1/W2: one write key at a time, returned verbatim ---")
    got = tc.extract_paths({"file_path": "/r/a.md"})
    check(got == ["/r/a.md"],
          f"W1 file_path -> ['/r/a.md'] (got {got!r})")
    got = tc.extract_paths({"notebook_path": "/r/n.ipynb"})
    check(got == ["/r/n.ipynb"],
          f"W2 notebook_path -> ['/r/n.ipynb'] (got {got!r})")


def test_non_vacuity_controls() -> None:
    """W3: the non-empty results above are discriminating.

    Also pins `extract_paths`' `isinstance` guard in touched_capture.py: a
    non-dict tool_input returns [] instead of raising.
    """
    print("--- W3 [controls]: nothing to extract -> [] ---")
    got = tc.extract_paths({})
    check(got == [],
          f"W3a empty tool_input -> [] (got {got!r})")
    got = tc.extract_paths("not a dict")  # type: ignore[arg-type]
    check(got == [],
          f"W3b non-dict tool_input -> [] (isinstance guard) (got {got!r})")


def test_both_keys_in_one_input() -> None:
    """W4: the second key is not shadowed by the first.

    The failure mode W2 exists to catch is a loop that stops at the first hit;
    that defect is invisible when each key is tested alone. Order is
    WRITE_PATH_KEYS order, which is what `extract_paths`' key loop iterates.
    """
    print("--- W4: both write keys present in one tool_input ---")
    got = tc.extract_paths({"file_path": "/r/a.md",
                            "notebook_path": "/r/n.ipynb"})
    check(got == ["/r/a.md", "/r/n.ipynb"],
          f"W4 both keys -> both recorded in WRITE_PATH_KEYS order "
          f"(got {got!r}, keys={tc.WRITE_PATH_KEYS!r})")


def main() -> int:
    print("=== touched_capture.py tool_input write-key extraction (W1..W4) ===")
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
