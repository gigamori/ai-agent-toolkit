#!/usr/bin/env python3
"""Unit tests for scripts/pj_rules.py (the /pj-rules skill's deterministic
helper — spec §9, review findings M2/M3/M4).

Covers:
  - M4: `show` line-count is the frontmatter-stripped body; cap read from
    frontmatter `max_lines` (default 100); `over_cap` set correctly.
  - `show` heading extraction: `## ` only, fenced-code `##` excluded, `###`+
    ignored (same contract as hooks/session_init.py's extract_headings).
  - `show` on a missing rules.md: exit 1, `exists: false`.
  - M2: `reset-indexed` is merge-preserving — unrelated state fields
    (progress_capture_done, exec_bind, ...) survive; project_rules_indexed
    becomes "".
  - `reset-indexed` error handling: missing file / non-dict JSON -> exit 2.

stdlib only. Run with:  uv run python plugins/taskflow/tests/test_pj_rules_script.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pj_rules.py"

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


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        # Strict UTF-8, not the platform default: the script's output is what
        # the assertions read, so a substituting decode would hide a defect
        # instead of failing on it.
        encoding="utf-8",
    )


def test_show_missing_file() -> None:
    print("[show] missing rules.md")
    with tempfile.TemporaryDirectory() as td:
        proc = run("show", td)
        if proc.returncode == 1:
            ok("exit code 1 for missing rules.md")
        else:
            bad(f"expected exit 1, got {proc.returncode}: {proc.stdout} {proc.stderr}")
        if "exists: false" in proc.stdout:
            ok("reports exists: false")
        else:
            bad(f"missing 'exists: false' in output: {proc.stdout!r}")


def test_show_headings_and_cap() -> None:
    print("[show] headings, cap, fence exclusion")
    with tempfile.TemporaryDirectory() as td:
        rules = Path(td) / "rules.md"
        rules.write_text(
            "---\n"
            "inject_every_turn: true\n"
            "max_lines: 3\n"
            "---\n"
            "# Rules\n"
            "\n"
            "## Real rule one\n"
            "Body.\n"
            "\n"
            "## Real rule two\n"
            "```\n"
            "## Fenced heading — not a rule\n"
            "```\n"
            "### Sub-heading — not level-2\n",
            encoding="utf-8",
        )
        proc = run("show", td)
        if proc.returncode == 0:
            ok("exit code 0 for existing rules.md")
        else:
            bad(f"expected exit 0, got {proc.returncode}: {proc.stderr}")
        if "headings: 2" in proc.stdout:
            ok("counts exactly 2 real headings (fence + ### excluded)")
        else:
            bad(f"heading count wrong: {proc.stdout!r}")
        if "- Real rule one" in proc.stdout and "- Real rule two" in proc.stdout:
            ok("lists real heading titles")
        else:
            bad(f"heading titles missing: {proc.stdout!r}")
        if "Fenced heading" in proc.stdout:
            bad("fenced heading leaked into output")
        else:
            ok("fenced heading excluded from output")
        if "over_cap: true" in proc.stdout:
            ok("over_cap true when body exceeds max_lines")
        else:
            bad(f"expected over_cap: true: {proc.stdout!r}")
        if "inject_every_turn: true" in proc.stdout:
            ok("reports inject_every_turn from frontmatter")
        else:
            bad(f"inject_every_turn not reported: {proc.stdout!r}")


def test_show_under_cap_and_default_cap() -> None:
    print("[show] default cap (no frontmatter) and under-cap")
    with tempfile.TemporaryDirectory() as td:
        rules = Path(td) / "rules.md"
        rules.write_text("# Rules\n\n## Only rule\nShort.\n", encoding="utf-8")
        proc = run("show", td)
        if "cap: 100" in proc.stdout:
            ok("defaults max_lines to 100 when frontmatter absent")
        else:
            bad(f"default cap wrong: {proc.stdout!r}")
        if "over_cap: false" in proc.stdout:
            ok("over_cap false when under cap")
        else:
            bad(f"expected over_cap: false: {proc.stdout!r}")


def test_reset_indexed_merge_preserving() -> None:
    print("[reset-indexed] merge-preserving (M2)")
    with tempfile.TemporaryDirectory() as td:
        state_file = Path(td) / "state.json"
        original = {
            "project": "foo",
            "rules_loaded": True,
            "indexed_project": "foo",
            "guidelines_loaded": True,
            "project_rules_indexed": "foo",
            "origin": "cc",
            "progress_capture_done": True,
            "exec_bind": ["a.md", "b.md"],
        }
        state_file.write_text(json.dumps(original), encoding="utf-8")
        proc = run("reset-indexed", str(state_file))
        if proc.returncode == 0:
            ok("exit code 0 on successful reset")
        else:
            bad(f"expected exit 0, got {proc.returncode}: {proc.stderr}")
        result = json.loads(state_file.read_text(encoding="utf-8"))
        if result.get("project_rules_indexed") == "":
            ok("project_rules_indexed reset to empty string")
        else:
            bad(f"project_rules_indexed not reset: {result!r}")
        untouched = {
            "project": "foo",
            "rules_loaded": True,
            "indexed_project": "foo",
            "guidelines_loaded": True,
            "origin": "cc",
            "progress_capture_done": True,
            "exec_bind": ["a.md", "b.md"],
        }
        if all(result.get(k) == v for k, v in untouched.items()):
            ok("all unrelated fields (incl. progress_capture_done, exec_bind) preserved")
        else:
            bad(f"unrelated fields clobbered: {result!r}")


def test_reset_indexed_errors() -> None:
    print("[reset-indexed] error handling")
    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "does-not-exist.json"
        proc = run("reset-indexed", str(missing))
        if proc.returncode == 2:
            ok("exit code 2 for missing state file")
        else:
            bad(f"expected exit 2, got {proc.returncode}")

        not_dict = Path(td) / "list.json"
        not_dict.write_text("[1, 2, 3]", encoding="utf-8")
        proc = run("reset-indexed", str(not_dict))
        if proc.returncode == 2:
            ok("exit code 2 for non-dict JSON")
        else:
            bad(f"expected exit 2, got {proc.returncode}")


def main() -> int:
    test_show_missing_file()
    test_show_headings_and_cap()
    test_show_under_cap_and_default_cap()
    test_reset_indexed_merge_preserving()
    test_reset_indexed_errors()

    print("")
    if FAIL == 0:
        print(f"All {PASS} tests passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
