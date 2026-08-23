import json

import pytest

from llmwiki.ingest import ingest_driver as drv
from llmwiki.ingest import ledger
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


def _begin_with_pending(tmp_path, pending_entries, body="ledger lifecycle"):
    src = tmp_path / "input.txt"
    src.write_text(body, encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    sidecar = tmp_path / drv.SIDECAR_NAME
    state = json.loads(sidecar.read_text(encoding="utf-8"))
    state["pending_ledger_entries"] = [
        {"hash": e.hash, "first_sid": e.first_sid,
         "first_uuid": e.first_uuid, "first_ts": e.first_ts}
        for e in pending_entries
    ]
    sidecar.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")


def test_finish_success_appends_pending_to_ledger(tmp_path):
    _init_wiki(tmp_path)
    entries = [
        ledger.LedgerEntry("h-A", "sidA", "uuidA", "2026-07-02T10:00:00"),
        ledger.LedgerEntry("h-B", "sidA", "uuidB", "2026-07-02T10:00:01"),
    ]
    _begin_with_pending(tmp_path, entries)

    res = drv.finish(str(tmp_path), "success", expected_pages=[], title="lc")
    assert res == {"committed": True}

    on_disk = ledger.read_ledger(tmp_path)
    assert set(on_disk) == {"h-A", "h-B"}
    assert on_disk["h-A"].first_sid == "sidA"
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


def test_finish_success_no_pending_does_not_create_ledger(tmp_path):
    _init_wiki(tmp_path)
    _begin_with_pending(tmp_path, [])
    drv.finish(str(tmp_path), "success", expected_pages=[], title="lc")
    assert not (tmp_path / ledger.LEDGER_NAME).exists()


def test_seen_hash_is_skipped_by_read_seen_hashes(tmp_path):
    _init_wiki(tmp_path)
    entries = [ledger.LedgerEntry("h-seen", "sidA", "uuidA", "t0")]
    _begin_with_pending(tmp_path, entries)
    drv.finish(str(tmp_path), "success", expected_pages=[], title="lc")

    seen = ledger.read_seen_hashes(tmp_path)
    assert "h-seen" in seen
    assert "h-unseen" not in seen


def test_rollback_reverts_append_over_preexisting_ledger(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    owner = ledger.LedgerEntry("h-owner", "sidOwner", "uuidO", "t-owner")
    ledger.append_entries(tmp_path, [owner])
    before = (tmp_path / ledger.LEDGER_NAME).read_bytes()

    _begin_with_pending(
        tmp_path,
        [ledger.LedgerEntry("h-new", "sidNew", "uuidN", "t-new")],
    )
    def _boom(*a, **k):
        raise RuntimeError("simulated commit failure")
    monkeypatch.setattr(drv.transaction, "commit", _boom)

    with pytest.raises(RuntimeError):
        drv.finish(str(tmp_path), "success", expected_pages=[], title="lc")

    after = (tmp_path / ledger.LEDGER_NAME).read_bytes()
    assert after == before
    assert set(ledger.read_ledger(tmp_path)) == {"h-owner"}


def test_rollback_unlinks_freshly_created_ledger(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    assert not (tmp_path / ledger.LEDGER_NAME).exists()

    _begin_with_pending(
        tmp_path,
        [ledger.LedgerEntry("h-new", "sidNew", "uuidN", "t-new")],
    )
    def _boom(*a, **k):
        raise RuntimeError("simulated commit failure")
    monkeypatch.setattr(drv.transaction, "commit", _boom)

    with pytest.raises(RuntimeError):
        drv.finish(str(tmp_path), "success", expected_pages=[], title="lc")

    assert not (tmp_path / ledger.LEDGER_NAME).exists()


def test_finish_fail_does_not_append(tmp_path):
    _init_wiki(tmp_path)
    _begin_with_pending(
        tmp_path,
        [ledger.LedgerEntry("h-x", "sidX", "uuidX", "t")],
    )
    res = drv.finish(str(tmp_path), "fail")
    assert res == {"rolled_back": True}
    assert not (tmp_path / ledger.LEDGER_NAME).exists()


def test_f3_failed_leading_session_lets_following_session_file_prefix(
        tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    shared = ledger.LedgerEntry("h-shared", "sidA", "uuidA", "tA")

    _begin_with_pending(tmp_path, [shared])
    def _boom(*a, **k):
        raise RuntimeError("session A commit failure")
    monkeypatch.setattr(drv.transaction, "commit", _boom)
    with pytest.raises(RuntimeError):
        drv.finish(str(tmp_path), "success", expected_pages=[], title="A")
    monkeypatch.undo()

    assert "h-shared" not in ledger.read_seen_hashes(tmp_path)
    assert not (tmp_path / drv.SIDECAR_NAME).exists()

    seen_before_b = ledger.read_seen_hashes(tmp_path)
    assert "h-shared" not in seen_before_b

    _begin_with_pending(tmp_path, [shared])
    res = drv.finish(str(tmp_path), "success", expected_pages=[], title="B")
    assert res == {"committed": True}

    assert "h-shared" in ledger.read_seen_hashes(tmp_path), (
        "a leading session that failed leaves its prefix unrecorded, so the "
        "following session still sees it as novel and files it"
    )
