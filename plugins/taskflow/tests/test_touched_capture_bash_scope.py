#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
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
    ("U1", "sed -i 's/a/b/' _projects/p/tasks/1_in_progress/t.md",
     ["_projects/p/tasks/1_in_progress/t.md"]),
    ("U1c", "sed -n '1,5p' _projects/p/tasks/1_in_progress/t.md", []),
    ("U2", "sed -i 's|X|Y|' _projects/p/project-notes/index.md",
     ["_projects/p/project-notes/index.md"]),
    ("U2c", "grep -n 'a|b' _projects/p/project-notes/index.md", []),
    ("U3", "sed -i -e 's/a/b/' f1.md f2.md", ["f1.md", "f2.md"]),
    ("U3c", "sed -i 's/a/b/'", []),
    ("U1d", 'sed -i \'s/a/b/\' "$f"', []),
    ("U1e", "sed -i 's/a/b/' *.md", []),
    ("U1f", "sed -i.bak 's/a/b/' f.md", ["f.md"]),
    ("U1g", "sed --in-place 's/a/b/' f.md", ["f.md"]),
    ("U1h", "sed -i '' 's/a/b/' f.md", ["f.md"]),
    ("U4", 'rm -rf /tmp/x\necho "=== gone ==="', ["/tmp/x"]),
    ("U4c", 'rm -rf /tmp/x && echo "=== gone ==="', ["/tmp/x"]),
    ("U5", "mv 'a;b.md' 'c;d.md'", ["a;b.md", "c;d.md"]),
    ("U5c", "echo 'x && y' > out.txt", ["out.txt"]),
    ("U7", "cd /abs/repo/_projects/p/project-notes && echo x >> index.md",
     ["index.md"]),
    ("U7c", "cd /abs && echo x >> _projects/p/project-notes/index.md",
     ["_projects/p/project-notes/index.md"]),
    ("U8c",
     "cat >> _projects/p/project-notes/x.md <<'EOF'\n19 -> 31 -> 34\nEOF",
     ["_projects/p/project-notes/x.md"]),
    ("U9", "sed -i 's/a/b/' f.md 2>err.txt", ["err.txt", "f.md"]),
    ("U9b", "sed -i 's/a/b/' f.md 2>&1", ["f.md"]),
    ("U9c", "sed -i 's/a/b/' f.md >out.txt", ["out.txt", "f.md"]),
    ("U9d", "sed -i 's/a/b/' f.md", ["f.md"]),
    ("U10", "mv a.md b.md 2>err.txt", ["err.txt", "a.md", "b.md"]),
    ("U10b", "cp a.md b.md 2>&1", ["a.md", "b.md"]),
    ("U10c", "mv a.md b.md", ["a.md", "b.md"]),
]


def test_cases() -> None:
    print("--- extract_bash_paths() over the bash-scope case set ---")
    for cid, cmd, expected in CASES:
        got = tc.extract_bash_paths(cmd)
        check(got == expected, f"{cid} {cmd!r} -> {expected!r} (got {got!r})")


def test_u8a_python_c_control() -> None:
    print("--- python -c open() (D2 control) ---")
    cmd = "python -c \"import io; io.open('_projects/p/project-notes/x.md','w')\""
    got = tc.extract_bash_paths(cmd)
    check("_projects/p/project-notes/x.md" not in got,
          f"U8a {cmd!r}: '_projects/p/project-notes/x.md' not in {got!r}")


def test_u8b_noclobber_control() -> None:
    print("--- echo x >| <path> (control) ---")
    cmd = "echo x >| _projects/p/project-notes/x.md"
    got = tc.extract_bash_paths(cmd)
    check(got == [],
          "U8b (control -- a measurement, not a substrate limit; re-open if a "
          f"genuine `>|` redirection is ever observed): got {got!r}")


def test_u6_shlex_gate() -> None:
    print("--- shlex parse error gate ---")

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
          f"U6c stderr is empty -- the diagnostic is gated to a stage whose first word "
          f"is a recognised write verb (mv/cp/rm/tee/sed) (got {err2!r})")


def main() -> int:
    print("=== touched_capture.py bash-scope tests ===")
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
