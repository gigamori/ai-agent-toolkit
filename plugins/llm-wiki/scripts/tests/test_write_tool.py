"""Tests: allowlist write tool (D19/D20).

Covers: targets limited to wiki/ + wiki/derived/; reject SCHEMA.md/.llmwiki/raw/,
absolute paths, traversal; budget overflow -> human gate; derived-origin edits
confined to wiki/derived/; commit writes files.
"""
import pytest

import write_tool as wt


def test_accepts_wiki_and_derived():
    assert wt.classify_target("wiki/page.md").ok
    assert wt.classify_target("wiki/derived/page.md").ok


def test_rejects_outside_wiki():
    c = wt.classify_target("notes/x.md")
    assert not c.ok and c.gate == "path"


def test_rejects_protected_files():
    assert not wt.classify_target("SCHEMA.md").ok
    assert not wt.classify_target(".llmwiki").ok
    assert wt.classify_target("SCHEMA.md").gate == "protected"


def test_rejects_raw():
    c = wt.classify_target("raw/abc.txt")
    assert not c.ok and c.gate == "protected"
    assert not wt.classify_target("raw/derived/x.md").ok


def test_rejects_absolute_paths():
    assert not wt.classify_target("/etc/passwd").ok
    assert wt.classify_target("/etc/passwd").gate == "absolute"
    assert not wt.classify_target("C:/Users/x/page.md").ok
    assert wt.classify_target("C:/Users/x/page.md").gate == "absolute"


def test_rejects_traversal():
    c = wt.classify_target("wiki/../SCHEMA.md")
    assert not c.ok and c.gate == "traversal"


def test_budget_count_overflow_is_human_gate(tmp_path):
    s = wt.WriteSession(tmp_path, max_count=2, origin="source")
    s.add("wiki/a.md", "x")
    s.add("wiki/b.md", "y")
    with pytest.raises(wt.WriteRejected) as e:
        s.add("wiki/c.md", "z")
    assert e.value.gate == "budget"


def test_budget_size_overflow_is_human_gate(tmp_path):
    s = wt.WriteSession(tmp_path, max_bytes=10, origin="source")
    with pytest.raises(wt.WriteRejected) as e:
        s.add("wiki/a.md", "0123456789ABCDEF")  # 16 bytes > 10
    assert e.value.gate == "budget"


def test_derived_origin_confined_to_derived(tmp_path):
    s = wt.WriteSession(tmp_path, origin="derived")
    s.add("wiki/derived/ok.md", "fine")
    with pytest.raises(wt.WriteRejected) as e:
        s.add("wiki/escape.md", "into source ns")
    assert e.value.gate == "cross_namespace"


def test_commit_writes_files(tmp_path):
    s = wt.WriteSession(tmp_path, origin="source")
    s.add("wiki/a.md", "content a")
    s.add("wiki/derived/b.md", "content b")
    written = s.commit()
    assert set(written) == {"wiki/a.md", "wiki/derived/b.md"}
    assert (tmp_path / "wiki" / "a.md").read_text(encoding="utf-8") == "content a"
    assert (tmp_path / "wiki" / "derived" / "b.md").read_text(encoding="utf-8") == "content b"
