# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""allowlist write tool (D19/D20) — the Stage2 write gate.

All Stage2 LLM page writes go ONLY through this tool. It is one of the two
non-negotiable code gates (R10). It enforces:

  - write targets limited to `wiki/` and `wiki/derived/` (D19);
  - reject `SCHEMA.md`, `.llmwiki`, `raw/` as targets (D19);
  - reject absolute paths and `..` traversal (D19);
  - budget (count / total size) overflow -> human gate (D19);
  - derived-origin edits land ONLY in `wiki/derived/` (D20 enforce).

I/O contract:
    classify_target(rel_path) -> TargetCheck
      in : a write target path relative to wiki root
      out: TargetCheck { ok, reason } — ok iff inside wiki/ or wiki/derived/ and
           not a rejected file, not absolute, no traversal.

    WriteSession(wiki_root, *, max_count, max_bytes, origin)
      A budget-bounded batch of proposed writes (origin in {source, derived}).
      .add(rel_path, content) -> WriteOp
        raises WriteRejected on a disallowed target, a D20 cross-namespace
        violation, or budget overflow (overflow -> human gate signal).
      .commit() -> list[str]
        writes every staged op to disk (caller runs this inside the git
        transaction); returns the written rel_paths.

WriteRejected carries .reason and .gate ("path"|"traversal"|"absolute"|
"protected"|"budget"|"cross_namespace"); "budget" means escalate to the human
gate rather than a hard error.

Path checks are performed on the STRING (no filesystem touch) so traversal is
caught before any IO and cannot be normalized away by a symlink.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath, Path


WIKI_DIR = "wiki"
DERIVED_PREFIX = "wiki/derived/"
PROTECTED_NAMES = {"SCHEMA.md", ".llmwiki"}
PROTECTED_PREFIXES = ("raw/", "raw\\")


class WriteRejected(Exception):
    def __init__(self, reason: str, gate: str):
        super().__init__(reason)
        self.reason = reason
        self.gate = gate


@dataclass
class TargetCheck:
    ok: bool
    reason: str = ""
    gate: str = ""


def _is_absolute(rel_path: str) -> bool:
    p = rel_path.replace("\\", "/")
    if p.startswith("/"):
        return True
    # Windows drive-letter / UNC
    if len(rel_path) >= 2 and rel_path[1] == ":":
        return True
    if p.startswith("//"):
        return True
    return False


def classify_target(rel_path: str) -> TargetCheck:
    if not isinstance(rel_path, str) or not rel_path.strip():
        return TargetCheck(False, "empty path", "path")
    if _is_absolute(rel_path):
        return TargetCheck(False, f"absolute path rejected: {rel_path}", "absolute")
    norm = rel_path.replace("\\", "/")
    # Traversal: any `..` segment.
    if any(part == ".." for part in PurePosixPath(norm).parts):
        return TargetCheck(False, f"traversal rejected: {rel_path}", "traversal")
    # Protected files / dirs.
    if norm in PROTECTED_NAMES or PurePosixPath(norm).name in PROTECTED_NAMES:
        return TargetCheck(False, f"protected target rejected: {rel_path}", "protected")
    if norm.startswith("raw/"):
        return TargetCheck(False, f"raw/ is immutable: {rel_path}", "protected")
    # Must be under wiki/.
    if not (norm == WIKI_DIR or norm.startswith(WIKI_DIR + "/")):
        return TargetCheck(False, f"outside wiki/: {rel_path}", "path")
    # Must be a page file (a .md), not the wiki dir itself.
    if norm in (WIKI_DIR, DERIVED_PREFIX.rstrip("/")):
        return TargetCheck(False, f"not a page file: {rel_path}", "path")
    return TargetCheck(True)


def _is_derived_target(rel_path: str) -> bool:
    return rel_path.replace("\\", "/").startswith(DERIVED_PREFIX)


@dataclass
class WriteOp:
    rel_path: str
    content: str


@dataclass
class WriteSession:
    wiki_root: "str | Path"
    max_count: int = 100
    max_bytes: int = 10 * 1024 * 1024
    origin: str = "source"   # "source" | "derived" — derived edits go wiki/derived/ only
    ops: list = field(default_factory=list)
    _bytes: int = 0

    def add(self, rel_path: str, content: str) -> WriteOp:
        chk = classify_target(rel_path)
        if not chk.ok:
            raise WriteRejected(chk.reason, chk.gate)
        # D20: derived-origin edits land only in wiki/derived/.
        if self.origin == "derived" and not _is_derived_target(rel_path):
            raise WriteRejected(
                f"cross-namespace: derived-origin edit must target wiki/derived/: {rel_path}",
                "cross_namespace",
            )
        # Budget (count) overflow -> human gate.
        if len(self.ops) + 1 > self.max_count:
            raise WriteRejected(
                f"budget overflow: count > {self.max_count}", "budget")
        size = len(content.encode("utf-8"))
        if self._bytes + size > self.max_bytes:
            raise WriteRejected(
                f"budget overflow: total bytes > {self.max_bytes}", "budget")
        op = WriteOp(rel_path=rel_path.replace("\\", "/"), content=content)
        self.ops.append(op)
        self._bytes += size
        return op

    def commit(self) -> list[str]:
        root = Path(self.wiki_root)
        written: list[str] = []
        for op in self.ops:
            dest = root / Path(op.rel_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(op.content, encoding="utf-8")
            written.append(op.rel_path)
        return written
