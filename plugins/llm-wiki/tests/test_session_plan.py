"""Tests: ingest_driver `session-plan` verb (T6/T9) — read-only Path B resolver.

Covers the T9 locked spec for session-plan:
  - --pj filter: `_projects/_state/*.json` filtered by `project == <name>` ->
    the matching state files' stems are the sids;
  - --pj omitted: the CC project dir is resolved from the RUNNING session's own
    log location (U3 ground truth), then its `*.jsonl` stems are the sids;
  - zero matches is an explicit DriverError (fail-closed, like enumerate);
  - the returned sids are ordered by session-start ts ASCENDING.

The state dir is keyed off the process CWD and the CC log dir off $HOME/env; the
ts ordering queries the live cc store via DuckDB. To keep the tests hermetic and
deterministic we monkeypatch the resolver's own module-level seams:
  - `_state_dir` -> a tmp state dir we populate;
  - `_cc_project_dir_from_running_session` / `_cc_project_dir_from_cwd` ->
    controlled dir Paths;
  - `_order_sids_by_started_ts` -> a fixed ts map (the ordering assertion below
    also verifies the SORT contract directly, without the live store).

`session_plan` requires a wiki-root marker, so each test builds a minimal
`.llmwiki` directory (like the sibling driver tests).
"""
import json

import pytest

from llmwiki.ingest import ingest_driver as drv


def _init_wiki(tmp_path):
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text("---\nconfig: {}\n---\n# SCHEMA\n",
                                        encoding="utf-8")


def _write_state(state_dir, sid, project):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{sid}.json").write_text(
        json.dumps({"project": project, "origin": "cc"}), encoding="utf-8")


# --------------------------------------------------------------------------- #
# --pj filter: only state files whose project == <name> contribute their stems
# --------------------------------------------------------------------------- #
def test_pj_filter_selects_matching_project_stems(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-keep-1", "llm-wiki")
    _write_state(state_dir, "sid-keep-2", "llm-wiki")
    _write_state(state_dir, "sid-other", "some-other-project")
    _write_state(state_dir, "sid-empty", "")

    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    # Keep ts-ordering out of the live store: order by sid string, deterministic.
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), pj="llm-wiki")
    assert out["scope"] == "pj"
    assert out["sids"] == ["sid-keep-1", "sid-keep-2"]
    assert out["pattern"].endswith("*.json")


def test_pj_zero_match_is_error(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-a", "project-a")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), pj="__no_such_project__")
    assert "zero sessions" in str(ei.value)


def test_pj_skips_malformed_state_file(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_state(state_dir, "sid-good", "p")
    (state_dir / "broken.json").write_text("{ not valid json", encoding="utf-8")

    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), pj="p")
    # The malformed file is skipped (never fatal); the good sid survives.
    assert out["sids"] == ["sid-good"]


# --------------------------------------------------------------------------- #
# --pj omitted: CC project dir resolved from the RUNNING session (ground truth)
# --------------------------------------------------------------------------- #
def test_cwd_ground_truth_resolves_running_session_dir(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    cc_dir = tmp_path / "cc-project"
    cc_dir.mkdir()
    # Two real sessions + an agent child + journal (both excluded).
    (cc_dir / "sidX.jsonl").write_text("{}\n", encoding="utf-8")
    (cc_dir / "sidY.jsonl").write_text("{}\n", encoding="utf-8")
    (cc_dir / "agent-child.jsonl").write_text("{}\n", encoding="utf-8")
    (cc_dir / "journal.jsonl").write_text("{}\n", encoding="utf-8")

    # PRIMARY ground-truth path resolves the running-session dir.
    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session",
                        lambda: cc_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), pj=None)
    assert out["scope"] == "cwd"
    # agent-* and journal are excluded; only real sessions planned.
    assert out["sids"] == ["sidX", "sidY"]
    assert out["pattern"] == str(cc_dir)


def test_cwd_falls_back_to_slug_dir_when_running_session_unresolved(
        tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    cc_dir = tmp_path / "slug-dir"
    cc_dir.mkdir()
    (cc_dir / "sidZ.jsonl").write_text("{}\n", encoding="utf-8")

    # PRIMARY returns None -> SECONDARY (cwd reverse-slug) fallback fires.
    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", lambda: None)
    monkeypatch.setattr(drv, "_cc_project_dir_from_cwd", lambda cwd=None: cc_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), pj=None)
    assert out["scope"] == "cwd"
    assert out["sids"] == ["sidZ"]


def test_cwd_unresolvable_is_error(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    # Neither the running session nor the cwd-slug dir resolves -> fail-closed.
    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", lambda: None)
    monkeypatch.setattr(drv, "_cc_project_dir_from_cwd", lambda cwd=None: None)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), pj=None)
    assert "could not resolve the CC project dir" in str(ei.value)


def test_cwd_dir_with_zero_sessions_is_error(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    cc_dir = tmp_path / "empty-cc"
    cc_dir.mkdir()
    (cc_dir / "agent-only.jsonl").write_text("{}\n", encoding="utf-8")  # no real session
    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", lambda: cc_dir)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), pj=None)
    assert "zero sessions" in str(ei.value)


# --------------------------------------------------------------------------- #
# non-wiki-root is a clean DriverError (surface parity with begin)
# --------------------------------------------------------------------------- #
def test_non_wiki_root_is_error(tmp_path):
    # No .llmwiki marker written.
    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), pj="anything")
    assert "not a wiki root" in str(ei.value)


# --------------------------------------------------------------------------- #
# ts ordering: sids are returned ASCENDING by session-start ts (F2-B)
# --------------------------------------------------------------------------- #
def test_order_sids_by_started_ts_ascending(monkeypatch):
    """`_order_sids_by_started_ts` sorts by each sid's `cc_session.started`
    ascending; None-started sids sort LAST but are still returned; ties fall
    back to the sid string. Tested WITHOUT the live store by stubbing the DuckDB
    query result the function consumes."""
    import datetime as _dt

    class _FakeCon:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, sql, params=None):
            # First call installs the views (returns self); second call is the
            # SELECT whose fetchall yields (sid, started) rows.
            if sql.strip().upper().startswith("SELECT"):
                self._is_select = True
            else:
                self._is_select = False
            return self

        def fetchall(self):
            return self._rows

    # started ts: sid-late is newest, sid-early oldest, sid-none has no row.
    rows = [
        ("sid-late", _dt.datetime(2026, 7, 2, 15, 0, 0)),
        ("sid-early", _dt.datetime(2026, 6, 25, 9, 0, 0)),
        ("sid-mid", _dt.datetime(2026, 6, 30, 12, 0, 0)),
    ]

    class _FakeDuckdb:
        @staticmethod
        def connect():
            return _FakeCon(rows)

    monkeypatch.setitem(
        __import__("sys").modules, "duckdb", _FakeDuckdb)
    # _VIEWS_SQL.read_text() is called on the fake con path; the fake con ignores
    # the SQL text, so a dummy read is fine — but the real Path.read_text works.
    ordered = drv._order_sids_by_started_ts(
        ["sid-late", "sid-early", "sid-mid", "sid-none"])
    # ascending by started; sid-none (no row) sorts LAST but is not dropped.
    assert ordered == ["sid-early", "sid-mid", "sid-late", "sid-none"]
