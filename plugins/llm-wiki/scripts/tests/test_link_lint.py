"""Tests: link-lint orphan / missing cross-ref detection (design §5).

Covers: extract wikilinks; missing target detected; orphan page detected;
linked page not flagged orphan.
"""
import link_lint


def test_extract_links():
    text = "see [[Alpha]] and [[Beta|the beta page]] and [[Gamma]]"
    assert link_lint.extract_links(text) == ["Alpha", "Beta", "Gamma"]


def _make(tmp_path, files):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "derived").mkdir()
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def test_missing_cross_ref(tmp_path):
    _make(tmp_path, {
        "wiki/index.md": "links to [[Ghost]]",
        "wiki/index2.md": "no links",   # gives index.md an inbound? no — separate
    })
    rep = link_lint.lint(tmp_path)
    assert ("wiki/index.md", "Ghost") in rep.missing


def test_orphan_detection(tmp_path):
    _make(tmp_path, {
        "wiki/hub.md": "see [[leaf]]",
        "wiki/leaf.md": "I am referenced",
        "wiki/lonely.md": "nobody links to me",
    })
    rep = link_lint.lint(tmp_path)
    # leaf has an inbound link -> not orphan.
    assert "wiki/leaf.md" not in rep.orphans
    # hub and lonely have no inbound links -> orphans.
    assert "wiki/hub.md" in rep.orphans
    assert "wiki/lonely.md" in rep.orphans


def test_derived_and_source_share_namespace(tmp_path):
    _make(tmp_path, {
        "wiki/hub.md": "see [[synth]]",
        "wiki/derived/synth.md": "derived page named synth",
    })
    rep = link_lint.lint(tmp_path)
    # [[synth]] resolves to the derived page (name-based), so no missing ref.
    assert not any(t == "synth" for _, t in rep.missing)
    assert "wiki/derived/synth.md" not in rep.orphans
