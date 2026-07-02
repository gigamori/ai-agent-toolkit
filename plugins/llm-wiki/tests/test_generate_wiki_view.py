"""Tests: generate_wiki_view — local wiki viewer (plan §1-A / §3 T1).

Covers (traced to plan §/Q):
  - md -> HTML renders heading/list/code/table         (Q2 full md)
  - [[X]] becomes an href to the right page URL         (§1-A wikilink, link_lint reuse)
  - same-name source+derived yields two tier-labelled links (Q3)
  - a missing [[Y]] is rendered distinct / non-navigable (Q3)
  - raw/ pages are NOT listed                            (Q4)
  - server smoke: build_app + bind 127.0.0.1, no long-lived serve (§3 T1)
"""
import socket

from llmwiki.core import wiki_index
from llmwiki.view import generate_wiki_view as gwv


# ── fixture builder ──────────────────────────────────────────────────────────

def _make(tmp_path, files, *, marker=True):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "derived").mkdir()
    if marker:
        (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                           encoding="utf-8")
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def _pages(tmp_path):
    from llmwiki.lint import link_lint
    return link_lint.build_graph(tmp_path).pages


# ── frontmatter ──────────────────────────────────────────────────────────────

def test_split_frontmatter_scalars_and_body():
    text = "---\nprovenance: source\ndoc_type: note\n---\n# Body\n"
    fm, body = gwv.split_frontmatter(text)
    assert fm["provenance"] == "source"
    assert fm["doc_type"] == "note"
    assert "# Body" in body
    assert "provenance" not in body


def test_split_frontmatter_none():
    text = "# No frontmatter\n"
    fm, body = gwv.split_frontmatter(text)
    assert fm == {}
    assert body == text


# ── md -> HTML rendering (Q2) ────────────────────────────────────────────────

def test_render_markdown_heading_list_code_table(tmp_path):
    md = (
        "# Title\n\n"
        "- one\n- two\n\n"
        "```python\nprint('hi')\n```\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    _make(tmp_path, {"wiki/index.md": md})
    html = gwv.render_page_html(tmp_path, "wiki/index.md", _pages(tmp_path))
    assert "<h1" in html and "Title" in html
    assert "<ul>" in html and "<li>one</li>" in html
    assert "<code" in html or "<pre" in html
    assert "<table>" in html and "<th>a</th>" in html


# ── wikilink -> href (link_lint reuse, §1-A) ─────────────────────────────────

def test_wikilink_becomes_href_to_page_url(tmp_path):
    _make(tmp_path, {
        "wiki/index.md": "see [[Target]] here",
        "wiki/Target.md": "I am the target",
    })
    html = gwv.render_page_html(tmp_path, "wiki/index.md", _pages(tmp_path))
    assert gwv.page_url("wiki/Target.md") in html
    assert 'class="wikilink"' in html


def test_wikilink_alias_label_used(tmp_path):
    _make(tmp_path, {
        "wiki/index.md": "see [[Target|the alias]] here",
        "wiki/Target.md": "x",
    })
    html = gwv.render_page_html(tmp_path, "wiki/index.md", _pages(tmp_path))
    assert "the alias" in html
    assert gwv.page_url("wiki/Target.md") in html


# ── same-name source + derived -> two tier-labelled links (Q3) ───────────────

def test_same_name_source_and_derived_dual_links(tmp_path):
    _make(tmp_path, {
        "wiki/index.md": "go to [[synth]]",
        "wiki/synth.md": "source synth",
        "wiki/derived/synth.md": "derived synth",
    })
    html = gwv.render_page_html(tmp_path, "wiki/index.md", _pages(tmp_path))
    # BOTH candidate page URLs present
    assert gwv.page_url("wiki/synth.md") in html
    assert gwv.page_url("wiki/derived/synth.md") in html
    # tier labels surfaced
    assert "(source)" in html
    assert "(derived)" in html


# ── missing link rendered distinct / non-navigable (Q3) ──────────────────────

def test_missing_link_is_distinct_and_non_navigable(tmp_path):
    _make(tmp_path, {"wiki/index.md": "dangling [[Ghost]] ref"})
    html = gwv.render_page_html(tmp_path, "wiki/index.md", _pages(tmp_path))
    assert 'class="wikilink missing"' in html
    # no anchor href to a Ghost page
    assert gwv.page_url("wiki/Ghost.md") not in html
    assert "Ghost" in html


# ── raw/ excluded from listing (Q4) ──────────────────────────────────────────

def test_raw_pages_not_listed(tmp_path):
    _make(tmp_path, {
        "wiki/index.md": "hi",
        "wiki/derived/d.md": "derived",
    })
    # a raw artifact must never be viewable
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "abc.md").write_text("raw secret", encoding="utf-8")
    entries = wiki_index.scan_pages(tmp_path)
    rels = {pe.rel_path for pe in entries}
    assert "wiki/index.md" in rels
    assert "wiki/derived/d.md" in rels
    assert not any(r.startswith("raw/") for r in rels)
    html = gwv.render_index_html(tmp_path, entries)
    assert "raw/abc.md" not in html


# ── index listing shows tier badges + root page ──────────────────────────────

def test_index_lists_pages_with_tier_badges(tmp_path):
    _make(tmp_path, {
        "wiki/index.md": "root",
        "wiki/derived/d.md": "derived",
    })
    entries = wiki_index.scan_pages(tmp_path)
    html = gwv.render_index_html(tmp_path, entries)
    assert "tier-source" in html
    assert "tier-derived" in html
    assert gwv.page_url("wiki/index.md") in html


# ── server smoke: build app + bind 127.0.0.1 (no long-lived serve) ───────────

def test_build_app_returns_handler(tmp_path):
    _make(tmp_path, {"wiki/index.md": "hi"})
    handler = gwv.build_app(tmp_path)
    assert handler is not None
    from http.server import BaseHTTPRequestHandler
    assert issubclass(handler, BaseHTTPRequestHandler)


def test_server_binds_127_0_0_1(tmp_path):
    _make(tmp_path, {"wiki/index.md": "hi"})
    from http.server import HTTPServer
    handler = gwv.build_app(tmp_path)
    # bind ephemeral port on the loopback host the script uses; close immediately
    server = HTTPServer((gwv.SERVE_HOST, 0), handler)
    try:
        host, port = server.server_address[:2]
        assert host == "127.0.0.1"
        assert gwv.SERVE_HOST == "127.0.0.1"
        assert port != 0
    finally:
        server.server_close()


def test_default_port_is_17330():
    assert gwv.DEFAULT_PORT == 17330


def test_marker_absent_errors(tmp_path, monkeypatch):
    # no .llmwiki marker -> main() must refuse with exit code 2 (§1-A wiki-root check)
    (tmp_path / "wiki").mkdir()
    monkeypatch.chdir(tmp_path)
    rc = gwv.main(["--serve", "--no-open"])
    assert rc == 2


# ensure socket import is used (avoids unused-import lint in some runners)
assert socket is not None
