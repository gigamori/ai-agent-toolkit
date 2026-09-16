import os

import pytest

from llmwiki.core import config_resolver as cr
from llmwiki.read import qmd_search as qs


def _make_wiki(tmp_path):
    (tmp_path / "wiki" / "derived").mkdir(parents=True)
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki" / "p1.md").write_text("p1", encoding="utf-8")
    (tmp_path / "wiki" / "p2.md").write_text("p2", encoding="utf-8")
    (tmp_path / "wiki" / "derived" / "d1.md").write_text("d1", encoding="utf-8")
    (tmp_path / "wiki" / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "raw" / "secret.md").write_text("secret", encoding="utf-8")


def _uri(tmp_path, rel):
    return "qmd://" + str(tmp_path / rel)


def test_reconstruct_rel_absolute_qmd_uri(tmp_path):
    _make_wiki(tmp_path)
    assert qs._reconstruct_rel(_uri(tmp_path, "wiki/derived/d1.md"), tmp_path) \
        == "wiki/derived/d1.md"
    assert qs._reconstruct_rel(_uri(tmp_path, "wiki/p1.md"), tmp_path) == "wiki/p1.md"


def test_reconstruct_rel_outside_root_is_none(tmp_path):
    _make_wiki(tmp_path)
    outside = "qmd://" + str(tmp_path.parent / "elsewhere.md")
    assert qs._reconstruct_rel(outside, tmp_path) is None
    assert qs._reconstruct_rel("", tmp_path) is None


def test_hits_to_pages_drops_non_pages_and_tags_tier(tmp_path):
    _make_wiki(tmp_path)
    hits = [
        {"file": _uri(tmp_path, "wiki/derived/d1.md")},
        {"file": _uri(tmp_path, "wiki/README.md")},
        {"file": _uri(tmp_path, "raw/secret.md")},
        {"file": _uri(tmp_path, "wiki/p1.md")},
        {"file": _uri(tmp_path, "wiki/p1.md")},
        {"file": _uri(tmp_path, "wiki/p2.md")},
    ]
    out = qs._hits_to_pages(hits, tmp_path, k=10)
    assert out == [
        ("derived", "wiki/derived/d1.md"),
        ("source", "wiki/p1.md"),
        ("source", "wiki/p2.md"),
    ]
    rels = [rel for _, rel in out]
    assert "wiki/README.md" not in rels
    assert "raw/secret.md" not in rels


def test_hits_to_pages_trims_to_k_preserving_rank(tmp_path):
    _make_wiki(tmp_path)
    hits = [
        {"file": _uri(tmp_path, "wiki/p2.md")},
        {"file": _uri(tmp_path, "wiki/README.md")},
        {"file": _uri(tmp_path, "wiki/p1.md")},
        {"file": _uri(tmp_path, "wiki/derived/d1.md")},
    ]
    out = qs._hits_to_pages(hits, tmp_path, k=2)
    assert out == [("source", "wiki/p2.md"), ("source", "wiki/p1.md")], (
        "non-pages are dropped before trimming, so a dropped hit never consumes a slot"
    )


def test_hits_to_pages_tier_matches_tier_of(tmp_path):
    from llmwiki.core import wiki_index
    _make_wiki(tmp_path)
    hits = [{"file": _uri(tmp_path, "wiki/derived/d1.md")},
            {"file": _uri(tmp_path, "wiki/p1.md")}]
    for tier, rel in qs._hits_to_pages(hits, tmp_path, k=10):
        assert tier == wiki_index.tier_of(rel)


def _res(backend="qmd", qmd_bin="qmd", threshold="0"):
    return {
        "search_backend": cr.Resolution("search_backend", backend, "wiki"),
        "qmd_bin": cr.Resolution("qmd_bin", qmd_bin, "default"),
        "qmd_page_threshold": cr.Resolution("qmd_page_threshold", threshold, "wiki"),
    }


def test_should_use_false_when_backend_index(tmp_path, monkeypatch):
    _make_wiki(tmp_path)
    monkeypatch.setattr(qs, "is_available", lambda b: True)
    assert qs.should_use(tmp_path, _res(backend="index")) is False


def test_should_use_false_when_bin_absent(tmp_path, monkeypatch):
    _make_wiki(tmp_path)
    monkeypatch.setattr(qs, "is_available", lambda b: False)
    assert qs.should_use(tmp_path, _res(backend="qmd", threshold="0")) is False


def test_should_use_threshold_boundary(tmp_path, monkeypatch):
    _make_wiki(tmp_path)
    monkeypatch.setattr(qs, "is_available", lambda b: True)
    assert qs.should_use(tmp_path, _res(threshold="3")) is False, (
        "the fixture holds 3 pages and the predicate is strictly greater-than"
    )
    assert qs.should_use(tmp_path, _res(threshold="2")) is True


def test_should_use_non_integer_threshold_is_false(tmp_path, monkeypatch):
    _make_wiki(tmp_path)
    monkeypatch.setattr(qs, "is_available", lambda b: True)
    assert qs.should_use(tmp_path, _res(threshold="lots")) is False


def test_is_available_false_for_missing_binary():
    assert qs.is_available("llmwiki-no-such-qmd-binary-xyz") is False


def test_is_initialized_reflects_dot_qmd_dir(tmp_path):
    assert qs.is_initialized(tmp_path) is False
    (tmp_path / ".qmd").mkdir()
    assert qs.is_initialized(tmp_path) is True


def test_run_resolves_binary_sets_pwd_and_utf8(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw

        class _R:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return _R()

    monkeypatch.setattr(qs.subprocess, "run", fake_run)
    monkeypatch.setattr(qs.shutil, "which", lambda b: r"/resolved/qmd.CMD")
    qs._run("qmd", ["init"], tmp_path)

    assert captured["argv"][0] == r"/resolved/qmd.CMD"
    assert captured["kw"]["env"]["PWD"] == os.path.abspath(str(tmp_path)), (
        "PWD is set explicitly because the child resolves its index from PWD, not cwd"
    )
    assert captured["kw"]["cwd"] == str(tmp_path)
    assert captured["kw"]["encoding"] == "utf-8"
    assert captured["kw"]["errors"] == "replace"


def test_query_raises_qmderror_when_binary_missing(tmp_path):
    _make_wiki(tmp_path)
    with pytest.raises(qs.QmdError):
        qs.query(tmp_path, "llmwiki-no-such-qmd-binary-xyz", "anything")


def test_ensure_collection_raises_qmderror_when_binary_missing(tmp_path):
    _make_wiki(tmp_path)
    with pytest.raises(qs.QmdError):
        qs.ensure_collection(tmp_path, "llmwiki-no-such-qmd-binary-xyz")


def test_query_doc_puts_vec_first_then_one_lex_per_term():
    lines = qs._query_doc("how does the ingest journal roll back").splitlines()
    assert lines[0] == "vec: how does the ingest journal roll back"
    assert lines[1:] == ["lex: ingest", "lex: journal", "lex: roll", "lex: back"], (
        "qmd ANDs terms inside one lex: line, so terms go on separate lines"
    )


def test_query_doc_strips_chars_qmd_rejects_in_vec():
    assert '"' not in qs._query_doc('the "write allowlist" gate')
    assert qs._query_doc("ingest -rollback").splitlines()[0] == "vec: ingest rollback", (
        "a `-` after whitespace parses as an unsupported negation and exits 1"
    )
    assert qs._query_doc("-leading hyphen").splitlines()[0] == "vec: leading hyphen"


def test_query_doc_splits_cjk_on_hiragana():
    lines = qs._query_doc("書き込みゲートの設計").splitlines()
    assert lines[0] == "vec: 書き込みゲートの設計"
    assert lines[1:] == ["lex: ゲート", "lex: 設計"], (
        "qmd matches CJK as an exact substring with no word segmentation, so "
        "the runs between inflections are the units worth sending"
    )


def test_query_doc_caps_terms_and_drops_stopwords():
    terms = qs._lex_terms("what are the " + " ".join(f"term{i}" for i in range(20)))
    assert len(terms) == qs._LEX_TERMS_MAX
    assert "what" not in terms and "are" not in terms and "the" not in terms


def test_query_sends_typed_doc_and_skips_rerank(tmp_path, monkeypatch):
    _make_wiki(tmp_path)
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv

        class _R:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return _R()

    monkeypatch.setattr(qs.subprocess, "run", fake_run)
    monkeypatch.setattr(qs.shutil, "which", lambda b: "/resolved/qmd")
    qs.query(tmp_path, "qmd", "rollback journal", k=5)

    assert captured["argv"][1] == "query"
    assert captured["argv"][2] == "vec: rollback journal\nlex: rollback\nlex: journal", (
        "a typed document routes through structuredSearch, which never invokes "
        "query expansion; bare text invokes it whenever BM25 is unsure"
    )
    assert "--no-rerank" in captured["argv"], (
        "the reranker is the second model-backed cost source on the read path"
    )
    assert captured["argv"][-2:] == ["-n", str(5 + qs._OVERFETCH_MARGIN)]


def test_launcher_unwraps_windows_shim_to_node(tmp_path, monkeypatch):
    entry = tmp_path / "node_modules" / "@tobilu" / "qmd" / "bin" / "qmd"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    shim = tmp_path / "qmd.CMD"
    shim.write_text(
        r'SET "_prog=%dp0%\node.exe"' "\r\n"
        r'"%_prog%"  "%dp0%\node_modules\@tobilu\qmd\bin\qmd" %*' "\r\n",
        encoding="utf-8")

    monkeypatch.setattr(qs.shutil, "which",
                        lambda b: str(shim) if b == "qmd" else "/usr/bin/node")

    assert qs._launcher("qmd") == ["/usr/bin/node", str(entry)], (
        "cmd.exe truncates a command line at a newline, so the multi-line typed "
        "query document must not be passed through the shim"
    )


def test_launcher_passes_through_a_real_binary(monkeypatch):
    monkeypatch.setattr(qs.shutil, "which", lambda b: "/usr/local/bin/qmd")
    assert qs._launcher("qmd") == ["/usr/local/bin/qmd"]


def test_launcher_falls_back_when_shim_target_missing(tmp_path, monkeypatch):
    shim = tmp_path / "qmd.CMD"
    shim.write_text(r'"%_prog%"  "%dp0%\node_modules\gone\bin\qmd" %*' "\r\n",
                    encoding="utf-8")
    monkeypatch.setattr(qs.shutil, "which", lambda b: str(shim))
    assert qs._launcher("qmd") == [str(shim)]


def test_ensure_collection_passes_no_timeout(tmp_path, monkeypatch):
    _make_wiki(tmp_path)
    timeouts = []

    def fake_run(argv, **kw):
        timeouts.append(kw["timeout"])

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(qs.subprocess, "run", fake_run)
    monkeypatch.setattr(qs.shutil, "which", lambda b: "/resolved/qmd")
    qs.ensure_collection(tmp_path, "qmd")

    assert timeouts == [None, None, None], (
        "qmd embed is one-time index construction; a ceiling would abort the "
        "build partway rather than make it faster, so _QUERY_TIMEOUT bounds "
        "the query only"
    )
