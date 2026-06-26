"""Tests: ingest_driver `enumerate` verb — read-only glob enumeration.

Covers (plan §2 A-3 completion criteria, G-a/G-b/G-e/G-d):
  - glob expansion is Python-side / deterministic (sorted, OS-independent);
  - G-b internal paths are force-excluded (raw/x.md, wiki/y.md, .git/*,
    SCHEMA.md, .llmwiki, log.md, index.md are dropped);
  - a directory-only argument applies the text-type extension allowlist
    (a `.png` is dropped, a `.md`/`.json` kept);
  - `**` recurses;
  - zero matches is an explicit error (DriverError), not an empty success;
  - the verb is read-only (no .llmwiki.lock / .llmwiki.txn created).

AUTHORED ONLY — not run here (TC/debug owns execution). No git fixture is
needed: `enumerate` takes no lock, writes nothing, and does not require a wiki
marker — it only enumerates a directory tree.
"""
import pytest

import ingest_driver as drv


def _touch(path, body="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# glob expansion is Python-side + deterministic (G-a)
# --------------------------------------------------------------------------- #
def test_explicit_glob_expands_in_python_sorted(tmp_path):
    _touch(tmp_path / "b.md")
    _touch(tmp_path / "a.md")
    _touch(tmp_path / "c.txt")     # not matched by *.md

    out = drv.enumerate_files(str(tmp_path), "*.md")
    # Deterministic order (sorted), Python-side expansion (not shell).
    assert out["files"] == ["a.md", "b.md"]
    assert out["pattern"] == "*.md"
    assert out["excluded"] == 0


# --------------------------------------------------------------------------- #
# G-b: wiki-internal paths are force-excluded
# --------------------------------------------------------------------------- #
def test_internal_paths_excluded(tmp_path):
    # Real document we want.
    _touch(tmp_path / "docs" / "keep.md")
    # Internal paths that must be dropped (G-b).
    _touch(tmp_path / "raw" / "x.md")
    _touch(tmp_path / "wiki" / "y.md")
    _touch(tmp_path / "wiki" / "derived" / "z.md")   # nested under wiki/
    _touch(tmp_path / ".git" / "config")
    _touch(tmp_path / "SCHEMA.md")
    _touch(tmp_path / ".llmwiki")
    _touch(tmp_path / "log.md")
    _touch(tmp_path / "index.md")

    out = drv.enumerate_files(str(tmp_path), "**/*.md")
    assert out["files"] == ["docs/keep.md"]
    # raw/x.md, wiki/y.md, wiki/derived/z.md, SCHEMA.md, log.md, index.md dropped.
    assert out["excluded"] >= 6


def test_nested_schema_md_excluded(tmp_path):
    # SCHEMA.md is dropped wherever it appears in the tree, not just at root.
    _touch(tmp_path / "docs" / "real.md")
    _touch(tmp_path / "docs" / "sub" / "SCHEMA.md")

    out = drv.enumerate_files(str(tmp_path), "**/*.md")
    assert out["files"] == ["docs/real.md"]
    assert out["excluded"] == 1


# --------------------------------------------------------------------------- #
# G-e: directory-only argument applies the text-type allowlist
# --------------------------------------------------------------------------- #
def test_directory_only_applies_text_allowlist(tmp_path):
    docs = tmp_path / "docs"
    _touch(docs / "note.md")
    _touch(docs / "data.json")
    _touch(docs / "log.txt")
    _touch(docs / "image.png")        # non-text -> dropped (G-e)
    _touch(docs / "archive.zip")      # non-text -> dropped (G-e)

    out = drv.enumerate_files(str(tmp_path), "docs/")
    assert out["files"] == ["docs/data.json", "docs/log.txt", "docs/note.md"]
    # image.png + archive.zip excluded by the allowlist.
    assert out["excluded"] == 2
    # Directory-only sugar expands to <dir>/**/*.
    assert out["pattern"] == "docs/**/*"


def test_directory_only_without_trailing_slash(tmp_path):
    # A metacharacter-free token resolving to an existing dir is also dir-only.
    docs = tmp_path / "docs"
    _touch(docs / "note.md")
    _touch(docs / "image.png")

    out = drv.enumerate_files(str(tmp_path), "docs")
    assert out["files"] == ["docs/note.md"]
    assert out["pattern"] == "docs/**/*"


def test_explicit_glob_with_extension_honored_no_allowlist(tmp_path):
    # An explicit glob with its own extension is honored as-is (allowlist NOT
    # applied) — only the G-b internal exclusions apply.
    _touch(tmp_path / "a.png")
    _touch(tmp_path / "b.png")

    out = drv.enumerate_files(str(tmp_path), "*.png")
    assert out["files"] == ["a.png", "b.png"]
    assert out["pattern"] == "*.png"


# --------------------------------------------------------------------------- #
# G-d: `**` recurses
# --------------------------------------------------------------------------- #
def test_doublestar_recurses(tmp_path):
    _touch(tmp_path / "top.md")
    _touch(tmp_path / "a" / "mid.md")
    _touch(tmp_path / "a" / "b" / "deep.md")

    out = drv.enumerate_files(str(tmp_path), "**/*.md")
    assert out["files"] == ["a/b/deep.md", "a/mid.md", "top.md"]


# --------------------------------------------------------------------------- #
# G-d: zero matches is an explicit error, not an empty success
# --------------------------------------------------------------------------- #
def test_zero_match_is_error(tmp_path):
    _touch(tmp_path / "a.txt")
    with pytest.raises(drv.DriverError):
        drv.enumerate_files(str(tmp_path), "*.md")


def test_all_excluded_is_error(tmp_path):
    # Even if the glob matched files, if every match is internal (G-b) the
    # result is empty -> explicit error.
    _touch(tmp_path / "raw" / "only.md")
    with pytest.raises(drv.DriverError):
        drv.enumerate_files(str(tmp_path), "**/*.md")


# --------------------------------------------------------------------------- #
# directory entries are dropped (files only)
# --------------------------------------------------------------------------- #
def test_files_only_directories_not_emitted(tmp_path):
    _touch(tmp_path / "docs" / "keep.md")
    (tmp_path / "docs" / "sub").mkdir(parents=True, exist_ok=True)

    out = drv.enumerate_files(str(tmp_path), "docs/**/*")
    # The `sub` directory entry is not emitted; only the file is.
    assert out["files"] == ["docs/keep.md"]


# --------------------------------------------------------------------------- #
# read-only: no lock / sidecar side effects
# --------------------------------------------------------------------------- #
def test_enumerate_is_read_only(tmp_path):
    import transaction as tx
    _touch(tmp_path / "docs" / "keep.md")
    drv.enumerate_files(str(tmp_path), "**/*.md")
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
