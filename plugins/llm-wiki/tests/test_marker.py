from llmwiki.core import marker


def test_detect_present(tmp_path):
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n", encoding="utf-8")
    m = marker.detect(tmp_path)
    assert m is not None
    assert m.version == "1"
    assert m.schema_path.name == "SCHEMA.md"
    assert m.schema_path.parent == tmp_path
    assert marker.is_active(tmp_path) is True


def test_detect_absent(tmp_path):
    assert marker.detect(tmp_path) is None
    assert marker.is_active(tmp_path) is False


def test_detect_ignores_comments(tmp_path):
    (tmp_path / ".llmwiki").write_text(
        "# comment line\nversion: 2\nschema: WIKI.md\n", encoding="utf-8"
    )
    m = marker.detect(tmp_path)
    assert m.version == "2"
    assert m.schema_path.name == "WIKI.md"
