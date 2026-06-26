"""Tests: wiki initializer (plan T2, §2-B, W-c/W-d/W-e, R-1/R-6).

Covers (plan §2-B completion):
  - refuses to overwrite an existing `.llmwiki` (no-op / error);
  - copies the contract templates into the target root;
  - nested `git init` makes the root its OWN repo (a `.git` appears in it) +
    an initial commit (W-d);
  - parent-repo detection + the wiki-root's relative path lands in the parent's
    `.git/info/exclude`, and re-running is idempotent (W-e / R-1 / Q1).

Each git-dependent test builds a throwaway parent repo under tmp_path. git is
required; those tests skip when git is unavailable.

These tests AUTHOR the expectations only (T2: execute, no self-run; T4 runs).
"""
import shutil
import subprocess

import pytest

import wiki_init


def _git_available():
    return shutil.which("git") is not None


gitmark = pytest.mark.skipif(not _git_available(), reason="git not available")


def _init_parent_repo(path):
    """Make `path` a git repo with one commit; return a `g(*args)` runner."""
    def g(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True,
                       capture_output=True, text=True)
    path.mkdir(parents=True, exist_ok=True)
    g("init", "-q")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    g("config", "commit.gpgsign", "false")
    (path / "seed.md").write_text("seed", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "seed")
    return g


# --------------------------------------------------------------------------- #
# refuse to overwrite
# --------------------------------------------------------------------------- #
def test_refuses_to_overwrite_existing_wiki(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    (root / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n", encoding="utf-8")
    sentinel = root / "wiki" / "keep.md"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("do not touch", encoding="utf-8")

    with pytest.raises(wiki_init.WikiInitError):
        wiki_init.init_wiki(root, scope="prompt")

    # No-op: the pre-existing content is untouched.
    assert sentinel.read_text(encoding="utf-8") == "do not touch"


# --------------------------------------------------------------------------- #
# template copy
# --------------------------------------------------------------------------- #
@gitmark
def test_copies_contract_templates(tmp_path):
    root = tmp_path / "wiki"
    wiki_init.init_wiki(root, scope="prompt")

    assert (root / ".llmwiki").is_file()
    assert (root / "SCHEMA.md").is_file()
    assert (root / "index.md").is_file()
    assert (root / "log.md").is_file()
    assert (root / "raw").is_dir()
    assert (root / "wiki").is_dir()
    assert (root / "wiki" / "derived").is_dir()


# --------------------------------------------------------------------------- #
# nested git repo (W-d)
# --------------------------------------------------------------------------- #
@gitmark
def test_nested_git_init_makes_root_its_own_repo(tmp_path):
    root = tmp_path / "standalone"
    res = wiki_init.init_wiki(root, scope="prompt")

    # A `.git` appears in the wiki-root -> it is its own repo.
    assert (root / ".git").exists()
    toplevel = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert toplevel  # resolves to the wiki-root itself

    # An initial commit was made.
    count = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert int(count) == 1

    # No parent repo here -> nothing force-ignored.
    assert res.parent_repo is None
    assert res.exclude_entry is None


# --------------------------------------------------------------------------- #
# parent-repo detection + force-ignore (W-e / R-1 / Q1)
# --------------------------------------------------------------------------- #
@gitmark
def test_parent_repo_force_ignore_written(tmp_path):
    parent = tmp_path / "parent"
    _init_parent_repo(parent)
    root = parent / "sub" / "mywiki"

    res = wiki_init.init_wiki(root, scope="pj")

    # Parent repo was detected (from root's PARENT dir, not the new nested repo).
    assert res.parent_repo == parent.resolve()

    # The wiki-root's relative path is in the parent's `.git/info/exclude`.
    exclude = parent / ".git" / "info" / "exclude"
    assert res.exclude_path == exclude
    assert exclude.is_file()
    content = exclude.read_text(encoding="utf-8")
    assert "/sub/mywiki/" in content
    assert res.exclude_entry == "/sub/mywiki/"

    # Parent's git status must NOT list the nested wiki (force-ignored).
    status = subprocess.run(
        ["git", "-C", str(parent), "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout
    assert "sub/mywiki" not in status


@gitmark
def test_parent_exclude_is_idempotent(tmp_path):
    parent = tmp_path / "parent"
    _init_parent_repo(parent)
    exclude = parent / ".git" / "info" / "exclude"

    root1 = parent / "wikiA"
    wiki_init.init_wiki(root1, scope="pj")
    after_first = exclude.read_text(encoding="utf-8")

    # Re-register the SAME wiki-root entry (simulate a re-run for the path):
    # the helper must not double-add the line.
    again = wiki_init._register_parent_exclude(root1.resolve(), parent.resolve())
    assert again == (exclude, "/wikiA/")
    after_second = exclude.read_text(encoding="utf-8")
    assert after_first == after_second
    assert after_second.count("/wikiA/") == 1


@gitmark
def test_parent_detection_runs_from_parent_dir_not_nested_repo(tmp_path):
    # R-1: detection must yield the PARENT repo, never the freshly-made nested
    # wiki repo. After init both repos exist; the reported parent is the outer.
    parent = tmp_path / "outer"
    _init_parent_repo(parent)
    root = parent / "inner_wiki"

    res = wiki_init.init_wiki(root, scope="workspace")
    assert res.parent_repo == parent.resolve()
    # The nested repo's own toplevel is the wiki-root, distinct from the parent.
    nested_top = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert nested_top  # exists and is the wiki-root, not the parent
