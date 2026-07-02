"""Tests: ingest_driver — the deterministic ingest CLI (plan C1, §3 contract).

Covers:
  - begin -> finish(success) round-trip (zero LLM-threaded state: state lives in
    the .llmwiki.txn sidecar) -> {committed: true}, index regenerated, sidecar +
    lock cleared;
  - finish(fail) and abort both replay the journal (remove the orphan raw) +
    delete the sidecar;
  - a success-path failure rolls back journaled writes + releases;
  - dedup_noop path (same content re-ingest -> dedup_noop True);
  - plan-fanout ceil split (each cluster <= k);
  - the consistency invariant violation (apply_fanout_k > max_count) aborts begin
    BEFORE locking;
  - the sidecar is removed on every terminal path (success / fail / abort).

No git — the transaction is a file journal.
"""
import json
import os

import pytest

from llmwiki.ingest import ingest_driver as drv
from llmwiki.write import transaction as tx
from llmwiki.core import config_resolver


_SCHEMA = """---
config:
  activation_scope: scoped
  read_grounding:  implicit
  write_mode:      explicit
  write_autocommit: auto
  override_scope:  operation
  apply_fanout_k:  10
  max_count:       100
  max_bytes:       10485760
---
# SCHEMA
"""


def _init_wiki(tmp_path):
    """A .llmwiki marker + SCHEMA.md + index/log — a plain directory (no git)."""
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "index.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# Log\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# begin -> finish(success): commit (discard journal), zero LLM-threaded state
# --------------------------------------------------------------------------- #
def test_begin_finish_success_round_trip(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("third party content", encoding="utf-8")

    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert (tmp_path / drv.SIDECAR_NAME).is_file()
    assert out["dedup_noop"] is False
    assert out["origin"] == drv.ORIGIN_FE_B
    assert out["max_count"] == 100
    assert out["apply_fanout_k"] == 10
    assert out["redacted_body"] == "third party content"

    # Simulate Stage2 having written a page (the driver does NOT author content).
    (tmp_path / "wiki").mkdir(exist_ok=True)
    (tmp_path / "wiki" / "page.md").write_text("# Page", encoding="utf-8")

    res = drv.finish(str(tmp_path), "success",
                     expected_pages=["wiki/page.md"], title="page")
    assert res == {"committed": True}
    # Sidecar removed + lock released + journal discarded on the terminal path.
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
    # index regenerated to include the new page.
    assert "wiki/page.md" in (tmp_path / "index.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# finish(fail): replay journal + remove orphan raw + release + delete sidecar
# --------------------------------------------------------------------------- #
def test_finish_fail_replays_and_cleans_orphan_raw(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("rollback me", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b")
    fe_hash = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))["fe_hash"]
    raw_rel = f"raw/{fe_hash}.txt"
    assert (tmp_path / raw_rel).exists()

    res = drv.finish(str(tmp_path), "fail")
    assert res == {"rolled_back": True}
    # Orphan raw removed by the journal replay (the raw create is undone).
    assert not (tmp_path / raw_rel).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


# --------------------------------------------------------------------------- #
# finish(success) failure: a raise on the success path must roll back journaled
# writes + release + delete sidecar (honours one-of-commit/rollback)
# --------------------------------------------------------------------------- #
def test_finish_success_failure_rolls_back(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("regenerate will fail", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b")
    fe_hash = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))["fe_hash"]
    raw_rel = f"raw/{fe_hash}.txt"
    assert (tmp_path / raw_rel).exists()

    # Simulate Stage2 writing a page THROUGH the journal (as WriteSession does),
    # so the failed success-path rolls it back too.
    (tmp_path / "wiki").mkdir(exist_ok=True)
    tx.journal_before_write(str(tmp_path), ["wiki/page.md"])
    (tmp_path / "wiki" / "page.md").write_text("# Page", encoding="utf-8")

    def _boom(*a, **k):
        raise RuntimeError("simulated regenerate failure")
    monkeypatch.setattr(drv.wiki_index, "regenerate", _boom)

    with pytest.raises(RuntimeError):
        drv.finish(str(tmp_path), "success",
                   expected_pages=["wiki/page.md"], title="page")

    # Rollback restored the pre-ingest state: orphan raw + the page are gone.
    assert not (tmp_path / raw_rel).exists()
    assert not (tmp_path / "wiki" / "page.md").exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()


# --------------------------------------------------------------------------- #
# abort: replay + release + delete sidecar (manual recovery, D-g)
# --------------------------------------------------------------------------- #
def test_abort_replays_and_deletes_sidecar(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("abort me", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b")
    fe_hash = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))["fe_hash"]
    raw_rel = f"raw/{fe_hash}.txt"
    assert (tmp_path / raw_rel).exists()

    res = drv.abort(str(tmp_path))
    assert res["aborted"] is True
    assert not (tmp_path / raw_rel).exists()        # orphan raw cleaned
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


def test_abort_no_sidecar_is_noop_with_message(tmp_path):
    _init_wiki(tmp_path)
    res = drv.abort(str(tmp_path))
    assert res["aborted"] is False
    assert "message" in res


def test_abort_recovers_crashed_begin_without_sidecar(tmp_path):
    """F1: a hard crash in begin's window between the lock and the sidecar leaves
    a held lock + a journal (+ orphan raw) but NO sidecar. abort must still
    recover (replay journal -> remove orphan raw + release lock), else the lock
    wedges every future ingest and the orphan raw poisons D18 dedup."""
    _init_wiki(tmp_path)
    # Simulate begin up to just before _write_sidecar: lock, checkpoint, and a
    # journaled-then-written raw artifact — sidecar never reached.
    tx.acquire_lock(str(tmp_path))
    tx.checkpoint(str(tmp_path))
    tx.journal_before_write(str(tmp_path), ["raw/deadbeef.txt"])
    (tmp_path / "raw").mkdir(exist_ok=True)
    (tmp_path / "raw" / "deadbeef.txt").write_text("orphan raw", encoding="utf-8")
    assert not (tmp_path / drv.SIDECAR_NAME).exists()      # crashed before sidecar
    assert (tmp_path / tx.LOCK_NAME).exists()

    res = drv.abort(str(tmp_path))
    assert res["aborted"] is True
    assert res["recovered_without_sidecar"] is True
    assert not (tmp_path / "raw" / "deadbeef.txt").exists()  # orphan raw undone
    assert not (tmp_path / tx.LOCK_NAME).exists()            # lock released
    assert not (tmp_path / tx.JOURNAL_DIR).exists()          # journal discarded


# --------------------------------------------------------------------------- #
# dedup_noop path: same content re-ingest is a no-op (D18)
# --------------------------------------------------------------------------- #
def test_dedup_noop_path(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("stable third-party content", encoding="utf-8")

    # First ingest: not a no-op; finish(success) commits the raw + index/log.
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    drv.finish(str(tmp_path), "success", title="first")

    # Second ingest of identical content -> dedup_noop True (raw already exists).
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert out["dedup_noop"] is True
    # Per the contract the caller now finish(fail) to roll back (nothing new was
    # written) and release.
    drv.finish(str(tmp_path), "fail")
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


# --------------------------------------------------------------------------- #
# plan-fanout: ceil split, each cluster <= k
# --------------------------------------------------------------------------- #
def test_plan_fanout_under_k_one_cluster(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")   # k=10 from config
    touched = [f"wiki/p{i}.md" for i in range(7)]      # 7 <= 10
    out = drv.plan_fanout(str(tmp_path), json.dumps({"touched": touched}))
    assert out["clusters"] == [touched]
    drv.abort(str(tmp_path))


def test_plan_fanout_over_k_ceil_split_each_le_k(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")   # k=10 from config
    touched = [f"wiki/p{i}.md" for i in range(23)]     # 23 > 10
    out = drv.plan_fanout(str(tmp_path), json.dumps(touched))
    clusters = out["clusters"]
    # ceil(23/10) = 3 clusters, each <= 10, union == touched, order preserved.
    assert len(clusters) == 3
    assert all(len(c) <= 10 for c in clusters)
    assert [p for c in clusters for p in c] == touched
    drv.abort(str(tmp_path))


def test_plan_fanout_requires_sidecar(tmp_path):
    _init_wiki(tmp_path)
    with pytest.raises(drv.DriverError):
        drv.plan_fanout(str(tmp_path), json.dumps(["wiki/a.md"]))


def test_plan_fanout_over_max_count_hits_human_gate(tmp_path):
    """F2: total touched > max_count escalates to the human gate at plan-fanout —
    the per-worker WriteSession budget would otherwise be multiplied by the
    cluster count and never gate the ingest as a whole (D19)."""
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")    # max_count=100 from config
    touched = [f"wiki/p{i}.md" for i in range(101)]     # 101 > 100
    with pytest.raises(drv.DriverError) as ei:
        drv.plan_fanout(str(tmp_path), json.dumps(touched))
    assert "budget overflow" in str(ei.value)
    drv.abort(str(tmp_path))


# --------------------------------------------------------------------------- #
# R6: a binary (non-UTF-8) FE-B source is a clean DriverError, not a traceback
# --------------------------------------------------------------------------- #
def test_begin_binary_source_is_clean_driver_error(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "image.bin"
    src.write_bytes(b"\xff\xfe\x00\x01\x02\x80\x81")     # invalid UTF-8
    with pytest.raises(drv.DriverError):
        drv.begin(str(tmp_path), str(src), kind="fe_b")
    # The read fails BEFORE locking -> nothing stranded (glob loop continues, G-f).
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


# --------------------------------------------------------------------------- #
# consistency invariant: violation aborts begin BEFORE locking
# --------------------------------------------------------------------------- #
def test_consistency_violation_aborts_begin_before_locking(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("never ingested", encoding="utf-8")

    # apply_fanout_k (200, prompt override) > max_count (100) violates D-c.
    with pytest.raises(config_resolver.ConfigInconsistency):
        drv.begin(str(tmp_path), str(src), kind="fe_b", apply_fanout_k="200")

    # No side effect: no lock, no sidecar, no journal, no raw artifact written.
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
    assert not (tmp_path / "raw").exists()


# --------------------------------------------------------------------------- #
# sidecar schema: begin writes the documented keys
# --------------------------------------------------------------------------- #
def test_sidecar_schema_keys(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("schema check", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    state = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))
    # T4 (S8-b) added `pending_ledger_entries` — the begin->finish on-disk channel
    # carrying the FE-B' projector's novel turn-content-hash entries (FE-B emits
    # none, so it is []). The sidecar schema is now 10 keys.
    assert set(state) == {
        "journal_dir", "origin", "doc_type",
        "max_count", "max_bytes", "apply_fanout_k", "fe_hash", "pid",
        "lock_token", "pending_ledger_entries",
    }
    # FE-B has no projection, so the channel is present-but-empty.
    assert state["pending_ledger_entries"] == []
    drv.abort(str(tmp_path))   # clean up the lock/sidecar


# --------------------------------------------------------------------------- #
# DEC-R1=D: finish/abort refuse a transaction they do not own (token mismatch)
# --------------------------------------------------------------------------- #
def test_finish_refuses_on_lock_ownership_mismatch(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("owned by A", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")

    # Simulate a DIFFERENT ingest holding the lock: overwrite it with a foreign
    # token + a live pid (so it is genuinely held, not stale residue).
    (tmp_path / tx.LOCK_NAME).write_text(
        json.dumps({"pid": os.getpid(), "token": "FOREIGN"}), encoding="utf-8")

    with pytest.raises(drv.DriverError) as ei:
        drv.finish(str(tmp_path), "success", expected_pages=[], title="x")
    assert "ownership mismatch" in str(ei.value)
    # Refused WITHOUT touching the foreign lock or our sidecar.
    assert (tmp_path / tx.LOCK_NAME).exists()
    assert (tmp_path / drv.SIDECAR_NAME).exists()


def test_abort_refuses_on_lock_ownership_mismatch(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("owned by A", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    (tmp_path / tx.LOCK_NAME).write_text(
        json.dumps({"pid": os.getpid(), "token": "FOREIGN"}), encoding="utf-8")

    res = drv.abort(str(tmp_path))
    assert res["aborted"] is False and "ownership mismatch" in res["message"]
    assert (tmp_path / tx.LOCK_NAME).exists()
    assert (tmp_path / drv.SIDECAR_NAME).exists()
