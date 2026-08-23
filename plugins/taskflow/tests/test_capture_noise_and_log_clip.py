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
HOOKS = REPO_ROOT / "plugins" / "taskflow" / "hooks"
HOOK = HOOKS / "touched_capture.py"
REAL_STATE = REPO_ROOT / "_projects" / "_state"

sys.path.insert(0, str(HOOKS))
import touched_capture as tc  # noqa: E402
import session_progress_capture as spc  # noqa: E402

SID = "clipnoise0-1111-2222-3333-444455556666"

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



def test_clip_note() -> None:
    print("--- D1 _clip_note (T1-T4) ---")
    cap = spc.NOTE_CAP

    short = "already   short\nsummary"
    check(spc._clip_note(short) == "already short summary",
          "T1: a fitting summary is normalized and returned unmarked")

    words = ("alpha " * 60).strip()
    clipped = spc._clip_note(words)
    check(len(clipped) <= cap and clipped.endswith("…"),
          f"T2: an over-cap summary is <= {cap} chars and ends with an ellipsis "
          f"(got {len(clipped)})")
    check(not clipped[:-1].endswith(" ") and clipped[:-1].split()[-1] == "alpha",
          "T2: the cut lands on a word boundary, not mid-word")

    dense = "あ" * 400
    hard = spc._clip_note(dense)
    check(len(hard) == cap and hard.endswith("…") and hard[:-1] == "あ" * (cap - 1),
          f"T3: with no word boundary the cut is hard at cap-1 + ellipsis "
          f"(got {len(hard)})")

    check(spc._clip_note(clipped) == clipped and spc._clip_note(hard) == hard,
          "T4: re-clipping an already clipped note is a no-op (text-key "
          "idempotency of log_block_has_note survives a re-apply)")



def test_state_ledger_predicate() -> None:
    print("--- D2 _is_state_ledger_path (unit) ---")
    check(tc._is_state_ledger_path("_projects/_state/abc.r1.capture"),
          "state sidecar path is recognized")
    check(tc._is_state_ledger_path("_projects\\_state\\abc.touched"),
          "backslash-separated state path is recognized")
    check(tc._is_state_ledger_path("_Projects/_State/abc.bind"),
          "recognition is case-insensitive (Windows paths)")
    check(not tc._is_state_ledger_path(
              "_projects/demo/tasks/1_in_progress/x.md"),
          "a task path under another project is NOT excluded")
    check(not tc._is_state_ledger_path("_projects/demo/project-notes/specs/y.md"),
          "a project-notes deliverable is NOT excluded")



HEREDOC_CASES = [
    ("T7", "cat >> index.md <<'EOF'\nprogress: 19 -> 31 -> 34\nEOF\n",
     ["index.md"]),
    ("T8", "cat <<'EOF' > real_out.txt\necho hi > body.txt\ntee body2.txt\nEOF\n",
     ["real_out.txt"]),
    ("T9", "cat <<EOF\nbody > ignored.txt\nEOF\nls > after.txt\n",
     ["after.txt"]),
    ("T10", "cat <<EOF\nstuff > kept.txt\n", ["kept.txt"]),
    ("T11", "cmd <<< 'a > b' > out.txt", ["out.txt"]),
    ("T12", "cat <<-EOF\n\tbody > ignored.txt\n\tEOF\nls > after.txt\n",
     ["after.txt"]),
    ("T13", "cat << EOF > out.txt\nbody\nEOF\n", ["out.txt"]),
    ("T14", "bash <<'EOF'\necho x > written_by_body.txt\nEOF\n", []),
    ("T15", "echo $((1<<2)) > out.txt", ["out.txt"]),
    ("T16", "cat <<A <<B\nbody_a > ignored.txt\nA\nrest > kept.txt\n",
     ["kept.txt"]),
]


def test_heredoc() -> None:
    print("--- D3 heredoc body stripping (T7-T16) ---")
    for tid, cmd, expected in HEREDOC_CASES:
        got = tc.extract_bash_paths(cmd)
        check(got == expected,
              f"{tid} {cmd!r} -> {expected} (got {got})")



def assert_isolated(p: Path, label: str) -> None:
    d = p.resolve()
    while True:
        if (d / "_projects" / "_state").is_dir():
            die(f"{label} has an ancestor holding _projects/_state: {d}")
        parent = d.parent
        if parent == d:
            return
        d = parent


def make_ws(root: Path) -> Path:
    ws = root / "ws"
    (ws / "_projects" / "_state").mkdir(parents=True)
    (ws / "_projects" / "demo" / "tasks" / "1_in_progress").mkdir(parents=True)
    io.open(ws / "_projects" / "_state" / f"{SID}.json", "w",
            encoding="utf-8").write("{}")
    return ws


def run_hook(cwd: Path, command: str) -> subprocess.CompletedProcess:
    payload = {"session_id": SID, "tool_input": {"command": command}}
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(HOOK)],
        cwd=str(cwd), input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def touched_lines(ws: Path) -> list:
    p = ws / "_projects" / "_state" / f"{SID}.touched"
    if not p.exists():
        return []
    return [ln for ln in io.open(p, encoding="utf-8").read().splitlines() if ln]


def test_state_exclusion_e2e(tmp_root: Path) -> None:
    print("--- D2 sidecar exclusion through main() (T5-T6) ---")
    task_rel = "_projects/demo/tasks/1_in_progress/2026-01-01_demo.md"

    ws = make_ws(tmp_root / "t5")
    res = run_hook(ws, f"echo x > _projects/_state/{SID}.r1.capture")
    check(res.returncode == 0, f"T5: hook exits 0 (rc={res.returncode})")
    check(touched_lines(ws) == [],
          f"T5: a sidecar-only write records no ledger line "
          f"(got {touched_lines(ws)})")

    ws_ctl = make_ws(tmp_root / "t5ctl")
    run_hook(ws_ctl, f"echo x > {task_rel}")
    check(touched_lines(ws_ctl) == [task_rel],
          f"T5 [control]: the same shape outside _state DOES record "
          f"(got {touched_lines(ws_ctl)})")

    ws2 = make_ws(tmp_root / "t6")
    run_hook(ws2, f"echo x > {task_rel} && echo y > _projects/_state/{SID}.bind")
    check(touched_lines(ws2) == [task_rel],
          f"T6: only the task path is recorded from a mixed write "
          f"(got {touched_lines(ws2)})")


def main() -> int:
    before = sorted(p.name for p in REAL_STATE.glob("*")) if REAL_STATE.is_dir() else []

    test_clip_note()
    print()
    test_state_ledger_predicate()
    print()
    test_heredoc()
    print()

    tmp_root = Path(tempfile.mkdtemp(prefix="tf_clipnoise_"))
    print(f"temp root: {tmp_root}")
    assert_isolated(tmp_root, "temp root")
    try:
        tmp_root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        pass
    else:
        die(f"temp root is INSIDE the repo ({REPO_ROOT}); the hook's ancestor "
            f"walk would reach the real _projects/_state")
    try:
        test_state_exclusion_e2e(tmp_root)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print()
    after = sorted(p.name for p in REAL_STATE.glob("*")) if REAL_STATE.is_dir() else []
    check(before == after,
          "real _projects/_state/ is unchanged by this test run")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} check(s) failed ({PASS} passed).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
