"""Tests: single git transaction + .llmwiki.lock (D21).

Covers: lock excludes concurrent ingest; success -> single commit; failure ->
reset + scoped clean removes orphan raw to the pre-ingest state.

Each test builds a throwaway git repo under tmp_path. git is required; tests that
need it skip when git is unavailable.
"""
import subprocess
import shutil

import pytest

import transaction as tx


def _git_available():
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _git_available(), reason="git not available")


def _init_repo(tmp_path):
    def g(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                       capture_output=True, text=True)
    g("init", "-q")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    g("config", "commit.gpgsign", "false")
    (tmp_path / "seed.md").write_text("seed", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "seed")
    return g


def test_lock_excludes_concurrent_ingest(tmp_path):
    _init_repo(tmp_path)
    h = tx.acquire_lock(tmp_path)
    try:
        with pytest.raises(tx.LockHeld):
            tx.acquire_lock(tmp_path)
    finally:
        tx.release_lock(h)
    # After release a new acquire succeeds.
    h2 = tx.acquire_lock(tmp_path)
    tx.release_lock(h2)


def test_success_makes_single_commit(tmp_path):
    g = _init_repo(tmp_path)
    before = subprocess.run(["git", "-C", str(tmp_path), "rev-list", "--count", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    with tx.transaction(tmp_path, "ingest: add page"):
        (tmp_path / "wiki").mkdir(exist_ok=True)
        (tmp_path / "wiki" / "p.md").write_text("page", encoding="utf-8")
    after = subprocess.run(["git", "-C", str(tmp_path), "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    assert int(after) == int(before) + 1
    assert (tmp_path / "wiki" / "p.md").exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()   # lock released


def test_failure_resets_and_removes_orphan_raw(tmp_path):
    _init_repo(tmp_path)
    raw = tmp_path / "raw" / "derived"
    with pytest.raises(RuntimeError):
        with tx.transaction(tmp_path, "ingest: will fail"):
            raw.mkdir(parents=True)
            (raw / "orphanhash.md").write_text("orphan raw", encoding="utf-8")
            (tmp_path / "wiki").mkdir(exist_ok=True)
            (tmp_path / "wiki" / "partial.md").write_text("partial", encoding="utf-8")
            raise RuntimeError("simulated ingest failure")
    # Reset + clean restored the pre-ingest state: orphan raw and partial page gone.
    assert not (raw / "orphanhash.md").exists()
    assert not (tmp_path / "wiki" / "partial.md").exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()   # lock released even on failure


def test_lock_released_on_failure(tmp_path):
    _init_repo(tmp_path)
    with pytest.raises(ValueError):
        with tx.transaction(tmp_path, "ingest"):
            raise ValueError("boom")
    # Lock must be free for a subsequent ingest.
    h = tx.acquire_lock(tmp_path)
    tx.release_lock(h)


# --------------------------------------------------------------------------- #
# F1 — nested-repo guard at the transaction boundary (review §2 / F1).
# A wiki-root that is NOT its own git repo toplevel must be refused before any
# reset/clean could walk up to a PARENT repo.
# --------------------------------------------------------------------------- #
def test_assert_repo_root_accepts_own_toplevel(tmp_path):
    # tmp_path IS the repo toplevel -> the guard passes (no raise).
    _init_repo(tmp_path)
    tx.assert_repo_root(tmp_path)  # must not raise


def test_assert_repo_root_refuses_plain_subdir_of_parent_repo(tmp_path):
    # tmp_path is a repo; a plain subdir of it is NOT its own toplevel -> refuse.
    _init_repo(tmp_path)
    subdir = tmp_path / "notes" / "mywiki"
    subdir.mkdir(parents=True)
    with pytest.raises(tx.NotARepoRoot):
        tx.assert_repo_root(subdir)


def test_assert_repo_root_refuses_non_repo_dir(tmp_path):
    # A directory that is not inside any git repo at all -> refuse.
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(tx.NotARepoRoot):
        tx.assert_repo_root(plain)


def test_assert_repo_root_accepts_nested_repo_inside_parent(tmp_path):
    # A wiki-root that is its OWN nested repo inside a parent repo passes:
    # git -C <nested> rev-parse --show-toplevel resolves to the nested root.
    _init_repo(tmp_path)                      # parent repo
    nested = tmp_path / "notes" / "mywiki"
    nested.mkdir(parents=True)                # git -C needs the dir to exist
    _init_repo(nested)                        # nested repo == its own toplevel
    tx.assert_repo_root(nested)               # must not raise


def test_checkpoint_refuses_non_repo_root(tmp_path):
    # The guard fires through checkpoint() (the chokepoint begin/transaction use).
    subdir = tmp_path / "sub"
    subdir.mkdir()
    with pytest.raises(tx.NotARepoRoot):
        tx.checkpoint(subdir)


def test_transaction_refuses_subdir_of_parent_repo(tmp_path):
    # End-to-end: transaction() on a plain subdir of a parent repo refuses at
    # entry (checkpoint guard) and never reaches reset --hard / clean -fd.
    _init_repo(tmp_path)
    subdir = tmp_path / "child"
    subdir.mkdir()
    with pytest.raises(tx.NotARepoRoot):
        with tx.transaction(subdir, "ingest: should be refused"):
            pass
    # The lock acquired before checkpoint must be released on the refusal so no
    # empty .llmwiki.lock strands in the bad-wiki dir.
    assert not (subdir / tx.LOCK_NAME).exists()


def test_transaction_refuses_non_repo_dir_without_stranding_lock(tmp_path):
    # transaction() on a directory that is not inside any git repo refuses and
    # leaves no .llmwiki.lock behind (checkpoint-strand regression).
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(tx.NotARepoRoot):
        with tx.transaction(plain, "ingest: should be refused"):
            pass
    assert not (plain / tx.LOCK_NAME).exists()


def test_transaction_rolls_back_and_releases_lock_on_commit_failure(tmp_path, monkeypatch):
    # commit() failure on the normal-exit path must roll back to the checkpoint
    # (pre-ingest state restored) and release the lock — all-or-nothing.
    _init_repo(tmp_path)
    head_before = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()

    def _boom(*a, **k):
        raise tx.GitError("simulated commit failure")
    monkeypatch.setattr(tx, "commit", _boom)

    with pytest.raises(tx.GitError):
        with tx.transaction(tmp_path, "ingest: commit will fail"):
            (tmp_path / "wiki").mkdir(exist_ok=True)
            (tmp_path / "wiki" / "p.md").write_text("page", encoding="utf-8")

    # Rollback restored the pre-ingest state: the body write is gone, HEAD unmoved.
    assert not (tmp_path / "wiki" / "p.md").exists()
    head_after = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    assert head_after == head_before
    assert not (tmp_path / tx.LOCK_NAME).exists()   # lock released on commit failure
