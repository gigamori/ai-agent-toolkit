# /// script
# requires-python = ">=3.11"
# dependencies = ["markdown", "nh3"]
# ///
"""generate_wiki_view.py — local wiki page viewer (md display + wikilink traversal).

Deterministic, dependency-light viewer for an llm-wiki (plan §1-A / §5).
Renders wiki/ + wiki/derived/ markdown pages to HTML on demand and turns
``[[Target]]`` wikilinks into navigable links between page views.

Design decisions (plan §5):
  Q1  HTTP server, ``--serve`` only, bound to 127.0.0.1 (never externally reachable).
  Q2  md -> HTML via the ``markdown`` library (PEP723 inline dep; uv resolves it).
  Q3  Pages are identified by rel_path (tier-distinct): wiki/X.md and
      wiki/derived/X.md are SEPARATE pages. A ``[[X]]`` whose basename resolves to
      both renders BOTH candidates as tier-labelled links (source / derived).
  Q4  Only wiki/ + wiki/derived/ are viewable. raw/ is NOT exposed.

Wikilink parsing is REUSED from ``link_lint`` (no new parser is written here):
  - ``link_lint.extract_links(text)``  -> targets of every [[T]] / [[T|alias]]
  - ``link_lint.build_graph(root)``    -> LinkGraph{pages: {name: rel_path}, edges}
  - ``link_lint.page_name(rel_path)``  -> basename stem

Usage:
    uv run python generate_wiki_view.py --serve [--port 17330] [--root <path>]

The wiki root is resolved via wiki_root_resolver (prompt>pj>workspace>cwd);
``--root <path>`` is the top override. If nothing resolves, exit 2.

Exit codes:
    0 = success (server ran)
    2 = error (no .llmwiki marker / port in use)
"""

from __future__ import annotations

import argparse
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from llmwiki.lint import link_lint   # REUSE extract_links / build_graph / page_name
from llmwiki.core import marker      # REUSE marker.detect (wiki-root check)
from llmwiki.core import wiki_index  # REUSE scan_pages / tier_of
from llmwiki.core import wiki_root_resolver  # REUSE resolve() (multi-scope wiki-root)

import markdown    # (PEP723 inline dep)
import nh3         # (PEP723 inline dep) — HTML sanitizer (stored-XSS hardening)


SERVE_HOST = "127.0.0.1"   # never 0.0.0.0 — not externally reachable (plan §5-Q1 / R-3)
DEFAULT_PORT = 17330       # proposal (plan §1-A); --port overrides

# Loopback Host allowlist (DNS-rebinding hardening). 127.0.0.1 binding keeps the
# socket off the network, but a rebinding attacker points THEIR hostname at
# 127.0.0.1 and reads the wiki through the victim's browser — the Host header is
# the only place that attack is visible, so any other Host value is refused.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Page bodies are untrusted (FE-A files conversation text verbatim; FE-B ingests
# third-party documents; redaction strips secrets/paths, NOT markup), and
# python-markdown passes raw inline HTML through. Belt: nh3 strips active
# content (script/event handlers/js: URLs). Suspenders: the CSP below keeps
# even a sanitizer miss inert (no script eval, no external fetch, no forms).
CSP_POLICY = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:"

# nh3 attribute allowlist: defaults + the viewer's own styling hooks. linkify()
# injects anchors/spans carrying class/title BEFORE the markdown pass, so the
# sanitizer must keep them (href stays default-allowed on <a>; javascript: is
# not an allowed scheme, relative /page?path=... URLs pass through).
NH3_ATTRIBUTES = {
    "*": {"class", "title"},
    "a": {"href", "class", "title"},
    "img": {"src", "alt", "class", "title"},
    # markdown's "toc" extension stamps heading ids (in-page anchors).
    "h1": {"id"}, "h2": {"id"}, "h3": {"id"},
    "h4": {"id"}, "h5": {"id"}, "h6": {"id"},
}


# ── markdown frontmatter (dependency-light, top-level scalars only) ──────────

def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Parses a leading ``---`` YAML-ish fence
    as a flat ``key: value`` scan (scalars only) — same dependency-free style as
    marker.py. Nested structures are skipped; the body is everything after the
    closing fence.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    fm: dict[str, str] = {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    for raw in lines[1:end]:
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        # only top-level (non-indented) scalar keys
        if line[:1].isspace():
            continue
        key, _, val = line.partition(":")
        val = val.strip().strip("'\"").strip()
        if val:   # skip mapping/list parents (value empty)
            fm[key.strip()] = val
    body = "".join(lines[end + 1:])
    return fm, body


# ── HTML helpers ─────────────────────────────────────────────────────────────

def esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def page_url(rel_path: str) -> str:
    return "/page?path=" + quote(rel_path)


_WIKILINK_RE = link_lint._WIKILINK_RE   # REUSE the same compiled pattern


def linkify(body: str, rel_paths: list[str]) -> str:
    """Replace every ``[[Target]]`` / ``[[Target|alias]]`` token with an inline
    HTML anchor (or a styled non-navigable span when missing), keyed off the
    full set of viewable page rel_paths (plan §1-A, Q3).

    Pages are tier-distinct (Q3), so candidate resolution MUST start from the
    tier-distinct rel_path list (link_lint's name->rel_path graph is
    basename-keyed / last-write-wins and would drop a same-name twin). This
    buckets every rel_path by basename so a source+derived same-name pair yields
    BOTH tier-labelled links.
    """
    # basename -> [rel_path, ...] (all tiers sharing the name)
    by_name: dict[str, list[str]] = {}
    for rel in rel_paths:
        by_name.setdefault(link_lint.page_name(rel), []).append(rel)
    for name in by_name:
        by_name[name].sort()   # stable: wiki/X.md before wiki/derived/X.md? sort posix

    def _replace(m) -> str:
        # group(1) = target name; the original token may carry |alias
        target = m.group(1).strip()
        whole = m.group(0)
        alias = None
        inner = whole[2:-2]
        if "|" in inner:
            alias = inner.split("|", 1)[1].strip()
        candidates = by_name.get(target, [])
        if not candidates:
            # missing link — visually distinct, non-navigable (plan §1-A, Q3)
            label = alias or target
            return (
                f'<span class="wikilink missing" '
                f'title="missing page: {esc(target)}">{esc(label)}</span>'
            )
        if len(candidates) == 1:
            rel = candidates[0]
            label = alias or target
            return (
                f'<a class="wikilink" href="{esc(page_url(rel))}">{esc(label)}</a>'
            )
        # same-name across tiers -> render BOTH as tier-labelled links (Q3)
        parts = []
        for rel in candidates:
            tier = wiki_index.tier_of(rel)
            base = alias or target
            parts.append(
                f'<a class="wikilink dup" href="{esc(page_url(rel))}">'
                f'{esc(base)} <span class="tierhint">({esc(tier)})</span></a>'
            )
        return (
            '<span class="wikilink-group" '
            f'title="{esc(target)} resolves to {len(candidates)} pages">'
            + " / ".join(parts)
            + "</span>"
        )

    return _WIKILINK_RE.sub(_replace, body)


def render_page_html(root: Path, rel_path: str, pages: dict[str, str]) -> str:
    """On-demand render of a single page (plan §1-A): read md, split frontmatter,
    linkify wikilinks, then md -> HTML with the ``markdown`` library.

    ``pages`` is accepted for back-compat but candidate resolution uses the
    tier-distinct rel_path set from ``scan_pages`` (a basename-keyed ``pages``
    dict drops same-name source/derived twins — see ``linkify``)."""
    text = (root / rel_path).read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    rel_paths = [pe.rel_path for pe in wiki_index.scan_pages(root)]
    linked = linkify(body, rel_paths)
    html_body = markdown.markdown(
        linked, extensions=["tables", "fenced_code", "toc"]
    )
    # Sanitize AFTER the md->HTML pass: page bodies are untrusted and
    # python-markdown passes raw inline HTML through (stored XSS). nh3 strips
    # scripts / event handlers / javascript: URLs while keeping the viewer's
    # own linkify() markup (see NH3_ATTRIBUTES). CSP is the second layer.
    html_body = nh3.clean(html_body, attributes=NH3_ATTRIBUTES)

    tier = wiki_index.tier_of(rel_path)
    name = link_lint.page_name(rel_path)

    fm_rows = ""
    for key in ("provenance", "doc_type", "derived_origin"):
        if key in fm:
            fm_rows += (
                f'<span class="fm-item"><b>{esc(key)}</b>: {esc(fm[key])}</span>'
            )
    fm_html = f'<div class="frontmatter">{fm_rows}</div>' if fm_rows else ""

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(name)} — llm-wiki</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<header>
  <a class="home" href="/">&#8962; index</a>
  <span class="page-name">{esc(rel_path)}</span>
  <span class="badge tier-{esc(tier)}">{esc(tier)}</span>
</header>
{fm_html}
<main class="content">
{html_body}
</main>
</body>
</html>
"""


def render_index_html(root: Path, entries) -> str:
    """Listing of all viewable pages (wiki/ + wiki/derived/ only; raw/ excluded
    because scan_pages only walks wiki/ — plan §1-A, Q4)."""
    rows = ""
    for pe in entries:
        rows += (
            f'<li><a class="wikilink" href="{esc(page_url(pe.rel_path))}">'
            f'{esc(pe.rel_path)}</a> '
            f'<span class="badge tier-{esc(pe.tier)}">{esc(pe.tier)}</span></li>\n'
        )
    index_link = ""
    # surface the root page (index.md) prominently if present
    for pe in entries:
        if pe.rel_path == "wiki/index.md":
            index_link = (
                f'<p class="root-page"><a class="wikilink" '
                f'href="{esc(page_url(pe.rel_path))}">&#9733; index.md (root)</a></p>'
            )
            break
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>llm-wiki</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<header><span class="page-name">llm-wiki</span>
<span class="meta">{len(entries)} pages</span></header>
{index_link}
<main class="content"><ul class="page-list">{rows}</ul></main>
</body>
</html>
"""


PAGE_CSS = """\
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  background:#f7fafc;color:#1a202c;margin:0;line-height:1.6}
header{background:#1a202c;color:#fff;padding:10px 20px;display:flex;
  align-items:center;gap:12px;flex-wrap:wrap}
header .home{color:#90cdf4;text-decoration:none;font-weight:600}
header .home:hover{text-decoration:underline}
.page-name{font-family:monospace;font-size:14px;color:#e2e8f0}
.meta{color:#a0aec0;font-size:12px}
.badge{font-size:10px;font-weight:700;color:#fff;padding:2px 7px;border-radius:3px;
  text-transform:uppercase;letter-spacing:.04em}
.tier-source{background:#2b6cb0}
.tier-derived{background:#6b46c1}
.frontmatter{background:#edf2f7;padding:6px 20px;display:flex;gap:16px;flex-wrap:wrap;
  font-size:12px;color:#4a5568;border-bottom:1px solid #e2e8f0}
.fm-item b{color:#2d3748}
.content{max-width:860px;margin:0 auto;padding:24px 20px}
.content table{border-collapse:collapse;margin:12px 0}
.content th,.content td{border:1px solid #cbd5e0;padding:6px 12px}
.content th{background:#edf2f7}
.content pre{background:#1a202c;color:#e2e8f0;padding:12px;border-radius:6px;overflow-x:auto}
.content code{font-family:monospace}
.content :not(pre)>code{background:#edf2f7;padding:1px 5px;border-radius:3px}
a.wikilink{color:#3182ce;text-decoration:none}
a.wikilink:hover{text-decoration:underline}
.wikilink.missing{color:#c53030;border-bottom:1px dashed #c53030;cursor:not-allowed}
.wikilink-group .tierhint{font-size:.85em;color:#718096}
ul.page-list{list-style:none;padding:0}
ul.page-list li{padding:5px 0;border-bottom:1px solid #edf2f7;display:flex;
  align-items:center;gap:8px}
.root-page{max-width:860px;margin:16px auto 0;padding:0 20px;font-size:16px}
.root-page a{font-weight:700}
"""


# ── server ───────────────────────────────────────────────────────────────────

class ExclusiveHTTPServer(HTTPServer):
    """HTTPServer that refuses to co-bind an in-use port.

    ``socketserver.TCPServer`` defaults ``allow_reuse_address = True`` (sets
    ``SO_REUSEADDR``), which on Windows lets a SECOND viewer bind the SAME
    ``127.0.0.1:PORT`` as a still-running one. Two servers then listen on the
    fixed default port and the OS routes each browser connection to one of them
    non-deterministically — so a stale viewer left over from another wiki
    silently serves the WRONG wiki (observed in E2E: the top page showed an
    unrelated wiki's pages). Disabling reuse makes a second bind fail cleanly
    with ``OSError`` so ``main`` reports "port in use — retry with --port"
    instead of masquerading as the active wiki.
    """

    allow_reuse_address = False


def build_app(root: Path):
    """Construct the request handler bound to a wiki root. Pure factory — does
    not bind a socket (server smoke can call this without serving)."""

    def viewable() -> list:
        return wiki_index.scan_pages(root)

    def graph_pages() -> dict:
        return link_lint.build_graph(root).pages

    class WikiViewHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            # DNS-rebinding hardening: the ONLY legitimate way to reach this
            # 127.0.0.1-bound server is via a loopback hostname. A rebinding
            # attack arrives with the attacker's Host header — refuse it before
            # touching any wiki state. urlsplit lowercases, strips the port and
            # the [] of a bracketed IPv6 literal; a missing Host fails closed.
            from urllib.parse import urlsplit
            raw_host = self.headers.get("Host") or ""
            try:
                hostname = urlsplit(f"//{raw_host}").hostname
            except ValueError:
                hostname = None
            if hostname not in ALLOWED_HOSTS:
                self._respond(403, b"forbidden: bad Host")
                return
            parsed = urlparse(self.path)
            if parsed.path in ("/", ""):
                self._html(render_index_html(root, viewable()))
                return
            if parsed.path == "/page":
                qs = parse_qs(parsed.query)
                rel = (qs.get("path") or [""])[0]
                entries = viewable()
                allowed = {pe.rel_path for pe in entries}
                if rel not in allowed:
                    # raw/ and anything outside wiki/ is not viewable (Q4)
                    self._respond(404, b"page not found")
                    return
                pages = {pe.rel_path: pe.rel_path for pe in entries}
                # build the basename graph from link_lint (REUSE)
                self._html(render_page_html(root, rel, graph_pages()))
                return
            self._respond(404, b"not found")

        def _html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # Second XSS layer (belt = nh3 in render_page_html): even a
            # sanitizer miss cannot run script, fetch out, or submit forms.
            self.send_header("Content-Security-Policy", CSP_POLICY)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _respond(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:   # suppress request logs
            pass

    return WikiViewHandler


def open_browser(url: str) -> None:
    import subprocess
    if sys.platform == "win32":
        os.startfile(url)   # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    else:
        subprocess.run(["xdg-open", url], check=False)


def main(argv: list[str] | None = None) -> int:
    # Fix stdio to UTF-8 regardless of the host locale (S1; same idiom as
    # cli.py:main — non-ASCII roots/paths print on stderr).
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Serve a local llm-wiki viewer.")
    parser.add_argument(
        "--serve", action="store_true",
        help=f"Serve on http://{SERVE_HOST}:{DEFAULT_PORT}/ (127.0.0.1 only)",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port to bind (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--no-open", action="store_true", help="Do not open the browser",
    )
    parser.add_argument(
        "--root", default=None,
        help="Explicit wiki root (top override). If omitted, the wiki root is "
             "resolved by wiki_root_resolver (prompt>pj>workspace>cwd).",
    )
    args = parser.parse_args(argv)

    resolution = wiki_root_resolver.resolve(args.root)
    if resolution is None:
        print(
            "error: no wiki resolved (prompt>pj>workspace>cwd all empty). "
            "Pass --root <path> or run from a wiki root.",
            file=sys.stderr,
        )
        return 2
    root = resolution.root

    entries = wiki_index.scan_pages(root)

    if not args.serve:
        print("error: --serve is required", file=sys.stderr)
        return 2

    handler = build_app(root)
    try:
        server = ExclusiveHTTPServer((SERVE_HOST, args.port), handler)
    except OSError as e:
        print(
            f"error: cannot bind {SERVE_HOST}:{args.port} ({e}). "
            f"The port is already in use — most likely a wiki-view server is "
            f"still running (possibly for a DIFFERENT wiki). Stop it "
            f"(`pkill -f \"llmwiki-view view --serve\"`) or retry with "
            f"--port <other> (e.g. --port {args.port + 1}).",
            file=sys.stderr,
        )
        return 2

    url = f"http://{SERVE_HOST}:{args.port}/"
    # summary line the skill greps (mirrors kanban's "serving" line)
    print(
        f"[wiki-view] serving {len(entries)} pages at {url} (Ctrl+C to stop)",
        file=sys.stderr,
    )
    if not args.no_open:
        open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[wiki-view] stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
