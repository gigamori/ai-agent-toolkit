import json

from llmwiki.core import wiki_root_resolver as wrr


def _make_wiki(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n", encoding="utf-8")
    return path


def _write_state(cwd, project, name="0000.json"):
    state_dir = cwd / "_projects" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    f = state_dir / name
    f.write_text(json.dumps({"project": project}), encoding="utf-8")
    return f


def test_prompt_root_wins(tmp_path):
    res = wrr.resolve(prompt_root=str(tmp_path / "anywhere"), cwd=tmp_path)
    assert res is not None
    assert res.scope == "prompt"
    assert res.root == (tmp_path / "anywhere")


def test_prompt_root_not_existence_gated(tmp_path):
    res = wrr.resolve(prompt_root=str(tmp_path / "nope"), cwd=tmp_path)
    assert res.scope == "prompt"


def test_prompt_root_is_absolutized(tmp_path):
    res = wrr.resolve(prompt_root=str(tmp_path / "anywhere"), cwd=tmp_path)
    assert res.root.is_absolute()
    assert res.root == (tmp_path / "anywhere").resolve()


def test_relative_prompt_root_resolves_against_cwd_stably(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = wrr.resolve(prompt_root="relwiki", cwd=tmp_path)
    assert res.scope == "prompt"
    assert res.root.is_absolute()
    assert res.root == (tmp_path / "relwiki").resolve()


def test_pj_resolves_from_state_and_taskflow_roots(tmp_path, monkeypatch):
    proot = tmp_path / "roots"
    wiki = _make_wiki(proot / "llm-wiki" / "wiki")
    _write_state(tmp_path, "llm-wiki")
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(cwd=tmp_path)
    assert res is not None
    assert res.scope == "pj"
    assert res.root == wiki


def test_pj_resolves_via_projects_fallback_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    wiki = _make_wiki(tmp_path / "_projects" / "llm-wiki" / "wiki")
    _write_state(tmp_path, "llm-wiki")
    res = wrr.resolve(cwd=tmp_path)
    assert res.scope == "pj"
    assert res.root == wiki


def test_pj_picks_first_matching_root_in_order(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    wiki = _make_wiki(second / "llm-wiki" / "wiki")
    (first / "llm-wiki" / "wiki").mkdir(parents=True, exist_ok=True)
    _write_state(tmp_path, "llm-wiki")
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", f"{first};{second}")
    res = wrr.resolve(cwd=tmp_path)
    assert res.scope == "pj"
    assert res.root == wiki


def test_pj_uses_most_recent_state_file_by_mtime(tmp_path, monkeypatch):
    import os
    proot = tmp_path / "roots"
    new_wiki = _make_wiki(proot / "newproj" / "wiki")
    _make_wiki(proot / "oldproj" / "wiki")
    old = _write_state(tmp_path, "oldproj", name="aaa.json")
    new = _write_state(tmp_path, "newproj", name="bbb.json")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(cwd=tmp_path)
    assert res.scope == "pj"
    assert res.root == new_wiki


def test_pj_prefers_session_state_over_mtime_latest(tmp_path, monkeypatch):
    import os
    proot = tmp_path / "roots"
    my_wiki = _make_wiki(proot / "myproj" / "wiki")
    _make_wiki(proot / "otherproj" / "wiki")
    mine = _write_state(tmp_path, "myproj", name="sid-1234.json")
    other = _write_state(tmp_path, "otherproj", name="zzz-newer.json")
    os.utime(mine, (1000, 1000))
    os.utime(other, (2000, 2000))
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(cwd=tmp_path, session_id="sid-1234")
    assert res.scope == "pj"
    assert res.root == my_wiki


def test_pj_fails_closed_when_session_file_absent(tmp_path, monkeypatch):
    proot = tmp_path / "roots"
    _make_wiki(proot / "onlyproj" / "wiki")
    _write_state(tmp_path, "onlyproj", name="bbb.json")
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    assert wrr.resolve(cwd=tmp_path, session_id="no-such-sid") is None


def test_pj_fails_closed_when_session_file_has_no_project(tmp_path, monkeypatch):
    proot = tmp_path / "roots"
    _make_wiki(proot / "fallbackproj" / "wiki")
    state_dir = tmp_path / "_projects" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "sid-x.json").write_text("{}", encoding="utf-8")
    _write_state(tmp_path, "fallbackproj", name="other.json")
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    assert wrr.resolve(cwd=tmp_path, session_id="sid-x") is None


def test_pj_fails_closed_when_session_project_is_empty_string(tmp_path, monkeypatch):
    import os
    proot = tmp_path / "roots"
    _make_wiki(proot / "otherproj" / "wiki")
    mine = _write_state(tmp_path, "", name="sid-e.json")
    other = _write_state(tmp_path, "otherproj", name="zzz-newer.json")
    os.utime(mine, (1000, 1000))
    os.utime(other, (2000, 2000))
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    assert wrr.resolve(cwd=tmp_path, session_id="sid-e") is None


def test_pj_empty_session_id_still_uses_mtime_latest(tmp_path, monkeypatch):
    import os
    proot = tmp_path / "roots"
    new_wiki = _make_wiki(proot / "newproj" / "wiki")
    _make_wiki(proot / "oldproj" / "wiki")
    old = _write_state(tmp_path, "oldproj", name="aaa.json")
    new = _write_state(tmp_path, "newproj", name="bbb.json")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(cwd=tmp_path, session_id="")
    assert res.scope == "pj"
    assert res.root == new_wiki


def test_pj_skip_continues_to_workspace_not_abort(tmp_path, monkeypatch):
    import os
    proot = tmp_path / "roots"
    proot.mkdir(parents=True, exist_ok=True)
    ws_wiki = _make_wiki(tmp_path / "_llm-wiki")
    _make_wiki(proot / "otherproj" / "wiki")
    other = _write_state(tmp_path, "otherproj", name="zzz-newer.json")
    os.utime(other, (2000, 2000))
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(cwd=tmp_path, session_id="no-such-sid")
    assert res is not None
    assert res.scope == "workspace", (
        "a session-file miss degrades to the lower scopes instead of aborting the "
        "resolve, and the seeded newer state would have produced pj if the "
        "mtime-latest fallback had run"
    )
    assert res.root == ws_wiki


def test_pj_session_id_none_preserves_mtime_latest(tmp_path, monkeypatch):
    import os
    proot = tmp_path / "roots"
    new_wiki = _make_wiki(proot / "newproj" / "wiki")
    _make_wiki(proot / "oldproj" / "wiki")
    old = _write_state(tmp_path, "oldproj", name="aaa.json")
    new = _write_state(tmp_path, "newproj", name="bbb.json")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(cwd=tmp_path, session_id=None)
    assert res.scope == "pj"
    assert res.root == new_wiki


def test_pj_skipped_when_no_state_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    assert wrr.resolve(cwd=tmp_path) is None


def test_pj_skipped_when_project_root_has_no_wiki(tmp_path, monkeypatch):
    proot = tmp_path / "roots"
    (proot / "llm-wiki" / "wiki").mkdir(parents=True, exist_ok=True)
    _write_state(tmp_path, "llm-wiki")
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    assert wrr.resolve(cwd=tmp_path) is None


def test_pj_skipped_on_malformed_state_file(tmp_path, monkeypatch):
    state_dir = tmp_path / "_projects" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    assert wrr.resolve(cwd=tmp_path) is None


def test_workspace_resolves_from_llm_wiki_dir_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    wiki = _make_wiki(tmp_path / "_llm-wiki")
    res = wrr.resolve(cwd=tmp_path)
    assert res is not None
    assert res.scope == "workspace"
    assert res.root == wiki


def test_workspace_root_is_parent_of_taskflow_root(tmp_path, monkeypatch):
    container = tmp_path / "ws" / "_projects"
    container.mkdir(parents=True, exist_ok=True)
    wiki = _make_wiki(tmp_path / "ws" / "_llm-wiki")
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(container))
    res = wrr.resolve(cwd=tmp_path)
    assert res.scope == "workspace"
    assert res.root == wiki


def test_cwd_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    _make_wiki(tmp_path)
    res = wrr.resolve(cwd=tmp_path)
    assert res is not None
    assert res.scope == "cwd"
    assert res.root == tmp_path


def test_none_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    assert wrr.resolve(cwd=tmp_path) is None


def test_precedence_prompt_over_all(tmp_path, monkeypatch):
    proot = tmp_path / "roots"
    _make_wiki(proot / "llm-wiki" / "wiki")
    _write_state(tmp_path, "llm-wiki")
    _make_wiki(tmp_path / "_llm-wiki")
    _make_wiki(tmp_path)
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(prompt_root=str(tmp_path / "override"), cwd=tmp_path)
    assert res.scope == "prompt"
    assert res.root == (tmp_path / "override")


def test_precedence_pj_over_workspace_and_cwd(tmp_path, monkeypatch):
    proot = tmp_path / "roots"
    pj_wiki = _make_wiki(proot / "llm-wiki" / "wiki")
    _write_state(tmp_path, "llm-wiki")
    _make_wiki(tmp_path / "_llm-wiki")
    _make_wiki(tmp_path)
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(cwd=tmp_path)
    assert res.scope == "pj"
    assert res.root == pj_wiki


def test_precedence_workspace_over_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    ws_wiki = _make_wiki(tmp_path / "_llm-wiki")
    _make_wiki(tmp_path)
    res = wrr.resolve(cwd=tmp_path)
    assert res.scope == "workspace"
    assert res.root == ws_wiki


def test_resolver_never_generates(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    assert wrr.resolve(cwd=tmp_path) is None
    assert not (tmp_path / "_llm-wiki").exists()
    assert not (tmp_path / ".llmwiki").exists()
    assert not (tmp_path / "_projects").exists()


def test_child_resolves_single_marked_child(tmp_path, monkeypatch):
    monkeypatch.delenv(wrr.TASKFLOW_PROJECT_ROOTS, raising=False)
    wiki = _make_wiki(tmp_path / "wiki")
    (tmp_path / "source").mkdir()
    (tmp_path / "disposable").mkdir()
    r = wrr.resolve(cwd=tmp_path)
    assert r is not None
    assert r.root == wiki
    assert r.scope == "child"


def test_child_ambiguous_two_marked_children_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv(wrr.TASKFLOW_PROJECT_ROOTS, raising=False)
    _make_wiki(tmp_path / "wiki-a")
    _make_wiki(tmp_path / "wiki-b")
    assert wrr.resolve(cwd=tmp_path) is None


def test_child_is_depth_one_only(tmp_path, monkeypatch):
    monkeypatch.delenv(wrr.TASKFLOW_PROJECT_ROOTS, raising=False)
    _make_wiki(tmp_path / "nested" / "wiki")
    assert wrr.resolve(cwd=tmp_path) is None


def test_cwd_marker_beats_child(tmp_path, monkeypatch):
    monkeypatch.delenv(wrr.TASKFLOW_PROJECT_ROOTS, raising=False)
    _make_wiki(tmp_path)
    _make_wiki(tmp_path / "wiki")
    r = wrr.resolve(cwd=tmp_path)
    assert r is not None
    assert r.root == tmp_path
    assert r.scope == "cwd"


def test_workspace_beats_child(tmp_path, monkeypatch):
    monkeypatch.delenv(wrr.TASKFLOW_PROJECT_ROOTS, raising=False)
    ws_wiki = _make_wiki(tmp_path / wrr.WORKSPACE_WIKI_DIRNAME)
    _make_wiki(tmp_path / "other-wiki")
    r = wrr.resolve(cwd=tmp_path)
    assert r is not None
    assert r.root == ws_wiki
    assert r.scope == "workspace"
