"""Tests: begin's stdout-JSON `ledger_skipped` contract (T7/F6, T9 coverage).

The Path B loop (wiki-ingest-project.md) must be able to SUM the per-run
ledger-skipped TURN count across sids so an incremental re-run is not a silent
no-op (RS-d). That count flows projector -> begin's stdout JSON. This asserts the
driver's begin-JSON surface:
  - FE-B' begin surfaces `ledger_skipped` (0 on first ingest, >0 on a re-run);
  - FE-B begin surfaces `ledger_skipped == 0` (no projection) — key ALWAYS present
    for a stable contract;
  - the key is gated to FE-B' exactly like `pending_ledger_entries`.

The FE-B' projector reads the live cc store (DuckDB), which is not hermetic, so
`cc_log_project.project_owned` is monkeypatched to return a controlled
`ProjectionResult` — this test targets the driver's begin-JSON plumbing (the F6
wiring point), not the projector's own counting (covered in
test_cc_log_project.py). frontends.fe_b_prime runs for real over the injected
markdown (unchanged FE-B' contract).
"""
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


def _fake_projection(markdown, novel_entries, ledger_skipped):
    def _stub(root, sid, *, ledger):
        return cc_log_project.ProjectionResult(
            markdown=markdown,
            novel_entries=list(novel_entries),
            ledger_skipped=ledger_skipped,
        )
    return _stub


# --------------------------------------------------------------------------- #
# FE-B': ledger_skipped == 0 on a FIRST ingest (nothing owned yet)
# --------------------------------------------------------------------------- #
def test_fe_b_prime_ledger_skipped_zero_first_ingest(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    monkeypatch.setattr(
        cc_log_project, "project_owned",
        _fake_projection("# CC Session transcript\n\n## Turn 1 [t]\n\n**Human**: hi\n",
                         [{"hash": "h1", "first_sid": "s", "first_uuid": "u", "first_ts": "t"}],
                         0),
    )
    out = drv.begin(str(tmp_path), "somesid.jsonl", kind="fe_b_prime")
    assert out["origin"] == drv.ORIGIN_FE_B_PRIME
    assert out["ledger_skipped"] == 0
    assert "ledger_skipped" in out
    drv.abort(str(tmp_path))


# --------------------------------------------------------------------------- #
# FE-B': ledger_skipped > 0 on a RE-RUN (prior ingest already owns the turns)
# --------------------------------------------------------------------------- #
def test_fe_b_prime_ledger_skipped_positive_on_rerun(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    # Simulate the re-run: the projector dropped 62 already-owned turns.
    monkeypatch.setattr(
        cc_log_project, "project_owned",
        _fake_projection("# CC Session transcript\n", [], 62),
    )
    out = drv.begin(str(tmp_path), "somesid.jsonl", kind="fe_b_prime")
    assert out["origin"] == drv.ORIGIN_FE_B_PRIME
    assert out["ledger_skipped"] == 62      # surfaced from ProjectionResult (F6)
    drv.abort(str(tmp_path))


# --------------------------------------------------------------------------- #
# FE-B: no projection -> ledger_skipped == 0, key ALWAYS present (stable contract)
# --------------------------------------------------------------------------- #
def test_fe_b_ledger_skipped_zero_and_key_present(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("third party content", encoding="utf-8")
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert out["origin"] == drv.ORIGIN_FE_B
    assert "ledger_skipped" in out          # key always present
    assert out["ledger_skipped"] == 0       # gated to 0 for the non-projection origin
    drv.abort(str(tmp_path))
