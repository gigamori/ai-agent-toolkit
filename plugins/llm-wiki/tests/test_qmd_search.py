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
