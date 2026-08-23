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


def _make_wiki_tiny_bytes(tmp_path):
    (tmp_path / "wiki" / "derived").mkdir(parents=True)
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text(
        "---\nconfig:\n  max_bytes: 10\n---\n# SCHEMA\n", encoding="utf-8")
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
    h = ch.content_hash("the answer body")
    raw = tmp_path / "raw" / "derived" / f"{h}.md"
    assert raw.is_file()
    assert raw.read_text(encoding="utf-8") == "the answer body"
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
    secret = "AKIA" + "A" * 16
    body = f"the key is {secret} do not leak"
    rc, out = _run_file(monkeypatch, capsys, root,
                        "wiki/derived/leak.md", "Leak", body)
    assert rc == 0
    page = (tmp_path / "wiki" / "derived" / "leak.md").read_text(encoding="utf-8")
    assert secret not in page, "an AWS-key-shaped literal is masked in the page, not merely flagged"
    assert redaction.PH_SECRET in page
    assert "redaction-flags:" in out


def test_file_redaction_flag_previews_cap_at_20_and_report_true_total(monkeypatch, capsys, tmp_path):
    root = _make_wiki(tmp_path)
    body = "\n".join(f"key line {i}: AKIA{'A' * 16}" for i in range(100))
    rc, out = _run_file(monkeypatch, capsys, root,
                        "wiki/derived/many-leaks.md", "Many", body)
    assert rc == 0
    assert "redaction-flags: 100 " in out
    line_previews = [ln for ln in out.splitlines() if ln.startswith("  [line ")]
    assert len(line_previews) == 20
    assert "flags_total=100" in out


def test_file_outside_wiki_derived_is_rejected(monkeypatch, capsys, tmp_path):
    root = _make_wiki(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("body"))
    with pytest.raises(WriteRejected):
        cli.main(["file", root, "wiki/not-derived.md", "Outside"])
    out = capsys.readouterr().out
    assert "REJECTED" in out


def test_file_usage_discloses_wiki_derived_constraint(capsys):
    rc = cli.main(["file", "only-root"])
    assert rc == cli.EX_USAGE
    err = capsys.readouterr().err
    assert "wiki/derived/" in err


def test_file_tiny_max_bytes_rejects_budget(monkeypatch, capsys, tmp_path):
    root = _make_wiki_tiny_bytes(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("this body is over ten bytes"))
    with pytest.raises(WriteRejected):
        cli.main(["file", root, "wiki/derived/over.md", "Over"])
    out = capsys.readouterr().out
    assert "REJECTED budget" in out
    assert not (tmp_path / "wiki" / "derived" / "over.md").exists()


def test_file_default_budget_matches_dataclass_defaults(monkeypatch, capsys, tmp_path):
    root = _make_wiki(tmp_path)
    rc, out = _run_file(monkeypatch, capsys, root,
                        "wiki/derived/normal.md", "Normal", "a normal body")
    assert rc == 0
    page = tmp_path / "wiki" / "derived" / "normal.md"
    assert page.read_text(encoding="utf-8") == "a normal body"
