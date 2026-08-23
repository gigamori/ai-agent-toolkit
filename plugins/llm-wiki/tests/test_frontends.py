from llmwiki.ingest import frontends
from llmwiki.core import content_hash as ch


def test_fe_a_derived_conversation(tmp_path):
    res = frontends.fe_a(tmp_path, "a filing note")
    assert res.rel_path.startswith("raw/derived/")
    assert res.rel_path.endswith(".md")
    assert res.frontmatter["provenance"] == "derived"
    assert res.frontmatter["derived_origin"] == "conversation"
    assert res.exists is False


def test_fe_b_source_with_relative_source_ref(tmp_path):
    res = frontends.fe_b(tmp_path, "third party content", "txt",
                         external_locator="https://example.com/x")
    assert res.rel_path.startswith("raw/")
    assert not res.rel_path.startswith("raw/derived/")
    assert res.rel_path.endswith(".txt")
    assert res.frontmatter["provenance"] == "source"
    sr = res.frontmatter["source_ref"]
    assert sr["raw_path"] == res.rel_path
    assert not sr["raw_path"].startswith("/"), "source_ref.raw_path stays wiki-relative"
    assert ":" not in sr["raw_path"].split("/")[0], "source_ref.raw_path carries no drive letter"
    assert sr["external_locator"] == "https://example.com/x"


def test_fe_b_prime_cc_log_transcript(tmp_path):
    res = frontends.fe_b_prime(tmp_path, "# transcript\n**Human**: hi")
    assert res.rel_path.startswith("raw/derived/")
    assert res.frontmatter["provenance"] == "derived"
    assert res.frontmatter["derived_origin"] == "cc-log"
    assert res.frontmatter["doc_type"] == "transcript"


def test_redaction_runs_before_hash(tmp_path):
    secret = "token AKIAIOSFODNN7EXAMPLE end"
    clean = "token XXXX end"
    res = frontends.fe_a(tmp_path, secret)
    assert "AKIAIOSFODNN7EXAMPLE" not in res.body
    assert res.redaction_flags
    assert res.hash == ch.content_hash(res.body)
    assert res.hash != ch.content_hash(secret)
    assert res.hash != frontends.fe_a(tmp_path, clean).hash


def test_same_content_reingest_is_no_op(tmp_path):
    derived = tmp_path / "raw" / "derived"
    derived.mkdir(parents=True)
    first = frontends.fe_a(tmp_path, "stable content")
    (tmp_path / first.rel_path).write_text(first.body, encoding="utf-8")
    second = frontends.fe_a(tmp_path, "stable content")
    assert second.hash == first.hash
    assert second.exists is True
