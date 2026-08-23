#!/usr/bin/env python3
"""Pre-test gate for the llm-wiki test suites.

R1  No comments and no docstrings in a test file, except the enumerated
    directive allowlist. In a runner's own configuration R1 still applies, but
    one comment form passes: the pointer template `See <path>.md, "<heading>".`,
    whose path must exist. The heading is not checked -- checking it would turn
    a document edit into a build failure.
R2  No unreachable references, on every line -- comments, docstrings, test names
    and assertion messages alike.

R2 and the comment half of R1 are textual. The docstring half is not: a grep for
a triple-quoted string cannot tell a docstring from a fixture string assigned to
a name, so this scans the positions the language defines (a module's first
statement, and the first statement in a def or class body) through `ast` and
leaves every other string literal alone. Run this before running the suite:

    uv run --no-project python plugins/llm-wiki/scripts/lint_test_knowledge.py

Exit 0 = gate holds, 1 = violation, 2 = the gate scanned nothing (a coverage
failure, not a clean tree).
"""
from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_DIRS = [ROOT / "tests", ROOT / "scripts" / "tests"]
ALLOWLIST_FILE = ROOT / "tests" / ".knowledge-allowlist"

EXCLUDED_DIRS = {
    "__pycache__": "build artifact",
}

# A runner's own configuration asserts no contract of its own, so it is not a
# test -- but the same agent reads it, and a constraint written there misleads it
# exactly as one in a test would. R1 reaches it; only the pointer template passes.
CONFIG_FILE = re.compile(
    r"^(?:vitest(?:\.[a-z0-9-]+)?\.config\.[cm]?[jt]s|conftest\.py|test-setup\.[cm]?[jt]s)$")
POINTER = re.compile(r'^See ([A-Za-z0-9._/-]+\.md)(?:, "[^"]+")?\.$')

# Directive allowlist. Enumerated by grep over the tree, never from memory.
DIRECTIVE = re.compile(r"^#!|# noqa:|# type: ignore\[")
PEP723_OPEN = re.compile(r"^#\s*///\s*script\s*$")
PEP723_CLOSE = re.compile(r"^#\s*///\s*$")

# A pointer is a citation by construction, so R2's citation pattern would reject
# the one comment R1 permits in a runner's configuration. An accepted pointer line
# is exempt from THAT pattern only; every other R2 pattern still applies to it.
CITATION = re.compile(r"(?i)\b(?:see|per|spec:?|design|governed by|documented in|described in|"
                r"covers|mirrors|traced to)\s+[A-Za-z0-9_./-]*\.md\b")

# R2 patterns: references a reader holding only this checkout cannot open. The
# design and plan documents live under `_projects/llm-wiki/project-notes/`,
# which is gitignored, and the ids below are defined only inside them.
UNREACHABLE = [
    (re.compile(r"§"), "section sign -- the document it indexes is not in this checkout"),
    (re.compile(r"\b(?:D-?\d{1,2}[a-z]?|D-[a-z]|DEC-[A-Za-z0-9-]+|D-Q\d|P\d/[A-Za-z0-9+/-]+|"
                r"AC-[A-Z]?\d+[a-z]?|OI-\d+|INV-\d+|[A-Z]-\d+[a-z]?)\b"),
     "spec-scoped id -- defined only in a document outside this checkout"),
    (CITATION, "citation of a document by name"),
    (re.compile(r"\b[A-Za-z0-9_./-]+\.(?:py|ts|sql|md|json):\d+"),
     "file-and-line reference -- drifts on the next edit"),
    (re.compile(r"\bL\d{3,}(?:-\d+)?\b"), "line-number reference"),
    (re.compile(r"\bcommit [0-9a-f]{7,}\b"), "commit hash"),
]


def scan_targets() -> tuple[list[Path], list[str]]:
    files, skipped = [], []
    for base in TEST_DIRS:
        if not base.is_dir():
            skipped.append(f"{base.relative_to(ROOT).as_posix()}/ (absent)")
            continue
        for p in sorted(base.rglob("*")):
            if p.suffix not in (".py", ".sh") or not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            if any(part in EXCLUDED_DIRS for part in rel.parts):
                part = next(x for x in rel.parts if x in EXCLUDED_DIRS)
                skipped.append(f"{rel.as_posix()} ({EXCLUDED_DIRS[part]})")
                continue
            files.append(p)
    return files, skipped


def pep723_lines(lines: list[str]) -> set[int]:
    for i, line in enumerate(lines, 1):
        if PEP723_OPEN.match(line.strip()):
            for j in range(i + 1, len(lines) + 1):
                if PEP723_CLOSE.match(lines[j - 1].strip()):
                    return set(range(i, j + 1))
    return set()


def comments_py(src: str) -> list[tuple[int, str]]:
    protected = pep723_lines(src.splitlines())
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.COMMENT:
            continue
        if tok.start[0] in protected or DIRECTIVE.search(tok.string):
            continue
        out.append((tok.start[0], tok.string.strip()))
    return out


def docstrings_py(src: str) -> list[tuple[int, int, str]]:
    """Module, class and function docstrings -- the language-defined positions only."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.append((first.lineno, first.end_lineno or first.lineno,
                        " ".join(first.value.value.split())))
    return out


def flatten(text: str) -> str:
    return " ".join(re.sub(r"^#+\s*", "", text).split())


def pointer_target_exists(target: str) -> bool:
    return (ROOT / target).is_file() or (ROOT.parent.parent / target).is_file()


def main() -> int:
    files, skipped = scan_targets()
    failures: list[str] = []
    config_seen: list[str] = []

    for p in files:
        src = p.read_text(encoding="utf-8")
        rel = p.relative_to(ROOT).as_posix()
        prose = [(str(line_no), text) for line_no, text in comments_py(src)]
        prose += [(f"{a}" if a == b else f"{a}-{b}", text) for a, b, text in docstrings_py(src)]
        pointer_lines: set[int] = set()
        if CONFIG_FILE.match(p.name):
            config_seen.append(rel)
            for span, text in prose:
                m = POINTER.match(flatten(text))
                if not m:
                    failures.append(
                        f"R1 {rel}:{span}: not the pointer template: {flatten(text)[:100]}")
                elif not pointer_target_exists(m.group(1)):
                    failures.append(
                        f"R1 {rel}:{span}: pointer target does not exist: {m.group(1)}")
                else:
                    bounds = [int(x) for x in span.split("-")]
                    pointer_lines.update(range(bounds[0], bounds[-1] + 1))
        else:
            for line_no, text in comments_py(src):
                failures.append(
                    f"R1 {rel}:{line_no}: comment outside the directive allowlist: {text[:100]}")
            for start, end, text in docstrings_py(src):
                span = f"{start}" if start == end else f"{start}-{end}"
                failures.append(f"R1 {rel}:{span}: docstring: {text[:100]}")
        for i, line in enumerate(src.splitlines(), 1):
            for pat, why in UNREACHABLE:
                if pat is CITATION and i in pointer_lines:
                    continue
                m = pat.search(line)
                if m:
                    failures.append(f"R2 {rel}:{i}: {why}: {m.group(0)!r}")

    allow = []
    if ALLOWLIST_FILE.exists():
        allow = [line.strip() for line in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        failures = [f for f in failures if not any(a in f for a in allow)]

    print(f"scanned {len(files)} test files under "
          + ", ".join(d.relative_to(ROOT).as_posix() + "/" for d in TEST_DIRS))
    for s in skipped:
        print(f"  out of scope: {s}")
    print("  pointer-only (runner config, asserts no contract): "
          + (", ".join(sorted(config_seen)) if config_seen else "none"))
    print("  a pointer's path is resolved against the plugin root and against the "
          "repository root; its heading is never checked.")
    print(f"allowlist entries remaining: {len(allow)}"
          + ("" if allow else "  (no allowlist file -- migration complete)"))
    print("not seen by this gate: a citation phrased without one of its citation verbs and "
          "without a section sign; a bare gitignored path such as "
          "`_projects/<p>/project-notes/x.md`, which fixtures spell the same way a citation "
          "would; a spec item referred to as `item<N>`, a spelling ordinary prose also uses; "
          "a reference assembled at runtime from parts; anything outside the scanned set "
          "listed above.")

    if not files:
        print("GATE FAILED: scanned zero files. A gate that sees nothing is not a clean tree.")
        return 2

    if failures:
        print(f"\n{len(failures)} violation(s):")
        for f in failures:
            print(f"  {f}")
        return 1
    print("gate holds: no comments outside the directive allowlist, no docstrings, "
          "no unreachable references.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
