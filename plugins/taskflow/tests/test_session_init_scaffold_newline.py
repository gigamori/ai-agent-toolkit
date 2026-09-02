#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOKS_SRC = REPO_ROOT / "plugins" / "taskflow" / "hooks"
REAL_STATE = REPO_ROOT / "_projects" / "_state"

SID = "a1b2c3d4-e5f6-4789-9abc-def012345678"
PROJECT = "demo"

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
    ok(f"no ancestor of {root} holds _projects/_state")
    try:
        root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        ok(f"temp tree is outside the repo ({REPO_ROOT})")
    else:
        die(f"temp tree is inside the repo ({REPO_ROOT})")


def real_state_snapshot() -> tuple[int, str]:
    names = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
    digest = hashlib.md5("\n".join(names).encode("utf-8")).hexdigest()
    return len(names), digest


def copy_hooks(dest: Path) -> Path:
    tf = dest / "hooks"
    shutil.copytree(HOOKS_SRC, tf, ignore=shutil.ignore_patterns("__pycache__"))
    return tf


def run_session_init(hooks: Path, cwd: Path, payload: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(hooks / "session_init.py")],
        cwd=str(cwd), input=json.dumps(payload), capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=env)


def main() -> int:
    print("=== session_init.py scaffold newline unit test ===")
    before = real_state_snapshot()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert_isolated(root)
        hooks = copy_hooks(root)

        r = run_session_init(hooks, root, {
            "session_id": SID,
            "prompt": f"pj:{PROJECT} start",
            "transcript_path": "",
        })

        index_md = root / "_projects" / "index.md"
        check(index_md.exists(),
              f"scaffold wrote _projects/index.md (rc={r.returncode})")
        if index_md.exists():
            data = index_md.read_bytes()
            cr_count = data.count(b"\r")
            check(cr_count == 0,
                  f"scaffolded index.md has zero \\r bytes (got {cr_count})")
            check(len(data) > 0,
                  "scaffolded index.md is non-empty (positive marker)")

    after = real_state_snapshot()
    check(after == before,
          f"real _projects/_state/ is unchanged "
          f"(before={before!r}, after={after!r})")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
