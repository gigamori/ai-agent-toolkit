import inspect

import pytest

from llmwiki.core import wiki_index
from llmwiki.write import write_tool as wt
from llmwiki.write import transaction as tx


def test_tier_of_signature_is_path_only():
    sig = inspect.signature(wiki_index.tier_of)
    assert list(sig.parameters) == ["rel_path"], (
        "this signature is frozen: a failure here is public-contract drift to "
        "investigate, not a test to update"
    )


def test_tier_of_depends_on_path_only():
    assert wiki_index.tier_of("wiki/foo.md") == "source"
    assert wiki_index.tier_of("wiki/derived/foo.md") == "derived"
    assert wiki_index.tier_of("wiki\\derived\\foo.md") == "derived"
    assert wiki_index.tier_of("wiki/does/not/exist.md") == "source"
    assert wiki_index.tier_of("wiki/derived-notes.md") == "source"


def test_scan_pages_signature():
    sig = inspect.signature(wiki_index.scan_pages)
    assert list(sig.parameters) == ["wiki_root"], (
        "this signature is frozen: a failure here is public-contract drift to "
        "investigate, not a test to update"
    )


def _make_wiki(tmp_path):
    (tmp_path / "wiki" / "derived").mkdir(parents=True)
    (tmp_path / "wiki" / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "wiki" / "derived" / "b.md").write_text("b", encoding="utf-8")


def test_scan_pages_is_page_ness_authority(tmp_path):
    _make_wiki(tmp_path)
    (tmp_path / "wiki" / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "wiki" / "notes.txt").write_text("txt", encoding="utf-8")
    (tmp_path / "wiki" / "derived" / "README.md").write_text("r", encoding="utf-8")
    pages = {pe.rel_path: pe.tier for pe in wiki_index.scan_pages(tmp_path)}
    assert pages == {"wiki/a.md": "source", "wiki/derived/b.md": "derived"}
    assert "wiki/README.md" not in pages
    assert "wiki/derived/README.md" not in pages
    assert "wiki/notes.txt" not in pages


def test_scan_pages_empty_when_no_wiki(tmp_path):
    assert wiki_index.scan_pages(tmp_path) == []


def test_scan_pages_tier_matches_tier_of(tmp_path):
    _make_wiki(tmp_path)
    for pe in wiki_index.scan_pages(tmp_path):
        assert pe.tier == wiki_index.tier_of(pe.rel_path)


def test_classify_target_signature():
    sig = inspect.signature(wt.classify_target)
    assert list(sig.parameters) == ["rel_path"], (
        "this signature is frozen: a failure here is public-contract drift to "
        "investigate, not a test to update"
    )


@pytest.mark.parametrize("rel_path, gate", [
    ("/etc/passwd", "absolute"),
    ("C:/Windows/x.md", "absolute"),
    ("//host/share/x.md", "absolute"),
    ("wiki/../escape.md", "traversal"),
    ("../escape.md", "traversal"),
    ("raw/anything.md", "protected"),
    ("SCHEMA.md", "protected"),
    (".llmwiki", "protected"),
    ("wiki/SCHEMA.md", "protected"),
    ("outside.md", "path"),
    ("", "path"),
])
def test_classify_target_reject_set(rel_path, gate):
    chk = wt.classify_target(rel_path)
    assert chk.ok is False
    assert chk.gate == gate


@pytest.mark.parametrize("rel_path", [
    "wiki/page.md",
    "wiki/derived/page.md",
    "wiki/nested/deep/page.md",
])
def test_classify_target_accepts_in_namespace(rel_path):
    chk = wt.classify_target(rel_path)
    assert chk.ok is True
    assert chk.gate == ""


def test_classify_target_rejects_the_wiki_dir_itself():
    assert wt.classify_target("wiki").gate == "path"
    assert wt.classify_target("wiki/derived").gate == "path"


GATE_DOMAIN = {"path", "traversal", "absolute", "protected", "budget",
               "cross_namespace"}


def test_write_rejected_carries_reason_and_gate():
    e = wt.WriteRejected("because", "budget")
    assert e.reason == "because"
    assert e.gate == "budget"


def test_cross_namespace_gate_on_derived_origin_breakout(tmp_path):
    sess = wt.WriteSession(tmp_path, origin="derived")
    with pytest.raises(wt.WriteRejected) as ei:
        sess.add("wiki/source_page.md", "x")
    assert ei.value.gate == "cross_namespace"
    assert ei.value.gate in GATE_DOMAIN


def test_budget_gate_on_count_overflow(tmp_path):
    sess = wt.WriteSession(tmp_path, max_count=1)
    sess.add("wiki/a.md", "a")
    with pytest.raises(wt.WriteRejected) as ei:
        sess.add("wiki/b.md", "b")
    assert ei.value.gate == "budget"


def test_all_classify_reject_gates_are_in_domain():
    samples = ["/abs.md", "wiki/../e.md", "raw/x.md", "SCHEMA.md", "outside.md", ""]
    for s in samples:
        chk = wt.classify_target(s)
        if not chk.ok:
            assert chk.gate in GATE_DOMAIN


def test_transaction_is_a_context_manager():
    assert hasattr(tx.transaction, "__wrapped__") or callable(tx.transaction)
    sig = inspect.signature(tx.transaction)
    assert list(sig.parameters) == ["wiki_root", "message"]


def test_transaction_commits_on_normal_exit(tmp_path):
    (tmp_path / "wiki").mkdir(exist_ok=True)
    with tx.transaction(tmp_path, "commit-on-success"):
        tx.journal_before_write(tmp_path, ["wiki/p.md"])
        (tmp_path / "wiki" / "p.md").write_text("page", encoding="utf-8")
    assert (tmp_path / "wiki" / "p.md").exists()
    assert not (tmp_path / tx.JOURNAL_DIR).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


def test_transaction_rolls_back_on_exception(tmp_path):
    (tmp_path / "wiki").mkdir(exist_ok=True)
    with pytest.raises(RuntimeError):
        with tx.transaction(tmp_path, "rollback-on-failure"):
            tx.journal_before_write(tmp_path, ["wiki/partial.md"])
            (tmp_path / "wiki" / "partial.md").write_text("partial", encoding="utf-8")
            raise RuntimeError("boom")
    assert not (tmp_path / "wiki" / "partial.md").exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
