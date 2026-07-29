#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""Tests for session_sync.py's CLAUDE_CONFIG_DIR resolution.

Covers Change 2 / AC-1 / AC-3 / AC-4 of
_projects/harness-taskflow/project-notes/specs/claude-config-dir-support.md:
the Stop hook resolves plans/memory from a SINGLE dir — `$CLAUDE_CONFIG_DIR`
when set (literal value, no `expanduser`), otherwise `~/.claude` — because it
runs inside the writer's process tree and inherits the writer's env.

The hook reads the env at module load, so each case runs the real hook in a
subprocess (`uv run --no-project python <hook>`), with:
  - cwd = a temp workspace holding its own `_projects/` fixture, per the
    `e2e_state_dir_sandbox` rule in plugins/taskflow/CLAUDE.md;
  - HOME / USERPROFILE pointed at a temp fake home carrying a decoy
    `.claude/plans/` universe, so the real `~/.claude` is never even reachable.

stdlib only. Run with:
  uv run --script plugins/taskflow/tests/test_session_sync_config_dir.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "session_sync.py"
REPO_STATE_DIR = Path(__file__).resolve().parents[3] / "_projects" / "_state"
REAL_CLAUDE_DIR = Path.home() / ".claude"

SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PROJECT = "sandbox-project"

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


def _encode_cwd(path: str) -> str:
    """Replicate the hook's CWD encoding."""
    return path.replace("\\", "/").lower().replace(":", "-").replace("/", "-")


def _build_workspace(tmp: Path) -> tuple[Path, Path, Path]:
    """Create (workspace, fake_home, env_cfg) fixtures.

    The fake home carries a decoy `~/.claude` universe; the env cfg carries the
    universe the hook must prefer when CLAUDE_CONFIG_DIR is set.
    """
    ws = Path(os.path.realpath(tmp / "ws"))
    (ws / "_projects" / "_state").mkdir(parents=True)
    (ws / "_projects" / PROJECT).mkdir(parents=True)
    (ws / "_projects" / "_state" / f"{SESSION_ID}.json").write_text(
        json.dumps({"project": PROJECT}), encoding="utf-8")

    fake_home = Path(os.path.realpath(tmp / "home"))
    env_cfg = Path(os.path.realpath(tmp / "cfg"))
    encoded = _encode_cwd(str(ws))
    for root, tag in ((fake_home / ".claude", "home"), (env_cfg, "env")):
        (root / "plans").mkdir(parents=True)
        (root / "plans" / f"plan_{tag}.md").write_text(f"{tag} plan\n", encoding="utf-8")
        (root / "projects" / encoded / "memory").mkdir(parents=True)
        (root / "projects" / encoded / "memory" / f"mem_{tag}.md").write_text(
            f"{tag} memory\n", encoding="utf-8")
    return ws, fake_home, env_cfg


def _run_hook(ws: Path, fake_home: Path, cfg: str | None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    if cfg is not None:
        env["CLAUDE_CONFIG_DIR"] = cfg
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(HOOK)],
        input=json.dumps({"session_id": SESSION_ID}).encode("utf-8"),
        cwd=str(ws), env=env, capture_output=True, timeout=120,
    )


def _synced(ws: Path, subdir: str) -> list[str]:
    d = ws / "_projects" / PROJECT / subdir
    return sorted(p.name for p in d.glob("*.md")) if d.is_dir() else []


def test_env_set_uses_env_universe_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws, fake_home, env_cfg = _build_workspace(Path(tmp))
        proc = _run_hook(ws, fake_home, str(env_cfg))
        check(proc.returncode == 0, f"env set: hook exits 0 (stderr: {proc.stderr[:300]!r})")
        check(_synced(ws, "plans") == ["plan_env.md"],
              f"AC-3: env set -> only env-universe plans are synced, got {_synced(ws, 'plans')}")
        check(_synced(ws, "memory") == ["mem_env.md"],
              f"AC-3: env set -> only env-universe memory is synced, got {_synced(ws, 'memory')}")


def test_env_unset_uses_home() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws, fake_home, env_cfg = _build_workspace(Path(tmp))
        proc = _run_hook(ws, fake_home, None)
        check(proc.returncode == 0, f"env unset: hook exits 0 (stderr: {proc.stderr[:300]!r})")
        check(_synced(ws, "plans") == ["plan_home.md"],
              f"AC-1: env unset -> ~/.claude plans are synced, got {_synced(ws, 'plans')}")
        check(_synced(ws, "memory") == ["mem_home.md"],
              f"AC-1: env unset -> ~/.claude memory is synced, got {_synced(ws, 'memory')}")


def test_env_relative_value_is_literal() -> None:
    """AC-4: a relative value resolves against the hook's cwd, not the home dir."""
    with tempfile.TemporaryDirectory() as tmp:
        ws, fake_home, _ = _build_workspace(Path(tmp))
        rel_cfg = ws / "~" / "cfgtest"
        (rel_cfg / "plans").mkdir(parents=True)
        (rel_cfg / "plans" / "plan_literal.md").write_text("literal\n", encoding="utf-8")
        proc = _run_hook(ws, fake_home, "~/cfgtest")
        check(proc.returncode == 0, f"literal env: hook exits 0 (stderr: {proc.stderr[:300]!r})")
        check(_synced(ws, "plans") == ["plan_literal.md"],
              f"AC-4: '~/cfgtest' resolves to <cwd>/~/cfgtest, got {_synced(ws, 'plans')}")
        check(not (fake_home / "cfgtest").exists(),
              "AC-4: '~/cfgtest' is not expanded to the home directory")


def main() -> int:
    state_before = sorted(p.name for p in REPO_STATE_DIR.glob("*")) if REPO_STATE_DIR.is_dir() else []
    claude_before = sorted(p.name for p in REAL_CLAUDE_DIR.glob("*")) if REAL_CLAUDE_DIR.is_dir() else []

    test_env_set_uses_env_universe_only()
    test_env_unset_uses_home()
    test_env_relative_value_is_literal()

    state_after = sorted(p.name for p in REPO_STATE_DIR.glob("*")) if REPO_STATE_DIR.is_dir() else []
    claude_after = sorted(p.name for p in REAL_CLAUDE_DIR.glob("*")) if REAL_CLAUDE_DIR.is_dir() else []
    check(state_before == state_after,
          "AC-6: real _projects/_state/ is unchanged by this test run")
    check(claude_before == claude_after,
          "AC-6: real ~/.claude/ top-level entries are unchanged by this test run")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} check(s) failed ({PASS} passed).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
