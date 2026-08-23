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


def test_pj_filter_selects_matching_project_stems(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-keep-1", "llm-wiki")
    _write_state(state_dir, "sid-keep-2", "llm-wiki")
    _write_state(state_dir, "sid-other", "some-other-project")
    _write_state(state_dir, "sid-empty", "")

    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
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
    assert out["sids"] == ["sid-good"]


def test_cwd_ground_truth_resolves_running_session_dir(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    cc_dir = tmp_path / "cc-project"
    cc_dir.mkdir()
    (cc_dir / "sidX.jsonl").write_text("{}\n", encoding="utf-8")
    (cc_dir / "sidY.jsonl").write_text("{}\n", encoding="utf-8")
    (cc_dir / "agent-child.jsonl").write_text("{}\n", encoding="utf-8")
    (cc_dir / "journal.jsonl").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session",
                        lambda: cc_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), pj=None)
    assert out["scope"] == "cwd"
    assert out["sids"] == ["sidX", "sidY"]
    assert out["pattern"] == str(cc_dir)


def test_cwd_falls_back_to_slug_dir_when_running_session_unresolved(
        tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    cc_dir = tmp_path / "slug-dir"
    cc_dir.mkdir()
    (cc_dir / "sidZ.jsonl").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", lambda: None)
    monkeypatch.setattr(drv, "_cc_project_dir_from_cwd", lambda cwd=None: cc_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), pj=None)
    assert out["scope"] == "cwd"
    assert out["sids"] == ["sidZ"]


def test_cwd_unresolvable_is_error(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", lambda: None)
    monkeypatch.setattr(drv, "_cc_project_dir_from_cwd", lambda cwd=None: None)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), pj=None)
    assert "could not resolve the CC project dir" in str(ei.value)


def test_cwd_dir_with_zero_sessions_is_error(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    cc_dir = tmp_path / "empty-cc"
    cc_dir.mkdir()
    (cc_dir / "agent-only.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", lambda: cc_dir)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), pj=None)
    assert "zero sessions" in str(ei.value)


def test_non_wiki_root_is_error(tmp_path):
    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), pj="anything")
    assert "not a wiki root" in str(ei.value)


def test_order_sids_by_started_ts_ascending(monkeypatch):
    import datetime as _dt

    class _FakeCon:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("SELECT"):
                self._is_select = True
            else:
                self._is_select = False
            return self

        def fetchall(self):
            return self._rows

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
    ordered = drv._order_sids_by_started_ts(
        ["sid-late", "sid-early", "sid-mid", "sid-none"])
    assert ordered == ["sid-early", "sid-mid", "sid-late", "sid-none"], (
        "a sid with no start timestamp sorts last but is still returned"
    )


def test_cc_path_sid_arg_over_env_call_arity(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    calls = []

    def _capture(*args):
        calls.append(args)
        return None

    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", _capture)
    cc_dir = tmp_path / "cc-project"
    cc_dir.mkdir()
    (cc_dir / "sidX.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(drv, "_cc_project_dir_from_cwd", lambda cwd=None: cc_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    drv.session_plan(str(tmp_path), pj=None)
    assert calls[-1] == ()

    drv.session_plan(str(tmp_path), pj=None, sid="EXPLICIT_SID")
    assert calls[-1] == ("EXPLICIT_SID",)


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
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-early", "p")
    _write_state(state_dir, "sid-late", "p")
    _write_state(state_dir, "sid-foreign", "p")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)

    sessions_dir = _use_pi_agent_dir(tmp_path, monkeypatch)
    proj_dir = sessions_dir / "--proj--"
    _write_pi_session(proj_dir, "sid-late", "2026-07-02T12-00-00-000Z")
    _write_pi_session(proj_dir, "sid-early", "2026-07-02T09-00-00-000Z")

    out = drv.session_plan(str(tmp_path), pj="p", kind="fe_pi_log")
    assert out["scope"] == "pj"
    assert out["sids"] == ["sid-early", "sid-late"], (
        "a state file may name a sid belonging to another harness; it is excluded "
        "and counted in filtered_out, never silently dropped"
    )
    assert out["filtered_out"] == 1


def test_current_sid_env_is_the_real_cc_env_var_name():
    assert drv._CURRENT_SID_ENV == "CLAUDE_CODE_SESSION_ID"


def test_running_session_dir_reads_the_fixed_env_var(tmp_path, monkeypatch):
    cc_dir = tmp_path / "cc-project"
    cc_dir.mkdir()
    sid = "env-resolved-sid"
    (cc_dir / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(drv, "_cc_projects_roots", lambda: [tmp_path])
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)

    resolved = drv._cc_project_dir_from_running_session()
    assert resolved == cc_dir


def test_running_session_dir_falls_through_to_the_second_root(tmp_path, monkeypatch):
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
    _init_wiki(tmp_path)
    cc_dir = tmp_path / "cc-project"
    cc_dir.mkdir()
    (cc_dir / "sidQ.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", lambda: cc_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path))
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
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    def _boom():
        raise AssertionError("no-args scope=pj must not consult the cwd path")
    monkeypatch.setattr(drv, "_cc_project_dir_from_running_session", _boom)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), scope="pj", sid="no-such-sid")
    assert "--pj" in str(ei.value)


def test_no_args_scope_pj_no_sid_fails_closed(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "some-other-sid", "some-proj")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), scope="prompt")
    assert "--pj" in str(ei.value)


def test_no_args_scope_pj_empty_project_fails_closed(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-e", "")
    _write_state(state_dir, "sid-other", "some-other-project")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)

    with pytest.raises(drv.DriverError) as ei:
        drv.session_plan(str(tmp_path), scope="pj", sid="sid-e")
    assert "--pj" in str(ei.value)


def test_no_args_scope_pj_missing_project_key_fails_closed(tmp_path, monkeypatch):
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
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    _write_state(state_dir, "sid-w", "any-proj")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), workspace=True, scope="cwd")
    assert out["scope"] == "workspace"
    assert out["sids"] == ["sid-w"]


def test_explicit_pj_wins_over_scope_param(tmp_path, monkeypatch):
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
    assert out["sids"] == [sid_early, sid_late]
