#!/usr/bin/env python3
"""Acceptance tests for the `_projects` ancestor-search rollout across the
taskflow hooks (mode-orchestrator-runs/2026-08-20_remaining-hooks-cwd-dependence,
02-plan.md §5.2 AC-1 / AC-4 / AC-7 under the §5.3 guards, plus the two additions
the 03a decision record's addendum assigns to the test turn).

What landed, and what this file discriminates
---------------------------------------------
Turn 03 gave `session_init.py`, `session_sync.py`, `session_compact_reset.py`
and `session_progress_capture.py` the same `_find_state_root()` walk
`touched_capture.py` already had, and pointed `precompact_flush.py`'s
normalization base at the imported `STATE_ROOT`. Turn 03b changed
`session_progress_capture.main()`'s `cwd = os.getcwd()` to `cwd = STATE_ROOT`.
The bulk stale-marker sweep stayed cwd-pinned (`SWEEP_STATE_DIR`); that half is
covered by `test_sweep_pin_state_dir.py`, not here.

Every arm carries a control, because "nothing was produced" is otherwise
indistinguishable from "the hook never ran". Three builds are derived from the
REAL source into the temp tree, so a stale copy cannot make a control vacuous:

  builds/current  : verbatim copy of `plugins/taskflow/hooks/`.
  builds/pre03b   : current, with `session_progress_capture.py`'s
                    `cwd = STATE_ROOT` reverted to `cwd = os.getcwd()` and the
                    same reversion in `precompact_flush.py`. This is the
                    turn-03 state; it isolates the 03b edit.
  builds/head     : every `hooks/*.py` replaced by its `git show HEAD:` blob —
                    the fully pre-rollout state. Read-only; the working tree is
                    never touched.

03b-execute.md finding F-1 governs over 03a-decision.md: for a ledger written by
the current `touched_capture.py` the lines are already STATE_ROOT-relative and
`normalize_path` is a no-op on relative input, so the read base cannot change
touched-task resolution. AC-4's stated control ("the unmodified
`cwd = os.getcwd()` version must fail to match the same line") is therefore
expected to be VACUOUS, and this file measures that rather than asserting it
away: arm AC-4-CTRL records what the reverted build actually does, and arm
AC-4b supplies the base-discriminating case (an ABSOLUTE ledger line under
STATE_ROOT, the one shape the two bases disagree on).

The non-vacuous Stop-side arm (ADD-1) asserts what the 03b edit really changed:
the `_rel()` key shape persisted into `.bind`'s `exec_tried`. It FAILS on the
pre03b build (`../_projects/...`) and PASSES on current (`_projects/...`).

ADD-2 covers the `.bind` key-shape migration 03b-execute.md §2.1 raises: a
`.bind` written before the line-1335 edit, read by the current code, in BOTH
configurations.

Sandbox (`e2e_state_dir_sandbox`, modelled on
`tests/test_touched_capture_state_root.py`): SIX hooks now resolve their root by
walking ANCESTORS, so a temp workspace inside the repo tree resolves to the REAL
`_projects/_state`. Every workspace is a `tempfile.mkdtemp()` tree OUTSIDE the
repo, asserted up front to have no ancestor holding `_projects/_state`, and
every hook is invoked as a SUBPROCESS with an explicit `cwd=` (the roots are
module scope, so importing cannot vary cwd). The real state dir is fingerprinted
before and after with the live session's own churn excluded.

stdlib only; no PEP723 header. Run with:
    uv run --no-project python plugins/taskflow/tests/test_hooks_state_root_rollout.py
Exits 0 when all checks pass, 1 otherwise, 2 on an aborted sandbox guard.
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

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOKS_SRC = REPO_ROOT / "plugins" / "taskflow" / "hooks"
PROMPTS_SRC = REPO_ROOT / "plugins" / "taskflow" / "prompts"
REAL_STATE = REPO_ROOT / "_projects" / "_state"

# Synthetic session id. 36 chars (UUID shape) so the sweep's `len(stem) == 36`
# json branch and every orphan guard see the same shape a real session has.
# Leak detection keys on the FULL id, not on a short prefix: the real state dir
# holds unrelated sessions whose ids can share any 2-4 char prefix (measured
# 2026-08-20: a live `f005be44-...json` made a `f0` prefix test false-positive).
SID = "f0110000-1111-2222-3333-444455556666"
SYNTH_IDS = (SID,)

PROJECT = "demo"
TASK_BASE = "2026-01-01_demo-task.md"
TASK_REL = f"_projects/{PROJECT}/tasks/1_in_progress/{TASK_BASE}"
EXEC_BASE = "2026-01-02_exec-task.md"
EXEC_REL = f"_projects/{PROJECT}/tasks/1_in_progress/{EXEC_BASE}"

TASK_BODY = (
    "# demo task\n\n"
    "<!-- @log:begin -->\n"
    "<!-- @log:end -->\n"
)
# Deliberately unbindable: two `@log:begin`, no `@log:end`. `repair_log_markers`
# returns None for that damage shape, so `append_auto_binding` returns False and
# the exec-bind records a 打止め in `exec_tried` — the only `_rel()` value that
# survives the process.
EXEC_BODY = (
    "# exec task\n\n"
    "<!-- @log:begin -->\n"
    "<!-- @log:begin -->\n"
)

# Substitution anchors. Each is asserted to occur exactly once in its file.
SPC_FIXED_CWD = "    cwd = STATE_ROOT"
SPC_PRE_CWD = "    cwd = os.getcwd()"

PASS = 0
FAIL = 0
NOTES: list[str] = []


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


def note(msg: str) -> None:
    """A measured observation that is reported but does not gate the exit code
    (used where 02-plan.md's stated control is expected to be non-discriminating
    per 03b-execute.md F-1)."""
    NOTES.append(msg)
    print(f"  NOTE: {msg}")


def die(msg: str) -> None:
    print(f"  ABORT: {msg}")
    raise SystemExit(2)


# --- sandbox guards --------------------------------------------------------

def ancestors_with_state(start: Path) -> list[str]:
    """Every ancestor of `start` (inclusive) holding `_projects/_state` — i.e.
    exactly what `_find_state_root` would walk into."""
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
            f"{hits}. The ancestor search would walk into them.")
    ok(f"G1: no ancestor of {root} holds _projects/_state")
    try:
        rel = root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        ok(f"G2: temp tree is outside the repo ({REPO_ROOT})")
    else:
        die(f"G2 VIOLATED: temp tree is INSIDE the repo ({rel})")


def state_snapshot() -> list[str]:
    return sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []


def churn_excluded(names: list[str], live: set[str]) -> list[str]:
    return [n for n in names if n[:8] not in live]


def live_prefixes(before: list[str], after: list[str]) -> set[str]:
    """Session prefixes whose state json is present in BOTH snapshots. Those
    sessions are alive and rewrite their own `.touched` every Stop
    (e2e_state_dir_sandbox step 3 sub-bullet: a raw count comparison is a
    false-positive generator). A DEAD session's json disappearing is exactly
    what the guard must still catch, and such a prefix is not in this set."""
    jb = {n[:8] for n in before if n.endswith(".json") and len(n) == 41}
    ja = {n[:8] for n in after if n.endswith(".json") and len(n) == 41}
    return jb & ja


# --- builds ----------------------------------------------------------------

def _copy_plugin(dest_root: Path) -> Path:
    tf = dest_root / "taskflow"
    shutil.copytree(HOOKS_SRC, tf / "hooks",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(PROMPTS_SRC, tf / "prompts")
    return tf / "hooks"


def _git_show(rel: str) -> str | None:
    r = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=str(REPO_ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.stdout if r.returncode == 0 else None


def make_builds(root: Path) -> dict[str, Path]:
    """current / pre03b / head, all derived from the real tree. No git write
    operation of any kind: `git show` only."""
    builds: dict[str, Path] = {}

    builds["current"] = _copy_plugin(root / "builds" / "current")

    pre = _copy_plugin(root / "builds" / "pre03b")
    for fname in ("session_progress_capture.py", "precompact_flush.py"):
        p = pre / fname
        src = io.open(p, encoding="utf-8").read()
        if src.count(SPC_FIXED_CWD) != 1:
            die(f"{fname} does not carry {SPC_FIXED_CWD!r} exactly once "
                f"(count={src.count(SPC_FIXED_CWD)}) — the 03b edit is not in "
                f"place, so its control cannot be derived")
        io.open(p, "w", encoding="utf-8", newline="").write(
            src.replace(SPC_FIXED_CWD, SPC_PRE_CWD, 1))
    builds["pre03b"] = pre

    head = _copy_plugin(root / "builds" / "head")
    replaced = []
    for py in sorted(head.glob("*.py")):
        blob = _git_show(f"plugins/taskflow/hooks/{py.name}")
        if blob is not None:
            io.open(py, "w", encoding="utf-8", newline="").write(blob)
            replaced.append(py.name)
    hsrc = io.open(head / "session_progress_capture.py", encoding="utf-8").read()
    if "SWEEP_STATE_DIR" in hsrc or "_find_state_root" in hsrc:
        die("the HEAD blob of session_progress_capture.py already carries the "
            "rollout — HEAD is not a pre-rollout control")
    if "PROGRESS_ROOT = os.path.join(os.getcwd(), '_projects')" not in hsrc:
        die("the HEAD blob of session_progress_capture.py does not carry the "
            "cwd-direct root — the control build is not what it claims")
    ok(f"HEAD control build derived read-only via `git show` "
       f"({len(replaced)} files)")
    builds["head"] = head

    return builds


# --- fixture ---------------------------------------------------------------

def build_ws(root: Path, name: str, *, state: dict | None,
             tasks: dict[str, str] | None = None,
             bind: dict | None = None) -> Path:
    ws = root / name
    (ws / "sub").mkdir(parents=True, exist_ok=True)
    (ws / "_projects" / "_state").mkdir(parents=True, exist_ok=True)
    (ws / "_projects" / PROJECT / "tasks" / "1_in_progress").mkdir(
        parents=True, exist_ok=True)
    io.open(ws / "_projects" / PROJECT / "progress.md", "w",
            encoding="utf-8").write("# progress\n")
    for rel, body in (tasks or {}).items():
        io.open(ws / rel, "w", encoding="utf-8").write(body)
    if state is not None:
        io.open(ws / "_projects" / "_state" / f"{SID}.json", "w",
                encoding="utf-8").write(json.dumps(state))
    if bind is not None:
        io.open(ws / "_projects" / "_state" / f"{SID}.bind", "w",
                encoding="utf-8").write(json.dumps(bind))
    return ws


def run_hook(hooks: Path, script: str, cwd: Path, payload: dict,
             env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """e2e_state_dir_sandbox step 2: `uv run --no-project` (the hooks carry no
    PEP723 header, and from a temp cwd uv would otherwise resolve this repo's
    pyproject). G5: subprocess with an explicit cwd, never an import."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("TASKFLOW_SWEEP_MAX", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(hooks / script)],
        cwd=str(cwd), input=json.dumps(payload), capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=env)


def read_json(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(io.open(p, encoding="utf-8").read())
    except ValueError:
        return "<unparseable>"


def touched_lines(ws: Path) -> list[str] | None:
    p = ws / "_projects" / "_state" / f"{SID}.touched"
    if not p.exists():
        return None
    return [ln for ln in io.open(p, encoding="utf-8").read().splitlines() if ln]


def bind_of(ws: Path):
    return read_json(ws / "_projects" / "_state" / f"{SID}.bind")


def state_json_of(ws: Path):
    return read_json(ws / "_projects" / "_state" / f"{SID}.json")


def stop_payload(msg: str = "done for now") -> dict:
    return {"session_id": SID, "last_assistant_message": msg}


def write_payload(ws: Path, rel: str) -> dict:
    return {"session_id": SID, "tool_name": "Write",
            "tool_input": {"file_path": str(ws / rel)}}


# --- arms ------------------------------------------------------------------

def arm_ac1(root: Path, builds: dict[str, Path]) -> None:
    print()
    print("--- AC-1: subdir-launched session — init writes state, "
          "touched_capture appends, Stop opens a round ---")

    ws = build_ws(root, "ac1_current", state=None,
                  tasks={TASK_REL: TASK_BODY})
    sub = ws / "sub"

    r = run_hook(builds["current"], "session_init.py", sub,
                 {"session_id": SID, "prompt": f"pj:{PROJECT} start",
                  "transcript_path": ""})
    st = state_json_of(ws)
    check(isinstance(st, dict) and st.get("project") == PROJECT,
          f"[fixed] session_init at <ws>/sub wrote <ws>/_projects/_state/"
          f"<sid>.json with project={PROJECT!r} (got {st!r}, rc={r.returncode})")
    check(not (ws / "sub" / "_projects").exists(),
          "[fixed] no stray <ws>/sub/_projects/ tree was created")

    r = run_hook(builds["current"], "touched_capture.py", sub,
                 write_payload(ws, TASK_REL))
    got = touched_lines(ws)
    check(got == [TASK_REL],
          f"[fixed] touched_capture at <ws>/sub appended [{TASK_REL!r}] to "
          f"<ws>/_projects/_state/<sid>.touched (got {got!r}, rc={r.returncode})")

    r = run_hook(builds["current"], "session_progress_capture.py", sub,
                 stop_payload())
    b = bind_of(ws)
    cap = b.get("capture") if isinstance(b, dict) else None
    try:
        out = json.loads(r.stdout.strip()) if r.stdout.strip() else {}
    except ValueError:
        out = {}
    check(isinstance(cap, dict) and cap.get("status") == "requested"
          and cap.get("round") == 1
          and cap.get("items", {}).get("tasks") == [f"{PROJECT}/{TASK_BASE}"],
          f"[fixed] Stop at <ws>/sub opened round 1 for {PROJECT}/{TASK_BASE} "
          f"(capture={cap!r}, rc={r.returncode})")
    check(out.get("decision") == "block"
          and "Spawn the async capture subagent" in out.get("reason", ""),
          f"[fixed] Stop blocked and requested the capture subagent "
          f"(G6 positive execution marker; stdout={r.stdout.strip()[:160]!r})")

    # ---- control: the same fixture against the pre-rollout build ----
    ws2 = build_ws(root, "ac1_head", state=None, tasks={TASK_REL: TASK_BODY})
    sub2 = ws2 / "sub"
    r = run_hook(builds["head"], "session_init.py", sub2,
                 {"session_id": SID, "prompt": f"pj:{PROJECT} start",
                  "transcript_path": ""})
    st2 = state_json_of(ws2)
    stray = ws2 / "sub" / "_projects" / "_state" / f"{SID}.json"
    check(st2 is None,
          f"[control, PRE-ROLLOUT] session_init at <ws>/sub wrote NO state json "
          f"under <ws>/_projects/_state (got {st2!r}, rc={r.returncode})")
    check(stray.exists(),
          f"[control, PRE-ROLLOUT] it created the STRAY tree instead: "
          f"<ws>/sub/_projects/_state/<sid>.json exists = {stray.exists()}")

    r = run_hook(builds["head"], "touched_capture.py", sub2,
                 write_payload(ws2, TASK_REL))
    got2 = touched_lines(ws2)
    check(got2 is None,
          f"[control, PRE-ROLLOUT] touched_capture wrote no ledger under "
          f"<ws>/_projects/_state (got {got2!r}, rc={r.returncode})")

    r = run_hook(builds["head"], "session_progress_capture.py", sub2,
                 stop_payload())
    check(r.returncode == 0 and not r.stdout.strip() and bind_of(ws2) is None,
          f"[control, PRE-ROLLOUT] Stop at <ws>/sub is a silent no-op "
          f"(rc={r.returncode}, stdout={r.stdout.strip()[:120]!r}, "
          f"bind={bind_of(ws2)!r})")


def arm_ac4(root: Path, builds: dict[str, Path]) -> None:
    print()
    print("--- AC-4: PreCompact reads the ledger against the same base "
          "touched_capture wrote it with ---")

    def pc_ws(name: str) -> Path:
        return build_ws(root, name,
                        state={"session_id": SID, "project": PROJECT},
                        tasks={TASK_REL: TASK_BODY})

    ws = pc_ws("ac4_current")
    sub = ws / "sub"
    run_hook(builds["current"], "touched_capture.py", sub,
             write_payload(ws, TASK_REL))
    r = run_hook(builds["current"], "precompact_flush.py", sub,
                 {"session_id": SID, "hook_event_name": "PreCompact",
                  "trigger": "manual"})
    want = f"{PROJECT}/{TASK_BASE}"
    body = io.open(ws / TASK_REL, encoding="utf-8").read()
    check(want in r.stdout and "Preserve verbatim" in r.stdout,
          f"[fixed] PreCompact at <ws>/sub named {want!r} "
          f"(stdout={r.stdout.strip()[:200]!r}, rc={r.returncode})")
    check("(auto) unflushed at compaction" in body,
          "[fixed] PreCompact appended its placeholder into the task @log "
          "(G6 positive execution marker)")

    # ---- 02-plan.md §5.2's stated control, measured rather than assumed ----
    ws_c = pc_ws("ac4_pre03b")
    sub_c = ws_c / "sub"
    run_hook(builds["current"], "touched_capture.py", sub_c,
             write_payload(ws_c, TASK_REL))
    rc_ = run_hook(builds["pre03b"], "precompact_flush.py", sub_c,
                   {"session_id": SID, "hook_event_name": "PreCompact",
                    "trigger": "manual"})
    matched = want in rc_.stdout
    if matched:
        note(f"AC-4's stated control is VACUOUS, as 03b-execute.md F-1 predicts: "
             f"the `cwd = os.getcwd()` build ALSO matched the line "
             f"(stdout={rc_.stdout.strip()[:160]!r}). The current writer emits "
             f"STATE_ROOT-relative lines and `normalize_path` is a no-op on "
             f"relative input, so the read base cannot discriminate here. "
             f"AC-4b below supplies the arm that does.")
    else:
        ok(f"[control, PRE-03b] the cwd-based build failed to match "
           f"(stdout={rc_.stdout.strip()[:160]!r})")

    # ---- rollout-level control: pre-rollout PreCompact produces nothing ----
    ws_h = pc_ws("ac4_head")
    sub_h = ws_h / "sub"
    run_hook(builds["current"], "touched_capture.py", sub_h,
             write_payload(ws_h, TASK_REL))
    rh = run_hook(builds["head"], "precompact_flush.py", sub_h,
                  {"session_id": SID, "hook_event_name": "PreCompact",
                   "trigger": "manual"})
    check(not rh.stdout.strip(),
          f"[control, PRE-ROLLOUT] PreCompact at <ws>/sub emits nothing at all "
          f"(rc={rh.returncode}, stdout={rh.stdout.strip()[:160]!r})")

    # ---- AC-4b: the shape the two bases DO disagree on ----
    print()
    print("--- AC-4b: an ABSOLUTE ledger line under STATE_ROOT — the base is "
          "load-bearing here (non-vacuous) ---")
    for label, build in (("fixed", "current"), ("PRE-03b", "pre03b")):
        wsx = pc_ws(f"ac4b_{build}")
        abs_line = str(wsx / TASK_REL).replace("\\", "/")
        io.open(wsx / "_projects" / "_state" / f"{SID}.touched", "w",
                encoding="utf-8").write(abs_line + "\n")
        rx = run_hook(builds[build], "precompact_flush.py", wsx / "sub",
                      {"session_id": SID, "hook_event_name": "PreCompact",
                       "trigger": "manual"})
        hit = want in rx.stdout
        if build == "current":
            check(hit,
                  f"[{label}] base=STATE_ROOT strips the absolute line and "
                  f"resolves it (stdout={rx.stdout.strip()[:160]!r})")
        else:
            check(not hit,
                  f"[control, {label}] base=os.getcwd() leaves it absolute, "
                  f"`_PROJECT_RE` drops it, nothing is flushed "
                  f"(stdout={rx.stdout.strip()[:160]!r})")


def arm_ac7(root: Path, builds: dict[str, Path]) -> None:
    print()
    print("--- AC-7: the search-derived root and the pinned sweep target are "
          "DIFFERENT under a nested cwd ---")
    ws = build_ws(root, "ac7", state=None, tasks={TASK_REL: TASK_BODY})
    probe = root / "roots_probe.py"
    io.open(probe, "w", encoding="utf-8").write(
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('spc', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "print('STATE_ROOT=' + m.STATE_ROOT)\n"
        "print('STATE_DIR=' + m.STATE_DIR)\n"
        "print('SWEEP_STATE_DIR=' + m.SWEEP_STATE_DIR)\n"
    )

    def probe_at(cwd: Path) -> dict[str, str]:
        r = subprocess.run(
            ["uv", "run", "--no-project", "python", str(probe),
             str(builds["current"] / "session_progress_capture.py")],
            cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        out = {}
        for ln in r.stdout.splitlines():
            if "=" in ln:
                k, _, v = ln.partition("=")
                out[k] = v
        out["_rc"] = str(r.returncode)
        out["_stderr"] = r.stderr.strip()[:200]
        return out

    nested = probe_at(ws / "sub")
    want_state = str(ws / "_projects" / "_state")
    want_sweep = str(ws / "sub" / "_projects" / "_state")
    check(nested.get("STATE_DIR") != nested.get("SWEEP_STATE_DIR"),
          f"[nested cwd] STATE_DIR != SWEEP_STATE_DIR "
          f"({nested.get('STATE_DIR')!r} vs {nested.get('SWEEP_STATE_DIR')!r})")
    check(os.path.normcase(nested.get("STATE_DIR", "")) ==
          os.path.normcase(want_state),
          f"[nested cwd] STATE_DIR followed the ancestor search to "
          f"{want_state!r} (got {nested.get('STATE_DIR')!r})")
    check(os.path.normcase(nested.get("SWEEP_STATE_DIR", "")) ==
          os.path.normcase(want_sweep),
          f"[nested cwd] SWEEP_STATE_DIR stayed pinned to the cwd "
          f"{want_sweep!r} (got {nested.get('SWEEP_STATE_DIR')!r})")

    at_root = probe_at(ws)
    check(at_root.get("STATE_DIR") == at_root.get("SWEEP_STATE_DIR"),
          f"[control, root cwd] the two constants COINCIDE when the cwd already "
          f"holds the state dir — the asymmetry is cwd-driven, not a constant "
          f"fork ({at_root.get('STATE_DIR')!r})")


def arm_add1(root: Path, builds: dict[str, Path]) -> None:
    print()
    print("--- ADD-1 (non-vacuous Stop-side arm): the persisted `exec_tried` "
          "key shape — FAILS on pre-03b code, PASSES on current ---")

    def exec_ws(name: str) -> Path:
        return build_ws(
            root, name,
            state={"session_id": SID, "project": PROJECT,
                   "exec_bind": [EXEC_BASE]},
            tasks={TASK_REL: TASK_BODY, EXEC_REL: EXEC_BODY})

    results: dict[str, tuple[list, str]] = {}
    for build in ("current", "pre03b"):
        ws = exec_ws(f"add1_{build}_B")
        r = run_hook(builds[build], "session_progress_capture.py", ws / "sub",
                     stop_payload())
        b = bind_of(ws)
        tried = b.get("exec_tried") if isinstance(b, dict) else []
        results[build] = (tried, r.stderr)

    cur_tried, cur_err = results["current"]
    pre_tried, pre_err = results["pre03b"]
    check(cur_tried == [EXEC_REL],
          f"[fixed, config B] `.bind` exec_tried == [{EXEC_REL!r}] "
          f"(got {cur_tried!r})")
    check(pre_tried == [f"../{EXEC_REL}"],
          f"[control, PRE-03b, config B] the SAME arm yields "
          f"['../{EXEC_REL}'] — the assertion above genuinely fails "
          f"against pre-edit code (got {pre_tried!r})")
    check(f"auto-skip(ambiguous): {EXEC_REL} " in cur_err
          or f"auto-skip(ambiguous): {EXEC_REL}\n" in cur_err,
          f"[fixed, config B] the stderr report carries the `_projects/` shape "
          f"(stderr={cur_err.strip()[:200]!r})")
    check(f"auto-skip(ambiguous): ../{EXEC_REL}" in pre_err,
          f"[control, PRE-03b, config B] the same report carried `../_projects/` "
          f"(stderr={pre_err.strip()[:200]!r})")

    # ---- configuration A: the substitution is string-identical ----
    a_tried: dict[str, list] = {}
    for build in ("current", "pre03b"):
        ws = exec_ws(f"add1_{build}_A")
        run_hook(builds[build], "session_progress_capture.py", ws,
                 stop_payload())
        b = bind_of(ws)
        a_tried[build] = b.get("exec_tried") if isinstance(b, dict) else []
    check(a_tried["current"] == [EXEC_REL] == a_tried["pre03b"],
          f"[control, config A] both builds compute the identical key at a "
          f"root cwd — 03b's substitution is a no-op there "
          f"(current={a_tried['current']!r}, pre03b={a_tried['pre03b']!r})")


def arm_add2(root: Path, builds: dict[str, Path]) -> None:
    print()
    print("--- ADD-2: a `.bind` written BEFORE the line-1335 edit, read by the "
          "current code (03b-execute.md §2.1 / F-2) ---")

    def seeded(name: str, tried: list[str]) -> Path:
        return build_ws(
            root, name,
            state={"session_id": SID, "project": PROJECT,
                   "exec_bind": [EXEC_BASE]},
            tasks={TASK_REL: TASK_BODY, EXEC_REL: EXEC_BODY},
            bind={"reminded": {"someone": 1}, "exec_tried": tried,
                  "capture": {"status": "",
                              "items": {"tasks": [], "notes": [],
                                        "allow_tasks": []},
                              "requested_ts": 0, "tried_notes": [],
                              "tried_tasks": [], "touch_cursor": 0,
                              "round": 7, "log_seen": {},
                              "round_base": {}, "history": {}}})

    # --- configuration B: the shape a turn-03-window subdir session wrote ---
    old_key = f"../{EXEC_REL}"
    ws = seeded("add2_B", [old_key])
    r1 = run_hook(builds["current"], "session_progress_capture.py", ws / "sub",
                  stop_payload())
    b1 = bind_of(ws)
    check(isinstance(b1, dict) and isinstance(b1.get("capture"), dict),
          f"[config B] the pre-edit `.bind` is NOT unreadable: it parses and "
          f"round-trips (got {type(b1).__name__})")
    check(isinstance(b1, dict)
          and b1.get("capture", {}).get("round") == 7
          and b1.get("reminded") == {"someone": 1},
          f"[config B] the whole `capture` lifecycle block and `reminded` are "
          f"honoured verbatim (round={b1.get('capture', {}).get('round')!r}, "
          f"reminded={b1.get('reminded')!r})")
    check(f"auto-skip(ambiguous): {EXEC_REL}" in r1.stderr,
          f"[config B] DEGRADED: the old-shape 打止め no longer matches, so the "
          f"exec-bind is retried and re-reported once "
          f"(stderr={r1.stderr.strip()[:200]!r})")
    check(b1.get("exec_tried") == [old_key, EXEC_REL],
          f"[config B] the old entry SURVIVES as a permanently dead string and "
          f"the new shape is appended next to it (got "
          f"{b1.get('exec_tried')!r})")

    r2 = run_hook(builds["current"], "session_progress_capture.py", ws / "sub",
                  stop_payload())
    b2 = bind_of(ws)
    check(f"auto-skip(ambiguous): {EXEC_REL}" not in r2.stderr
          and b2.get("exec_tried") == [old_key, EXEC_REL],
          f"[config B] the cost is bounded to ONE redundant cycle: the second "
          f"Stop is silent and appends nothing "
          f"(stderr={r2.stderr.strip()[:160]!r}, "
          f"exec_tried={b2.get('exec_tried')!r})")

    # --- configuration A: a pre-edit root-launched `.bind` ---
    ws_a = seeded("add2_A", [EXEC_REL])
    ra = run_hook(builds["current"], "session_progress_capture.py", ws_a,
                  stop_payload())
    ba = bind_of(ws_a)
    check(f"auto-skip(ambiguous)" not in ra.stderr
          and ba.get("exec_tried") == [EXEC_REL]
          and ba.get("capture", {}).get("round") == 7,
          f"[config A] UNAFFECTED: a pre-edit `.bind` written at a root cwd "
          f"already holds `_projects/` keys, the 打止め still fires and nothing "
          f"is appended (stderr={ra.stderr.strip()[:160]!r}, "
          f"exec_tried={ba.get('exec_tried')!r})")


# --- driver ----------------------------------------------------------------

def main() -> int:
    print("=== ancestor-search rollout: AC-1 / AC-4 / AC-7 + ADD-1 / ADD-2 ===")
    print()
    if not HOOKS_SRC.is_dir():
        die(f"hooks dir not found: {HOOKS_SRC}")

    before = state_snapshot()
    print(f"real _projects/_state BEFORE: {len(before)} entries")

    tmp_root = Path(tempfile.mkdtemp(prefix="tf_rollout_"))
    print(f"temp root: {tmp_root}")
    print()
    print("--- sandbox guards (02-plan.md §5.3 G1/G2) ---")
    assert_isolated(tmp_root)

    aborted = True
    try:
        builds = make_builds(tmp_root)
        arm_ac1(tmp_root, builds)
        arm_ac4(tmp_root, builds)
        arm_ac7(tmp_root, builds)
        arm_add1(tmp_root, builds)
        arm_add2(tmp_root, builds)
        aborted = False
    finally:
        print()
        print("--- G3/G4: real _projects/_state fingerprint ---")
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
    for n in NOTES:
        print(f"NOTE: {n}")
    if FAIL == 0 and not aborted:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
