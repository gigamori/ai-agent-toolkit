"""Tests: Path B loop / glob-loop parity (T6/T7, T9 E2E coverage).

The Path B command (wiki-ingest-sessions.md) loops the `session-plan` sid list
one-sid-per-transaction, passing each bare `sid` to `begin` as `$SOURCE`. This
must be PARITY with /wiki-ingest-docs's glob/dir loop:
  1. a bare `sid` and a `<sid>.jsonl` path derive the SAME sid via
     `Path(source).stem` (the FE-B' branch's normalization), so the two surfaces
     reach the projector identically;
  2. the loop is N INDEPENDENT transactions with failure-continue: a sid whose
     begin FAILS leaves NO lock / sidecar / journal (G-f), so the NEXT sid's
     transaction proceeds cleanly (the failing sid does not wedge the loop).

The projector reads the live cc store (not hermetic), so begin's projector seam
(#19 two-phase: `extract_owned` pre-lock, `project_from_turns` in-lock) is
monkeypatched; this test targets the loop/transaction PARITY (the begin/finish
contract each sid runs through), not the projection itself. A real
begin->finish is driven per sid over a minimal `.llmwiki` wiki (like the sibling
driver tests).
"""
import json
from pathlib import Path

import pytest

from llmwiki.ingest import ingest_driver as drv
from llmwiki.ingest import cc_log_project
from llmwiki.write import transaction as tx


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
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "index.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# Log\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# parity (1): a bare sid and a <sid>.jsonl path derive the SAME sid
# --------------------------------------------------------------------------- #
def test_bare_sid_and_jsonl_path_derive_same_sid():
    sid = "025c2fff-572d-4aff-8487-853a9719ad9f"
    # /wiki-file (Path A) passes `<sid>.jsonl`; wiki-ingest-sessions (Path B)
    # passes the bare `sid`. Both go through `Path(source).stem` in begin.
    assert Path(f"{sid}.jsonl").stem == sid
    assert Path(sid).stem == sid
    assert Path(f"{sid}.jsonl").stem == Path(sid).stem


# --------------------------------------------------------------------------- #
# parity (2): N independent transactions with failure-continue over a sid list
# --------------------------------------------------------------------------- #
def test_path_b_loop_failure_continue_independent_transactions(tmp_path, monkeypatch):
    """Loop three sids; the MIDDLE one's begin FAILS. The failing sid strands
    nothing (no lock/sidecar/journal), so the third sid's transaction commits —
    exactly /wiki-ingest-docs's per-file glob loop (failure-continue, G-f)."""
    _init_wiki(tmp_path)
    sids = ["sid-ok-1", "sid-boom", "sid-ok-2"]

    # #19 two-phase seam: the failing sid raises in the PRE-LOCK extract half
    # (so the loop's "no lock / no sidecar / no journal" invariant holds), the
    # healthy sids flow through the in-lock project_from_turns.
    def _extract(sid, *, ledger):
        if sid == "sid-boom":
            raise cc_log_project.ProjectionError(f"projection failed for {sid}")
        return []

    def _project(root, sid, turns, *, ledger):
        # A minimal FE-B'-compatible transcript for the healthy sids.
        return cc_log_project.ProjectionResult(
            markdown=f"# CC Session transcript\n\n## Turn 1 [t]\n\n**Human**: {sid}\n",
            novel_entries=[{"hash": f"h-{sid}", "first_sid": sid,
                            "first_uuid": "u", "first_ts": "t"}],
            ledger_skipped=0,
        )
    monkeypatch.setattr(cc_log_project, "extract_owned", _extract)
    monkeypatch.setattr(cc_log_project, "project_from_turns", _project)

    succeeded, failed = [], []
    for sid in sids:
        # Each iteration is a complete, independent transaction (one sid = one
        # begin->finish), keyed per-sid, --kind=fe_b_prime. Bare sid as $SOURCE.
        try:
            drv.begin(str(tmp_path), sid, kind="fe_b_prime")
        except cc_log_project.ProjectionError:
            # The failing begin must strand NOTHING (read/project fails before
            # locking -> no lock, no sidecar, no journal): the loop continues.
            failed.append(sid)
            assert not (tmp_path / tx.LOCK_NAME).exists()
            assert not (tmp_path / drv.SIDECAR_NAME).exists()
            assert not (tmp_path / tx.JOURNAL_DIR).exists()
            continue
        res = drv.finish(str(tmp_path), "success", expected_pages=[], title=sid)
        assert res == {"committed": True}
        succeeded.append(sid)

    # Parity summary shape: N total / M succeeded / K failed (failure-continue).
    assert succeeded == ["sid-ok-1", "sid-ok-2"]   # the healthy sids both committed
    assert failed == ["sid-boom"]                  # the failing sid was counted, not fatal
    # No residue after the loop (every terminal path cleaned up).
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


def test_path_b_loop_accumulates_ledger_skipped(tmp_path, monkeypatch):
    """The loop sums each begin's `ledger_skipped` across sids (F6 summary) —
    including all-owned (incremental) sids that dedup-no-op."""
    _init_wiki(tmp_path)
    skip_by_sid = {"s1": 0, "s2": 5, "s3": 7}

    def _project(root, sid, turns, *, ledger):
        return cc_log_project.ProjectionResult(
            markdown="# CC Session transcript\n",
            novel_entries=[],
            ledger_skipped=skip_by_sid[sid],
        )
    monkeypatch.setattr(cc_log_project, "extract_owned",
                        lambda sid, *, ledger: [])
    monkeypatch.setattr(cc_log_project, "project_from_turns", _project)

    total_skipped = 0
    for sid in ["s1", "s2", "s3"]:
        out = drv.begin(str(tmp_path), sid, kind="fe_b_prime")
        total_skipped += out["ledger_skipped"]
        # C1: s2/s3 dedup-no-op (identical markdown -> same raw hash). A dedup
        # begin auto-closes the txn itself (no sidecar), and the Path B loop
        # (wiki-ingest-sessions.md, F2) reports only — it must NOT call finish
        # (doing so raises "nothing to finish"). ledger_skipped is surfaced on
        # the begin regardless, so the per-sid sum is unaffected.
        if not out["auto_closed"]:
            drv.finish(str(tmp_path), "success", expected_pages=[], title=sid)

    assert total_skipped == 12   # 0 + 5 + 7 (surfaced per sid, summed by the loop)


# --------------------------------------------------------------------------- #
# R1 / F-H1: Path B scan-collapse — begin --turns must NOT re-scan the corpus
# --------------------------------------------------------------------------- #
def test_project_batch_then_begin_turns_no_rescan(tmp_path, monkeypatch):
    """The full R1 Path B shape: `project-batch` extracts all sids' turns ONCE,
    then each `begin --turns=<path>` consumes the pre-extracted turns and runs the
    cheap per-sid half WITHOUT re-scanning (must not call `_fetch_turns`)."""
    _init_wiki(tmp_path)
    sids = ["sidA", "sidB"]

    # Fake the ONE batch scan: extract_turns_batch returns per-sid turn dicts.
    def _fake_batch(sid_list, *, ledger):
        return {
            sid: [{
                "role": "user", "uuid": f"u-{sid}", "ts": "t",
                "projected_text": f"hello from {sid}",
                "hash": ledger.compute_hash("user", f"hello from {sid}"),
            }]
            for sid in sid_list
        }
    monkeypatch.setattr(cc_log_project, "extract_turns_batch", _fake_batch)

    # project-batch verb: one scan, writes per-sid turn files, returns the map.
    batch = drv.project_batch(str(tmp_path), sids)
    assert set(batch["turns"].keys()) == set(sids)
    assert batch["scanned"] == 2

    # If begin re-scanned via _fetch_turns, this blows up (F-H1 violated).
    def _boom(sid):
        raise AssertionError("begin --turns re-scanned via _fetch_turns (F-H1!)")
    monkeypatch.setattr(cc_log_project, "_fetch_turns", _boom)

    for sid in sids:
        turns_path = batch["turns"][sid]
        # #1: the real Path B threads project-batch's out_dir into begin
        # (`--out_dir="$OUT_DIR"`) so the code-authored stage1 blob path lands
        # under the batch temp dir (and rides project-batch-cleanup).
        out = drv.begin(str(tmp_path), sid, kind="fe_b_prime", turns=turns_path,
                        out_dir=batch["out_dir"])
        assert out["origin"] == "fe_b_prime"
        # E1/D-1: begin no longer inlines `redacted_body`; it returns
        # `raw_rel_path`, the wiki-relative path of the raw artifact it wrote.
        # Stage1 Reads the body from there — so the parity check reads the raw
        # file off disk instead of the (removed) stdout field.
        assert "redacted_body" not in out
        # #1: begin hands back an ABSOLUTE stage1 blob path under out_dir, keyed
        # by sid — the orchestrator uses it verbatim (never reconstructs it).
        blob = Path(out["stage1_blob_path"])
        assert blob.is_absolute()
        assert blob.parent == Path(batch["out_dir"])
        assert blob.name == f"stage1-{sid}.json"
        raw = (tmp_path / out["raw_rel_path"]).read_text(encoding="utf-8")
        assert f"hello from {sid}" in raw
        drv.finish(str(tmp_path), "success", expected_pages=[], title=sid)

    # cleanup parity: the temp dir is outside the wiki root (loop deletes it).
    assert not Path(batch["out_dir"]).is_relative_to(tmp_path)


def test_begin_turns_sid_mismatch_fails_closed(tmp_path):
    """begin --turns must fail closed when the turn file's sid != source sid."""
    _init_wiki(tmp_path)
    bad = tmp_path.parent / "bad-turns.json"
    bad.write_text(json.dumps({"sid": "WRONG", "turns": []}), encoding="utf-8")
    with pytest.raises(drv.DriverError, match="sid mismatch"):
        drv.begin(str(tmp_path), "actual-sid", kind="fe_b_prime", turns=str(bad))


def test_project_batch_empty_sids_fails_closed(tmp_path):
    _init_wiki(tmp_path)
    with pytest.raises(drv.DriverError, match="at least one sid"):
        drv.project_batch(str(tmp_path), [])


# --------------------------------------------------------------------------- #
# D2/D3: workspace-scope parity — a `session_plan(workspace=True)`-resolved sid
# set (D3's `_sids_workspace` union, no project filter) must run through the
# EXACT SAME per-sid begin->finish loop as any other scope's sid list — the
# scope-tree only widens the SET (D2), it introduces no special-casing in the
# per-sid transaction cycle itself (workspace-session-ingest.md Tests §).
# --------------------------------------------------------------------------- #
def test_workspace_resolved_sids_run_the_same_per_sid_loop(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    # Two DIFFERENT taskflow projects registered in the same workspace — the
    # scenario D3 exists for (sub-projects launched from different dirs).
    for sid, project in (("sid-proj-a", "project-a"), ("sid-proj-b", "project-b")):
        (state_dir / f"{sid}.json").write_text(
            json.dumps({"project": project}), encoding="utf-8")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    plan = drv.session_plan(str(tmp_path), workspace=True)
    assert plan["scope"] == "workspace"
    assert plan["sids"] == ["sid-proj-a", "sid-proj-b"]

    def _extract(sid, *, ledger):
        return []

    def _project(root, sid, turns, *, ledger):
        return cc_log_project.ProjectionResult(
            markdown=f"# CC Session transcript\n\n## Turn 1 [t]\n\n**Human**: {sid}\n",
            novel_entries=[{"hash": f"h-{sid}", "first_sid": sid,
                            "first_uuid": "u", "first_ts": "t"}],
            ledger_skipped=0,
        )
    monkeypatch.setattr(cc_log_project, "extract_owned", _extract)
    monkeypatch.setattr(cc_log_project, "project_from_turns", _project)

    # The SAME per-sid begin->finish cycle every other scope's sid list runs
    # through (no workspace-specific branch anywhere in the transaction path).
    succeeded = []
    for sid in plan["sids"]:
        drv.begin(str(tmp_path), sid, kind="fe_b_prime")
        res = drv.finish(str(tmp_path), "success", expected_pages=[], title=sid)
        assert res == {"committed": True}
        succeeded.append(sid)

    assert succeeded == ["sid-proj-a", "sid-proj-b"]
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


# --------------------------------------------------------------------------- #
# OI-1 S3(c) Path B (pi): project-batch(kind=fe_pi_log) stamps "origin" ->
# begin --turns(kind=fe_pi_log) consumes it -> finish. Synthetic pi-log
# fixture (tmp_path + PI_CODING_AGENT_DIR override), real pi_log_project (no
# monkeypatch) so the round trip exercises the actual extract_turns_batch /
# project_from_turns split, not a stub.
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


def test_project_batch_fe_pi_log_stamps_origin_then_begin_turns_consumes_it(
        tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from llmwiki.ingest import pi_log_project

    agent_dir = tmp_path / "agentdir"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    session_dir = pi_log_project._session_dir() / "--proj--"
    sids = ["p0000000-0000-0000-0000-000000000001",
            "p0000000-0000-0000-0000-000000000002"]
    for i, sid in enumerate(sids):
        _write_pi_session(
            session_dir, sid, f"2026-07-02T09-0{i}-00-000Z",
            [_pi_msg(f"e{i}", None, "user", f"2026-07-02T09:0{i}:00.000Z",
                     f"hello from {sid}")])

    wiki_root = tmp_path / "wiki_root"
    wiki_root.mkdir()
    _init_wiki(wiki_root)

    batch = drv.project_batch(str(wiki_root), sids, kind="fe_pi_log")
    assert batch["scanned"] == 2
    for sid in sids:
        turns_path = batch["turns"][sid]
        stamped = json.loads(Path(turns_path).read_text(encoding="utf-8"))
        assert stamped["origin"] == "fe_pi_log"

        out = drv.begin(str(wiki_root), sid, kind="fe_pi_log", turns=turns_path)
        assert out["origin"] == "fe_pi_log"
        # E1/D-1: raw body is read off disk via `raw_rel_path`, not from the
        # (removed) inline `redacted_body` stdout field.
        assert "redacted_body" not in out
        raw = (wiki_root / out["raw_rel_path"]).read_text(encoding="utf-8")
        assert f"hello from {sid}" in raw
        drv.finish(str(wiki_root), "success", expected_pages=[], title=sid)

    # cleanup parity with the cc Path B loop: the temp dir is outside the wiki
    # root (the loop owns deletion).
    assert not Path(batch["out_dir"]).is_relative_to(wiki_root)
