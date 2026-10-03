"""OS file-lock driver for the run lock and state lock of `dispatch`/`wait`: a
wrapper holds the run lock only while it is actually admitted and running, and
releases it only after its own final write, so an observer's `probe` tells a
live wrapper from a vanished one without trusting any PID. `run` and `resume`
take the run dir's lock through it as well.

Domain-level primitives only (`probe` / `try_acquire` / `acquire` /
`release`); `wfrun.__main__` is the only caller. Every lock file holds no
content of its own -- callers write a sibling marker file when they need to
say who is holding it -- and every lock is exactly 1 byte at offset 0, so
that:

- POSIX uses `fcntl.flock` (advisory, tied to the open file description)
  rather than `fcntl.lockf`/`F_SETLK` (POSIX record locks are per-PROCESS:
  a second handle opened by the same process never contends with the
  first, which would silently break every in-process unit test that
  simulates "a live wrapper" by opening a second handle in this same
  interpreter).
- Windows uses `msvcrt.locking`, whose contention raises `PermissionError`
  (errno EACCES) regardless of whether the other holder is this process or
  another one -- measured 2026-09-29 on this host (docs/dev/test-constraints.md).
"""
from __future__ import annotations

import errno
import os
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

__all__ = ["HELD", "FREE", "UNKNOWN", "LockHandle",
           "probe", "acquire", "try_acquire", "release", "is_unsupported"]

HELD = "HELD"
FREE = "FREE"
UNKNOWN = "UNKNOWN"

_LOCK_LEN = 1
_UNSUPPORTED_ERRNOS = {getattr(errno, name) for name in
                       ("ENOLCK", "ENOTSUP", "EOPNOTSUPP", "ENOSYS")
                       if hasattr(errno, name)}


class LockHandle:
    """An open, locked file handle. Opaque to callers; pass back to
    `release()` only."""
    __slots__ = ("_fh",)

    def __init__(self, fh):
        self._fh = fh


def _ensure_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()


def _is_contention(exc: OSError) -> bool:
    if os.name == "nt":
        return isinstance(exc, PermissionError) and exc.errno == errno.EACCES
    return isinstance(exc, BlockingIOError)


def is_unsupported(exc: OSError) -> bool:
    """True when `exc` (raised by `try_acquire`) means locking itself is
    not implemented for this path -- as opposed to ordinary contention or
    some other failure (permission, a transient filesystem error), which
    callers may treat differently."""
    return exc.errno in _UNSUPPORTED_ERRNOS


def _lock_nb(fh) -> None:
    """Raise on contention (see `_is_contention`) or any other lock
    failure; return normally once the byte is held."""
    fh.seek(0)
    if os.name == "nt":
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, _LOCK_LEN)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh) -> None:
    fh.seek(0)
    if os.name == "nt":
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, _LOCK_LEN)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def probe(path: str | Path) -> str:
    """HELD / FREE / UNKNOWN for the lock at `path`, without creating the
    file or leaving any lock behind. UNKNOWN covers both "never touched by
    any wrapper" (the file does not exist) and "the lock call itself
    failed for a reason other than contention" (unsupported filesystem,
    permission) -- callers that need to tell those apart use the caller-
    side fallback (a time deadline), never this return value alone."""
    path = Path(path)
    if not path.exists():
        return UNKNOWN
    try:
        fh = open(path, "r+b")
    except OSError:
        return UNKNOWN
    try:
        try:
            _lock_nb(fh)
        except OSError as e:
            return HELD if _is_contention(e) else UNKNOWN
        else:
            _unlock(fh)
            return FREE
    finally:
        fh.close()


def try_acquire(path: str | Path) -> LockHandle | None:
    """Non-blocking acquire: a handle to hold and later `release()`, or
    None when another holder already has it. Raises OSError for anything
    else -- whether that means fail-open or fail-closed is the caller's
    policy, not this driver's."""
    path = Path(path)
    _ensure_file(path)
    fh = open(path, "r+b")
    try:
        _lock_nb(fh)
    except OSError as e:
        fh.close()
        if _is_contention(e):
            return None
        raise
    return LockHandle(fh)


def acquire(path: str | Path) -> LockHandle:
    """Blocking acquire. Used by tests only, to simulate a live holder
    in-process; production code always uses the bounded retry built on
    `try_acquire` (wfrun.__main__) instead, so a wedged lock cannot hang a
    CLI invocation indefinitely."""
    path = Path(path)
    _ensure_file(path)
    fh = open(path, "r+b")
    try:
        fh.seek(0)
        if os.name == "nt":
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, _LOCK_LEN)
                    break
                except OSError as e:
                    if not _is_contention(e):
                        raise
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except BaseException:
        fh.close()
        raise
    return LockHandle(fh)


def release(handle: LockHandle) -> None:
    _unlock(handle._fh)
    handle._fh.close()
