"""Tests: ingest_driver — the deterministic ingest CLI (plan C1, §3 contract).

Covers (plan T2 completion criteria):
  - begin -> finish(success) round-trip commits exactly once (zero LLM-threaded
    state: state lives in the .llmwiki.txn sidecar);
  - finish(fail) and abort both roll back + clean the orphan raw + delete the
    sidecar;
  - dedup_noop path (same content re-ingest -> dedup_noop True);
  - plan-fanout ceil split (each cluster <= k);
  - the consistency invariant violation (apply_fanout_k > max_count) aborts
    begin BEFORE locking (no .llmwiki.lock / .llmwiki.txn left behind);
  - the sidecar is removed on every terminal path (success / fail / abort).

AUTHORED ONLY — not run here (T3/debug owns execution).

Each test builds a throwaway git repo under tmp_path with a .llmwiki marker +
SCHEMA.md, mirroring test_transaction.py's repo fixture. git is required.
"""
import json
import subprocess
import shutil

import pytest

import ingest_driver as drv
import transaction as tx
import config_resolver
import content_hash as ch


def _git_available():
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _git_available(), reason="git not available")


_SCHEMA = """---
config:
  activation_scope: scoped
  read_grounding:  implicit
  write_mode:      explicit
  write_autocommit: auto
  override_scope:  operation
  apply_fanout_k:  10
  max_count:       100
  max_bytes:       10485760
---
# SCHEMA
"""


def _init_wiki(tmp_path):
    """Git repo + .llmwiki marker + SCHEMA.md, committed as the seed state."""
    def g(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                       capture_output=True, text=True)
    g("init", "-q")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    g("config", "commit.gpgsign", "false")
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    # A real wiki root ships templates/.gitignore so the ephemeral transaction
    # dotfiles (.llmwiki.lock / .llmwiki.txn) are never committed by commit()'s
    # `git add -A`; without it a committed lock is resurrected by the next
    # ingest's checkpoint stash. Mirror that here.
    (tmp_path / ".gitignore").write_text(
        ".llmwiki.lock\n.llmwiki.txn\n", encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "index.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# Log\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "seed")
    return g


def _commit_count(tmp_path):
    return int(subprocess.run(
        ["git", "-C", str(tmp_path), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True).stdout.strip())


# --------------------------------------------------------------------------- #
# begin -> finish(success): single commit, zero LLM-threaded state
# --------------------------------------------------------------------------- #
def test_begin_finish_success_round_trip_single_commit(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("third party content", encoding="utf-8")

    before = _commit_count(tmp_path)
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    # The sidecar carries the transaction state (no LLM-threaded state).
    assert (tmp_path / drv.SIDECAR_NAME).is_file()
    assert out["dedup_noop"] is False
    assert out["origin"] == drv.ORIGIN_FE_B
    assert out["max_count"] == 100
    assert out["apply_fanout_k"] == 10
    assert out["redacted_body"] == "third party content"

    # Simulate Stage2 having written a page (the driver does NOT author content).
    (tmp_path / "wiki").mkdir(exist_ok=True)
    (tmp_path / "wiki" / "page.md").write_text("# Page", encoding="utf-8")

    res = drv.finish(str(tmp_path), "success",
                     expected_pages=["wiki/page.md"], title="page")
    assert "committed_sha" in res and res["committed_sha"]
    # Exactly one new commit (D21 single transaction).
    assert _commit_count(tmp_path) == before + 1
    # Sidecar removed + lock released on the terminal path.
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()
    # index regenerated to include the new page.
    assert "wiki/page.md" in (tmp_path / "index.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# finish(fail): rollback + clean orphan raw + release + delete sidecar
# --------------------------------------------------------------------------- #
def test_finish_fail_rolls_back_and_cleans_orphan_raw(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("rollback me", encoding="utf-8")

    before = _commit_count(tmp_path)
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    raw_rel = None
    # The raw artifact was written by begin (FE does not write; the driver does).
    # Locate it via the dedup status the FE used.
    fe_hash = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))["fe_hash"]
    raw_rel = f"raw/{fe_hash}.txt"
    assert (tmp_path / raw_rel).exists()

    res = drv.finish(str(tmp_path), "fail")
    assert "rolled_back_to" in res
    # Orphan raw removed by reset + scoped clean (D21).
    assert not (tmp_path / raw_rel).exists()
    # No new commit on failure.
    assert _commit_count(tmp_path) == before
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


# --------------------------------------------------------------------------- #
# finish(success) failure: a failed commit on the success path must roll back +
# release + delete sidecar (honours the one-of-commit/rollback invariant)
# --------------------------------------------------------------------------- #
def test_finish_success_commit_failure_rolls_back(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("commit will fail", encoding="utf-8")

    before = _commit_count(tmp_path)
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    fe_hash = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))["fe_hash"]
    raw_rel = f"raw/{fe_hash}.txt"
    assert (tmp_path / raw_rel).exists()

    # Simulate Stage2 having written a page, then force commit to fail on the
    # success path (regenerate/log succeed; commit raises).
    (tmp_path / "wiki").mkdir(exist_ok=True)
    (tmp_path / "wiki" / "page.md").write_text("# Page", encoding="utf-8")

    def _boom(*a, **k):
        raise tx.GitError("simulated commit failure")
    monkeypatch.setattr(tx, "commit", _boom)

    with pytest.raises(tx.GitError):
        drv.finish(str(tmp_path), "success",
                   expected_pages=["wiki/page.md"], title="page")

    # Rollback restored the pre-ingest state: orphan raw + the page are gone and
    # there is no new commit.
    assert not (tmp_path / raw_rel).exists()
    assert not (tmp_path / "wiki" / "page.md").exists()
    assert _commit_count(tmp_path) == before
    # Terminal path still released the lock + deleted the sidecar — no strand.
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()


# --------------------------------------------------------------------------- #
# abort: rollback + release + delete sidecar (manual recovery, D-g)
# --------------------------------------------------------------------------- #
def test_abort_rolls_back_and_deletes_sidecar(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("abort me", encoding="utf-8")

    before = _commit_count(tmp_path)
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    fe_hash = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))["fe_hash"]
    raw_rel = f"raw/{fe_hash}.txt"
    assert (tmp_path / raw_rel).exists()

    res = drv.abort(str(tmp_path))
    assert res["aborted"] is True
    assert not (tmp_path / raw_rel).exists()        # orphan raw cleaned
    assert _commit_count(tmp_path) == before        # no commit
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


def test_abort_no_sidecar_is_noop_with_message(tmp_path):
    _init_wiki(tmp_path)
    res = drv.abort(str(tmp_path))
    assert res["aborted"] is False
    assert "message" in res


# --------------------------------------------------------------------------- #
# dedup_noop path: same content re-ingest is a no-op (D18)
# --------------------------------------------------------------------------- #
def test_dedup_noop_path(tmp_path):
    _init_wiki(tmp_path)
    # The source is a 3rd-party artifact OUTSIDE the wiki root: a source inside an
    # untracked root would be carried off by checkpoint()'s clean-tree stash (R8)
    # and a successful commit does not pop that stash, so a same-content re-ingest
    # could not re-read it. Placing it outside the root matches the real contract.
    ext = tmp_path.parent / (tmp_path.name + "_src")
    ext.mkdir()
    src = ext / "input.txt"
    src.write_text("stable third-party content", encoding="utf-8")

    # First ingest: not a no-op; finish(success) with no pages just commits the
    # raw artifact + index/log, then releases.
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    drv.finish(str(tmp_path), "success", title="first")

    # Second ingest of identical content -> dedup_noop True (raw already exists).
    out = drv.begin(str(tmp_path), str(src), kind="fe_b")
    assert out["dedup_noop"] is True
    # Per the contract the caller now finish(fail) to roll back the just-written
    # raw (here nothing new was written) and release.
    drv.finish(str(tmp_path), "fail")
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / tx.LOCK_NAME).exists()


# --------------------------------------------------------------------------- #
# plan-fanout: ceil split, each cluster <= k
# --------------------------------------------------------------------------- #
def test_plan_fanout_under_k_one_cluster(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")   # k=10 from config
    touched = [f"wiki/p{i}.md" for i in range(7)]      # 7 <= 10
    out = drv.plan_fanout(str(tmp_path), json.dumps({"touched": touched}))
    assert out["clusters"] == [touched]


def test_plan_fanout_over_k_ceil_split_each_le_k(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("x", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")   # k=10 from config
    touched = [f"wiki/p{i}.md" for i in range(23)]     # 23 > 10
    out = drv.plan_fanout(str(tmp_path), json.dumps(touched))
    clusters = out["clusters"]
    # ceil(23/10) = 3 clusters, each <= 10, union == touched, order preserved.
    assert len(clusters) == 3
    assert all(len(c) <= 10 for c in clusters)
    assert [p for c in clusters for p in c] == touched


def test_plan_fanout_requires_sidecar(tmp_path):
    _init_wiki(tmp_path)
    with pytest.raises(drv.DriverError):
        drv.plan_fanout(str(tmp_path), json.dumps(["wiki/a.md"]))


# --------------------------------------------------------------------------- #
# consistency invariant: violation aborts begin BEFORE locking
# --------------------------------------------------------------------------- #
def test_consistency_violation_aborts_begin_before_locking(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("never ingested", encoding="utf-8")

    # apply_fanout_k (200, prompt override) > max_count (100) violates D-c.
    with pytest.raises(config_resolver.ConfigInconsistency):
        drv.begin(str(tmp_path), str(src), kind="fe_b", apply_fanout_k="200")

    # No side effect: no lock, no sidecar, no raw artifact written.
    assert not (tmp_path / tx.LOCK_NAME).exists()
    assert not (tmp_path / drv.SIDECAR_NAME).exists()
    assert not (tmp_path / "raw").exists()


# --------------------------------------------------------------------------- #
# sidecar schema: begin writes the documented keys
# --------------------------------------------------------------------------- #
def test_sidecar_schema_keys(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path / "input.txt"
    src.write_text("schema check", encoding="utf-8")
    drv.begin(str(tmp_path), str(src), kind="fe_b")
    state = json.loads((tmp_path / drv.SIDECAR_NAME).read_text(encoding="utf-8"))
    assert set(state) == {
        "checkpoint_head", "stashed", "origin", "doc_type",
        "max_count", "max_bytes", "apply_fanout_k", "fe_hash", "pid",
    }
    drv.abort(str(tmp_path))   # clean up the lock/sidecar
