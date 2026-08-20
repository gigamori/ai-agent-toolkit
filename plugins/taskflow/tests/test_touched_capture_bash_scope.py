#!/usr/bin/env python3
"""Unit tests for the bash-parse-gap / cd-relative-target scope decisions
(mode-orchestrator-runs/2026-08-19_touched-capture-bash-parse-gap-cd-target/
02-plan.md §5.2, as amended by 03-review-dev.md F3).

Covers, against the REAL `extract_bash_paths`:
  - D1 (`sed -i`/`--in-place` recognition, `_sed_is_inplace`/`_sed_operands`),
    including the F4 unexpanded-shell-metacharacter guard.
  - P1 (an unquoted newline is a verb-loop stage boundary, `_split_stages`).
  - P2 (the chain/pipe split is quote-aware, `_split_stages`).
  - §2.3 (the `shlex parse error` diagnostic is gated to stages whose first
    word is a recognised write verb).
  - D5 (a bare relative target written after a `cd` is recorded VERBATIM,
    never joined against any guessed base) -- both directions.
  - D2/D3/D4 controls: `python -c open()`, `>|`, and a heredoc BODY remain
    deliberately unrecognised (unchanged by this turn).

Every id below is the plan's own id (U1..U8c); each "expected" value is the
FULL `extract_bash_paths()` return, not a membership test, for the same
reason test_touched_capture_quoted_redirect.py's CASES table is: a
quote/stage-aware scan is judged as much by what it does NOT record as by
what it does.

Platform note (03-review-dev.md F3, verified directly against source):
`touched_capture.py` lexes with `shlex.split(stage, posix=(os.name != 'nt'))`
-- on this platform (win32, `os.name == 'nt'`) that is `posix=False`, which
RETAINS quote characters in a token (`shlex.split("mv 'a;b.md' c.md",
posix=False)` -> `["mv", "'a;b.md'", "c.md"]`, not `["mv", "a;b.md", "c.md"]`).
`extract_bash_paths`'s verb loop now strips a token's surrounding quotes
before recording it (matching `extract_redirect_targets`'s existing rule), so
every expected value below is the DEQUOTED path and holds on both platforms
-- it does not encode a posix=False-only expectation.

Sandbox (carried verbatim from test_touched_capture_quoted_redirect.py, same
conclusion, same reason): §8's claim that `e2e_state_dir_sandbox` is
non-applicable "because this hook resolves no `_projects` path and writes
nothing" is FALSE -- touched_capture.py resolves STATE_ROOT / PROGRESS_ROOT /
STATE_DIR at module scope and `main()` appends to
`<STATE_DIR>/<session_id>.touched`. Since 2026-08-19 that module-scope
resolution is `_find_state_root(os.getcwd()) or os.getcwd()`, so merely
IMPORTING this module walks the cwd's ancestors (the cwd itself included)
looking for a `_projects/_state` directory -- read-only `isdir` probes, but
they do reach outside the cwd, and run from inside the repo they resolve to
the real `_projects/_state`. The conclusion survives only for this test's
exact shape: every case below calls `extract_bash_paths` directly and NEVER
`main()`, so nothing is written wherever STATE_DIR landed. Do not exercise
these cases end-to-end through the hook -- the E2E half lives in
`test_touched_capture_bash_scope_e2e.py` (§5.3, a later turn).

stdlib only. Run with:
    uv run --no-project python plugins/taskflow/tests/test_touched_capture_bash_scope.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import io
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


# (id, command, expected extract_bash_paths() result) -- full-list, not
# membership. Ids and commands are the plan's own (§5.2).
CASES = [
    # --- D1: sed -i recognition (§1.1) --------------------------------------
    ("U1", "sed -i 's/a/b/' _projects/p/tasks/1_in_progress/t.md",
     ["_projects/p/tasks/1_in_progress/t.md"]),
    ("U1c", "sed -n '1,5p' _projects/p/tasks/1_in_progress/t.md", []),
    # proves P2: a naive `|` split would shatter the script here
    ("U2", "sed -i 's|X|Y|' _projects/p/project-notes/index.md",
     ["_projects/p/project-notes/index.md"]),
    ("U2c", "grep -n 'a|b' _projects/p/project-notes/index.md", []),
    ("U3", "sed -i -e 's/a/b/' f1.md f2.md", ["f1.md", "f2.md"]),
    ("U3c", "sed -i 's/a/b/'", []),
    # F4 metacharacter guard, scoped to sed only (06-review-dev.md F-B)
    ("U1d", 'sed -i \'s/a/b/\' "$f"', []),
    ("U1e", "sed -i 's/a/b/' *.md", []),
    # remaining D1 recognition forms (06-review-dev.md F-B)
    ("U1f", "sed -i.bak 's/a/b/' f.md", ["f.md"]),
    ("U1g", "sed --in-place 's/a/b/' f.md", ["f.md"]),
    # F-A fix pin: BSD/macOS `sed -i ''` no longer records the script itself
    # as a written path (06-review-dev.md F-A)
    ("U1h", "sed -i '' 's/a/b/' f.md", ["f.md"]),
    # --- P1: unquoted newline is a stage boundary ---------------------------
    ("U4", 'rm -rf /tmp/x\necho "=== gone ==="', ["/tmp/x"]),
    ("U4c", 'rm -rf /tmp/x && echo "=== gone ==="', ["/tmp/x"]),
    # --- P2: quote-aware chain/pipe split ------------------------------------
    ("U5", "mv 'a;b.md' 'c;d.md'", ["a;b.md", "c;d.md"]),
    ("U5c", "echo 'x && y' > out.txt", ["out.txt"]),
    # --- D5: a bare relative target after `cd` is recorded VERBATIM --------
    ("U7", "cd /abs/repo/_projects/p/project-notes && echo x >> index.md",
     ["index.md"]),
    ("U7c", "cd /abs && echo x >> _projects/p/project-notes/index.md",
     ["_projects/p/project-notes/index.md"]),
    # --- D2/D3/D4 controls: deliberately NOT closed by this turn -----------
    ("U8c",
     "cat >> _projects/p/project-notes/x.md <<'EOF'\n19 -> 31 -> 34\nEOF",
     ["_projects/p/project-notes/x.md"]),
]


def test_cases() -> None:
    print("--- extract_bash_paths() over the bash-scope case set ---")
    for cid, cmd, expected in CASES:
        got = tc.extract_bash_paths(cmd)
        check(got == expected, f"{cid} {cmd!r} -> {expected!r} (got {got!r})")


def test_u8a_python_c_control() -> None:
    """D2 control: `python -c open()` remains unrecognised (substrate
    rejection, 02-plan.md §1.2 -- not a frequency call, so no reversal
    trigger applies here the way F7 requires for D3)."""
    print("--- U8a: python -c open() (D2 control) ---")
    cmd = "python -c \"import io; io.open('_projects/p/project-notes/x.md','w')\""
    got = tc.extract_bash_paths(cmd)
    check("_projects/p/project-notes/x.md" not in got,
          f"U8a {cmd!r}: '_projects/p/project-notes/x.md' not in {got!r}")


def test_u8b_noclobber_control() -> None:
    """D3 control: `>|` remains unrecognised. F7 (03-review-dev.md): this pin
    records a MEASUREMENT, not a substrate limit -- `>|` measured 0 genuine
    occurrences in 13,824 Bash commands from a single installation,
    2026-08-20; re-open D3 if a genuine `>|` redirection is observed in any
    consumer's corpus."""
    print("--- U8b: echo x >| <path> (D3 control) ---")
    cmd = "echo x >| _projects/p/project-notes/x.md"
    got = tc.extract_bash_paths(cmd)
    check(got == [],
          "U8b (D3 control -- measurement, not substrate limit; re-open if a "
          f"genuine `>|` is observed elsewhere; see F7): got {got!r}")


def test_u6_shlex_gate() -> None:
    """§2.3: the shlex-failure diagnostic is gated to a stage whose first
    word is a recognised write verb (mv/cp/rm/tee/sed)."""
    print("--- U6/U6c: shlex parse error gate ---")

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        got = tc.extract_bash_paths('mv "a b.md c.md')
    err = buf.getvalue()
    check(got == [], f"U6 result == [] (got {got!r})")
    check("[touched_capture] shlex parse error" in err,
          f"U6 stderr carries the diagnostic (got {err!r})")

    buf2 = io.StringIO()
    with contextlib.redirect_stderr(buf2):
        got2 = tc.extract_bash_paths('grep -n "a b.md')
    err2 = buf2.getvalue()
    check(got2 == [], f"U6c result == [] (got {got2!r})")
    check(err2 == "",
          f"U6c stderr is empty -- gate suppresses the non-write-verb case "
          f"(got {err2!r})")


def main() -> int:
    print("=== touched_capture.py bash-scope tests (D1/D2/D3/D4/D5/P1/P2) ===")
    test_cases()
    print()
    test_u8a_python_c_control()
    print()
    test_u8b_noclobber_control()
    print()
    test_u6_shlex_gate()
    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
