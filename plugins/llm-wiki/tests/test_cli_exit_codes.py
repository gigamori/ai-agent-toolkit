import contextlib
import io
import json
import os
import sys

import pytest

from llmwiki import cli


def _run(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    if stdin:
        sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
    finally:
        sys.stdin = old_stdin
    return rc, out.getvalue(), err.getvalue()


def _make_wiki(root):
    (root / "wiki" / "derived").mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "alpha.md").write_text("a", encoding="utf-8")
    (root / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n", encoding="utf-8")
    (root / "SCHEMA.md").write_text("---\nconfig:\n---\n# SCHEMA", encoding="utf-8")
    return str(root)


def test_unknown_verb_is_ex_usage(tmp_path):
    rc, _, err = _run(["no-such-verb"])
    assert rc == cli.EX_USAGE and "unknown verb" in err


def test_missing_verb_is_ex_usage():
    rc, _, err = _run([])
    assert rc == cli.EX_USAGE and "usage: llmwiki" in err


@pytest.mark.parametrize("argv", [
    ["scan-pages"],
    ["search", "/x"],
    ["marker-detect"],
    ["declare"],
    ["promote-check", "/x"],
    ["promote", "/x"],
    ["lint"],
    ["ingest-apply", "/x"],
    ["reindex"],
    ["toggle"],
    ["toggle", "set", "/x"],
    ["toggle", "set", "/x", "sid", "maybe"],
    ["toggle", "bogus"],
], ids=lambda a: "_".join(a) or "empty")
def test_usage_errors_are_ex_usage(argv):
    rc, _, _ = _run(argv)
    assert rc == cli.EX_USAGE


def test_resolve_root_no_wiki_is_sentinel_rc2(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    monkeypatch.chdir(tmp_path)
    rc, _, err = _run(["resolve-root"])
    assert rc == 2 and "NO-WIKI" in err


def test_marker_detect_no_marker_is_sentinel_rc2(tmp_path):
    rc, _, err = _run(["marker-detect", str(tmp_path)])
    assert rc == 2 and "NO-MARKER" in err


def test_declare_not_a_wiki_is_sentinel_rc2(tmp_path):
    rc, out, _ = _run(["declare", str(tmp_path)])
    assert rc == 2 and "NOT-A-WIKI" in out


def test_ingest_apply_no_journal_is_sentinel_rc2(tmp_path):
    root = _make_wiki(tmp_path)
    rc, _, err = _run(["ingest-apply", root, "fe_b"], stdin="[]")
    assert rc == 2 and "REFUSED no-journal" in err


def test_promote_check_missing_file_is_sentinel_rc2(tmp_path):
    root = _make_wiki(tmp_path)
    rc, _, err = _run(["promote-check", root, "wiki/derived/no-such.md"])
    assert rc == 2 and "NOT-FOUND" in err


def test_floor_check_malformed_json_is_ex_usage(tmp_path):
    rc, _, err = _run(["floor-check"], stdin="{not json")
    assert rc == cli.EX_USAGE and "malformed stdin JSON" in err


def test_scan_pages_non_wiki_fails_closed(tmp_path):
    rc, out, err = _run(["scan-pages", str(tmp_path)])
    assert rc == 2 and "NOT-A-WIKI" in err
    assert out == "", "a missing marker fails closed instead of printing an empty page set"


def test_search_non_wiki_fails_closed(tmp_path):
    rc, out, err = _run(["search", str(tmp_path), "--q", "anything"])
    assert rc == 2 and "NOT-A-WIKI" in err
    assert out == "", "a missing marker fails closed instead of printing an empty page set"


def test_scan_pages_valid_wiki_still_enumerates(tmp_path):
    root = _make_wiki(tmp_path)
    rc, out, _ = _run(["scan-pages", root])
    assert rc == 0 and "wiki/alpha.md" in out


def _make_pj_wiki(projects_root, project):
    wiki = projects_root / project / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n", encoding="utf-8")
    return wiki


def test_resolve_root_sid_selects_per_session_state(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    monkeypatch.chdir(tmp_path)
    projects = tmp_path / "_projects"
    state = projects / "_state"
    state.mkdir(parents=True)
    wiki_a = _make_pj_wiki(projects, "projA")
    _make_pj_wiki(projects, "projB")
    (state / "sidA.json").write_text(json.dumps({"project": "projA"}), encoding="utf-8")
    fileB = state / "sidB.json"
    fileB.write_text(json.dumps({"project": "projB"}), encoding="utf-8")
    os.utime(state / "sidA.json", (1_000, 1_000))
    os.utime(fileB, (2_000, 2_000))

    rc, out, _ = _run(["resolve-root", "--sid", "sidA"])
    assert rc == 0
    root_line = out.splitlines()[0].strip()
    assert (
        root_line == str(wiki_a.resolve())
        or root_line == str(wiki_a)
        or root_line == wiki_a.resolve().as_posix()
    )


def test_resolve_root_sid_without_state_skips_pj_and_explains(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    monkeypatch.chdir(tmp_path)
    projects = tmp_path / "_projects"
    state = projects / "_state"
    state.mkdir(parents=True)
    _make_pj_wiki(projects, "projB")
    (state / "sidB.json").write_text(json.dumps({"project": "projB"}), encoding="utf-8")

    rc, out, err = _run(["resolve-root", "--sid", "sidA"])
    assert rc == 2
    assert out.strip() == ""
    assert "NO-WIKI" in err
    assert err.splitlines()[0].startswith("pj-skip:")


def test_resolve_root_sid_explain_line_absent_on_happy_path(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    monkeypatch.chdir(tmp_path)
    projects = tmp_path / "_projects"
    state = projects / "_state"
    state.mkdir(parents=True)
    _make_pj_wiki(projects, "projA")
    _make_pj_wiki(projects, "projB")
    (state / "sidA.json").write_text(json.dumps({"project": "projA"}), encoding="utf-8")
    fileB = state / "sidB.json"
    fileB.write_text(json.dumps({"project": "projB"}), encoding="utf-8")
    os.utime(state / "sidA.json", (1_000, 1_000))
    os.utime(fileB, (2_000, 2_000))

    rc, out, err = _run(["resolve-root", "--sid", "sidA"])
    assert rc == 0
    assert "pj-skip:" not in err


def test_resolve_root_without_sid_emits_no_explain_line(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    monkeypatch.chdir(tmp_path)
    projects = tmp_path / "_projects"
    state = projects / "_state"
    state.mkdir(parents=True)
    _make_pj_wiki(projects, "projA")
    _make_pj_wiki(projects, "projB")
    (state / "sidA.json").write_text(json.dumps({"project": "projA"}), encoding="utf-8")
    fileB = state / "sidB.json"
    fileB.write_text(json.dumps({"project": "projB"}), encoding="utf-8")
    os.utime(state / "sidA.json", (1_000, 1_000))
    os.utime(fileB, (2_000, 2_000))

    rc, out, err = _run(["resolve-root"])
    assert rc == 0
    assert "pj-skip:" not in err


def test_resolve_root_without_sid_uses_mtime_latest(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    monkeypatch.chdir(tmp_path)
    projects = tmp_path / "_projects"
    state = projects / "_state"
    state.mkdir(parents=True)
    _make_pj_wiki(projects, "projA")
    wiki_b = _make_pj_wiki(projects, "projB")
    (state / "sidA.json").write_text(json.dumps({"project": "projA"}), encoding="utf-8")
    fileB = state / "sidB.json"
    fileB.write_text(json.dumps({"project": "projB"}), encoding="utf-8")
    os.utime(state / "sidA.json", (1_000, 1_000))
    os.utime(fileB, (2_000, 2_000))

    rc, out, _ = _run(["resolve-root"])
    assert rc == 0
    root_line = out.splitlines()[0].strip()
    assert (
        root_line == str(wiki_b.resolve())
        or root_line == str(wiki_b)
        or root_line == wiki_b.resolve().as_posix()
    )


def test_file_content_file_is_read_instead_of_stdin(tmp_path):
    root = _make_wiki(tmp_path)
    body = tmp_path / "llmwiki-body-x.md"
    body.write_text("from the content file", encoding="utf-8")
    rc, out, _ = _run(["file", root, "wiki/derived/p.md", "T",
                       "--content-file", str(body)], stdin="from stdin")
    assert rc == 0, out
    assert (tmp_path / "wiki" / "derived" / "p.md").read_text(encoding="utf-8") == (
        "from the content file"
    ), "the flag wins over stdin, which deliberately carries different text here"


def test_file_content_file_named_llmwiki_body_is_deleted_after_read(tmp_path):
    root = _make_wiki(tmp_path)
    body = tmp_path / "llmwiki-body-y.md"
    body.write_text("one shot", encoding="utf-8")
    rc, _, _ = _run(["file", root, "wiki/derived/q.md", "T", "--content-file", str(body)])
    assert rc == 0
    assert not body.exists(), "the pre-redaction text does not survive the call"


def test_file_content_file_with_other_name_is_kept(tmp_path):
    root = _make_wiki(tmp_path)
    body = tmp_path / "notes.md"
    body.write_text("keep me", encoding="utf-8")
    rc, _, _ = _run(["file", root, "wiki/derived/r.md", "T", "--content-file", str(body)])
    assert rc == 0
    assert body.exists(), (
        "the unlink is guarded by the temp-name shape, so a caller-owned path is "
        "never removed"
    )


def test_file_missing_content_file_fails_closed_rc2(tmp_path):
    root = _make_wiki(tmp_path)
    rc, _, err = _run(["file", root, "wiki/derived/s.md", "T",
                       "--content-file", str(tmp_path / "gone.md")], stdin="stdin body")
    assert rc == 2 and "NO-CONTENT-FILE" in err
    assert not (tmp_path / "wiki" / "derived" / "s.md").exists()


def test_file_still_reads_stdin_when_flag_absent(tmp_path):
    root = _make_wiki(tmp_path)
    rc, out, _ = _run(["file", root, "wiki/derived/t.md", "T"], stdin="classic stdin body")
    assert rc == 0, out
    assert (tmp_path / "wiki" / "derived" / "t.md").read_text(encoding="utf-8") == (
        "classic stdin body"
    )


def test_file_content_file_without_value_is_ex_usage(tmp_path):
    root = _make_wiki(tmp_path)
    rc, _, err = _run(["file", root, "wiki/derived/u.md", "T", "--content-file"])
    assert rc == cli.EX_USAGE and "--content-file requires a path" in err
    assert not (tmp_path / "wiki" / "derived" / "u.md").exists()


def test_file_content_file_with_empty_value_is_ex_usage(tmp_path):
    root = _make_wiki(tmp_path)
    rc, _, err = _run(["file", root, "wiki/derived/v.md", "T", "--content-file="])
    assert rc == cli.EX_USAGE and "--content-file requires a path" in err
    assert not (tmp_path / "wiki" / "derived" / "v.md").exists()


def test_file_misspelt_flag_is_ex_usage_not_silently_ignored(tmp_path):
    root = _make_wiki(tmp_path)
    rc, _, err = _run(["file", root, "wiki/derived/w.md", "T", "--content_file=x"],
                      stdin="stdin body")
    assert rc == cli.EX_USAGE and "usage: file" in err
    assert not (tmp_path / "wiki" / "derived" / "w.md").exists()


def test_file_title_may_start_with_dashes(tmp_path):
    root = _make_wiki(tmp_path)
    rc, out, _ = _run(["file", root, "wiki/derived/x.md", "--- draft ---"], stdin="body")
    assert rc == 0, out
    assert (tmp_path / "wiki" / "derived" / "x.md").read_text(encoding="utf-8") == "body"
