import json
import tempfile
from pathlib import Path

import pytest

from llmwiki.ingest import ledger


def test_compute_hash_basic() -> None:
    h1 = ledger.compute_hash("user", "hello")
    assert isinstance(h1, str)
    assert len(h1) == 32
    assert h1.islower()

    h2 = ledger.compute_hash("user", "hello")
    assert h1 == h2

    h3 = ledger.compute_hash("assistant", "hello")
    assert h1 != h3

    h4 = ledger.compute_hash("user", "world")
    assert h1 != h4


def test_compute_hash_nfc_normalization() -> None:
    precomposed = "café"
    decomposed = "café"

    h1 = ledger.compute_hash("user", precomposed)
    h2 = ledger.compute_hash("user", decomposed)
    assert h1 == h2, "NFC normalization should make é variants hash identically"


def test_compute_hash_delimiter() -> None:
    h1 = ledger.compute_hash("user", "assistant")
    h2 = ledger.compute_hash("userassistant", "")
    assert h1 != h2, "Delimiter should prevent collisions between role and text"


def test_ledger_entry_serialization() -> None:
    entry = ledger.LedgerEntry(
        hash="abc123def456",
        first_sid="sid-12345",
        first_uuid="uuid-67890",
        first_ts="2026-07-02T12:34:56Z"
    )

    json_str = entry.to_json()
    assert isinstance(json_str, str)
    data = json.loads(json_str)
    assert data["hash"] == "abc123def456"
    assert data["first_sid"] == "sid-12345"

    entry2 = ledger.LedgerEntry.from_json(json_str)
    assert entry == entry2


def test_read_ledger_empty(tmp_path: Path) -> None:
    result = ledger.read_ledger(tmp_path)
    assert result == {}


def test_read_ledger_existing(tmp_path: Path) -> None:
    ledger_path = tmp_path / ".cc-turn-ledger.jsonl"
    entries_to_write = [
        ledger.LedgerEntry("hash1", "sid1", "uuid1", "2026-07-02T10:00:00Z"),
        ledger.LedgerEntry("hash2", "sid2", "uuid2", "2026-07-02T11:00:00Z"),
    ]
    lines = [e.to_json() for e in entries_to_write]
    ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = ledger.read_ledger(tmp_path)
    assert len(result) == 2
    assert result["hash1"].first_sid == "sid1"
    assert result["hash2"].first_sid == "sid2"


def test_is_seen(tmp_path: Path) -> None:
    assert not ledger.is_seen(tmp_path, "hash1")

    entry = ledger.LedgerEntry("hash1", "sid1", "uuid1", "2026-07-02T10:00:00Z")
    ledger.append_entries(tmp_path, [entry])

    assert ledger.is_seen(tmp_path, "hash1")
    assert not ledger.is_seen(tmp_path, "hash2")


def test_append_entries(tmp_path: Path) -> None:
    entry1 = ledger.LedgerEntry("hash1", "sid1", "uuid1", "2026-07-02T10:00:00Z")
    entry2 = ledger.LedgerEntry("hash2", "sid2", "uuid2", "2026-07-02T11:00:00Z")

    ledger.append_entries(tmp_path, [entry1])
    result = ledger.read_ledger(tmp_path)
    assert len(result) == 1
    assert "hash1" in result

    ledger.append_entries(tmp_path, [entry2])
    result = ledger.read_ledger(tmp_path)
    assert len(result) == 2
    assert "hash2" in result

    ledger.append_entries(tmp_path, [])
    result = ledger.read_ledger(tmp_path)
    assert len(result) == 2


def test_read_write_ledger_lines(tmp_path: Path) -> None:
    lines = ledger.read_ledger_lines(tmp_path)
    assert lines == []

    entry1 = ledger.LedgerEntry("hash1", "sid1", "uuid1", "2026-07-02T10:00:00Z")
    entry2 = ledger.LedgerEntry("hash2", "sid2", "uuid2", "2026-07-02T11:00:00Z")
    ledger.append_entries(tmp_path, [entry1, entry2])

    lines = ledger.read_ledger_lines(tmp_path)
    assert len(lines) == 2

    ledger.write_ledger_lines(tmp_path, lines[1:])
    result = ledger.read_ledger(tmp_path)
    assert len(result) == 1
    assert "hash2" in result


def test_ledger_path(tmp_path: Path) -> None:
    path = ledger.ledger_path(tmp_path)
    assert path.name == ".cc-turn-ledger.jsonl"
    assert path.parent == tmp_path


def test_append_and_rollback_scenario(tmp_path: Path) -> None:
    entry_a = ledger.LedgerEntry("hash1", "sidA", "uuidA", "2026-07-02T10:00:00Z")
    ledger.append_entries(tmp_path, [entry_a])

    result = ledger.read_ledger(tmp_path)
    assert "hash1" in result

    lines_before = ledger.read_ledger_lines(tmp_path)
    entry_b = ledger.LedgerEntry("hash2", "sidB", "uuidB", "2026-07-02T11:00:00Z")

    result = ledger.read_ledger(tmp_path)
    assert "hash1" in result
    assert "hash2" not in result, (
        "a session that fails before its append leaves the ledger unchanged, so a "
        "later session can still file the same turns"
    )

    entry_c = ledger.LedgerEntry("hash1", "sidC", "uuidC", "2026-07-02T12:00:00Z")
    assert ledger.is_seen(tmp_path, "hash1")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
