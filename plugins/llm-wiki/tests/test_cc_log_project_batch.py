import json

import pytest

from llmwiki.ingest import cc_log_project as proj
from llmwiki.ingest import ledger


def _turn(role, uuid, ts, texts, order=0):
    return proj._Turn(
        role=role, uuid=uuid, ts=ts,
        text_parts=list(texts),
        order=order,
    )


def _row(uuid, sid, role, ts, bi, btype, text=None, is_meta=False):
    return (uuid, sid, role, ts, bi, btype, text, is_meta)


def test_project_from_turns_parity_with_project_owned(tmp_path, monkeypatch):
    turns = [
        _turn("user", "u1", "2026-07-02 10:00:00", ["question A"], order=0),
        _turn("assistant", "a1", "2026-07-02 10:00:01", ["answer A"], order=1),
        _turn("user", "u2", "2026-07-02 10:00:02", ["question A"], order=2),
    ]
    monkeypatch.setattr(proj, "_fetch_turns", lambda sid: list(turns))
    monkeypatch.setattr(proj, "_require_session_file", lambda sid: None)
    owned = proj.project_owned(tmp_path, "sid-x", ledger=ledger)

    dicts = [proj._turn_to_dict(t, ledger=ledger) for t in turns]
    split = proj.project_from_turns(tmp_path, "sid-x", dicts, ledger=ledger)

    assert split.markdown == owned.markdown
    assert split.novel_entries == owned.novel_entries
    assert split.ledger_skipped == owned.ledger_skipped
    hashes = [e["hash"] for e in split.novel_entries]
    assert len(hashes) == len(set(hashes))


def test_project_from_turns_does_not_open_duckdb(tmp_path, monkeypatch):
    def _boom(sid):
        raise AssertionError("project_from_turns opened DuckDB via _fetch_turns")
    monkeypatch.setattr(proj, "_fetch_turns", _boom)
    dicts = [proj._turn_to_dict(_turn("user", "u1", "t", ["hi"]), ledger=ledger)]
    res = proj.project_from_turns(tmp_path, "sid", dicts, ledger=ledger)
    assert "hi" in res.markdown
    assert len(res.novel_entries) == 1


def test_project_from_turns_uses_carried_hash(tmp_path):
    d = proj._turn_to_dict(_turn("user", "u1", "t", ["hello world"]), ledger=ledger)
    assert d["hash"] == ledger.compute_hash("user", "hello world")
    res = proj.project_from_turns(tmp_path, "sid", [d], ledger=ledger)
    assert res.novel_entries[0]["hash"] == d["hash"]


def test_project_from_turns_ledger_diff_drops_and_counts(tmp_path, monkeypatch):
    d1 = proj._turn_to_dict(_turn("user", "u1", "t1", ["owned already"]), ledger=ledger)
    d2 = proj._turn_to_dict(_turn("assistant", "a1", "t2", ["novel one"]), ledger=ledger)
    monkeypatch.setattr(ledger, "read_seen_hashes", lambda root: {d1["hash"]})
    res = proj.project_from_turns(tmp_path, "sid", [d1, d2], ledger=ledger)
    assert res.ledger_skipped == 1
    assert [e["hash"] for e in res.novel_entries] == [d2["hash"]]
    assert "novel one" in res.markdown
    assert "owned already" not in res.markdown


def test_turn_to_dict_is_json_safe():
    t = _turn("assistant", "a1", "ts", ["did a thing"])
    d = proj._turn_to_dict(t, ledger=ledger)
    assert json.loads(json.dumps(d, ensure_ascii=False)) == d
    assert set(d.keys()) == {"role", "uuid", "ts", "projected_text", "hash"}


def test_group_rows_multi_text_same_uuid_concatenates():
    rows = [
        _row("u1", "s", "user", "10:00:00", 0, "text", text="USER: hi"),
        _row("u1", "s", "user", "10:00:00", 1, "text", text="ASSISTANT: hello"),
        _row("u1", "s", "user", "10:00:00", 2, "text", text="USER: bye"),
    ]
    turns = proj._group_rows_to_turns(rows)
    assert len(turns) == 1, "text blocks sharing a record_uuid form one record-grain turn"
    assert turns[0].text_parts == ["USER: hi", "ASSISTANT: hello", "USER: bye"]


def test_meta_noise_record_dropped_when_flag_and_pattern_both_hold():
    rows = [
        _row("u1", "s", "user", "10:00:00", 0, "text",
             text="pj:llm-wiki /progress start some-task"),
        _row("m1", "s", "user", "10:00:01", 0, "text", is_meta=True,
             text="Base directory for this skill: c:\\plugins\\taskflow\\skills\\progress\n\n# /progress\n..."),
    ]
    turns = proj._group_rows_to_turns(rows)
    assert [t.uuid for t in turns] == ["u1"]


def test_meta_flag_without_denylist_match_is_kept():
    rows = [
        _row("m1", "s", "user", "10:00:00", 0, "text", is_meta=True,
             text="mode:survey 1の実測だけやれ"),
        _row("m2", "s", "user", "10:00:01", 0, "text", is_meta=True,
             text="The coordinator sent a message while you were working:\n"
                  "追加検証を実施せよ。"),
    ]
    turns = proj._group_rows_to_turns(rows)
    assert [t.uuid for t in turns] == ["m1", "m2"]


def test_denylist_match_without_meta_flag_is_kept():
    rows = [
        _row("u1", "s", "user", "10:00:00", 0, "text", is_meta=False,
             text="Stop hook feedback: why does this line keep appearing?"),
    ]
    turns = proj._group_rows_to_turns(rows)
    assert [t.uuid for t in turns] == ["u1"]


@pytest.mark.parametrize("noise", [
    "Base directory for this skill: /x/y",
    "Skill /progress is already loaded above; instructions unchanged. Arguments: start",
    "(Re-invocation of /officecli — the skill instructions were previously loaded.)",
    "<local-command-caveat>Caveat: ...</local-command-caveat>",
    "Stop hook feedback:\n[progress capture] session=abc",
    "Your tool call was malformed and could not be parsed. Please retry.",
    "The previous response failed to produce a valid tool call. Please retry the tool call now.",
    "Continue from where you left off.",
    "[Your previous response had no visible output. Please continue.]",
    "[Image: original 3259x406, displayed at 2000x249.]",
])
def test_is_meta_noise_matches_every_verified_harness_shape(noise):
    assert proj._is_meta_noise(noise) is True


@pytest.mark.parametrize("kept", [
    "The coordinator sent a message while you were working:\n読み取り専用で検証せよ。",
    "続き: progress-router の結果を受けて Step 4 へ進む",
    "mode:survey 1の実測だけやれ",
    "<task-notification>\n<task-id>abc</task-id>\n<result>real content</result>",
    "<command-name>/model</command-name>",
    "<local-command-stdout>Set model to claude-sonnet-5</local-command-stdout>",
    "",
])
def test_is_meta_noise_leaves_content_bearing_shapes_alone(kept):
    assert proj._is_meta_noise(kept) is False


def test_extract_turns_batch_splits_per_sid(monkeypatch):
    all_rows = [
        _row("a", "sidA", "user", "10:00:00", 0, "text", text="A first"),
        _row("b", "sidB", "user", "11:00:00", 0, "text", text="B first"),
        _row("a2", "sidA", "assistant", "10:00:01", 0, "text", text="A second"),
    ]

    class _FakeCon:
        def execute(self, sql, params=None):
            self._rows = all_rows if params else []
            return self
        def fetchall(self):
            return self._rows

    monkeypatch.setattr(proj.duckdb, "connect", lambda: _FakeCon())
    out = proj.extract_turns_batch(["sidA", "sidB"], ledger=ledger)
    assert set(out.keys()) == {"sidA", "sidB"}
    assert [t["projected_text"] for t in out["sidA"]] == ["A first", "A second"]
    assert [t["projected_text"] for t in out["sidB"]] == ["B first"]
    assert out["sidA"][0]["hash"] == ledger.compute_hash("user", "A first")


def test_extract_turns_batch_empty_sids_returns_empty():
    assert proj.extract_turns_batch([], ledger=ledger) == {}
