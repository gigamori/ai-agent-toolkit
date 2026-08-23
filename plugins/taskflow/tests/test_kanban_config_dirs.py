#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import generate_kanban as gk  # noqa: E402

REPO_STATE_DIR = Path(__file__).resolve().parents[3] / "_projects" / "_state"
REAL_CLAUDE_DIR = Path.home() / ".claude"

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


class _Env:

    def __init__(self, cfg: str | None, home: Path | None = None):
        self.cfg = cfg
        self.home = home

    def __enter__(self):
        self._orig_cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        self._orig_home = Path.home
        if self.cfg is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.cfg
        if self.home is not None:
            fake = self.home
            Path.home = classmethod(lambda cls: fake)  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        if self._orig_cfg is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._orig_cfg
        Path.home = self._orig_home  # type: ignore[assignment]
        return False


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))



def test_t1_env_unset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        (home / ".claude").mkdir(parents=True)
        with _Env(None, home):
            dirs = gk._cc_config_dirs()
        check(len(dirs) == 1 and _same(dirs[0], home / ".claude"),
              f"T1: env unset -> [~/.claude] only, got {dirs}")


def test_t2_env_absolute() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        (home / ".claude").mkdir(parents=True)
        cfg = Path(tmp) / "cfg"
        cfg.mkdir()
        with _Env(str(cfg), home):
            dirs = gk._cc_config_dirs()
        check(len(dirs) == 2 and _same(dirs[0], cfg) and _same(dirs[1], home / ".claude"),
              f"T2: absolute env value -> [env, ~/.claude] in that order, got {dirs}")


def test_t3_env_equals_default() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        default = home / ".claude"
        default.mkdir(parents=True)
        with _Env(str(default).upper(), home):
            dirs = gk._cc_config_dirs()
        expected = 1 if os.path.normcase("A") == os.path.normcase("a") else 2
        check(len(dirs) == expected,
              f"T3: env pointing at ~/.claude dedupes to {expected} dir(s), got {dirs}")


def test_t4_env_tilde_is_literal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        (home / ".claude").mkdir(parents=True)
        cwd = os.getcwd()
        with _Env("~/x", home):
            dirs = gk._cc_config_dirs()
        expected = Path(os.path.abspath(os.path.join(cwd, "~/x")))
        check(_same(dirs[0], expected),
              f"T4: '~/x' is cwd-relative literal (no expanduser) -> {expected}, got {dirs[0]}")
        check(not _same(dirs[0], home / "x"),
              "T4: '~/x' does NOT resolve under the home directory")



UUID_SHARED = "11111111-1111-1111-1111-111111111111"
UUID_ENV_ONLY = "22222222-2222-2222-2222-222222222222"
UUID_HOME_ONLY = "33333333-3333-3333-3333-333333333333"


def _seed(root: Path, proj: str, uuids: list[str]) -> None:
    d = root / "projects" / proj
    d.mkdir(parents=True, exist_ok=True)
    for u in uuids:
        (d / f"{u}.jsonl").write_text("{}\n", encoding="utf-8")


def test_t5_union_scan() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        cfg = Path(tmp) / "cfg"
        _seed(home / ".claude", "-w-home", [UUID_HOME_ONLY])
        _seed(cfg, "-w-env", [UUID_ENV_ONLY])
        with _Env(str(cfg), home):
            index = gk.build_cc_session_index()
        check(UUID_ENV_ONLY in index and UUID_HOME_ONLY in index,
              f"T5: index resolves sessions from both universes, got {sorted(index)}")
        check(_same(index[UUID_ENV_ONLY], cfg / "projects" / "-w-env" / f"{UUID_ENV_ONLY}.jsonl"),
              "T5: env-universe session maps to the env path")
        check(_same(index[UUID_HOME_ONLY],
                    home / ".claude" / "projects" / "-w-home" / f"{UUID_HOME_ONLY}.jsonl"),
              "T5: home-universe session maps to the ~/.claude path")


def test_t5b_env_unset_scans_home_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        cfg = Path(tmp) / "cfg"
        _seed(home / ".claude", "-w-home", [UUID_HOME_ONLY])
        _seed(cfg, "-w-env", [UUID_ENV_ONLY])
        with _Env(None, home):
            index = gk.build_cc_session_index()
        check(sorted(index) == [UUID_HOME_ONLY],
              f"env unset -> only ~/.claude/projects is scanned, got {sorted(index)}")


def test_t6_collision_env_wins() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        cfg = Path(tmp) / "cfg"
        _seed(home / ".claude", "-w-home", [UUID_SHARED])
        _seed(cfg, "-w-env", [UUID_SHARED])
        with _Env(str(cfg), home):
            index = gk.build_cc_session_index()
        check(_same(index[UUID_SHARED], cfg / "projects" / "-w-env" / f"{UUID_SHARED}.jsonl"),
              f"T6: same UUID in both universes -> env path wins, got {index[UUID_SHARED]}")


def test_missing_dirs_are_skipped() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        with _Env(str(Path(tmp) / "nonexistent"), home):
            index = gk.build_cc_session_index()
        check(index == {}, f"nonexistent config dirs are skipped without raising, got {index}")


def main() -> int:
    state_before = sorted(p.name for p in REPO_STATE_DIR.glob("*")) if REPO_STATE_DIR.is_dir() else []
    claude_before = sorted(p.name for p in REAL_CLAUDE_DIR.glob("*")) if REAL_CLAUDE_DIR.is_dir() else []

    test_t1_env_unset()
    test_t2_env_absolute()
    test_t3_env_equals_default()
    test_t4_env_tilde_is_literal()
    test_t5_union_scan()
    test_t5b_env_unset_scans_home_only()
    test_t6_collision_env_wins()
    test_missing_dirs_are_skipped()

    state_after = sorted(p.name for p in REPO_STATE_DIR.glob("*")) if REPO_STATE_DIR.is_dir() else []
    claude_after = sorted(p.name for p in REAL_CLAUDE_DIR.glob("*")) if REAL_CLAUDE_DIR.is_dir() else []
    check(state_before == state_after,
          "real _projects/_state/ is unchanged by this test run")
    check(claude_before == claude_after,
          "real ~/.claude/ top-level entries are unchanged by this test run")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} check(s) failed ({PASS} passed).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
