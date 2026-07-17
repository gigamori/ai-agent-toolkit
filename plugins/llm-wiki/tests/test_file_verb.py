"""Tests: the `file` verb (FE-A filing) — DEC-R3 completion.

Verifies the full FE-A write envelope now realized in `cli._file`:
  - a raw/derived/<hash>.md provenance snapshot is written (D1/D18);
  - the PAGE is the REDACTED body (D16), staged through the allowlist (D20);
  - re-filing identical content is a content-hash dedup no-op (D18);
  - a seeded secret is masked in the page AND surfaced as a redaction flag (D16).
"""
import io

import pytest

from llmwiki import cli
from llmwiki.core import content_hash as ch
from llmwiki.ingest import redaction
from llmwiki.write.write_tool import WriteRejected


def _make_wiki(tmp_path):
    (tmp_path / "wiki" / "derived").mkdir(parents=True)
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text("---\nconfig:\n---\n# SCHEMA\n",
                                        encoding="utf-8")
    (tmp_path / "index.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# Log\n", encoding="utf-8")
    return str(tmp_path)


def _run_file(monkeypatch, capsys, root, page, title, content):
    monkeypatch.setattr("sys.stdin", io.StringIO(content))
    rc = cli.main(["file", root, page, title])
    return rc, capsys.readouterr().out


def test_file_writes_raw_snapshot_and_page(monkeypatch, capsys, tmp_path):
    root = _make_wiki(tmp_path)
    rc, out = _run_file(monkeypatch, capsys, root,
                        "wiki/derived/answer.md", "Answer", "the answer body")
    assert rc == 0
    page = tmp_path / "wiki" / "derived" / "answer.md"
    assert page.read_text(encoding="utf-8") == "the answer body"
    # Raw provenance snapshot under raw/derived/<hash>.md (D1/D18).
    h = ch.content_hash("the answer body")
    raw = tmp_path / "raw" / "derived" / f"{h}.md"
    assert raw.is_file()
    assert raw.read_text(encoding="utf-8") == "the answer body"
    # Index regenerated to include the page.
    assert "wiki/derived/answer.md" in (
        tmp_path / "index.md").read_text(encoding="utf-8")


def test_file_second_time_is_dedup_noop(monkeypatch, capsys, tmp_path):
    root = _make_wiki(tmp_path)
    _run_file(monkeypatch, capsys, root, "wiki/derived/a.md", "A", "same body")
    rc, out = _run_file(monkeypatch, capsys, root,
                        "wiki/derived/a.md", "A", "same body")
    assert rc == 0
    assert "dedup no-op" in out


def test_file_redacts_secret_in_page_and_flags(monkeypatch, capsys, tmp_path):
    root = _make_wiki(tmp_path)
    secret = "AKIA" + "A" * 16          # matches the AWS-key pattern (redaction.py)
    body = f"the key is {secret} do not leak"
    rc, out = _run_file(monkeypatch, capsys, root,
                        "wiki/derived/leak.md", "Leak", body)
    assert rc == 0
    page = (tmp_path / "wiki" / "derived" / "leak.md").read_text(encoding="utf-8")
    assert secret not in page                       # D16: masked in the page
    assert redaction.PH_SECRET in page
    assert "redaction-flags:" in out                # surfaced to the human gate


def test_file_outside_wiki_derived_is_rejected(monkeypatch, capsys, tmp_path):
    # item4: filing a page OUTSIDE wiki/derived/ stays REJECTED (D20 cross-
    # namespace), the existing contract. A `file`-origin write is derived-origin.
    root = _make_wiki(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("body"))
    with pytest.raises(WriteRejected):
        cli.main(["file", root, "wiki/not-derived.md", "Outside"])
    out = capsys.readouterr().out
    assert "REJECTED" in out


def test_file_usage_discloses_wiki_derived_constraint(capsys):
    # item4: too-few args prints the usage, which now discloses the
    # wiki/derived/ target constraint.
    rc = cli.main(["file", "only-root"])            # < 3 positional args
    assert rc == cli.EX_USAGE
    err = capsys.readouterr().err
    assert "wiki/derived/" in err
