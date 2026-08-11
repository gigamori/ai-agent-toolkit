"""Tests: cc_log_project batch split + begin --turns path (R1 / F-H1).

Covers the R1 (case-A scan-collapse) refactor of the projector:
  - `project_from_turns` consumes pre-extracted turn DICTS (JSON hand-off shape)
    WITHOUT opening DuckDB, and reproduces the dedup + ledger-diff + markdown of
    the composed `project_owned` on the same turns;
  - a turn dict carries its F5 hash (assigned at extraction), and
    `project_from_turns` uses THAT hash (single source of truth) — the same
    (role, text) collapses, a ledger-seen hash is dropped and counted;
  - `_group_rows_to_turns` groups projection rows (session_id column present) by
    record_uuid; rows are text-only (D5) by the SQL, so no
    tool_use/tool_result/thinking row ever reaches this function, and the D12
    meta-noise drop (is_meta AND denylist) is applied at record grain here;
  - `_turn_to_dict` is JSON-safe (round-trips through json.dumps/loads);
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
def _turn(role, uuid, ts, texts, order=0):
    return proj._Turn(
        role=role, uuid=uuid, ts=ts,
        text_parts=list(texts),
        order=order,
    )


# a projection row matches _PROJECT_COLUMNS order (D5: text-only, no tool cols;
# D12: is_meta surfaced as the trailing column):
#   record_uuid, session_id, role, ts_str, block_index, block_type, text, is_meta
def _row(uuid, sid, role, ts, bi, btype, text=None, is_meta=False):
    return (uuid, sid, role, ts, bi, btype, text, is_meta)


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
    # Path A's absent-sid gate has no filesystem to find "sid-x" in — this test
    # is about the split/compose parity, not the gate (covered in
    # test_cc_log_project.py).
    monkeypatch.setattr(proj, "_require_session_file", lambda sid: None)
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
# _turn_to_dict JSON-safety
# --------------------------------------------------------------------------- #
def test_turn_to_dict_is_json_safe():
    t = _turn("assistant", "a1", "ts", ["did a thing"])
    d = proj._turn_to_dict(t, ledger=ledger)
    # round-trips through JSON unchanged
    assert json.loads(json.dumps(d, ensure_ascii=False)) == d
    assert set(d.keys()) == {"role", "uuid", "ts", "projected_text", "hash"}


# --------------------------------------------------------------------------- #
# _group_rows_to_turns: record_uuid grouping, ordering (D5: text rows only —
# tool_use/tool_result never reach this function, the SQL excludes them)
# --------------------------------------------------------------------------- #
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
# D12: the meta-noise drop is the AND of the is_meta flag and the denylist.
# Both halves are load-bearing — see _META_NOISE_PATTERNS.
# --------------------------------------------------------------------------- #
def test_meta_noise_record_dropped_when_flag_and_pattern_both_hold():
    """The F1 case: an expanded SKILL body is a user-role TEXT record carrying
    isMeta=true — it must not become a turn."""
    rows = [
        _row("u1", "s", "user", "10:00:00", 0, "text",
             text="pj:llm-wiki /progress start some-task"),
        _row("m1", "s", "user", "10:00:01", 0, "text", is_meta=True,
             text="Base directory for this skill: c:\\plugins\\taskflow\\skills\\progress\n\n# /progress\n..."),
    ]
    turns = proj._group_rows_to_turns(rows)
    assert [t.uuid for t in turns] == ["u1"]


def test_meta_flag_without_denylist_match_is_kept():
    """Human steering typed mid-turn also carries isMeta=true (measured), so the
    flag ALONE must never drop a record."""
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
    """A user who PASTES a noise-shaped string is not the harness: without the
    flag the denylist must not fire."""
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
    # A local command's other two records carry NO isMeta flag (measured), so
    # they are NOT this denylist's job — D7's _BOILERPLATE_PATTERNS strips them.
    "<command-name>/model</command-name>",
    "<local-command-stdout>Set model to claude-sonnet-5</local-command-stdout>",
    "",
])
def test_is_meta_noise_leaves_content_bearing_shapes_alone(kept):
    assert proj._is_meta_noise(kept) is False


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
