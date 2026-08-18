# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""source-ref log (D12) — where a raw artifact's ORIGIN is recorded.

`source_ref` answers "where did this raw come from, and how would I re-fetch it"
(D12: `{raw_path: relative (always), external_locator?: url/permalink}`). It is a
DIFFERENT axis from content identity: the content-hash (D18) names the bytes, the
source-ref names the provenance of those bytes.

Location: <wiki-root>/.llmwiki.source-ref.jsonl
Schema (one JSON object per line, append-only):
  {
    "raw_rel_path":     "raw/<hash>.<ext>"  (wiki-relative, ALWAYS — D12),
    "content_hash":     "<sha256 hex>"      (the raw's content-hash id, D18),
    "provenance":       "source" | "derived",
    "derived_origin":   "conversation" | "cc-log" | "pi-log" | "" (source tier),
    "doc_type":         "<resolved doc_type>" | "",
    "external_locator": "<url/permalink>" | ""   (only when `--external` was given),
    "recorded_at":      "YYYY-MM-DD"
  }

`supersedes` is a RESERVED key name (D18 version linking, NOT implemented — see
`content_hash.supersedes_link`, which has no production caller). Do not reuse the
name for anything else.

## Why a root-level log and not the raw's own frontmatter

Three constraints rule out embedding the metadata in the raw artifact:

  1. **Fixed point.** `source_ref.raw_path` IS `raw/<hash>.<ext>` — a function of
     the hash. Feeding a frontmatter block that contains it back into the hash
     input gives `hash = H(fm(hash) + body)`, which does not resolve. Dropping
     `raw_path` does not save it either: `external_locator` would then move the
     hash, making the D18 dedup key a function of the invocation FLAGS rather
     than of the content (ingesting one document with and without `--external`
     would create two raws).
  2. **Non-Markdown raws.** FE-B keeps the source extension and the driver's
     text allowlist admits `.json` / `.jsonl`, so `raw/<hash>.json` is a normal
     path. Prepending YAML would make it unparseable, and branching on extension
     would put the metadata exactly where `external_locator` matters most.
  3. **Cardinality.** One content can legitimately arrive from several locators
     (the same document mirrored at two URLs). Frontmatter is 1:1 with the raw;
     an append-only log is 1:N, which is what "citation form + re-fetch" needs.

Keeping the raw's bytes untouched also preserves `sha256(file) == filename`, so
a raw stays independently verifiable, and it means NO migration: an existing
wiki simply has no line for its older raws ("locator not recorded"), which is
exactly the status quo. A backfill is impossible by construction — the original
locator cannot be recovered from the raw's content.

## Class: driver-written state file (like index.md / log.md / the turn ledger)

The ENGINE writes this file (`ingest_driver.begin`, `cli.py`'s `file` verb); the
Stage2 allowlist write tool cannot reach it (`write_tool.classify_target` admits
only `wiki/` `.md` targets), so this adds no surface to the two code gates (R10).

Rollback mirrors the turn ledger exactly, with no new mechanism: the caller runs
``transaction.journal_before_write(root, [SOURCE_REF_LOG_NAME])`` BEFORE
appending, so a failed `finish` / `abort` restores the pre-append backup (or
unlinks a freshly-created log). A line therefore never outlives the raw it
describes.

## v1 non-goals (deliberate, documented)

  - **Dedup no-op does not append.** When `fe.exists` is true `begin` writes no
    raw and auto-closes the transaction (rollback + release_lock), so a line
    written there would be replayed away. Recording "same content, new locator"
    needs that auto-close to commit instead — the 1:N schema above keeps the
    door open, but v1 does not walk through it.
  - **Projection origins carry no locator.** `fe_b_prime` / `fe_pi_log` take
    `(wiki_root, markdown)` only; a transcript has no external locator. `begin`
    rejects `--external` for those origins outright (it used to be dropped
    silently) rather than threading a value nothing can use.

I/O contract:
    source_ref_log_path(wiki_root) -> Path
    append_entries(wiki_root, entries)          # append-only; [] is a no-op
    read_entries(wiki_root) -> list[SourceRefEntry]   # [] when absent
    today() -> str                              # "YYYY-MM-DD" stamp helper
    is_local_abs_path(value) -> bool            # the D12 no-absolute-path guard
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path


SOURCE_REF_LOG_NAME = ".llmwiki.source-ref.jsonl"

# Reserved for D18 version linking; unused in v1 (see module docstring).
RESERVED_KEYS = ("supersedes",)


class SourceRefRejected(ValueError):
    """An entry violated the D12 no-absolute-path invariant.

    Absolute local paths are secrets (repo policy) and D12 explicitly stores the
    relative `raw_path` so the wiki never carries one. Production callers cannot
    trip this — `raw_rel_path` always comes from `content_hash.dedup_status` and
    `begin` validates `--external` before locking — so it is a programming-error
    guard, deliberately raised rather than silently dropped.
    """


def today() -> str:
    """The `recorded_at` stamp ("YYYY-MM-DD"), matching wiki_log's date form."""
    return _dt.date.today().isoformat()


def is_local_abs_path(value: str) -> bool:
    """True if `value` is an absolute LOCAL path (never allowed in the log).

    A URL locator (`https://…`) is fine — that is the point of the field. What
    must never land here is a local absolute path: a POSIX/UNC leading slash, a
    Windows drive letter, or a `file://` URL (which is an absolute local path
    wearing a scheme).

    DELIBERATELY OVER-REJECTING, do not "fix" it: a one-letter URI scheme
    (`x:foo`) is read as a drive letter. Requiring a separator after the colon
    would clear that false positive, but it would simultaneously admit `C:foo` —
    a Windows DRIVE-RELATIVE local path — which is exactly the leak this guard
    exists to stop, and the two cases are indistinguishable without a scheme
    allowlist. No registered scheme is one character long, so the false positive
    costs nothing while the false negative would cost a recorded local path.
    The same heuristic backs `write_tool._is_absolute`; neither should be
    loosened without the other.
    """
    if not isinstance(value, str) or not value:
        return False
    norm = value.replace("\\", "/")
    if norm.startswith("/"):                       # POSIX absolute + UNC
        return True
    if len(value) >= 2 and value[1] == ":":        # Windows drive letter
        return True
    if norm.lower().startswith("file://"):         # absolute path with a scheme
        return True
    return False


@dataclass(frozen=True)
class SourceRefEntry:
    """One raw-creation event's origin record (see the module docstring schema)."""

    raw_rel_path: str
    content_hash: str
    provenance: str
    derived_origin: str = ""
    doc_type: str = ""
    external_locator: str = ""
    recorded_at: str = ""

    def __post_init__(self) -> None:
        # D12: the raw path is ALWAYS relative, and no field may carry a local
        # absolute path. Checked at construction so a bad value can never be
        # appended (frozen dataclass -> the invariant holds for the object's life).
        if is_local_abs_path(self.raw_rel_path):
            raise SourceRefRejected(
                f"raw_rel_path must be wiki-relative, got {self.raw_rel_path!r}")
        if is_local_abs_path(self.external_locator):
            raise SourceRefRejected(
                f"external_locator must not be a local absolute path, got "
                f"{self.external_locator!r}")

    def to_json(self) -> str:
        """Serialize to a JSON line (no trailing newline — caller adds it)."""
        return json.dumps({
            "raw_rel_path": self.raw_rel_path,
            "content_hash": self.content_hash,
            "provenance": self.provenance,
            "derived_origin": self.derived_origin,
            "doc_type": self.doc_type,
            "external_locator": self.external_locator,
            "recorded_at": self.recorded_at,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "SourceRefEntry":
        """Deserialize one JSON line. Unknown keys are ignored (forward-compat)."""
        data = json.loads(line)
        return cls(
            raw_rel_path=data["raw_rel_path"],
            content_hash=data.get("content_hash", ""),
            provenance=data.get("provenance", ""),
            derived_origin=data.get("derived_origin", ""),
            doc_type=data.get("doc_type", ""),
            external_locator=data.get("external_locator", ""),
            recorded_at=data.get("recorded_at", ""),
        )


def source_ref_log_path(wiki_root: "str | Path") -> Path:
    """The absolute path to the source-ref log under `wiki_root`."""
    return Path(wiki_root) / SOURCE_REF_LOG_NAME


def append_entries(wiki_root: "str | Path", entries: "list[SourceRefEntry]") -> None:
    """Append entries in order; create the file if absent. Empty list = no-op.

    The caller MUST have journaled `SOURCE_REF_LOG_NAME` first (see the module
    docstring) — this function owns no rollback logic, exactly like `ledger`.
    """
    if not entries:
        return
    text = "".join(entry.to_json() + "\n" for entry in entries)
    with open(source_ref_log_path(wiki_root), "a", encoding="utf-8") as f:
        f.write(text)


def read_entries(wiki_root: "str | Path") -> "list[SourceRefEntry]":
    """Every recorded entry in file order; empty list when the log is absent.

    Corruption is surfaced, never hidden — the same posture as
    `ledger.read_ledger`. Three distinct failures reach the caller, not one: a
    malformed line raises `json.JSONDecodeError`; a line missing `raw_rel_path`
    raises `KeyError`; and a line whose path or locator was rewritten into an
    absolute one raises `SourceRefRejected` from `SourceRefEntry.__post_init__`.
    A caller that means to survive a damaged log must handle all three.
    """
    path = source_ref_log_path(wiki_root)
    if not path.is_file():
        return []
    return [SourceRefEntry.from_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
