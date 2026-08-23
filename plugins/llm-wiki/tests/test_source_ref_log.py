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
    assert e.recorded_at
    assert not e.raw_rel_path.startswith("/")
    assert ":" not in e.raw_rel_path.split("/")[0]


def test_raw_bytes_and_hash_are_unaffected_by_the_record(tmp_path):
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
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        _init_wiki(tmp_path / name)
    body = "identical bytes"
    a = drv.begin(str(tmp_path / "a"), _src(tmp_path, "a.txt", body), kind="fe_b")
    b = drv.begin(str(tmp_path / "b"), _src(tmp_path, "b.txt", body), kind="fe_b",
                  external="https://example.com/x")
    assert a["raw_rel_path"] == b["raw_rel_path"], (
        "the locator is recorded outside the artifact, so the raw stays byte-identical "
        "and the content-hash dedup key does not move"
    )


def test_finish_fail_removes_the_freshly_created_log(tmp_path):
    _init_wiki(tmp_path)
    src = _src(tmp_path, "input.txt", "rollback me")

    out = drv.begin(str(tmp_path), src, kind="fe_b",
                    external="https://example.com/gone")
    assert srl.source_ref_log_path(tmp_path).is_file()

    assert drv.finish(str(tmp_path), "fail") == {"rolled_back": True}

    assert not (tmp_path / out["raw_rel_path"]).exists()
    assert srl.read_entries(tmp_path) == []
    assert not srl.source_ref_log_path(tmp_path).exists()
    assert _no_txn_residue(tmp_path)


def test_rollback_restores_the_prior_log_content(tmp_path):
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


def test_dedup_noop_appends_nothing(tmp_path):
    _init_wiki(tmp_path)
    body = "same content twice"
    drv.begin(str(tmp_path), _src(tmp_path, "one.txt", body), kind="fe_b",
              external="https://example.com/first")
    drv.finish(str(tmp_path), "success", expected_pages=[], title="one")
    assert len(srl.read_entries(tmp_path)) == 1

    out = drv.begin(str(tmp_path), _src(tmp_path, "two.txt", body), kind="fe_b",
                    external="https://example.com/second")
    assert out["dedup_noop"] is True
    assert out["auto_closed"] is True

    entries = srl.read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].external_locator == "https://example.com/first"
    assert _no_txn_residue(tmp_path)


def test_source_ref_log_is_excluded_from_enumerate(tmp_path):
    _init_wiki(tmp_path)
    (tmp_path / "doc.md").write_text("# doc", encoding="utf-8")
    srl.append_entries(tmp_path, [srl.SourceRefEntry(
        raw_rel_path="raw/abc.txt", content_hash="abc", provenance="source")])

    out = drv.enumerate_files(str(tmp_path), "**/*")

    assert "doc.md" in out["files"]
    assert srl.SOURCE_REF_LOG_NAME not in out["files"]
    assert srl.SOURCE_REF_LOG_NAME in drv._EXCLUDED_FILES


@pytest.mark.parametrize("kind", ["fe_b_prime", "fe_pi_log"])
def test_external_on_a_projection_origin_is_a_usage_error(tmp_path, kind):
    _init_wiki(tmp_path)

    with pytest.raises(drv.DriverUsageError):
        drv.begin(str(tmp_path), "some-session-id", kind=kind,
                  external="https://example.com/x")

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
    "C:\\Users\\someone\\secret.txt",
    "/home/someone/secret.txt",
    "file:///C:/Users/someone/secret.txt",
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
    line = json.dumps({"raw_rel_path": "raw/x.md", "content_hash": "h",
                       "provenance": "source", "supersedes": "raw/old.md"})
    e = srl.SourceRefEntry.from_json(line)
    assert e.raw_rel_path == "raw/x.md", (
        "a line written by a newer engine, carrying a reserved key this one does "
        "not use, still loads"
    )
    assert "supersedes" in srl.RESERVED_KEYS


def test_append_entries_empty_is_a_noop(tmp_path):
    srl.append_entries(tmp_path, [])
    assert not srl.source_ref_log_path(tmp_path).exists()
    assert srl.read_entries(tmp_path) == []


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
    assert e.external_locator == ""


def test_file_verb_rollback_removes_the_record(monkeypatch, capsys, tmp_path):
    root = _make_wiki_for_file_verb(tmp_path, schema_config="  max_bytes: 10\n")
    monkeypatch.setattr("sys.stdin", io.StringIO("this body is over ten bytes"))
    with pytest.raises(WriteRejected):
        cli.main(["file", root, "wiki/derived/over.md", "Over"])

    assert srl.read_entries(tmp_path) == []
    assert not srl.source_ref_log_path(tmp_path).exists()
