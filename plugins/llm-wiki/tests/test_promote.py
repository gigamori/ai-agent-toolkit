"""Tests: promote (D20).

Covers: promote = move (not copy) wiki/derived/X -> wiki/X; inbound path-reference
link-rewrite; provenance flipped derived -> source; reject derived contamination
(inline transclusion / explicit derived-inline marker); reject non-derived source.
"""
import pytest

from llmwiki.write import promote


def _make(tmp_path, files):
    (tmp_path / "wiki" / "derived").mkdir(parents=True)
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def test_derived_to_source_path():
    assert promote.derived_to_source_path("wiki/derived/x.md") == "wiki/x.md"


def test_promote_moves_and_rewrites(tmp_path):
    _make(tmp_path, {
        "wiki/derived/synth.md": "---\nprovenance: derived\n---\nbody",
        "wiki/hub.md": "ref to wiki/derived/synth.md here",
    })
    res = promote.promote(tmp_path, "wiki/derived/synth.md")
    assert res.ok
    assert res.dest_rel == "wiki/synth.md"
    # Move, not copy: source gone, dest present.
    assert not (tmp_path / "wiki" / "derived" / "synth.md").exists()
    dest = tmp_path / "wiki" / "synth.md"
    assert dest.exists()
    # provenance flipped.
    assert "provenance: source" in dest.read_text(encoding="utf-8")
    # inbound path-reference rewritten.
    hub = (tmp_path / "wiki" / "hub.md").read_text(encoding="utf-8")
    assert "wiki/synth.md" in hub
    assert "wiki/derived/synth.md" not in hub
    assert "wiki/hub.md" in res.rewritten


def test_reject_inline_transclusion_contamination(tmp_path):
    _make(tmp_path, {
        "wiki/derived/dirty.md": "synthesis ![[wiki/derived/other]] inlined",
    })
    with pytest.raises(promote.PromoteRejected) as e:
        promote.promote(tmp_path, "wiki/derived/dirty.md")
    assert "contamination" in str(e.value)


def test_reject_explicit_derived_inline_marker(tmp_path):
    _make(tmp_path, {
        "wiki/derived/dirty.md": "text\n<!-- derived-inline -->\npasted derived",
    })
    with pytest.raises(promote.PromoteRejected):
        promote.promote(tmp_path, "wiki/derived/dirty.md")


def test_reject_non_derived(tmp_path):
    _make(tmp_path, {"wiki/already.md": "a source page"})
    with pytest.raises(promote.PromoteRejected):
        promote.promote(tmp_path, "wiki/already.md")


def test_reject_missing(tmp_path):
    _make(tmp_path, {})
    with pytest.raises(promote.PromoteRejected):
        promote.promote(tmp_path, "wiki/derived/nope.md")


def test_detect_contamination_clean():
    assert promote.detect_contamination("just a [[Link]] and prose") == []
