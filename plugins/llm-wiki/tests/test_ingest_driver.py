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
