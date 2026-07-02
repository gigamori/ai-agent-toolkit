"""Tests: single-transaction file journal + .llmwiki.lock (supersedes D21).

Covers: lock excludes concurrent ingest; success discards the journal; failure
replays the journal (deletes created files incl. ORPHAN raw for D18, restores
modified files from backup, restores a move) to the pre-transaction state; the
prune floor never removes the template skeleton; replay is idempotent (crash /
abort). No git — the engine invokes none.
"""
import json
import os

import pytest

from llmwiki.write import transaction as tx


def test_lock_excludes_concurrent_ingest(tmp_path):
    h = tx.acquire_lock(tmp_path)
    try:
        with pytest.raises(tx.LockHeld):
            tx.acquire_lock(tmp_path)
    finally:
        tx.release_lock(h)
    # After release a new acquire succeeds.
    h2 = tx.acquire_lock(tmp_path)
    tx.release_lock(h2)


# --------------------------------------------------------------------------- #
# F3 (narrowed by DEC-R1=B): stale-lock reclaim — a BARE lock-only residue whose
# owner pid is dead is reclaimed; a live/unknowable owner, OR a dead owner that
# still has a journal/sidecar (in-flight / crashed-mid-txn), is NOT reclaimed
# (fail closed, so a busy or cross-process ingest is never stolen — H1).
# --------------------------------------------------------------------------- #
def test_stale_lock_only_residue_reclaimed_when_owner_dead(tmp_path):
    # A bare lock-only residue (dead pid, no journal, no sidecar) IS reclaimable.
    # A legacy bare-int lock is tolerated as input; the reclaimed lock is JSON.
    lock = tmp_path / tx.LOCK_NAME
    lock.write_text(str(2**31 - 1), encoding="utf-8")   # dead pid, legacy format
    h = tx.acquire_lock(tmp_path)                        # reclaims the stale lock
    try:
        assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    finally:
        tx.release_lock(h)


def test_live_lock_not_reclaimed(tmp_path):
    lock = tmp_path / tx.LOCK_NAME
    lock.write_text(str(os.getpid()), encoding="utf-8")  # our own pid = alive
    with pytest.raises(tx.LockHeld):
        tx.acquire_lock(tmp_path)
    lock.unlink()


def test_unparseable_lock_not_reclaimed(tmp_path):
    lock = tmp_path / tx.LOCK_NAME
    lock.write_text("not-a-pid", encoding="utf-8")       # unreadable pid -> held
    with pytest.raises(tx.LockHeld):
        tx.acquire_lock(tmp_path)
    lock.unlink()


def test_stale_lock_not_reclaimed_when_journal_present(tmp_path):
    # H1 regression (DEC-R1=B): a dead-pid lock WITH an open transaction (journal
    # dir present) is an in-flight cross-process ingest — its short-lived `begin`
    # pid is dead by design — and must NOT be reclaimed (the old code stole it,
    # mixing two transactions). Recovery of a genuine crash is the manual `abort`.
    lock = tmp_path / tx.LOCK_NAME
    lock.write_text(json.dumps({"pid": 2**31 - 1, "token": "abc"}),
                    encoding="utf-8")
    (tmp_path / tx.JOURNAL_DIR).mkdir()
    with pytest.raises(tx.LockHeld):
        tx.acquire_lock(tmp_path)


def test_stale_lock_not_reclaimed_when_sidecar_present(tmp_path):
    # DEC-R1=B: a dead-pid lock WITH a sidecar is a crashed-mid-txn residue, not a
    # bare lock-only residue -> not auto-reclaimed (recovery is manual `abort`).
    lock = tmp_path / tx.LOCK_NAME
    lock.write_text(json.dumps({"pid": 2**31 - 1, "token": "abc"}),
                    encoding="utf-8")
    (tmp_path / tx.SIDECAR_NAME).write_text("{}", encoding="utf-8")
    with pytest.raises(tx.LockHeld):
        tx.acquire_lock(tmp_path)


def test_success_discards_journal_and_keeps_writes(tmp_path):
    (tmp_path / "wiki").mkdir()
    with tx.transaction(tmp_path, "ingest: add page"):
        tx.journal_before_write(tmp_path, ["wiki/p.md"])
        (tmp_path / "wiki" / "p.md").write_text("page", encoding="utf-8")
    assert (tmp_path / "wiki" / "p.md").read_text(encoding="utf-8") == "page"
    assert not (tmp_path / tx.JOURNAL_DIR).exists()   # journal discarded on commit
    assert not (tmp_path / tx.LOCK_NAME).exists()     # lock released


def test_failure_removes_created_and_orphan_raw(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "raw" / "derived").mkdir(parents=True)
    with pytest.raises(RuntimeError):
        with tx.transaction(tmp_path, "ingest: will fail"):
            tx.journal_before_write(tmp_path, ["raw/derived/orphanhash.md"])
            (tmp_path / "raw" / "derived" / "orphanhash.md").write_text(
                "orphan raw", encoding="utf-8")
            tx.journal_before_write(tmp_path, ["wiki/partial.md"])
            (tmp_path / "wiki" / "partial.md").write_text("partial", encoding="utf-8")
            raise RuntimeError("simulated ingest failure")
    # Replay restored the pre-ingest state: orphan raw + partial page gone.
    assert not (tmp_path / "raw" / "derived" / "orphanhash.md").exists()
    assert not (tmp_path / "wiki" / "partial.md").exists()
    assert (tmp_path / "raw" / "derived").is_dir()    # skeleton preserved (F6)
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()     # lock released on failure


def test_failure_restores_modified_file(tmp_path):
    (tmp_path / "wiki").mkdir()
    page = tmp_path / "wiki" / "existing.md"
    page.write_text("ORIGINAL", encoding="utf-8")
    with pytest.raises(RuntimeError):
        with tx.transaction(tmp_path, "modify then fail"):
            tx.journal_before_write(tmp_path, ["wiki/existing.md"])
            page.write_text("MODIFIED", encoding="utf-8")
            raise RuntimeError("boom")
    assert page.read_text(encoding="utf-8") == "ORIGINAL"   # restored from backup
    assert not (tmp_path / tx.LOCK_NAME).exists()


def test_lock_released_on_failure(tmp_path):
    with pytest.raises(ValueError):
        with tx.transaction(tmp_path, "ingest"):
            raise ValueError("boom")
    h = tx.acquire_lock(tmp_path)
    tx.release_lock(h)


def test_prune_floor_removes_created_subdir_keeps_skeleton(tmp_path):
    (tmp_path / "wiki" / "derived").mkdir(parents=True)
    with pytest.raises(RuntimeError):
        with tx.transaction(tmp_path, "nested create then fail"):
            tx.journal_before_write(tmp_path, ["wiki/sub/deep.md"])
            (tmp_path / "wiki" / "sub").mkdir()
            (tmp_path / "wiki" / "sub" / "deep.md").write_text("x", encoding="utf-8")
            raise RuntimeError("boom")
    assert not (tmp_path / "wiki" / "sub").exists()   # created subdir pruned
    assert (tmp_path / "wiki").is_dir()               # skeleton floor kept
    assert (tmp_path / "wiki" / "derived").is_dir()


def test_move_rollback_restores_src_removes_dst(tmp_path):
    # promote-shaped move: journal_before_move backs up src; rollback restores it
    # and removes dst.
    (tmp_path / "wiki" / "derived").mkdir(parents=True)
    src = tmp_path / "wiki" / "derived" / "x.md"
    src.write_text("DERIVED", encoding="utf-8")
    with pytest.raises(RuntimeError):
        with tx.transaction(tmp_path, "move then fail"):
            tx.journal_before_move(tmp_path, "wiki/derived/x.md", "wiki/x.md")
            (tmp_path / "wiki" / "x.md").write_text("SOURCE", encoding="utf-8")
            src.unlink()
            raise RuntimeError("boom")
    assert src.read_text(encoding="utf-8") == "DERIVED"   # src restored
    assert not (tmp_path / "wiki" / "x.md").exists()      # dst removed


def test_replay_is_idempotent_crash_then_abort(tmp_path):
    # Simulate a crash: checkpoint + journaled write persist, then a separate
    # rollback (as the `abort` verb does) restores; a second rollback is a no-op.
    (tmp_path / "wiki").mkdir()
    handle = tx.acquire_lock(tmp_path)
    cp = tx.checkpoint(tmp_path)
    tx.journal_before_write(tmp_path, ["wiki/p.md"])
    (tmp_path / "wiki" / "p.md").write_text("partial", encoding="utf-8")
    assert (tmp_path / tx.JOURNAL_DIR).is_dir()           # journal persisted
    tx.rollback(tmp_path, cp)
    assert not (tmp_path / "wiki" / "p.md").exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
    tx.rollback(tmp_path, cp)                             # idempotent: no-op, no raise
    tx.release_lock(handle)


# --------------------------------------------------------------------------- #
# DEC-R2: checkpoint refuses a pre-existing NON-EMPTY journal (crash residue) so
# the next transaction never silently discards the crashed writes un-replayed (H2).
# --------------------------------------------------------------------------- #
def test_checkpoint_refuses_over_stale_journal(tmp_path):
    tx.checkpoint(tmp_path)                               # opens a fresh journal
    tx.journal_before_write(tmp_path, ["wiki/p.md"])      # writes a segment entry
    with pytest.raises(tx.StaleJournal):
        tx.checkpoint(tmp_path)                           # residue -> refuse


def test_checkpoint_reuses_empty_leftover_dir(tmp_path):
    # An empty leftover journal dir (only the bare backup/ skeleton, no segment /
    # backup file) is NOT residue -> checkpoint reuses it without raising.
    tx.checkpoint(tmp_path)                               # creates JOURNAL_DIR/backup/
    tx.checkpoint(tmp_path)                               # empty -> no StaleJournal
    assert (tmp_path / tx.JOURNAL_DIR).is_dir()
