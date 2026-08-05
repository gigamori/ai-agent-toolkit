#!/usr/bin/env python3
"""Per-task advisory lock for serializing `@log` writes (stdlib only).

Shared helper for the taskflow Stop-hook backstop and any script that appends
to a task md's `<!-- @log:begin/end -->` block. Serializes concurrent
read-modify-write of the same task file so two writers do not clobber each
other's append.

Mechanism (advisory, OS-native, stdlib only):
  - Windows (win32 primary): `msvcrt.locking` on a sidecar lock file.
  - POSIX:                    `fcntl.flock` on a sidecar lock file.

The lock is taken on a separate file (NOT the task md itself), so the locked
region never overlaps the bytes being rewritten and the task md can be fully
truncated/rewritten while the lock is held.

Lock file location — `<project_root>/.locks/<task-basename>.lock`, keyed on the
task's BASENAME, not on its current `tasks/<status>/` path (`lock_path_for`).
A status-folder move (`/progress start|approve|unstart`) must not change which
file protects a task: the resolution layer already identifies tasks by basename
(`session_progress_capture._task_basename_index`), so a path-derived key let two
writers acquire two different lock files for the same task whenever a move
interleaved. The sidecar is deleted when its last holder releases it, so
`.locks/` carries no residue for live tasks. Full rationale, the two-live-locks
race, and the platform-specific unlink ordering:
project-notes/specs/log-lock-stable-key.md.

Usage (context manager):

    from log_lock import log_lock
    with log_lock(task_md_path):
        # read-modify-write the task md's @log block here
        ...

Known limitation (R-lock, spec §10 G2): this is an *advisory* lock between
processes that cooperate by calling this helper. The LLM Edit-tool append
happens at the tool layer and cannot acquire this lock; that residual
concurrent-append race is OUT OF SCOPE here and is only logged, not solved.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time

# Bounded-acquire timeout (seconds) for the POSIX flock path — INV-2
# (no-deadlock): removes the unbounded LOCK_EX block. win32 msvcrt.locking is
# already bounded (~10s, raises). Tunable per exec-binding.md §9 (rec 2-3s);
# overridable via env so distributors can adjust without editing code.
try:
    LOCK_TIMEOUT_S = float(os.environ.get('TASKFLOW_LOCK_TIMEOUT', '3.0'))
except ValueError:
    LOCK_TIMEOUT_S = 3.0

# win32 primary: prefer msvcrt; fall back to fcntl on POSIX.
try:
    import msvcrt  # type: ignore

    _HAVE_MSVCRT = True
except ImportError:  # POSIX
    _HAVE_MSVCRT = False
    try:
        import fcntl  # type: ignore

        _HAVE_FCNTL = True
    except ImportError:
        _HAVE_FCNTL = False


def lock_path_for(task_path: str) -> str:
    """Return the sidecar lock path for a task md path.

    Stable-keyed on the task's basename under its project's `tasks/`
    directory: `<project_root>/.locks/<basename>.lock`. `task_path` is
    normalized (realpath + normcase) before locating the nearest `tasks/`
    ancestor, so callers passing the same task under different spellings
    (relative/absolute, drive-letter vs Git-Bash form, case, symlink)
    resolve to the identical lock file — the rendezvous point a mutual-
    exclusion lock depends on must not vary with caller-supplied path
    spelling (project-notes/specs/log-lock-stable-key.md §3.1, H1).

    Deriving the key from the STATUS FOLDER (as the legacy `<task>.lock`
    did) is the root cause this replaces: a task's resolved identity is its
    basename (hooks/session_progress_capture.py `_task_basename_index`),
    not its current status folder, so a status-folder move mid-session
    must not change which lock file protects it — see spec §2.1 for the
    two-live-locks race this produces if the key is path-derived instead.

    Falls back to `<task_path>.lock` (legacy behavior, logged to stderr) if
    no `tasks/` ancestor is found — keeps this function total for callers
    outside the `tasks/<status>/` layout (e.g. the `__main__` self-check
    below intentionally exercises the primary path instead, per spec §3.1).
    """
    norm = os.path.normcase(os.path.realpath(task_path))
    parts = norm.split(os.sep)
    basename = os.path.basename(norm)
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == 'tasks':
            project_root = os.sep.join(parts[:i])
            return os.path.join(project_root, '.locks', basename + '.lock')
    print(f'[log_lock] no tasks/ ancestor found for {task_path}; '
          f'falling back to legacy sidecar lock', file=sys.stderr)
    return task_path + '.lock'


def _lock_file_is_current(fh, lock_file: str) -> bool:
    """POSIX only: True if `lock_file` on disk still refers to the same
    inode as the already-open, already-flock'd handle `fh`.

    A `flock()` acquisition only protects the inode the fd points at, not
    the path. If another writer released its lock and then unlinked
    `lock_file` (release-time delete, spec §3.2) between our `open()` and
    our `flock()`, we may hold an exclusive lock on an orphaned inode while
    a fresh `open()` elsewhere creates a *different* inode at the same
    path and locks that instead — two lockers proceed concurrently despite
    both believing they hold "the" lock. This detects that condition
    (`nlink == 0`, the path missing, or the path now pointing at a
    different inode) so the caller can release, reopen, and retry within
    the bounded deadline (INV-2 is preserved — see `log_lock`).

    Pure stat/fstat comparison; platform-independent so it is unit-testable
    without a POSIX runtime (spec §4 M4 — the retry logic must stay
    verifiable even where `fcntl` itself cannot be exercised).
    """
    try:
        held = os.fstat(fh.fileno())
    except OSError:
        return False
    if held.st_nlink == 0:
        return False
    try:
        disk = os.stat(lock_file)
    except OSError:
        return False
    return held.st_ino == disk.st_ino and held.st_dev == disk.st_dev


@contextlib.contextmanager
def log_lock(task_path: str):
    """Serialize `@log` writes to `task_path` via an advisory lock on
    `<project_root>/.locks/<task-basename>.lock` (see `lock_path_for`).

    Bounded acquire (POSIX: LOCK_NB retried to a LOCK_TIMEOUT_S deadline;
    win32: msvcrt ~10s), released on context exit (including on exception).
    INV-2 (no-deadlock): never blocks unbounded. On acquire timeout, or if the
    platform lock primitive is unavailable, the body still runs unlocked
    (degrade-unlocked + log; exec-binding.md §3.5 / R1) — the residual
    concurrent-append race is the known R-lock gap and must be logged, never
    silently treated as solved.

    Release-time delete (spec §3.2): the lock sidecar is removed once the
    last holder releases it, so `.locks/` holds no permanent residue for
    live tasks. The safe unlink POSITION differs by platform and must not
    be swapped:
      - win32: unlock -> close -> unlink. `open()` on Windows does not pass
        FILE_SHARE_DELETE, so a still-open handle (ours, until closed, or
        any other writer's) makes unlink fail — the OS enforces "only the
        last holder deletes it" for free. Deleting before close would
        simply fail for us too, so the order also happens to be the only
        one that can work.
      - POSIX: unlink -> unlock (delete WHILE still holding the flock).
        `unlink()` never fails just because an fd is open, so unlinking
        AFTER unlock would let a waiter (already blocked in `flock()`,
        already holding a stat-verified-current fd from before our
        unlink) proceed on an orphaned inode while a third writer opens a
        fresh inode at the now-vacant path — two lockers live at once.
        Unlinking first, then unlocking, means any waiter that acquires
        next must have opened AFTER our unlink and therefore sees the new
        inode; `_lock_file_is_current` (called right after every POSIX
        acquire) is the other half of this — it catches the case where a
        waiter's `open()` raced ahead of our unlink instead.
    Unlink only ever runs when `locked` is True — a caller that degraded
    unlocked never touches a sidecar it does not own (spec §3.2 L1).
    """
    lock_file = lock_path_for(task_path)
    try:
        os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    except OSError:
        pass  # best-effort; the open() below will raise and degrade-unlocked

    fh = None
    try:
        # Open (create if absent) the lock sidecar. Use a stable handle for the
        # whole locked region.
        fh = open(lock_file, 'a+')
    except OSError:
        # Could not create the lock file (e.g. unwritable dir). Proceed
        # unlocked rather than blocking the write entirely; the race is the
        # known R-lock residual.
        print(f'[log_lock] could not open lock file {lock_file}; proceeding unlocked',
              file=sys.stderr)
        yield
        return

    locked = False
    try:
        if _HAVE_MSVCRT:
            # msvcrt locks a byte range from the current file position. Lock 1
            # byte at offset 0 (blocking). The lock file may be empty, so seek
            # to 0 and lock a single byte region.
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            except OSError:
                print(f'[log_lock] msvcrt lock failed on {lock_file}; proceeding unlocked',
                      file=sys.stderr)
        elif _HAVE_FCNTL:
            # Bounded acquire (INV-2): non-blocking flock retried until a
            # deadline, instead of an unbounded LOCK_EX. On timeout, degrade
            # unlocked + log (exec-binding.md §3.5 / R1).
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    if time.monotonic() >= deadline:
                        print(f'[log_lock] fcntl bounded acquire timed out '
                              f'(>{LOCK_TIMEOUT_S}s) on {lock_file}; proceeding unlocked',
                              file=sys.stderr)
                        break
                    time.sleep(0.05)
                    continue

                if _lock_file_is_current(fh, lock_file):
                    locked = True
                    break

                # Stale: we hold flock on an inode a prior holder already
                # unlinked (or that a third party replaced). Release, close,
                # and reopen the (possibly now different) path — bounded by
                # the same deadline (INV-2).
                print(f'[log_lock] stale lock on {lock_file} (unlinked by a '
                      f'prior holder); reacquiring', file=sys.stderr)
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    fh.close()
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    print(f'[log_lock] stale-lock retry exhausted timeout on '
                          f'{lock_file}; proceeding unlocked', file=sys.stderr)
                    fh = None
                    break
                try:
                    fh = open(lock_file, 'a+')
                except OSError:
                    print(f'[log_lock] could not reopen lock file {lock_file} '
                          f'after stale detection; proceeding unlocked',
                          file=sys.stderr)
                    fh = None
                    break
        else:
            print(f'[log_lock] no lock primitive available; proceeding unlocked on {lock_file}',
                  file=sys.stderr)

        yield
    finally:
        try:
            if fh is not None:
                if locked and _HAVE_MSVCRT:
                    fh.seek(0)
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                elif locked and _HAVE_FCNTL:
                    try:
                        os.unlink(lock_file)
                    except OSError:
                        pass
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            if locked and _HAVE_MSVCRT:
                # Only reachable after close: win32 denies unlink of a file
                # with any open handle (ours, until just now, or another
                # writer's). A failure here means someone else still holds
                # it — expected, not an error (spec §3.2 win32).
                try:
                    os.unlink(lock_file)
                except OSError:
                    pass


if __name__ == '__main__':
    # Tiny self-check: acquire and release the lock on a temp task under a
    # real tasks/<status>/ layout (exercises the primary lock_path_for path,
    # not the legacy fallback — spec §3.1).
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tasks_dir = os.path.join(d, 'tasks', '1_in_progress')
        os.makedirs(tasks_dir, exist_ok=True)
        target = os.path.join(tasks_dir, 'task.md')
        with open(target, 'w', encoding='utf-8') as _f:
            _f.write('x')
        with log_lock(target):
            print('acquired')
        print('released')
        assert not os.path.exists(lock_path_for(target)), (
            'release-time delete failed: lock sidecar still present after release')
        print('deleted')
