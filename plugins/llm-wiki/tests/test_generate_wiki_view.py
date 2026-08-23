import socket

import pytest

from llmwiki.core import wiki_index
from llmwiki.view import generate_wiki_view as gwv



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



def test_same_name_source_and_derived_dual_links(tmp_path):
    _make(tmp_path, {
        "wiki/index.md": "go to [[synth]]",
        "wiki/synth.md": "source synth",
        "wiki/derived/synth.md": "derived synth",
    })
    html = gwv.render_page_html(tmp_path, "wiki/index.md", _pages(tmp_path))
    assert gwv.page_url("wiki/synth.md") in html
    assert gwv.page_url("wiki/derived/synth.md") in html
    assert "(source)" in html
    assert "(derived)" in html



def test_missing_link_is_distinct_and_non_navigable(tmp_path):
    _make(tmp_path, {"wiki/index.md": "dangling [[Ghost]] ref"})
    html = gwv.render_page_html(tmp_path, "wiki/index.md", _pages(tmp_path))
    assert 'class="wikilink missing"' in html
    assert gwv.page_url("wiki/Ghost.md") not in html
    assert "Ghost" in html



def test_raw_pages_not_listed(tmp_path):
    _make(tmp_path, {
        "wiki/index.md": "hi",
        "wiki/derived/d.md": "derived",
    })
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "abc.md").write_text("raw secret", encoding="utf-8")
    entries = wiki_index.scan_pages(tmp_path)
    rels = {pe.rel_path for pe in entries}
    assert "wiki/index.md" in rels
    assert "wiki/derived/d.md" in rels
    assert not any(r.startswith("raw/") for r in rels)
    html = gwv.render_index_html(tmp_path, entries)
    assert "raw/abc.md" not in html



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
    (tmp_path / "wiki").mkdir()
    monkeypatch.chdir(tmp_path)
    rc = gwv.main(["--serve", "--no-open"])
    assert rc == 2


assert socket is not None



import threading
from http.client import HTTPConnection
from http.server import HTTPServer


def _serve(tmp_path):
    handler = gwv.build_app(tmp_path)
    server = HTTPServer((gwv.SERVE_HOST, 0), handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def test_rebinding_host_refused_403(tmp_path):
    _make(tmp_path, {"wiki/index.md": "hi"})
    server = _serve(tmp_path)
    try:
        port = server.server_address[1]
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/", headers={"Host": "evil.example"})
        resp = conn.getresponse()
        assert resp.status == 403
        conn.close()
        conn2 = HTTPConnection("127.0.0.1", port, timeout=10)
        conn2.putrequest("GET", "/", skip_host=True)
        conn2.endheaders()
        resp2 = conn2.getresponse()
        assert resp2.status == 403, "a request with no Host header fails closed as well"
        conn2.close()
    finally:
        server.shutdown()
        server.server_close()


def test_loopback_host_200_with_csp_header(tmp_path):
    _make(tmp_path, {"wiki/index.md": "hi"})
    server = _serve(tmp_path)
    try:
        port = server.server_address[1]
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Security-Policy") == gwv.CSP_POLICY
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_script_in_page_body_is_sanitized_but_wikilinks_survive(tmp_path):
    _make(tmp_path, {
        "wiki/index.md": (
            "before\n\n"
            "<script>fetch('http://attacker.example/')</script>\n\n"
            '<img src="x" onerror="alert(1)">\n\n'
            "see [[Target]] after\n"
        ),
        "wiki/Target.md": "target body",
    })
    server = _serve(tmp_path)
    try:
        port = server.server_address[1]
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/page?path=wiki/index.md")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert "<script" not in body
        assert "onerror" not in body
        assert "attacker.example" not in body
        assert 'class="wikilink"' in body
        assert gwv.page_url("wiki/Target.md") in body
        assert resp.getheader("Content-Security-Policy") == gwv.CSP_POLICY
        conn.close()
    finally:
        server.shutdown()
        server.server_close()



def test_exclusive_server_refuses_second_bind_on_same_port(tmp_path):
    _make(tmp_path, {"wiki/index.md": "hi"})
    handler = gwv.build_app(tmp_path)
    first = gwv.ExclusiveHTTPServer((gwv.SERVE_HOST, 0), handler)
    try:
        port = first.server_address[1]
        with pytest.raises(OSError):
            gwv.ExclusiveHTTPServer((gwv.SERVE_HOST, port), handler)
        assert first.server_address[1] == port, (
            "two viewers must not co-bind one port: the second would serve a "
            "different wiki on an address the browser routes to either one"
        )
    finally:
        first.server_close()


def test_exclusive_server_disables_address_reuse():
    assert gwv.ExclusiveHTTPServer.allow_reuse_address is False
