"""CLI exit-code contract + read-verb fail-closed + resolve-root --sid (theme1).

Covers the theme1 boundary-contract changes:
  - i:39  usage / protocol errors return EX_USAGE (64); verb-specific SENTINELs
          (NO-WIKI / NO-MARKER / NOT-A-WIKI / REFUSED) stay rc2.
  - i:45  scan-pages / search fail CLOSED (NOT-A-WIKI rc2) on a missing marker
          instead of enumerating an empty page set as if the wiki were empty.
  - i:63  resolve-root threads --sid to the resolver so the pj scope reads the
          exact per-session state file first (concurrent-session cross-talk fix).

Model-free and dependency-free (no uv / qmd / network).
"""
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
    """Minimal wiki-root: `.llmwiki` marker + one page dir."""
    (root / "wiki" / "derived").mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "alpha.md").write_text("a", encoding="utf-8")
    (root / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n", encoding="utf-8")
    (root / "SCHEMA.md").write_text("---\nconfig:\n---\n# SCHEMA", encoding="utf-8")
    return str(root)


# --------------------------------------------------------------------------- #
# i:39 — usage / protocol errors return EX_USAGE (64)
# --------------------------------------------------------------------------- #
def test_unknown_verb_is_ex_usage(tmp_path):
    rc, _, err = _run(["no-such-verb"])
    assert rc == cli.EX_USAGE and "unknown verb" in err


def test_missing_verb_is_ex_usage():
    rc, _, err = _run([])
    assert rc == cli.EX_USAGE and "usage: llmwiki" in err


@pytest.mark.parametrize("argv", [
    ["scan-pages"],
    ["search", "/x"],                 # missing --q
    ["marker-detect"],
    ["declare"],
    ["promote-check", "/x"],
    ["promote", "/x"],
    ["lint"],
    ["ingest-apply", "/x"],
    ["reindex"],
    ["toggle"],
    ["toggle", "set", "/x"],          # too few args
    ["toggle", "set", "/x", "sid", "maybe"],   # bad state
    ["toggle", "bogus"],              # unknown sub-command
], ids=lambda a: "_".join(a) or "empty")
def test_usage_errors_are_ex_usage(argv):
    rc, _, _ = _run(argv)
    assert rc == cli.EX_USAGE


# --------------------------------------------------------------------------- #
# i:39 — genuine SENTINELs stay rc2
# --------------------------------------------------------------------------- #
def test_resolve_root_no_wiki_is_sentinel_rc2(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    monkeypatch.chdir(tmp_path)                      # empty cwd, no marker
    rc, _, err = _run(["resolve-root"])
    assert rc == 2 and "NO-WIKI" in err


def test_marker_detect_no_marker_is_sentinel_rc2(tmp_path):
    rc, _, err = _run(["marker-detect", str(tmp_path)])
    assert rc == 2 and "NO-MARKER" in err


def test_declare_not_a_wiki_is_sentinel_rc2(tmp_path):
    rc, out, _ = _run(["declare", str(tmp_path)])
    assert rc == 2 and "NOT-A-WIKI" in out          # declare prints to stdout


def test_ingest_apply_no_journal_is_sentinel_rc2(tmp_path):
    root = _make_wiki(tmp_path)
    rc, _, err = _run(["ingest-apply", root, "fe_b"], stdin="[]")
    assert rc == 2 and "REFUSED no-journal" in err


def test_promote_check_missing_file_is_sentinel_rc2(tmp_path):
    # P10 clean-error: a missing preview target is data, not a usage error.
    root = _make_wiki(tmp_path)
    rc, _, err = _run(["promote-check", root, "wiki/derived/no-such.md"])
    assert rc == 2 and "NOT-FOUND" in err


def test_floor_check_malformed_json_is_ex_usage(tmp_path):
    # P10 clean-error: malformed stdin JSON is a protocol violation.
    rc, _, err = _run(["floor-check"], stdin="{not json")
    assert rc == cli.EX_USAGE and "malformed stdin JSON" in err


# --------------------------------------------------------------------------- #
# i:45 — read verbs fail CLOSED on a missing marker
# --------------------------------------------------------------------------- #
def test_scan_pages_non_wiki_fails_closed(tmp_path):
    rc, out, err = _run(["scan-pages", str(tmp_path)])   # no .llmwiki marker
    assert rc == 2 and "NOT-A-WIKI" in err
    assert out == ""                                     # NOT an empty exit-0


def test_search_non_wiki_fails_closed(tmp_path):
    rc, out, err = _run(["search", str(tmp_path), "--q", "anything"])
    assert rc == 2 and "NOT-A-WIKI" in err
    assert out == ""


def test_scan_pages_valid_wiki_still_enumerates(tmp_path):
    root = _make_wiki(tmp_path)
    rc, out, _ = _run(["scan-pages", root])
    assert rc == 0 and "wiki/alpha.md" in out


# --------------------------------------------------------------------------- #
# i:63 — resolve-root --sid threads the session id to the pj scope
# --------------------------------------------------------------------------- #
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
    # Two sessions on two different projects, each with its own wiki.
    wiki_a = _make_pj_wiki(projects, "projA")
    _make_pj_wiki(projects, "projB")
    (state / "sidA.json").write_text(json.dumps({"project": "projA"}), encoding="utf-8")
    fileB = state / "sidB.json"
    fileB.write_text(json.dumps({"project": "projB"}), encoding="utf-8")
    # Make B the mtime-latest so the fallback would pick B, proving --sid overrides it.
    os.utime(state / "sidA.json", (1_000, 1_000))
    os.utime(fileB, (2_000, 2_000))

    rc, out, _ = _run(["resolve-root", "--sid", "sidA"])
    assert rc == 0
    root_line = out.strip().split("\t")[0]
    assert root_line == str(wiki_a.resolve()) or root_line == str(wiki_a)


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

    rc, out, _ = _run(["resolve-root"])              # no --sid -> mtime-latest (B)
    assert rc == 0
    root_line = out.strip().split("\t")[0]
    assert root_line == str(wiki_b.resolve()) or root_line == str(wiki_b)
