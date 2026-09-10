#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / "plugins" / "taskflow" / "hooks" / "read_capture.py"
REAL_STATE = REPO_ROOT / "_projects" / "_state"
SID = "readcap111-2222-3333-4444-555566667777"

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
        die(f"temp tree is not isolated: ancestor(s) hold _projects/_state: {hits}")
    try:
        root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        ok(f"temp tree {root} is outside the repo and no ancestor holds _projects/_state")
    else:
        die(f"temp tree is inside the repo ({REPO_ROOT})")


def real_state_snapshot() -> tuple[int, str]:
    names = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
    return len(names), hashlib.md5("\n".join(names).encode("utf-8")).hexdigest()


def build_ws(root: Path, with_sid_json: bool) -> Path:
    ws = root / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "_projects" / "_state").mkdir(parents=True)
    if with_sid_json:
        (ws / "_projects" / "_state" / f"{SID}.json").write_text(
            json.dumps({"project": "demo"}), encoding="utf-8")
    return ws


def run_hook(cwd: Path, file_path: str, sid: str = SID) -> subprocess.CompletedProcess:
    payload = {"session_id": sid, "tool_name": "Read", "tool_input": {"file_path": file_path}}
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(HOOK)],
        cwd=str(cwd), input=json.dumps(payload), capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def ledger(ws: Path) -> Path:
    return ws / "_projects" / "_state" / f"{SID}.reads"


def lines_of(p: Path) -> list[str]:
    return [ln for ln in p.read_text(encoding="utf-8").split("\n") if ln]


def test_append_and_exclusions(root: Path) -> None:
    print("--- append + exclusions ---")
    ws = build_ws(root, with_sid_json=True)
    cp = run_hook(ws, str(ws / "src" / "a.py"))
    check(cp.returncode == 0 and cp.stdout == "", f"hook exits 0 silently (rc={cp.returncode}, out={cp.stdout!r}, err={cp.stderr!r})")
    check(ledger(ws).is_file() and lines_of(ledger(ws)) == ["src/a.py"],
          f"an absolute path under the state root is recorded repo-relative (got {lines_of(ledger(ws)) if ledger(ws).is_file() else None})")
    check(not (ws / "_projects" / "_state" / f"{SID}.touched").exists(),
          "the .touched ledger is never created by a Read")

    run_hook(ws, str(ws / "_projects" / "_state" / "other.json"))
    run_hook(ws, str(ws / "tmp" / "scratch.md"))
    run_hook(ws, "C:/elsewhere/outside.py")
    run_hook(ws, "/outside/abs.py")
    run_hook(ws, "src/b.py")
    check(lines_of(ledger(ws)) == ["src/a.py", "src/b.py"],
          f"state-ledger, tmp/ and out-of-tree absolute paths are skipped; a relative path is kept (got {lines_of(ledger(ws))})")


def test_truncation(root: Path) -> None:
    print("--- tail truncation ---")
    ws = build_ws(root, with_sid_json=True)
    ledger(ws).write_text("".join(f"src/old{i}.py\n" for i in range(205)), encoding="utf-8")
    run_hook(ws, str(ws / "src" / "a.py"))
    got = lines_of(ledger(ws))
    check(len(got) == 200 and got[-1] == "src/a.py" and got[0] == "src/old6.py",
          f"ledger keeps only the last 200 lines with the new read last (len={len(got)}, first={got[0]!r}, last={got[-1]!r})")


def test_no_state_json(root: Path) -> None:
    print("--- orphan guard ---")
    ws = build_ws(root, with_sid_json=False)
    cp = run_hook(ws, str(ws / "src" / "a.py"))
    check(cp.returncode == 0 and not ledger(ws).exists(),
          "a session without a state json writes no ledger")


def main() -> int:
    print("=== read_capture.py hook tests ===")
    before = real_state_snapshot()
    for t in (test_append_and_exclusions, test_truncation, test_no_state_json):
        with tempfile.TemporaryDirectory() as d:
            assert_isolated(Path(d))
            t(Path(d))
    after = real_state_snapshot()
    check(before == after, f"the real _projects/_state listing is unchanged (before={before}, after={after})")
    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
