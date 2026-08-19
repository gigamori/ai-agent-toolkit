#!/usr/bin/env python3
"""Acceptance tests for touched_capture.py's state-root resolution (04-plan
section 1.5, A-AC1..A-AC4; method A-m1, scope A-s1).

The defect: PROGRESS_ROOT/STATE_DIR were anchored on `os.getcwd()` at module
scope, and `main()` normalized paths against `os.getcwd()` too. A Claude Code
session launched inside a subdirectory keeps that subdirectory as its hook cwd
for its whole life (live probe on 2.1.233), so STATE_DIR resolved to a path that
does not exist and `main()` returned at its first line -- every write of that
session was dropped from the `.touched` ledger, which is the sole input to task
and note resolution.

The fix is two INSEPARABLE hunks: `_find_state_root()` + STATE_ROOT, and
`cwd = STATE_ROOT` in `main()`. Fixing only the first moves the loss from the
ledger to the classifier: the line is written, but as an ABSOLUTE path, which
fails `_PROJECT_RE` in session_progress_capture.py so `extract_project` returns
'' and the line is dropped from both the task and the note resolution. A-AC2 is
that negative control, run against a hunk-1-only variant derived from the real
source.

Every arm carries its stated control, because a "no line was written" result is
otherwise indistinguishable from a fixture that never triggers the hook at all.

Sandbox (plugins/taskflow/CLAUDE.md `e2e_state_dir_sandbox`, plus the new
condition from 04-plan section 5.2 (4) / R-A2): the hook is invoked as a
SUBPROCESS with an explicit `cwd=` (STATE_DIR is module scope, so importing
cannot vary cwd), each workspace is a `tempfile.mkdtemp()` tree, and -- because
A-m1 now WALKS UP -- this script asserts up front that no ancestor of that temp
tree holds `_projects/_state`. A temp workspace created inside this repo would
resolve to the REAL `_projects/_state`, so it must stay outside the repo. The
real state dir's file count is asserted unchanged at the end (A-AC6).

stdlib only. Run with:
    uv run --no-project python plugins/taskflow/tests/test_touched_capture_state_root.py
Exits 0 when all checks pass, 1 otherwise.
"""
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

SID = "statroot11-2222-3333-4444-555566667777"
TASK_REL = "_projects/demo/tasks/1_in_progress/2026-01-01_demo-task.md"
REPLACEMENT_CHAR = "�"

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


# --- sandbox guards --------------------------------------------------------

def ancestors_with_state(start: Path) -> list[str]:
    """Every ancestor of `start` (inclusive) that holds `_projects/_state` --
    i.e. exactly what `_find_state_root` would walk into."""
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
            f"_projects/_state: {hits}. A-m1 would walk up into them.")
    ok(f"{label}: no ancestor of {root} holds _projects/_state (R-A2)")


# --- hook variants ---------------------------------------------------------

FIXED_STATE_ROOT = "STATE_ROOT = _find_state_root(os.getcwd()) or os.getcwd()"
PREFIX_STATE_ROOT = "STATE_ROOT = os.getcwd()"
FIXED_CWD = "    cwd = STATE_ROOT"
PREFIX_CWD = "    cwd = os.getcwd()"


def make_variants(dest: Path) -> tuple[Path, Path]:
    """Derive the two control hooks from the REAL source by substitution, so a
    stale copy cannot make a control arm vacuous. Each substitution is asserted
    to have matched exactly once.

    - `prefix.py`   : both hunks reverted. `STATE_ROOT = os.getcwd()` makes
                      `cwd = STATE_ROOT` byte-equivalent to the old
                      `cwd = os.getcwd()`, so this IS the pre-fix hook.
                      Control for A-AC1.
    - `rootonly.py` : hunk 1 only (state root resolved by walking up, but the
                      normalization base is still the cwd). Control for A-AC2.
    """
    src = io.open(HOOK, encoding="utf-8").read()
    for anchor in (FIXED_STATE_ROOT, FIXED_CWD):
        if src.count(anchor) != 1:
            die(f"hook source does not carry {anchor!r} exactly once "
                f"(count={src.count(anchor)}) -- the fix is not in place")
    prefix = dest / "prefix.py"
    rootonly = dest / "rootonly.py"
    io.open(prefix, "w", encoding="utf-8").write(
        src.replace(FIXED_STATE_ROOT, PREFIX_STATE_ROOT, 1))
    io.open(rootonly, "w", encoding="utf-8").write(
        src.replace(FIXED_CWD, PREFIX_CWD, 1))
    return prefix, rootonly


# --- fixture / invocation --------------------------------------------------

def build_ws(root: Path, with_state: bool, with_sid_json: bool) -> Path:
    ws = root
    (ws / "plugins" / "sub").mkdir(parents=True, exist_ok=True)
    (ws / "_projects" / "demo" / "tasks" / "1_in_progress").mkdir(
        parents=True, exist_ok=True)
    io.open(ws / TASK_REL, "w", encoding="utf-8").write("# demo task\n")
    if with_state:
        (ws / "_projects" / "_state").mkdir(parents=True, exist_ok=True)
    if with_sid_json:
        io.open(ws / "_projects" / "_state" / f"{SID}.json", "w",
                encoding="utf-8").write(
            json.dumps({"session_id": SID, "project": "demo"}))
    return ws


def run_hook(hook: Path, cwd: Path, payload: dict) -> subprocess.CompletedProcess:
    """`uv run --no-project` per e2e_state_dir_sandbox step 2: from a temp cwd,
    uv would otherwise resolve this repo's pyproject."""
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(hook)],
        cwd=str(cwd), input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def touched_lines(ws: Path) -> list[str] | None:
    """Ledger lines, or None when the ledger file does not exist at all."""
    p = ws / "_projects" / "_state" / f"{SID}.touched"
    if not p.exists():
        return None
    return [ln for ln in io.open(p, encoding="utf-8").read().splitlines() if ln]


def reset_ledger(ws: Path) -> None:
    p = ws / "_projects" / "_state" / f"{SID}.touched"
    if p.exists():
        p.unlink()


def payload_for(ws: Path, tool_name: str) -> dict:
    return {"session_id": SID, "tool_name": tool_name,
            "tool_input": {"file_path": str(ws / TASK_REL)}}


def warn_replacement(res: subprocess.CompletedProcess, label: str) -> None:
    if REPLACEMENT_CHAR in (res.stdout or "") or REPLACEMENT_CHAR in (res.stderr or ""):
        print(f"  WARN: {label}: undecodable bytes in child output were "
              f"replaced with U+FFFD")


# --- arms ------------------------------------------------------------------

def main() -> int:
    print("=== touched_capture.py state-root resolution (A-AC1..A-AC4) ===")
    print()

    if not HOOK.is_file():
        die(f"hook not found: {HOOK}")
    before = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
    print(f"real _projects/_state file count BEFORE: {len(before)}")
    print()

    tmp_root = Path(tempfile.mkdtemp(prefix="tf_stateroot_"))
    print(f"temp root: {tmp_root}")
    print()
    print("--- sandbox guards (04-plan 5.2 condition (4) / R-A2) ---")
    assert_isolated(tmp_root, "temp root")
    try:
        rel = tmp_root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        ok(f"temp root is outside the repo tree ({REPO_ROOT})")
    else:
        die(f"temp root is INSIDE the repo ({rel}); A-m1 would reach the real "
            f"_projects/_state")

    aborted = True
    try:
        ws = build_ws(tmp_root / "ws", with_state=True, with_sid_json=True)
        sub = ws / "plugins" / "sub"
        prefix_hook, rootonly_hook = make_variants(tmp_root)

        # ---------------- A-AC1 ----------------
        print()
        print("--- A-AC1: subdir cwd records a repo-relative line ---")
        for tool in ("Write", "Edit"):
            reset_ledger(ws)
            res = run_hook(HOOK, sub, payload_for(ws, tool))
            warn_replacement(res, f"A-AC1 {tool}")
            got = touched_lines(ws)
            check(got == [TASK_REL],
                  f"[fixed] cwd=<ws>/plugins/sub {tool} -> [{TASK_REL!r}] "
                  f"(got {got!r}, rc={res.returncode})")

        reset_ledger(ws)
        res = run_hook(HOOK, ws, payload_for(ws, "Write"))
        got_root = touched_lines(ws)
        check(got_root == [TASK_REL],
              f"[control, fixed] cwd=<ws> Write -> [{TASK_REL!r}] "
              f"(got {got_root!r}, rc={res.returncode})")

        reset_ledger(ws)
        res = run_hook(prefix_hook, ws, payload_for(ws, "Write"))
        got_pre_root = touched_lines(ws)
        check(got_pre_root == [TASK_REL],
              f"[control, PRE-FIX] cwd=<ws> Write still records -- non-zero "
              f"base rate (got {got_pre_root!r}, rc={res.returncode})")

        reset_ledger(ws)
        res = run_hook(prefix_hook, sub, payload_for(ws, "Write"))
        got_pre_sub = touched_lines(ws)
        check(got_pre_sub is None,
              f"[control, PRE-FIX] cwd=<ws>/plugins/sub writes NO ledger at all "
              f"(<absent>) (got {got_pre_sub!r}, rc={res.returncode})")

        # ---------------- A-AC2 ----------------
        print()
        print("--- A-AC2: the recorded line is not an absolute path (hunk 2) ---")
        reset_ledger(ws)
        res = run_hook(HOOK, sub, payload_for(ws, "Write"))
        got = touched_lines(ws) or []
        check(bool(got) and not os.path.isabs(got[0]) and got[0] == TASK_REL,
              f"[fixed] subdir line is repo-relative (got {got!r})")

        reset_ledger(ws)
        res = run_hook(rootonly_hook, sub, payload_for(ws, "Write"))
        got_ro = touched_lines(ws) or []
        check(len(got_ro) == 1
              and os.path.isabs(got_ro[0].replace("/", os.sep)),
              f"[control, HUNK-1-ONLY] subdir line is ABSOLUTE -- proves hunk 2 "
              f"is load-bearing (got {got_ro!r})")

        # ---------------- A-AC3 ----------------
        print()
        print("--- A-AC3: no ancestor holds state -> '' and a silent return 0 ---")
        bare = tmp_root / "bare" / "deep"
        bare.mkdir(parents=True)
        probe = tmp_root / "probe.py"
        io.open(probe, "w", encoding="utf-8").write(
            "import importlib.util, os, sys\n"
            "spec = importlib.util.spec_from_file_location('tc', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "print('FOUND=' + m._find_state_root(os.getcwd()))\n"
        )
        pr = subprocess.run(
            ["uv", "run", "--no-project", "python", str(probe), str(HOOK)],
            cwd=str(bare), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        warn_replacement(pr, "A-AC3 probe")
        check(pr.stdout.strip() == "FOUND=",
              f"_find_state_root() returns '' from a stateless tree "
              f"(got {pr.stdout.strip()!r}, rc={pr.returncode})")

        pr2 = subprocess.run(
            ["uv", "run", "--no-project", "python", str(probe), str(HOOK)],
            cwd=str(sub), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        warn_replacement(pr2, "A-AC3 probe control")
        check(pr2.stdout.strip() == f"FOUND={ws}",
              f"[control] _find_state_root() returns <ws> from <ws>/plugins/sub "
              f"(got {pr2.stdout.strip()!r})")

        res = run_hook(HOOK, bare,
                       {"session_id": SID, "tool_name": "Write",
                        "tool_input": {"file_path": str(bare / "x.md")}})
        warn_replacement(res, "A-AC3 main")
        leaked = list((tmp_root / "bare").rglob("*.touched"))
        check(res.returncode == 0 and not res.stdout.strip() and not leaked,
              f"main() in a stateless tree returns 0 silently and writes nothing "
              f"(rc={res.returncode}, stdout={res.stdout.strip()!r}, "
              f"leaked={leaked!r})")
        check(not (tmp_root / "bare" / "_projects").exists(),
              "main() in a stateless tree creates no _projects/")

        # ---------------- A-AC4 ----------------
        print()
        print("--- A-AC4: orphan guard still fires on the resolved state dir ---")
        ws2 = build_ws(tmp_root / "ws_noguard", with_state=True,
                       with_sid_json=False)
        sub2 = ws2 / "plugins" / "sub"
        res = run_hook(HOOK, sub2, payload_for(ws2, "Write"))
        got2 = touched_lines(ws2)
        check(got2 is None,
              f"state dir found but no <sid>.json -> no ledger (got {got2!r}, "
              f"rc={res.returncode})")

        io.open(ws2 / "_projects" / "_state" / f"{SID}.json", "w",
                encoding="utf-8").write(
            json.dumps({"session_id": SID, "project": "demo"}))
        res = run_hook(HOOK, sub2, payload_for(ws2, "Write"))
        got2b = touched_lines(ws2)
        check(got2b == [TASK_REL],
              f"[control] the same tree WITH <sid>.json records the line "
              f"(got {got2b!r}, rc={res.returncode})")

        aborted = False
    finally:
        # ---------------- A-AC6 ----------------
        print()
        print("--- A-AC6: real _projects/_state untouched ---")
        after = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
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
