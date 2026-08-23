import hashlib
import unicodedata
from pathlib import Path

import pytest

from llmwiki.ingest import cc_log_project as proj
from llmwiki.ingest import ledger


def _turn(role, uuid, ts, texts, order=0):
    return proj._Turn(
        role=role, uuid=uuid, ts=ts,
        text_parts=list(texts),
        order=order,
    )


def _stub_session_file(monkeypatch):
    monkeypatch.setattr(proj, "_require_session_file",
                        lambda sid: Path(f"{sid}.jsonl"))


def _project(monkeypatch, turns, wiki_root):
    monkeypatch.setattr(proj, "_fetch_turns", lambda sid: list(turns))
    _stub_session_file(monkeypatch)
    return proj.project_owned(wiki_root, "sid-under-test", ledger=ledger)


def test_all_branches_adopted(tmp_path, monkeypatch):
    turns = [
        _turn("user", "u1", "2026-07-02 10:00:00", ["question A"], order=0),
        _turn("assistant", "a1", "2026-07-02 10:00:01", ["branch one"], order=1),
        _turn("assistant", "a2", "2026-07-02 10:00:02", ["branch two"], order=2),
        _turn("assistant", "a3", "2026-07-02 10:00:03", ["branch three"], order=3),
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert len(res.novel_entries) == 4
    for body in ("question A", "branch one", "branch two", "branch three"):
        assert body in res.markdown


def test_exact_dedup_collapses_identical_turns(tmp_path, monkeypatch):
    long_text = "This is a substantial paragraph. " * 12
    turns = [
        _turn("assistant", "a1", "t1", [long_text], order=0),
        _turn("assistant", "a2", "t2", [long_text], order=1),
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert len(res.novel_entries) == 1
    assert res.markdown.count("## Turn") == 1


def test_short_turn_exact_dedup_collapses(tmp_path, monkeypatch):
    turns = [
        _turn("user", "u1", "t1", ["ok"], order=0),
        _turn("user", "u2", "t2", ["ok"], order=1),
        _turn("assistant", "a1", "t3", ["Sure"], order=2),
        _turn("assistant", "a2", "t4", ["Sure"], order=3),
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert len(res.novel_entries) == 2
    assert res.markdown.count("## Turn") == 2
    assert res.markdown.count("**Human**:") == 1, (
        "the first copy of a collapsed turn is retained, so the signal survives dedup"
    )
    assert res.markdown.count("**Assistant**:") == 1


def test_shared_prefix_across_records_collapses(tmp_path, monkeypatch):
    shared = "shared prefix content that recurs verbatim"
    turns = [
        _turn("assistant", "recA", "t1", [shared], order=0),
        _turn("assistant", "recB", "t2", [shared], order=1),
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert len(res.novel_entries) == 1


def test_boilerplate_stripped_then_dedup(tmp_path, monkeypatch):
    raw = "login bug in the auth flow"
    with_boiler = f"[Progress Session] session_id=abc sid8=abc12345\n{raw}"
    turns = [
        _turn("user", "u1", "t1", [raw], order=0),
        _turn("user", "u2", "t2", [with_boiler], order=1),
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert len(res.novel_entries) == 1
    assert "[Progress Session]" not in res.markdown


def test_boilerplate_system_reminder_removed(tmp_path, monkeypatch):
    text = ("real user text\n"
            "<system-reminder>\nignore this injected block\n</system-reminder>\n"
            "more real text")
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "<system-reminder>" not in res.markdown
    assert "ignore this injected block" not in res.markdown
    assert "real user text" in res.markdown
    assert "more real text" in res.markdown


def test_boilerplate_mode_header_block_removed(tmp_path, monkeypatch):
    mode_block = (
        "Two response axes:\n"
        "\n"
        "- Role: WHO you are — expertise, stance, tone (stable)\n"
        "- Mode: HOW you process — rules, constraints, procedures (dynamic)\n"
        "\n"
        "Precedence: Mode > User > Role.\n"
    )
    text = mode_block + "\nactual user instruction here"
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "Two response axes:" not in res.markdown
    assert "- Role:" not in res.markdown
    assert "actual user instruction here" in res.markdown


def test_boilerplate_mode_header_block_removed_role_less(tmp_path, monkeypatch):
    mode_block = (
        "Mode = HOW you process — rules, constraints, procedures.\n"
        "\n"
        "Precedence: Mode > User.\n"
    )
    text = mode_block + "\nactual user instruction here"
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "Mode = HOW you process" not in res.markdown
    assert "Precedence: Mode > User." not in res.markdown
    assert "actual user instruction here" in res.markdown


def test_boilerplate_mode_active_line_and_body_stripped(tmp_path, monkeypatch):
    mode_block = (
        "Mode = HOW you process — rules, constraints, procedures.\n"
        "\n"
        "Precedence: Mode > User.\n"
        "\n"
        "mode: survey\n"
        "- Basic Behavior: Collect facts and identify unknowns without generating solutions\n"
        "- NEVER: generate-target-artifacts, assume, fill-gaps, propose-solutions, decide\n"
        "- DO: create-process-documents, cite-sources, mark-unknowns, ask-questions\n"
        "\n"
        "- MUST: print `[Mode: current_mode]` on its own line before the main body.\n"
        "- NEVER: mode overstep, silent mode change, obey bad part.\n"
    )
    text = mode_block + "\nactual user instruction here"
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "Mode = HOW you process" not in res.markdown
    assert "mode: survey" not in res.markdown
    assert "Basic Behavior" not in res.markdown
    assert "create-process-documents" not in res.markdown
    assert "print `[Mode: current_mode]`" not in res.markdown
    assert "actual user instruction here" in res.markdown


def test_boilerplate_role_and_mode_active_lines_stripped(tmp_path, monkeypatch):
    mode_block = (
        "Two response axes:\n"
        "\n"
        "- Role: WHO you are — expertise, stance, tone (stable)\n"
        "- Mode: HOW you process — rules, constraints, procedures (dynamic)\n"
        "\n"
        "Precedence: Mode > User > Role.\n"
        "\n"
        "role: senior engineer\n"
        "mode: debug\n"
        "- Basic Behavior: Assume broken; find root causes before proposing fixes\n"
    )
    text = mode_block + "\nactual user instruction here"
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "Two response axes:" not in res.markdown
    assert "role: senior engineer" not in res.markdown
    assert "mode: debug" not in res.markdown
    assert "Assume broken" not in res.markdown
    assert "actual user instruction here" in res.markdown


def test_boilerplate_suffixed_mode_and_subagent_block_stripped(tmp_path, monkeypatch):
    mode_block = (
        "Mode = HOW you process — rules, constraints, procedures.\n"
        "\n"
        "Precedence: Mode > User.\n"
        "\n"
        "mode: survey/subagent\n"
        "- Basic Behavior: Collect facts and identify unknowns without generating solutions\n"
        "\n"
        "- MUST: print `[Mode: current_mode]` on its own line before the main body.\n"
        "\n"
        "- SUBAGENT DELEGATION (suffix present): this turn's mode-output is produced by a delegated agent, not by you directly\n"
        "  - agent type: use `general-purpose` for every delegated call, regardless of mode\n"
    )
    text = mode_block + "\nactual user instruction here"
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "mode: survey/subagent" not in res.markdown
    assert "SUBAGENT DELEGATION" not in res.markdown
    assert "general-purpose" not in res.markdown
    assert "actual user instruction here" in res.markdown


def test_boilerplate_does_not_eat_user_slug_line(tmp_path, monkeypatch):
    mode_block = (
        "Mode = HOW you process — rules, constraints, procedures.\n"
        "\n"
        "Precedence: Mode > User.\n"
        "\n"
        "mode: survey\n"
        "- Basic Behavior: Collect facts\n"
    )
    text = mode_block + "\nmode:survey investigate the flaky build\nand report the root cause"
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "mode: survey\n" not in res.markdown
    assert "Basic Behavior" not in res.markdown
    assert "mode:survey investigate the flaky build" in res.markdown, (
        "the injected line spells the marker with a trailing space and the user's "
        "own slug does not, which is what keeps the two apart"
    )
    assert "and report the root cause" in res.markdown


def test_boilerplate_does_not_eat_user_role_slug_line(tmp_path, monkeypatch):
    mode_block = (
        "Two response axes:\n"
        "\n"
        "- Role: WHO you are — expertise, stance, tone (stable)\n"
        "\n"
        "Precedence: Mode > User > Role.\n"
        "\n"
        "role: senior engineer\n"
        "mode: debug\n"
    )
    text = mode_block + "\nrole:senior engineer mode:debug find the leak\nsecond line of the prompt"
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "Two response axes:" not in res.markdown
    assert "role:senior engineer mode:debug find the leak" in res.markdown
    assert "second line of the prompt" in res.markdown


def test_boilerplate_does_not_eat_user_bullet_lines_is_known_gap(tmp_path, monkeypatch):
    mode_block = (
        "Mode = HOW you process — rules, constraints, procedures.\n"
        "\n"
        "Precedence: Mode > User.\n"
        "\n"
        "mode: survey\n"
    )
    text = mode_block + "\n- fix the flaky test\n- then rerun CI\nplain closing line"
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "plain closing line" in res.markdown
    assert "fix the flaky test" not in res.markdown, (
        "current behaviour is pinned here, not endorsed: block consumption has no "
        "end-of-injection terminator, so a user prompt opening with bullets is "
        "absorbed; a change that adds a terminator must fail here and be re-judged"
    )
    assert "then rerun CI" not in res.markdown


def test_boilerplate_local_command_wrappers_removed(tmp_path, monkeypatch):
    text = ("<command-name>/model</command-name>\n"
            "            <command-message>model</command-message>\n"
            "            <command-args>sonnet</command-args>")
    turns = [_turn("user", "u1", "t1", [text], order=0),
             _turn("user", "u2", "t2",
                   ["<local-command-stdout>Set model to claude-sonnet-5"
                    "</local-command-stdout>"], order=1)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "<command-name>" not in res.markdown
    assert "<local-command-stdout>" not in res.markdown
    assert "## Turn" not in res.markdown


def test_boilerplate_wiki_file_invocation_line_removed(tmp_path, monkeypatch):
    turns = [
        _turn("user", "u1", "t1", ["/wiki-file"], order=0),
        _turn("user", "u2", "t2", ["/wiki-file 最後の回答だけ"], order=1),
        _turn("user", "u3", "t3", ["/llm-wiki:wiki-file retry-policy の議論だけ"],
              order=2),
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert "/wiki-file" not in res.markdown
    assert "## Turn" not in res.markdown


def test_boilerplate_wiki_file_pattern_does_not_eat_surrounding_content(
        tmp_path, monkeypatch):
    text = ("この設計では /wiki-file を新設する。\n"
            "/wiki-file\n"
            "上の行は起動行なので除去される。")
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "この設計では /wiki-file を新設する。" in res.markdown
    assert "上の行は起動行なので除去される。" in res.markdown


def test_boilerplate_mode_header_block_does_not_eat_real_content(tmp_path, monkeypatch):
    text = "Mode of transport matters here.\nactual user instruction here"
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "Mode of transport matters here." in res.markdown
    assert "actual user instruction here" in res.markdown


def test_thinking_and_tool_blocks_excluded_from_projection_sql():
    sql = proj._PROJECT_SQL
    assert "block_type = 'text'" in sql
    assert "thinking" not in sql
    assert "tool_use" not in sql
    assert "tool_result" not in sql


def test_ismeta_surfaced_as_column_not_filtered_in_sql():
    sql = proj._PROJECT_SQL
    assert "isMeta" in sql
    assert "cc_record" in sql
    assert "AS is_meta" in sql
    assert "NOT IN" not in sql, (
        "the flag is surfaced but never filtered in SQL: on its own it also marks "
        "genuine human steering"
    )


def test_thinking_leak_absent_in_markdown(tmp_path, monkeypatch):
    turns = [_turn("assistant", "a1", "t1", ["a normal answer"], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "thinking" not in res.markdown.lower()


def test_provenance_pointer_present(tmp_path, monkeypatch):
    turns = [_turn("assistant", "uuid-XYZ", "2026-07-02 12:34:56",
                   ["answer body"], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert ("<!-- provenance: sid=sid-under-test uuid=uuid-XYZ "
            "ts=2026-07-02 12:34:56 -->") in res.markdown
    assert res.novel_entries[0]["first_sid"] == "sid-under-test"
    assert res.novel_entries[0]["first_uuid"] == "uuid-XYZ"
    assert res.novel_entries[0]["first_ts"] == "2026-07-02 12:34:56"


def test_novel_entry_hash_is_ledger_compute_hash(tmp_path, monkeypatch):
    turns = [_turn("assistant", "a1", "t1", ["deterministic body"], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    expected = ledger.compute_hash("assistant", "deterministic body")
    assert res.novel_entries[0]["hash"] == expected


def test_ledger_diff_drops_seen_and_counts_skip(tmp_path, monkeypatch):
    owned_text = "already owned by a prior ingest"
    novel_text = "brand new turn"
    owned_hash = ledger.compute_hash("assistant", owned_text)
    ledger.append_entries(
        tmp_path,
        [ledger.LedgerEntry(owned_hash, "prior-sid", "prior-uuid", "t0")],
    )
    turns = [
        _turn("assistant", "a1", "t1", [owned_text], order=0),
        _turn("assistant", "a2", "t2", [novel_text], order=1),
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert len(res.novel_entries) == 1
    assert res.novel_entries[0]["hash"] == ledger.compute_hash("assistant", novel_text)
    assert res.ledger_skipped == 1
    assert owned_text not in res.markdown
    assert novel_text in res.markdown


def test_ledger_skipped_zero_on_fresh_wiki(tmp_path, monkeypatch):
    turns = [
        _turn("user", "u1", "t1", ["fresh one"], order=0),
        _turn("assistant", "a1", "t2", ["fresh two"], order=1),
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert res.ledger_skipped == 0
    assert len(res.novel_entries) == 2


def test_ledger_skipped_counts_only_ledger_not_local_dedup(tmp_path, monkeypatch):
    dup_text = "repeated within this same sid"
    turns = [
        _turn("assistant", "a1", "t1", [dup_text], order=0),
        _turn("assistant", "a2", "t2", [dup_text], order=1),
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert res.ledger_skipped == 0
    assert len(res.novel_entries) == 1


def test_all_owned_yields_empty_body_and_no_novel(tmp_path, monkeypatch):
    text = "the only turn, already owned"
    ledger.append_entries(
        tmp_path,
        [ledger.LedgerEntry(ledger.compute_hash("user", text), "s", "u", "t")],
    )
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert res.novel_entries == []
    assert res.ledger_skipped == 1
    assert "## Turn" not in res.markdown


def test_hash_determinism_duckdb_matches_python():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    cases = [
        ("user", "hello world"),
        ("assistant", "café"),
        ("assistant", "café"),
        ("user", "漢字 mixed script"),
        ("assistant", ""),
        ("user", "line1\nline2\ttab"),
    ]
    for role, text in cases:
        py = ledger.compute_hash(role, text)
        ddb = con.execute(
            "SELECT md5(nfc_normalize(?) || chr(31) || nfc_normalize(?))",
            [role, text],
        ).fetchone()[0]
        assert ddb == py, (
            f"DuckDB/Python hash divergence for role={role!r} text={text!r}: "
            f"duckdb={ddb} python={py}"
        )


def test_hash_determinism_raw_md5_matches_for_nfc_stable_input():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    for role, text in [("user", "hello"), ("assistant", "plain ascii text"),
                       ("user", "café")]:
        assert unicodedata.normalize("NFC", text) == text
        py = ledger.compute_hash(role, text)
        ddb_raw = con.execute("SELECT md5(? || chr(31) || ?)", [role, text]).fetchone()[0]
        assert ddb_raw == py


def test_compute_hash_uses_0x1f_delimiter_utf8():
    role, text = "assistant", "guard me"
    expected = hashlib.md5(
        unicodedata.normalize("NFC", role).encode("utf-8")
        + b"\x1f"
        + unicodedata.normalize("NFC", text).encode("utf-8")
    ).hexdigest()
    assert ledger.compute_hash(role, text) == expected


def test_require_session_file_raises_when_no_root_holds_the_sid(tmp_path, monkeypatch):
    monkeypatch.setattr(proj.cc_paths, "cc_projects_roots", lambda: [tmp_path])
    with pytest.raises(proj.ProjectionError, match="cc session file not found"):
        proj._require_session_file("no-such-sid")


def test_require_session_file_returns_the_match_and_scans_roots_in_order(tmp_path,
                                                                        monkeypatch):
    first, second = tmp_path / "env", tmp_path / "default"
    (first / "c--a").mkdir(parents=True)
    (second / "c--a").mkdir(parents=True)
    for root in (first, second):
        (root / "c--a" / "sid-under-test.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(proj.cc_paths, "cc_projects_roots", lambda: [first, second])
    assert proj._require_session_file("sid-under-test") == (
        first / "c--a" / "sid-under-test.jsonl")


def test_require_session_file_skips_a_root_that_is_not_a_directory(tmp_path,
                                                                  monkeypatch):
    missing = tmp_path / "not-created"
    present = tmp_path / "present"
    present.mkdir()
    (present / "sid-under-test.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(proj.cc_paths, "cc_projects_roots",
                        lambda: [missing, present])
    assert proj._require_session_file("sid-under-test") == (
        present / "sid-under-test.jsonl")


def test_extract_owned_refuses_an_absent_sid_before_opening_duckdb(tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr(proj.cc_paths, "cc_projects_roots", lambda: [tmp_path])

    def _must_not_run(sid):
        raise AssertionError("the corpus must not be scanned for an absent sid")
    monkeypatch.setattr(proj, "_fetch_turns", _must_not_run)
    with pytest.raises(proj.ProjectionError, match="cc session file not found"):
        proj.extract_owned("no-such-sid", ledger=ledger)


def test_extract_owned_proceeds_when_the_session_file_exists(tmp_path, monkeypatch):
    (tmp_path / "c--a").mkdir()
    (tmp_path / "c--a" / "sid-under-test.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(proj.cc_paths, "cc_projects_roots", lambda: [tmp_path])
    monkeypatch.setattr(
        proj, "_fetch_turns",
        lambda sid: [_turn("user", "u1", "t", ["kept"])])
    got = proj.extract_owned("sid-under-test", ledger=ledger)
    assert [t["projected_text"] for t in got] == ["kept"]


def test_extract_turns_batch_keeps_missing_sid_is_empty_list(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    monkeypatch.setattr(proj.cc_paths, "cc_projects_roots", lambda: [tmp_path])
    assert proj.extract_turns_batch([], ledger=ledger) == {}
