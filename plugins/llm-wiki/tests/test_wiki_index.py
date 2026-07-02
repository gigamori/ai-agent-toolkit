"""Tests: index integrity + tier marker (D22).

Covers: tier assigned from path (wiki/ = source, wiki/derived/ = derived);
build/regenerate produces a table; integrity detects missing/stale.
"""
from llmwiki.core import wiki_index


def test_tier_of_path():
    assert wiki_index.tier_of("wiki/foo.md") == "source"
    assert wiki_index.tier_of("wiki/derived/bar.md") == "derived"
    assert wiki_index.tier_of("wiki\\derived\\bar.md") == "derived"


def _make_wiki(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "derived").mkdir()
    (tmp_path / "wiki" / "a.md").write_text("page a", encoding="utf-8")
    (tmp_path / "wiki" / "derived" / "b.md").write_text("page b", encoding="utf-8")


def test_scan_assigns_tier(tmp_path):
    _make_wiki(tmp_path)
    pages = {pe.rel_path: pe.tier for pe in wiki_index.scan_pages(tmp_path)}
    assert pages["wiki/a.md"] == "source"
    assert pages["wiki/derived/b.md"] == "derived"


def test_build_and_regenerate_index(tmp_path):
    _make_wiki(tmp_path)
    body = wiki_index.regenerate(tmp_path)
    assert "| wiki/a.md | source |" in body
    assert "| wiki/derived/b.md | derived |" in body
    assert (tmp_path / "index.md").read_text(encoding="utf-8") == body


def test_integrity_detects_missing_and_stale(tmp_path):
    _make_wiki(tmp_path)
    # index references a stale page and omits a real one.
    (tmp_path / "index.md").write_text(
        "| Page | Tier |\n|--|--|\n| wiki/ghost.md | source |\n", encoding="utf-8"
    )
    rep = wiki_index.check_integrity(tmp_path)
    assert rep.ok is False
    assert "wiki/a.md" in rep.missing
    assert "wiki/ghost.md" in rep.stale


def test_integrity_ok_after_regenerate(tmp_path):
    _make_wiki(tmp_path)
    wiki_index.regenerate(tmp_path)
    rep = wiki_index.check_integrity(tmp_path)
    assert rep.ok is True
