"""Tests: content-hash dedup (D18).

Covers: same content -> same hash; existing hash -> exists True (no-op);
new content -> exists False; supersedes link form.
"""
from llmwiki.core import content_hash as ch


def test_same_content_same_hash():
    assert ch.content_hash("hello") == ch.content_hash("hello")
    assert ch.content_hash(b"hello") == ch.content_hash("hello")


def test_different_content_different_hash():
    assert ch.content_hash("a") != ch.content_hash("b")


def test_filenames():
    h = ch.content_hash("x")
    assert ch.raw_filename(h, "txt") == f"{h}.txt"
    assert ch.raw_filename(h, ".md") == f"{h}.md"
    assert ch.derived_filename(h) == f"{h}.md"


def test_dedup_no_op_when_exists(tmp_path):
    h = ch.content_hash("payload")
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / f"{h}.txt").write_text("payload", encoding="utf-8")
    status = ch.dedup_status(tmp_path, "raw", h, "txt")
    assert status.exists is True
    assert status.rel_path == f"raw/{h}.txt"


def test_dedup_new_when_absent(tmp_path):
    h = ch.content_hash("fresh")
    status = ch.dedup_status(tmp_path, "raw/derived", h, "md")
    assert status.exists is False
    assert status.rel_path == f"raw/derived/{h}.md"


def test_supersedes_link():
    h = ch.content_hash("old")
    assert ch.supersedes_link(h, "md") == f"raw/{h}.md"
