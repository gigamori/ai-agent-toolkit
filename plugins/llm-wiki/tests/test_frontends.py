"""Tests: normalization front-ends FE-A / FE-B / FE-B' (design §4, D12/D16/D18).

Covers: each FE lands at the correct raw path with correct provenance/origin;
redaction runs before hashing (a secret changes the hash and is flagged);
same-content re-ingest is a no-op (exists True); source_ref.raw_path is relative.
"""
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
    assert not sr["raw_path"].startswith("/")          # always relative (D12)
    assert ":" not in sr["raw_path"].split("/")[0]     # no drive letter
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
    # The body is redacted, and the hash is over the redacted body.
    assert "AKIAIOSFODNN7EXAMPLE" not in res.body
    assert res.redaction_flags
    assert res.hash == ch.content_hash(res.body)
    # The hash differs from the raw (un-redacted) content's hash.
    assert res.hash != ch.content_hash(secret)
    # Identical-after-redaction text would collide; but distinct clean text does not.
    assert res.hash != frontends.fe_a(tmp_path, clean).hash


def test_same_content_reingest_is_no_op(tmp_path):
    derived = tmp_path / "raw" / "derived"
    derived.mkdir(parents=True)
    first = frontends.fe_a(tmp_path, "stable content")
    # Simulate the artifact having been written by a prior ingest.
    (tmp_path / first.rel_path).write_text(first.body, encoding="utf-8")
    second = frontends.fe_a(tmp_path, "stable content")
    assert second.hash == first.hash
    assert second.exists is True   # dedup no-op
