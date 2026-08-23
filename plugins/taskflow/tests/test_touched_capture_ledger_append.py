#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
HOOK = REPO_ROOT / "plugins" / "taskflow" / "hooks" / "touched_capture.py"
REAL_STATE = REPO_ROOT / "_projects" / "_state"

SID = "ledgapp01-1111-2222-3333-444455556666"
TASK_REL = "_projects/demo/tasks/1_in_progress/2026-01-01_demo-task.md"
NB_REL = "_projects/demo/nb/2026-01-01_demo.ipynb"

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


def die(msg: str) -> None:
    print(f"  ABORT: {msg}")
    raise SystemExit(2)



def ancestors_with_state(start: Path) -> list[str]:
    hits: list[str] = []
    d = start.resolve()
    while True:
        if (d / "_projects" / "_state").is_dir():
            hits.append(str(d))
        if d.parent == d:
            return hits
        d = d.parent


def assert_isolated(root: Path, label: str) -> None:
    hits = ancestors_with_state(root)
    if hits:
        die(f"{label}: temp tree is NOT isolated -- ancestor(s) hold "
            f"_projects/_state: {hits}. The hook's ancestor walk would reach "
            f"them.")
    ok(f"{label}: no ancestor of {root} holds _projects/_state")



def build_ws(root: Path) -> Path:
    ws = root
    (ws / "_projects" / "demo" / "tasks" / "1_in_progress").mkdir(
        parents=True, exist_ok=True)
    (ws / "_projects" / "demo" / "nb").mkdir(parents=True, exist_ok=True)
    io.open(ws / TASK_REL, "w", encoding="utf-8").write("# demo task\n")
    io.open(ws / NB_REL, "w", encoding="utf-8").write("{}\n")
    (ws / "_projects" / "_state").mkdir(parents=True, exist_ok=True)
    io.open(ws / "_projects" / "_state" / f"{SID}.json", "w",
            encoding="utf-8").write(
        json.dumps({"session_id": SID, "project": "demo"}))
    return ws


def run_hook(cwd: Path, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(HOOK)],
        cwd=str(cwd), input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def payload_for(ws: Path, key: str, rel: str) -> dict:
    """`tool_name` is carried for realism only: `main()` never reads it and no arm below
    is keyed on it."""
    tool = "NotebookEdit" if key == "notebook_path" else "Write"
    return {"session_id": SID, "tool_name": tool,
            "tool_input": {key: str(ws / rel)}}


def touched_lines(ws: Path) -> list[str] | None:
    p = ws / "_projects" / "_state" / f"{SID}.touched"
    if not p.exists():
        return None
    return [ln for ln in io.open(p, encoding="utf-8").read().splitlines() if ln]


def warn_replacement(res: subprocess.CompletedProcess, label: str) -> None:
    if "�" in (res.stdout or "") or "�" in (res.stderr or ""):
        print(f"  WARN: {label}: undecodable bytes in child output were "
              f"replaced with U+FFFD")



def main() -> int:
    print("=== touched_capture.py ledger write semantics ===")
    print()

    if not HOOK.is_file():
        die(f"hook not found: {HOOK}")
    before = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
    print(f"real _projects/_state file count BEFORE: {len(before)}")
    print()

    tmp_root = Path(tempfile.mkdtemp(prefix="tf_ledgerappend_"))
    print(f"temp root: {tmp_root}")
    print()
    print("--- sandbox guards ---")
    assert_isolated(tmp_root, "temp root")
    try:
        tmp_root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        ok(f"temp root is outside the repo tree ({REPO_ROOT})")
    else:
        die(f"temp root is INSIDE the repo ({REPO_ROOT}); the hook's ancestor "
            f"walk would reach the real _projects/_state")

    aborted = True
    try:
        ws = build_ws(tmp_root / "ws")

        print()
        print("--- the ledger does not pre-exist ---")
        got0 = touched_lines(ws)
        check(got0 is None,
              f"L0: no ledger before any invocation, so every later line is "
              f"attributable to a run (got {got0!r})")

        print()
        print("--- first invocation creates the ledger ---")
        res = run_hook(ws, payload_for(ws, "file_path", TASK_REL))
        warn_replacement(res, "L1")
        got1 = touched_lines(ws)
        check(got1 == [TASK_REL],
              f"L1: file_path -> [{TASK_REL!r}] (got {got1!r}, "
              f"rc={res.returncode}, stderr={res.stderr!r})")

        print()
        print("--- second invocation APPENDS, it does not truncate ---")
        res = run_hook(ws, payload_for(ws, "notebook_path", NB_REL))
        warn_replacement(res, "L2")
        got2 = touched_lines(ws)
        check(got2 == [TASK_REL, NB_REL],
              f"L2: L1's line survived and the new one was appended after it "
              f"-> [{TASK_REL!r}, {NB_REL!r}] (got {got2!r}, "
              f"rc={res.returncode}, stderr={res.stderr!r})")

        print()
        print("--- `seen` is per-invocation, not cross-invocation ---")
        res = run_hook(ws, payload_for(ws, "file_path", TASK_REL))
        warn_replacement(res, "L3")
        got3 = touched_lines(ws)
        check(got3 == [TASK_REL, NB_REL, TASK_REL],
              f"L3: re-writing L1's path appends a THIRD line rather than "
              f"deduplicating against the ledger (got {got3!r}, "
              f"rc={res.returncode}, stderr={res.stderr!r})")

        aborted = False
    finally:
        print()
        print("--- real _projects/_state untouched across the whole run ---")
        after = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
        print(f"  [forensic] appeared: {sorted(set(after) - set(before))!r}")
        print(f"  [forensic] disappeared: {sorted(set(before) - set(after))!r}")
        check(len(after) == len(before),
              f"real _projects/_state count unchanged ({len(before)} -> "
              f"{len(after)})")
        strays = [f for f in after if f.startswith(SID.split("-")[0])]
        check(not strays,
              f"no test-SID file leaked into the real state dir "
              f"(found {strays!r})")

        if FAIL == 0 and not aborted:
            shutil.rmtree(tmp_root, ignore_errors=True)
            print(f"  (temp tree removed: {tmp_root})")
        else:
            print(f"  (temp tree KEPT for inspection: {tmp_root})")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
