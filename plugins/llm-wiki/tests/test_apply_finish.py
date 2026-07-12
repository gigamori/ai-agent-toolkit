"""Tests: `apply-finish` compound verb (spec E3 / F1 / F2).

Covers the Step-2 completion criteria:
  - success: all manifests apply in ordinal order -> central finish(success) ->
    the E3/F1 stdout contract {"clusters":[{ordinal,written}],"committed":true},
    pages on disk, index regenerated, sidecar/lock/journal cleared;
  - manifest-count != planned-cluster-count -> fail (rollback) BEFORE applying;
  - a manifest rel_path NOT in its planned cluster (F2 ii) -> fail (rollback);
  - the 2nd manifest REJECTED (D20 cross-namespace) rolls back the 1st manifest's
    already-committed pages too (F1 partial-write semantics);
  - a foreign lock is refused WITHOUT touching the transaction.

The transaction is set up with the CHEAP `begin(kind="fe_b")` + `plan-fanout`
(real sidecar with lock_token + planned_clusters, no projector scan). The
apply-finish `<origin>` arg drives ONLY the WriteSession tier mapping and is
independent of the sidecar origin, so passing `fe_b_prime` exercises the derived
(wiki/derived/-only) gate without a duckdb-backed cc-log fixture.
"""
import json
import os
from pathlib import Path

import pytest

from llmwiki.ingest import ingest_driver as drv
from llmwiki.ingest import apply_finish as af
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


def _manifest_file(tmp_path, name, entries):
    """Write a manifest JSON [{rel_path, content}] OUTSIDE the wiki root."""
    path = tmp_path.parent / name
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def _begin_and_plan(tmp_path, touched, *, k="1"):
    """Cheap FE-B begin + plan-fanout -> a real sidecar with planned_clusters."""
    src = tmp_path / "input.txt"
    src.write_text("apply-finish fixture " + ",".join(touched), encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b", apply_fanout_k=k)
    out = drv.plan_fanout(str(tmp_path), json.dumps(touched))
    return out["clusters"]


# --------------------------------------------------------------------------- #
# success: apply every manifest in ordinal order -> central finish(success)
# --------------------------------------------------------------------------- #
def test_apply_finish_success_contract(tmp_path, capsys):
    _init_wiki(tmp_path)
    clusters = _begin_and_plan(tmp_path, ["wiki/a.md", "wiki/b.md"], k="1")
    assert clusters == [["wiki/a.md"], ["wiki/b.md"]]

    m0 = _manifest_file(tmp_path, "m0.json",
                        [{"rel_path": "wiki/a.md", "content": "# A"}])
    m1 = _manifest_file(tmp_path, "m1.json",
                        [{"rel_path": "wiki/b.md", "content": "# B"}])

    rc = af.run_apply_finish_cli(
        [str(tmp_path), "fe_b", "--manifest", m0, "--manifest", m1,
         "--title=two clusters"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out == {
        "clusters": [
            {"ordinal": 0, "written": ["wiki/a.md"]},
            {"ordinal": 1, "written": ["wiki/b.md"]},
        ],
        "committed": True,
    }
    # Pages on disk; index regenerated to include both; txn fully closed.
    assert (tmp_path / "wiki" / "a.md").read_text(encoding="utf-8") == "# A"
    assert (tmp_path / "wiki" / "b.md").read_text(encoding="utf-8") == "# B"
    index_text = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "wiki/a.md" in index_text and "wiki/b.md" in index_text
    assert "two clusters" in (tmp_path / "log.md").read_text(encoding="utf-8")
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


# --------------------------------------------------------------------------- #
# F2 (i): --manifest count != planned cluster count -> fail (rollback)
# --------------------------------------------------------------------------- #
def test_apply_finish_manifest_count_mismatch_rolls_back(tmp_path):
    _init_wiki(tmp_path)
    _begin_and_plan(tmp_path, ["wiki/a.md", "wiki/b.md"], k="1")  # 2 clusters
    m0 = _manifest_file(tmp_path, "only.json",
                        [{"rel_path": "wiki/a.md", "content": "# A"}])

    with pytest.raises(af.ApplyFinishRejected) as ei:
        af.apply_finish(str(tmp_path), "fe_b", [m0])   # 1 manifest, 2 planned
    assert ei.value.gate == "manifest_count"
    # Fail (rollback) BEFORE applying: nothing written, txn fully closed.
    assert not (tmp_path / "wiki" / "a.md").exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


# --------------------------------------------------------------------------- #
# F2 (ii): a manifest rel_path NOT in its planned cluster -> fail (rollback)
# --------------------------------------------------------------------------- #
def test_apply_finish_rel_path_not_in_cluster_rolls_back(tmp_path):
    _init_wiki(tmp_path)
    _begin_and_plan(tmp_path, ["wiki/a.md", "wiki/b.md"], k="1")
    m0 = _manifest_file(tmp_path, "p0.json",
                        [{"rel_path": "wiki/a.md", "content": "# A"}])
    # manifest[1] targets wiki/c.md, which is NOT in planned cluster 1 (wiki/b.md).
    m1 = _manifest_file(tmp_path, "p1.json",
                        [{"rel_path": "wiki/c.md", "content": "# C"}])

    with pytest.raises(af.ApplyFinishRejected) as ei:
        af.apply_finish(str(tmp_path), "fe_b", [m0, m1])
    assert ei.value.gate == "cluster_pageset"
    # The subset check runs BEFORE any apply -> not even cluster 0 was written.
    assert not (tmp_path / "wiki" / "a.md").exists()
    assert not (tmp_path / "wiki" / "c.md").exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


# --------------------------------------------------------------------------- #
# F1 partial-write: 2nd manifest REJECTED -> 1st manifest's pages rolled back too
# --------------------------------------------------------------------------- #
def test_apply_finish_second_manifest_reject_rolls_back_first(tmp_path, capsys):
    _init_wiki(tmp_path)
    # planned derived path for cluster 0 (allowed) and a wiki/ (non-derived) path
    # for cluster 1 that PASSES the ⊆ check but the derived-origin WriteSession
    # REJECTS (cross_namespace, D20) — the reject fires DURING apply, not F2.
    _begin_and_plan(tmp_path, ["wiki/derived/a.md", "wiki/b.md"], k="1")
    m0 = _manifest_file(tmp_path, "d0.json",
                        [{"rel_path": "wiki/derived/a.md", "content": "# A"}])
    m1 = _manifest_file(tmp_path, "d1.json",
                        [{"rel_path": "wiki/b.md", "content": "# B"}])

    # `fe_b_prime` -> derived tier: cluster 0 (wiki/derived/) commits, then
    # cluster 1 (wiki/b.md, non-derived) is REJECTED cross_namespace.
    rc = af.run_apply_finish_cli(
        [str(tmp_path), "fe_b_prime", "--manifest", m0, "--manifest", m1])
    assert rc == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"rolled_back": True}
    # (begin echoes the resolved-value declaration to stderr first; the REJECTED
    # line is the failure marker of the F1 contract.)
    assert "REJECTED cross_namespace" in captured.err
    # Partial-write rollback: cluster 0's already-committed page is gone too.
    assert not (tmp_path / "wiki" / "derived" / "a.md").exists()
    assert not (tmp_path / "wiki" / "b.md").exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


# --------------------------------------------------------------------------- #
# DEC-R1=D: a foreign lock is refused WITHOUT touching the transaction (no write,
# no rollback of someone else's txn).
# --------------------------------------------------------------------------- #
def test_apply_finish_refuses_foreign_lock(tmp_path):
    _init_wiki(tmp_path)
    _begin_and_plan(tmp_path, ["wiki/a.md"], k="1")
    m0 = _manifest_file(tmp_path, "f0.json",
                        [{"rel_path": "wiki/a.md", "content": "# A"}])
    # Overwrite the lock with a foreign token + a live pid (genuinely held).
    (tmp_path / tx.LOCK_NAME).write_text(
        json.dumps({"pid": os.getpid(), "token": "FOREIGN"}), encoding="utf-8")

    with pytest.raises(drv.DriverError, match="ownership mismatch"):
        af.apply_finish(str(tmp_path), "fe_b", [m0])
    # Refused WITHOUT touching the foreign lock or our sidecar; nothing written.
    assert (tmp_path / tx.LOCK_NAME).exists()
    assert (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / "wiki" / "a.md").exists()
    # Clean up our still-open transaction.
    (tmp_path / tx.LOCK_NAME).unlink()
