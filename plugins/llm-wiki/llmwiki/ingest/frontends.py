# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Normalization front-ends (design §4, D9/D12/D16/D18).

Three normalization entrances feed the single ingest core. Every front-end runs
redaction (D16) BEFORE content-hashing (D18). Each produces a raw artifact with
the provenance/origin/source_ref frontmatter fields the contract requires.

  FE-A  (対話/filing)   -> raw/derived/<hash>.md  provenance:derived origin:conversation
  FE-B  (3rd-party cmd) -> raw/<hash>.<ext>       provenance:source  source_ref{raw_path, external?}
  FE-B' (cc-log jsonl)  -> raw/derived/<hash>.md  provenance:derived origin:cc-log

I/O contract (each front-end):
    fe_a(wiki_root, text, *, external_locator=None) -> FEResult
    fe_b(wiki_root, content, ext, *, external_locator=None) -> FEResult
    fe_b_prime(wiki_root, markdown) -> FEResult      # markdown from cc_log_project

    FEResult {
      hash: str,            # content-hash of the REDACTED content (D18)
      rel_path: str,        # posix relative raw path under wiki root
      exists: bool,         # True -> dedup no-op (D18); caller skips write
      redaction_flags: list,# redaction events for the human gate (D16)
      frontmatter: dict,    # provenance / origin / source_ref / supersedes ...
      body: str,            # the redacted body to be written (when not exists)
    }

The front-ends DO NOT write to disk by themselves and DO NOT commit; the
file-journal transaction (transaction.py) orchestrates lock/checkpoint/write/
finalize. A front-end only computes the artifact (redact -> hash -> dedup-check
-> assemble).
This keeps redaction-before-hash invariant testable in isolation.

source_ref.raw_path is ALWAYS the relative raw path (absolute paths forbidden,
D12). external_locator is optional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from llmwiki.core import content_hash as ch
from llmwiki.ingest import redaction


RAW_DIR = "raw"
RAW_DERIVED_DIR = "raw/derived"


@dataclass
class FEResult:
    hash: str
    rel_path: str
    exists: bool
    redaction_flags: list = field(default_factory=list)
    frontmatter: dict = field(default_factory=dict)
    body: str = ""


def _redact(text: str) -> redaction.RedactionResult:
    return redaction.redact(text)


def fe_a(wiki_root: "str | Path", text: str, *,
         external_locator: "str | None" = None) -> FEResult:
    """FE-A: conversation / filing snapshot -> raw/derived/<hash>.md, derived."""
    red = _redact(text)
    h = ch.content_hash(red.text)
    status = ch.dedup_status(wiki_root, RAW_DERIVED_DIR, h, "md")
    fm = {
        "provenance": "derived",
        "derived_origin": "conversation",
    }
    if external_locator:
        fm["source_ref"] = {"external_locator": external_locator}
    return FEResult(
        hash=h, rel_path=status.rel_path, exists=status.exists,
        redaction_flags=red.flags, frontmatter=fm, body=red.text,
    )


def fe_b(wiki_root: "str | Path", content: str, ext: str, *,
         external_locator: "str | None" = None) -> FEResult:
    """FE-B: 3rd-party source -> raw/<hash>.<ext>, source, source_ref."""
    red = _redact(content)
    h = ch.content_hash(red.text)
    status = ch.dedup_status(wiki_root, RAW_DIR, h, ext)
    source_ref = {"raw_path": status.rel_path}   # ALWAYS relative (D12)
    if external_locator:
        source_ref["external_locator"] = external_locator
    fm = {
        "provenance": "source",
        "source_ref": source_ref,
    }
    return FEResult(
        hash=h, rel_path=status.rel_path, exists=status.exists,
        redaction_flags=red.flags, frontmatter=fm, body=red.text,
    )


def fe_b_prime(wiki_root: "str | Path", markdown: str) -> FEResult:
    """FE-B': cc-log jsonl markdown -> raw/derived/<hash>.md, derived origin:cc-log.

    `markdown` is the output of cc_log_project.project_owned (text + tool_use).
    Redaction (D16) is mandatory here (FE-B' is the lowest-supervision quadrant).
    """
    red = _redact(markdown)
    h = ch.content_hash(red.text)
    status = ch.dedup_status(wiki_root, RAW_DERIVED_DIR, h, "md")
    fm = {
        "provenance": "derived",
        "derived_origin": "cc-log",
        "doc_type": "transcript",   # FE-B' floor: doc_type fixed to transcript
    }
    return FEResult(
        hash=h, rel_path=status.rel_path, exists=status.exists,
        redaction_flags=red.flags, frontmatter=fm, body=red.text,
    )
