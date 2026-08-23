import io
import json

import pytest

from llmwiki import cli
from llmwiki.write import transaction
from llmwiki.write.write_tool import WriteRejected


def _init_wiki_with_txn(tmp_path):
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text("# SCHEMA\n", encoding="utf-8")
    (tmp_path / "index.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# Log\n", encoding="utf-8")
    (tmp_path / transaction.JOURNAL_DIR).mkdir()
    (tmp_path / ".llmwiki.txn").write_text(
        json.dumps({"max_count": 100, "max_bytes": 10485760}),
        encoding="utf-8")


def _run_ingest_apply(monkeypatch, root, origin, manifest) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(manifest)))
    return cli._ingest_apply([str(root), origin])


def test_fe_pi_log_maps_to_derived_tier(tmp_path, monkeypatch):
    _init_wiki_with_txn(tmp_path)
    with pytest.raises(WriteRejected) as exc:
        _run_ingest_apply(monkeypatch, tmp_path, "fe_pi_log",
                          [{"rel_path": "wiki/evil.md", "content": "x"}])
    assert exc.value.gate == "cross_namespace"
    assert not (tmp_path / "wiki" / "evil.md").exists()


def test_fe_pi_log_derived_target_accepted(tmp_path, monkeypatch):
    _init_wiki_with_txn(tmp_path)
    rc = _run_ingest_apply(monkeypatch, tmp_path, "fe_pi_log",
                           [{"rel_path": "wiki/derived/ok.md", "content": "x"}])
    assert rc == 0
    assert (tmp_path / "wiki" / "derived" / "ok.md").is_file()


def test_fe_b_prime_maps_to_derived_tier(tmp_path, monkeypatch):
    _init_wiki_with_txn(tmp_path)
    with pytest.raises(WriteRejected) as exc:
        _run_ingest_apply(monkeypatch, tmp_path, "fe_b_prime",
                          [{"rel_path": "wiki/evil.md", "content": "x"}])
    assert exc.value.gate == "cross_namespace"


def test_fe_b_maps_to_source_tier(tmp_path, monkeypatch):
    _init_wiki_with_txn(tmp_path)
    rc = _run_ingest_apply(monkeypatch, tmp_path, "fe_b",
                           [{"rel_path": "wiki/from-source.md", "content": "x"}])
    assert rc == 0
    assert (tmp_path / "wiki" / "from-source.md").is_file()
