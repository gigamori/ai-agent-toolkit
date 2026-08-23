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

sys.path.insert(0, str(REPO_ROOT / "plugins" / "taskflow" / "hooks"))
import session_progress_capture as spc  # noqa: E402

SID = "e2ebashsc0-1111-2222-3333-444455556666"
TASK_REL = "_projects/demo/tasks/1_in_progress/2026-01-01_demo.md"
NOTE_REL = "_projects/demo/project-notes/index.md"

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



def build_ws(root: Path, with_sid_json: bool = True) -> Path:
    ws = root
    (ws / "_projects" / "demo" / "tasks" / "1_in_progress").mkdir(
        parents=True, exist_ok=True)
    (ws / "_projects" / "demo" / "project-notes").mkdir(
        parents=True, exist_ok=True)
    io.open(ws / TASK_REL, "w", encoding="utf-8").write("# demo task\n")
    io.open(ws / NOTE_REL, "w", encoding="utf-8").write("# notes\n")
    (ws / "_projects" / "_state").mkdir(parents=True, exist_ok=True)
    if with_sid_json:
        io.open(ws / "_projects" / "_state" / f"{SID}.json", "w",
                encoding="utf-8").write(
            json.dumps({"session_id": SID, "project": "demo"}))
    return ws


def run_hook(cwd: Path, command: str) -> subprocess.CompletedProcess:
    payload = {"session_id": SID, "tool_input": {"command": command}}
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(HOOK)],
        cwd=str(cwd), input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def touched_lines(ws: Path) -> list[str] | None:
    p = ws / "_projects" / "_state" / f"{SID}.touched"
    if not p.exists():
        return None
    return [ln for ln in io.open(p, encoding="utf-8").read().splitlines() if ln]


def abspath_fwd(p: Path) -> str:
    return str(p).replace("\\", "/")


def warn_replacement(res: subprocess.CompletedProcess, label: str) -> None:
    if "�" in (res.stdout or "") or "�" in (res.stderr or ""):
        print(f"  WARN: {label}: undecodable bytes in child output were "
              f"replaced with U+FFFD")



def main() -> int:
    print("=== touched_capture.py bash-scope E2E tests ===")
    print()

    if not HOOK.is_file():
        die(f"hook not found: {HOOK}")
    before = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
    print(f"real _projects/_state file count BEFORE: {len(before)}")
    print()

    tmp_root = Path(tempfile.mkdtemp(prefix="tf_bashscope_e2e_"))
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
        print()
        print("--- sed -i operand extraction + normalize_path ---")
        ws1 = build_ws(tmp_root / "e1")
        abs_task = abspath_fwd(ws1 / TASK_REL)
        res = run_hook(ws1, f"sed -i 's/a/b/' {abs_task}")
        warn_replacement(res, "E1")
        got = touched_lines(ws1)
        check(got == [TASK_REL],
              f"E1: sed -i on an absolute in-workspace path -> [{TASK_REL!r}] "
              f"(got {got!r}, rc={res.returncode}, stderr={res.stderr!r})")

        ws1c = build_ws(tmp_root / "e1c")
        abs_task_c = abspath_fwd(ws1c / TASK_REL)
        res = run_hook(ws1c, f"sed -n '1,5p' {abs_task_c}")
        warn_replacement(res, "E1c")
        got_c = touched_lines(ws1c)
        check(got_c is None,
              f"E1c [control]: read-only sed -n writes no ledger at all "
              f"(got {got_c!r}, rc={res.returncode})")

        print()
        print("--- cd-relative target recorded verbatim, not bindable ---")
        ws2 = build_ws(tmp_root / "e2")
        notes_dir = abspath_fwd(ws2 / "_projects" / "demo" / "project-notes")
        res = run_hook(ws2, f"cd {notes_dir} && echo x >> index.md")
        warn_replacement(res, "E2")
        got2 = touched_lines(ws2)
        check(got2 == ["index.md"],
              f"E2: bare target after cd recorded VERBATIM, not joined "
              f"(got {got2!r}, rc={res.returncode}, stderr={res.stderr!r})")
        check(spc.extract_project("index.md") == "",
              "E2: extract_project('index.md') == '' -- not bindable")

        ws2c = build_ws(tmp_root / "e2c")
        abs_note = abspath_fwd(ws2c / NOTE_REL)
        res = run_hook(ws2c, f"echo x >> {abs_note}")
        warn_replacement(res, "E2c")
        got2c = touched_lines(ws2c)
        check(got2c == [NOTE_REL],
              f"E2c [control]: the same write via a full path resolves "
              f"(got {got2c!r}, rc={res.returncode}, stderr={res.stderr!r})")
        check(spc.extract_project(NOTE_REL) == "demo",
              "E2c [control]: extract_project(...) == 'demo' -- so E2's ''"
              " is discriminating, this shape, not a harness that never"
              " resolves anything")

        print()
        print("--- newline-bled verb-loop stage produces no extra line ---")
        ws3 = build_ws(tmp_root / "e3")
        scratch = abspath_fwd(ws3 / "scratch")
        cmd3 = f'rm -rf {scratch}\necho "=== gone ==="'
        res = run_hook(ws3, cmd3)
        warn_replacement(res, "E3")
        got3 = touched_lines(ws3) or []
        check(len(got3) == 1,
              f"E3: ledger has exactly one line (got {got3!r}, "
              f"rc={res.returncode}, stderr={res.stderr!r})")
        blob = "\n".join(got3)
        check("echo" not in blob and "=== gone ===" not in blob,
              f"E3: neither 'echo' nor '=== gone ===' appears in the ledger "
              f"(got {got3!r})")
        check(len(got3) >= 1,
              "E3c [non-vacuity, folded into E3 per plan]: the ledger "
              f"is non-empty, so E3's 'no extra line' is discriminating, not "
              f"a vacuous empty result (got {got3!r})")

        print()
        print("--- orphan guard -- no <SID>.json fixture -> no ledger file ---")
        ws4 = build_ws(tmp_root / "e4", with_sid_json=False)
        abs_task4 = abspath_fwd(ws4 / TASK_REL)
        res = run_hook(ws4, f"sed -i 's/a/b/' {abs_task4}")
        warn_replacement(res, "E4")
        got4 = touched_lines(ws4)
        check(got4 is None,
              f"E4: no <SID>.json -> no ledger file created at all "
              f"(got {got4!r}, rc={res.returncode})")

        aborted = False
    finally:
        print()
        print("--- real _projects/_state untouched across the whole run ---")
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
