"""Tests: turn-ledger driver lifecycle (T4/T9) — append / skip / rollback / F3.

These exercise the LEDGER through the REAL ingest driver transaction (not the
data-layer primitive alone), which is the T9 locked spec:
  - finish(success) journals + APPENDS the sidecar's pending_ledger_entries to
    `.cc-turn-ledger.jsonl` (inside the single transaction);
  - a turn already recorded is SKIPPED by the projector's ledger diff (covered
    where the projector runs; here we assert the read-side seen-set the driver
    consumes: `ledger.read_seen_hashes`);
  - finish(fail) / a raise on the success path ROLLS BACK the append via the
    transaction journal (the ledger is journal-tracked exactly like index/log);
  - F3 absorption: a FAILED leading session leaves the ledger unchanged, so a
    following begin sees the same prefix as novel and files it (sequential
    read-after-write on the ledger).

Setup mirrors test_ingest_driver.py's `_init_wiki`: a plain `.llmwiki` marker
directory (no git — the transaction is a file journal). The projector/DuckDB is
NOT exercised here; `pending_ledger_entries` is injected onto the sidecar that
`begin(kind="fe_b")` writes, which is exactly the on-disk begin->finish channel
the FE-B' projector populates in production.
"""
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
    """Open a real transaction via begin(fe_b), then inject pending ledger
    entries onto the sidecar (the FE-B' projector would populate these)."""
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


# --------------------------------------------------------------------------- #
# finish(success): the pending entries are APPENDED to the ledger (inside the tx)
# --------------------------------------------------------------------------- #
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
    # tx torn down cleanly.
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()


def test_finish_success_no_pending_does_not_create_ledger(tmp_path):
    """Empty pending -> the ledger file is never created (empty-append no-op),
    and it is NOT journaled (LEDGER_NAME only added to journal when pending)."""
    _init_wiki(tmp_path)
    _begin_with_pending(tmp_path, [])
    drv.finish(str(tmp_path), "success", expected_pages=[], title="lc")
    assert not (tmp_path / ledger.LEDGER_NAME).exists()


# --------------------------------------------------------------------------- #
# ledger diff (read side): an already-recorded hash is SKIPPED
# --------------------------------------------------------------------------- #
def test_seen_hash_is_skipped_by_read_seen_hashes(tmp_path):
    """The driver/projector diff consumes ledger.read_seen_hashes; a recorded
    hash is in the seen set (so the projector drops that turn)."""
    _init_wiki(tmp_path)
    entries = [ledger.LedgerEntry("h-seen", "sidA", "uuidA", "t0")]
    _begin_with_pending(tmp_path, entries)
    drv.finish(str(tmp_path), "success", expected_pages=[], title="lc")

    seen = ledger.read_seen_hashes(tmp_path)
    assert "h-seen" in seen        # a following begin would SKIP this turn
    assert "h-unseen" not in seen  # a novel turn is not skipped


# --------------------------------------------------------------------------- #
# rollback reverts the append: a raise on the success path (after append) must
# restore the pre-append ledger bytes exactly (append is journaled, F3 core)
# --------------------------------------------------------------------------- #
def test_rollback_reverts_append_over_preexisting_ledger(tmp_path, monkeypatch):
    """A pre-existing ledger + a failing finish must leave the ledger EXACTLY at
    its pre-append content (the appended entries vanish via the journal `modify`
    backup)."""
    _init_wiki(tmp_path)
    # Pre-existing owner entry (so the ledger file already exists).
    owner = ledger.LedgerEntry("h-owner", "sidOwner", "uuidO", "t-owner")
    ledger.append_entries(tmp_path, [owner])
    before = (tmp_path / ledger.LEDGER_NAME).read_bytes()

    _begin_with_pending(
        tmp_path,
        [ledger.LedgerEntry("h-new", "sidNew", "uuidN", "t-new")],
    )
    # Force a failure on the success path AFTER the append (commit raises), so
    # rollback must undo the just-appended line.
    def _boom(*a, **k):
        raise RuntimeError("simulated commit failure")
    monkeypatch.setattr(drv.transaction, "commit", _boom)

    with pytest.raises(RuntimeError):
        drv.finish(str(tmp_path), "success", expected_pages=[], title="lc")

    after = (tmp_path / ledger.LEDGER_NAME).read_bytes()
    assert after == before                       # append rolled back byte-for-byte
    assert set(ledger.read_ledger(tmp_path)) == {"h-owner"}  # h-new gone


def test_rollback_unlinks_freshly_created_ledger(tmp_path, monkeypatch):
    """No pre-existing ledger + a failing finish must leave NO ledger (the
    journal `create` marker is undone by unlink)."""
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

    assert not (tmp_path / ledger.LEDGER_NAME).exists()   # freshly-created ledger unlinked


def test_finish_fail_does_not_append(tmp_path):
    """finish(fail) rolls back straight away: pending entries are never appended."""
    _init_wiki(tmp_path)
    _begin_with_pending(
        tmp_path,
        [ledger.LedgerEntry("h-x", "sidX", "uuidX", "t")],
    )
    res = drv.finish(str(tmp_path), "fail")
    assert res == {"rolled_back": True}
    assert not (tmp_path / ledger.LEDGER_NAME).exists()


# --------------------------------------------------------------------------- #
# F3 absorption: a FAILED leading session leaves the ledger unchanged, so the
# FOLLOWING begin sees the same prefix as novel and files it (read-after-write)
# --------------------------------------------------------------------------- #
def test_f3_failed_leading_session_lets_following_session_file_prefix(
        tmp_path, monkeypatch):
    """Session A (leading) shares a prefix hash h-shared but FAILS before commit
    -> the ledger never records h-shared. Session B (following) reads the ledger
    AFTER A's failure, still sees h-shared as unseen, and files it (F3)."""
    _init_wiki(tmp_path)
    shared = ledger.LedgerEntry("h-shared", "sidA", "uuidA", "tA")

    # --- Session A: begin, inject the shared prefix, but the finish FAILS. ---
    _begin_with_pending(tmp_path, [shared])
    def _boom(*a, **k):
        raise RuntimeError("session A commit failure")
    monkeypatch.setattr(drv.transaction, "commit", _boom)
    with pytest.raises(RuntimeError):
        drv.finish(str(tmp_path), "success", expected_pages=[], title="A")
    monkeypatch.undo()   # restore the real commit for session B

    # A failed -> the ledger did NOT record h-shared.
    assert "h-shared" not in ledger.read_seen_hashes(tmp_path)
    assert not (tmp_path / drv.SIDECAR_NAME).exists()   # A's tx fully torn down

    # --- Session B: reads the ledger (post-A), still sees h-shared as novel. ---
    seen_before_b = ledger.read_seen_hashes(tmp_path)
    assert "h-shared" not in seen_before_b   # the read-after-write the projector does

    _begin_with_pending(tmp_path, [shared])
    res = drv.finish(str(tmp_path), "success", expected_pages=[], title="B")
    assert res == {"committed": True}

    # B filed the shared prefix (F3: the prefix is NOT lost by A's failure).
    assert "h-shared" in ledger.read_seen_hashes(tmp_path)
