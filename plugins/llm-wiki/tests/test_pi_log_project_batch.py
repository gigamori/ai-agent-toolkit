"""Tests: pi_log_project batch split + composition (OI-1 S1, design A/A2).

Mirrors test_cc_log_project_batch.py's shape for the pi mirror of the R1
scan-collapse split (plan 12-plan-oi1.md rev4, S1):

  - `extract_turns_batch` walks the session dir ONCE for ALL requested sids
    (single filesystem walk, not one rglob per sid), assigns the F5 hash at
    extraction time, and maps a sid with no matching session file to an empty
    list (does NOT raise — mirrors cc's not-found-is-empty-list semantics);
  - `project_from_turns` consumes pre-extracted turn dicts (does not
    recompute the hash — a tampered hash passes through unchanged), applies
    within-sid exact dedup + ledger diff, and raises `ProjectionError` when a
    non-empty-text turn is missing its `hash` key (fail-closed, pi-specific:
    cc KeyErrors on the same condition instead);
  - `project_owned` (Path A) is the composition `extract_turns_batch([sid])`
    -> `project_from_turns`, verified equivalent to the direct call on a
    synthetic fixture (S1 non-regression criterion, formalized here without
    depending on real ~/.pi/agent/sessions data).

Session fixtures are synthetic and deterministic: `PI_CODING_AGENT_DIR` is
overridden (via monkeypatch.setenv) to a tmp_path-rooted "agent dir" so
`pi_log_project._session_dir()` resolves under the fixture, never the real
user session store (per S1's confirmed override mechanism,
`pi_log_project.py:137-143`). Most fixtures use the pi JSONL ARRAY content
-block shape (`[{"type": "text", "text": ...}]`, via the `_msg` helper)
since that was the shape observed in real P6 session data. The plain-
VARCHAR content shape (`_msg_str` helper) is ALSO a legal pi message shape
-- ``UserMessage.content`` is typed ``string | (TextContent | ImageContent)
[]`` (pi-mono packages/ai/src/types.ts:186) -- and previously raised a
DuckDB ConversionException in `pi_views.sql`'s CASE branch, because DuckDB
1.5.4 eagerly evaluates the ARRAY-cast subquery regardless of which CASE
branch is taken. This was fixed (item 4, 2026-07-03) by changing the ARRAY
branch's `cast(json_extract(j, '$.message.content') AS JSON[])` to
`try_cast(...)` in `pi_views.sql`, so the subquery now returns NULL instead
of raising when the content is VARCHAR-shaped; the existing VARCHAR CASE
branch (`json_extract_string`) already produced the correct result and was
unaffected. `test_extract_turns_batch_varchar_content_only` and
`test_project_owned_mixed_varchar_and_array_content` below exercise the
VARCHAR-only and mixed VARCHAR/ARRAY shapes directly.
"""
import json

import pytest

from llmwiki.ingest import pi_log_project as pilp
from llmwiki.ingest import ledger


# --------------------------------------------------------------------------- #
# fixture helpers
# --------------------------------------------------------------------------- #
def _msg(entry_id, parent_id, role, ts, text):
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": ts,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _msg_str(entry_id, parent_id, role, ts, text):
    """pi JSONL message entry with content as a PLAIN STRING (VARCHAR shape).

    ``UserMessage.content: string | (TextContent | ImageContent)[]``
    (pi-mono packages/ai/src/types.ts:186) -- plain string is a legal, real
    shape for user messages, not merely a defensive/hypothetical case.
    """
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": ts,
        "message": {"role": role, "content": text},
    }


def _write_session(session_dir, sid, ts_prefix, entries, cwd="/synthetic/cwd"):
    """Write one synthetic pi session file `<ts_prefix>_<sid>.jsonl`."""
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{ts_prefix}_{sid}.jsonl"
    header = {"type": "session", "version": 1, "id": sid, "cwd": cwd}
    lines = [header] + entries
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _use_agent_dir(tmp_path, monkeypatch):
    """Override PI_CODING_AGENT_DIR so _session_dir() resolves under tmp_path."""
    agent_dir = tmp_path / "agentdir"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    return pilp._session_dir()


# --------------------------------------------------------------------------- #
# extract_turns_batch: single walk, multi-sid split, hash assignment
# --------------------------------------------------------------------------- #
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

    # exactly ONE rglob call covers BOTH sids (R1 batch walk, not per-sid).
    assert len(calls) == 1
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


# --------------------------------------------------------------------------- #
# project_from_turns: dedup, ledger-diff, hash-not-recomputed, missing-hash
# --------------------------------------------------------------------------- #
def _turn(entry_id, parent_id, role, ts, text, *, hash_=None):
    d = {"id": entry_id, "parentId": parent_id, "role": role, "ts": ts, "text": text}
    if hash_ is not None:
        d["hash"] = hash_
    return d


def test_project_from_turns_within_sid_dedup(tmp_path):
    t1 = pilp._assign_hash(
        _turn("u1", None, "user", "t1", "question A"), ledger=ledger)
    t2 = pilp._assign_hash(
        _turn("u2", "u1", "user", "t2", "question A"), ledger=ledger)  # dup text
    t3 = pilp._assign_hash(
        _turn("a1", "u2", "assistant", "t3", "answer A"), ledger=ledger)

    res = pilp.project_from_turns(tmp_path, "sid-x", [t1, t2, t3], ledger=ledger)
    hashes = [e["hash"] for e in res.novel_entries]
    assert len(hashes) == len(set(hashes))
    assert len(res.novel_entries) == 2  # "question A" collapses once


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
    """A tampered hash passes through unchanged (single source of truth)."""
    d = pilp._assign_hash(
        _turn("u1", None, "user", "t1", "hello"), ledger=ledger)
    real_hash = d["hash"]
    d["hash"] = "TAMPERED_" + real_hash

    res = pilp.project_from_turns(tmp_path, "sid", [d], ledger=ledger)
    assert res.novel_entries[0]["hash"] == "TAMPERED_" + real_hash
    assert res.novel_entries[0]["hash"] != real_hash


def test_project_from_turns_missing_hash_raises_projection_error(tmp_path):
    d = _turn("u1", None, "user", "t1", "has no hash key")  # no hash_=...
    with pytest.raises(pilp.ProjectionError, match="hash"):
        pilp.project_from_turns(tmp_path, "sid", [d], ledger=ledger)


def test_project_from_turns_empty_text_turn_skips_before_hash_check(tmp_path):
    """A turn with empty text is skipped before the hash check (no raise)."""
    d = _turn("u1", None, "user", "t1", "")  # no hash key, empty text
    res = pilp.project_from_turns(tmp_path, "sid", [d], ledger=ledger)
    assert res.novel_entries == []


# --------------------------------------------------------------------------- #
# project_owned == extract_turns_batch([sid]) -> project_from_turns (S1 parity)
# --------------------------------------------------------------------------- #
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
    assert len(owned.novel_entries) == 2  # "question A" dedup collapses e1/e3


# --------------------------------------------------------------------------- #
# VARCHAR-content message shape (item 4: pi_views.sql try_cast fix)
# --------------------------------------------------------------------------- #
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
    """VARCHAR- and ARRAY-shaped messages in the SAME session both project,
    in chronological order, and dedup collapses across the two shapes (the
    F5 hash is computed from role+text, not from the raw JSONL shape)."""
    pytest.importorskip("duckdb")
    sessions_dir = _use_agent_dir(tmp_path, monkeypatch)
    sid = "11111111-1111-0000-0000-000000000007"
    _write_session(
        sessions_dir / "--projG--", sid, "2026-07-02T09-00-00-000Z",
        [
            _msg("g1", None, "user", "2026-07-02T09:00:00.000Z",
                 "question A"),  # ARRAY shape
            _msg_str("g2", "g1", "assistant", "2026-07-02T09:00:01.000Z",
                      "answer A"),  # VARCHAR shape
            _msg_str("g3", "g2", "user", "2026-07-02T09:00:02.000Z",
                      "question A"),  # VARCHAR shape, dup text of g1
        ])

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    res = pilp.project_owned(wiki_root, sid, ledger=ledger)

    # g3 ("question A", VARCHAR) dedups against g1 ("question A", ARRAY):
    # only 2 novel turns survive (g1, g2), not 3.
    assert len(res.novel_entries) == 2
    assert "question A" in res.markdown
    assert "answer A" in res.markdown
    # chronological order preserved: g1's turn precedes g2's turn.
    assert res.markdown.index("question A") < res.markdown.index("answer A")
