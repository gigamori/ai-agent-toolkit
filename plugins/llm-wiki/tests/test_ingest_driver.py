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
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from llmwiki.ingest import ingest_driver as drv
from llmwiki.ingest import cc_log_project
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
    # E1 (D-1): begin stdout no longer carries the inline `redacted_body`; it
    # carries the raw artifact's wiki-relative path (Read-able by a downstream
    # stage) plus a short code-side declaration hash instead.
    assert "redacted_body" not in out
    assert out["raw_rel_path"].startswith("raw/")
    assert out["raw_rel_path"].endswith(".txt")
    assert (tmp_path / out["raw_rel_path"]).read_text(
        encoding="utf-8") == "third party content"
    assert isinstance(out["declaration_hash"], str)
    assert len(out["declaration_hash"]) == 12

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
    # C1: begin now auto-closes the txn itself (rollback + release_lock), so it
    # also returns auto_closed True and leaves NO sidecar/lock residue. The caller
    # must NOT run finish (there is no sidecar to finish; a finish would error).
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert out["dedup_noop"] is True
    assert out["auto_closed"] is True
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


# --------------------------------------------------------------------------- #
# C1 / F5: a dedup begin auto-closes with NO lock/sidecar residue and does NOT
# destroy the legitimately committed raw.
#
# Structural premise (spec ingest-llm-dep-fixes.md :24 — "dedup_noop => orphan
# raw non-survival"): a dedup can only ever match a COMMITTED raw, never a half-
# written orphan. While a lock is held the *next* begin fails with LockHeld
# BEFORE it reaches the dedup check, and abort's journal replay removes any
# orphan raw. So auto-close's rollback (here a no-op journal replay — a dedup
# begin journals nothing) leaves both the released lock and the committed raw
# clean, and the caller never needs to run finish to reclaim the lock.
# --------------------------------------------------------------------------- #
def test_dedup_begin_auto_closes_without_residue(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("owned third-party content", encoding="utf-8")

    # Commit a raw so the content is a legitimately owned (committed) artifact.
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    drv.finish(str(tmp_path), "success", title="owned")

    # begin against already-owned content: dedup no-op, auto-closed, no residue.
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert out["dedup_noop"] is True
    assert out["auto_closed"] is True
    # Caller runs NO finish, yet the lock + sidecar are already gone.
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()

    # The lock was genuinely released (not stranded): a subsequent begin proceeds
    # instead of raising LockHeld — and it STILL sees the committed raw (auto-
    # close's rollback did not remove the legitimate raw), so it dedups again.
    again = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert again["dedup_noop"] is True
    assert again["auto_closed"] is True
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()


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


# --------------------------------------------------------------------------- #
# B-6 (F-2): plan-fanout returns manifest_paths — one code-authored ABSOLUTE
# path per cluster ordinal, so the orchestrator/worker never reconstructs a
# temp path across turns (same defect class as #1 stage1_blob_path above).
# --------------------------------------------------------------------------- #
def test_plan_fanout_manifest_paths_inline_json_uses_system_temp(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")   # k=10 from config
    touched = [f"wiki/p{i}.md" for i in range(23)]     # 23 > 10 -> 3 clusters
    out = drv.plan_fanout(str(tmp_path), json.dumps(touched))
    state = drv._read_sidecar(tmp_path)
    fe_hash12 = state["fe_hash"][:12]
    assert len(out["manifest_paths"]) == len(out["clusters"]) == 3
    for i, p in enumerate(out["manifest_paths"]):
        path = Path(p)
        assert path.is_absolute()
        assert path.parent == Path(tempfile.gettempdir())   # inline form -> temp fallback
        assert path.name == f"manifest-{fe_hash12}-{i}.json"
    drv.abort(str(tmp_path))


def test_plan_fanout_manifest_paths_file_input_parented_at_blob_dir(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")   # k=10 from config
    touched = [f"wiki/p{i}.md" for i in range(7)]      # 7 <= 10 -> one cluster
    blobdir = tmp_path / "blobdir"
    blobdir.mkdir()
    proposal_path = blobdir / "stage1.json"
    proposal_path.write_text(json.dumps({"touched": touched}), encoding="utf-8")
    out = drv.plan_fanout(str(tmp_path), str(proposal_path))
    state = drv._read_sidecar(tmp_path)
    fe_hash12 = state["fe_hash"][:12]
    assert len(out["manifest_paths"]) == len(out["clusters"])
    for i, p in enumerate(out["manifest_paths"]):
        path = Path(p)
        assert path.is_absolute()
        assert path.parent == blobdir                       # FILE-path branch, not temp
        assert path.name == f"manifest-{fe_hash12}-{i}.json"
    drv.abort(str(tmp_path))


def test_plan_fanout_manifest_paths_file_under_out_dir_rides_cleanup(tmp_path):
    """Path B: a proposal file inside $OUT_DIR yields manifest_paths under
    that same out_dir, so the manifest rides the project-batch-cleanup sweep."""
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")   # k=10 from config
    touched = [f"wiki/p{i}.md" for i in range(23)]     # 23 > 10 -> 3 clusters
    out_dir = tmp_path / "batchtmp"
    out_dir.mkdir()
    proposal_path = out_dir / "stage1.json"
    proposal_path.write_text(json.dumps(touched), encoding="utf-8")
    out = drv.plan_fanout(str(tmp_path), str(proposal_path))
    assert len(out["manifest_paths"]) == 3
    for p in out["manifest_paths"]:
        assert Path(p).parent == out_dir
    drv.abort(str(tmp_path))


def test_plan_fanout_manifest_paths_empty_touched_aligned(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")   # k=10 from config
    out = drv.plan_fanout(str(tmp_path), json.dumps([]))
    assert out["clusters"] == []
    assert out["manifest_paths"] == []
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
# #1 follow-up: begin returns an ABSOLUTE stage1_blob_path (code-authored) so
# the orchestrator never reconstructs a temp path across turns (a reconstructed
# `AppData\Local\Temp\...` was resolved against the CWD on Windows). Under
# `--out_dir` for Path B (rides project-batch-cleanup); system temp otherwise.
# --------------------------------------------------------------------------- #
def test_begin_stage1_blob_path_absolute_under_out_dir(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    out_dir = tmp_path / "batchtmp"
    out_dir.mkdir()
    out = drv.begin(str(tmp_path), str(src), kind="fe_b", out_dir=str(out_dir))
    blob = Path(out["stage1_blob_path"])
    assert blob.is_absolute()
    assert blob.parent == out_dir                       # placed under out_dir
    assert blob.name == "stage1-input.json"             # keyed by source stem
    drv.abort(str(tmp_path))


def test_begin_stage1_blob_path_defaults_to_system_temp(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "doc.md"
    src.write_text("y", encoding="utf-8")
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")   # no out_dir
    blob = Path(out["stage1_blob_path"])
    assert blob.is_absolute()
    assert blob.parent == Path(tempfile.gettempdir())
    assert blob.name == "stage1-doc.json"
    drv.abort(str(tmp_path))


# --------------------------------------------------------------------------- #
# #2 follow-up: plan-fanout enforces the derived tier for projection origins.
# The derived-tier prefix is a deterministic function of origin (fe_b_prime /
# fe_pi_log -> wiki/derived/, D20), so a Stage1 proposal that omits it is
# rejected HERE (fail-closed, before planned_clusters or any write) rather than
# surfacing only as a late apply-finish cluster_pageset REJECT.
# --------------------------------------------------------------------------- #
def _begin_fe_b_prime(tmp_path, monkeypatch, sid="sid-x"):
    """Open a fe_b_prime transaction via a stubbed projection (no corpus scan)."""
    tf = tmp_path.parent / f"tier-turns-{sid}.json"
    tf.write_text(json.dumps({"sid": sid, "origin": drv.ORIGIN_FE_B_PRIME,
                              "turns": []}), encoding="utf-8")

    def _fake_project_from_turns(root, s, turn_list, *, ledger):
        return cc_log_project.ProjectionResult(
            markdown="# CC Session transcript\n", novel_entries=[],
            ledger_skipped=0)
    monkeypatch.setattr(cc_log_project, "project_from_turns",
                        _fake_project_from_turns)
    return drv.begin(str(tmp_path), sid, kind="fe_b_prime", turns=str(tf))


def test_plan_fanout_derived_origin_rejects_base_tier(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    _begin_fe_b_prime(tmp_path, monkeypatch)
    # fe_b_prime writes the derived tier: a base-tier `wiki/...` touched page is
    # rejected before planned_clusters is persisted.
    with pytest.raises(drv.DriverError, match="tier mismatch"):
        drv.plan_fanout(str(tmp_path), json.dumps(["wiki/db-spec/foo.md"]))
    sidecar = json.loads(
        (tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))
    assert "planned_clusters" not in sidecar             # fail-closed, nothing written
    drv.abort(str(tmp_path))


def test_plan_fanout_derived_origin_accepts_derived_tier(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    _begin_fe_b_prime(tmp_path, monkeypatch)
    touched = ["wiki/derived/db-spec/foo.md", "wiki/derived/db-spec/bar.md"]
    out = drv.plan_fanout(str(tmp_path), json.dumps(touched))
    assert out["clusters"] == [touched]
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
# A-3 fail-closed kind gate (DEC-KIND-1 = Option A / F-3): .jsonl under
# auto/empty --kind is refused; explicit --kind bypasses; non-jsonl auto is
# byte-identical.
# --------------------------------------------------------------------------- #
def test_begin_jsonl_auto_kind_refused_fail_closed(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "some-sid.jsonl"
    src.write_text('{"turns": []}\n', encoding="utf-8")
    with pytest.raises(drv.DriverError) as exc_info:
        drv.begin(str(tmp_path), str(src))     # kind defaults "auto"
    msg = str(exc_info.value)
    assert "--kind=fe_b_prime" in msg
    assert "--kind=fe_pi_log" in msg
    assert "--kind=fe_b" in msg
    # Refused BEFORE any side effect -> nothing locked/written (mirrors
    # test_begin_binary_source_is_clean_driver_error).
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
    assert not (tmp_path / "raw").exists()


def test_begin_jsonl_explicit_fe_b_bypasses_gate(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "some-sid.jsonl"
    src.write_text('{"turns": []}\n', encoding="utf-8")
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert out["origin"] == drv.ORIGIN_FE_B
    assert out["dedup_noop"] is False
    drv.abort(str(tmp_path))


def test_begin_jsonl_explicit_fe_b_prime_bypasses_gate(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    sid = "some-sid"
    src = tmp_path / f"{sid}.jsonl"
    src.write_text('{"turns": []}\n', encoding="utf-8")

    monkeypatch.setattr(cc_log_project, "extract_owned",
                        lambda sid, *, ledger: [])

    def _fake_project_from_turns(root, s, turn_list, *, ledger):
        return cc_log_project.ProjectionResult(
            markdown="# CC Session transcript\n", novel_entries=[],
            ledger_skipped=0)
    monkeypatch.setattr(cc_log_project, "project_from_turns",
                        _fake_project_from_turns)

    out = drv.begin(str(tmp_path), str(src), kind="fe_b_prime")
    assert out["origin"] == drv.ORIGIN_FE_B_PRIME
    drv.abort(str(tmp_path))


def test_begin_non_jsonl_auto_unaffected(tmp_path):
    """Scope guard: the gate's suffix conjunct means non-jsonl auto is
    byte-identical to pre-gate behavior."""
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("plain text source", encoding="utf-8")
    out = drv.begin(str(tmp_path), str(src))     # kind defaults "auto"
    assert out["origin"] == drv.ORIGIN_FE_B
    drv.abort(str(tmp_path))


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
    # P10: the refusal is a rc2 SENTINEL (raised DriverError), not a rc0
    # {"aborted": false} no-op — mirrors finish()'s ownership-mismatch DriverError.
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("owned by A", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    (tmp_path / tx.LOCK_NAME).write_text(
        json.dumps({"pid": os.getpid(), "token": "FOREIGN"}), encoding="utf-8")

    with pytest.raises(drv.DriverError) as ei:
        drv.abort(str(tmp_path))
    assert "ownership mismatch" in str(ei.value)
    # Refused WITHOUT touching the foreign lock or our sidecar.
    assert (tmp_path / tx.LOCK_NAME).exists()
    assert (tmp_path / drv.SIDECAR_NAME).exists()


# --------------------------------------------------------------------------- #
# OI-1 S3(a): _resolve_kind / _resolve_projection_kind — fe_pi_log dispatch
# (duckdb not needed — pure string -> origin table lookups)
# --------------------------------------------------------------------------- #
def test_resolve_kind_fe_pi_log():
    assert drv._resolve_kind("fe_pi_log") == drv.ORIGIN_FE_PI_LOG


def test_resolve_kind_unknown_lists_fe_pi_log_in_error():
    with pytest.raises(drv.DriverError) as ei:
        drv._resolve_kind("bogus")
    assert "fe_pi_log" in str(ei.value)


def test_resolve_projection_kind_auto_defaults_to_fe_b_prime():
    assert drv._resolve_projection_kind("auto") == drv.ORIGIN_FE_B_PRIME


def test_resolve_projection_kind_fe_b_defaults_to_fe_b_prime():
    # begin's --kind=fe_b maps to ORIGIN_FE_B; the projection-only verbs remap
    # that to fe_b_prime (change point 7/9, "auto->fe_b_prime 既定").
    assert drv._resolve_projection_kind("fe_b") == drv.ORIGIN_FE_B_PRIME


def test_resolve_projection_kind_fe_b_prime_stays_fe_b_prime():
    assert drv._resolve_projection_kind("fe_b_prime") == drv.ORIGIN_FE_B_PRIME


def test_resolve_projection_kind_fe_pi_log_stays_fe_pi_log():
    assert drv._resolve_projection_kind("fe_pi_log") == drv.ORIGIN_FE_PI_LOG


# --------------------------------------------------------------------------- #
# OI-1 S3(c) Path A: begin(kind=fe_pi_log) -> finish round trip, synthetic
# pi-log fixture (tmp_path + PI_CODING_AGENT_DIR override, per pi_log_project's
# _session_dir() override mechanism confirmed in S1). Verifies the sidecar
# origin is stamped fe_pi_log and the log.md header prefix comes from
# wiki_log.header_for_fe_pi_log ("file"/"pi-log").
# --------------------------------------------------------------------------- #
def _pi_msg(entry_id, parent_id, role, ts, text):
    return {
        "type": "message", "id": entry_id, "parentId": parent_id,
        "timestamp": ts,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _write_pi_session(session_dir, sid, ts_prefix, entries, cwd="/synthetic/cwd"):
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{ts_prefix}_{sid}.jsonl"
    header = {"type": "session", "version": 1, "id": sid, "cwd": cwd}
    path.write_text(
        "\n".join(json.dumps(l) for l in ([header] + entries)) + "\n",
        encoding="utf-8")
    return path


def test_path_a_begin_finish_fe_pi_log_round_trip(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from llmwiki.ingest import pi_log_project as pilp

    agent_dir = tmp_path / "agentdir"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    session_dir = pilp._session_dir() / "--proj--"
    sid = "f0000000-0000-0000-0000-000000000001"
    _write_pi_session(
        session_dir, sid, "2026-07-02T09-00-00-000Z",
        [_pi_msg("e1", None, "user", "2026-07-02T09:00:00.000Z", "hello pi")])

    wiki_root = tmp_path / "wiki_root"
    wiki_root.mkdir()
    _init_wiki(wiki_root)

    out = drv.begin(str(wiki_root), sid, kind="fe_pi_log")
    assert out["origin"] == drv.ORIGIN_FE_PI_LOG
    sidecar = json.loads((wiki_root / drv.SIDECAR_NAME).read_text(encoding="utf-8"))
    assert sidecar["origin"] == "fe_pi_log"

    res = drv.finish(str(wiki_root), "success", expected_pages=[], title=sid)
    assert res == {"committed": True}

    log_text = (wiki_root / "log.md").read_text(encoding="utf-8")
    # wiki_log.header_for_fe_pi_log() == ("file", "pi-log"); the appended line
    # carries that op/tag prefix (mirrors S2's smoke: "file|pi-log | ingest").
    assert "file|pi-log" in log_text
    assert not (wiki_root / drv.SIDECAR_NAME).exists()
    assert not (wiki_root / tx.LOCK_NAME).exists()


# --------------------------------------------------------------------------- #
# OI-1 S3(e): F-1 begin --turns origin check (cross-origin mismatch fail-closed
# / origin-key-absent backward-compat default) — duckdb not needed (turns are
# fed directly as JSON, no projector scan).
# --------------------------------------------------------------------------- #
def test_begin_turns_origin_mismatch_fails_closed(tmp_path):
    """A fe_pi_log-stamped turns file fed to --kind=fe_b_prime is rejected."""
    _init_wiki(tmp_path)
    bad = tmp_path.parent / "mismatch-turns.json"
    bad.write_text(
        json.dumps({"sid": "some-sid", "origin": "fe_pi_log", "turns": []}),
        encoding="utf-8")
    with pytest.raises(drv.DriverError, match="origin mismatch"):
        drv.begin(str(tmp_path), "some-sid", kind="fe_b_prime", turns=str(bad))


def test_begin_turns_origin_mismatch_fails_closed_reverse(tmp_path):
    """A fe_b_prime-stamped turns file fed to --kind=fe_pi_log is rejected."""
    _init_wiki(tmp_path)
    bad = tmp_path.parent / "mismatch-turns-2.json"
    bad.write_text(
        json.dumps({"sid": "some-sid", "origin": "fe_b_prime", "turns": []}),
        encoding="utf-8")
    with pytest.raises(drv.DriverError, match="origin mismatch"):
        drv.begin(str(tmp_path), "some-sid", kind="fe_pi_log", turns=str(bad))


def test_begin_turns_origin_absent_treated_as_fe_b_prime(tmp_path, monkeypatch):
    """No "origin" key (older project-batch output) is accepted by fe_b_prime."""
    _init_wiki(tmp_path)
    ok = tmp_path.parent / "no-origin-turns.json"
    ok.write_text(
        json.dumps({"sid": "some-sid", "turns": []}), encoding="utf-8")

    def _fake_project_from_turns(root, sid, turn_list, *, ledger):
        from llmwiki.ingest import cc_log_project
        return cc_log_project.ProjectionResult(
            markdown="# CC Session transcript\n", novel_entries=[],
            ledger_skipped=0)
    monkeypatch.setattr(cc_log_project, "project_from_turns",
                        _fake_project_from_turns)

    out = drv.begin(str(tmp_path), "some-sid", kind="fe_b_prime", turns=str(ok))
    assert out["origin"] == "fe_b_prime"


def test_begin_turns_origin_absent_rejected_by_fe_pi_log(tmp_path):
    """The SAME origin-key-absent file is rejected when --kind=fe_pi_log
    (absent-origin defaults to fe_b_prime, which mismatches fe_pi_log)."""
    _init_wiki(tmp_path)
    ok = tmp_path.parent / "no-origin-turns-2.json"
    ok.write_text(
        json.dumps({"sid": "some-sid", "turns": []}), encoding="utf-8")
    with pytest.raises(drv.DriverError, match="origin mismatch"):
        drv.begin(str(tmp_path), "some-sid", kind="fe_pi_log", turns=str(ok))
    # The mismatch is caught before locking (step 3b, before acquire_lock) —
    # no lock / sidecar is ever created (fail-closed, no side effects).
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


# --------------------------------------------------------------------------- #
# #19: begin's ledger diff runs INSIDE the lock (no TOCTOU vs a concurrent
# finish). A ledger append landing AT lock-acquisition time (i.e. after the
# old pre-lock diff point) MUST be visible to begin's diff: the appended turn
# is skipped, not re-filed (no duplicate page turn, no first_sid steal).
# --------------------------------------------------------------------------- #
def test_begin_ledger_diff_runs_inside_lock(tmp_path, monkeypatch):
    from llmwiki.ingest import ledger as ld

    _init_wiki(tmp_path)
    h1 = ld.compute_hash("user", "hello")
    h2 = ld.compute_hash("assistant", "world")
    turns = [
        {"role": "user", "uuid": "u1", "ts": "2026-07-07T00:00:00",
         "projected_text": "hello", "hash": h1, "tool_uses": []},
        {"role": "assistant", "uuid": "u2", "ts": "2026-07-07T00:00:01",
         "projected_text": "world", "hash": h2, "tool_uses": []},
    ]
    tf = tmp_path.parent / "race-turns.json"
    tf.write_text(json.dumps({"sid": "sid-a", "origin": drv.ORIGIN_FE_B_PRIME,
                              "turns": turns}), encoding="utf-8")

    real_acquire = tx.acquire_lock

    def racing_acquire(root):
        handle = real_acquire(root)
        # Simulate a concurrent ingest whose finish appended h1 between the OLD
        # pre-lock diff point and our lock acquisition (the exact TOCTOU window).
        ld.append_entries(tmp_path, [ld.LedgerEntry(
            hash=h1, first_sid="other-sid", first_uuid="ux",
            first_ts="2026-07-06T23:59:59")])
        return handle

    monkeypatch.setattr(drv.transaction, "acquire_lock", racing_acquire)

    out = drv.begin(str(tmp_path), "sid-a", kind="fe_b_prime", turns=str(tf))
    # The in-lock diff observed the concurrent append: h1 skipped, h2 novel.
    assert out["ledger_skipped"] == 1
    sidecar = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(
        encoding="utf-8"))
    pending_hashes = [e["hash"] for e in sidecar["pending_ledger_entries"]]
    assert pending_hashes == [h2]
    # Cleanup: terminal path releases the lock and removes the sidecar.
    drv.finish(str(tmp_path), "fail")
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


# --------------------------------------------------------------------------- #
# C3: project-batch-cleanup — code owns the temp-dir deletion (two guards), and
# project-batch prunes stale llmwiki-turns-* dirs as a backstop.
#
# Guard 1: basename must start with `_BATCH_TURNS_PREFIX`.
# Guard 2: parent (resolved) must be `tempfile.gettempdir()`.
# Either failing => REFUSED DriverError with NO deletion (never trusts the
# caller-supplied out_dir the way the old bare `rm -rf "$OUT_DIR"` did).
# --------------------------------------------------------------------------- #
def test_project_batch_cleanup_refuses_wrong_prefix():
    """A dir directly under the temp root but WITHOUT the llmwiki-turns- prefix
    is REFUSED and left untouched (guard 1)."""
    d = Path(tempfile.mkdtemp(prefix="notmine-"))
    try:
        with pytest.raises(drv.DriverError, match="REFUSED"):
            drv.project_batch_cleanup(str(d))
        assert d.is_dir()        # not deleted
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_project_batch_cleanup_refuses_outside_temp(tmp_path):
    """A dir with the right prefix but NOT directly under gettempdir (here a
    nested pytest tmp_path subdir) is REFUSED and left untouched (guard 2)."""
    d = tmp_path / "sub" / f"{drv._BATCH_TURNS_PREFIX}fake"
    d.mkdir(parents=True)
    with pytest.raises(drv.DriverError, match="REFUSED"):
        drv.project_batch_cleanup(str(d))
    assert d.is_dir()            # not deleted


def test_project_batch_cleanup_deletes_valid_dir():
    """A genuine project-batch temp dir (llmwiki-turns-* directly under the
    system temp dir) is deleted by the verb (both guards pass)."""
    d = Path(tempfile.mkdtemp(prefix=drv._BATCH_TURNS_PREFIX))
    (d / "sid.json").write_text("{}", encoding="utf-8")   # a per-sid turn file
    assert d.is_dir()
    res = drv.project_batch_cleanup(str(d))
    assert Path(res["cleaned"]) == d
    assert not d.exists()


def test_project_batch_prunes_stale_turn_dirs(tmp_path, monkeypatch):
    """C3 step 2 backstop: project-batch prunes a stale (>24h) llmwiki-turns-*
    temp dir at its start, while a fresh one (younger than the threshold) stays."""
    _init_wiki(tmp_path)
    stale = Path(tempfile.mkdtemp(prefix=drv._BATCH_TURNS_PREFIX))
    fresh = Path(tempfile.mkdtemp(prefix=drv._BATCH_TURNS_PREFIX))
    old = time.time() - (drv._BATCH_STALE_PRUNE_SECONDS + 3600)   # ~25h ago
    os.utime(stale, (old, old))

    # Stub the one expensive scan (default kind=auto -> fe_b_prime -> cc_log_project).
    monkeypatch.setattr(cc_log_project, "extract_turns_batch",
                        lambda sids, *, ledger: {s: [] for s in sids})

    out = None
    try:
        out = drv.project_batch(str(tmp_path), ["sidX"])
        assert not stale.exists()   # pruned (mtime older than the 24h threshold)
        assert fresh.exists()       # retained (fresh, under the threshold)
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
        if out is not None:
            shutil.rmtree(out["out_dir"], ignore_errors=True)
        shutil.rmtree(stale, ignore_errors=True)


# --------------------------------------------------------------------------- #
# C2 (Option C): per-dispatch cluster receipt. `plan-fanout` persists the
# planned cluster set (0-based ordinal = list index); `ingest-apply` appends a
# receipt per run (applied_clusters); `finish` (expected_pages OMITTED) checks
# every planned ordinal has a receipt -> a whole-cluster drop rolls back, while
# a legitimately empty manifest (receipt present, written empty) is NOT a false
# positive. Explicit expected_pages keeps the current on-disk page check.
# --------------------------------------------------------------------------- #
def _apply_cluster(monkeypatch, root, origin, ordinal, manifest) -> int:
    """Run the real `ingest-apply` verb (cli) with a cluster ordinal so it writes
    a dispatch receipt to the sidecar — mirrors the orchestrator's per-cluster
    apply call (wiki-ingest.md Step 4)."""
    import io
    from llmwiki import cli
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(manifest)))
    return cli._ingest_apply([str(root), origin, str(ordinal)])


def test_c2_cluster_drop_finish_rolls_back(tmp_path, monkeypatch):
    """(1) A cluster that was never dispatched (no ingest-apply receipt) is
    caught by finish (expected_pages omitted) -> rollback."""
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("c2 cluster drop", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b", apply_fanout_k="1")
    # k=1 so two touched pages split into two clusters (ordinals 0 and 1).
    out = drv.plan_fanout(str(tmp_path), json.dumps(["wiki/a.md", "wiki/b.md"]))
    assert len(out["clusters"]) == 2
    sidecar = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))
    assert sidecar["planned_clusters"] == [["wiki/a.md"], ["wiki/b.md"]]

    # Dispatch ONLY cluster 0; cluster 1 is dropped (its apply never runs).
    rc = _apply_cluster(monkeypatch, tmp_path, "fe_b", 0,
                        [{"rel_path": "wiki/a.md", "content": "# A"}])
    assert rc == 0

    with pytest.raises(drv.DriverError, match="never dispatched"):
        drv.finish(str(tmp_path), "success")   # expected_pages OMITTED
    # Rolled back: sidecar/lock/journal cleared, cluster-0 page removed too.
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
    assert not (tmp_path / "wiki" / "a.md").exists()


def test_c2_empty_manifest_cluster_is_not_false_positive(tmp_path, monkeypatch):
    """(2) A cluster whose apply legitimately wrote nothing still recorded its
    receipt -> finish(success) commits (no false positive)."""
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("c2 empty manifest", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b", apply_fanout_k="1")
    out = drv.plan_fanout(str(tmp_path), json.dumps(["wiki/a.md"]))
    assert len(out["clusters"]) == 1

    # The single cluster's apply commits an EMPTY manifest (written == []).
    rc = _apply_cluster(monkeypatch, tmp_path, "fe_b", 0, [])
    assert rc == 0
    sidecar = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))
    assert sidecar["applied_clusters"] == [0]      # receipt present
    assert sidecar["applied_written"] == []        # but wrote nothing

    res = drv.finish(str(tmp_path), "success")     # expected_pages OMITTED
    assert res == {"committed": True}
    assert not (tmp_path / drv.SIDECAR_NAME).exists()


def test_c2_explicit_expected_pages_backward_compat(tmp_path):
    """(3) Explicit expected_pages keeps the on-disk page check and BYPASSES the
    cluster-receipt check (backward compat): planned clusters with NO receipts do
    not block a finish that supplied an on-disk-satisfied expected_pages."""
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("c2 explicit expected", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b", apply_fanout_k="1")
    # Two planned clusters, but NEITHER receipt recorded -> the cluster check
    # WOULD fail; explicit expected_pages must take the on-disk branch instead.
    drv.plan_fanout(str(tmp_path), json.dumps(["wiki/a.md", "wiki/b.md"]))
    (tmp_path / "wiki").mkdir(exist_ok=True)
    (tmp_path / "wiki" / "page.md").write_text("# Page", encoding="utf-8")

    res = drv.finish(str(tmp_path), "success", expected_pages=["wiki/page.md"])
    assert res == {"committed": True}


def test_c2_single_cluster_unapplied_rolls_back(tmp_path):
    """(4) D-COV: even a <= K single cluster (ordinal 0) that was never applied
    is caught (plan-fanout is called for <= K too) -> finish rolls back."""
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("c2 single cluster drop", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b")   # k=10 default (<= K path)
    out = drv.plan_fanout(str(tmp_path), json.dumps(["wiki/a.md"]))
    assert len(out["clusters"]) == 1                  # one cluster, ordinal 0
    # No ingest-apply receipt for ordinal 0.
    with pytest.raises(drv.DriverError, match="never dispatched"):
        drv.finish(str(tmp_path), "success")          # expected_pages OMITTED
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
