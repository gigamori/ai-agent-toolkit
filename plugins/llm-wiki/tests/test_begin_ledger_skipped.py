import json

import pytest

from llmwiki.ingest import ingest_driver as drv
from llmwiki.ingest import cc_log_project


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


def _patch_projection(monkeypatch, markdown, novel_entries, ledger_skipped):
    monkeypatch.setattr(cc_log_project, "extract_owned",
                        lambda sid, *, ledger: [])

    def _stub(root, sid, turns, *, ledger):
        return cc_log_project.ProjectionResult(
            markdown=markdown,
            novel_entries=list(novel_entries),
            ledger_skipped=ledger_skipped,
        )
    monkeypatch.setattr(cc_log_project, "project_from_turns", _stub)


def test_fe_b_prime_ledger_skipped_zero_first_ingest(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    _patch_projection(
        monkeypatch,
        "# CC Session transcript\n\n## Turn 1 [t]\n\n**Human**: hi\n",
        [{"hash": "h1", "first_sid": "s", "first_uuid": "u", "first_ts": "t"}],
        0,
    )
    out = drv.begin(str(tmp_path), "somesid.jsonl", kind="fe_b_prime")
    assert out["origin"] == drv.ORIGIN_FE_B_PRIME
    assert out["ledger_skipped"] == 0
    assert "ledger_skipped" in out
    drv.abort(str(tmp_path))


def test_fe_b_prime_ledger_skipped_positive_on_rerun(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    _patch_projection(monkeypatch, "# CC Session transcript\n", [], 62)
    out = drv.begin(str(tmp_path), "somesid.jsonl", kind="fe_b_prime")
    assert out["origin"] == drv.ORIGIN_FE_B_PRIME
    assert out["ledger_skipped"] == 62
    drv.abort(str(tmp_path))


def test_fe_b_ledger_skipped_zero_and_key_present(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("third party content", encoding="utf-8")
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert out["origin"] == drv.ORIGIN_FE_B
    assert "ledger_skipped" in out, (
        "the key is present on every origin, so a loop summing it across sids "
        "never has to distinguish absent from zero"
    )
    assert out["ledger_skipped"] == 0
    drv.abort(str(tmp_path))
