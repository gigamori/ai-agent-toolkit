"""CLI-level tests for the `search` (P5/DEC-3) and `reindex` (S5) verbs.

Model-free: the qmd-SELECTED-but-absent cases use a non-existent `qmd_bin` so the
D-Q8 predicate / loud-announce / no-op fallbacks are exercised WITHOUT installing
qmd or downloading its models. The live hybrid `qmd query` path (which needs the
~GB models) is left to acceptance, not unit tests.
"""
import contextlib
import io

import pytest

from llmwiki import cli


def _make_wiki(tmp_path, *, backend=None, qmd_bin=None):
    """A minimal wiki-root: `.llmwiki` marker + SCHEMA.md (config block) + pages."""
    (tmp_path / "wiki" / "derived").mkdir(parents=True)
    (tmp_path / "wiki" / "alpha.md").write_text("a", encoding="utf-8")
    (tmp_path / "wiki" / "derived" / "beta.md").write_text("b", encoding="utf-8")
    (tmp_path / "wiki" / "README.md").write_text("r", encoding="utf-8")
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    cfg = ["---", "config:"]
    if backend is not None:
        cfg.append(f"  search_backend: {backend}")
    if qmd_bin is not None:
        cfg.append(f"  qmd_bin: {qmd_bin}")
    cfg.append("  qmd_page_threshold: 0")   # any non-empty wiki exceeds 0
    cfg += ["---", "", "# SCHEMA"]
    (tmp_path / "SCHEMA.md").write_text("\n".join(cfg), encoding="utf-8")
    return str(tmp_path)


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- #
# search — index-direct byte-equivalence + usage
# --------------------------------------------------------------------------- #
def test_search_index_equals_scan_pages(tmp_path):
    root = _make_wiki(tmp_path, backend="index")
    _, scan, _ = _run(["scan-pages", root])
    rc, search, _ = _run(["search", root, "--q", "some phrased question"])
    assert rc == 0
    assert search == scan          # Invariant #4: index default is byte-identical
    assert "wiki/README.md" not in search


def test_search_requires_query(tmp_path):
    # A usage error is a contract violation, not a sentinel: it returns EX_USAGE
    # (64), NOT rc2 (theme1 i:39 exit-code split).
    root = _make_wiki(tmp_path, backend="index")
    rc, _, err = _run(["search", root])
    assert rc == cli.EX_USAGE and "usage: search" in err


def test_search_qmd_selected_but_binary_absent_falls_back_loud(tmp_path):
    # D-Q8: backend=qmd but the binary is unresolvable -> loud-announce (never
    # silent) + degrade to index-direct enumeration.
    root = _make_wiki(tmp_path, backend="qmd",
                      qmd_bin="llmwiki-no-such-qmd-binary-xyz")
    _, scan, _ = _run(["scan-pages", root])
    rc, search, err = _run(["search", root, "--q", "q"])
    assert rc == 0
    assert search == scan                      # fell back to index enumeration
    assert "[search]" in err and "not on PATH" in err   # loud-announced


# --------------------------------------------------------------------------- #
# reindex (S5) — no-op-safe, no crash
# --------------------------------------------------------------------------- #
def test_reindex_not_a_wiki(tmp_path):
    rc, _, err = _run(["reindex", str(tmp_path)])   # no .llmwiki marker
    assert rc == 2 and "NOT-A-WIKI" in err


def test_reindex_noop_when_backend_index(tmp_path):
    root = _make_wiki(tmp_path, backend="index")
    rc, out, _ = _run(["reindex", root])
    assert rc == 0
    assert "not qmd" in out and "nothing to reindex" in out


def test_reindex_noop_when_qmd_absent(tmp_path):
    root = _make_wiki(tmp_path, backend="qmd",
                      qmd_bin="llmwiki-no-such-qmd-binary-xyz")
    rc, out, _ = _run(["reindex", root])
    assert rc == 0                              # no crash
    assert "not on PATH" in out and "skipped" in out
