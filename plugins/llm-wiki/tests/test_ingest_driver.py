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
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "index.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# Log\n", encoding="utf-8")


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
    assert "redacted_body" not in out
    assert out["raw_rel_path"].startswith("raw/")
    assert out["raw_rel_path"].endswith(".txt")
    assert (tmp_path / out["raw_rel_path"]).read_text(
        encoding="utf-8") == "third party content"
    assert isinstance(out["declaration_hash"], str)
    assert len(out["declaration_hash"]) == 12

    (tmp_path / "wiki").mkdir(exist_ok=True)
    (tmp_path / "wiki" / "page.md").write_text("# Page", encoding="utf-8")

    res = drv.finish(str(tmp_path), "success",
                     expected_pages=["wiki/page.md"], title="page")
    assert res == {"committed": True}
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
    assert "wiki/page.md" in (tmp_path / "index.md").read_text(encoding="utf-8")


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
    assert not (tmp_path / raw_rel).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


def test_finish_success_failure_rolls_back(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("regenerate will fail", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b")
    fe_hash = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))["fe_hash"]
    raw_rel = f"raw/{fe_hash}.txt"
    assert (tmp_path / raw_rel).exists()

    (tmp_path / "wiki").mkdir(exist_ok=True)
    tx.journal_before_write(str(tmp_path), ["wiki/page.md"])
    (tmp_path / "wiki" / "page.md").write_text("# Page", encoding="utf-8")

    def _boom(*a, **k):
        raise RuntimeError("simulated regenerate failure")
    monkeypatch.setattr(drv.wiki_index, "regenerate", _boom)

    with pytest.raises(RuntimeError):
        drv.finish(str(tmp_path), "success",
                   expected_pages=["wiki/page.md"], title="page")

    assert not (tmp_path / raw_rel).exists()
    assert not (tmp_path / "wiki" / "page.md").exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()


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
    assert not (tmp_path / raw_rel).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


def test_abort_no_sidecar_is_noop_with_message(tmp_path):
    _init_wiki(tmp_path)
    res = drv.abort(str(tmp_path))
    assert res["aborted"] is False
    assert "message" in res


def test_abort_recovers_crashed_begin_without_sidecar(tmp_path):
    _init_wiki(tmp_path)
    tx.acquire_lock(str(tmp_path))
    tx.checkpoint(str(tmp_path))
    tx.journal_before_write(str(tmp_path), ["raw/deadbeef.txt"])
    (tmp_path / "raw").mkdir(exist_ok=True)
    (tmp_path / "raw" / "deadbeef.txt").write_text("orphan raw", encoding="utf-8")
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert (tmp_path / tx.LOCK_NAME).exists()

    res = drv.abort(str(tmp_path))
    assert res["aborted"] is True
    assert res["recovered_without_sidecar"] is True
    assert not (tmp_path / "raw" / "deadbeef.txt").exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


def test_dedup_noop_path(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("stable third-party content", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b")
    drv.finish(str(tmp_path), "success", title="first")

    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert out["dedup_noop"] is True
    assert out["auto_closed"] is True
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


def test_dedup_begin_auto_closes_without_residue(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("owned third-party content", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b")
    drv.finish(str(tmp_path), "success", title="owned")

    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert out["dedup_noop"] is True
    assert out["auto_closed"] is True
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()

    again = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert again["dedup_noop"] is True
    assert again["auto_closed"] is True
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()


def test_plan_fanout_under_k_one_cluster(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    touched = [f"wiki/p{i}.md" for i in range(7)]
    out = drv.plan_fanout(str(tmp_path), json.dumps({"touched": touched}))
    assert out["clusters"] == [touched]
    drv.abort(str(tmp_path))


def test_plan_fanout_over_k_ceil_split_each_le_k(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    touched = [f"wiki/p{i}.md" for i in range(23)]
    out = drv.plan_fanout(str(tmp_path), json.dumps(touched))
    clusters = out["clusters"]
    assert len(clusters) == 3
    assert all(len(c) <= 10 for c in clusters)
    assert [p for c in clusters for p in c] == touched
    drv.abort(str(tmp_path))


def test_plan_fanout_manifest_paths_inline_json_uses_system_temp(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    touched = [f"wiki/p{i}.md" for i in range(23)]
    out = drv.plan_fanout(str(tmp_path), json.dumps(touched))
    state = drv._read_sidecar(tmp_path)
    fe_hash12 = state["fe_hash"][:12]
    assert len(out["manifest_paths"]) == len(out["clusters"]) == 3
    for i, p in enumerate(out["manifest_paths"]):
        path = Path(p)
        assert path.is_absolute()
        assert path.parent == Path(tempfile.gettempdir())
        assert path.name == f"manifest-{fe_hash12}-{i}.json"
    drv.abort(str(tmp_path))


def test_plan_fanout_manifest_paths_file_input_parented_at_blob_dir(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    touched = [f"wiki/p{i}.md" for i in range(7)]
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
        assert path.parent == blobdir
        assert path.name == f"manifest-{fe_hash12}-{i}.json"
    drv.abort(str(tmp_path))


def test_plan_fanout_manifest_paths_file_under_out_dir_rides_cleanup(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    touched = [f"wiki/p{i}.md" for i in range(23)]
    out_dir = tmp_path / "batchtmp"
    out_dir.mkdir()
    proposal_path = out_dir / "stage1.json"
    proposal_path.write_text(json.dumps(touched), encoding="utf-8")
    out = drv.plan_fanout(str(tmp_path), str(proposal_path))
    assert len(out["manifest_paths"]) == 3
    for p in out["manifest_paths"]:
        assert Path(p).parent == out_dir
    drv.abort(str(tmp_path))


def test_plan_fanout_manifests_inherit_the_per_run_stage1_dir(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    blob = Path(out["stage1_blob_path"])
    blob.write_text(json.dumps({"touched": ["wiki/p0.md"]}), encoding="utf-8")

    planned = drv.plan_fanout(str(tmp_path), str(blob))
    assert planned["manifest_paths"], "expected at least one manifest path"
    for p in planned["manifest_paths"]:
        assert Path(p).parent == blob.parent
        assert Path(p).parent.name.startswith(drv._STAGE1_DIR_PREFIX)
    drv.abort(str(tmp_path))
    shutil.rmtree(blob.parent, ignore_errors=True)


def test_plan_fanout_manifest_paths_empty_touched_aligned(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    out = drv.plan_fanout(str(tmp_path), json.dumps([]))
    assert out["clusters"] == []
    assert out["manifest_paths"] == []
    drv.abort(str(tmp_path))


def test_plan_fanout_requires_sidecar(tmp_path):
    _init_wiki(tmp_path)
    with pytest.raises(drv.DriverError):
        drv.plan_fanout(str(tmp_path), json.dumps(["wiki/a.md"]))


def test_plan_fanout_over_max_count_hits_human_gate(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    touched = [f"wiki/p{i}.md" for i in range(101)]
    with pytest.raises(drv.DriverError) as ei:
        drv.plan_fanout(str(tmp_path), json.dumps(touched))
    assert "budget overflow" in str(ei.value)
    drv.abort(str(tmp_path))


def test_begin_stage1_blob_path_absolute_under_out_dir(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    out_dir = tmp_path / "batchtmp"
    out_dir.mkdir()
    out = drv.begin(str(tmp_path), str(src), kind="fe_b", out_dir=str(out_dir))
    blob = Path(out["stage1_blob_path"])
    assert blob.is_absolute()
    assert blob.parent == out_dir
    assert blob.name == "stage1-input.json"
    drv.abort(str(tmp_path))


def test_begin_stage1_blob_path_defaults_to_a_per_run_temp_dir(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "doc.md"
    src.write_text("y", encoding="utf-8")
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    blob = Path(out["stage1_blob_path"])
    assert blob.is_absolute()
    assert blob.name == "stage1-doc.json"
    assert blob.parent.name.startswith(drv._STAGE1_DIR_PREFIX)
    assert blob.parent.parent == Path(tempfile.gettempdir())
    assert blob.parent.is_dir()
    drv.abort(str(tmp_path))
    shutil.rmtree(blob.parent, ignore_errors=True)


def test_begin_stage1_blob_dir_is_unique_per_run(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "same-source.md"
    src.write_text("first", encoding="utf-8")

    first = drv.begin(str(tmp_path), str(src), kind="fe_b")
    blob1 = Path(first["stage1_blob_path"])
    drv.abort(str(tmp_path))

    src.write_text("second", encoding="utf-8")
    second = drv.begin(str(tmp_path), str(src), kind="fe_b")
    blob2 = Path(second["stage1_blob_path"])
    drv.abort(str(tmp_path))

    assert blob1.name == blob2.name
    assert blob1 != blob2
    assert blob1.parent != blob2.parent, (
        "two begins on the same source share a blob FILENAME by design, so only the "
        "per-run parent dir keeps concurrent runs from overwriting each other"
    )
    for d in (blob1.parent, blob2.parent):
        shutil.rmtree(d, ignore_errors=True)


def test_begin_dedup_noop_creates_no_stage1_dir(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "dup.md"
    src.write_text("identical body", encoding="utf-8")

    first = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert first["dedup_noop"] is False
    drv.finish(str(tmp_path), "success", expected_pages=[], title="dup")
    shutil.rmtree(Path(first["stage1_blob_path"]).parent, ignore_errors=True)

    before = set(Path(tempfile.gettempdir()).glob(f"{drv._STAGE1_DIR_PREFIX}*"))
    second = drv.begin(str(tmp_path), str(src), kind="fe_b")
    after = set(Path(tempfile.gettempdir()).glob(f"{drv._STAGE1_DIR_PREFIX}*"))

    assert second["dedup_noop"] is True
    assert second["auto_closed"] is True
    assert after == before, "a dedup no-op begin created a stage1 temp dir"


def test_begin_with_out_dir_creates_no_extra_stage1_dir(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "b.md"
    src.write_text("z", encoding="utf-8")
    out_dir = tmp_path / "batchtmp"
    out_dir.mkdir()

    before = set(Path(tempfile.gettempdir()).glob(f"{drv._STAGE1_DIR_PREFIX}*"))
    out = drv.begin(str(tmp_path), str(src), kind="fe_b", out_dir=str(out_dir))
    after = set(Path(tempfile.gettempdir()).glob(f"{drv._STAGE1_DIR_PREFIX}*"))

    assert Path(out["stage1_blob_path"]).parent == out_dir
    assert after == before, "begin created a stage1 temp dir despite --out_dir"
    drv.abort(str(tmp_path))


def _begin_fe_b_prime(tmp_path, monkeypatch, sid="sid-x"):
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
    with pytest.raises(drv.DriverError, match="tier mismatch"):
        drv.plan_fanout(str(tmp_path), json.dumps(["wiki/db-spec/foo.md"]))
    sidecar = json.loads(
        (tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))
    assert "planned_clusters" not in sidecar
    drv.abort(str(tmp_path))


def test_plan_fanout_derived_origin_accepts_derived_tier(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    _begin_fe_b_prime(tmp_path, monkeypatch)
    touched = ["wiki/derived/db-spec/foo.md", "wiki/derived/db-spec/bar.md"]
    out = drv.plan_fanout(str(tmp_path), json.dumps(touched))
    assert out["clusters"] == [touched]
    drv.abort(str(tmp_path))


def test_begin_binary_source_is_clean_driver_error(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "image.bin"
    src.write_bytes(b"\xff\xfe\x00\x01\x02\x80\x81")
    with pytest.raises(drv.DriverError):
        drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


def test_begin_jsonl_auto_kind_refused_fail_closed(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "some-sid.jsonl"
    src.write_text('{"turns": []}\n', encoding="utf-8")
    with pytest.raises(drv.DriverError) as exc_info:
        drv.begin(str(tmp_path), str(src))
    msg = str(exc_info.value)
    assert "--kind=fe_b_prime" in msg
    assert "--kind=fe_pi_log" in msg
    assert "--kind=fe_b" in msg
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
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("plain text source", encoding="utf-8")
    out = drv.begin(str(tmp_path), str(src))
    assert out["origin"] == drv.ORIGIN_FE_B
    drv.abort(str(tmp_path))


def test_consistency_violation_aborts_begin_before_locking(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("never ingested", encoding="utf-8")

    with pytest.raises(config_resolver.ConfigInconsistency):
        drv.begin(str(tmp_path), str(src), kind="fe_b", apply_fanout_k="200")

    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
    assert not (tmp_path / "raw").exists()


def test_sidecar_schema_keys(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("schema check", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    state = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))
    assert set(state) == {
        "journal_dir", "origin", "doc_type",
        "max_count", "max_bytes", "apply_fanout_k", "fe_hash", "pid",
        "lock_token", "pending_ledger_entries",
    }
    assert state["pending_ledger_entries"] == []
    drv.abort(str(tmp_path))


def test_finish_refuses_on_lock_ownership_mismatch(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("owned by A", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")

    (tmp_path / tx.LOCK_NAME).write_text(
        json.dumps({"pid": os.getpid(), "token": "FOREIGN"}), encoding="utf-8")

    with pytest.raises(drv.DriverError) as ei:
        drv.finish(str(tmp_path), "success", expected_pages=[], title="x")
    assert "ownership mismatch" in str(ei.value)
    assert (tmp_path / tx.LOCK_NAME).exists()
    assert (tmp_path / drv.SIDECAR_NAME).exists()


def test_abort_refuses_on_lock_ownership_mismatch(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("owned by A", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    (tmp_path / tx.LOCK_NAME).write_text(
        json.dumps({"pid": os.getpid(), "token": "FOREIGN"}), encoding="utf-8")

    with pytest.raises(drv.DriverError) as ei:
        drv.abort(str(tmp_path))
    assert "ownership mismatch" in str(ei.value)
    assert (tmp_path / tx.LOCK_NAME).exists()
    assert (tmp_path / drv.SIDECAR_NAME).exists()


def test_resolve_kind_fe_pi_log():
    assert drv._resolve_kind("fe_pi_log") == drv.ORIGIN_FE_PI_LOG


def test_resolve_kind_unknown_lists_fe_pi_log_in_error():
    with pytest.raises(drv.DriverError) as ei:
        drv._resolve_kind("bogus")
    assert "fe_pi_log" in str(ei.value)


def test_resolve_projection_kind_auto_defaults_to_fe_b_prime():
    assert drv._resolve_projection_kind("auto") == drv.ORIGIN_FE_B_PRIME


def test_resolve_projection_kind_fe_b_defaults_to_fe_b_prime():
    assert drv._resolve_projection_kind("fe_b") == drv.ORIGIN_FE_B_PRIME


def test_resolve_projection_kind_fe_b_prime_stays_fe_b_prime():
    assert drv._resolve_projection_kind("fe_b_prime") == drv.ORIGIN_FE_B_PRIME


def test_resolve_projection_kind_fe_pi_log_stays_fe_pi_log():
    assert drv._resolve_projection_kind("fe_pi_log") == drv.ORIGIN_FE_PI_LOG


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
    assert "file|pi-log" in log_text
    assert not (wiki_root / drv.SIDECAR_NAME).exists()
    assert not (wiki_root / tx.LOCK_NAME).exists()


def test_begin_turns_origin_mismatch_fails_closed(tmp_path):
    _init_wiki(tmp_path)
    bad = tmp_path.parent / "mismatch-turns.json"
    bad.write_text(
        json.dumps({"sid": "some-sid", "origin": "fe_pi_log", "turns": []}),
        encoding="utf-8")
    with pytest.raises(drv.DriverError, match="origin mismatch"):
        drv.begin(str(tmp_path), "some-sid", kind="fe_b_prime", turns=str(bad))


def test_begin_turns_origin_mismatch_fails_closed_reverse(tmp_path):
    _init_wiki(tmp_path)
    bad = tmp_path.parent / "mismatch-turns-2.json"
    bad.write_text(
        json.dumps({"sid": "some-sid", "origin": "fe_b_prime", "turns": []}),
        encoding="utf-8")
    with pytest.raises(drv.DriverError, match="origin mismatch"):
        drv.begin(str(tmp_path), "some-sid", kind="fe_pi_log", turns=str(bad))


def test_begin_turns_origin_absent_treated_as_fe_b_prime(tmp_path, monkeypatch):
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
    _init_wiki(tmp_path)
    ok = tmp_path.parent / "no-origin-turns-2.json"
    ok.write_text(
        json.dumps({"sid": "some-sid", "turns": []}), encoding="utf-8")
    with pytest.raises(drv.DriverError, match="origin mismatch"):
        drv.begin(str(tmp_path), "some-sid", kind="fe_pi_log", turns=str(ok))
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


def test_begin_ledger_diff_runs_inside_lock(tmp_path, monkeypatch):
    from llmwiki.ingest import ledger as ld

    _init_wiki(tmp_path)
    h1 = ld.compute_hash("user", "hello")
    h2 = ld.compute_hash("assistant", "world")
    turns = [
        {"role": "user", "uuid": "u1", "ts": "2026-07-07T00:00:00",
         "projected_text": "hello", "hash": h1},
        {"role": "assistant", "uuid": "u2", "ts": "2026-07-07T00:00:01",
         "projected_text": "world", "hash": h2},
    ]
    tf = tmp_path.parent / "race-turns.json"
    tf.write_text(json.dumps({"sid": "sid-a", "origin": drv.ORIGIN_FE_B_PRIME,
                              "turns": turns}), encoding="utf-8")

    real_acquire = tx.acquire_lock

    def racing_acquire(root):
        handle = real_acquire(root)
        ld.append_entries(tmp_path, [ld.LedgerEntry(
            hash=h1, first_sid="other-sid", first_uuid="ux",
            first_ts="2026-07-06T23:59:59")])
        return handle

    monkeypatch.setattr(drv.transaction, "acquire_lock", racing_acquire)

    out = drv.begin(str(tmp_path), "sid-a", kind="fe_b_prime", turns=str(tf))
    assert out["ledger_skipped"] == 1, (
        "the ledger diff runs inside the lock, so an append landing at "
        "lock-acquisition time is still seen and its turn is skipped, not re-filed"
    )
    sidecar = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(
        encoding="utf-8"))
    pending_hashes = [e["hash"] for e in sidecar["pending_ledger_entries"]]
    assert pending_hashes == [h2]
    drv.finish(str(tmp_path), "fail")
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


def _t(role, text, uuid="u"):
    from llmwiki.ingest import ledger as ld
    return {"role": role, "uuid": uuid, "ts": "t",
            "projected_text": text, "hash": ld.compute_hash(role, text)}


def test_cutoff_last_user_drops_invocation_turn_and_everything_after():
    turns = [
        _t("user", "real question", "u1"),
        _t("assistant", "real answer", "a1"),
        _t("user", "/wiki-file", "u2"),
        _t("assistant", "active wiki: /w", "a2"),
    ]
    kept = drv._apply_cutoff(turns, drv._CUTOFF_LAST_USER)
    assert [t["uuid"] for t in kept] == ["u1", "a1"]


def test_cutoff_none_is_the_default_and_keeps_everything():
    turns = [_t("user", "q", "u1"), _t("assistant", "a", "a1"),
             _t("user", "later", "u2")]
    assert drv._apply_cutoff(turns, drv._CUTOFF_NONE) == turns


def test_cutoff_anchors_on_an_empty_user_turn_too():
    turns = [
        _t("user", "real question", "u1"),
        _t("assistant", "real answer", "a1"),
        _t("user", "", "u2"),
    ]
    kept = drv._apply_cutoff(turns, drv._CUTOFF_LAST_USER)
    assert [t["uuid"] for t in kept] == ["u1", "a1"]


def test_cutoff_survives_the_d7_invocation_strip(tmp_path):
    def _projected(role, raw_text):
        t = cc_log_project._Turn(role=role, uuid=f"u-{role}", ts="t")
        t.text_parts = [raw_text]
        return cc_log_project._turn_to_dict(t, ledger=ld_mod())

    def ld_mod():
        from llmwiki.ingest import ledger as _l
        return _l

    for invocation in ("/wiki-file", "/wiki-file just the last answer",
                       "/llm-wiki:wiki-file the retry-policy part"):
        turns = [
            _projected("user", "real question"),
            _projected("assistant", "real answer"),
            _projected("user", invocation),
        ]
        assert turns[-1]["projected_text"] == "", (
            f"{invocation!r}: extraction is expected to strip the invocation line")
        kept = drv._apply_cutoff(turns, drv._CUTOFF_LAST_USER)
        assert [t["projected_text"] for t in kept] == [
            "real question", "real answer"], (
            f"{invocation!r}: the cutoff must keep the conversation and drop "
            f"only the invocation turn")


def test_cutoff_with_no_user_turn_drops_nothing():
    turns = [_t("assistant", "one", "a1"), _t("assistant", "two", "a2")]
    assert drv._apply_cutoff(turns, drv._CUTOFF_LAST_USER) == turns


def test_begin_fails_closed_when_the_cc_sid_has_no_session_log(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    empty_corpus = tmp_path.parent / "cc-corpus-without-the-sid"
    empty_corpus.mkdir()
    monkeypatch.setattr(cc_log_project.cc_paths, "cc_projects_roots",
                        lambda: [empty_corpus])
    with pytest.raises(cc_log_project.ProjectionError,
                       match="cc session file not found"):
        drv.begin(str(tmp_path), "no-such-sid", kind="fe_b_prime")
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert list((tmp_path / "raw" / "derived").glob("*.md")) == []


def test_begin_turns_channel_does_not_require_a_session_log(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    empty_corpus = tmp_path.parent / "cc-corpus-for-the-turns-channel"
    empty_corpus.mkdir()
    monkeypatch.setattr(cc_log_project.cc_paths, "cc_projects_roots",
                        lambda: [empty_corpus])
    tf = _write_turns(tmp_path, [_t("user", "keep me", "u1")])
    monkeypatch.setattr(
        cc_log_project, "project_from_turns",
        lambda root, sid, turn_list, *, ledger:
            cc_log_project.ProjectionResult(markdown="# CC Session transcript\n"))
    out = drv.begin(str(tmp_path), "sid-a", kind="fe_b_prime", turns=str(tf))
    assert out["origin"] == drv.ORIGIN_FE_B_PRIME
    drv.abort(str(tmp_path))


def test_begin_rejects_unknown_cutoff_value(tmp_path):
    _init_wiki(tmp_path)
    with pytest.raises(drv.DriverUsageError):
        drv.begin(str(tmp_path), "sid-a", kind="fe_b_prime", cutoff="bogus")


def test_begin_rejects_cutoff_on_fe_b(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "doc.txt"
    src.write_text("a document, not a transcript", encoding="utf-8")
    with pytest.raises(drv.DriverUsageError, match="not applicable"):
        drv.begin(str(tmp_path), str(src), kind="fe_b",
                  cutoff=drv._CUTOFF_LAST_USER)
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()


def test_begin_applies_cutoff_on_the_path_a_channel(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    extracted = [
        _t("user", "real question", "u1"),
        _t("assistant", "real answer", "a1"),
        _t("user", "/wiki-file", "u2"),
    ]
    monkeypatch.setattr(cc_log_project, "extract_owned",
                        lambda sid, *, ledger: list(extracted))
    seen = {}

    def _capture(root, sid, turn_list, *, ledger):
        seen["turns"] = turn_list
        return cc_log_project.ProjectionResult(markdown="# CC Session transcript\n")
    monkeypatch.setattr(cc_log_project, "project_from_turns", _capture)

    out = drv.begin(str(tmp_path), "sid-a", kind="fe_b_prime",
                    cutoff=drv._CUTOFF_LAST_USER)
    assert [t["uuid"] for t in seen["turns"]] == ["u1", "a1"]
    assert out["cutoff_dropped"] == 1
    drv.abort(str(tmp_path))


def test_begin_reports_zero_cutoff_dropped_without_the_flag(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    extracted = [_t("user", "real question", "u1"),
                 _t("user", "/wiki-file", "u2")]
    monkeypatch.setattr(cc_log_project, "extract_owned",
                        lambda sid, *, ledger: list(extracted))
    monkeypatch.setattr(
        cc_log_project, "project_from_turns",
        lambda root, sid, turn_list, *, ledger:
            cc_log_project.ProjectionResult(markdown="# CC Session transcript\n"))
    out = drv.begin(str(tmp_path), "sid-a", kind="fe_b_prime")
    assert out["cutoff_dropped"] == 0
    drv.abort(str(tmp_path))


def _write_turns(tmp_path, turns, sid="sid-a"):
    tf = tmp_path.parent / "narrowed-turns.json"
    tf.write_text(json.dumps({"sid": sid, "origin": drv.ORIGIN_FE_B_PRIME,
                              "turns": turns}), encoding="utf-8")
    return tf


def test_turns_hash_verify_accepts_a_pure_subset(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    full = [_t("user", "keep me", "u1"), _t("assistant", "drop me", "a1")]
    tf = _write_turns(tmp_path, [full[0]])
    seen = {}

    def _capture(root, sid, turn_list, *, ledger):
        seen["turns"] = turn_list
        return cc_log_project.ProjectionResult(markdown="# CC Session transcript\n")
    monkeypatch.setattr(cc_log_project, "project_from_turns", _capture)

    drv.begin(str(tmp_path), "sid-a", kind="fe_b_prime", turns=str(tf))
    assert [t["uuid"] for t in seen["turns"]] == ["u1"]
    drv.abort(str(tmp_path))


def test_turns_hash_verify_rejects_edited_text(tmp_path):
    _init_wiki(tmp_path)
    tampered = _t("user", "original", "u1")
    tampered["projected_text"] = "rewritten by the LLM"
    tf = _write_turns(tmp_path, [tampered])
    with pytest.raises(drv.DriverUsageError, match="hash check"):
        drv.begin(str(tmp_path), "sid-a", kind="fe_b_prime", turns=str(tf))
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()


def test_turns_hash_verify_rejects_a_fabricated_entry(tmp_path):
    _init_wiki(tmp_path)
    fabricated = {"role": "user", "uuid": "u9", "ts": "t",
                  "projected_text": "content the projector never produced",
                  "hash": "0" * 32}
    tf = _write_turns(tmp_path, [fabricated])
    with pytest.raises(drv.DriverUsageError, match="hash check"):
        drv.begin(str(tmp_path), "sid-a", kind="fe_b_prime", turns=str(tf))


def test_turns_hash_verify_rejects_a_non_object_entry(tmp_path):
    _init_wiki(tmp_path)
    tf = _write_turns(tmp_path, ["not an object"])
    with pytest.raises(drv.DriverUsageError, match="not an object"):
        drv.begin(str(tmp_path), "sid-a", kind="fe_b_prime", turns=str(tf))


def test_project_batch_cleanup_refuses_wrong_prefix():
    d = Path(tempfile.mkdtemp(prefix="notmine-"))
    try:
        with pytest.raises(drv.DriverError, match="REFUSED"):
            drv.project_batch_cleanup(str(d))
        assert d.is_dir()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_project_batch_cleanup_refuses_outside_temp(tmp_path):
    d = tmp_path / "sub" / f"{drv._BATCH_TURNS_PREFIX}fake"
    d.mkdir(parents=True)
    with pytest.raises(drv.DriverError, match="REFUSED"):
        drv.project_batch_cleanup(str(d))
    assert d.is_dir()


def test_project_batch_cleanup_refuses_a_stage1_dir():
    d = Path(tempfile.mkdtemp(prefix=drv._STAGE1_DIR_PREFIX))
    try:
        with pytest.raises(drv.DriverError, match="REFUSED"):
            drv.project_batch_cleanup(str(d))
        assert d.is_dir()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_prune_covers_stage1_dirs_too(tmp_path):
    stale = Path(tempfile.mkdtemp(prefix=drv._STAGE1_DIR_PREFIX))
    fresh = Path(tempfile.mkdtemp(prefix=drv._STAGE1_DIR_PREFIX))
    old = time.time() - (drv._BATCH_STALE_PRUNE_SECONDS + 3600)
    os.utime(stale, (old, old))
    try:
        drv._prune_stale_batch_dirs()
        assert not stale.exists()
        assert fresh.exists()
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
        shutil.rmtree(stale, ignore_errors=True)


def test_project_batch_cleanup_deletes_valid_dir():
    d = Path(tempfile.mkdtemp(prefix=drv._BATCH_TURNS_PREFIX))
    (d / "sid.json").write_text("{}", encoding="utf-8")
    assert d.is_dir()
    res = drv.project_batch_cleanup(str(d))
    assert Path(res["cleaned"]) == d
    assert not d.exists()


def test_project_batch_prunes_stale_turn_dirs(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    stale = Path(tempfile.mkdtemp(prefix=drv._BATCH_TURNS_PREFIX))
    fresh = Path(tempfile.mkdtemp(prefix=drv._BATCH_TURNS_PREFIX))
    old = time.time() - (drv._BATCH_STALE_PRUNE_SECONDS + 3600)
    os.utime(stale, (old, old))

    monkeypatch.setattr(cc_log_project, "extract_turns_batch",
                        lambda sids, *, ledger: {s: [] for s in sids})

    out = None
    try:
        out = drv.project_batch(str(tmp_path), ["sidX"])
        assert not stale.exists()
        assert fresh.exists()
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
        if out is not None:
            shutil.rmtree(out["out_dir"], ignore_errors=True)
        shutil.rmtree(stale, ignore_errors=True)


def _apply_cluster(monkeypatch, root, origin, ordinal, manifest) -> int:
    import io
    from llmwiki import cli
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(manifest)))
    return cli._ingest_apply([str(root), origin, str(ordinal)])


def test_c2_cluster_drop_finish_rolls_back(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("c2 cluster drop", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b", apply_fanout_k="1")
    out = drv.plan_fanout(str(tmp_path), json.dumps(["wiki/a.md", "wiki/b.md"]))
    assert len(out["clusters"]) == 2
    sidecar = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))
    assert sidecar["planned_clusters"] == [["wiki/a.md"], ["wiki/b.md"]]

    rc = _apply_cluster(monkeypatch, tmp_path, "fe_b", 0,
                        [{"rel_path": "wiki/a.md", "content": "# A"}])
    assert rc == 0

    with pytest.raises(drv.DriverError, match="never dispatched"):
        drv.finish(str(tmp_path), "success")
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
    assert not (tmp_path / "wiki" / "a.md").exists()


def test_c2_empty_manifest_cluster_is_not_false_positive(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("c2 empty manifest", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b", apply_fanout_k="1")
    out = drv.plan_fanout(str(tmp_path), json.dumps(["wiki/a.md"]))
    assert len(out["clusters"]) == 1

    rc = _apply_cluster(monkeypatch, tmp_path, "fe_b", 0, [])
    assert rc == 0
    sidecar = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))
    assert sidecar["applied_clusters"] == [0]
    assert sidecar["applied_written"] == [], (
        "a cluster that legitimately wrote nothing still recorded its receipt, so "
        "the dispatch check does not read it as a dropped cluster"
    )

    res = drv.finish(str(tmp_path), "success")
    assert res == {"committed": True}
    assert not (tmp_path / drv.SIDECAR_NAME).exists()


def test_c2_explicit_expected_pages_backward_compat(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("c2 explicit expected", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b", apply_fanout_k="1")
    drv.plan_fanout(str(tmp_path), json.dumps(["wiki/a.md", "wiki/b.md"]))
    (tmp_path / "wiki").mkdir(exist_ok=True)
    (tmp_path / "wiki" / "page.md").write_text("# Page", encoding="utf-8")

    res = drv.finish(str(tmp_path), "success", expected_pages=["wiki/page.md"])
    assert res == {"committed": True}


def test_c2_single_cluster_unapplied_rolls_back(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("c2 single cluster drop", encoding="utf-8")

    drv.begin(str(tmp_path), str(src), kind="fe_b")
    out = drv.plan_fanout(str(tmp_path), json.dumps(["wiki/a.md"]))
    assert len(out["clusters"]) == 1
    with pytest.raises(drv.DriverError, match="never dispatched"):
        drv.finish(str(tmp_path), "success")
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
