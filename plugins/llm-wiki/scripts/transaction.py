# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""single git transaction + .llmwiki.lock (D21).

Wraps an ingest as ONE git transaction:
  1. acquire `.llmwiki.lock` (exclusive; concurrent ingest is excluded);
  2. checkpoint BEFORE the front-ends (record HEAD + stash any uncommitted
     hand-edits so the tree is clean — R8 precondition);
  3. on success -> a single commit;
  4. on failure -> reset to the checkpoint + wiki-root-scoped clean that removes
     ORPHAN raw artifacts (so dedup, D18, stays valid), restoring the pre-ingest
     state;
  5. release the lock (always).

I/O contract:
    acquire_lock(wiki_root) -> LockHandle          # raises LockHeld if held
    release_lock(handle)
    checkpoint(wiki_root) -> Checkpoint            # records HEAD sha (+ stash)
    rollback(wiki_root, checkpoint)                # reset + scoped clean
    commit(wiki_root, message) -> str              # single commit, returns sha

    transaction(wiki_root, message) -> context manager
      Usage:
        with transaction(root, "ingest: <title>") as txn:
            ...front-ends + write_tool.commit()...
        # normal exit -> commit; exception -> rollback; lock always released.

The git ops are scoped to the wiki root (the repo is the wiki). `_git` returns
None on failure (non-fatal probing) but the transaction surfaces a hard error if
a required op (reset/commit) fails so the caller does not assume success.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


LOCK_NAME = ".llmwiki.lock"


class LockHeld(Exception):
    """Another ingest holds the .llmwiki.lock (D21 concurrency exclusion)."""


class GitError(Exception):
    """A required git operation failed."""


class NotARepoRoot(Exception):
    """The wiki-root is NOT its own git repo toplevel (F1 boundary guard).

    Refuses to operate when `<wiki_root>` is a plain subdirectory of a PARENT
    repo (or not a repo at all): `git -C <wiki_root>` would otherwise walk up to
    the parent toplevel and a rollback `reset --hard`/`clean -fd` would destroy
    the parent's tree. Each wiki-root MUST be its own nested repo (W-d). This is
    a deterministic precondition check at the load-bearing transaction boundary
    (review 05-review-design.md §2 / F1) — it does NOT change the reset/commit/
    clean semantics, it only refuses before they could hit a parent repo.
    """


@dataclass
class LockHandle:
    path: Path


@dataclass
class Checkpoint:
    head: "str | None"     # HEAD sha at checkpoint time (None if no commits yet)
    stashed: bool          # whether uncommitted edits were stashed (R8)


def _git(wiki_root: "str | Path", args: list, *, check: bool = False) -> "str | None":
    try:
        r = subprocess.run(
            ["git", "-C", str(wiki_root), *args],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        if check:
            raise GitError(f"git {' '.join(args)} failed: {e}") from e
        return None
    if r.returncode != 0:
        if check:
            raise GitError(f"git {' '.join(args)} -> {r.returncode}: {r.stderr.strip()}")
        return None
    return r.stdout.strip()


def assert_repo_root(wiki_root: "str | Path") -> None:
    """Refuse unless `<wiki_root>` is its OWN git repo toplevel (F1 guard).

    Runs `git -C <wiki_root> rev-parse --show-toplevel` and requires the
    resolved toplevel to equal `<wiki_root>` itself. If `<wiki_root>` is a plain
    subdirectory of a parent repo, `git` resolves the toplevel to the PARENT (≠
    `<wiki_root>`) → refuse. If it is not a repo at all, `rev-parse` fails →
    refuse. Either way raise `NotARepoRoot` and do NOT proceed, so a subsequent
    `reset --hard`/`clean -fd` can never reach a parent repo (review §2 / F1).
    Comparison is by resolved (absolutized) path so a relative/symlinked root
    still matches its own toplevel.
    """
    root = Path(wiki_root)
    top = _git(root, ["rev-parse", "--show-toplevel"])
    if top is None:
        raise NotARepoRoot(
            f"{root} is not inside a git repository (no nested wiki repo); "
            "refusing — each wiki-root must be its own git repo"
        )
    try:
        top_resolved = Path(top).resolve()
        root_resolved = root.resolve()
    except OSError as e:
        raise NotARepoRoot(f"could not resolve repo toplevel for {root}: {e}") from e
    if top_resolved != root_resolved:
        raise NotARepoRoot(
            f"wiki-root {root_resolved} is NOT its own git repo toplevel "
            f"(resolved toplevel is {top_resolved}); refusing — "
            "operating here would let reset --hard/clean -fd hit the parent repo"
        )


def acquire_lock(wiki_root: "str | Path") -> LockHandle:
    path = Path(wiki_root) / LOCK_NAME
    try:
        # O_CREAT | O_EXCL -> fails if the lock already exists (atomic).
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise LockHeld(f"{LOCK_NAME} already held")
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return LockHandle(path=path)


def release_lock(handle: LockHandle) -> None:
    try:
        handle.path.unlink()
    except FileNotFoundError:
        pass


def checkpoint(wiki_root: "str | Path") -> Checkpoint:
    # F1 precondition: refuse at the load-bearing boundary unless the wiki-root
    # is its own repo toplevel, so the rollback reset/clean can never reach a
    # parent repo (review §2 / F1). checkpoint() is the single chokepoint every
    # path crosses before any reset/commit (transaction() and ingest_driver.begin
    # both call it first), so guarding here covers ingest/promote/query-filing.
    assert_repo_root(wiki_root)
    head = _git(wiki_root, ["rev-parse", "HEAD"])
    status = _git(wiki_root, ["status", "--porcelain"]) or ""
    stashed = False
    if status.strip():
        # Stash uncommitted hand-edits to satisfy the clean-tree precondition (R8).
        res = _git(wiki_root, ["stash", "push", "--include-untracked",
                               "-m", "llm-wiki-ingest-checkpoint"])
        stashed = res is not None
    return Checkpoint(head=head, stashed=stashed)


def rollback(wiki_root: "str | Path", cp: Checkpoint) -> None:
    """Reset to checkpoint + wiki-root-scoped clean (removes orphan raw, D21)."""
    if cp.head:
        _git(wiki_root, ["reset", "--hard", cp.head])
    else:
        _git(wiki_root, ["reset", "--hard"])
    # Scoped clean: remove untracked files/dirs (the orphan raw artifacts the FEs
    # wrote before failure). Scope is the wiki root.
    _git(wiki_root, ["clean", "-fd"])
    if cp.stashed:
        _git(wiki_root, ["stash", "pop"])


def commit(wiki_root: "str | Path", message: str) -> str:
    _git(wiki_root, ["add", "-A"], check=True)
    _git(wiki_root, ["commit", "-m", message], check=True)
    sha = _git(wiki_root, ["rev-parse", "HEAD"], check=True)
    return sha or ""


@contextmanager
def transaction(wiki_root: "str | Path", message: str):
    """Single git transaction: lock -> checkpoint -> body -> commit/rollback.

    Lock-first ordering (acquire_lock THEN checkpoint). ingest_driver.begin uses
    the reverse (checkpoint-first) because its source may live inside the
    untracked wiki-root and must be read before checkpoint()'s stash
    (ingest-driver.md §"Checkpoint-before-lock"). The in-process CM (promote /
    query-filing) has no such concern, so it keeps lock-before-stash on purpose.
    """
    handle = acquire_lock(wiki_root)
    try:
        # checkpoint() can only raise via assert_repo_root (its first line, before
        # the L159 stash); _git defaults to check=False and does not raise. So a
        # bare release_lock here strands no stash. `except Exception` (not
        # BaseException) keeps that invariant exact and matches the body below; an
        # interrupt == process-kill, recovered via the abort/manual path.
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
        # commit() runs with check=True; on failure roll back to the checkpoint so
        # a failed commit is symmetric with a failed body (all-or-nothing). `else`
        # runs only when the body succeeded, so there is no double rollback.
        try:
            commit(wiki_root, message)
        except Exception:
            rollback(wiki_root, cp)
            raise
    finally:
        release_lock(handle)
