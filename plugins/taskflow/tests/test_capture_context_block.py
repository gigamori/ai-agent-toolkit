#!/usr/bin/env python3
from __future__ import annotations

import inspect
import json
import os
import sys
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


def test_single_entry_is_valid_json_and_absolute() -> None:
    print("--- single task/note entry: valid JSON, absolute paths ---")
    sidecar_path = spc.capture_sidecar_path(
        os.path.join(os.getcwd(), "_projects", "_state"), "abc123", 7)
    project_root = os.path.join(os.getcwd(), "_projects", "harness-taskflow")
    ctx = spc.build_capture_context(
        "abc123456789012", "2026-07-30T01:00:00+09:00", sidecar_path, project_root,
        {"harness-taskflow": project_root},
        ["harness-taskflow/2026-07-28_capture-sidecar-abs-path.md"],
        ["project-notes/specs/capture-context-abs-path.md"],
        7,
    )
    try:
        obj = json.loads(ctx)
    except ValueError as e:
        bad(f"context is valid JSON (parse error: {e})")
        return
    ok("context is valid JSON")
    check(os.path.isabs(obj["sidecar_path"]), f"sidecar_path is absolute: {obj['sidecar_path']}")
    check(os.path.isabs(obj["project_root"]), f"project_root is absolute: {obj['project_root']}")
    check("\\" not in obj["sidecar_path"], "sidecar_path has no backslash")
    check("\\" not in obj["project_root"], "project_root has no backslash")
    check(obj["sid"] == "abc123456789012", "sid (12-char tag) round-trips")
    check(obj["round"] == 7, f"round is emitted in the context block: {obj.get('round')}")
    check(obj["sidecar_path"].endswith("/abc123.r7.capture"),
          f"sidecar_path is the per-round name: {obj['sidecar_path']}")
    check(obj["touched_tasks"] == ["harness-taskflow/2026-07-28_capture-sidecar-abs-path.md"],
          "touched_tasks round-trips as a qualified <project>/<basename> key")
    check(obj["note_writes"] == ["project-notes/specs/capture-context-abs-path.md"], "note_writes round-trips")
    check(obj["project_roots"] == {"harness-taskflow": obj["project_root"]},
          f"project_roots maps the project to its absolute root: {obj.get('project_roots')}")
    check(all(os.path.isabs(v) and "\\" not in v for v in obj["project_roots"].values()),
          "every project_roots value is an absolute forward-slashed path")


def test_multi_entry_is_valid_json_d6_regression() -> None:
    print("--- 2+ task/note entries: valid JSON ---")
    sidecar_path = os.path.join(os.getcwd(), "_projects", "_state", "abc123.capture")
    project_root = os.path.join(os.getcwd(), "_projects", "harness-taskflow")
    other_root = os.path.join(os.getcwd(), "_projects", "pi-studio-dev")
    roots = {"harness-taskflow": project_root, "pi-studio-dev": other_root}
    tasks = ["harness-taskflow/a.md", "harness-taskflow/b.md", "pi-studio-dev/c.md"]
    notes = ["project-notes/specs/x.md", "project-notes/checks/y.md"]
    ctx = spc.build_capture_context(
        "abc123456789012", "2026-07-30T01:00:00+09:00", sidecar_path, project_root,
        roots, tasks, notes, 2,
    )
    try:
        obj = json.loads(ctx)
    except ValueError as e:
        bad(f"context with 2+ entries is valid JSON (parse error: {e}); "
            f"raw context: {ctx!r}")
        return
    ok("context with 2+ entries is valid JSON")
    check(obj["touched_tasks"] == tasks, "touched_tasks (3 entries, 2 projects) round-trips exactly")
    check(obj["note_writes"] == notes, "note_writes (2 entries) round-trips exactly")
    check(set(obj["project_roots"]) == {"harness-taskflow", "pi-studio-dev"},
          f"project_roots carries BOTH projects: {obj.get('project_roots')}")


def test_empty_arrays_are_valid_json() -> None:
    print("--- empty touched_tasks/note_writes: valid JSON ---")
    sidecar_path = os.path.join(os.getcwd(), "_projects", "_state", "abc123.capture")
    project_root = os.path.join(os.getcwd(), "_projects", "harness-taskflow")
    ctx = spc.build_capture_context(
        "abc123456789012", "2026-07-30T01:00:00+09:00", sidecar_path, project_root,
        {"harness-taskflow": project_root}, [], [], 1,
    )
    try:
        obj = json.loads(ctx)
    except ValueError as e:
        bad(f"context with empty arrays is valid JSON (parse error: {e})")
        return
    ok("context with empty arrays is valid JSON")
    check(obj["touched_tasks"] == [], "touched_tasks is an empty list")
    check(obj["note_writes"] == [], "note_writes is an empty list")


def test_no_filesystem_io() -> None:
    print("--- pure function: no I/O side effects ---")
    source = inspect.getsource(spc.build_capture_context)
    io_markers = ("open(", "os.remove", "os.rename", ".write(", "Write(")
    hits = [m for m in io_markers if m in source]
    check(not hits, f"build_capture_context source contains no I/O calls (found: {hits})")


def main() -> int:
    print("=== session_progress_capture.py build_capture_context unit tests ===")
    test_single_entry_is_valid_json_and_absolute()
    test_multi_entry_is_valid_json_d6_regression()
    test_empty_arrays_are_valid_json()
    test_no_filesystem_io()

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
