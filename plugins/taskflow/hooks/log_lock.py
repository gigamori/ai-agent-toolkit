#!/usr/bin/env python3
"""Per-task advisory lock for serializing `@log` writes (stdlib only).

Shared helper for the taskflow Stop-hook backstop and any script that appends
to a task md's `<!-- @log:begin/end -->` block. Serializes concurrent
read-modify-write of the same task file so two writers do not clobber each
other's append.

Mechanism (advisory, OS-native, stdlib only):
  - Windows (win32 primary): `msvcrt.locking` on a `<task>.lock` sidecar.
  - POSIX:                    `fcntl.flock` on a `<task>.lock` sidecar.

The lock is taken on a separate `<task>.lock` file (NOT the task md itself),
so the locked region never overlaps the bytes being rewritten and the task md
can be fully truncated/rewritten while the lock is held.

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
    """Return the sidecar lock path for a task md path: `<task>.lock`."""
    return task_path + '.lock'


@contextlib.contextmanager
def log_lock(task_path: str):
    """Serialize `@log` writes to `task_path` via an advisory lock on
    `<task>.lock`.

    Bounded acquire (POSIX: LOCK_NB retried to a LOCK_TIMEOUT_S deadline;
    win32: msvcrt ~10s), released on context exit (including on exception).
    INV-2 (no-deadlock): never blocks unbounded. On acquire timeout, or if the
    platform lock primitive is unavailable, the body still runs unlocked
    (degrade-unlocked + log; exec-binding.md §3.5 / R1) — the residual
    concurrent-append race is the known R-lock gap and must be logged, never
    silently treated as solved.
    """
    lock_file = lock_path_for(task_path)
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
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        print(f'[log_lock] fcntl bounded acquire timed out '
                              f'(>{LOCK_TIMEOUT_S}s) on {lock_file}; proceeding unlocked',
                              file=sys.stderr)
                        break
                    time.sleep(0.05)
        else:
            print(f'[log_lock] no lock primitive available; proceeding unlocked on {lock_file}',
                  file=sys.stderr)

        yield
    finally:
        try:
            if locked and _HAVE_MSVCRT:
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            elif locked and _HAVE_FCNTL:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                fh.close()
            except OSError:
                pass


if __name__ == '__main__':
    # Tiny self-check: acquire and release the lock on a temp path.
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, 'task.md')
        with open(target, 'w', encoding='utf-8') as _f:
            _f.write('x')
        with log_lock(target):
            print('acquired')
        print('released')
