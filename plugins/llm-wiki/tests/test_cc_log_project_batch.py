"""Tests: cc_log_project batch split + begin --turns path (R1 / F-H1).

Covers the R1 (case-A scan-collapse) refactor of the projector:
  - `project_from_turns` consumes pre-extracted turn DICTS (JSON hand-off shape)
    WITHOUT opening DuckDB, and reproduces the dedup + ledger-diff + markdown of
    the composed `project_owned` on the same turns;
  - a turn dict carries its F5 hash (assigned at extraction), and
    `project_from_turns` uses THAT hash (single source of truth) — the same
    (role, text) collapses, a ledger-seen hash is dropped and counted;
  - `_group_rows_to_turns` groups projection rows (session_id column present) by
    record_uuid, pairing tool_result under its tool_use, thinking excluded by the
    SQL (rows never include it);
  - `_turn_to_dict` is JSON-safe (round-trips through json.dumps/loads) and its
    tool_input normalization keeps `_render_tool_use` output identical;
  - `extract_turns_batch` splits multi-sid rows per sid (via a monkeypatched row
    source so the test is hermetic — the real DuckDB scan is covered by the E2E
    parity check on the live corpus, not here).

The begin `--turns` NO-RESCAN contract (begin must NOT call `_fetch_turns` when
`--turns` is given) lives in test_path_b_loop_parity.py alongside the loop parity.
"""
import json

import pytest

from llmwiki.ingest import cc_log_project as proj
from llmwiki.ingest import ledger


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _turn(role, uuid, ts, texts, tool_uses=None, order=0):
    return proj._Turn(
        role=role, uuid=uuid, ts=ts,
        text_parts=list(texts),
        tool_uses=list(tool_uses or []),
        order=order,
    )


# a projection row matches _PROJECT_COLUMNS order:
#   record_uuid, session_id, role, ts_str, block_index, block_type, text,
#   tool_name, tool_input, tool_use_id, tool_result_content
def _row(uuid, sid, role, ts, bi, btype, text=None, tool_name=None,
         tool_input=None, tuid=None, trc=None):
    return (uuid, sid, role, ts, bi, btype, text, tool_name, tool_input, tuid, trc)


# --------------------------------------------------------------------------- #
# project_from_turns == project_owned on the same turns (parity, no DuckDB)
# --------------------------------------------------------------------------- #
def test_project_from_turns_parity_with_project_owned(tmp_path, monkeypatch):
    turns = [
        _turn("user", "u1", "2026-07-02 10:00:00", ["question A"], order=0),
        _turn("assistant", "a1", "2026-07-02 10:00:01", ["answer A"], order=1),
        _turn("user", "u2", "2026-07-02 10:00:02", ["question A"], order=2),  # dup text, new uuid
    ]
    # project_owned composes _fetch_turns -> _turn_to_dict -> project_from_turns.
    monkeypatch.setattr(proj, "_fetch_turns", lambda sid: list(turns))
    owned = proj.project_owned(tmp_path, "sid-x", ledger=ledger)

    # Now do it via the split path explicitly: build dicts, call project_from_turns.
    dicts = [proj._turn_to_dict(t, ledger=ledger) for t in turns]
    split = proj.project_from_turns(tmp_path, "sid-x", dicts, ledger=ledger)

    assert split.markdown == owned.markdown
    assert split.novel_entries == owned.novel_entries
    assert split.ledger_skipped == owned.ledger_skipped
    # "question A" appears twice with different uuids -> within-sid exact dedup
    # collapses to one novel entry (u1's answer + one "question A").
    hashes = [e["hash"] for e in split.novel_entries]
    assert len(hashes) == len(set(hashes))  # no dup hash survives


def test_project_from_turns_does_not_open_duckdb(tmp_path, monkeypatch):
    """project_from_turns must be pure (no DuckDB): blow up _fetch_turns to prove."""
    def _boom(sid):
        raise AssertionError("project_from_turns opened DuckDB via _fetch_turns")
    monkeypatch.setattr(proj, "_fetch_turns", _boom)
    dicts = [proj._turn_to_dict(_turn("user", "u1", "t", ["hi"]), ledger=ledger)]
    res = proj.project_from_turns(tmp_path, "sid", dicts, ledger=ledger)
    assert "hi" in res.markdown
    assert len(res.novel_entries) == 1


def test_project_from_turns_uses_carried_hash(tmp_path):
    """The dict's own `hash` is the dedup/ledger key (single source of truth)."""
    d = proj._turn_to_dict(_turn("user", "u1", "t", ["hello world"]), ledger=ledger)
    # the carried hash must equal ledger.compute_hash of the projected text
    assert d["hash"] == ledger.compute_hash("user", "hello world")
    res = proj.project_from_turns(tmp_path, "sid", [d], ledger=ledger)
    assert res.novel_entries[0]["hash"] == d["hash"]


def test_project_from_turns_ledger_diff_drops_and_counts(tmp_path, monkeypatch):
    d1 = proj._turn_to_dict(_turn("user", "u1", "t1", ["owned already"]), ledger=ledger)
    d2 = proj._turn_to_dict(_turn("assistant", "a1", "t2", ["novel one"]), ledger=ledger)
    # pretend d1's hash is already owned by a prior ingest
    monkeypatch.setattr(ledger, "read_seen_hashes", lambda root: {d1["hash"]})
    res = proj.project_from_turns(tmp_path, "sid", [d1, d2], ledger=ledger)
    assert res.ledger_skipped == 1
    assert [e["hash"] for e in res.novel_entries] == [d2["hash"]]
    assert "novel one" in res.markdown
    assert "owned already" not in res.markdown


# --------------------------------------------------------------------------- #
# _turn_to_dict JSON-safety + tool_input normalization
# --------------------------------------------------------------------------- #
def test_turn_to_dict_is_json_safe():
    t = _turn("assistant", "a1", "ts", ["did a thing"],
              tool_uses=[("Bash", '{"command": "ls"}', "tuid1", "output")])
    d = proj._turn_to_dict(t, ledger=ledger)
    # round-trips through JSON unchanged
    assert json.loads(json.dumps(d, ensure_ascii=False)) == d
    assert d["tool_uses"][0]["name"] == "Bash"
    assert d["tool_uses"][0]["tool_input"] == '{"command": "ls"}'
    assert d["tool_uses"][0]["result"] == "output"


def test_turn_to_dict_tool_input_dict_becomes_json_string():
    # a dict tool_input (rather than a DuckDB json string) is dumped to a string
    t = _turn("assistant", "a1", "ts", [""],
              tool_uses=[("Write", {"file_path": "/x/y.md"}, "t2", "")])
    d = proj._turn_to_dict(t, ledger=ledger)
    ti = d["tool_uses"][0]["tool_input"]
    assert isinstance(ti, str)
    assert json.loads(ti) == {"file_path": "/x/y.md"}
    # and _render_tool_use produces the same display from the normalized string
    assert proj._render_tool_use("Write", ti) == "Write: /x/y.md"


# --------------------------------------------------------------------------- #
# _group_rows_to_turns: record_uuid grouping, tool_result pairing, ordering
# --------------------------------------------------------------------------- #
def test_group_rows_pairs_tool_result_under_tool_use():
    rows = [
        _row("a1", "s", "assistant", "10:00:00", 0, "text", text="calling"),
        _row("a1", "s", "assistant", "10:00:00", 1, "tool_use",
             tool_name="Bash", tool_input='{"command":"ls"}', tuid="T1"),
        _row("u1", "s", "user", "10:00:01", 0, "tool_result", tuid="T1",
             trc="file listing"),
    ]
    turns = proj._group_rows_to_turns(rows)
    # the tool_result row is folded under the tool_use, NOT a standalone turn
    assert [t.uuid for t in turns] == ["a1"]
    assert turns[0].tool_uses[0][0] == "Bash"
    assert turns[0].tool_uses[0][3] == "file listing"  # paired result


def test_group_rows_multi_text_same_uuid_concatenates():
    # the synthesized replay record shape: one uuid, many text blocks (R2 grain)
    rows = [
        _row("u1", "s", "user", "10:00:00", 0, "text", text="USER: hi"),
        _row("u1", "s", "user", "10:00:00", 1, "text", text="ASSISTANT: hello"),
        _row("u1", "s", "user", "10:00:00", 2, "text", text="USER: bye"),
    ]
    turns = proj._group_rows_to_turns(rows)
    assert len(turns) == 1  # ONE record-grain turn (not split into 3)
    assert turns[0].text_parts == ["USER: hi", "ASSISTANT: hello", "USER: bye"]


# --------------------------------------------------------------------------- #
# extract_turns_batch: multi-sid split (hermetic — monkeypatch the row source)
# --------------------------------------------------------------------------- #
def test_extract_turns_batch_splits_per_sid(monkeypatch):
    # Rows for two sids interleaved; the batch query returns them all in one scan.
    all_rows = [
        _row("a", "sidA", "user", "10:00:00", 0, "text", text="A first"),
        _row("b", "sidB", "user", "11:00:00", 0, "text", text="B first"),
        _row("a2", "sidA", "assistant", "10:00:01", 0, "text", text="A second"),
    ]

    class _FakeCon:
        # extract_turns_batch calls con.execute(views_sql) [no params], then
        # con.execute(projection_sql, sids) [with params]. Only the second yields
        # rows; the first (view definitions) is a no-op here.
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
    # each turn carries its F5 hash
    assert out["sidA"][0]["hash"] == ledger.compute_hash("user", "A first")


def test_extract_turns_batch_empty_sids_returns_empty():
    assert proj.extract_turns_batch([], ledger=ledger) == {}
