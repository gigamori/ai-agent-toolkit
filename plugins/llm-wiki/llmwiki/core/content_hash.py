# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Content-hash dedup (D18).

raw id = content-hash. Existing hash = no-op. An updated version links back to the
prior raw via `supersedes`. Only genuinely-new content reaches the LLM core.

I/O contract:
    content_hash(data: bytes | str) -> str
      in : redacted bytes (or text — encoded utf-8) of the raw artifact
      out: lowercase hex sha-256 digest (the raw id)

    raw_filename(hash_hex, ext) -> str          # "<hash>.<ext>"  (FE-B)
    derived_filename(hash_hex) -> str           # "<hash>.md"     (FE-A / FE-B')

    dedup_status(wiki_root, rel_dir, hash_hex, ext) -> DedupStatus
      in : wiki root, the raw subdir ("raw" or "raw/derived"), hash, extension
      out: DedupStatus { exists: bool, rel_path: str, abs_path: Path }
           exists == True  -> caller treats ingest as no-op (D18)
           exists == False -> caller writes the new raw artifact

    supersedes_link(prev_hash, ext) -> str
      out: relative raw path of the superseded artifact, for the `supersedes`
           frontmatter field on the new version.

Hash is computed over the REDACTED content (redaction runs before hashing, D16),
so two inputs that redact to the same bytes are correctly deduped.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


def content_hash(data: "bytes | str") -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def raw_filename(hash_hex: str, ext: str) -> str:
    ext = ext.lstrip(".")
    return f"{hash_hex}.{ext}"


def derived_filename(hash_hex: str) -> str:
    return f"{hash_hex}.md"


@dataclass
class DedupStatus:
    exists: bool
    rel_path: str   # posix relative path under wiki root, e.g. "raw/derived/<h>.md"
    abs_path: Path


def dedup_status(wiki_root: "str | Path", rel_dir: str, hash_hex: str,
                 ext: str = "md") -> DedupStatus:
    root = Path(wiki_root)
    fname = raw_filename(hash_hex, ext)
    rel = f"{rel_dir.rstrip('/')}/{fname}"
    abs_path = root / Path(rel)
    return DedupStatus(exists=abs_path.exists(), rel_path=rel, abs_path=abs_path)


def supersedes_link(prev_hash: str, ext: str = "md", rel_dir: str = "raw") -> str:
    return f"{rel_dir.rstrip('/')}/{raw_filename(prev_hash, ext)}"
