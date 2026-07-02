# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""link-lint — orphan page / missing cross-ref detection (design §5).

Computes the wikilink graph over wiki/ + wiki/derived/ pages and reports:
  - missing cross-refs: a `[[Target]]` that resolves to no existing page.
  - orphan pages: a page with no inbound `[[...]]` link from any other page
    (and itself not the index seed).

I/O contract:
    page_name(rel_path) -> str
      out: the wikilink-resolvable name of a page (basename without .md).

    extract_links(text) -> list[str]
      out: the targets of every [[Target]] (and [[Target|alias]]) in the text.

    build_graph(wiki_root) -> LinkGraph
      out: LinkGraph { pages: {name: rel_path}, edges: {src_name: [target,...]} }

    lint(wiki_root) -> LintReport
      out: LintReport { missing: [(src_rel, target)], orphans: [rel_path] }
           missing = links whose target name is not a known page.
           orphans = pages with zero inbound links.

A `[[Target]]` resolves by page name (basename). Resolution is case-sensitive and
namespace-agnostic: a derived page and a source page share the name space (matches
the promote move which only changes the directory, not the name).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from llmwiki.core import wiki_index


_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]")


@dataclass
class LinkGraph:
    pages: dict        # name -> rel_path
    edges: dict        # src_name -> list[target_name]


@dataclass
class LintReport:
    missing: list = field(default_factory=list)   # (src_rel_path, target_name)
    orphans: list = field(default_factory=list)    # rel_path with no inbound links


def page_name(rel_path: str) -> str:
    return Path(rel_path).stem


def extract_links(text: str) -> list[str]:
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(text)]


def build_graph(wiki_root: "str | Path") -> LinkGraph:
    root = Path(wiki_root)
    entries = wiki_index.scan_pages(root)
    pages: dict[str, str] = {}
    edges: dict[str, list[str]] = {}
    for pe in entries:
        name = page_name(pe.rel_path)
        pages[name] = pe.rel_path
    for pe in entries:
        name = page_name(pe.rel_path)
        text = (root / pe.rel_path).read_text(encoding="utf-8")
        edges[name] = extract_links(text)
    return LinkGraph(pages=pages, edges=edges)


def lint(wiki_root: "str | Path") -> LintReport:
    g = build_graph(wiki_root)
    missing: list = []
    inbound: dict[str, int] = {name: 0 for name in g.pages}
    for src, targets in g.edges.items():
        for t in targets:
            if t in g.pages:
                inbound[t] = inbound.get(t, 0) + 1
            else:
                missing.append((g.pages[src], t))
    orphans = sorted(g.pages[name] for name, c in inbound.items() if c == 0)
    return LintReport(missing=sorted(missing), orphans=orphans)
