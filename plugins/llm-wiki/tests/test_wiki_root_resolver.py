"""Tests: multi-scope wiki-root resolver (plan T1, §2-A, W-a/W-b).

Covers each scope in isolation and the full precedence chain:
  prompt > pj > workspace > cwd ; None when nothing exists.

pj uses the most-recent `_projects/_state/*.json` `project` field +
`$TASKFLOW_PROJECT_ROOTS` (or the `_projects/` fallback), and degrades cleanly
(skips pj) when there is no state file / no project / no matching wiki.

These tests AUTHOR the expectations only (T1: execute, no self-run).
"""
import json

from llmwiki.core import wiki_root_resolver as wrr


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _make_wiki(path):
    """Create a wiki-root marker `.llmwiki` at `path` and return `path`."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n", encoding="utf-8")
    return path


def _write_state(cwd, project, name="0000.json"):
    """Write a taskflow state file `{"project": ...}` under `_projects/_state/`."""
    state_dir = cwd / "_projects" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    f = state_dir / name
    f.write_text(json.dumps({"project": project}), encoding="utf-8")
    return f


# --------------------------------------------------------------------------- #
# scope: prompt
# --------------------------------------------------------------------------- #
def test_prompt_root_wins(tmp_path):
    # Even with nothing on disk, an explicit prompt_root resolves verbatim.
    res = wrr.resolve(prompt_root=str(tmp_path / "anywhere"), cwd=tmp_path)
    assert res is not None
    assert res.scope == "prompt"
    assert res.root == (tmp_path / "anywhere")


def test_prompt_root_not_existence_gated(tmp_path):
    # prompt scope is taken verbatim — no `.llmwiki` required.
    res = wrr.resolve(prompt_root=str(tmp_path / "nope"), cwd=tmp_path)
    assert res.scope == "prompt"


# --------------------------------------------------------------------------- #
# F3 — the prompt root is absolutized (review §5 / F3): a RELATIVE `--root` must
# resolve stably regardless of the process CWD.
# --------------------------------------------------------------------------- #
def test_prompt_root_is_absolutized(tmp_path):
    # An absolute prompt_root comes back absolute (resolved), not a bare Path.
    res = wrr.resolve(prompt_root=str(tmp_path / "anywhere"), cwd=tmp_path)
    assert res.root.is_absolute()
    assert res.root == (tmp_path / "anywhere").resolve()


def test_relative_prompt_root_resolves_against_cwd_stably(tmp_path, monkeypatch):
    # A RELATIVE --root is absolutized against the process CWD, so the result is
    # an absolute path (independent of how the caller spelled it). Chdir into
    # tmp_path so `Path("relwiki").resolve()` == tmp_path/relwiki.
    monkeypatch.chdir(tmp_path)
    res = wrr.resolve(prompt_root="relwiki", cwd=tmp_path)
    assert res.scope == "prompt"
    assert res.root.is_absolute()
    assert res.root == (tmp_path / "relwiki").resolve()


# --------------------------------------------------------------------------- #
# scope: pj
# --------------------------------------------------------------------------- #
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
    # marker only in the SECOND root -> first has no match, resolver falls to it.
    wiki = _make_wiki(second / "llm-wiki" / "wiki")
    (first / "llm-wiki" / "wiki").mkdir(parents=True, exist_ok=True)  # no .llmwiki
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


# --------------------------------------------------------------------------- #
# scope: pj — session-aware (Phase 1 P1)
# --------------------------------------------------------------------------- #
def test_pj_prefers_session_state_over_mtime_latest(tmp_path, monkeypatch):
    # Concurrent-session safety: the mtime-NEWEST state points at `otherproj`,
    # but the session's own `<sid>.json` points at `myproj`. With session_id,
    # the resolver MUST resolve `myproj` (not the mtime-latest one).
    import os
    proot = tmp_path / "roots"
    my_wiki = _make_wiki(proot / "myproj" / "wiki")
    _make_wiki(proot / "otherproj" / "wiki")
    mine = _write_state(tmp_path, "myproj", name="sid-1234.json")
    other = _write_state(tmp_path, "otherproj", name="zzz-newer.json")
    os.utime(mine, (1000, 1000))    # my state is OLDER
    os.utime(other, (2000, 2000))   # a concurrent session wrote a newer one
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(cwd=tmp_path, session_id="sid-1234")
    assert res.scope == "pj"
    assert res.root == my_wiki


def test_pj_falls_back_to_mtime_when_session_file_absent(tmp_path, monkeypatch):
    # session_id given but no `<sid>.json` -> fall back to mtime-latest.
    proot = tmp_path / "roots"
    wiki = _make_wiki(proot / "onlyproj" / "wiki")
    _write_state(tmp_path, "onlyproj", name="bbb.json")
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(cwd=tmp_path, session_id="no-such-sid")
    assert res.scope == "pj"
    assert res.root == wiki


def test_pj_falls_back_when_session_file_has_no_project(tmp_path, monkeypatch):
    # `<sid>.json` exists but has no usable `project` -> fall back to mtime-latest.
    proot = tmp_path / "roots"
    wiki = _make_wiki(proot / "fallbackproj" / "wiki")
    state_dir = tmp_path / "_projects" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "sid-x.json").write_text("{}", encoding="utf-8")  # no project
    _write_state(tmp_path, "fallbackproj", name="other.json")
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(cwd=tmp_path, session_id="sid-x")
    assert res.scope == "pj"
    assert res.root == wiki


def test_pj_session_id_none_preserves_mtime_latest(tmp_path, monkeypatch):
    # session_id=None (the CLI path) must keep the legacy mtime-latest behavior.
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
    # No state dir at all -> pj skipped, nothing else exists -> None (degrade).
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    assert wrr.resolve(cwd=tmp_path) is None


def test_pj_skipped_when_project_root_has_no_wiki(tmp_path, monkeypatch):
    # State file present, but `<proot>/<project>/wiki/` has no `.llmwiki`.
    proot = tmp_path / "roots"
    (proot / "llm-wiki" / "wiki").mkdir(parents=True, exist_ok=True)  # no marker
    _write_state(tmp_path, "llm-wiki")
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    assert wrr.resolve(cwd=tmp_path) is None


def test_pj_skipped_on_malformed_state_file(tmp_path, monkeypatch):
    # Unreadable/invalid JSON state file degrades to skip (never errors).
    state_dir = tmp_path / "_projects" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    assert wrr.resolve(cwd=tmp_path) is None


# --------------------------------------------------------------------------- #
# scope: workspace
# --------------------------------------------------------------------------- #
def test_workspace_resolves_from_llm_wiki_dir_fallback(tmp_path, monkeypatch):
    # No env -> workspace-root is CWD (parent of `_projects/`); wiki at
    # `<cwd>/_llm-wiki/`.
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    wiki = _make_wiki(tmp_path / "_llm-wiki")
    res = wrr.resolve(cwd=tmp_path)
    assert res is not None
    assert res.scope == "workspace"
    assert res.root == wiki


def test_workspace_root_is_parent_of_taskflow_root(tmp_path, monkeypatch):
    # workspace-root = parent of the first existing $TASKFLOW_PROJECT_ROOTS entry.
    container = tmp_path / "ws" / "_projects"
    container.mkdir(parents=True, exist_ok=True)
    wiki = _make_wiki(tmp_path / "ws" / "_llm-wiki")
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(container))
    res = wrr.resolve(cwd=tmp_path)
    assert res.scope == "workspace"
    assert res.root == wiki


# --------------------------------------------------------------------------- #
# scope: cwd
# --------------------------------------------------------------------------- #
def test_cwd_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    _make_wiki(tmp_path)  # marker directly on the CWD
    res = wrr.resolve(cwd=tmp_path)
    assert res is not None
    assert res.scope == "cwd"
    assert res.root == tmp_path


# --------------------------------------------------------------------------- #
# scope: none
# --------------------------------------------------------------------------- #
def test_none_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    assert wrr.resolve(cwd=tmp_path) is None


# --------------------------------------------------------------------------- #
# precedence chain: prompt > pj > workspace > cwd
# --------------------------------------------------------------------------- #
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
    _make_wiki(tmp_path / "_llm-wiki")   # workspace also present
    _make_wiki(tmp_path)                  # cwd also present
    monkeypatch.setenv("TASKFLOW_PROJECT_ROOTS", str(proot))
    res = wrr.resolve(cwd=tmp_path)
    assert res.scope == "pj"
    assert res.root == pj_wiki


def test_precedence_workspace_over_cwd(tmp_path, monkeypatch):
    # No state file -> pj skipped; workspace beats cwd.
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    ws_wiki = _make_wiki(tmp_path / "_llm-wiki")
    _make_wiki(tmp_path)  # cwd also present
    res = wrr.resolve(cwd=tmp_path)
    assert res.scope == "workspace"
    assert res.root == ws_wiki


def test_resolver_never_generates(tmp_path, monkeypatch):
    # No marker anywhere -> None, and the resolver must not create any path.
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    assert wrr.resolve(cwd=tmp_path) is None
    assert not (tmp_path / "_llm-wiki").exists()
    assert not (tmp_path / ".llmwiki").exists()
    assert not (tmp_path / "_projects").exists()


# --------------------------------------------------------------------------- #
# scope: child (2026-08-08 — exactly-one immediate child with a marker)
# --------------------------------------------------------------------------- #
def test_child_resolves_single_marked_child(tmp_path, monkeypatch):
    monkeypatch.delenv(wrr.TASKFLOW_PROJECT_ROOTS, raising=False)
    wiki = _make_wiki(tmp_path / "wiki")
    (tmp_path / "source").mkdir()          # unmarked siblings must not interfere
    (tmp_path / "disposable").mkdir()
    r = wrr.resolve(cwd=tmp_path)
    assert r is not None
    assert r.root == wiki
    assert r.scope == "child"


def test_child_ambiguous_two_marked_children_returns_none(tmp_path, monkeypatch):
    # Fail-closed: picking either silently could write to the wrong wiki.
    monkeypatch.delenv(wrr.TASKFLOW_PROJECT_ROOTS, raising=False)
    _make_wiki(tmp_path / "wiki-a")
    _make_wiki(tmp_path / "wiki-b")
    assert wrr.resolve(cwd=tmp_path) is None


def test_child_is_depth_one_only(tmp_path, monkeypatch):
    # A marker two levels down must NOT resolve (no recursive scan).
    monkeypatch.delenv(wrr.TASKFLOW_PROJECT_ROOTS, raising=False)
    _make_wiki(tmp_path / "nested" / "wiki")
    assert wrr.resolve(cwd=tmp_path) is None


def test_cwd_marker_beats_child(tmp_path, monkeypatch):
    # Precedence: cwd (scope 4) wins over a marked child (scope 5).
    monkeypatch.delenv(wrr.TASKFLOW_PROJECT_ROOTS, raising=False)
    _make_wiki(tmp_path)
    _make_wiki(tmp_path / "wiki")
    r = wrr.resolve(cwd=tmp_path)
    assert r is not None
    assert r.root == tmp_path
    assert r.scope == "cwd"


def test_workspace_beats_child(tmp_path, monkeypatch):
    # Precedence: workspace (scope 3) wins over a marked child (scope 5) even
    # though _llm-wiki is itself an immediate child — it must surface as
    # "workspace", not "child".
    monkeypatch.delenv(wrr.TASKFLOW_PROJECT_ROOTS, raising=False)
    ws_wiki = _make_wiki(tmp_path / wrr.WORKSPACE_WIKI_DIRNAME)
    _make_wiki(tmp_path / "other-wiki")
    r = wrr.resolve(cwd=tmp_path)
    assert r is not None
    assert r.root == ws_wiki
    assert r.scope == "workspace"
