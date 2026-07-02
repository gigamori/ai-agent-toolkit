# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""single-transaction file journal + .llmwiki.lock (supersedes D21).

Git-independent write-ahead undo journal. An ingest / file / promote is wrapped as
ONE transaction:
  1. acquire `.llmwiki.lock` (exclusive; concurrent ingest excluded);
  2. checkpoint = create the journal dir `.llmwiki.txn.d/` BEFORE any write;
  3. every writer calls `journal_before_write` / `journal_before_move` to record a
     path's pre-image (a backup for an existing file; a create marker otherwise)
     BEFORE mutating it (write-ahead ordering);
  4. on success  -> commit  = discard the journal (finalize);
  5. on failure  -> rollback = replay the journal in reverse (delete created files
     incl. ORPHAN raw — required for D18 dedup correctness; restore modified files
     from backup) then discard the journal;
  6. release the lock (always).

No git is invoked anywhere (the engine deliberately never touches git — a wiki-root
is not guaranteed to be its own repo, so `git -C <wiki-root>` could misfire into an
enclosing parent repo; versioning is purely the user's concern). Crash recovery:
the journal dir + `.llmwiki.txn` sidecar persist, so the driver `abort` verb replays
the journal to restore the pre-transaction state (idempotent).

Concurrency across fanout Stage-2 apply workers is safe: each PROCESS writes its OWN
segment file `journal-<pid>.jsonl` (no shared-file append contention) and clusters
touch disjoint paths. Every driver verb (`begin`/`ingest-apply`/`finish`) is a
separate process, so the default segment = pid is unique per writer automatically.

I/O contract:
    acquire_lock(wiki_root) -> LockHandle          # raises LockHeld if held
    release_lock(handle)
    checkpoint(wiki_root) -> Checkpoint            # creates the journal dir
    journal_before_write(wiki_root, rel_paths)     # WAL: record pre-images
    journal_before_move(wiki_root, src_rel, dst_rel)
    rollback(wiki_root, checkpoint=None)           # replay-reverse + discard
    commit(wiki_root, message) -> str              # discard journal (returns "")

    transaction(wiki_root, message) -> context manager
      Usage:
        with transaction(root, "ingest: <title>") as txn:
            ...writers call journal_before_write() before each mutation...
        # normal exit -> commit; exception -> rollback; lock always released.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


LOCK_NAME = ".llmwiki.lock"
JOURNAL_DIR = ".llmwiki.txn.d"
_BACKUP_SUBDIR = "backup"
# The ingest-driver's on-disk transaction sidecar. Defined here (not imported
# from the higher `ingest` layer) so the F3 residue-shape reclaim guard
# (DEC-R1=B) can tell an in-flight/crashed transaction (journal or sidecar
# present) from a bare lock-only residue — WITHOUT an upward import.
SIDECAR_NAME = ".llmwiki.txn"

# Template skeleton dirs that must NEVER be pruned when undoing a create (F6 prune
# floor): a rolled-back page under wiki/derived/ must not delete wiki/derived/.
_PRUNE_FLOOR = frozenset({
    "wiki", "wiki/derived", "raw", "raw/derived", "raw/assets",
})


class LockHeld(Exception):
    """Another ingest holds the .llmwiki.lock (concurrency exclusion)."""


class StaleJournal(Exception):
    """`checkpoint` found a pre-existing non-empty journal (crash residue).

    DEC-R2: a new transaction must NOT silently absorb a crashed transaction's
    journal (that would confirm its partial writes / poison D18 dedup). The
    caller surfaces this as a driver error naming `abort` (manual recovery).
    """


@dataclass
class LockHandle:
    path: Path
    token: "str | None" = None   # per-acquisition ownership token (DEC-R1=D)


@dataclass
class Checkpoint:
    journal_dir: str   # absolute path to this transaction's journal dir


# --------------------------------------------------------------------------- #
# lock (OS O_EXCL — non-git; unchanged from D21) + stale-lock reclaim (F3)
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    """Best-effort liveness of ``pid``. FAIL CLOSED: any doubt -> treat as alive
    (so a live ingest is never reclaimed). Dependency-free, cross-platform.

    - POSIX: ``os.kill(pid, 0)`` — ESRCH => dead, EPERM => alive-but-not-ours.
    - Windows: ``OpenProcess``/``GetExitCodeProcess`` via ctypes (``os.kill`` on
      Windows would TerminateProcess, so it must NOT be used for a liveness probe).
    """
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_INVALID_PARAMETER = 87
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # No such process -> dead; access-denied / other -> fail closed.
            return kernel32.GetLastError() != ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True   # EPERM (exists, not ours) or anything else -> fail closed
    return True


def _read_lock(path: Path) -> "dict | None":
    """Parse the lock file's `{pid, token}` record, or None if unreadable.

    The lock is JSON since DEC-R1=D (was a bare pid). A legacy bare-int lock
    (written by a pre-DEC version mid-upgrade) is tolerated as `{pid, token:None}`
    so an in-flight upgrade does not wedge. Unreadable/garbage -> None (the caller
    fails closed and treats the lock as held).
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, int):                   # legacy bare-int lock content
        return {"pid": data, "token": None}     # (a bare int is valid JSON)
    if isinstance(data, dict):
        return data
    return None


def read_lock_token(wiki_root: "str | Path") -> "str | None":
    """Public: the ownership token recorded in the lock file, or None (DEC-R1=D).

    `finish` / `abort` compare this against the sidecar's `lock_token` to refuse
    operating on a transaction they do not own.
    """
    info = _read_lock(Path(wiki_root) / LOCK_NAME)
    return info.get("token") if info else None


def _reclaim_if_stale(path: Path) -> bool:
    """Reclaim ONLY a lock-only residue whose owner pid is provably dead (F3,
    narrowed by DEC-R1=B).

    Returns True iff the stale lock was removed (caller may retry the O_EXCL
    create). FAIL CLOSED: reclaim happens ONLY when ALL hold:
      - the lock's pid is provably dead, AND
      - there is NO journal dir, AND
      - there is NO sidecar
    i.e. a bare lock-only residue. A dead pid WITH a journal or a sidecar is an
    in-flight (cross-process ingest whose short-lived `begin` pid is already
    dead) or crashed-mid-transaction state — never auto-reclaimed; recovery is
    the manual `abort` path. An unreadable/unparseable/live/uncertain pid is also
    treated as held. This closes H1: the old code reclaimed a live cross-process
    transaction's lock because its recorded `begin`-subprocess pid was dead.
    """
    info = _read_lock(path)
    if info is None:
        return False
    wiki_root = path.parent
    # Residue-shape guard (DEC-R1=B): only a bare lock-only residue is reclaimable.
    if (wiki_root / JOURNAL_DIR).exists() or (wiki_root / SIDECAR_NAME).exists():
        return False
    pid = info.get("pid")
    try:
        if pid is None or _pid_alive(int(pid)):
            return False
    except (TypeError, ValueError):
        return False
    # TOCTOU-safe reclaim (DEC-R1): the old code unlinked unconditionally, so a
    # concurrent legitimate re-acquirer's fresh lock could be deleted. Instead,
    # atomically move the stale lock aside, then confirm the moved inode is still
    # the one we inspected (same token). If a fresh lock replaced it in the gap,
    # the token differs -> restore it and abstain (never steal a live lock).
    token = info.get("token")
    tomb = wiki_root / f"{LOCK_NAME}.{token}.tomb"
    try:
        os.replace(path, tomb)
    except OSError:
        return False
    after = _read_lock(tomb)
    if after is None or after.get("token") != token:
        try:                                    # moved a DIFFERENT (fresh) lock
            os.replace(tomb, path)              # put it back; abstain
        except OSError:
            pass
        return False
    try:
        tomb.unlink()
    except FileNotFoundError:
        pass
    return True


def acquire_lock(wiki_root: "str | Path") -> LockHandle:
    path = Path(wiki_root) / LOCK_NAME
    token = uuid.uuid4().hex                     # per-acquisition ownership token
    try:
        # O_CREAT | O_EXCL -> fails if the lock already exists (atomic).
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # F3 (narrowed by DEC-R1=B): a bare lock-only residue left by a dead
        # process is reclaimed; a lock that still has a journal/sidecar is
        # in-flight or crashed-mid-txn and is genuinely held.
        if not _reclaim_if_stale(path):
            raise LockHeld(f"{LOCK_NAME} already held")
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LockHeld(f"{LOCK_NAME} already held")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"pid": os.getpid(), "token": token}))
    return LockHandle(path=path, token=token)


def release_lock(handle: LockHandle) -> None:
    try:
        handle.path.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# journal internals
# --------------------------------------------------------------------------- #
def _journal_root(wiki_root: "str | Path") -> Path:
    return Path(wiki_root) / JOURNAL_DIR


def _segment_path(wiki_root: "str | Path", segment: str) -> Path:
    return _journal_root(wiki_root) / f"journal-{segment}.jsonl"


def _read_segment(seg_path: Path) -> list:
    if not seg_path.is_file():
        return []
    out = []
    for line in seg_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _append_entry(seg_path: Path, entry: dict) -> None:
    # WAL: the record is flushed+fsynced BEFORE the caller mutates the path, so a
    # crash between record and mutation is recoverable (replay is idempotent).
    with seg_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def checkpoint(wiki_root: "str | Path") -> Checkpoint:
    """Create the journal dir FRESH (no git). Lock is acquired by the caller first.

    DEC-R2: refuse if a journal already exists with prior-transaction content
    (a segment file or a backup) — that is crash residue, and silently reusing
    it would let the new transaction's `commit` discard the crashed writes
    un-replayed (H2). The caller surfaces `StaleJournal` as a driver error naming
    `abort`. An empty leftover dir (only the bare `backup/` skeleton) is not
    residue and is reused. The residue-shape lock guard (DEC-R1=B) already blocks
    the with-lock crash shape earlier; this covers the lock-free dirty-journal
    shapes (a `rollback` that raised mid-replay, or `finish`'s finally releasing
    the lock after a partial rollback).
    """
    jr = _journal_root(wiki_root)
    if jr.exists():
        segments = list(jr.glob("journal-*.jsonl"))
        backup_dir = jr / _BACKUP_SUBDIR
        backups = [p for p in backup_dir.iterdir()] if backup_dir.is_dir() else []
        if segments or backups:
            raise StaleJournal(
                f"stale journal at {jr} (crash residue); run `abort` first")
    (jr / _BACKUP_SUBDIR).mkdir(parents=True, exist_ok=True)
    return Checkpoint(journal_dir=str(jr))


def journal_before_write(wiki_root: "str | Path", rel_paths, *,
                         segment: "str | None" = None) -> None:
    """Record each path's pre-image BEFORE the caller writes it (WAL).

    No-op if the journal dir is absent (a write outside a transaction stays
    unjournaled — callers that must be journaled run inside a transaction and, for
    the cross-process ingest path, refuse when the journal/sidecar is missing).
    First-touch-wins per segment: a create-then-modify on the same path in one
    writer keeps the original (create) undo.
    """
    jr = _journal_root(wiki_root)
    if not jr.is_dir():
        return
    if segment is None:
        segment = str(os.getpid())
    seg_path = _segment_path(wiki_root, segment)
    existing = _read_segment(seg_path)
    seen = {e.get("rel") for e in existing if e.get("rel")}
    seen |= {e.get("dst") for e in existing if e.get("kind") == "move"}
    n = sum(1 for e in existing if e.get("backup"))
    root = Path(wiki_root)
    backup_dir = jr / _BACKUP_SUBDIR
    for rel in rel_paths:
        rel_posix = str(rel).replace("\\", "/")
        if rel_posix in seen:
            continue
        seen.add(rel_posix)
        target = root / rel_posix
        if target.exists():
            backup_name = f"{segment}-{n}"
            n += 1
            shutil.copy2(target, backup_dir / backup_name)
            _append_entry(seg_path, {"kind": "modify", "rel": rel_posix,
                                     "backup": backup_name})
        else:
            _append_entry(seg_path, {"kind": "create", "rel": rel_posix})


def journal_before_move(wiki_root: "str | Path", src_rel: str, dst_rel: str, *,
                        segment: "str | None" = None) -> None:
    """Record a move (promote): back up src, so rollback restores src + removes dst."""
    jr = _journal_root(wiki_root)
    if not jr.is_dir():
        return
    if segment is None:
        segment = str(os.getpid())
    seg_path = _segment_path(wiki_root, segment)
    existing = _read_segment(seg_path)
    n = sum(1 for e in existing if e.get("backup"))
    root = Path(wiki_root)
    src_posix = str(src_rel).replace("\\", "/")
    dst_posix = str(dst_rel).replace("\\", "/")
    src = root / src_posix
    if src.exists():
        backup_name = f"{segment}-{n}"
        shutil.copy2(src, jr / _BACKUP_SUBDIR / backup_name)
        _append_entry(seg_path, {"kind": "move", "src": src_posix,
                                 "dst": dst_posix, "backup": backup_name})
    else:
        # src missing -> dst is a plain create to undo.
        _append_entry(seg_path, {"kind": "create", "rel": dst_posix})


# --------------------------------------------------------------------------- #
# rollback / commit
# --------------------------------------------------------------------------- #
def _restore(backup_path: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # temp + os.replace for an atomic overwrite (Windows-safe: MoveFileEx).
    tmp = target.parent / (target.name + ".llmwiki-restore-tmp")
    shutil.copy2(backup_path, tmp)
    os.replace(tmp, target)


def _prune_empty_parents(root: Path, rel_posix: str) -> None:
    parts = rel_posix.split("/")
    for i in range(len(parts) - 1, 0, -1):
        parent_rel = "/".join(parts[:i])
        if parent_rel in _PRUNE_FLOOR:
            break
        parent = root / parent_rel
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        else:
            break


def _all_segments(wiki_root: "str | Path") -> list:
    jr = _journal_root(wiki_root)
    if not jr.is_dir():
        return []
    return sorted(jr.glob("journal-*.jsonl"))


def _discard_journal(wiki_root: "str | Path") -> None:
    shutil.rmtree(_journal_root(wiki_root), ignore_errors=True)


def rollback(wiki_root: "str | Path", cp: "Checkpoint | None" = None) -> None:
    """Replay every journal segment in REVERSE, then discard the journal.

    Paths are disjoint across segments (distinct writers / disjoint clusters), so
    per-segment reverse order is sufficient; `cp` is unused (the journal dir is the
    fixed path under wiki_root) and kept for call-site compatibility.
    """
    root = Path(wiki_root)
    backup_dir = _journal_root(wiki_root) / _BACKUP_SUBDIR
    for seg_path in _all_segments(wiki_root):
        for e in reversed(_read_segment(seg_path)):
            kind = e.get("kind")
            if kind == "create":
                _unlink(root / e["rel"])
                _prune_empty_parents(root, e["rel"])
            elif kind == "modify":
                _restore(backup_dir / e["backup"], root / e["rel"])
            elif kind == "move":
                _unlink(root / e["dst"])
                _prune_empty_parents(root, e["dst"])
                _restore(backup_dir / e["backup"], root / e["src"])
    _discard_journal(wiki_root)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def commit(wiki_root: "str | Path", message: str) -> str:
    """Finalize: discard the journal (no git). Returns "" (no commit sha)."""
    _discard_journal(wiki_root)
    return ""


# --------------------------------------------------------------------------- #
# context manager (in-process: file / promote)
# --------------------------------------------------------------------------- #
@contextmanager
def transaction(wiki_root: "str | Path", message: str):
    """Single file-journal transaction: lock -> checkpoint -> body -> commit/rollback.

    Lock-first (acquire_lock THEN checkpoint) so only the lock holder ever creates
    or touches the fixed-path journal dir. The in-process writers (WriteSession /
    promote / index+log) call journal_before_write() before each mutation.
    """
    handle = acquire_lock(wiki_root)
    try:
        cp = checkpoint(wiki_root)
    except Exception:
        release_lock(handle)
        raise
    try:
        yield cp
    except Exception:
        rollback(wiki_root, cp)
        raise
    else:
        commit(wiki_root, message)
    finally:
        release_lock(handle)
