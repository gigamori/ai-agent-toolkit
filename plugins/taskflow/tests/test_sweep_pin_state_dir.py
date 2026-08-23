#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOKS_SRC = REPO_ROOT / "plugins" / "taskflow" / "hooks"
PROMPTS_SRC = REPO_ROOT / "plugins" / "taskflow" / "prompts"
REAL_STATE = REPO_ROOT / "_projects" / "_state"

SID = "f0220000-1111-2222-3333-444455556666"
DEAD_JSON = "f0d10000-1111-2222-3333-444455556666"
DEAD_TOUCHED = "f0d20000-1111-2222-3333-444455556666"
DEAD_BIND = "f0d30000-1111-2222-3333-444455556666"
SYNTH_IDS = (SID, DEAD_JSON, DEAD_TOUCHED, DEAD_BIND)

PROJECT = "demo"
TASK_BASE = "2026-01-01_demo-task.md"
TASK_REL = f"_projects/{PROJECT}/tasks/1_in_progress/{TASK_BASE}"
TASK_BODY = "# demo task\n\n<!-- @log:begin -->\n<!-- @log:end -->\n"

STALE_AGE_S = 8 * 86400

PIN_ANCHOR = "_cleanup_stale_markers(SWEEP_STATE_DIR)"
FOLLOW_ANCHOR = "_cleanup_stale_markers(STATE_DIR)"

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


def assert_isolated(root: Path) -> None:
    hits = ancestors_with_state(root)
    if hits:
        die(f"temp tree is NOT isolated — ancestor(s) hold _projects/_state: "
            f"{hits}. The `follow` build derived here would sweep them.")
    ok(f"G1: no ancestor of {root} holds _projects/_state")
    try:
        rel = root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        ok(f"G2: temp tree is outside the repo ({REPO_ROOT})")
    else:
        die(f"G2 VIOLATED: temp tree is INSIDE the repo ({rel})")


def state_snapshot() -> list[str]:
    return sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []


def live_prefixes(before: list[str], after: list[str]) -> set[str]:
    jb = {n[:8] for n in before if n.endswith(".json") and len(n) == 41}
    ja = {n[:8] for n in after if n.endswith(".json") and len(n) == 41}
    return jb & ja


def churn_excluded(names: list[str], live: set[str]) -> list[str]:
    return [n for n in names if n[:8] not in live]



def _copy_plugin(dest_root: Path) -> Path:
    tf = dest_root / "taskflow"
    shutil.copytree(HOOKS_SRC, tf / "hooks",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(PROMPTS_SRC, tf / "prompts")
    return tf / "hooks"


def make_builds(root: Path) -> dict[str, Path]:
    current = _copy_plugin(root / "builds" / "current")
    src = io.open(current / "session_progress_capture.py",
                  encoding="utf-8").read()
    if src.count(PIN_ANCHOR) != 1:
        die(f"session_progress_capture.py does not carry {PIN_ANCHOR!r} exactly "
            f"once (count={src.count(PIN_ANCHOR)}) — the pin is not in place, "
            f"so its control cannot be derived")
    if "SWEEP_STATE_DIR = os.path.join(os.getcwd(), '_projects', '_state')" \
            not in src:
        die("the pinned constant is not defined as a cwd-direct path")

    follow = _copy_plugin(root / "builds" / "follow")
    p = follow / "session_progress_capture.py"
    fsrc = io.open(p, encoding="utf-8").read()
    io.open(p, "w", encoding="utf-8", newline="").write(
        fsrc.replace(PIN_ANCHOR, FOLLOW_ANCHOR, 1))
    ok("derived the `follow` control build (sweep target = the search-derived "
       "STATE_DIR) from the real source")
    return {"current": current, "follow": follow}



def build_ws(root: Path, name: str) -> Path:
    ws = root / name
    (ws / "sub").mkdir(parents=True, exist_ok=True)
    (ws / "_projects" / "_state").mkdir(parents=True, exist_ok=True)
    (ws / "_projects" / PROJECT / "tasks" / "1_in_progress").mkdir(
        parents=True, exist_ok=True)
    io.open(ws / TASK_REL, "w", encoding="utf-8").write(TASK_BODY)
    io.open(ws / "_projects" / "_state" / f"{SID}.json", "w",
            encoding="utf-8").write(
        json.dumps({"session_id": SID, "project": PROJECT}))
    return ws


def plant(ws: Path, names: list[str]) -> list[Path]:
    """A `.json` candidate additionally needs a 36-char stem and an empty `project`; a
    sidecar is collected on mtime alone."""
    old = time.time() - STALE_AGE_S
    out = []
    for i, n in enumerate(names):
        p = ws / "_projects" / "_state" / n
        if n.endswith(".json"):
            io.open(p, "w", encoding="utf-8").write(
                json.dumps({"session_id": n[:-5], "project": ""}))
        else:
            io.open(p, "w", encoding="utf-8").write("stale\n")
        os.utime(p, (old - i * 3600, old - i * 3600))
        out.append(p)
    return out


def run_stop(hooks: Path, cwd: Path,
             env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("TASKFLOW_SWEEP_MAX", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["uv", "run", "--no-project", "python",
         str(hooks / "session_progress_capture.py")],
        cwd=str(cwd), input=json.dumps(
            {"session_id": SID, "last_assistant_message": "done"}),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env)


def bind_exists(ws: Path) -> bool:
    return (ws / "_projects" / "_state" / f"{SID}.bind").exists()


def touch_ledger(hooks: Path, ws: Path, cwd: Path) -> None:
    subprocess.run(
        ["uv", "run", "--no-project", "python", str(hooks / "touched_capture.py")],
        cwd=str(cwd), input=json.dumps(
            {"session_id": SID, "tool_name": "Write",
             "tool_input": {"file_path": str(ws / TASK_REL)}}),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})



TWO = [f"{DEAD_TOUCHED}.touched", f"{DEAD_JSON}.json"]
THREE = [f"{DEAD_TOUCHED}.touched", f"{DEAD_JSON}.json", f"{DEAD_BIND}.bind"]


def arm_ac2(root: Path, builds: dict[str, Path]) -> None:
    print()
    print("--- subdir cwd — the planted stale files SURVIVE (the pin) ---")
    ws = build_ws(root, "ac2_current")
    planted = plant(ws, TWO)
    touch_ledger(builds["current"], ws, ws / "sub")
    r = run_stop(builds["current"], ws / "sub")
    alive = [p.name for p in planted if p.exists()]
    check(sorted(alive) == sorted(TWO),
          f"[fixed] cwd=<ws>/sub: both planted files survive (alive={alive!r}, "
          f"rc={r.returncode})")
    check("cleanup: removed" not in r.stderr,
          f"[fixed] no cleanup line was emitted (stderr="
          f"{r.stderr.strip()[:200]!r})")
    check(bind_exists(ws),
          "[fixed] G6 positive execution marker: the Stop ran past the sweep "
          "and wrote <ws>/_projects/_state/<sid>.bind")

    ws_c = build_ws(root, "ac2_follow")
    planted_c = plant(ws_c, TWO)
    touch_ledger(builds["current"], ws_c, ws_c / "sub")
    rc_ = run_stop(builds["follow"], ws_c / "sub")
    gone = [p.name for p in planted_c if not p.exists()]
    check(sorted(gone) == sorted(TWO),
          f"[control, SWEEP-FOLLOWS-SEARCH] the same fixture DELETES both — the "
          f"arm above is not vacuous (deleted={gone!r}, rc={rc_.returncode})")
    check("cleanup: removed 2 stale file(s)" in rc_.stderr,
          f"[control, SWEEP-FOLLOWS-SEARCH] it reported the deletion "
          f"(stderr={rc_.stderr.strip()[:240]!r})")


def arm_ac3(root: Path, builds: dict[str, Path]) -> None:
    print()
    print("--- root cwd — the same planted files ARE deleted "
          "(pinning did not disable GC) ---")
    ws = build_ws(root, "ac3_current")
    planted = plant(ws, TWO)
    touch_ledger(builds["current"], ws, ws)
    r = run_stop(builds["current"], ws)
    gone = [p.name for p in planted if not p.exists()]
    check(sorted(gone) == sorted(TWO),
          f"[fixed] cwd=<ws>: both planted files are deleted (deleted={gone!r}, "
          f"rc={r.returncode})")
    target = str(ws / "_projects" / "_state")
    check("cleanup: removed 2 stale file(s)" in r.stderr and target in r.stderr,
          f"[fixed] the cleanup line names the pinned target {target!r} "
          f"(stderr={r.stderr.strip()[:240]!r})")
    check((ws / "_projects" / "_state" / f"{SID}.json").exists(),
          "[fixed] the live session's own fresh state json was NOT swept")


def arm_ac5(root: Path, builds: dict[str, Path]) -> None:
    print()
    print("--- TASKFLOW_SWEEP_MAX still caps and still defers, with the "
          "pinned target ---")
    ws = build_ws(root, "ac5_root")
    planted = plant(ws, THREE)
    touch_ledger(builds["current"], ws, ws)
    r = run_stop(builds["current"], ws, {"TASKFLOW_SWEEP_MAX": "2"})
    alive = [p.name for p in planted if p.exists()]
    target = str(ws / "_projects" / "_state")
    check(len(alive) == 1,
          f"[fixed] cap=2 over 3 candidates removed exactly 2 (alive={alive!r}, "
          f"rc={r.returncode})")
    check("cleanup: removed 2 stale file(s)" in r.stderr,
          f"[fixed] the cleanup line reports 2 removals "
          f"(stderr={r.stderr.strip()[:280]!r})")
    check(f"WARNING: sweep cap TASKFLOW_SWEEP_MAX=2 hit — 3 candidates, "
          f"removed 2, 1 deferred under {target}" in r.stderr,
          f"[fixed] the WARNING line is verbatim and names the PINNED target "
          f"(stderr={r.stderr.strip()[:400]!r})")
    check(alive == [THREE[0]],
          f"[fixed] the deferred candidate is the newest of the three "
          f"(oldest-first ordering; alive={alive!r})")

    ws2 = build_ws(root, "ac5_sub")
    planted2 = plant(ws2, THREE)
    touch_ledger(builds["current"], ws2, ws2 / "sub")
    r2 = run_stop(builds["current"], ws2 / "sub", {"TASKFLOW_SWEEP_MAX": "2"})
    alive2 = [p.name for p in planted2 if p.exists()]
    check(sorted(alive2) == sorted(THREE) and "WARNING: sweep cap" not in r2.stderr,
          f"[fixed] cwd=<ws>/sub with the same cap: nothing is swept and no cap "
          f"WARNING fires — the cap applies to the pinned dir, which does not "
          f"exist there (alive={alive2!r}, stderr={r2.stderr.strip()[:200]!r})")
    check(bind_exists(ws2),
          "[fixed] G6 positive execution marker: the Stop ran past the sweep")



def main() -> int:
    print("=== pinned bulk sweep: ===")
    print()
    if not HOOKS_SRC.is_dir():
        die(f"hooks dir not found: {HOOKS_SRC}")

    before = state_snapshot()
    print(f"real _projects/_state BEFORE: {len(before)} entries")

    tmp_root = Path(tempfile.mkdtemp(prefix="tf_sweeppin_"))
    print(f"temp root: {tmp_root}")
    print()
    print("--- sandbox guards ---")
    assert_isolated(tmp_root)

    aborted = True
    try:
        builds = make_builds(tmp_root)
        arm_ac2(tmp_root, builds)
        arm_ac3(tmp_root, builds)
        arm_ac5(tmp_root, builds)
        aborted = False
    finally:
        print()
        print("--- real _projects/_state fingerprint ---")
        after = state_snapshot()
        live = live_prefixes(before, after)
        b_ex, a_ex = churn_excluded(before, live), churn_excluded(after, live)
        check(b_ex == a_ex,
              f"real _projects/_state unchanged with live-session churn "
              f"excluded ({len(b_ex)} -> {len(a_ex)}; "
              f"removed={sorted(set(b_ex) - set(a_ex))[:8]}, "
              f"added={sorted(set(a_ex) - set(b_ex))[:8]})")
        strays = [f for f in after if f.startswith(SYNTH_IDS)]
        check(not strays,
              f"no synthetic-SID file leaked into the real state dir "
              f"(found {strays!r})")
        print(f"  (temp tree KEPT for inspection: {tmp_root})")

    print()
    if FAIL == 0 and not aborted:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
