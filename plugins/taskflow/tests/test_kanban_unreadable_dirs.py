#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
from __future__ import annotations

import io
import os
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import generate_kanban as gk  # noqa: E402

REPO_STATE_DIR = Path(__file__).resolve().parents[3] / "_projects" / "_state"

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


class _Deny:

    def __init__(self, *denied: Path):
        self.denied = {os.path.normcase(str(p)) for p in denied}

    def __enter__(self):
        self._orig = Path.iterdir
        orig, denied = self._orig, self.denied

        def patched(self_path):
            if os.path.normcase(str(self_path)) in denied:
                raise PermissionError(13, "Permission denied", str(self_path))
            return orig(self_path)

        Path.iterdir = patched  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        Path.iterdir = self._orig  # type: ignore[assignment]
        return False


class _Env:

    def __init__(self, cfg: str | None, home: Path):
        self.cfg = cfg
        self.home = home

    def __enter__(self):
        self._orig_cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        self._orig_home = Path.home
        if self.cfg is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.cfg
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


def _capture(fn):
    buf = io.StringIO()
    with redirect_stderr(buf):
        result = fn()
    return result, buf.getvalue()



UUID_STATE = "44444444-4444-4444-4444-444444444444"
UUID_ENV = "22222222-2222-2222-2222-222222222222"
UUID_HOME = "33333333-3333-3333-3333-333333333333"


def _seed_state(root: Path) -> Path:
    d = root / "_state"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{UUID_STATE}.json").write_text(
        '{"origin": "cc", "project": "demo-a"}', encoding="utf-8"
    )
    return d


def _seed_task(project_dir: Path, status: str, name: str, title: str) -> Path:
    d = project_dir / "tasks" / status
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\npriority: MID\n---\n\n# {title}\n", encoding="utf-8"
    )
    return d


def _seed_cc(root: Path, proj: str, uuid: str) -> Path:
    d = root / "projects"
    (d / proj).mkdir(parents=True, exist_ok=True)
    (d / proj / f"{uuid}.jsonl").write_text("{}\n", encoding="utf-8")
    return d


def _seed_index(root: Path, names: list[str]) -> None:
    lines = ["| Project | Description |", "| --- | --- |"]
    lines += [f"| {n} | {n} demo |" for n in names]
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")



def test_iter_dir_helper() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "d"
        d.mkdir()
        (d / "a.txt").write_text("x", encoding="utf-8")
        control, _ = _capture(lambda: gk.iter_dir(d))
        check([p.name for p in control] == ["a.txt"],
              f"iter_dir control: readable dir lists its entries, got {control}")
        with _Deny(d):
            denied, err = _capture(lambda: gk.iter_dir(d))
        check(denied == [], f"iter_dir: unreadable dir yields [] instead of raising, got {denied}")
        check("warn: cannot read directory" in err and str(d) in err,
              f"iter_dir: the refusal is reported on stderr with its path, got {err!r}")



def test_state_dir_unreadable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "_projects"
        state = _seed_state(root)
        result, _ = _capture(lambda: gk.build_uuid_index(state))
        prefix_index, _tail_index = result
        check(sorted(prefix_index.keys()) == [UUID_STATE[:8]],
              f"_state control: the seeded sidecar is indexed, got {sorted(prefix_index.keys())}")
        with _Deny(state):
            result, err = _capture(lambda: gk.build_uuid_index(state))
            index, _tail_index = result
        check(index == {}, f"_state unreadable -> empty index, no raise, got {index}")
        check(str(state) in err, f"_state unreadable -> warned with its path, got {err!r}")



def test_task_status_dir_unreadable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp) / "_projects" / "demo-a"
        todo = _seed_task(proj, "0_todo", "2026-08-12_todo-one", "Todo one")
        _seed_task(proj, "1_in_progress", "2026-08-12_wip-one", "Wip one")
        empty_prefix_index: dict[str, list[gk.StateEntry]] = {}
        empty_tail_index: dict[str, list[gk.StateEntry]] = {}
        control, _ = _capture(lambda: gk.load_tasks(proj, "demo-a", empty_prefix_index, empty_tail_index))
        check(sorted(t.h1 for t in control) == ["Todo one", "Wip one"],
              f"tasks control: both status dirs load, got {[t.h1 for t in control]}")
        with _Deny(todo):
            tasks, err = _capture(lambda: gk.load_tasks(proj, "demo-a", empty_prefix_index, empty_tail_index))
        check([t.h1 for t in tasks] == ["Wip one"],
              f"unreadable 0_todo/ is skipped; the other status dir still loads, got "
              f"{[t.h1 for t in tasks]}")
        check(str(todo) in err, f"unreadable status dir -> warned with its path, got {err!r}")



def test_cc_projects_dir_unreadable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        cfg = Path(tmp) / "cfg"
        _seed_cc(home / ".claude", "-w-home", UUID_HOME)
        env_projects = _seed_cc(cfg, "-w-env", UUID_ENV)
        with _Env(str(cfg), home):
            control, _ = _capture(gk.build_cc_session_index)
            check(sorted(control) == sorted([UUID_ENV, UUID_HOME]),
                  f"cc control: both universes resolve, got {sorted(control)}")
            with _Deny(env_projects):
                index, err = _capture(gk.build_cc_session_index)
        check(sorted(index) == [UUID_HOME],
              f"unreadable env config dir is skipped; ~/.claude still scanned, got {sorted(index)}")
        check(str(env_projects) in err,
              f"unreadable config projects dir -> warned with its path, got {err!r}")



def test_board_survives_one_unreadable_project() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        (home / ".claude").mkdir(parents=True)
        root = Path(tmp) / "_projects"
        _seed_index(root, ["demo-a", "demo-b"])
        todo_a = _seed_task(root / "demo-a", "0_todo", "2026-08-12_a-one", "A one")
        _seed_task(root / "demo-b", "0_todo", "2026-08-12_b-one", "B one")
        with _Env(None, home):
            control, _ = _capture(lambda: gk.load_projects([root], {}, {}))
            counts = {p.name: len(p.tasks) for p in control[0]}
            check(counts == {"demo-a": 1, "demo-b": 1},
                  f"board control: both projects load one task each, got {counts}")
            with _Deny(todo_a):
                (projects, _np, _npt), err = _capture(lambda: gk.load_projects([root], {}, {}))
        counts = {p.name: len(p.tasks) for p in projects}
        check(counts == {"demo-a": 0, "demo-b": 1},
              f"one unreadable task dir costs only that dir; the board still builds, got {counts}")
        check(str(todo_a) in err, f"the skipped directory is named on stderr, got {err!r}")


def main() -> int:
    state_before = sorted(p.name for p in REPO_STATE_DIR.glob("*")) if REPO_STATE_DIR.is_dir() else []

    test_iter_dir_helper()
    test_state_dir_unreadable()
    test_task_status_dir_unreadable()
    test_cc_projects_dir_unreadable()
    test_board_survives_one_unreadable_project()

    state_after = sorted(p.name for p in REPO_STATE_DIR.glob("*")) if REPO_STATE_DIR.is_dir() else []
    check(state_before == state_after,
          "real _projects/_state/ is unchanged by this test run")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} check(s) failed ({PASS} passed).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
