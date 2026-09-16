#!/usr/bin/env python3
from __future__ import annotations

import gc
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOKS = REPO_ROOT / "plugins" / "taskflow" / "hooks"
REAL_STATE = REPO_ROOT / "_projects" / "_state"

sys.path.insert(0, str(HOOKS))
import qmd_index  # noqa: E402

MODE_ENV = qmd_index.MODE_ENV

SID = "c7a10000-1111-2222-3333-444455556666"
SID_TAG = SID.replace("-", "")[-12:]
SYNTH_IDS = (SID,)

PROJECT = "demo"
TASK_REL = f"_projects/{PROJECT}/tasks/1_in_progress/2026-01-01_demo-task.md"
TASK_BODY = "# demo task\n\n<!-- @log:begin -->\n<!-- @log:end -->\n"
NOTE_REL = f"_projects/{PROJECT}/project-notes/specs/retrieval.md"
NOTE_BODY = "---\nsummary: demo\n---\n\n# retrieval\n"

NOTES_DIR_REL = f"_projects/{PROJECT}/project-notes"
EMBED_DELAY_MS = 4000
UPDATE_BLOCK_MS = 400
LATE_POLL_S = 30.0
SPAWN_POLL_S = 20.0
SETTLE_S = 5.0
UPDATE_TIMEOUT_S = qmd_index.UPDATE_TIMEOUT_S

SPAWN_LATENCY: list[float] = []

FAKE_QMD_JS = r"""
const fs = require('fs');
const log = process.env.TF_FAKE_QMD_LOG;
const argv = process.argv.slice(2);
function rec(tag) {
  fs.appendFileSync(log, JSON.stringify(
    {tag: tag, argv: argv, cwd: process.cwd(), pid: process.pid, t: Date.now()}
  ) + "\n");
}
function block(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}
if (argv[0] === 'update') {
  block(Number(process.env.TF_FAKE_QMD_UPDATE_BLOCK_MS || 0));
  rec('update');
} else {
  rec('embed');
  const late = Number(process.env.TF_FAKE_QMD_EMBED_DELAY_MS || 0);
  if (late > 0) { block(late); rec('embed-late'); }
}
"""

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
            f"{hits}. The hook would resolve STATE_DIR to one of them.")
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


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="").write(text)


def build_ws(root: Path, name: str, with_qmd: bool) -> Path:
    ws = root / name
    (ws / "_projects" / "_state").mkdir(parents=True, exist_ok=True)
    write(ws / TASK_REL, TASK_BODY)
    write(ws / NOTE_REL, NOTE_BODY)
    write(ws / "_projects" / "_state" / f"{SID}.json",
          json.dumps({"session_id": SID, "project": PROJECT}))
    if with_qmd:
        (ws / NOTES_DIR_REL / ".qmd").mkdir(parents=True, exist_ok=True)
    return ws


def hook_env(log: Path, fake_bin: Path, embed_delay_ms: int,
             update_block_ms: int, push_mode: str | None) -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TASKFLOW_QMD_BIN"] = str(fake_bin)
    env["TF_FAKE_QMD_LOG"] = str(log)
    env["TF_FAKE_QMD_UPDATE_BLOCK_MS"] = str(update_block_ms)
    env["TF_FAKE_QMD_EMBED_DELAY_MS"] = str(embed_delay_ms)
    env.pop("TASKFLOW_SWEEP_MAX", None)
    env.pop(MODE_ENV, None)
    if push_mode is not None:
        env[MODE_ENV] = push_mode
    return env


def touch_ledger(ws: Path, rels: list[str]) -> None:
    for rel in rels:
        subprocess.run(
            ["uv", "run", "--no-project", "python",
             str(HOOKS / "touched_capture.py")],
            cwd=str(ws), input=json.dumps(
                {"session_id": SID, "tool_name": "Write",
                 "tool_input": {"file_path": str(ws / rel)}}),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def run_stop(ws: Path, env: dict) -> tuple[subprocess.CompletedProcess, float]:
    cp = subprocess.run(
        ["uv", "run", "--no-project", "python",
         str(HOOKS / "session_progress_capture.py")],
        cwd=str(ws), input=json.dumps(
            {"session_id": SID, "last_assistant_message": "done"}),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env)
    return cp, time.time()


def read_log(log: Path) -> list[dict]:
    if not log.exists():
        return []
    out = []
    for line in io.open(log, encoding="utf-8").read().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def tags(entries: list[dict]) -> list[str]:
    return [e["tag"] for e in entries]


def wait_for(log: Path, tag: str, timeout: float) -> float | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tag in tags(read_log(log)):
            return time.time()
        time.sleep(0.05)
    return None


def settled(log: Path) -> list[dict]:
    time.sleep(SETTLE_S)
    return read_log(log)


def latency_note() -> str:
    if not SPAWN_LATENCY:
        return "spawn latency not yet measured"
    return (f"the measured spawn-to-marker latency is "
            f"{max(SPAWN_LATENCY):.2f}s, against a {SETTLE_S:.1f}s settle "
            f"window")


def bind_exists(ws: Path) -> bool:
    return (ws / "_projects" / "_state" / f"{SID}.bind").exists()


def lock_file(ws: Path) -> Path:
    return ws / NOTES_DIR_REL / ".qmd" / "embed.lock"


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.wait()
    pid = p.pid
    handle = getattr(p, "_handle", None)
    if handle is not None and hasattr(handle, "Close"):
        handle.Close()
    del p
    gc.collect()
    return pid


def arm(root: Path, fake: Path, name: str, *, with_qmd: bool,
        ledger: list[str], lock: dict | None = None,
        embed_delay_ms: int = 0,
        update_block_ms: int = UPDATE_BLOCK_MS,
        push_mode: str | None = None) -> dict:
    ws = build_ws(root, name, with_qmd)
    log = root / f"{name}.log"
    if lock is not None:
        write(lock_file(ws), json.dumps(lock))
    touch_ledger(ws, ledger)
    env = hook_env(log, fake, embed_delay_ms, update_block_ms, push_mode)
    t_start = time.time()
    cp, t_return = run_stop(ws, env)
    return {"ws": ws, "log": log, "cp": cp, "t_return": t_return, "env": env,
            "elapsed": t_return - t_start, "entries": read_log(log)}


def arm_note_round(root: Path, fake: Path) -> None:
    print()
    print("--- a round that WROTE a note: update sync, embed detached ---")
    r = arm(root, fake, "a1_note_round", with_qmd=True,
            ledger=[TASK_REL, NOTE_REL], embed_delay_ms=EMBED_DELAY_MS)
    entries, ws = r["entries"], r["ws"]

    check(bind_exists(ws),
          "positive execution marker: the Stop ran to completion and wrote "
          f"<ws>/_projects/_state/{SID}.bind")
    check(MODE_ENV not in r["env"],
          f"this arm ran with {MODE_ENV} unset — it pins the default path")
    check("update" in tags(entries),
          f"`update` ran, and it ran SYNCHRONOUSLY — its {UPDATE_BLOCK_MS} ms "
          f"blocking marker is already on disk when the hook returns "
          f"(tags={tags(entries)!r})")

    upd = [e for e in entries if e["tag"] == "update"]
    if upd:
        check(upd[0]["argv"] == ["update"],
              f"update argv is exactly ['update'] ({upd[0]['argv']!r})")
        check(Path(upd[0]["cwd"]).resolve() == (ws / NOTES_DIR_REL).resolve(),
              f"update ran in the project's project-notes/ dir "
              f"({upd[0]['cwd']!r})")

    check("embed-late" not in tags(entries),
          "the embed was NOT waited on: its delayed marker is still absent "
          "when the hook returns")

    t_spawn = wait_for(r["log"], "embed", SPAWN_POLL_S)
    check(t_spawn is not None,
          f"an embed was started (tags={tags(read_log(r['log']))!r})")
    if t_spawn is not None:
        SPAWN_LATENCY.append(t_spawn - r["t_return"])
        print(f"    (embed marker appeared {t_spawn - r['t_return']:.2f}s "
              f"after the hook returned)")

    emb = [e for e in read_log(r["log"]) if e["tag"] == "embed"]
    check(len(emb) == 1,
          f"exactly one embed was started "
          f"(tags={tags(read_log(r['log']))!r})")
    if emb:
        argv = emb[0]["argv"]
        check(argv == ["embed", "--no-gpu"],
              f"embed argv is exactly ['embed', '--no-gpu'] ({argv!r})")
        check("-f" not in argv and "--force" not in argv,
              f"embed argv carries no full-regeneration flag ({argv!r})")
        check(Path(emb[0]["cwd"]).resolve() == (ws / NOTES_DIR_REL).resolve(),
              f"embed ran in the project's project-notes/ dir "
              f"({emb[0]['cwd']!r})")

    late = wait_for(r["log"], "embed-late", LATE_POLL_S)
    check(late is not None,
          f"the detached embed OUTLIVED the hook process: its delayed marker "
          f"appeared {EMBED_DELAY_MS} ms after spawn, with the hook already "
          f"exited")

    info = json.loads(io.open(lock_file(ws), encoding="utf-8").read())
    check(info.get("sid") == SID_TAG and info.get("round") == 0
          and isinstance(info.get("pid"), int),
          f"the lock records pid/sid/round ({info!r})")
    if emb:
        check(info.get("pid") == emb[0]["pid"],
              f"the lock's pid is the embed that was actually spawned "
              f"({info.get('pid')} vs {emb[0]['pid']})")


def arm_no_note(root: Path, fake: Path) -> None:
    print()
    print("--- a round with NO note write: qmd is never started ---")
    r = arm(root, fake, "a2_no_note", with_qmd=True, ledger=[TASK_REL])
    check(bind_exists(r["ws"]),
          "positive execution marker: the Stop ran to completion and wrote "
          "the .bind — the arm is not a skipped hook")
    final = settled(r["log"])
    check(final == [],
          f"neither update nor embed was started; {latency_note()} "
          f"(entries={final!r})")
    check(not lock_file(r["ws"]).exists(),
          "no embed lock was created")


def arm_no_qmd_dir(root: Path, fake: Path) -> None:
    print()
    print("--- a note-writing round in a project with NO .qmd/: nothing runs ---")
    r = arm(root, fake, "a3_no_qmd", with_qmd=False,
            ledger=[TASK_REL, NOTE_REL])
    check(bind_exists(r["ws"]),
          "positive execution marker: the Stop ran to completion and wrote "
          "the .bind")
    final = settled(r["log"])
    check(final == [],
          f"qmd was never invoked for a project that is not onboarded; "
          f"{latency_note()} (entries={final!r})")
    check(not (r["ws"] / NOTES_DIR_REL / ".qmd").exists(),
          "the hook did not create a .qmd/ dir of its own")


def arm_lock_alive(root: Path, fake: Path) -> None:
    print()
    print("--- lock held by a LIVE pid: the embed is skipped ---")
    alive = os.getpid()
    planted = {"pid": alive, "sid": "foreignsid00", "round": 99}
    check(qmd_index._pid_alive(alive),
          f"non-vacuity: the planted pid {alive} really is alive")
    r = arm(root, fake, "a4_lock_alive", with_qmd=True,
            ledger=[TASK_REL, NOTE_REL], lock=planted)
    check(bind_exists(r["ws"]),
          "positive execution marker: the Stop ran to completion and wrote "
          "the .bind")
    final = settled(r["log"])
    check("embed" not in tags(final),
          f"no embed was started while the lock's pid is alive; "
          f"{latency_note()} (tags={tags(final)!r})")
    check("update" in tags(final),
          f"`update` still ran — the lock caps the CPU-bound embed only "
          f"(tags={tags(final)!r})")
    info = json.loads(io.open(lock_file(r["ws"]), encoding="utf-8").read())
    check(info == planted,
          f"the live lock was left untouched ({info!r})")


def arm_lock_stale(root: Path, fake: Path) -> None:
    print()
    print("--- lock whose pid is DEAD and whose round is another session's: "
          "retaken ---")
    pid = dead_pid()
    planted = {"pid": pid, "sid": "foreignsid00", "round": 99}
    check(not qmd_index._pid_alive(pid),
          f"non-vacuity: the planted pid {pid} really is dead")
    r = arm(root, fake, "a5_lock_stale", with_qmd=True,
            ledger=[TASK_REL, NOTE_REL], lock=planted)
    check(bind_exists(r["ws"]),
          "positive execution marker: the Stop ran to completion and wrote "
          "the .bind")
    wait_for(r["log"], "embed", SPAWN_POLL_S)
    emb = [e for e in read_log(r["log"]) if e["tag"] == "embed"]
    check(len(emb) == 1,
          f"a stale lock did not block the embed "
          f"(tags={tags(read_log(r['log']))!r})")
    info = json.loads(io.open(lock_file(r["ws"]), encoding="utf-8").read())
    check(info != planted and info.get("sid") == SID_TAG,
          f"the stale lock was retaken by this session ({info!r})")


def arm_lock_same_round(root: Path, fake: Path) -> None:
    print()
    print("--- lock whose pid is DEAD but names THIS round: still capped ---")
    pid = dead_pid()
    planted = {"pid": pid, "sid": SID_TAG, "round": 0}
    check(not qmd_index._pid_alive(pid),
          f"non-vacuity: the planted pid {pid} really is dead")
    r = arm(root, fake, "a6_lock_same_round", with_qmd=True,
            ledger=[TASK_REL, NOTE_REL], lock=planted)
    check(bind_exists(r["ws"]),
          "positive execution marker: the Stop ran to completion and wrote "
          "the .bind")
    final = settled(r["log"])
    check("embed" not in tags(final),
          f"the round's second embed was capped even though the first one had "
          f"already exited; {latency_note()} (tags={tags(final)!r})")
    info = json.loads(io.open(lock_file(r["ws"]), encoding="utf-8").read())
    check(info == planted,
          f"the round-pinned lock was left untouched ({info!r})")


def arm_update_timeout(root: Path, fake: Path) -> None:
    print()
    print(f"--- a hung `update` is cut at {UPDATE_TIMEOUT_S:.0f}s and the Stop "
          f"finishes anyway ---")
    hang_ms = int(UPDATE_TIMEOUT_S * 1000) + 5000
    r = arm(root, fake, "a7_update_timeout", with_qmd=True,
            ledger=[TASK_REL, NOTE_REL], update_block_ms=hang_ms)
    check(bind_exists(r["ws"]),
          "positive execution marker: the Stop ran to completion and wrote "
          "the .bind")
    check(r["elapsed"] < hang_ms / 1000.0,
          f"the Stop did not wait out the hung update "
          f"({r['elapsed']:.2f}s against a {hang_ms / 1000.0:.1f}s hang)")
    check(r["elapsed"] >= UPDATE_TIMEOUT_S,
          f"the {UPDATE_TIMEOUT_S:.0f}s update ceiling is the bound that cut "
          f"it, and it is the worst case a Stop can pay here "
          f"({r['elapsed']:.2f}s)")
    check("update" not in tags(read_log(r["log"])),
          f"the hung update was killed before it could finish "
          f"(tags={tags(read_log(r['log']))!r})")
    t_spawn = wait_for(r["log"], "embed", SPAWN_POLL_S)
    check(t_spawn is not None,
          f"a failed update does not suppress the embed "
          f"(tags={tags(read_log(r['log']))!r})")


def arm_push_mode(root: Path, fake: Path, mode: str, runs: bool) -> None:
    print()
    verb = "still runs" if runs else "starts nothing"
    print(f"--- {MODE_ENV}={mode}: the follow-up {verb} ---")
    r = arm(root, fake, f"a8_mode_{mode}", with_qmd=True,
            ledger=[TASK_REL, NOTE_REL], push_mode=mode)
    check(bind_exists(r["ws"]),
          "positive execution marker: the Stop ran to completion and wrote "
          "the .bind")
    if runs:
        wait_for(r["log"], "embed", SPAWN_POLL_S)
        final = read_log(r["log"])
        check(tags(final) == ["update", "embed"],
              f"{MODE_ENV}={mode} runs the follow-up exactly as an unset value "
              f"does (tags={tags(final)!r})")
        return
    final = settled(r["log"])
    check(final == [],
          f"{MODE_ENV}={mode} started neither update nor embed; "
          f"{latency_note()} (entries={final!r})")
    check(not lock_file(r["ws"]).exists(),
          f"{MODE_ENV}={mode} created no embed lock")


def main() -> int:
    print("=== qmd index follow-up on a note-writing round ===")
    print()
    if not HOOKS.is_dir():
        die(f"hooks dir not found: {HOOKS}")
    if subprocess.run(["node", "--version"], capture_output=True,
                      text=True).returncode != 0:
        die("node is not runnable; the fake qmd binary cannot be driven")

    before = state_snapshot()
    print(f"real _projects/_state BEFORE: {len(before)} entries")

    tmp_root = Path(tempfile.mkdtemp(prefix="tf_qmdindex_"))
    print(f"temp root: {tmp_root}")
    print()
    print("--- sandbox guards ---")
    assert_isolated(tmp_root)

    fake = tmp_root / "fake_qmd.js"
    write(fake, FAKE_QMD_JS)
    ok(f"fake qmd binary planted at {fake} (injected via TASKFLOW_QMD_BIN)")

    try:
        arm_note_round(tmp_root, fake)
        arm_no_note(tmp_root, fake)
        arm_no_qmd_dir(tmp_root, fake)
        arm_lock_alive(tmp_root, fake)
        arm_lock_stale(tmp_root, fake)
        arm_lock_same_round(tmp_root, fake)
        arm_update_timeout(tmp_root, fake)
        arm_push_mode(tmp_root, fake, "off", runs=False)
        arm_push_mode(tmp_root, fake, "lexical", runs=False)
        arm_push_mode(tmp_root, fake, "auto", runs=True)
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
              f"no synthetic session id leaked into the real state dir "
              f"({strays!r})")
        print()
        print(f"temp tree kept for inspection: {tmp_root}")

    print()
    print(f"=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
