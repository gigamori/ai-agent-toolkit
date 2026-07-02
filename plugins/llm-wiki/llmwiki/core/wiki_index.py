# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""index integrity + tier marker (D22).

Citation is by path, and the path encodes the tier: `wiki/<page>.md` = source,
`wiki/derived/<page>.md` = derived. Code (not a new LLM rule) assigns the tier
marker from the path and checks / regenerates index.md.

I/O contract:
    tier_of(rel_path) -> str
      in : a page path relative to wiki root (e.g. "wiki/derived/foo.md")
      out: "source" | "derived"  (derived iff path is under wiki/derived/)

    scan_pages(wiki_root) -> list[PageEntry]
      out: PageEntry { rel_path, tier } for every *.md under wiki/ (and
           wiki/derived/), tier assigned from path.

    build_index(wiki_root) -> str
      out: the index.md body — a table of (page path, tier) the code regenerates.

    check_integrity(wiki_root) -> IntegrityReport
      out: IntegrityReport { ok, missing: [paths in fs not in index],
           stale: [paths in index not in fs] } comparing index.md to fs.

    regenerate(wiki_root) -> str    # writes build_index() to index.md, returns body

`tier` is never inferred by the LLM; it is a deterministic function of the path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


WIKI_DIR = "wiki"
DERIVED_SUBDIR = "wiki/derived"

_INDEX_ROW_RE = re.compile(r"^\|\s*(wiki/[^\s|]+)\s*\|\s*(source|derived)\s*\|")


@dataclass
class PageEntry:
    rel_path: str
    tier: str


@dataclass
class IntegrityReport:
    ok: bool
    missing: list = field(default_factory=list)   # on fs, not in index
    stale: list = field(default_factory=list)     # in index, not on fs


def tier_of(rel_path: str) -> str:
    norm = rel_path.replace("\\", "/")
    return "derived" if norm.startswith(DERIVED_SUBDIR + "/") else "source"


def scan_pages(wiki_root: "str | Path") -> list[PageEntry]:
    root = Path(wiki_root)
    wiki = root / WIKI_DIR
    if not wiki.is_dir():
        return []
    out: list[PageEntry] = []
    for p in sorted(wiki.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        if Path(p).name == "README.md":
            continue
        out.append(PageEntry(rel_path=rel, tier=tier_of(rel)))
    return out


def build_index(wiki_root: "str | Path") -> str:
    pages = scan_pages(wiki_root)
    lines = [
        "# Index",
        "",
        "Content catalog. `tier` is assigned by code from the path (D22):",
        "`wiki/` = source, `wiki/derived/` = derived.",
        "",
        "| Page | Tier |",
        "|------|------|",
    ]
    for pe in pages:
        lines.append(f"| {pe.rel_path} | {pe.tier} |")
    lines.append("")
    return "\n".join(lines)


def _index_rows(index_path: Path) -> dict[str, str]:
    if not index_path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in index_path.read_text(encoding="utf-8").splitlines():
        m = _INDEX_ROW_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def check_integrity(wiki_root: "str | Path") -> IntegrityReport:
    root = Path(wiki_root)
    fs = {pe.rel_path: pe.tier for pe in scan_pages(root)}
    idx = _index_rows(root / "index.md")
    missing = sorted(p for p in fs if p not in idx)
    stale = sorted(p for p in idx if p not in fs)
    # Tier disagreements count as integrity failures too.
    tier_mismatch = sorted(p for p in fs if p in idx and idx[p] != fs[p])
    ok = not missing and not stale and not tier_mismatch
    rep = IntegrityReport(ok=ok, missing=missing, stale=stale)
    rep.tier_mismatch = tier_mismatch  # type: ignore[attr-defined]
    return rep


def regenerate(wiki_root: "str | Path") -> str:
    root = Path(wiki_root)
    body = build_index(root)
    (root / "index.md").write_text(body, encoding="utf-8")
    return body
