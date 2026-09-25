#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / "plugins" / "taskflow" / "hooks" / "notes_scan_gate.py"
HOOKS_JSON = REPO_ROOT / "plugins" / "taskflow" / "hooks" / "hooks.json"

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


def run_hook(path: str, pattern: str, cwd: str | None = None) -> str:
    payload = {"tool_name": "Grep", "tool_input": {"path": path, "pattern": pattern}}
    if cwd is not None:
        payload["cwd"] = cwd
    cp = subprocess.run(
        ["uv", "run", "--no-project", "python", str(HOOK)],
        input=json.dumps(payload), capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=dict(os.environ),
    )
    if cp.returncode != 0:
        raise SystemExit(f"hook exited {cp.returncode}: {cp.stderr}")
    return cp.stdout.strip()


def decision(out: str) -> str:
    if not out:
        return "allow"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


def test_four_arms() -> None:
    print("--- deny only the corpus scan ---")
    check(decision(run_hook("_projects/p/project-notes/specs", "^anchors:")) == "deny",
          "arm a: a non-symbol Grep aimed into project-notes is the forbidden corpus scan")
    check(decision(run_hook("_projects/p/project-notes/specs", "::normalize")) == "allow",
          "arm b: a ::<name> Grep is step 3 of the symbol route and must pass")
    check(decision(run_hook("src", "^anchors:")) == "allow",
          "arm c: the same pattern outside project-notes is not this rule's business")
    check(decision(run_hook("_projects/p/project-notes/_archive", "^anchors:")) == "deny",
          "arm d: _archive/ is still corpus, so scanning it is the same breach")


def test_reason_carries_the_rule() -> None:
    print("--- the deny explains itself ---")
    out = run_hook("_projects/p/project-notes/specs", "^summary:")
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    check("exactly three" in reason,
          "the reason states the rule, so a denied model knows what is allowed instead of retrying blind")
    check("query-lsp" in reason,
          "the reason names the route that produces a legitimate ::<name>")
    check("your task" in reason,
          "the reason names a file the task gives by path as a sanctioned read")
    check("another way" in reason,
          "the reason forbids running the refused search with another tool")


def test_shapes_that_must_not_deny() -> None:
    print("--- shapes that must not deny ---")
    check(decision(run_hook("", "^anchors:")) == "allow",
          "a Grep with no path is not aimed at the corpus")
    check(decision(run_hook("_projects/p/project-notes-archive/x", "^anchors:")) == "allow",
          "a sibling directory whose name merely starts with project-notes is a different tree")
    check(decision(run_hook("_projects\\p\\project-notes\\specs", "^anchors:")) == "deny",
          "a Windows-separator path is the same tree and must not slip past on separator alone")


def test_one_existing_file_passes() -> None:
    print("--- a Grep confined to one existing note passes ---")
    with tempfile.TemporaryDirectory() as tmp:
        specs = Path(tmp) / "_projects" / "p" / "project-notes" / "specs"
        specs.mkdir(parents=True)
        note = specs / "x.md"
        note.write_text("anchors: []\n", encoding="utf-8")
        fwd = note.as_posix()
        check(decision(run_hook(fwd, "^anchors:")) == "allow",
              "a Grep inside one existing note file searches no other note, so it is no corpus scan")
        check(decision(run_hook(fwd.replace("/", "\\"), "^anchors:")) == "allow",
              "a backslash-separated path to one existing note file passes the same way")
        check(decision(run_hook("_projects/p/project-notes/specs/x.md", "^anchors:", cwd=tmp)) == "allow",
              "a relative path is resolved against the payload cwd before it is judged")
        if os.name == "nt" and len(fwd) > 2 and fwd[1] == ":":
            msys = "/" + fwd[0].lower() + fwd[2:]
            check(decision(run_hook(msys, "^anchors:")) == "allow",
                  "an MSYS /c/ path to one existing note file passes on Windows")
        check(decision(run_hook(specs.as_posix(), "^anchors:")) == "deny",
              "an existing notes directory is still the corpus scan")
        check(decision(run_hook("_projects/p/project-notes/specs/x.md", "^anchors:")) == "deny",
              "a relative path with no payload cwd cannot be shown to be one file, so it is denied")


def test_no_path_judged_by_cwd() -> None:
    print("--- a Grep with no path searches its cwd ---")
    with tempfile.TemporaryDirectory() as tmp:
        notes = Path(tmp) / "_projects" / "p" / "project-notes"
        notes.mkdir(parents=True)
        check(decision(run_hook("", "^anchors:", cwd=notes.as_posix())) == "deny",
              "with no path the search root is the cwd, and a cwd inside project-notes is the corpus")
        check(decision(run_hook("", "^anchors:", cwd=tmp)) == "allow",
              "a root above project-notes is left alone: judging it would deny ordinary searches of the project")


def test_registered_on_grep() -> None:
    print("--- registered where it fires ---")
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    entries = [e for e in cfg["hooks"]["PreToolUse"]
               if any("notes_scan_gate.py" in h["command"] for h in e["hooks"])]
    check(len(entries) == 1 and entries[0]["matcher"] == "Grep",
          f"the gate is registered exactly once, on Grep (got {[e['matcher'] for e in entries]})")


def main() -> int:
    print("=== notes_scan_gate.py unit tests ===")
    test_four_arms()
    test_reason_carries_the_rule()
    test_shapes_that_must_not_deny()
    test_one_existing_file_passes()
    test_no_path_judged_by_cwd()
    test_registered_on_grep()
    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
