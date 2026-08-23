import json
import os
import sys
from pathlib import Path

import pytest

from llmwiki.ingest import cc_paths

_ANCHOR = cc_paths.CC_PROJECTS_GLOB_ANCHOR


def _redirect_home(monkeypatch, home):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)


def _write_log(projects_root, slug, sid):
    d = projects_root / slug
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text('{"sessionId": "%s", "type": "user"}\n' % sid, encoding="utf-8")
    return f


def test_roots_without_env_is_the_default_universe_only(tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    assert cc_paths.cc_projects_roots() == [tmp_path / ".claude" / "projects"]


def test_roots_with_absolute_env_are_env_then_default(tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path / "home")
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))

    assert cc_paths.cc_projects_roots() == [
        cfg / "projects",
        tmp_path / "home" / ".claude" / "projects",
    ]


def test_roots_dedup_when_env_points_at_the_default_dir(tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    default_cfg = tmp_path / ".claude"
    spelling = str(default_cfg).upper() if os.name == "nt" else str(default_cfg) + "/."
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", spelling)

    roots = cc_paths.cc_projects_roots()
    assert len(roots) == 1, roots


def test_env_value_is_literal_not_expanduser(tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/cfgtest")

    first = cc_paths.cc_projects_roots()[0]
    assert first == tmp_path / "~" / "cfgtest" / "projects"
    assert first != tmp_path / "home" / "cfgtest" / "projects", (
        "the env value is taken literally, the way the harness itself takes it; "
        "expanding it here would read a different place than the harness writes"
    )


def test_relative_env_value_resolves_against_cwd(tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "rel-cfg")

    assert cc_paths.cc_projects_roots()[0] == tmp_path / "rel-cfg" / "projects"


def test_nested_env_universe_collapses_to_the_default_root(tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    default = tmp_path / ".claude" / "projects"
    nested_cfg = default / "nested-cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(nested_cfg))

    assert cc_paths.cc_projects_roots() == [default], (
        "a config dir nested inside the default projects tree is dropped, or every "
        "row it holds would be read twice"
    )


def test_sibling_prefix_root_is_not_treated_as_nested(tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    default = tmp_path / ".claude" / "projects"
    sibling_cfg = tmp_path / ".claude" / "projectsX"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(sibling_cfg))

    roots = cc_paths.cc_projects_roots()
    assert len(roots) == 2, (
        "sharing a string prefix is not sharing a path segment, so both roots survive"
    )
    assert default in roots
    assert sibling_cfg / "projects" in roots


def test_no_env_leaves_the_sql_byte_identical(tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    sql = f"FROM read_json_objects({_ANCHOR}, format='newline_delimited')"

    assert cc_paths.apply_cc_projects_glob(sql) == sql


def test_both_universes_with_logs_become_a_glob_list_env_first(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    _redirect_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _write_log(cfg / "projects", "proj-env", "sid-env")
    _write_log(home / ".claude" / "projects", "proj-def", "sid-def")

    out = cc_paths.apply_cc_projects_glob(f"FROM read_json_objects({_ANCHOR}, x)")

    env_glob = (cfg / "projects" / "**/*.jsonl").as_posix()
    assert out == (
        f"FROM read_json_objects(['{env_glob}', "
        f"'{cc_paths.CC_PROJECTS_DEFAULT_GLOB}'], x)"
    )


def test_empty_default_universe_is_dropped_from_the_list(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    _redirect_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _write_log(cfg / "projects", "proj-env", "sid-env")
    (home / ".claude" / "projects").mkdir(parents=True)

    out = cc_paths.apply_cc_projects_glob(_ANCHOR)

    env_glob = (cfg / "projects" / "**/*.jsonl").as_posix()
    assert out == f"['{env_glob}']"


def test_empty_env_universe_is_dropped_from_the_list(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    _redirect_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _write_log(home / ".claude" / "projects", "proj-def", "sid-def")

    out = cc_paths.apply_cc_projects_glob(_ANCHOR)

    assert out == f"['{cc_paths.CC_PROJECTS_DEFAULT_GLOB}']"


def test_no_universe_with_logs_injects_the_env_glob_alone(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    _redirect_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))

    out = cc_paths.apply_cc_projects_glob(_ANCHOR)

    assert out == f"['{(cfg / 'projects' / '**/*.jsonl').as_posix()}']"
    assert "~/.claude" not in out


def test_glob_metacharacters_in_the_env_path_are_escaped(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = tmp_path / "cfg [test]"
    _redirect_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _write_log(cfg / "projects", "proj-env", "sid-env")

    out = cc_paths.apply_cc_projects_glob(_ANCHOR)

    expected = f"{tmp_path.as_posix()}/cfg [[]test[]]/projects/**/*.jsonl"
    assert out == f"['{expected}']", (
        "each bracket is wrapped in its own character class so the glob matches the "
        "literal directory name"
    )


def test_blank_env_value_is_treated_as_unset(tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")

    assert cc_paths.apply_cc_projects_glob(_ANCHOR) == _ANCHOR
    assert cc_paths.cc_projects_roots() == [tmp_path / ".claude" / "projects"]


def test_cc_views_sql_holds_the_anchor_exactly_once():
    from llmwiki.ingest import cc_log_project

    sql = cc_log_project._VIEWS_SQL.read_text(encoding="utf-8")
    assert sql.count(_ANCHOR) == 1, (
        "the literal-replace rewrite is only safe while this anchor appears once"
    )


def test_read_cc_views_sql_rewrites_the_real_views_file(tmp_path, monkeypatch):
    from llmwiki.ingest import cc_log_project

    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    _redirect_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _write_log(cfg / "projects", "proj-env", "sid-env")

    out = cc_paths.read_cc_views_sql(cc_log_project._VIEWS_SQL)

    assert _ANCHOR not in out
    assert (cfg / "projects" / "**/*.jsonl").as_posix() in out


_VIEWS_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import duckdb
from llmwiki.ingest import cc_log_project, cc_paths
con = duckdb.connect()
con.execute(cc_paths.read_cc_views_sql(cc_log_project._VIEWS_SQL))
rows = con.execute("SELECT DISTINCT session_id FROM cc_event").fetchall()
print(json.dumps(sorted(r[0] for r in rows)))
"""


def _sids_via_subprocess(tmp_path, home, cfg):
    import subprocess

    pkg_root = str(Path(__file__).resolve().parents[1])
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "CLAUDE_CONFIG_DIR")
    }
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if cfg is not None:
        env["CLAUDE_CONFIG_DIR"] = str(cfg)
    proc = subprocess.run(
        [sys.executable, "-c", _VIEWS_PROBE, pkg_root],
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    return set(json.loads(proc.stdout))


def test_views_read_both_universes_end_to_end(tmp_path):
    pytest.importorskip("duckdb")
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    _write_log(cfg / "projects", "proj-env", "sid-env")
    _write_log(home / ".claude" / "projects", "proj-def", "sid-def")

    assert _sids_via_subprocess(tmp_path, home, cfg) == {"sid-env", "sid-def"}


def test_views_load_when_only_the_env_universe_has_logs(tmp_path):
    pytest.importorskip("duckdb")
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    _write_log(cfg / "projects", "proj-env", "sid-env")
    (home / ".claude" / "projects").mkdir(parents=True)

    assert _sids_via_subprocess(tmp_path, home, cfg) == {"sid-env"}


def test_views_load_when_only_the_default_universe_has_logs(tmp_path):
    pytest.importorskip("duckdb")
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    _write_log(home / ".claude" / "projects", "proj-def", "sid-def")
    cfg.mkdir()

    assert _sids_via_subprocess(tmp_path, home, cfg) == {"sid-def"}


def test_views_read_a_bracketed_config_dir_end_to_end(tmp_path):
    pytest.importorskip("duckdb")
    home, cfg = tmp_path / "home", tmp_path / "cfg [test]"
    _write_log(cfg / "projects", "proj-env", "sid-env")
    _write_log(home / ".claude" / "projects", "proj-def", "sid-def")

    assert _sids_via_subprocess(tmp_path, home, cfg) == {"sid-env", "sid-def"}


def test_views_unchanged_path_reads_only_the_default_universe(tmp_path):
    pytest.importorskip("duckdb")
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    _write_log(home / ".claude" / "projects", "proj-def", "sid-def")
    _write_log(cfg / "projects", "proj-env", "sid-env")

    assert _sids_via_subprocess(tmp_path, home, cfg=None) == {"sid-def"}


_ROW_COUNT_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import duckdb
from llmwiki.ingest import cc_log_project, cc_paths
con = duckdb.connect()
con.execute(cc_paths.read_cc_views_sql(cc_log_project._VIEWS_SQL))
rows = con.execute("SELECT COUNT(*) FROM cc_event").fetchall()
print(json.dumps(rows[0][0]))
"""


def test_nested_env_universe_does_not_double_session_rows(tmp_path):
    pytest.importorskip("duckdb")
    import subprocess

    home = tmp_path / "home"
    nested_cfg = home / ".claude" / "projects" / "nested-cfg"
    _write_log(nested_cfg / "projects", "proj-nested", "sid-nested")

    pkg_root = str(Path(__file__).resolve().parents[1])
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "CLAUDE_CONFIG_DIR")
    }
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(nested_cfg)
    proc = subprocess.run(
        [sys.executable, "-c", _ROW_COUNT_PROBE, pkg_root],
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert json.loads(proc.stdout) == 1


def test_real_claude_dir_is_never_resolved_once_home_is_redirected(tmp_path, monkeypatch):
    real_projects = os.path.normcase(os.path.expanduser("~/.claude/projects"))
    _redirect_home(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))

    for root in cc_paths.cc_projects_roots():
        assert os.path.normcase(str(root)) != real_projects
