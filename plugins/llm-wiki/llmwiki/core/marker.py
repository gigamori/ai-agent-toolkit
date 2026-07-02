# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Marker detection (D8).

The `.llmwiki` dotfile in a directory marks it as a wiki-root. Detection-only:
SCHEMA.md is never used for detection (avoids generic-name misdetection). The
marker is thin: `{ version, schema: <path> }`.

I/O contract:
    detect(cwd) -> Marker | None
      in : a directory path (typically CWD)
      out: Marker { root, version, schema_path } if `<cwd>/.llmwiki` exists,
           else None (dormant — caller emits empty exit).

    is_active(cwd) -> bool        # convenience: detect(cwd) is not None

Parsing is the same dependency-free flat key: value scan used elsewhere; the
marker holds only `version:` and `schema:`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


MARKER_NAME = ".llmwiki"

_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$")


@dataclass
class Marker:
    root: Path
    version: str
    schema_path: Path   # absolute path to the schema file named in the marker


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _KV_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("'\"").strip()
    return out


def detect(cwd: "str | Path") -> "Marker | None":
    root = Path(cwd)
    marker = root / MARKER_NAME
    if not marker.is_file():
        return None
    try:
        kv = _parse(marker.read_text(encoding="utf-8"))
    except OSError:
        return None
    schema_rel = kv.get("schema", "SCHEMA.md")
    return Marker(
        root=root,
        version=kv.get("version", ""),
        schema_path=root / schema_rel,
    )


def is_active(cwd: "str | Path") -> bool:
    return detect(cwd) is not None
