# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Turn-level content-hash ledger — wiki-local state for dedup ownership.

The ledger records the first-ingested session for each turn's content hash,
enabling cross-path (Path A/B) and cross-run (retry/incremental) dedup. It is
the single source of truth for ownership (S8-b / F2-B), independent of
session timestamp (Path B loops in ts-ascending order so first-ingested ≈
earliest, but the ledger doesn't assume it).

Location: <wiki-root>/.cc-turn-ledger.jsonl
Schema (one JSON object per line):
  {
    "hash": "md5(role || 0x1F || text)",
    "first_sid": "session_id that first ingested this turn",
    "first_uuid": "uuid from first-ingested turn",
    "first_ts": "timestamp from first-ingested turn (ISO 8601)"
  }

Contract:
  - hash is computed from the redaction-BEFORE (projected) text (redaction is
    deterministic, so the pre/post hash are consistent per turn)
  - 0x1F (US, Unit Separator) as delimiter; UTF-8 + NFC normalization
    applied by both Python (hashlib) and DuckDB (md5) — verified by tests (F5).
    NOTE: production computes the hash ONLY here (Python `compute_hash`); the
    DuckDB `md5(nfc_normalize(role) || chr(31) || nfc_normalize(text))` byte-match
    is insurance for a hypothetical future SQL-side consumer (guarded by test),
    not currently load-bearing.
  - the ledger is a DRIVER-written state file (like index.md / log.md): the
    ingest driver appends to it and journal-tracks it via the transaction
    journal — it is NOT written through the Stage2 allowlist write tool.

Rollback mechanism (mirrors index.md / log.md — no new rollback path):
  the driver calls ``transaction.journal_before_write(root, [LEDGER_NAME])``
  BEFORE ``append_entries`` mutates the file, exactly as it journals
  ``["index.md", "log.md"]`` before regenerate/append. On rollback the journal
  restores the pre-append backup (existing ledger) or unlinks a freshly-created
  ledger, so the appended entries vanish. This module therefore only exposes
  read / append / hash primitives; it owns NO rollback logic of its own.

Lifecycle (append happens at finish; projection consumes the read at begin):
  - begin  (projector, T2): read the ledger to filter already-seen turns; the
    novel entries are carried to finish on the ``.llmwiki.txn`` sidecar.
  - finish (success): the driver journals LEDGER_NAME then append_entries() the
    novel turns (inside the single transaction).
  - finish (fail) / rollback: the journal reverts the append =>
      ledger unchanged (F3 absorption: the next session's begin sees the same
      prefix as novel and files it — sequential read-after-write).
"""

from __future__ import annotations

import json
import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


LEDGER_NAME = ".cc-turn-ledger.jsonl"

# Delimiter used in hash input: 0x1F is Unit Separator (US) in ASCII.
# UTF-8 encoding of 0x1F is the single byte 0x1F.
HASH_DELIMITER = b'\x1f'


@dataclass(frozen=True)
class LedgerEntry:
    """A single turn recorded in the ledger."""
    hash: str           # md5 of role || 0x1F || text
    first_sid: str      # session_id that first ingested this turn
    first_uuid: str     # uuid from the first-ingested record
    first_ts: str       # timestamp from the first-ingested record (ISO 8601)

    def to_json(self) -> str:
        """Serialize to a JSON line (no trailing newline — caller adds it)."""
        return json.dumps({
            "hash": self.hash,
            "first_sid": self.first_sid,
            "first_uuid": self.first_uuid,
            "first_ts": self.first_ts,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> LedgerEntry:
        """Deserialize from a JSON line."""
        data = json.loads(line)
        return cls(
            hash=data["hash"],
            first_sid=data["first_sid"],
            first_uuid=data["first_uuid"],
            first_ts=data["first_ts"],
        )


def compute_hash(role: str, text: str) -> str:
    """Compute the content hash: md5(role || 0x1F || text).

    Both role and text are UTF-8 encoded. Text is NFC-normalized before hashing
    to ensure determinism (matches DuckDB's normalization).

    Args:
        role: the turn's role (e.g. "user", "assistant")
        text: the (projected) turn text before redaction

    Returns:
        Lowercase hex digest of the MD5 hash
    """
    # NFC normalize both role and text
    norm_role = unicodedata.normalize("NFC", role)
    norm_text = unicodedata.normalize("NFC", text)

    # Encode as UTF-8 and concatenate with 0x1F delimiter
    h = hashlib.md5()
    h.update(norm_role.encode("utf-8"))
    h.update(HASH_DELIMITER)
    h.update(norm_text.encode("utf-8"))

    return h.hexdigest()


def ledger_path(wiki_root: str | Path) -> Path:
    """Return the absolute path to the ledger file."""
    return Path(wiki_root) / LEDGER_NAME


def read_ledger(wiki_root: str | Path) -> dict[str, LedgerEntry]:
    """Read the ledger and return a dict keyed by hash.

    If the ledger does not exist, returns an empty dict (no error).
    Raises json.JSONDecodeError if a line is malformed (ledger corruption).
    """
    path = ledger_path(wiki_root)
    if not path.exists():
        return {}

    entries: dict[str, LedgerEntry] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = LedgerEntry.from_json(line)
        entries[entry.hash] = entry
    return entries


def read_seen_hashes(wiki_root: str | Path) -> set[str]:
    """Return the set of turn-content hashes already recorded in the ledger.

    This is the clean consumption point for the T2 projector: it reads the
    ledger once per session and diffs each projected turn's ``compute_hash``
    against this set to drop already-seen turns. Empty set if no ledger yet.
    """
    return set(read_ledger(wiki_root).keys())


def is_seen(wiki_root: str | Path, hash_val: str) -> bool:
    """Check if a hash is already in the ledger."""
    entries = read_ledger(wiki_root)
    return hash_val in entries


def append_entries(wiki_root: str | Path, entries: list[LedgerEntry]) -> None:
    """Append new entries to the ledger.

    Entries are appended in order. File is created if it doesn't exist.
    This write is expected to be journal-tracked by the caller (ingest_driver).

    Args:
        wiki_root: the wiki root path
        entries: list of LedgerEntry objects to append
    """
    if not entries:
        return

    path = ledger_path(wiki_root)
    lines = [entry.to_json() for entry in entries]
    text = "\n".join(lines) + "\n"

    # Append to existing file or create new one
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def read_ledger_lines(wiki_root: str | Path) -> list[str]:
    """Read the ledger and return raw JSON lines (for rollback).

    Returns a list of JSON line strings (no trailing newlines).
    If the ledger doesn't exist, returns an empty list.
    """
    path = ledger_path(wiki_root)
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def write_ledger_lines(wiki_root: str | Path, lines: list[str]) -> None:
    """Overwrite the ledger with the given lines.

    Args:
        wiki_root: the wiki root path
        lines: list of JSON line strings (no trailing newlines)
    """
    path = ledger_path(wiki_root)
    text = "\n".join(lines)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")
