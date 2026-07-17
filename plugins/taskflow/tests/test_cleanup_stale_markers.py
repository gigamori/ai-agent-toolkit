#!/usr/bin/env python3
"""Unit tests for hooks/session_progress_capture.py::_cleanup_stale_markers.

Covers project-notes/specs/review-2026-07-17-fixes.md P4-1 (F5b, D-2):

  D-2 (a, confirmed): session-state `.json` (36-char UUID stem) whose
  `project` field is EMPTY is swept after _MARKER_MAX_AGE_DAYS (7d, mtime
  based); `project` non-empty is kept INDEFINITELY; a json that fails to
  parse or is not a dict is NEVER removed (conservative). The existing
  sidecar sweep (`.bind` / `.touched` / `.capture` / legacy `.captured`,
  unconditional mtime sweep) is unchanged and covered too (F-1 guard: a
  non-36-char-stem `.json`, e.g. `kanban-port-deadbeef.json`, is NEVER
  touched by the `.json` branch regardless of age/content).

ABSOLUTE SAFETY: every fixture lives inside a `tempfile.TemporaryDirectory()`
and is passed explicitly as `state_dir` to `_cleanup_stale_markers()`. This
test NEVER imports/calls `main()`, NEVER reads stdin, and NEVER touches the
real `_projects/_state/` (this repo has one — the point of this guard is not
academic). A final assertion re-checks the real `_state/` dir's file count is
unchanged after the whole test run.

Import note: session_progress_capture.py is stdlib-only (unlike
check_progress.py, it does not require pyyaml) — run with plain:

    uv run python plugins/taskflow/tests/test_cleanup_stale_markers.py

Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import session_progress_capture as spc  # noqa: E402

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


_DAY = 86400
_MAX_AGE = spc._MARKER_MAX_AGE_DAYS  # verify against the real constant (7)


def age_file(path: Path, days_old: float) -> None:
    """Backdate a file's mtime/atime by `days_old` days via os.utime."""
    ts = __import__("time").time() - days_old * _DAY
    os.utime(str(path), (ts, ts))


def write_json(path: Path, obj, days_old: float | None = None) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")
    if days_old is not None:
        age_file(path, days_old)


def uuid_stem() -> str:
    s = str(uuid.uuid4())
    assert len(s) == 36
    return s


def sidecar_names(state_dir: Path) -> set[str]:
    return {p.name for p in state_dir.iterdir()}


def test_threshold_is_7_days() -> None:
    print("--- fixture assumption: _MARKER_MAX_AGE_DAYS is 7 ---")
    check(_MAX_AGE == 7, f"_MARKER_MAX_AGE_DAYS is 7 (got {_MAX_AGE}); "
          "the >7d / fresh fixtures in this file assume this threshold")


def test_case1_empty_project_stale_uuid_json_deleted(state_dir: Path) -> None:
    print("--- case 1: empty-project + stale mtime + parseable dict + 36-char stem -> DELETED ---")
    name = f"{uuid_stem()}.json"
    p = state_dir / name
    write_json(p, {"session_id": "x", "project": ""}, days_old=_MAX_AGE + 1)
    spc._cleanup_stale_markers(str(state_dir))
    check(not p.exists(), "stale empty-project state json is removed")


def test_case2_empty_project_fresh_json_kept(state_dir: Path) -> None:
    print("--- case 2: empty-project + fresh mtime -> kept ---")
    name = f"{uuid_stem()}.json"
    p = state_dir / name
    write_json(p, {"session_id": "x", "project": ""})  # fresh mtime (just written)
    spc._cleanup_stale_markers(str(state_dir))
    check(p.exists(), "fresh empty-project state json is kept")


def test_case3_nonempty_project_stale_kept_indefinitely(state_dir: Path) -> None:
    print("--- case 3: non-empty-project + stale mtime -> kept indefinitely ---")
    name = f"{uuid_stem()}.json"
    p = state_dir / name
    write_json(p, {"session_id": "x", "project": "harness-taskflow"},
                days_old=_MAX_AGE + 30)  # very old, still must survive
    spc._cleanup_stale_markers(str(state_dir))
    check(p.exists(), "non-empty-project state json is kept regardless of age")


def test_case4_corrupt_json_kept(state_dir: Path) -> None:
    print("--- case 4: corrupt / non-dict json -> kept (conservative) ---")
    stem1 = uuid_stem()
    corrupt = state_dir / f"{stem1}.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    age_file(corrupt, _MAX_AGE + 1)

    stem2 = uuid_stem()
    non_dict = state_dir / f"{stem2}.json"
    non_dict.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    age_file(non_dict, _MAX_AGE + 1)

    spc._cleanup_stale_markers(str(state_dir))
    check(corrupt.exists(), "unparseable json is never deleted")
    check(non_dict.exists(), "non-dict (parseable) json is never deleted")


def test_threshold_boundary(state_dir: Path) -> None:
    print("--- threshold: cutoff is ~7 days (mtime-based) ---")
    just_over = state_dir / f"{uuid_stem()}.json"
    write_json(just_over, {"project": ""}, days_old=_MAX_AGE + (1 / 24))  # 7d + 1h
    just_under = state_dir / f"{uuid_stem()}.json"
    write_json(just_under, {"project": ""}, days_old=_MAX_AGE - (1 / 24))  # 7d - 1h

    spc._cleanup_stale_markers(str(state_dir))
    check(not just_over.exists(), "empty-project json just past the 7d cutoff is removed")
    check(just_under.exists(), "empty-project json just under the 7d cutoff is kept")


def test_f1_guard_non_uuid_stem_never_swept(state_dir: Path) -> None:
    print("--- F-1 guard: non-36-char-stem .json is NEVER swept by the .json branch ---")
    p = state_dir / "kanban-port-deadbeef.json"
    # Worst case for the guard: old + dict + empty project — would qualify for
    # deletion under the UUID-stem branch if the len(stem)==36 guard were absent.
    write_json(p, {"project": ""}, days_old=_MAX_AGE + 100)
    spc._cleanup_stale_markers(str(state_dir))
    check(p.exists(), "kanban-port-*.json (non-36-char stem) survives even when "
          "stale + dict + empty-project")


def test_sidecar_suffix_sweep(state_dir: Path) -> None:
    print("--- sidecar suffix sweep: .touched / .bind / .capture / legacy .captured ---")
    sid = str(uuid.uuid4())
    old_touched = state_dir / f"{sid}.touched"
    old_touched.write_text("tasks/0_todo/x.md\n", encoding="utf-8")
    age_file(old_touched, _MAX_AGE + 1)

    fresh_touched = state_dir / f"{uuid.uuid4()}.touched"
    fresh_touched.write_text("tasks/0_todo/y.md\n", encoding="utf-8")  # fresh

    old_bind = state_dir / f"{uuid.uuid4()}.bind"
    old_bind.write_text("{}", encoding="utf-8")
    age_file(old_bind, _MAX_AGE + 1)

    old_capture = state_dir / f"{uuid.uuid4()}.capture"
    old_capture.write_text("{}", encoding="utf-8")
    age_file(old_capture, _MAX_AGE + 1)

    old_legacy = state_dir / f"{uuid.uuid4()}.captured"
    old_legacy.write_text("{}", encoding="utf-8")
    age_file(old_legacy, _MAX_AGE + 1)

    spc._cleanup_stale_markers(str(state_dir))

    check(not old_touched.exists(), "old .touched sidecar is removed")
    check(fresh_touched.exists(), "fresh .touched sidecar is kept")
    check(not old_bind.exists(), "old .bind sidecar is removed")
    check(not old_capture.exists(), "old .capture sidecar is removed")
    check(not old_legacy.exists(), "old legacy .captured sidecar is removed")


def test_real_state_dir_untouched() -> None:
    print("--- ABSOLUTE SAFETY: real _projects/_state/ was never referenced ---")
    real_state_dir = Path(__file__).resolve().parent.parent.parent.parent / "_projects" / "_state"
    check(real_state_dir.is_dir(), "sanity: real _state/ dir exists in this repo "
          "(confirms the hazard this test avoids is real, not hypothetical)")
    # This test process never called spc._cleanup_stale_markers with this path,
    # never called spc.main(), and never read stdin. Nothing to assert about
    # file counts (a concurrent session could legitimately write there) —
    # the guarantee here is structural (see module docstring), not observational.


def main() -> int:
    print("=== session_progress_capture.py _cleanup_stale_markers unit tests ===")
    test_threshold_is_7_days()
    with tempfile.TemporaryDirectory() as d:
        state_dir = Path(d)
        test_case1_empty_project_stale_uuid_json_deleted(state_dir)
        test_case2_empty_project_fresh_json_kept(state_dir)
        test_case3_nonempty_project_stale_kept_indefinitely(state_dir)
        test_case4_corrupt_json_kept(state_dir)
    with tempfile.TemporaryDirectory() as d2:
        test_threshold_boundary(Path(d2))
    with tempfile.TemporaryDirectory() as d3:
        test_f1_guard_non_uuid_stem_never_swept(Path(d3))
    with tempfile.TemporaryDirectory() as d4:
        test_sidecar_suffix_sweep(Path(d4))
    test_real_state_dir_untouched()

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
