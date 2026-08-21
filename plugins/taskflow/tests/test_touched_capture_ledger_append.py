#!/usr/bin/env python3
"""E2E tests for touched_capture.py's ledger write semantics through `main()`.

The layer this file owns: what `main()` does to `<STATE_DIR>/<sid>.touched`
once the paths are already extracted -- the append itself (`main()`'s
`open(touched_path, 'a')`), the per-invocation `seen` set, and the
`_is_state_ledger_path` exclusion applied at write time (that function and its
docstring), all in touched_capture.py. L0..L3 below are that layer's first
occupants and cover the append plus `seen`'s INVOCATION SCOPE -- no more than
that. Two branches of the layer have no arm yet: `seen`'s within-invocation
dedup (two identical paths inside ONE call, which `extract_paths` can produce
by returning a write-key path and the same path recovered from `command`), and
the `_is_state_ledger_path` exclusion. A later ledger-write test belongs here
rather than in a seventh file.

Origin: the consolidation of 2026-08-20
(mode-orchestrator-runs/2026-08-20_test-touched-capture-sh-state-hazard/,
plan §2.3 as amended by 03-review-dev.md F3). L2 is the port of the retired
test_touched_capture.sh's append-accumulation property (its A15b), which was
implicit there -- a second invocation's `grep` happened to run against a ledger
the first invocation had created. Here it is explicit and ordered.

Why this is not an arm in an existing file: test_touched_capture_bash_scope.py
and test_touched_capture_quoted_redirect.py are FORBIDDEN from ever calling
`main()` (their own docstrings), and this assertion IS `main()`, three times.
test_touched_capture_state_root.py resets the ledger before every arm, which is
the negation of the invariant under test here, and
test_touched_capture_bash_scope_e2e.py builds a fresh workspace per arm.

Sandbox mechanics copied (mechanism, not text) from
`test_touched_capture_state_root.py`, per the `e2e_state_dir_sandbox` rule
(cited by rule id on purpose: the rule file has moved once already and every
candidate path is gitignored, so no path citation survives a clone; since
2026-08-20 its isolation steps bind every taskflow hook, no longer a
`touched_capture.py`-only carve-out): a
`tempfile.mkdtemp()` workspace OUTSIDE the repo tree; an up-front assertion
that no ancestor of that workspace holds `_projects/_state` (the hook walks up,
so an in-repo temp dir would resolve to the REAL state dir); the hook invoked
as a SUBPROCESS with an explicit `cwd=` via
`uv run --no-project python <hook absolute path>`; and the real
`_projects/_state` bracketed unchanged across the whole run (L4). The
`<STATE_DIR>/<sid>.json` orphan guard is a second barrier only, never the
isolation argument.

stdlib only. Run with:
    uv run --no-project python plugins/taskflow/tests/test_touched_capture_ledger_append.py
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

# Distinct, greppable prefix: it is what the real-state stray check keys on,
# and it must be written by nothing else in the tree.
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
            f"_projects/_state: {hits}. The hook's ancestor walk would reach "
            f"them.")
    ok(f"{label}: no ancestor of {root} holds _projects/_state")


# --- fixture / invocation --------------------------------------------------

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
    """`uv run --no-project` per e2e_state_dir_sandbox step 2: from a temp cwd,
    plain `uv run` would otherwise resolve this repo's environment."""
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(HOOK)],
        cwd=str(cwd), input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def payload_for(ws: Path, key: str, rel: str) -> dict:
    """`tool_name` is carried for realism only -- `main()` never reads it (the
    PostToolUse matcher does the gating outside this hook, see
    `extract_paths`' docstring in touched_capture.py). No arm below is keyed on
    it."""
    tool = "NotebookEdit" if key == "notebook_path" else "Write"
    return {"session_id": SID, "tool_name": tool,
            "tool_input": {key: str(ws / rel)}}


def touched_lines(ws: Path) -> list[str] | None:
    """Ledger lines, or None when the ledger file does not exist at all."""
    p = ws / "_projects" / "_state" / f"{SID}.touched"
    if not p.exists():
        return None
    return [ln for ln in io.open(p, encoding="utf-8").read().splitlines() if ln]


def warn_replacement(res: subprocess.CompletedProcess, label: str) -> None:
    if "�" in (res.stdout or "") or "�" in (res.stderr or ""):
        print(f"  WARN: {label}: undecodable bytes in child output were "
              f"replaced with U+FFFD")


# --- arms ------------------------------------------------------------------

def main() -> int:
    print("=== touched_capture.py ledger write semantics (L0..L4) ===")
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
        # ONE workspace, three sequential invocations, NO reset between them --
        # that is the whole point of this file.
        ws = build_ws(tmp_root / "ws")

        # ---------------- L0 ----------------
        print()
        print("--- L0 [control]: the ledger does not pre-exist ---")
        got0 = touched_lines(ws)
        check(got0 is None,
              f"L0: no ledger before any invocation, so every later line is "
              f"attributable to a run (got {got0!r})")

        # ---------------- L1 ----------------
        print()
        print("--- L1: first invocation creates the ledger ---")
        res = run_hook(ws, payload_for(ws, "file_path", TASK_REL))
        warn_replacement(res, "L1")
        got1 = touched_lines(ws)
        check(got1 == [TASK_REL],
              f"L1: file_path -> [{TASK_REL!r}] (got {got1!r}, "
              f"rc={res.returncode}, stderr={res.stderr!r})")

        # ---------------- L2 ----------------
        print()
        print("--- L2: second invocation APPENDS, it does not truncate ---")
        res = run_hook(ws, payload_for(ws, "notebook_path", NB_REL))
        warn_replacement(res, "L2")
        got2 = touched_lines(ws)
        check(got2 == [TASK_REL, NB_REL],
              f"L2: L1's line survived and the new one was appended after it "
              f"-> [{TASK_REL!r}, {NB_REL!r}] (got {got2!r}, "
              f"rc={res.returncode}, stderr={res.stderr!r})")

        # ---------------- L3 ----------------
        print()
        print("--- L3: `seen` is per-invocation, not cross-invocation ---")
        # A SEMANTIC pin, not merely a current-behaviour pin: `.touched` is
        # append-only and its raw line COUNT is the round cursor -- stated in
        # `_is_state_ledger_path`'s docstring in touched_capture.py and in
        # test_touched_capture_quoted_redirect.py:15-16. A cursor that counts
        # occurrences requires a repeat to be recorded; deduplicating across
        # invocations would change what the cursor means, not merely tidy the
        # file. `seen` is built fresh inside `main()` for exactly that reason,
        # and this arm is what would fail if it were hoisted.
        res = run_hook(ws, payload_for(ws, "file_path", TASK_REL))
        warn_replacement(res, "L3")
        got3 = touched_lines(ws)
        check(got3 == [TASK_REL, NB_REL, TASK_REL],
              f"L3: re-writing L1's path appends a THIRD line rather than "
              f"deduplicating against the ledger (got {got3!r}, "
              f"rc={res.returncode}, stderr={res.stderr!r})")

        aborted = False
    finally:
        # ---------------- L4 ----------------
        print()
        print("--- L4: real _projects/_state untouched across the whole run ---")
        after = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
        # Printed, never asserted: a live session writes and consumes its own
        # `<sid>.touched` continuously, so a name-set assertion flakes here
        # (e2e_state_dir_sandbox step 3). The print closes the count check's
        # add+remove blind spot in the run log at zero flake cost.
        # The COUNT asserted below is exposed to that same churn -- measured in
        # the run that created this file, where the real dir read 471, then 472,
        # then 471 again (05-execute.md §5 of the run directory named above) --
        # so a red L4 count must be RE-RUN before it is read as a leak; the
        # churn-immune attribution is the test-SID stray check that follows it.
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
