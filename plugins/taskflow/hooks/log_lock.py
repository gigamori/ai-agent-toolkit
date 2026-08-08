#!/usr/bin/env python3
"""Cross-harness advisory write lock, protocol v2 (stdlib only).

Shared helper for the taskflow Stop-hook backstop, `hooks/note_links.py`, and
`scripts/rebuild_progress.py`. Serializes concurrent read-modify-write of the
same file so two writers do not clobber each other.

Mechanism (advisory, "existence == lock"):
  - acquire: `O_CREAT|O_EXCL` create of a sidecar lock file; the fd is held
    open for the whole locked region.
  - release: close, then unlink -- on BOTH platforms.

This is one half of a protocol shared with the Pi taskflow extension's
`packages/taskflow/src/write-lock.ts`. Both sides must derive the same lock
path and use the same acquire/release discipline, or neither protects
anything against the other. The contract is specified in
`taskflow-write-lock-v2-design.md` §3; do not "improve" either side
unilaterally.

Note on path spelling across the two implementations: the two sides normalize
to DIFFERENT STRINGS on win32 (this side folds separators to `\\` via
`os.path.normcase`; the Pi side folds them to `/`). They resolve to the same
FILE, which is all mutual exclusion requires. Never write a test that compares
the two implementations' key strings.

Lock file location -- see `lock_path_for`. Task md files are keyed on their
BASENAME, not on their current `tasks/<status>/` path, so a status-folder move
(`/progress start|approve|unstart`) cannot split one task across two lock
files; the resolution layer identifies tasks by basename
(`session_progress_capture._task_basename_index`). `progress.md` gets its own
key under the same `.locks/` directory (new in v2).

Usage (context manager):

    from log_lock import log_lock      # or: from log_lock import write_lock
    with log_lock(task_md_path):
        # read-modify-write the target file here
        ...

`write_lock` is an alias of `log_lock`, preferred by callers that are not
writing an `@log` block (e.g. the `progress.md` rebuild). The `log_lock` name
is retained unchanged for its existing callers.

Known limitations, both accepted by the protocol design and NOT bugs to fix
here:

  - R-lock gap (spec §3.5): this is an *advisory* lock between processes that
    cooperate by calling this helper. The LLM Edit-tool append happens at the
    tool layer and cannot acquire it. That residual concurrent-append race is
    OUT OF SCOPE and is only logged, never treated as solved.
  - No automatic release on crash. "Existence == lock" has no kernel-backed
    owner, so a sidecar left by a killed holder is only reclaimed once it ages
    past `TASKFLOW_LOCK_STALE`. This is the deliberate trade against flock:
    it structurally removes the orphaned-inode hazard (a locked fd surviving
    while a fresh inode appears at the same path) at the cost of a bounded
    post-crash wait. Holds are sub-millisecond, so the trade is heavily
    favourable.
  - "Holders release within the stale threshold" is an IMPLICIT PRECONDITION.
    A sidecar's mtime is set once at create time and never refreshed, so a
    holder that runs longer than `TASKFLOW_LOCK_STALE` looks stale while still
    live. On POSIX a waiter can then unlink a live holder's sidecar, and that
    holder's own release unlinks unconditionally -- which can cascade into
    removing a *successor's* fresh sidecar. On win32 the OS blocks that: an
    open handle makes another process's unlink fail. Hold times are
    sub-millisecond against a 10 s threshold (four orders of margin), which is
    why this is acceptable. See `docs/architecture.md`.
  - The stale-break TOCTOU window is NARROWED, not eliminated -- see
    `_acquire_fd`.
"""
from __future__ import annotations

import contextlib
import datetime
import os
import random
import sys
import time

# Diagnostic payload tag written into the sidecar, so a stuck lock can be
# attributed to a harness. The Pi side writes `pi`. Not load-bearing.
_HARNESS_TAG = 'cc'

# Env-tunable parameters (spec §3.2). Read PER CALL rather than cached at
# module scope: tests drive a tiny `TASKFLOW_LOCK_TIMEOUT` to exercise the
# degrade-unlocked path, and a module imported once must still observe it.
# The Pi side pins the same behaviour for the same reason.
_TIMEOUT_ENV = 'TASKFLOW_LOCK_TIMEOUT'
_TIMEOUT_DEFAULT_S = 3.0
_STALE_ENV = 'TASKFLOW_LOCK_STALE'
_STALE_DEFAULT_S = 10.0


def _env_seconds(name: str, fallback: float) -> float:
    """Read a float-seconds tunable from the environment, per call."""
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def lock_path_for(target_path: str) -> str:
    """Return the sidecar lock path for a write target (spec §3.1).

    `target_path` is normalized first (realpath + normcase), so callers
    passing the same file under different spellings (relative/absolute,
    drive-letter vs Git-Bash form, case, symlink) resolve to the identical
    lock file -- the rendezvous point a mutual-exclusion lock depends on must
    not vary with caller-supplied path spelling.

    The three rules are evaluated IN THIS ORDER, matching the Pi side:

      1. the path has a `tasks` segment (scanned from the END, so a nested
         `tasks` closer to the target wins) ->
         `<project_root>/.locks/<basename>.lock`, where `<project_root>` is
         the PARENT of that `tasks` segment.

      2. otherwise, basename is exactly `progress.md` ->
         `<project_root>/.locks/progress.md.lock`, where `<project_root>` is
         `progress.md`'s OWN parent directory -- `progress.md` is a sibling of
         `tasks/`, not a child of it.

      3. otherwise -> `<target>.lock`, an adjacent fallback that keeps this
         function total for callers outside the project layout.

    Rule 2's definition of `<project_root>` deliberately DIFFERS from rule 1's.
    Both implementations must spell this out or they diverge.

    Accepted degenerate case: a task file literally named
    `tasks/<status>/progress.md` resolves via rule 1 to the same key as the
    project's real `progress.md`. That is over-serialization, not a
    correctness defect, and is left as-is (spec §3.1).

    Deriving the key from the STATUS FOLDER (as the pre-`6feb121` `<task>.lock`
    did) was the root cause this layout replaced: a task's resolved identity is
    its basename (`hooks/session_progress_capture.py`
    `_task_basename_index`), not its current status folder, so a status-folder
    move mid-session must not change which lock file protects it.
    """
    norm = os.path.normcase(os.path.realpath(target_path))
    parts = norm.split(os.sep)
    basename = os.path.basename(norm)

    # Rule 1 -- nearest `tasks/` ancestor, scanned from the end.
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == 'tasks':
            project_root = os.sep.join(parts[:i])
            return os.path.join(project_root, '.locks', basename + '.lock')

    # Rule 2 -- progress.md sits at the project root, beside tasks/.
    if basename == 'progress.md':
        return os.path.join(os.path.dirname(norm), '.locks', 'progress.md.lock')

    # Rule 3 -- adjacent fallback.
    print(f'[log_lock] no tasks/ ancestor found for {target_path}; '
          f'falling back to adjacent sidecar lock', file=sys.stderr)
    return target_path + '.lock'


def _stat_mtime(path: str) -> float | None:
    """Wall-clock mtime of `path`, or None if it cannot be stat'd."""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _acquire_fd(lock_file: str, stale_s: float, deadline: float) -> int | None:
    """Bounded acquire loop (spec §3.2).

    Returns the open fd on success -- held open for the whole locked region,
    which on win32 is also what makes stale-breaking safe, since an open handle
    blocks another process's unlink. Returns None on deadline expiry or on any
    unrecoverable create failure, in which case the caller degrades unlocked.

    `deadline` is a `time.monotonic()` value; mtime comparisons necessarily use
    wall clock, since that is what the filesystem records.

    TOCTOU: the stale check re-reads mtime immediately before unlinking, which
    NARROWS the window but does not eliminate it -- between the second mtime
    read and the unlink, the old holder could release and a third party could
    create a fresh sidecar, which this would then remove. The Pi side carries
    the identical residual window. Do not describe this as solved.
    """
    try:
        os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    except OSError:
        pass  # best-effort; the os.open below will fail and we degrade.

    while True:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass  # somebody holds it -- fall through to the stale check.
        except OSError:
            # Cannot create the sidecar at all (e.g. unwritable dir). No point
            # retrying to the deadline.
            return None
        else:
            try:
                stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
                os.write(fd, f'{os.getpid()} {stamp} {_HARNESS_TAG}\n'.encode('utf-8'))
            except OSError:
                pass  # diagnostic payload is best-effort; the lock still holds.
            return fd

        broke = False
        mtime = _stat_mtime(lock_file)
        if mtime is not None and time.time() - mtime > stale_s:
            # Re-check immediately before unlinking to narrow the TOCTOU
            # window. Only one racer can win the O_EXCL re-create at the top of
            # the loop, so a lost break is self-correcting.
            mtime2 = _stat_mtime(lock_file)
            if mtime2 is not None and time.time() - mtime2 > stale_s:
                try:
                    os.unlink(lock_file)
                    broke = True
                except OSError:
                    # Either another racer already broke it, or -- on win32 -- a
                    # LIVE holder's open handle is blocking the unlink, meaning
                    # the sidecar only LOOKS stale (its mtime is set once at
                    # create time and never refreshed). Fall through to the
                    # sleep: `continue`-ing on a break we cannot win would spin
                    # hot until the deadline.
                    pass

        if time.monotonic() >= deadline:
            return None
        if broke:
            continue  # we removed it -- retry the exclusive create immediately.
        time.sleep(random.uniform(0.010, 0.025))


@contextlib.contextmanager
def log_lock(target_path: str):
    """Serialize writes to `target_path` via the protocol v2 sidecar lock.

    Bounded acquire to a `TASKFLOW_LOCK_TIMEOUT` deadline (default 3.0 s),
    released on context exit including on exception. INV-2 (no-deadlock): this
    never blocks unbounded and never raises out of the lock machinery. On
    acquire timeout the body still runs UNLOCKED, with one warning line -- the
    residual race is the known R-lock gap and must be logged, never silently
    treated as solved.

    Release is close -> unlink on both platforms (spec §3.3). A call that
    degraded unlocked does NOT unlink: it does not own the sidecar.
    """
    lock_file = lock_path_for(target_path)
    deadline = time.monotonic() + _env_seconds(_TIMEOUT_ENV, _TIMEOUT_DEFAULT_S)

    try:
        fd = _acquire_fd(lock_file, _env_seconds(_STALE_ENV, _STALE_DEFAULT_S),
                         deadline)
    except OSError:
        # Acquire must never throw out of this function; degrade instead.
        fd = None

    if fd is None:
        print(f'[log_lock] could not acquire {lock_file} within timeout; '
              f'proceeding unlocked', file=sys.stderr)

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(lock_file)
            except OSError:
                pass


# Preferred name for callers that are not writing an `@log` block (e.g.
# `scripts/rebuild_progress.py`). Same object -- `log_lock` stays exported
# unchanged for its existing callers.
write_lock = log_lock


if __name__ == '__main__':
    # Tiny self-check: acquire and release on a temp task under a real
    # tasks/<status>/ layout (exercises rule 1, not the fallback), then the
    # same for progress.md (rule 2).
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

        progress = os.path.join(d, 'progress.md')
        with open(progress, 'w', encoding='utf-8') as _f:
            _f.write('x')
        expected = os.path.join(os.path.normcase(os.path.realpath(d)),
                                '.locks', 'progress.md.lock')
        assert os.path.normcase(lock_path_for(progress)) == expected, (
            f'progress.md key mismatch: {lock_path_for(progress)!r} != {expected!r}')
        with write_lock(progress):
            print('acquired progress.md')
        assert not os.path.exists(lock_path_for(progress)), (
            'release-time delete failed for progress.md sidecar')
        print('deleted progress.md')
