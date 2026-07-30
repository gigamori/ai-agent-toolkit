"""Tests: cc_log_project — the fork-aware cc-log projector (T2/T9).

Covers the T9 locked spec for the projector:
  - all-branch adoption (every projected turn is kept, no frontier selection);
  - LENGTH-INDEPENDENT exact dedup within a sid (short turns collapse too — the
    >=200-char min-length guard is WITHDRAWN, F4);
  - a cross-record shared prefix collapses (same (role,text) -> one turn);
  - thinking blocks are excluded (S8-c);
  - a per-turn provenance pointer (uuid/ts) is emitted;
  - injected boilerplate is stripped BEFORE the hash so turns differing only by
    boilerplate collapse (F4/U2);
  - the ledger diff drops already-owned turns (F1-b/T4) and the drop is counted
    on ProjectionResult.ledger_skipped (F6);
  - the DuckDB `md5` and Python `hashlib.md5` hashes byte-MATCH for the same
    input (0x1F delimiter / UTF-8 / NFC — F5).

The DuckDB projection itself (`_fetch_turns`) reads the live cc store, which is
not hermetic; the projector's PURE logic (grouping -> boilerplate strip -> dedup
-> ledger diff -> markdown) is exercised by monkeypatching `_fetch_turns` to
return synthetic `_Turn` rows (the sanctioned T2 approach). The real `ledger`
module is injected so the hash / seen-set are the true single source of truth.

The F5 test opens an in-memory DuckDB and applies the vendored `cc_views.sql`
schema function (`md5`/`nfc_normalize`), replicating the DuckDB-side hash the
projection SQL relies on, and asserts byte-parity with `ledger.compute_hash`.
"""
import hashlib
import unicodedata

import pytest

from llmwiki.ingest import cc_log_project as proj
from llmwiki.ingest import ledger


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _turn(role, uuid, ts, texts, tool_uses=None, order=0):
    """Build a synthetic projector `_Turn` (post-fetch, pre-dedup)."""
    return proj._Turn(
        role=role, uuid=uuid, ts=ts,
        text_parts=list(texts),
        tool_uses=list(tool_uses or []),
        order=order,
    )


def _project(monkeypatch, turns, wiki_root):
    """Project a fixed list of synthetic turns (monkeypatch the DuckDB fetch)."""
    monkeypatch.setattr(proj, "_fetch_turns", lambda sid: list(turns))
    return proj.project_owned(wiki_root, "sid-under-test", ledger=ledger)


# --------------------------------------------------------------------------- #
# all-branch adoption: every distinct turn is kept (no frontier selection)
# --------------------------------------------------------------------------- #
def test_all_branches_adopted(tmp_path, monkeypatch):
    turns = [
        _turn("user", "u1", "2026-07-02 10:00:00", ["question A"], order=0),
        _turn("assistant", "a1", "2026-07-02 10:00:01", ["branch one"], order=1),
        _turn("assistant", "a2", "2026-07-02 10:00:02", ["branch two"], order=2),
        _turn("assistant", "a3", "2026-07-02 10:00:03", ["branch three"], order=3),
    ]
    res = _project(monkeypatch, turns, tmp_path)
    # All four distinct turns survive (nothing is a duplicate) -> 4 novel entries.
    assert len(res.novel_entries) == 4
    for body in ("question A", "branch one", "branch two", "branch three"):
        assert body in res.markdown


# --------------------------------------------------------------------------- #
# length-independent exact dedup: identical (role,text) turns collapse to ONE,
# INCLUDING short turns (the >=200-char guard is withdrawn, F4)
# --------------------------------------------------------------------------- #
def test_exact_dedup_collapses_identical_turns(tmp_path, monkeypatch):
    long_text = "This is a substantial paragraph. " * 12  # > 200 chars
    turns = [
        _turn("assistant", "a1", "t1", [long_text], order=0),
        _turn("assistant", "a2", "t2", [long_text], order=1),   # exact dup -> collapse
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert len(res.novel_entries) == 1
    assert res.markdown.count("## Turn") == 1


def test_short_turn_exact_dedup_collapses(tmp_path, monkeypatch):
    """A SHORT affirmation is collapsed too (length-independent, F4). The first
    copy is retained so the decision signal survives."""
    turns = [
        _turn("user", "u1", "t1", ["ok"], order=0),
        _turn("user", "u2", "t2", ["ok"], order=1),       # short exact dup
        _turn("assistant", "a1", "t3", ["Sure"], order=2),
        _turn("assistant", "a2", "t4", ["Sure"], order=3),  # short exact dup
    ]
    res = _project(monkeypatch, turns, tmp_path)
    # 2 unique short turns survive (one "ok", one "Sure"); the 2 dups collapse.
    assert len(res.novel_entries) == 2
    assert res.markdown.count("## Turn") == 2
    # first copy retained -> the signal is present exactly once each.
    assert res.markdown.count("**Human**:") == 1
    assert res.markdown.count("**Assistant**:") == 1


def test_shared_prefix_across_records_collapses(tmp_path, monkeypatch):
    """A shared prefix appearing in two records (the cross-record collapse case)
    dedups to one turn regardless of the surrounding record."""
    shared = "shared prefix content that recurs verbatim"
    turns = [
        _turn("assistant", "recA", "t1", [shared], order=0),
        _turn("assistant", "recB", "t2", [shared], order=1),  # same text, other record
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert len(res.novel_entries) == 1


# --------------------------------------------------------------------------- #
# boilerplate strip BEFORE hash: turns differing ONLY by injected boilerplate
# collapse (F4/U2)
# --------------------------------------------------------------------------- #
def test_boilerplate_stripped_then_dedup(tmp_path, monkeypatch):
    raw = "login bug in the auth flow"
    with_boiler = f"[Progress Session] session_id=abc sid8=abc12345\n{raw}"
    turns = [
        _turn("user", "u1", "t1", [raw], order=0),
        _turn("user", "u2", "t2", [with_boiler], order=1),  # only differs by boilerplate
    ]
    res = _project(monkeypatch, turns, tmp_path)
    # After the [Progress Session] strip both hash the same -> collapse to one.
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
    # The real role-mode "Two response axes:" header block (verified against
    # plugins/role-mode/prompts/modes/_meta.md).
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
    # The role-less variant (verified against
    # plugins/role-mode/prompts/modes/_meta.md, split 2026-07-30): no Role
    # axis text at all, only the Mode line + narrowed precedence.
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


def test_boilerplate_mode_header_block_does_not_eat_real_content(tmp_path, monkeypatch):
    # A user turn that merely starts with the word "Mode" must not be
    # swallowed by the role-less header's literal match.
    text = "Mode of transport matters here.\nactual user instruction here"
    turns = [_turn("user", "u1", "t1", [text], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "Mode of transport matters here." in res.markdown
    assert "actual user instruction here" in res.markdown


# --------------------------------------------------------------------------- #
# thinking exclusion (S8-c): the projection SQL never selects thinking blocks
# --------------------------------------------------------------------------- #
def test_thinking_excluded_from_projection_sql():
    """S8-c is enforced at the SQL level: block_type IN
    ('text','tool_use','tool_result') — 'thinking' is never fetched."""
    sql = proj._PROJECT_SQL
    assert "block_type IN ('text', 'tool_use', 'tool_result')" in sql
    assert "thinking" not in sql


def test_thinking_leak_absent_in_markdown(tmp_path, monkeypatch):
    # _fetch_turns already excludes thinking at the SQL layer; a synthetic turn
    # carries only text_parts, so a thinking string is never introduced. Assert
    # the rendered markdown carries no thinking marker for a normal turn.
    turns = [_turn("assistant", "a1", "t1", ["a normal answer"], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "thinking" not in res.markdown.lower()


# --------------------------------------------------------------------------- #
# provenance pointer (uuid/ts) is emitted per surviving turn
# --------------------------------------------------------------------------- #
def test_provenance_pointer_present(tmp_path, monkeypatch):
    turns = [_turn("assistant", "uuid-XYZ", "2026-07-02 12:34:56",
                   ["answer body"], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    assert "<!-- provenance: uuid=uuid-XYZ ts=2026-07-02 12:34:56 -->" in res.markdown
    # novel_entries record the sid + per-turn uuid/ts (first-ingested ownership).
    assert res.novel_entries[0]["first_sid"] == "sid-under-test"
    assert res.novel_entries[0]["first_uuid"] == "uuid-XYZ"
    assert res.novel_entries[0]["first_ts"] == "2026-07-02 12:34:56"


def test_novel_entry_hash_is_ledger_compute_hash(tmp_path, monkeypatch):
    """The novel-entry hash is exactly ledger.compute_hash(role, projected_text)
    — the single source of truth, NOT a reimplementation (F5 basis)."""
    turns = [_turn("assistant", "a1", "t1", ["deterministic body"], order=0)]
    res = _project(monkeypatch, turns, tmp_path)
    expected = ledger.compute_hash("assistant", "deterministic body")
    assert res.novel_entries[0]["hash"] == expected


# --------------------------------------------------------------------------- #
# ledger diff (F1-b/T4) + ledger_skipped counter (F6)
# --------------------------------------------------------------------------- #
def test_ledger_diff_drops_seen_and_counts_skip(tmp_path, monkeypatch):
    owned_text = "already owned by a prior ingest"
    novel_text = "brand new turn"
    # Pre-seed the ledger so `owned_text` is already seen.
    owned_hash = ledger.compute_hash("assistant", owned_text)
    ledger.append_entries(
        tmp_path,
        [ledger.LedgerEntry(owned_hash, "prior-sid", "prior-uuid", "t0")],
    )
    turns = [
        _turn("assistant", "a1", "t1", [owned_text], order=0),   # dropped by ledger
        _turn("assistant", "a2", "t2", [novel_text], order=1),   # novel
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert len(res.novel_entries) == 1
    assert res.novel_entries[0]["hash"] == ledger.compute_hash("assistant", novel_text)
    # F6: the ledger-diff drop is counted (this drop, not the within-sid dedup).
    assert res.ledger_skipped == 1
    assert owned_text not in res.markdown
    assert novel_text in res.markdown


def test_ledger_skipped_zero_on_fresh_wiki(tmp_path, monkeypatch):
    """First ingest into an empty wiki: nothing owned yet -> ledger_skipped == 0."""
    turns = [
        _turn("user", "u1", "t1", ["fresh one"], order=0),
        _turn("assistant", "a1", "t2", ["fresh two"], order=1),
    ]
    res = _project(monkeypatch, turns, tmp_path)
    assert res.ledger_skipped == 0
    assert len(res.novel_entries) == 2


def test_ledger_skipped_counts_only_ledger_not_local_dedup(tmp_path, monkeypatch):
    """F6 precision: ledger_skipped counts ONLY the ledger-diff drop, NOT the
    within-sid exact-dedup collapse (a different signal)."""
    dup_text = "repeated within this same sid"
    turns = [
        _turn("assistant", "a1", "t1", [dup_text], order=0),
        _turn("assistant", "a2", "t2", [dup_text], order=1),  # within-sid dup (NOT ledger)
    ]
    res = _project(monkeypatch, turns, tmp_path)
    # The within-sid collapse must NOT inflate ledger_skipped.
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
    # Header only, no turn body.
    assert "## Turn" not in res.markdown


# --------------------------------------------------------------------------- #
# F5 — DuckDB md5 == Python hashlib.md5 for the SAME input (0x1F / UTF-8 / NFC)
# --------------------------------------------------------------------------- #
def test_hash_determinism_duckdb_matches_python():
    """The turn-content hash must be byte-identical across the DuckDB and Python
    sides (F5). Python side = ledger.compute_hash. DuckDB side =
    md5(nfc_normalize(role) || chr(31) || nfc_normalize(text)) — nfc_normalize is
    REQUIRED for byte-parity when the input is not already NFC (a decomposed
    combining character otherwise diverges)."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    cases = [
        ("user", "hello world"),
        ("assistant", "café"),                      # precomposed (NFC)
        ("assistant", "café"),                 # decomposed -> needs NFC
        ("user", "漢字 mixed script"),        # CJK
        ("assistant", ""),                            # empty text
        ("user", "line1\nline2\ttab"),                # embedded control chars
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
    """For input that is ALREADY NFC-stable (the common case), the raw DuckDB
    expression md5(role || chr(31) || text) also byte-matches Python — documents
    that the vendored views' plain md5 is correct for NFC-stable text, while
    nfc_normalize is the universally-correct form (see the test above)."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    for role, text in [("user", "hello"), ("assistant", "plain ascii text"),
                       ("user", "café")]:  # precomposed é is NFC-stable
        assert unicodedata.normalize("NFC", text) == text  # precondition: NFC-stable
        py = ledger.compute_hash(role, text)
        ddb_raw = con.execute("SELECT md5(? || chr(31) || ?)", [role, text]).fetchone()[0]
        assert ddb_raw == py


def test_compute_hash_uses_0x1f_delimiter_utf8():
    """Guard the exact Python hash construction (0x1F delimiter, UTF-8, NFC)
    against drift in ledger.compute_hash."""
    role, text = "assistant", "guard me"
    expected = hashlib.md5(
        unicodedata.normalize("NFC", role).encode("utf-8")
        + b"\x1f"
        + unicodedata.normalize("NFC", text).encode("utf-8")
    ).hexdigest()
    assert ledger.compute_hash(role, text) == expected
