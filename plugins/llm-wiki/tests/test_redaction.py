"""Tests: redaction / secret-scan (D16).

Covers: known secrets and absolute paths are masked; clean text untouched;
deterministic placeholders keep dedup stable.
"""
from llmwiki.ingest import redaction


def test_masks_aws_key():
    r = redaction.redact("key AKIAIOSFODNN7EXAMPLE here")
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text
    assert redaction.PH_SECRET in r.text
    assert r.count == 1
    assert r.flags[0].kind == "secret"


def test_masks_github_token():
    r = redaction.redact("token ghp_0123456789abcdefghijABCDEFGHIJKLMN")
    assert "ghp_" not in r.text
    assert redaction.PH_SECRET in r.text


def test_masks_key_value_secret():
    r = redaction.redact("api_key = s3cr3tValue123")
    assert "s3cr3tValue123" not in r.text


def test_masks_windows_abs_path():
    r = redaction.redact(r"see C:\Users\alice\secret.txt for details")
    assert "alice" not in r.text
    assert redaction.PH_ABSPATH in r.text
    assert any(f.kind == "abs_path" for f in r.flags)


def test_masks_posix_home_path():
    r = redaction.redact("at /home/alice/.ssh/id_rsa now")
    assert "/home/alice" not in r.text
    assert redaction.PH_ABSPATH in r.text


def test_masks_tilde_path():
    r = redaction.redact("see ~/.aws/credentials")
    assert "~/.aws" not in r.text


def test_clean_text_untouched():
    r = redaction.redact("a normal sentence with relative path wiki/foo.md")
    assert r.count == 0
    assert r.text == "a normal sentence with relative path wiki/foo.md"
    assert redaction.is_clean("a normal sentence with relative path wiki/foo.md")


def test_deterministic_placeholder_keeps_hash_stable():
    a = redaction.redact("token AKIAIOSFODNN7EXAMPLE").text
    b = redaction.redact("token AKIAIOSFODNN7EXAMPLE").text
    assert a == b


def test_empty_and_non_str_safe():
    assert redaction.redact("").count == 0
    assert redaction.redact(None).text == ""
