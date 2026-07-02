"""Tests: wiki initializer (git-independent).

Covers:
  - refuses to overwrite an existing `.llmwiki` (no-op / error);
  - copies the contract templates into the target root;
  - invokes git in ZERO places: no nested `.git` is created and a surrounding
    parent repo's `.git` is never mutated (the former nested-init + parent-exclude
    were removed to avoid misfiring into a parent repo).
"""
import subprocess

import pytest

from llmwiki.init import wiki_init


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


def test_refuses_on_template_name_collision(tmp_path):
    # M5: initializing into a non-wiki dir that already holds a template-named
    # file (a foreign index.md) must refuse and write NOTHING — copytree would
    # otherwise silently overwrite it.
    root = tmp_path / "existing-project"
    root.mkdir()
    foreign = root / "index.md"
    foreign.write_text("MY PROJECT README", encoding="utf-8")

    with pytest.raises(wiki_init.WikiInitError) as ei:
        wiki_init.init_wiki(root, scope="prompt")
    assert "index.md" in str(ei.value)
    # Nothing written: foreign file intact, no wiki created.
    assert foreign.read_text(encoding="utf-8") == "MY PROJECT README"
    assert not (root / ".llmwiki").exists()
    assert not (root / "SCHEMA.md").exists()
    assert not (root / "wiki").exists()


# --------------------------------------------------------------------------- #
# template copy
# --------------------------------------------------------------------------- #
def test_copies_contract_templates(tmp_path):
    root = tmp_path / "wiki"
    res = wiki_init.init_wiki(root, scope="prompt")

    assert (root / ".llmwiki").is_file()
    assert (root / "SCHEMA.md").is_file()
    assert (root / "index.md").is_file()
    assert (root / "log.md").is_file()
    assert (root / "raw").is_dir()
    assert (root / "wiki").is_dir()
    assert (root / "wiki" / "derived").is_dir()
    # The InitResult carries only the resolved root + scope (no git fields).
    assert res.root == root.resolve()
    assert res.scope == "prompt"
    assert not hasattr(res, "parent_repo")


# --------------------------------------------------------------------------- #
# git-independence
# --------------------------------------------------------------------------- #
def test_init_creates_no_nested_git_repo(tmp_path):
    root = tmp_path / "standalone"
    wiki_init.init_wiki(root, scope="prompt")
    # No nested repo is created (the wiki-root is a plain directory).
    assert not (root / ".git").exists()


def test_init_does_not_mutate_parent_repo(tmp_path):
    # A wiki initialized inside a parent git repo must NOT touch the parent's
    # .git (no exclude registration, no staging) — git is never invoked.
    parent = tmp_path / "parent"
    parent.mkdir()

    def g(*a):
        subprocess.run(["git", "-C", str(parent), *a], check=True,
                       capture_output=True, text=True)
    import shutil
    if shutil.which("git") is None:
        pytest.skip("git not available")
    g("init", "-q")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    (parent / "seed.md").write_text("seed", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "seed")

    exclude = parent / ".git" / "info" / "exclude"
    before = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""

    wiki_init.init_wiki(parent / "sub" / "mywiki", scope="pj")

    after = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    assert after == before                      # parent .git/info/exclude untouched
    assert "mywiki" not in after
