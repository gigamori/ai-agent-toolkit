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



def test_passthrough_https_url():
    s = "see https://example.com/path here"
    r = redaction.redact(s)
    assert r.count == 0
    assert r.text == s


def test_passthrough_tilde_percent():
    s = "about ~50% done"
    r = redaction.redact(s)
    assert r.count == 0
    assert r.text == s


def test_passthrough_tilde_strikethrough():
    s = "~~strike~~ text"
    r = redaction.redact(s)
    assert r.count == 0
    assert r.text == s


def test_passthrough_tilde_range():
    s = "range 10~20 items"
    r = redaction.redact(s)
    assert r.count == 0
    assert r.text == s


def test_passthrough_markdown_link():
    s = "[label](https://example.com/x) markdown link"
    r = redaction.redact(s)
    assert r.count == 0
    assert r.text == s



def test_masks_windows_abs_path_no_lead():
    r = redaction.redact(r"open C:\Users\x\a.txt")
    assert redaction.PH_ABSPATH in r.text
    assert r"C:\Users" not in r.text


def test_masks_windows_abs_path_leading_space():
    r = redaction.redact(" C:/Users/x/a.txt")
    assert redaction.PH_ABSPATH in r.text
    assert "C:/Users" not in r.text


def test_masks_unc_path():
    r = redaction.redact(r"\\host\share\file")
    assert redaction.PH_ABSPATH in r.text
    assert "host" not in r.text


def test_masks_tilde_path_still_masks():
    r = redaction.redact("~/.aws/credentials")
    assert redaction.PH_ABSPATH in r.text
    assert "~/.aws" not in r.text


def test_masks_posix_home_path_still_masks():
    r = redaction.redact("/home/x/.ssh/id")
    assert redaction.PH_ABSPATH in r.text
    assert "/home/x" not in r.text



def test_flag_carries_masked_snippet_and_line_no_no_raw_bytes():
    text = "line one\napi_key = FAKEfake1234567890 trailing\nline three"
    r = redaction.redact(text)
    assert len(r.flags) >= 1
    f = r.flags[0]
    assert f.line_no == 2
    assert "FAKEfake1234567890" not in f.preview
    assert redaction.PH_SECRET in f.preview
    assert len(f.preview) <= 120



def test_flag_preview_no_raw_bytes_multi_secret_same_line_kv():
    text = "api_key = AAAAAAAA88 and token = BBBBBBBB99"
    r = redaction.redact(text)
    assert len(r.flags) == 2
    for f in r.flags:
        assert "AAAAAAAA88" not in f.preview
        assert "BBBBBBBB99" not in f.preview


def test_flag_preview_no_raw_bytes_two_aws_keys_same_line():
    text = "keys AKIAIOSFODNN7EXAMPLE and AKIA1234567890ABCDEF here"
    r = redaction.redact(text)
    assert len(r.flags) == 2
    for f in r.flags:
        assert "AKIAIOSFODNN7EXAMPLE" not in f.preview
        assert "AKIA1234567890ABCDEF" not in f.preview


def test_flag_preview_no_raw_bytes_two_winpaths_same_line():
    text = r"open C:\Users\alice\a.txt and C:\Users\bob\b.txt"
    r = redaction.redact(text)
    assert len(r.flags) == 2
    for f in r.flags:
        assert "alice" not in f.preview
        assert "bob" not in f.preview
