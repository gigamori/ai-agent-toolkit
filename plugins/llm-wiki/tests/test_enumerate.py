import pytest

from llmwiki.ingest import ingest_driver as drv


def _touch(path, body="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_explicit_glob_expands_in_python_sorted(tmp_path):
    _touch(tmp_path / "b.md")
    _touch(tmp_path / "a.md")
    _touch(tmp_path / "c.txt")

    out = drv.enumerate_files(str(tmp_path), "*.md")
    assert out["files"] == ["a.md", "b.md"]
    assert out["pattern"] == "*.md"
    assert out["excluded"] == 0


def test_internal_paths_excluded(tmp_path):
    _touch(tmp_path / "docs" / "keep.md")
    _touch(tmp_path / "raw" / "x.md")
    _touch(tmp_path / "wiki" / "y.md")
    _touch(tmp_path / "wiki" / "derived" / "z.md")
    _touch(tmp_path / ".git" / "config")
    _touch(tmp_path / "SCHEMA.md")
    _touch(tmp_path / ".llmwiki")
    _touch(tmp_path / "log.md")
    _touch(tmp_path / "index.md")

    out = drv.enumerate_files(str(tmp_path), "**/*.md")
    assert out["files"] == ["docs/keep.md"]
    assert out["excluded"] >= 6


def test_qmd_dir_excluded(tmp_path):
    _touch(tmp_path / "docs" / "keep.md")
    _touch(tmp_path / ".qmd" / "index.yml")
    _touch(tmp_path / ".qmd" / "index.sqlite")

    out = drv.enumerate_files(str(tmp_path), "**/*")
    assert out["files"] == ["docs/keep.md"]
    assert ".qmd/index.yml" not in out["files"]


def test_nested_schema_md_excluded(tmp_path):
    _touch(tmp_path / "docs" / "real.md")
    _touch(tmp_path / "docs" / "sub" / "SCHEMA.md")

    out = drv.enumerate_files(str(tmp_path), "**/*.md")
    assert out["files"] == ["docs/real.md"]
    assert out["excluded"] == 1


def test_directory_only_applies_text_allowlist(tmp_path):
    docs = tmp_path / "docs"
    _touch(docs / "note.md")
    _touch(docs / "data.json")
    _touch(docs / "log.txt")
    _touch(docs / "image.png")
    _touch(docs / "archive.zip")

    out = drv.enumerate_files(str(tmp_path), "docs/")
    assert out["files"] == ["docs/data.json", "docs/log.txt", "docs/note.md"]
    assert out["excluded"] == 2
    assert out["pattern"] == "docs/**/*"


def test_enumerate_dir_keeps_jsonl(tmp_path):
    docs = tmp_path / "docs"
    _touch(docs / "a.jsonl")
    _touch(docs / "b.txt")

    out = drv.enumerate_files(str(tmp_path), "docs/")
    assert "docs/a.jsonl" in out["files"], (
        "the text allowlist keeps .jsonl so a directory enumeration still surfaces "
        "session logs for the per-file kind gate to refuse loudly"
    )


def test_directory_only_without_trailing_slash(tmp_path):
    docs = tmp_path / "docs"
    _touch(docs / "note.md")
    _touch(docs / "image.png")

    out = drv.enumerate_files(str(tmp_path), "docs")
    assert out["files"] == ["docs/note.md"]
    assert out["pattern"] == "docs/**/*"


def test_explicit_glob_with_extension_honored_no_allowlist(tmp_path):
    _touch(tmp_path / "a.png")
    _touch(tmp_path / "b.png")

    out = drv.enumerate_files(str(tmp_path), "*.png")
    assert out["files"] == ["a.png", "b.png"]
    assert out["pattern"] == "*.png"


def test_doublestar_recurses(tmp_path):
    _touch(tmp_path / "top.md")
    _touch(tmp_path / "a" / "mid.md")
    _touch(tmp_path / "a" / "b" / "deep.md")

    out = drv.enumerate_files(str(tmp_path), "**/*.md")
    assert out["files"] == ["a/b/deep.md", "a/mid.md", "top.md"]


def test_zero_match_is_error(tmp_path):
    _touch(tmp_path / "a.txt")
    with pytest.raises(drv.DriverError):
        drv.enumerate_files(str(tmp_path), "*.md")


def test_all_excluded_is_error(tmp_path):
    _touch(tmp_path / "raw" / "only.md")
    with pytest.raises(drv.DriverError):
        drv.enumerate_files(str(tmp_path), "**/*.md")


def test_files_only_directories_not_emitted(tmp_path):
    _touch(tmp_path / "docs" / "keep.md")
    (tmp_path / "docs" / "sub").mkdir(parents=True, exist_ok=True)

    out = drv.enumerate_files(str(tmp_path), "docs/**/*")
    assert out["files"] == ["docs/keep.md"]


def test_enumerate_is_read_only(tmp_path):
    from llmwiki.write import transaction as tx
    _touch(tmp_path / "docs" / "keep.md")
    drv.enumerate_files(str(tmp_path), "**/*.md")
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
