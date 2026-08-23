#!/usr/bin/env python3
"""Pre-test gate for plugins/taskflow/tests/.

R1  No comments in a test file, except the enumerated directive allowlist.
R2  No unreachable references, on every line -- comments, test names, assertion
    messages and docstrings alike.

Both are grep-level checks. Run this before running the suites:

    uv run --no-project python plugins/taskflow/scripts/lint_test_knowledge.py

Exit 0 = gate holds, 1 = violation, 2 = the gate scanned nothing (a coverage
failure, not a clean tree).
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
RATCHET = TESTS / "test_sandbox_guard_ratchet.py"
ALLOWLIST_FILE = TESTS / ".knowledge-allowlist"

# Out of scope: instruments built to answer one question that the normal change
# loop does not run. Named here so a reader can see they were decided, not missed.
EXCLUDED_DIRS = {
    "race": "probe / race harness -- not run by the ordinary change loop",
    "fixtures": "fixture data, not test code",
    "__pycache__": "build artifact",
}

# Directive allowlist. Enumerated by grep over the tree, never from memory.
DIRECTIVE = re.compile(r"^#!|# noqa:|# type: ignore\[")
PEP723_OPEN = re.compile(r"^#\s*///\s*script\s*$")
PEP723_CLOSE = re.compile(r"^#\s*///\s*$")

# R2 patterns: references a reader holding only this checkout cannot open.
# `_projects/`, `mode-orchestrator-runs/` and `notes/` are gitignored; `§`, plan
# and run-directory filenames, and spec-scoped ids live only inside them.
UNREACHABLE = [
    (re.compile(r"§"), "section sign -- the document it indexes is not in this checkout"),
    (re.compile(r"mode-orchestrator-runs/"), "run directory (gitignored)"),
    (re.compile(r"\b\d{2}[a-z]?-(?:plan|debug|execute|review|review-dev|decision)\.md\b"),
     "run-directory document (gitignored)"),
    (re.compile(r"(?i)\b(?:see|per|spec:?|design|governed by|documented in|described in|covers)\s+"
                r"[A-Za-z0-9_./-]*\.md\b"), "citation of a document by name"),
    (re.compile(r"\b(?:T-[A-Z0-9]+-\d+[a-z]?|[AB]-AC\d+[a-z]?|AC-\d+[a-z]?|F-[A-Z0-9]+|INV-\d|"
                r"B-m\d|B-c\d|D-\d|ADD-\d|OBS\d-[A-Z]+)\b"),
     "spec-scoped id -- defined only in a document outside this checkout"),
    (re.compile(r"\bcommit [0-9a-f]{7,}\b"), "commit hash"),
]


def scan_targets() -> tuple[list[Path], list[str]]:
    files, skipped = [], []
    for p in sorted(TESTS.rglob("*")):
        if p.suffix not in (".py", ".sh") or not p.is_file():
            continue
        rel = p.relative_to(TESTS)
        top = rel.parts[0] if len(rel.parts) > 1 else ""
        if top in EXCLUDED_DIRS:
            skipped.append(f"{rel.as_posix()} ({EXCLUDED_DIRS[top]})")
            continue
        files.append(p)
    return files, skipped


def pep723_lines(lines: list[str]) -> set[int]:
    for i, l in enumerate(lines, 1):
        if PEP723_OPEN.match(l.strip()):
            for j in range(i + 1, len(lines) + 1):
                if PEP723_CLOSE.match(lines[j - 1].strip()):
                    return set(range(i, j + 1))
    return set()


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def comments_py(path: Path, src: str) -> list[tuple[int, str]]:
    protected = pep723_lines(src.splitlines())
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.COMMENT:
            continue
        if tok.start[0] in protected or DIRECTIVE.search(tok.string):
            continue
        out.append((tok.start[0], tok.string.strip()))
    return out


def comments_sh(path: Path, src: str) -> list[tuple[int, str]]:
    """Whole-line and trailing comments outside heredoc bodies.

    A heredoc body is fixture data, not source: a markdown `# heading` inside one
    is content under test and never a comment.
    """
    out, delim, strip_tabs = [], None, False
    for i, line in enumerate(src.splitlines(), 1):
        if delim is not None:
            probe = line.lstrip("\t") if strip_tabs else line
            if probe.strip() == delim:
                delim = None
            continue
        s = line.strip()
        if s.startswith("#"):
            if not DIRECTIVE.search(s):
                out.append((i, s))
        else:
            state, pos = None, None
            for j, ch in enumerate(line):
                if state is None:
                    if ch in "'\"":
                        state = ch
                    elif ch == "#" and j > 0 and line[j - 1] in " \t":
                        pos = j
                        break
                elif ch == state:
                    state = None
            if pos is not None and not DIRECTIVE.search(line[pos:]):
                out.append((i, line[pos:].strip()))
        m = HEREDOC.search(line)
        if m:
            delim, strip_tabs = m.group(2), "<<-" in line
    return out


def main() -> int:
    files, skipped = scan_targets()
    failures: list[str] = []

    for p in files:
        src = p.read_text(encoding="utf-8")
        rel = p.relative_to(ROOT).as_posix()
        finder = comments_py if p.suffix == ".py" else comments_sh
        for line_no, text in finder(p, src):
            failures.append(f"R1 {rel}:{line_no}: comment outside the directive allowlist: {text[:100]}")
        for i, line in enumerate(src.splitlines(), 1):
            for pat, why in UNREACHABLE:
                m = pat.search(line)
                if m:
                    failures.append(f"R2 {rel}:{i}: {why}: {m.group(0)!r}")

    allow = []
    if ALLOWLIST_FILE.exists():
        allow = [l.strip() for l in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]
        failures = [f for f in failures if not any(a in f for a in allow)]

    print(f"scanned {len(files)} test files under {TESTS.relative_to(ROOT).as_posix()}/")
    for s in skipped:
        print(f"  out of scope: {s}")
    print(f"allowlist entries remaining: {len(allow)}"
          + ("" if allow else "  (no allowlist file -- migration complete)"))
    print("not seen by this gate: a citation phrased without one of its citation verbs and "
          "without a section sign; a bare gitignored path such as `_projects/<p>/project-notes/x.md`, "
          "which fixtures spell the same way as a citation would; a reference assembled at "
          "runtime from parts; anything outside the scanned set listed above.")

    if not files:
        print("GATE FAILED: scanned zero files. A gate that sees nothing is not a clean tree.")
        return 2

    ratchet = subprocess.run(
        [sys.executable, str(RATCHET)], capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    print(f"sandbox-guard ratchet: {'holds' if ratchet.returncode == 0 else 'BROKEN'}")
    if ratchet.returncode != 0:
        print(ratchet.stdout)
        print(ratchet.stderr, file=sys.stderr)

    if failures:
        print(f"\n{len(failures)} violation(s):")
        for f in failures:
            print(f"  {f}")
        return 1
    if ratchet.returncode != 0:
        return 1
    print("gate holds: no comments outside the directive allowlist, no unreachable references.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
