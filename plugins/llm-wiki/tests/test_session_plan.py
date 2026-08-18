"""Tests: ingest_driver `session-plan` verb (T6/T9) — read-only Path B resolver.

Covers the T9 locked spec for session-plan:
  - --pj filter: `_projects/_state/*.json` filtered by `project == <name>` ->
    the matching state files' stems are the sids;
  - --pj omitted: the CC project dir is resolved from the RUNNING session's own
    log location (U3 ground truth), then its `*.jsonl` stems are the sids;
  - zero matches is an explicit DriverError (fail-closed, like enumerate);
  - the returned sids are ordered by session-start ts ASCENDING.

The state dir is keyed off the process CWD and the CC log dirs off
`$CLAUDE_CONFIG_DIR` + `~/.claude` (the roots union `cc_paths` resolves); the ts
ordering queries the live cc store via DuckDB. To keep the tests hermetic and
deterministic we monkeypatch the resolver's own module-level seams:
  - `_state_dir` -> a tmp state dir we populate;
  - `_cc_projects_roots` -> tmp CC log roots;
  - `_cc_project_dir_from_running_session` / `_cc_project_dir_from_cwd` ->
    controlled dir Paths;
  - `_order_sids_by_started_ts` -> a fixed ts map (the ordering assertion below
    also verifies the SORT contract directly, without the live store).

`session_plan` requires a wiki-root marker, so each test builds a minimal
`.llmwiki` directory (like the sibling driver tests).
"""
import json
import re

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


# --------------------------------------------------------------------------- #
# OI-1 S3(e) F-13: session-plan's cc-path threads --sid arg-over-env — an
# explicit sid resolves via the ARG (not env), and omitting --sid preserves
# the EXACT pre-existing 0-argument call shape into
# `_cc_project_dir_from_running_session` (S2 report §2 change-point-9,
# formalized here as a pytest call-arity assertion).
# --------------------------------------------------------------------------- #
def test_cc_path_sid_arg_over_env_call_arity(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    calls = []

    def _capture(*args):
        calls.append(args)
        return None  # force the fallback so session_plan doesn't need a real dir

    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", _capture)
    cc_dir = tmp_path / "cc-project"
    cc_dir.mkdir()
    (cc_dir / "sidX.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(drv, "_cc_project_dir_from_cwd", lambda cwd=None: cc_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    # sid OMITTED -> byte-identical 0-argument call (pre-existing shape).
    drv.session_plan(str(tmp_path), pj=None)
    assert calls[-1] == ()

    # sid GIVEN -> arg-over-env, the arg is threaded through as the sole arg.
    drv.session_plan(str(tmp_path), pj=None, sid="EXPLICIT_SID")
    assert calls[-1] == ("EXPLICIT_SID",)


# --------------------------------------------------------------------------- #
# OI-1 S3(d): session-plan pi (--kind=fe_pi_log) path — synthetic session dir
# fixtures (`--<slug>--/<ts>_<sid>.jsonl`, tmp_path + PI_CODING_AGENT_DIR
# override; the exact encoding is pi_log_project._encode_cwd, re-verified
# against session-manager.ts by S1 — session_dir_for_cwd/sids_in_session_dir
# are exercised here for real, not re-derived by hand).
# --------------------------------------------------------------------------- #
def _pi_msg(entry_id, parent_id, role, ts, text):
    return {
        "type": "message", "id": entry_id, "parentId": parent_id,
        "timestamp": ts,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _write_pi_session(session_dir, sid, ts_prefix, cwd="/synthetic/cwd"):
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{ts_prefix}_{sid}.jsonl"
    header = {"type": "session", "version": 1, "id": sid, "cwd": cwd}
    entry = _pi_msg("e1", None, "user", "2026-07-02T09:00:00.000Z", "hi")
    path.write_text(
        "\n".join(json.dumps(l) for l in (header, entry)) + "\n",
        encoding="utf-8")
    return path


def _use_pi_agent_dir(tmp_path, monkeypatch):
    from llmwiki.ingest import pi_log_project
    agent_dir = tmp_path / "agentdir"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    return pi_log_project._session_dir()


def test_pi_cwd_sid_primary_resolves_running_session_dir(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    sessions_dir = _use_pi_agent_dir(tmp_path, monkeypatch)
    proj_dir = sessions_dir / "--proj--"
    sid_target = "10000000-0000-0000-0000-000000000001"
    _write_pi_session(proj_dir, sid_target, "2026-07-02T09-00-00-000Z")

    out = drv.session_plan(str(tmp_path), kind="fe_pi_log", sid=sid_target)
    assert out["scope"] == "cwd"
    assert out["sids"] == [sid_target]
    assert out["pattern"] == str(proj_dir)
    assert out["filtered_out"] == 0


def test_pi_cwd_sid_not_found_falls_back_to_cwd_dir(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    from llmwiki.ingest import pi_log_project
    _use_pi_agent_dir(tmp_path, monkeypatch)
    # sid does not resolve (no matching session file anywhere) -> None -> the
    # cwd fallback dir is used instead (F-6 primary->fallback chain).
    fallback_dir = tmp_path / "fallback-dir"
    sid_fallback = "20000000-0000-0000-0000-000000000002"
    _write_pi_session(fallback_dir, sid_fallback, "2026-07-02T10-00-00-000Z")
    monkeypatch.setattr(pi_log_project, "session_dir_for_cwd",
                        lambda cwd: fallback_dir)

    out = drv.session_plan(str(tmp_path), kind="fe_pi_log", sid="no-such-sid")
    assert out["scope"] == "cwd"
    assert out["sids"] == [sid_fallback]
    assert out["pattern"] == str(fallback_dir)


def test_pi_cwd_zero_match_is_fail_closed_error(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    from llmwiki.ingest import pi_log_project
    _use_pi_agent_dir(tmp_path, monkeypatch)
    empty_dir = tmp_path / "empty-session-dir"
    empty_dir.mkdir()
    monkeypatch.setattr(pi_log_project, "session_dir_for_cwd",
                        lambda cwd: empty_dir)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), kind="fe_pi_log")
    assert "session-plan" in str(ei.value) or "pi session dir" in str(ei.value)


def test_pi_pj_locality_filter_and_filtered_out(tmp_path, monkeypatch):
    """F-2: pj-path enumeration (_sids_for_pj, harness-neutral) is intersected
    with sids that actually have a pi session file; the taskflow state may
    list a foreign-harness sid with no pi session file — it is excluded and
    counted, not silently dropped."""
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-early", "p")
    _write_state(state_dir, "sid-late", "p")
    _write_state(state_dir, "sid-foreign", "p")  # no pi session file for this
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)

    sessions_dir = _use_pi_agent_dir(tmp_path, monkeypatch)
    proj_dir = sessions_dir / "--proj--"
    _write_pi_session(proj_dir, "sid-late", "2026-07-02T12-00-00-000Z")
    _write_pi_session(proj_dir, "sid-early", "2026-07-02T09-00-00-000Z")
    # sid-foreign deliberately has NO session file.

    out = drv.session_plan(str(tmp_path), pj="p", kind="fe_pi_log")
    assert out["scope"] == "pj"
    assert out["sids"] == ["sid-early", "sid-late"]  # ts-ascending, foreign excluded
    assert out["filtered_out"] == 1


# --------------------------------------------------------------------------- #
# D5: `_CURRENT_SID_ENV` must read the env var CC actually sets, not the
# harness prompt-template substitution name it was previously conflated with
# (workspace-session-ingest.md D5; probe 2026-07-10: `CLAUDE_SESSION_ID` UNSET /
# `CLAUDE_CODE_SESSION_ID` SET len=36).
# --------------------------------------------------------------------------- #
def test_current_sid_env_is_the_real_cc_env_var_name():
    assert drv._CURRENT_SID_ENV == "CLAUDE_CODE_SESSION_ID"


def test_running_session_dir_reads_the_fixed_env_var(tmp_path, monkeypatch):
    """`_cc_project_dir_from_running_session` (env-only, no `sid` arg) must read
    `$CLAUDE_CODE_SESSION_ID` — the stale `$CLAUDE_SESSION_ID` name silently
    always returned None (D5's diagnosed root cause)."""
    cc_dir = tmp_path / "cc-project"
    cc_dir.mkdir()
    sid = "env-resolved-sid"
    (cc_dir / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(drv, "_cc_projects_roots", lambda: [tmp_path])
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)

    resolved = drv._cc_project_dir_from_running_session()
    assert resolved == cc_dir


# --------------------------------------------------------------------------- #
# CLAUDE_CONFIG_DIR (A class): the CC log dir lookups scan the roots UNION
# `[$CLAUDE_CONFIG_DIR, ~/.claude]` with the env universe first. Resolution
# semantics live in `cc_paths` (tested in test_cc_paths.py); these cover the two
# driver-side lookups that consume them. Spec:
# `_projects/llm-wiki/project-notes/specs/cc-config-dir-ingest.md` C2.
# --------------------------------------------------------------------------- #
def test_running_session_dir_falls_through_to_the_second_root(tmp_path, monkeypatch):
    """A sid present only in the DEFAULT universe is still found."""
    env_root, default_root = tmp_path / "env", tmp_path / "default"
    env_root.mkdir()
    cc_dir = default_root / "cc-project"
    cc_dir.mkdir(parents=True)
    sid = "only-in-default"
    (cc_dir / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(drv, "_cc_projects_roots", lambda: [env_root, default_root])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)

    assert drv._cc_project_dir_from_running_session() == cc_dir


def test_running_session_dir_prefers_the_env_universe_on_collision(tmp_path, monkeypatch):
    """AC-A2: the same sid in both universes resolves to the env one (first-wins)."""
    env_root, default_root = tmp_path / "env", tmp_path / "default"
    sid = "in-both"
    env_dir = env_root / "cc-project"
    default_dir = default_root / "cc-project"
    for d in (env_dir, default_dir):
        d.mkdir(parents=True)
        (d / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(drv, "_cc_projects_roots", lambda: [env_root, default_root])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)

    assert drv._cc_project_dir_from_running_session() == env_dir


def test_cwd_fallback_scans_every_root_env_first(tmp_path, monkeypatch):
    """The slug-dir fallback checks each root in order and takes the first hit."""
    env_root, default_root = tmp_path / "env", tmp_path / "default"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    slug = re.sub(r"[\\/:]", "-", str(workspace.resolve()))
    env_root.mkdir()
    (default_root / slug).mkdir(parents=True)
    monkeypatch.setattr(drv, "_cc_projects_roots", lambda: [env_root, default_root])

    assert drv._cc_project_dir_from_cwd(workspace) == default_root / slug

    (env_root / slug).mkdir()
    assert drv._cc_project_dir_from_cwd(workspace) == env_root / slug


# --------------------------------------------------------------------------- #
# D2/D3/D4: the no-args scope tree + explicit --workspace (workspace-session-
# ingest.md). `workspace`/`scope` are new session_plan kwargs; kind defaults to
# cc (fe_b_prime) — D3's workspace path is cc-only.
# --------------------------------------------------------------------------- #
def test_explicit_workspace_flag_unions_all_state_sids_no_project_filter(
        tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-proj-a", "project-a")
    _write_state(state_dir, "sid-proj-b", "project-b")
    _write_state(state_dir, "sid-proj-a-2", "project-a")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), workspace=True)
    assert out["scope"] == "workspace"
    assert out["sids"] == ["sid-proj-a", "sid-proj-a-2", "sid-proj-b"]
    assert out["pattern"].endswith("*.json")


def test_explicit_workspace_zero_state_files_is_error(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), workspace=True)
    assert "workspace" in str(ei.value)


def test_no_args_scope_workspace_matches_explicit_workspace(tmp_path, monkeypatch):
    """D2 A-follow: no-args with the caller's resolved scope == "workspace" must
    take the SAME `_sids_workspace` union path as explicit --workspace."""
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-x", "proj-x")
    _write_state(state_dir, "sid-y", "proj-y")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), scope="workspace")
    assert out["scope"] == "workspace"
    assert out["sids"] == ["sid-x", "sid-y"]


def test_no_args_scope_cwd_default_unchanged_when_scope_omitted(tmp_path, monkeypatch):
    """D4: no-args with `scope` omitted (None) must still hit the pre-existing
    cwd-slug resolution byte-identically (back-compat for callers that do not
    thread --scope yet)."""
    _init_wiki(tmp_path)
    cc_dir = tmp_path / "cc-project"
    cc_dir.mkdir()
    (cc_dir / "sidQ.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", lambda: cc_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path))     # scope omitted entirely
    assert out["scope"] == "cwd"
    assert out["sids"] == ["sidQ"]


def test_no_args_scope_cwd_explicit_same_as_omitted(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    cc_dir = tmp_path / "cc-project"
    cc_dir.mkdir()
    (cc_dir / "sidR.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", lambda: cc_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), scope="cwd")
    assert out["scope"] == "cwd"
    assert out["sids"] == ["sidR"]


def test_no_args_scope_pj_resolves_active_project_via_sid(tmp_path, monkeypatch):
    """D2: no-args, scope pj/prompt -> the taskflow-APPLIED project for THIS
    session (`_state/<sid>.json`.project), NOT a name derived from the wiki
    path, then the same `_sids_for_pj` enumeration."""
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "running-sid", "active-proj")
    _write_state(state_dir, "sid-a1", "active-proj")
    _write_state(state_dir, "sid-a2", "active-proj")
    _write_state(state_dir, "sid-other", "other-proj")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), scope="pj", sid="running-sid")
    assert out["scope"] == "pj"
    assert out["sids"] == ["running-sid", "sid-a1", "sid-a2"]


def test_no_args_scope_prompt_same_resolution_as_pj_provisional(tmp_path, monkeypatch):
    """D2 Follow-ups: `prompt` scope's no-args set is PROVISIONALLY folded into
    the same active-project resolution as `pj`."""
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "running-sid", "active-proj")
    _write_state(state_dir, "sid-a1", "active-proj")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), scope="prompt", sid="running-sid")
    assert out["scope"] == "pj"
    assert out["sids"] == ["running-sid", "sid-a1"]


def test_no_args_scope_pj_fails_closed_without_active_project(tmp_path, monkeypatch):
    """D2: no active taskflow project for this session -> fail closed with
    guidance to pass --pj, NOT a silent fall-back to the narrow cwd-slug set
    (the diagnosed symptom)."""
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    # Even if a cwd dir WOULD resolve, the pj/prompt branch must not fall
    # through to it — assert it's never even consulted.
    def _boom():
        raise AssertionError("no-args scope=pj must not consult the cwd path")
    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", _boom)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), scope="pj", sid="no-such-sid")
    assert "--pj" in str(ei.value)


def test_no_args_scope_pj_no_sid_fails_closed(tmp_path, monkeypatch):
    """D2: no `sid` at all (e.g. --sid never threaded) also fails closed on the
    pj/prompt no-args branch — `_active_project_for_sid(None)` is None."""
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "some-other-sid", "some-proj")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), scope="prompt")
    assert "--pj" in str(ei.value)


def test_no_args_scope_pj_empty_project_fails_closed(tmp_path, monkeypatch):
    """Contrast pair (review-dev AC-1): `<sid>.json` exists with
    `{"project": ""}` (the shape taskflow's session_init.py writes for a
    pj-unassigned session) -> `_active_project_for_sid` already treats this as
    no active project, same as the resolver's post-fix sid-given posture. A
    second state file names a real project, so the assertion proves the driver
    did not widen to another session's set."""
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-e", "")
    _write_state(state_dir, "sid-other", "some-other-project")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), scope="pj", sid="sid-e")
    assert "--pj" in str(ei.value)


def test_no_args_scope_pj_missing_project_key_fails_closed(tmp_path, monkeypatch):
    """Contrast pair (review-dev AC-1): `<sid>.json` exists but has no
    `project` key at all (`_write_state`'s helper always emits the key, so this
    writes the file directly). A second state file names a real project, so the
    assertion proves the driver did not widen to another session's set."""
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "sid-k.json").write_text(json.dumps({"origin": "cc"}), encoding="utf-8")
    _write_state(state_dir, "sid-other", "some-other-project")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), scope="pj", sid="sid-k")
    assert "--pj" in str(ei.value)


def test_explicit_workspace_wins_over_scope_param(tmp_path, monkeypatch):
    """`workspace=True` is an explicit override and wins even if `scope` (the
    caller's resolved WIKI_SCOPE) says something else, e.g. "cwd"."""
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-w", "any-proj")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), workspace=True, scope="cwd")
    assert out["scope"] == "workspace"
    assert out["sids"] == ["sid-w"]


def test_explicit_pj_wins_over_scope_param(tmp_path, monkeypatch):
    """An explicit `--pj` overrides `scope` too (unchanged precedence)."""
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-p", "target-proj")
    _write_state(state_dir, "sid-q", "other-proj")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), pj="target-proj", scope="workspace")
    assert out["scope"] == "pj"
    assert out["sids"] == ["sid-p"]


def test_pi_ts_ascending_order_cwd_path(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    sessions_dir = _use_pi_agent_dir(tmp_path, monkeypatch)
    proj_dir = sessions_dir / "--proj--"
    sid_late = "30000000-0000-0000-0000-000000000003"
    sid_early = "40000000-0000-0000-0000-000000000004"
    _write_pi_session(proj_dir, sid_late, "2026-07-02T15-00-00-000Z")
    _write_pi_session(proj_dir, sid_early, "2026-07-02T09-00-00-000Z")

    out = drv.session_plan(str(tmp_path), kind="fe_pi_log", sid=sid_early)
    assert out["scope"] == "cwd"
    assert out["sids"] == [sid_early, sid_late]  # ts-ascending, not insertion order
