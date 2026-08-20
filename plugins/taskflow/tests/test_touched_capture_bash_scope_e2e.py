#!/usr/bin/env python3
"""E2E tests for the bash-parse-gap / cd-relative-target scope decisions,
run as a real subprocess against the real hook (mode-orchestrator-runs/
2026-08-19_touched-capture-bash-parse-gap-cd-target/02-plan.md §5.3, E1..E5).

Separate file from `test_touched_capture_bash_scope.py` on purpose: that
file's cases call `extract_bash_paths` directly and are FORBIDDEN from ever
calling `main()` (see its own docstring); this file is the E2E half and
exercises `main()` end to end -- extraction, `normalize_path` against
`STATE_ROOT`, and (for E2/E2c) the downstream `extract_project` classifier.

Sandbox mechanics copied (mechanism, not text) from
`test_touched_capture_state_root.py`, per plan §5.3/§6 and
`plugins/taskflow/CLAUDE.md` `e2e_state_dir_sandbox`'s `touched_capture.py`
carve-out: a `tempfile.mkdtemp()` workspace OUTSIDE the repo tree; an
up-front assertion that no ancestor of that workspace holds
`_projects/_state` (the hook walks up, so an in-repo temp dir would resolve
to the REAL state dir); the hook invoked as a SUBPROCESS with an explicit
`cwd=` via `uv run --no-project python <hook 絶対パス>`; and the real
`_projects/_state` file set bracketed unchanged across the whole run.

stdlib only. Run with:
    uv run --no-project python plugins/taskflow/tests/test_touched_capture_bash_scope_e2e.py
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

# extract_project is imported as a PURE function (plan §5.3): its module
# scope only builds path strings (PROGRESS_ROOT/STATE_DIR from os.getcwd())
# and compiles regexes -- no filesystem mutation. _cleanup_stale_markers
# (the real hazard) is called from exactly one place, main(), which this
# file never calls. Verified 2026-08-20 by reading session_progress_capture.py.
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


# --- sandbox guards ----------------------------------------------------

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


# --- fixture / invocation -----------------------------------------------

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
    """`uv run --no-project` per e2e_state_dir_sandbox step 2/§6.3: from a
    temp cwd, plain `uv run` would otherwise resolve this repo's pyproject."""
    payload = {"session_id": SID, "tool_input": {"command": command}}
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(HOOK)],
        cwd=str(cwd), input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def touched_lines(ws: Path) -> list[str] | None:
    """Ledger lines, or None when the ledger file does not exist at all."""
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


# --- arms ----------------------------------------------------------------

def main() -> int:
    print("=== touched_capture.py bash-scope E2E tests (02-plan.md §5.3 E1..E5) ===")
    print()

    if not HOOK.is_file():
        die(f"hook not found: {HOOK}")
    before = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
    print(f"real _projects/_state file count BEFORE: {len(before)}")
    print()

    tmp_root = Path(tempfile.mkdtemp(prefix="tf_bashscope_e2e_"))
    print(f"temp root: {tmp_root}")
    print()
    print("--- sandbox guards (§6) ---")
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
        # ---------------- E1 / E1c ----------------
        print()
        print("--- E1/E1c: sed -i operand extraction + normalize_path (D1) ---")
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

        # ---------------- E2 / E2c ----------------
        print()
        print("--- E2/E2c: cd-relative target recorded verbatim, not bindable (D5) ---")
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

        # ---------------- E3 / E3c ----------------
        print()
        print("--- E3: newline-bled verb-loop stage produces no extra line (P1) ---")
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
              "E3c [non-vacuity, folded into E3 per plan §5.3]: the ledger "
              f"is non-empty, so E3's 'no extra line' is discriminating, not "
              f"a vacuous empty result (got {got3!r})")

        # ---------------- E4 ----------------
        print()
        print("--- E4: orphan guard -- no <SID>.json fixture -> no ledger file ---")
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
        # ---------------- E5 ----------------
        print()
        print("--- E5: real _projects/_state untouched across the whole run ---")
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
