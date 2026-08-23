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


_SCHEMA_TINY_BYTES = """---
config:
  activation_scope: scoped
  read_grounding:  implicit
  write_mode:      explicit
  write_autocommit: auto
  override_scope:  operation
  apply_fanout_k:  10
  max_count:       100
  max_bytes:       100
---
# SCHEMA
"""


def _init_wiki_tiny_bytes(tmp_path):
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text(_SCHEMA_TINY_BYTES, encoding="utf-8")
    (tmp_path / "index.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# Log\n", encoding="utf-8")


def _manifest_file(tmp_path, name, entries):
    path = tmp_path.parent / name
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def _begin_and_plan(tmp_path, touched, *, k="1"):
    src = tmp_path / "input.txt"
    src.write_text("apply-finish fixture " + ",".join(touched), encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b", apply_fanout_k=k)
    out = drv.plan_fanout(str(tmp_path), json.dumps(touched))
    return out["clusters"]


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
    assert (tmp_path / "wiki" / "a.md").read_text(encoding="utf-8") == "# A"
    assert (tmp_path / "wiki" / "b.md").read_text(encoding="utf-8") == "# B"
    index_text = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "wiki/a.md" in index_text and "wiki/b.md" in index_text
    assert "two clusters" in (tmp_path / "log.md").read_text(encoding="utf-8")
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


def test_apply_finish_manifest_count_mismatch_rolls_back(tmp_path):
    _init_wiki(tmp_path)
    _begin_and_plan(tmp_path, ["wiki/a.md", "wiki/b.md"], k="1")
    m0 = _manifest_file(tmp_path, "only.json",
                        [{"rel_path": "wiki/a.md", "content": "# A"}])

    with pytest.raises(af.ApplyFinishRejected) as ei:
        af.apply_finish(str(tmp_path), "fe_b", [m0])
    assert ei.value.gate == "manifest_count"
    assert not (tmp_path / "wiki" / "a.md").exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


def test_apply_finish_rel_path_not_in_cluster_rolls_back(tmp_path):
    _init_wiki(tmp_path)
    _begin_and_plan(tmp_path, ["wiki/a.md", "wiki/b.md"], k="1")
    m0 = _manifest_file(tmp_path, "p0.json",
                        [{"rel_path": "wiki/a.md", "content": "# A"}])
    m1 = _manifest_file(tmp_path, "p1.json",
                        [{"rel_path": "wiki/c.md", "content": "# C"}])

    with pytest.raises(af.ApplyFinishRejected) as ei:
        af.apply_finish(str(tmp_path), "fe_b", [m0, m1])
    assert ei.value.gate == "cluster_pageset"
    assert not (tmp_path / "wiki" / "a.md").exists()
    assert not (tmp_path / "wiki" / "c.md").exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


def test_apply_finish_second_manifest_reject_rolls_back_first(tmp_path, capsys):
    _init_wiki(tmp_path)
    _begin_and_plan(tmp_path, ["wiki/derived/a.md", "wiki/b.md"], k="1")
    m0 = _manifest_file(tmp_path, "d0.json",
                        [{"rel_path": "wiki/derived/a.md", "content": "# A"}])
    m1 = _manifest_file(tmp_path, "d1.json",
                        [{"rel_path": "wiki/b.md", "content": "# B"}])

    rc = af.run_apply_finish_cli(
        [str(tmp_path), "fe_b_prime", "--manifest", m0, "--manifest", m1])
    assert rc == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"rolled_back": True}
    assert "REJECTED cross_namespace" in captured.err
    assert not (tmp_path / "wiki" / "derived" / "a.md").exists()
    assert not (tmp_path / "wiki" / "b.md").exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


def test_apply_finish_refuses_foreign_lock(tmp_path):
    _init_wiki(tmp_path)
    _begin_and_plan(tmp_path, ["wiki/a.md"], k="1")
    m0 = _manifest_file(tmp_path, "f0.json",
                        [{"rel_path": "wiki/a.md", "content": "# A"}])
    (tmp_path / tx.LOCK_NAME).write_text(
        json.dumps({"pid": os.getpid(), "token": "FOREIGN"}), encoding="utf-8")

    with pytest.raises(drv.DriverError, match="ownership mismatch"):
        af.apply_finish(str(tmp_path), "fe_b", [m0])
    assert (tmp_path / tx.LOCK_NAME).exists()
    assert (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / "wiki" / "a.md").exists()
    (tmp_path / tx.LOCK_NAME).unlink()


def test_apply_finish_cross_cluster_combined_over_budget_rolls_back(tmp_path, capsys):
    _init_wiki_tiny_bytes(tmp_path)
    clusters = _begin_and_plan(
        tmp_path, ["wiki/a.md", "wiki/b.md", "wiki/c.md"], k="1")
    assert clusters == [["wiki/a.md"], ["wiki/b.md"], ["wiki/c.md"]]

    body = "x" * 40
    m0 = _manifest_file(tmp_path, "c0.json",
                        [{"rel_path": "wiki/a.md", "content": body}])
    m1 = _manifest_file(tmp_path, "c1.json",
                        [{"rel_path": "wiki/b.md", "content": body}])
    m2 = _manifest_file(tmp_path, "c2.json",
                        [{"rel_path": "wiki/c.md", "content": body}])

    rc = af.run_apply_finish_cli(
        [str(tmp_path), "fe_b", "--manifest", m0, "--manifest", m1,
         "--manifest", m2])
    assert rc == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"rolled_back": True}
    assert "REJECTED budget" in captured.err
    assert not (tmp_path / "wiki" / "a.md").exists()
    assert not (tmp_path / "wiki" / "b.md").exists()
    assert not (tmp_path / "wiki" / "c.md").exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


def test_apply_finish_cross_cluster_under_combined_budget_succeeds(tmp_path, capsys):
    _init_wiki(tmp_path)
    _begin_and_plan(tmp_path, ["wiki/a.md", "wiki/b.md", "wiki/c.md"], k="1")

    body = "x" * 40
    m0 = _manifest_file(tmp_path, "g0.json",
                        [{"rel_path": "wiki/a.md", "content": body}])
    m1 = _manifest_file(tmp_path, "g1.json",
                        [{"rel_path": "wiki/b.md", "content": body}])
    m2 = _manifest_file(tmp_path, "g2.json",
                        [{"rel_path": "wiki/c.md", "content": body}])

    rc = af.run_apply_finish_cli(
        [str(tmp_path), "fe_b", "--manifest", m0, "--manifest", m1,
         "--manifest", m2])
    assert rc == 0, (
        "the same three manifests commit under the default budget, so the sibling "
        "test fails on the cross-cluster carry and not on a per-cluster limit"
    )
    assert (tmp_path / "wiki" / "a.md").read_text(encoding="utf-8") == body
    assert (tmp_path / "wiki" / "b.md").read_text(encoding="utf-8") == body
    assert (tmp_path / "wiki" / "c.md").read_text(encoding="utf-8") == body
