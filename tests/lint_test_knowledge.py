#!/usr/bin/env python3
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
ALLOWLIST_FILE = ROOT / "tests" / ".knowledge-allowlist"

TEST_NAME = re.compile(r"^(?:test_.*|.*_test)\.(?:py|sh)$")

EXCLUDED_DIRS = {
    "evals": "opt-in sampling harness that spends money -- not run by the "
             "ordinary change loop",
    "fixtures": "fixture data, not test code",
    "__pycache__": "build artifact",
    ".venv": "build artifact",
    "node_modules": "build artifact",
}

DIRECTIVE = re.compile(r"^#!|# noqa:|# type: ignore\[|# pragma|# ruff:")
PEP723_OPEN = re.compile(r"^#\s*///\s*script\s*$")
PEP723_CLOSE = re.compile(r"^#\s*///\s*$")

GITIGNORED_TREES = ("_projects/", "mode-orchestrator-runs/",
                    "analytics-expert-using-sql-runs/")
CITATION = re.compile(
    r"(?i)\b(?:see|per|spec:?|design|governed by|documented in|described in|"
    r"covers)\s+([A-Za-z0-9_./-]*\.md)\b")
SECTION_SIGN = re.compile(r"§")
COMMIT_HASH = re.compile(r"\bcommit [0-9a-f]{7,}\b")

SKIP_WALK = {".git", "_projects", "node_modules", ".venv", "tmp",
             "mode-orchestrator-runs", "analytics-expert-using-sql-runs",
             "notes", "__pycache__"}


def reachable_markdown() -> set[str]:
    names = set()
    stack = [ROOT]
    while stack:
        d = stack.pop()
        for entry in d.iterdir():
            if entry.is_dir():
                if entry.name not in SKIP_WALK:
                    stack.append(entry)
            elif entry.suffix == ".md":
                names.add(entry.name)
    return names


def scan_targets() -> tuple[list[Path], list[str]]:
    files, skipped = [], []
    for p in sorted(SKILLS.rglob("*")):
        if not p.is_file() or not TEST_NAME.match(p.name):
            continue
        rel = p.relative_to(ROOT)
        excluded = next((part for part in rel.parts[:-1] if part in EXCLUDED_DIRS),
                        None)
        if excluded:
            skipped.append(f"{rel.as_posix()} ({EXCLUDED_DIRS[excluded]})")
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


def docstrings_py(src: str) -> list[tuple[int, str]]:
    tree = ast.parse(src)
    out = []
    nodes = [tree] + [n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                        ast.ClassDef))]
    for node in nodes:
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            where = "module" if isinstance(node, ast.Module) else f"{node.name}()"
            out.append((first.lineno, f"{where}: {first.value.value.strip()[:80]}"))
    return out


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def comments_sh(src: str) -> list[tuple[int, str]]:
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


def unreachable_refs(line: str, markdown: set[str]) -> list[str]:
    hits = []
    if SECTION_SIGN.search(line):
        hits.append("section sign -- the document it indexes is not in this checkout")
    for tree in GITIGNORED_TREES:
        if tree in line:
            hits.append(f"gitignored tree: {tree}")
    for cited in CITATION.findall(line):
        if Path(cited).name not in markdown:
            hits.append(f"citation of a document not in this checkout: {cited}")
    if COMMIT_HASH.search(line):
        hits.append("commit hash")
    return hits


def main() -> int:
    files, skipped = scan_targets()
    markdown = reachable_markdown()
    failures: list[str] = []

    for p in files:
        src = p.read_text(encoding="utf-8")
        rel = p.relative_to(ROOT).as_posix()
        finder = comments_py if p.suffix == ".py" else comments_sh
        for line_no, text in finder(src):
            failures.append(
                f"R1 {rel}:{line_no}: comment outside the directive allowlist: "
                f"{text[:100]}")
        if p.suffix == ".py":
            for line_no, text in docstrings_py(src):
                failures.append(f"R1 {rel}:{line_no}: docstring: {text}")
        for i, line in enumerate(src.splitlines(), 1):
            for why in unreachable_refs(line, markdown):
                failures.append(f"R2 {rel}:{i}: {why}")

    allow = []
    if ALLOWLIST_FILE.exists():
        allow = [line.strip() for line
                 in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        failures = [f for f in failures if not any(a in f for a in allow)]

    print(f"scanned {len(files)} test files under skills/")
    for s in skipped:
        print(f"  out of scope: {s}")
    print(f"allowlist entries remaining: {len(allow)}"
          + ("" if allow else "  (no allowlist file -- migration complete)"))
    print("not seen by this gate: a citation phrased without one of its citation "
          "verbs; a document name written bare; a reference assembled at runtime "
          "from parts; every test file outside skills/ (plugins/ carry their own "
          "gates, tests/ at the root carries none).")

    if not files:
        print("GATE FAILED: scanned zero files. A gate that sees nothing is not a "
              "clean tree.")
        return 2

    if failures:
        print(f"\n{len(failures)} violation(s):")
        for f in failures:
            print(f"  {f}")
        return 1
    print("gate holds: no comments or docstrings outside the directive allowlist, "
          "no unreachable references.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
