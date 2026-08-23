import json

import pytest

from llmwiki.ingest import pi_log_project as pilp
from llmwiki.ingest import ledger


def _msg(entry_id, parent_id, role, ts, text):
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": ts,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _msg_str(entry_id, parent_id, role, ts, text):
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": ts,
        "message": {"role": role, "content": text},
    }


def _write_session(session_dir, sid, ts_prefix, entries, cwd="/synthetic/cwd"):
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{ts_prefix}_{sid}.jsonl"
    header = {"type": "session", "version": 1, "id": sid, "cwd": cwd}
    lines = [header] + entries
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _use_agent_dir(tmp_path, monkeypatch):
    agent_dir = tmp_path / "agentdir"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    return pilp._session_dir()


def test_extract_turns_batch_one_walk_for_multiple_sids(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    sessions_dir = _use_agent_dir(tmp_path, monkeypatch)
    sid_a = "aaaaaaaa-0000-0000-0000-000000000001"
    sid_b = "bbbbbbbb-0000-0000-0000-000000000002"
    _write_session(
        sessions_dir / "--projA--", sid_a, "2026-07-02T09-00-00-000Z",
        [_msg("a1", None, "user", "2026-07-02T09:00:00.000Z", "A first")])
    _write_session(
        sessions_dir / "--projB--", sid_b, "2026-07-02T10-00-00-000Z",
        [_msg("b1", None, "user", "2026-07-02T10:00:00.000Z", "B first")])

    from pathlib import Path as _Path
    calls = []
    orig_rglob = _Path.rglob

    def counting_rglob(self, pattern):
        calls.append((self, pattern))
        return orig_rglob(self, pattern)

    monkeypatch.setattr(_Path, "rglob", counting_rglob)

    out = pilp.extract_turns_batch([sid_a, sid_b], ledger=ledger)

    assert len(calls) == 1, "one filesystem walk covers every requested sid, not one per sid"
    assert set(out.keys()) == {sid_a, sid_b}
    assert [t["text"] for t in out[sid_a]] == ["A first"]
    assert [t["text"] for t in out[sid_b]] == ["B first"]


def test_extract_turns_batch_missing_sid_returns_empty_list(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    sessions_dir = _use_agent_dir(tmp_path, monkeypatch)
    sid_present = "cccccccc-0000-0000-0000-000000000003"
    _write_session(
        sessions_dir / "--projC--", sid_present, "2026-07-02T09-00-00-000Z",
        [_msg("c1", None, "user", "2026-07-02T09:00:00.000Z", "present")])

    out = pilp.extract_turns_batch(
        [sid_present, "no-such-sid-at-all"], ledger=ledger)
    assert out["no-such-sid-at-all"] == []
    assert len(out[sid_present]) == 1


def test_extract_turns_batch_empty_sids_returns_empty(tmp_path, monkeypatch):
    _use_agent_dir(tmp_path, monkeypatch)
    assert pilp.extract_turns_batch([], ledger=ledger) == {}


def test_extract_turns_batch_assigns_hash(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    sessions_dir = _use_agent_dir(tmp_path, monkeypatch)
    sid = "dddddddd-0000-0000-0000-000000000004"
    _write_session(
        sessions_dir / "--projD--", sid, "2026-07-02T09-00-00-000Z",
        [_msg("d1", None, "user", "2026-07-02T09:00:00.000Z", "hello world")])

    out = pilp.extract_turns_batch([sid], ledger=ledger)
    assert out[sid][0]["hash"] == ledger.compute_hash("user", "hello world")


def _turn(entry_id, parent_id, role, ts, text, *, hash_=None):
    d = {"id": entry_id, "parentId": parent_id, "role": role, "ts": ts, "text": text}
    if hash_ is not None:
        d["hash"] = hash_
    return d


def test_project_from_turns_within_sid_dedup(tmp_path):
    t1 = pilp._assign_hash(
        _turn("u1", None, "user", "t1", "question A"), ledger=ledger)
    t2 = pilp._assign_hash(
        _turn("u2", "u1", "user", "t2", "question A"), ledger=ledger)
    t3 = pilp._assign_hash(
        _turn("a1", "u2", "assistant", "t3", "answer A"), ledger=ledger)

    res = pilp.project_from_turns(tmp_path, "sid-x", [t1, t2, t3], ledger=ledger)
    hashes = [e["hash"] for e in res.novel_entries]
    assert len(hashes) == len(set(hashes))
    assert len(res.novel_entries) == 2


def test_project_from_turns_ledger_diff_drops_and_counts(tmp_path, monkeypatch):
    d1 = pilp._assign_hash(
        _turn("u1", None, "user", "t1", "owned already"), ledger=ledger)
    d2 = pilp._assign_hash(
        _turn("a1", "u1", "assistant", "t2", "novel one"), ledger=ledger)
    monkeypatch.setattr(ledger, "read_seen_hashes", lambda root: {d1["hash"]})

    res = pilp.project_from_turns(tmp_path, "sid", [d1, d2], ledger=ledger)
    assert res.ledger_skipped == 1
    assert [e["hash"] for e in res.novel_entries] == [d2["hash"]]
    assert "novel one" in res.markdown
    assert "owned already" not in res.markdown


def test_project_from_turns_does_not_recompute_hash(tmp_path):
    d = pilp._assign_hash(
        _turn("u1", None, "user", "t1", "hello"), ledger=ledger)
    real_hash = d["hash"]
    d["hash"] = "TAMPERED_" + real_hash

    res = pilp.project_from_turns(tmp_path, "sid", [d], ledger=ledger)
    assert res.novel_entries[0]["hash"] == "TAMPERED_" + real_hash
    assert res.novel_entries[0]["hash"] != real_hash


def test_project_from_turns_missing_hash_raises_projection_error(tmp_path):
    d = _turn("u1", None, "user", "t1", "has no hash key")
    with pytest.raises(pilp.ProjectionError, match="hash"):
        pilp.project_from_turns(tmp_path, "sid", [d], ledger=ledger)


def test_project_from_turns_empty_text_turn_skips_before_hash_check(tmp_path):
    d = _turn("u1", None, "user", "t1", "")
    res = pilp.project_from_turns(tmp_path, "sid", [d], ledger=ledger)
    assert res.novel_entries == []


def test_project_owned_equals_batch_then_project_composition(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    sessions_dir = _use_agent_dir(tmp_path, monkeypatch)
    sid = "eeeeeeee-0000-0000-0000-000000000005"
    _write_session(
        sessions_dir / "--projE--", sid, "2026-07-02T09-00-00-000Z",
        [
            _msg("e1", None, "user", "2026-07-02T09:00:00.000Z", "question A"),
            _msg("e2", "e1", "assistant", "2026-07-02T09:00:01.000Z", "answer A"),
            _msg("e3", "e2", "user", "2026-07-02T09:00:02.000Z", "question A"),
        ])

    wiki_root_a = tmp_path / "wiki_a"
    wiki_root_a.mkdir()
    wiki_root_b = tmp_path / "wiki_b"
    wiki_root_b.mkdir()

    owned = pilp.project_owned(wiki_root_a, sid, ledger=ledger)

    batch = pilp.extract_turns_batch([sid], ledger=ledger)
    split = pilp.project_from_turns(wiki_root_b, sid, batch[sid], ledger=ledger)

    assert split.markdown == owned.markdown
    assert split.novel_entries == owned.novel_entries
    assert split.ledger_skipped == owned.ledger_skipped
    assert len(owned.novel_entries) == 2


def test_extract_turns_batch_varchar_content_only(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    sessions_dir = _use_agent_dir(tmp_path, monkeypatch)
    sid = "ffffffff-0000-0000-0000-000000000006"
    _write_session(
        sessions_dir / "--projF--", sid, "2026-07-02T09-00-00-000Z",
        [
            _msg_str("f1", None, "user", "2026-07-02T09:00:00.000Z",
                      "plain string question"),
            _msg_str("f2", "f1", "assistant", "2026-07-02T09:00:01.000Z",
                      "plain string answer"),
        ])

    out = pilp.extract_turns_batch([sid], ledger=ledger)
    assert [t["text"] for t in out[sid]] == [
        "plain string question", "plain string answer"]


def test_project_owned_mixed_varchar_and_array_content(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    sessions_dir = _use_agent_dir(tmp_path, monkeypatch)
    sid = "11111111-1111-0000-0000-000000000007"
    _write_session(
        sessions_dir / "--projG--", sid, "2026-07-02T09-00-00-000Z",
        [
            _msg("g1", None, "user", "2026-07-02T09:00:00.000Z",
                 "question A"),
            _msg_str("g2", "g1", "assistant", "2026-07-02T09:00:01.000Z",
                      "answer A"),
            _msg_str("g3", "g2", "user", "2026-07-02T09:00:02.000Z",
                      "question A"),
        ])

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    res = pilp.project_owned(wiki_root, sid, ledger=ledger)

    assert len(res.novel_entries) == 2
    assert "question A" in res.markdown
    assert "answer A" in res.markdown
    assert res.markdown.index("question A") < res.markdown.index("answer A")


_WIKI_FILE_BODY = (
    "# /wiki-file\n"
    "\n"
    "Arguments: `retry-policy の議論だけ`\n"
    "\n"
    "You are the filing orchestrator for the RUNNING session.\n"
)


def test_is_command_invocation_matches_every_shipped_h1():
    for h1 in pilp._INVOCATION_H1S:
        turn = {"role": "user", "text": f"{h1}\n\nArguments: ``\n\nbody text\n"}
        assert pilp._is_command_invocation(turn), h1


def test_is_command_invocation_requires_the_user_role():
    turn = {"role": "assistant", "text": _WIKI_FILE_BODY}
    assert not pilp._is_command_invocation(turn)


def test_is_command_invocation_ignores_a_mere_mention():
    assert not pilp._is_command_invocation(
        {"role": "user", "text": "how does # /wiki-file decide the cutoff?"})
    assert not pilp._is_command_invocation(
        {"role": "user", "text": "context first\n\n# /wiki-file\n"})
    assert not pilp._is_command_invocation(
        {"role": "user", "text": "# /wiki-file-ish"})
    assert not pilp._is_command_invocation({"role": "user", "text": ""})


def test_blank_command_invocations_keeps_position_and_role():
    turns = [
        {"role": "user", "text": "real question", "id": "u1"},
        {"role": "assistant", "text": "real answer", "id": "a1"},
        {"role": "user", "text": _WIKI_FILE_BODY, "id": "u2"},
    ]
    out = pilp._blank_command_invocations(turns)

    assert [t["role"] for t in out] == ["user", "assistant", "user"]
    assert [t["id"] for t in out] == ["u1", "a1", "u2"]
    assert out[2]["text"] == ""
    assert [t["text"] for t in out[:2]] == ["real question", "real answer"]
    assert turns[2]["text"] == _WIKI_FILE_BODY


def test_extract_owned_blanks_the_invocation_turn(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    sessions_dir = _use_agent_dir(tmp_path, monkeypatch)
    sid = "dddddddd-0000-0000-0000-000000000010"
    _write_session(
        sessions_dir / "--projD7A--", sid, "2026-08-12T09-00-00-000Z",
        [
            _msg("d1", None, "user", "2026-08-12T09:00:00.000Z", "real question"),
            _msg("d2", "d1", "assistant", "2026-08-12T09:00:01.000Z", "real answer"),
            _msg("d3", "d2", "user", "2026-08-12T09:00:02.000Z", _WIKI_FILE_BODY),
        ])

    turns = pilp.extract_owned(sid, ledger=ledger)

    assert [t["text"] for t in turns] == ["real question", "real answer", ""]
    assert turns[-1]["role"] == "user", (
        "the invocation turn is blanked and kept, never dropped, so it stays the "
        "cutoff anchor"
    )


def test_extract_turns_batch_blanks_the_invocation_turn(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    sessions_dir = _use_agent_dir(tmp_path, monkeypatch)
    sid = "dddddddd-0000-0000-0000-000000000011"
    _write_session(
        sessions_dir / "--projD7B--", sid, "2026-08-12T09-00-00-000Z",
        [
            _msg("e1", None, "user", "2026-08-12T09:00:00.000Z", "kept"),
            _msg("e2", "e1", "user", "2026-08-12T09:00:01.000Z",
                 "# /wiki-ingest-sessions\n\nArguments: ``\n\nbody\n"),
        ])

    out = pilp.extract_turns_batch([sid], ledger=ledger)
    assert [t["text"] for t in out[sid]] == ["kept", ""]


def test_blanked_invocation_never_reaches_the_wiki_or_the_ledger(tmp_path,
                                                                monkeypatch):
    pytest.importorskip("duckdb")
    sessions_dir = _use_agent_dir(tmp_path, monkeypatch)
    sid = "dddddddd-0000-0000-0000-000000000012"
    _write_session(
        sessions_dir / "--projD7C--", sid, "2026-08-12T09-00-00-000Z",
        [
            _msg("f1", None, "user", "2026-08-12T09:00:00.000Z", "real question"),
            _msg("f2", "f1", "assistant", "2026-08-12T09:00:01.000Z", "real answer"),
            _msg("f3", "f2", "user", "2026-08-12T09:00:02.000Z", _WIKI_FILE_BODY),
        ])

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    res = pilp.project_owned(wiki_root, sid, ledger=ledger)

    assert "You are the filing orchestrator" not in res.markdown
    assert "/wiki-file" not in res.markdown
    assert res.ledger_skipped == 0
    assert len(res.novel_entries) == 2
