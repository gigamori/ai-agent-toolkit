# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""index-direct read path (read/ layer; package-cli-architecture.md §CLI verb,
optional-search-qmd.md Invariant #4).

The `search` verb dispatches HERE when the qmd backend is off / absent / below
threshold. It performs NO full-text ranking (the index carries no such signal); it
returns the full page enumeration exactly as the `scan-pages` verb does, so the
`index` default stays byte-identical to today's wiki-query grounding (Invariant #4)
— the LLM then selects which pages to Read. Dependency-free; imports `core` only,
so the `search` verb's index path keeps a clean read-profile closure (D-2).
"""
from __future__ import annotations

from llmwiki.core import wiki_index


def enumerate_pages(root):
    """Index-direct 'search': the full ``[(tier, rel_path)]`` page set, in
    scan_pages order (sorted path, README skipped). Identical grounding to the
    `scan-pages` verb — the `index` backend adds no ranking (Invariant #4)."""
    return [(pe.tier, pe.rel_path) for pe in wiki_index.scan_pages(root)]
