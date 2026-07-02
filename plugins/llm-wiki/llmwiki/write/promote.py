# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""promote (D20) — derived -> source page promotion.

Promotion is `wiki/derived/X` -> `wiki/X` via:
  - move (NOT copy — gist single-artifact rule);
  - inbound `[[X]]` link-rewrite handled by code (the link target name is
    unchanged because only the directory moves, so cross-refs by name keep
    resolving — but any path-form references are rewritten);
  - the page's frontmatter `provenance` is flipped derived -> source to mirror
    the new location (D15);
  - reject derived contamination: a derived page that itself INLINE-embeds other
    derived content must not be promoted (a source page may reference derived by
    link only, D20). Detection is structural (an explicit derived-inline marker /
    a transclusion of a wiki/derived/ path), not semantic.

Human approval is required and is enforced by the caller (the command surface);
this module is the deterministic move + rewrite + contamination check.

I/O contract:
    derived_to_source_path(rel_path) -> str
      in : "wiki/derived/X.md"   out: "wiki/X.md"

    detect_contamination(text) -> list[str]
      out: list of contamination reasons (inline transclusion of a wiki/derived/
           path, or an explicit derived-inline marker). Empty -> clean.

    promote(wiki_root, derived_rel) -> PromoteResult
      in : wiki root, a "wiki/derived/X.md" path
      out: PromoteResult { ok, dest_rel, rewritten: [rel_paths], reason }
           - raises PromoteRejected on a non-derived source, missing file, or
             derived contamination.
           - moves the file, flips provenance, rewrites inbound path-references
             in every other page, returns the changed paths.

This module mutates the working tree; the caller runs it inside the file-journal
transaction (transaction.py) so a failed promote rolls back cleanly. The move and
each inbound rewrite are journaled (journal_before_move / journal_before_write)
before mutation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from llmwiki.core import wiki_index


DERIVED_PREFIX = "wiki/derived/"


class PromoteRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class PromoteResult:
    ok: bool
    dest_rel: str
    rewritten: list = field(default_factory=list)
    reason: str = ""


# An explicit inline-transclusion of a derived page (e.g. `![[wiki/derived/x]]`
# or a fenced include referencing a wiki/derived/ path) = contamination.
_INLINE_TRANSCLUDE_RE = re.compile(r"!\[\[[^\]]*\]\]")
_DERIVED_PATH_REF_RE = re.compile(r"wiki/derived/[^\s)\]\"']+")
# Explicit marker a synthesis page may carry when it pastes derived content inline.
_DERIVED_INLINE_MARKER = "<!-- derived-inline -->"


def derived_to_source_path(rel_path: str) -> str:
    norm = rel_path.replace("\\", "/")
    if not norm.startswith(DERIVED_PREFIX):
        raise PromoteRejected(f"not a derived page: {rel_path}")
    return "wiki/" + norm[len(DERIVED_PREFIX):]


def detect_contamination(text: str) -> list[str]:
    reasons: list[str] = []
    if _DERIVED_INLINE_MARKER in text:
        reasons.append("explicit derived-inline marker present")
    # Inline transclusion (![[...]]) of any target = inline embed, not a link.
    for m in _INLINE_TRANSCLUDE_RE.finditer(text):
        reasons.append(f"inline transclusion: {m.group(0)}")
    return reasons


def _flip_provenance(text: str) -> str:
    # Flip a frontmatter `provenance: derived` to `provenance: source` (D15).
    return re.sub(
        r"(?m)^(provenance:\s*)derived\s*$",
        r"\1source",
        text,
        count=1,
    )


def promote(wiki_root: "str | Path", derived_rel: str) -> PromoteResult:
    root = Path(wiki_root)
    norm = derived_rel.replace("\\", "/")
    if not norm.startswith(DERIVED_PREFIX):
        raise PromoteRejected(f"not a derived page: {derived_rel}")
    src = root / Path(norm)
    if not src.is_file():
        raise PromoteRejected(f"page not found: {derived_rel}")

    text = src.read_text(encoding="utf-8")
    contamination = detect_contamination(text)
    if contamination:
        raise PromoteRejected("derived contamination: " + "; ".join(contamination))

    dest_rel = derived_to_source_path(norm)
    dest = root / Path(dest_rel)
    if dest.exists():
        raise PromoteRejected(f"destination already exists: {dest_rel}")

    # Move (not copy) + flip provenance. Journal the move first (no-op outside a
    # transaction) so a failed promote restores src and removes dest on rollback.
    from llmwiki.write import transaction
    transaction.journal_before_move(root, norm, dest_rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_flip_provenance(text), encoding="utf-8")
    src.unlink()

    # Inbound link-rewrite: any other page referencing the OLD path (wiki/derived/X)
    # is rewritten to the new path. Name-form [[X]] links are unaffected (name
    # unchanged), so only path-form references need rewriting.
    rewritten: list[str] = []
    for pe in wiki_index.scan_pages(root):
        if pe.rel_path == dest_rel:
            continue
        p = root / Path(pe.rel_path)
        body = p.read_text(encoding="utf-8")
        if norm in body:
            transaction.journal_before_write(root, [pe.rel_path])
            p.write_text(body.replace(norm, dest_rel), encoding="utf-8")
            rewritten.append(pe.rel_path)

    return PromoteResult(ok=True, dest_rel=dest_rel, rewritten=sorted(rewritten))
