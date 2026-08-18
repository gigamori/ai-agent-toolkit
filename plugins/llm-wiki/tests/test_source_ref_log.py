"""Tests: the source-ref log (D12) — where a raw artifact's ORIGIN is recorded.

Covers the invariants that made a root-level append-only log the storage site
instead of the raw's own frontmatter (see `source_ref_log`'s docstring):

  - `begin` records one line per raw it writes, carrying `--external` when given;
  - the raw's BYTES and its content-hash (D18) are unaffected, so `sha256(file)
    == filename` still holds and no migration is implied;
  - the line is journaled WITH the raw, so a rollback removes/restores both —
    a line never outlives the raw it describes;
  - a dedup no-op appends nothing (the v1 non-goal, pinned so it cannot drift in
    silently);
  - `enumerate` never offers the log as an ingest source (self-ingest guard);
  - `--external` fails closed on a projection origin (it used to be dropped in
    silence at the `_FE_BY_ORIGIN` dispatch) and on a local absolute path (D12).
"""
import io
import json

import pytest

from llmwiki import cli
from llmwiki.core import content_hash as ch
from llmwiki.ingest import ingest_driver as drv
from llmwiki.ingest import source_ref_log as srl
from llmwiki.write import transaction as tx
from llmwiki.write.write_tool import WriteRejected


_SCHEMA = """---
config:
  activation_scope: scoped
  write_mode:      explicit
  apply_fanout_k:  10
  max_count:       100
  max_bytes:       10485760
---
# SCHEMA
"""


def _init_wiki(tmp_path):
    """A .llmwiki marker + SCHEMA.md + index/log — a plain directory (no git)."""
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "index.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# Log\n", encoding="utf-8")


def _src(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def _no_txn_residue(tmp_path):
    return (not (tmp_path / drv.SIDECAR_NAME).exists()
            and not (tmp_path / tx.LOCK_NAME).exists()
            and not (tmp_path / tx.JOURNAL_DIR).exists())


# --------------------------------------------------------------------------- #
# begin: one line per raw, with the locator
# --------------------------------------------------------------------------- #
def test_begin_records_source_ref_with_external_locator(tmp_path):
    _init_wiki(tmp_path)
    src = _src(tmp_path, "input.txt", "third party content")

    out = drv.begin(str(tmp_path), src, kind="fe_b",
                    external="https://example.com/rfc")

    entries = srl.read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e.raw_rel_path == out["raw_rel_path"]
    assert e.content_hash == ch.content_hash("third party content")
    assert e.provenance == "source"
    assert e.derived_origin == ""
    assert e.external_locator == "https://example.com/rfc"
    assert e.recorded_at                      # stamped
    # D12: the recorded path is wiki-relative, never absolute.
    assert not e.raw_rel_path.startswith("/")
    assert ":" not in e.raw_rel_path.split("/")[0]


def test_raw_bytes_and_hash_are_unaffected_by_the_record(tmp_path):
    # The whole reason the metadata lives outside the artifact: the raw stays
    # byte-identical to the redacted body, so `sha256(file) == filename` holds
    # and the D18 dedup key does not move when `--external` is passed.
    _init_wiki(tmp_path)
    body = "third party content"
    src = _src(tmp_path, "input.txt", body)

    out = drv.begin(str(tmp_path), src, kind="fe_b",
                    external="https://example.com/rfc")

    raw = tmp_path / out["raw_rel_path"]
    assert raw.read_text(encoding="utf-8") == body
    assert out["raw_rel_path"] == f"raw/{ch.content_hash(body)}.txt"


def test_begin_without_external_records_an_empty_locator(tmp_path):
    _init_wiki(tmp_path)
    src = _src(tmp_path, "input.txt", "no locator here")

    drv.begin(str(tmp_path), src, kind="fe_b")

    entries = srl.read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].external_locator == ""


def test_the_same_content_hashes_identically_with_and_without_locator(tmp_path):
    # Two separate wikis so the dedup check is not what makes them agree.
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        _init_wiki(tmp_path / name)
    body = "identical bytes"
    a = drv.begin(str(tmp_path / "a"), _src(tmp_path, "a.txt", body), kind="fe_b")
    b = drv.begin(str(tmp_path / "b"), _src(tmp_path, "b.txt", body), kind="fe_b",
                  external="https://example.com/x")
    assert a["raw_rel_path"] == b["raw_rel_path"]     # the locator moved nothing


# --------------------------------------------------------------------------- #
# rollback: the line never outlives its raw
# --------------------------------------------------------------------------- #
def test_finish_fail_removes_the_freshly_created_log(tmp_path):
    _init_wiki(tmp_path)
    src = _src(tmp_path, "input.txt", "rollback me")

    out = drv.begin(str(tmp_path), src, kind="fe_b",
                    external="https://example.com/gone")
    assert srl.source_ref_log_path(tmp_path).is_file()

    assert drv.finish(str(tmp_path), "fail") == {"rolled_back": True}

    # The raw is gone (existing contract) and so is its origin record: the log
    # was created by this transaction, so the journal replay unlinks it.
    assert not (tmp_path / out["raw_rel_path"]).exists()
    assert srl.read_entries(tmp_path) == []
    assert not srl.source_ref_log_path(tmp_path).exists()
    assert _no_txn_residue(tmp_path)


def test_rollback_restores_the_prior_log_content(tmp_path):
    # The `modify` half of the journal contract: a second ingest that fails must
    # leave the FIRST ingest's line intact, not truncate the log.
    _init_wiki(tmp_path)

    first = drv.begin(str(tmp_path), _src(tmp_path, "one.txt", "kept"),
                      kind="fe_b", external="https://example.com/one")
    drv.finish(str(tmp_path), "success", expected_pages=[], title="one")
    assert len(srl.read_entries(tmp_path)) == 1

    drv.begin(str(tmp_path), _src(tmp_path, "two.txt", "discarded"),
              kind="fe_b", external="https://example.com/two")
    assert len(srl.read_entries(tmp_path)) == 2
    drv.finish(str(tmp_path), "fail")

    entries = srl.read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].raw_rel_path == first["raw_rel_path"]
    assert entries[0].external_locator == "https://example.com/one"


def test_abort_removes_the_record_with_the_orphan_raw(tmp_path):
    _init_wiki(tmp_path)
    out = drv.begin(str(tmp_path), _src(tmp_path, "input.txt", "crashed"),
                    kind="fe_b")

    assert drv.abort(str(tmp_path))["aborted"] is True

    assert not (tmp_path / out["raw_rel_path"]).exists()
    assert srl.read_entries(tmp_path) == []


# --------------------------------------------------------------------------- #
# v1 non-goal: a dedup no-op appends nothing
# --------------------------------------------------------------------------- #
def test_dedup_noop_appends_nothing(tmp_path):
    _init_wiki(tmp_path)
    body = "same content twice"
    drv.begin(str(tmp_path), _src(tmp_path, "one.txt", body), kind="fe_b",
              external="https://example.com/first")
    drv.finish(str(tmp_path), "success", expected_pages=[], title="one")
    assert len(srl.read_entries(tmp_path)) == 1

    # Same bytes from a DIFFERENT locator -> dedup no-op; v1 does not record the
    # second locator (begin auto-closes with a rollback, so a line written there
    # would be replayed away anyway).
    out = drv.begin(str(tmp_path), _src(tmp_path, "two.txt", body), kind="fe_b",
                    external="https://example.com/second")
    assert out["dedup_noop"] is True
    assert out["auto_closed"] is True

    entries = srl.read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].external_locator == "https://example.com/first"
    assert _no_txn_residue(tmp_path)


# --------------------------------------------------------------------------- #
# self-ingest guard
# --------------------------------------------------------------------------- #
def test_source_ref_log_is_excluded_from_enumerate(tmp_path):
    _init_wiki(tmp_path)
    (tmp_path / "doc.md").write_text("# doc", encoding="utf-8")
    srl.append_entries(tmp_path, [srl.SourceRefEntry(
        raw_rel_path="raw/abc.txt", content_hash="abc", provenance="source")])

    out = drv.enumerate_files(str(tmp_path), "**/*")

    assert "doc.md" in out["files"]
    assert srl.SOURCE_REF_LOG_NAME not in out["files"]
    assert srl.SOURCE_REF_LOG_NAME in drv._EXCLUDED_FILES


# --------------------------------------------------------------------------- #
# --external fail-closed (was a silent drop at the _FE_BY_ORIGIN dispatch)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["fe_b_prime", "fe_pi_log"])
def test_external_on_a_projection_origin_is_a_usage_error(tmp_path, kind):
    _init_wiki(tmp_path)

    with pytest.raises(drv.DriverUsageError):
        drv.begin(str(tmp_path), "some-session-id", kind=kind,
                  external="https://example.com/x")

    # Raised before acquire_lock: nothing locked, journaled, or written.
    assert _no_txn_residue(tmp_path)
    assert not srl.source_ref_log_path(tmp_path).exists()
    assert not (tmp_path / "raw").exists()


def test_external_on_a_projection_origin_exits_ex_usage(tmp_path, capsys):
    _init_wiki(tmp_path)
    rc = drv.main(["begin", str(tmp_path), "some-session-id",
                   "--kind=fe_b_prime", "--external=https://example.com/x"])
    assert rc == drv.EX_USAGE
    assert "--external" in capsys.readouterr().err


@pytest.mark.parametrize("locator", [
    "C:\\Users\\someone\\secret.txt",       # Windows drive letter
    "/home/someone/secret.txt",             # POSIX absolute
    "file:///C:/Users/someone/secret.txt",  # absolute path wearing a scheme
])
def test_external_local_absolute_path_is_refused(tmp_path, locator):
    _init_wiki(tmp_path)
    src = _src(tmp_path, "input.txt", "body")

    with pytest.raises(drv.DriverUsageError):
        drv.begin(str(tmp_path), src, kind="fe_b", external=locator)

    assert _no_txn_residue(tmp_path)
    assert not srl.source_ref_log_path(tmp_path).exists()


def test_a_url_locator_is_not_mistaken_for_an_absolute_path():
    assert not srl.is_local_abs_path("https://example.com/a/b")
    assert not srl.is_local_abs_path("http://example.com/")
    assert not srl.is_local_abs_path("")
    assert srl.is_local_abs_path("/etc/passwd")
    assert srl.is_local_abs_path("C:/tmp/x")
    assert srl.is_local_abs_path("\\\\server\\share\\x")


# --------------------------------------------------------------------------- #
# module-level guards + serialization
# --------------------------------------------------------------------------- #
def test_entry_rejects_an_absolute_raw_path():
    with pytest.raises(srl.SourceRefRejected):
        srl.SourceRefEntry(raw_rel_path="/abs/raw/x.md", content_hash="h",
                           provenance="source")


def test_entry_rejects_an_absolute_locator():
    with pytest.raises(srl.SourceRefRejected):
        srl.SourceRefEntry(raw_rel_path="raw/x.md", content_hash="h",
                           provenance="source",
                           external_locator="C:\\secret\\x.txt")


def test_entry_json_round_trip():
    e = srl.SourceRefEntry(
        raw_rel_path="raw/derived/abc.md", content_hash="abc",
        provenance="derived", derived_origin="cc-log", doc_type="transcript",
        external_locator="", recorded_at="2026-08-19")
    assert srl.SourceRefEntry.from_json(e.to_json()) == e


def test_from_json_ignores_unknown_keys():
    # Forward-compat: a line written by a newer engine (e.g. carrying the
    # reserved `supersedes` key) must still load here.
    line = json.dumps({"raw_rel_path": "raw/x.md", "content_hash": "h",
                       "provenance": "source", "supersedes": "raw/old.md"})
    e = srl.SourceRefEntry.from_json(line)
    assert e.raw_rel_path == "raw/x.md"
    assert "supersedes" in srl.RESERVED_KEYS


def test_append_entries_empty_is_a_noop(tmp_path):
    srl.append_entries(tmp_path, [])
    assert not srl.source_ref_log_path(tmp_path).exists()
    assert srl.read_entries(tmp_path) == []


# --------------------------------------------------------------------------- #
# the `file` verb (FE-A) records too, and rolls back with its transaction
# --------------------------------------------------------------------------- #
def _make_wiki_for_file_verb(tmp_path, schema_config=""):
    (tmp_path / "wiki" / "derived").mkdir(parents=True)
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text(
        f"---\nconfig:\n{schema_config}---\n# SCHEMA\n", encoding="utf-8")
    (tmp_path / "index.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# Log\n", encoding="utf-8")
    return str(tmp_path)


def test_file_verb_records_the_conversation_origin(monkeypatch, capsys, tmp_path):
    root = _make_wiki_for_file_verb(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("the answer body"))
    assert cli.main(["file", root, "wiki/derived/answer.md", "Answer"]) == 0

    entries = srl.read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e.raw_rel_path == f"raw/derived/{ch.content_hash('the answer body')}.md"
    assert e.provenance == "derived"
    assert e.derived_origin == "conversation"
    assert e.external_locator == ""          # a conversation has no locator


def test_file_verb_rollback_removes_the_record(monkeypatch, capsys, tmp_path):
    # A budget rejection rolls the filing transaction back; the origin record
    # must go with the raw it described.
    root = _make_wiki_for_file_verb(tmp_path, schema_config="  max_bytes: 10\n")
    monkeypatch.setattr("sys.stdin", io.StringIO("this body is over ten bytes"))
    with pytest.raises(WriteRejected):
        cli.main(["file", root, "wiki/derived/over.md", "Over"])

    assert srl.read_entries(tmp_path) == []
    assert not srl.source_ref_log_path(tmp_path).exists()
