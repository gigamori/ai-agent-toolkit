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

SID = "scratch01-1111-2222-3333-444455556666"
TASK_REL = "_projects/demo/tasks/1_in_progress/2026-01-01_demo-task.md"

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
    io.open(ws / TASK_REL, "w", encoding="utf-8").write("# demo task\n")
    (ws / "_projects" / "_state").mkdir(parents=True, exist_ok=True)
    io.open(ws / "_projects" / "_state" / f"{SID}.json", "w",
            encoding="utf-8").write(
        json.dumps({"session_id": SID, "project": "demo"}))
    return ws


def run_hook(cwd: Path, command: str) -> subprocess.CompletedProcess:
    payload = {"session_id": SID, "tool_name": "Bash",
               "tool_input": {"command": command}}
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(HOOK)],
        cwd=str(cwd), input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def touched_lines(ws: Path) -> list[str]:
    p = ws / "_projects" / "_state" / f"{SID}.touched"
    if not p.exists():
        return []
    return [ln for ln in io.open(p, encoding="utf-8").read().splitlines() if ln]


def clear_ledger(ws: Path) -> None:
    p = ws / "_projects" / "_state" / f"{SID}.touched"
    if p.exists():
        p.unlink()


def warn_replacement(res: subprocess.CompletedProcess, label: str) -> None:
    if "�" in (res.stdout or "") or "�" in (res.stderr or ""):
        print(f"  WARN: {label}: undecodable bytes in child output were "
              f"replaced with U+FFFD")


def main() -> int:
    print("=== touched_capture.py cwd-relative tmp/ exclusion ===")
    print()

    if not HOOK.is_file():
        die(f"hook not found: {HOOK}")
    before = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
    print(f"real _projects/_state file count BEFORE: {len(before)}")
    print()

    tmp_root = Path(tempfile.mkdtemp(prefix="tf_scratchskip_"))
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
        print("--- control arm: a task write is still recorded ---")
        clear_ledger(ws)
        res = run_hook(ws, f'echo x > "{ws / TASK_REL}"')
        warn_replacement(res, "S0")
        got = touched_lines(ws)
        check(got == [TASK_REL],
              f"S0: the harness can observe a hit, so a later empty ledger is "
              f"evidence of exclusion and not of a dead probe "
              f"(got {got!r}, rc={res.returncode}, stderr={res.stderr!r})")

        print()
        print("--- a cwd-relative tmp/ write is not recorded ---")
        clear_ledger(ws)
        res = run_hook(ws, f'echo x > "{ws}/tmp/probe.txt"')
        warn_replacement(res, "S1")
        got = touched_lines(ws)
        check(got == [],
              f"S1: an absolute path under the workspace tmp/ normalises to "
              f"tmp/probe.txt and is dropped (got {got!r}, "
              f"rc={res.returncode}, stderr={res.stderr!r})")

        print()
        print("--- the ./ spelling is dropped too ---")
        clear_ledger(ws)
        res = run_hook(ws, "echo x > ./tmp/probe.txt")
        warn_replacement(res, "S2")
        got = touched_lines(ws)
        check(got == [],
              f"S2: a leading ./ does not smuggle a scratch path past the "
              f"exclusion (got {got!r}, rc={res.returncode}, "
              f"stderr={res.stderr!r})")

        print()
        print("--- the exclusion is anchored at the workspace root ---")
        clear_ledger(ws)
        res = run_hook(ws, "echo x > sub/tmp/keep.txt")
        warn_replacement(res, "S3")
        got = touched_lines(ws)
        check(got == ["sub/tmp/keep.txt"],
              f"S3: a nested directory named tmp is ordinary work and stays "
              f"recorded (got {got!r}, rc={res.returncode}, "
              f"stderr={res.stderr!r})")

        print()
        print("--- the prefix does not over-match a filename ---")
        clear_ledger(ws)
        res = run_hook(ws, "echo x > tmpfile.txt")
        warn_replacement(res, "S4")
        got = touched_lines(ws)
        check(got == ["tmpfile.txt"],
              f"S4: a name merely starting with tmp is not a scratch tree "
              f"(got {got!r}, rc={res.returncode}, stderr={res.stderr!r})")

        print()
        print("--- a mixed invocation keeps only the non-scratch path ---")
        clear_ledger(ws)
        res = run_hook(ws, f'cp "{ws / TASK_REL}" tmp/copy.md')
        warn_replacement(res, "S5")
        got = touched_lines(ws)
        check(got == [TASK_REL],
              f"S5: one command touching both trees records the task path and "
              f"drops the scratch one (got {got!r}, rc={res.returncode}, "
              f"stderr={res.stderr!r})")

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
