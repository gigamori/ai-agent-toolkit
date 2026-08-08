#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""clean_locks.py — remove dead `@log` advisory-lock sidecars.

`hooks/log_lock.py` keys a task's lock on its BASENAME and stores the sidecar at
`<project_root>/.locks/<task-basename>.lock`, deleting it when the last holder
releases. This script removes the two populations that release-time delete does
NOT cover:

  dead    — `.locks/<name>.lock` whose `<name>` matches no task md anywhere
            under `tasks/`. The task was deleted or renamed, so no writer can
            ever resolve to that key again.
  legacy  — `tasks/<status>/*.md.lock`, the pre-stable-key location. Nothing
            creates these any more, so every one of them is dead regardless of
            whether a sibling `.md` still exists. (The old orphan definition
            — "no sibling .md" — only ever caught the subset left behind on the
            DEPARTURE side of a status move; the arrival-side ones looked live.)

NOT covered by either population, by design: a sidecar left behind because a
holder was killed mid-write. Its task still exists, so `dead` does not match it,
and it is harmless — under protocol v2 the next writer breaks it once it ages
past `TASKFLOW_LOCK_STALE` (default 10 s) and takes ownership. The same applies
to a crash-orphaned `.locks/progress.md.lock`, which `dead` deliberately never
matches (see `PROGRESS_LOCK_NAME`): stale-break, not this sweep, is what
reclaims it. See project-notes/specs/log-lock-stable-key.md §3.3.

Safety
------
Dry-run by DEFAULT. Nothing is deleted without `--apply`.

`_projects/` is typically gitignored, so deletions here are NOT git-recoverable.
Run this only when no other Claude Code session is active: a live session's hook
may legitimately hold a sidecar. On win32 the OS enforces this for us (unlink of
an open file fails, and the file is reported as `held`); on POSIX unlink always
succeeds, and `log_lock`'s post-acquire staleness revalidation is what keeps a
concurrent holder correct.

Usage:
  uv run --script clean_locks.py <project_dir>              # dry-run, both populations
  uv run --script clean_locks.py <project_dir> --apply
  uv run --script clean_locks.py <project_dir> --dead-only  # skip legacy sweep
  uv run --script clean_locks.py <project_dir> --legacy-only

Exit codes:
  0 = clean, or a successful --apply (nothing left that should have been removed)
  1 = dead/legacy locks found in dry-run mode (nothing deleted), or --apply left
      files behind (e.g. held by a live process on win32)
  2 = script error (bad arguments, missing project dir)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

LOCKS_DIRNAME = ".locks"
# Protocol v2 gave `progress.md` its own sidecar in the SAME `.locks/` dir
# (hooks/log_lock.py `lock_path_for` rule 2). It is not keyed on a task md, so
# the `dead` rule below would flag it in every project, forever. Exempt it.
# LOCKSTEP: scripts/check_progress.py::check_orphan_lock carries the same
# exemption as a separate implementation -- change both together.
PROGRESS_LOCK_NAME = "progress.md.lock"


def task_md_basenames(project_dir: Path) -> set[str]:
    """Every task md basename under `tasks/`, whole-tree.

    Walk range MUST mirror hooks/session_progress_capture.py::_task_basename_index
    (os.walk of the WHOLE tasks/ tree, case-insensitive .md). A basename this
    misses would make its live lock look dead and get deleted.
    """
    names: set[str] = set()
    tasks_dir = project_dir / "tasks"
    if not tasks_dir.is_dir():
        return names
    for dirpath, _dirs, files in os.walk(tasks_dir):
        for fn in files:
            if fn.lower().endswith(".md"):
                names.add(fn)
    return names


def find_dead_locks(project_dir: Path) -> list[Path]:
    """`.locks/<name>.lock` entries whose `<name>` is no longer a task md."""
    locks_dir = project_dir / LOCKS_DIRNAME
    if not locks_dir.is_dir():
        return []
    live = task_md_basenames(project_dir)
    dead: list[Path] = []
    for p in sorted(locks_dir.iterdir()):
        if not p.is_file() or p.suffix != ".lock":
            continue
        if p.name == PROGRESS_LOCK_NAME:
            continue  # not task-keyed -- see PROGRESS_LOCK_NAME.
        # `<task-basename>.lock` -> `<task-basename>` (which still ends in .md)
        if p.name[: -len(".lock")] not in live:
            dead.append(p)
    return dead


def find_legacy_locks(project_dir: Path) -> list[Path]:
    """Pre-stable-key `tasks/<status>/*.md.lock` sidecars (all now dead)."""
    tasks_dir = project_dir / "tasks"
    if not tasks_dir.is_dir():
        return []
    found: list[Path] = []
    for dirpath, _dirs, files in os.walk(tasks_dir):
        for fn in sorted(files):
            if fn.endswith(".md.lock"):
                found.append(Path(dirpath) / fn)
    return sorted(found)


def remove(paths: list[Path], project_dir: Path, apply: bool) -> tuple[int, list[Path]]:
    """Return (removed_count, still_present). Dry-run removes nothing."""
    removed = 0
    held: list[Path] = []
    for p in paths:
        rel = p.relative_to(project_dir).as_posix()
        if not apply:
            print(f"  would remove: {rel}")
            continue
        try:
            p.unlink()
            removed += 1
            print(f"  removed: {rel}")
        except OSError as e:
            held.append(p)
            print(f"  HELD (skipped): {rel} — {e}")
    return removed, held


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove dead @log advisory-lock sidecars (dry-run unless --apply)."
    )
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default: dry-run, delete nothing)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dead-only", action="store_true",
                       help="only sweep .locks/ entries with no matching task md")
    group.add_argument("--legacy-only", action="store_true",
                       help="only sweep pre-stable-key tasks/<status>/*.md.lock")
    args = parser.parse_args(argv)

    project_dir: Path = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return 2

    do_dead = not args.legacy_only
    do_legacy = not args.dead_only

    dead = find_dead_locks(project_dir) if do_dead else []
    legacy = find_legacy_locks(project_dir) if do_legacy else []

    if not dead and not legacy:
        print(f"clean: no dead or legacy lock files under {project_dir}")
        return 0

    if not args.apply:
        print("DRY RUN — nothing will be deleted. Re-run with --apply to remove.")
        print("Run only when no other Claude Code session is active; "
              "_projects/ is usually gitignored and deletions are not recoverable.")
        print()

    held_all: list[Path] = []
    if dead:
        print(f"dead locks in {LOCKS_DIRNAME}/ (task md no longer exists): {len(dead)}")
        _, held = remove(dead, project_dir, args.apply)
        held_all += held
        print()
    if legacy:
        print(f"legacy locks under tasks/ (pre-stable-key location): {len(legacy)}")
        _, held = remove(legacy, project_dir, args.apply)
        held_all += held
        print()

    if not args.apply:
        return 1
    if held_all:
        print(f"{len(held_all)} file(s) still held by a live process; "
              f"stop other sessions and re-run.")
        return 1
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
