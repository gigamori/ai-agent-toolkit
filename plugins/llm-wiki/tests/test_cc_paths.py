"""Tests: `cc_paths` — CLAUDE_CONFIG_DIR-aware CC session-log resolution (A class).

Covers the design locked in
`_projects/llm-wiki/project-notes/specs/cc-config-dir-ingest.md`:
  - A-D1: the env value is LITERAL (no expanduser / no expandvars) and relative
    values resolve against the cwd, replicating what CC itself does;
  - A-D2: readers scan the union `[$CLAUDE_CONFIG_DIR, ~/.claude]`, env first,
    deduped case-insensitively;
  - A-D3': the views' anchor glob is rewritten by the LOADER only when the env
    var is set, and only universes that actually contain logs are injected — a
    glob matching nothing aborts CREATE VIEW in DuckDB (probed 2026-07-29,
    duckdb 1.5.5), so an empty universe must never reach the SQL;
  - AC-A5: `cc_views.sql` keeps the anchor literal, exactly once.

Hermetic: `HOME`/`USERPROFILE` are redirected to tmp dirs, so the real
`~/.claude` is never read. `USERPROFILE` is the one that matters on Windows —
both DuckDB and `os.path.expanduser` resolve `~` from it there (probed), and
redirecting only `HOME` would silently leave `~` pointing at the real profile.
"""
import json
import os
import sys
from pathlib import Path

import pytest

from llmwiki.ingest import cc_paths

_ANCHOR = cc_paths.CC_PROJECTS_GLOB_ANCHOR


def _redirect_home(monkeypatch, home):
    """Point `~` at `home` for both posix and Windows expanduser rules."""
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


# --------------------------------------------------------------------------- #
# A-D1 / A-D2: root resolution
# --------------------------------------------------------------------------- #
def test_roots_without_env_is_the_default_universe_only(tmp_path, monkeypatch):
    """AC-A1: unset env -> exactly today's single root."""
    _redirect_home(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    assert cc_paths.cc_projects_roots() == [tmp_path / ".claude" / "projects"]


def test_roots_with_absolute_env_are_env_then_default(tmp_path, monkeypatch):
    """AC-A2: env universe FIRST (first-wins lookups get env priority)."""
    _redirect_home(monkeypatch, tmp_path / "home")
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))

    assert cc_paths.cc_projects_roots() == [
        cfg / "projects",
        tmp_path / "home" / ".claude" / "projects",
    ]


def test_roots_dedup_when_env_points_at_the_default_dir(tmp_path, monkeypatch):
    """Same dir under a different spelling collapses to one root.

    On Windows that means case-insensitively (`os.path.normcase`); elsewhere the
    `abspath` normalization is what collapses the two spellings.
    """
    _redirect_home(monkeypatch, tmp_path)
    default_cfg = tmp_path / ".claude"
    spelling = str(default_cfg).upper() if os.name == "nt" else str(default_cfg) + "/."
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", spelling)

    roots = cc_paths.cc_projects_roots()
    assert len(roots) == 1, roots


def test_env_value_is_literal_not_expanduser(tmp_path, monkeypatch):
    """AC-A3: `~` in the value is NOT expanded; the value is cwd-relative literal.

    This mirrors CC's measured behaviour (a value of `~/cfgtest` produced
    `<cwd>/~/cfgtest`), so we must resolve it the same way or we would read a
    different place than CC writes.
    """
    _redirect_home(monkeypatch, tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/cfgtest")

    first = cc_paths.cc_projects_roots()[0]
    assert first == tmp_path / "~" / "cfgtest" / "projects"
    assert first != tmp_path / "home" / "cfgtest" / "projects"  # NOT expanduser'd


def test_relative_env_value_resolves_against_cwd(tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "rel-cfg")

    assert cc_paths.cc_projects_roots()[0] == tmp_path / "rel-cfg" / "projects"


def test_nested_env_universe_collapses_to_the_default_root(tmp_path, monkeypatch):
    """A config dir nested inside the default's `projects` tree must be dropped:
    reading it as well would double every row it holds."""
    _redirect_home(monkeypatch, tmp_path)
    default = tmp_path / ".claude" / "projects"
    nested_cfg = default / "nested-cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(nested_cfg))

    assert cc_paths.cc_projects_roots() == [default]


def test_sibling_prefix_root_is_not_treated_as_nested(tmp_path, monkeypatch):
    """`<default>/projectsX` merely shares a string prefix with
    `<default>/projects` -- not a real path segment -- so both roots survive."""
    _redirect_home(monkeypatch, tmp_path)
    default = tmp_path / ".claude" / "projects"
    sibling_cfg = tmp_path / ".claude" / "projectsX"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(sibling_cfg))

    roots = cc_paths.cc_projects_roots()
    assert len(roots) == 2
    assert default in roots
    assert sibling_cfg / "projects" in roots


# --------------------------------------------------------------------------- #
# A-D3': loader-side SQL rewrite
# --------------------------------------------------------------------------- #
def test_no_env_leaves_the_sql_byte_identical(tmp_path, monkeypatch):
    """AC-A1: zero replacement without the env var — no regression path."""
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
    """A-D3': an empty universe must not reach DuckDB (it aborts CREATE VIEW)."""
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    _redirect_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _write_log(cfg / "projects", "proj-env", "sid-env")
    (home / ".claude" / "projects").mkdir(parents=True)  # exists but empty

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
    """The read still fails (as today), but the error names the CONFIGURED dir."""
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    _redirect_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))

    out = cc_paths.apply_cc_projects_glob(_ANCHOR)

    assert out == f"['{(cfg / 'projects' / '**/*.jsonl').as_posix()}']"
    assert "~/.claude" not in out


def test_glob_metacharacters_in_the_env_path_are_escaped(tmp_path, monkeypatch):
    """F1: `[`/`]` are legal in Windows dir names but glob metachars to DuckDB.

    Unescaped, the pathlib existence filter (root literal) says "has logs" while
    DuckDB's glob matches nothing -> CREATE VIEW aborts (probed). The injected
    glob must therefore class-escape `[]*?` in the directory part.
    """
    home = tmp_path / "home"
    cfg = tmp_path / "cfg [test]"
    _redirect_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _write_log(cfg / "projects", "proj-env", "sid-env")

    out = cc_paths.apply_cc_projects_glob(_ANCHOR)

    # `cfg [test]` -> `cfg [[]test[]]` (each of `[` `]` wrapped in a class)
    expected = f"{tmp_path.as_posix()}/cfg [[]test[]]/projects/**/*.jsonl"
    assert out == f"['{expected}']"


def test_blank_env_value_is_treated_as_unset(tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")

    assert cc_paths.apply_cc_projects_glob(_ANCHOR) == _ANCHOR
    assert cc_paths.cc_projects_roots() == [tmp_path / ".claude" / "projects"]


# --------------------------------------------------------------------------- #
# AC-A5: the anchor contract the rewrite depends on
# --------------------------------------------------------------------------- #
def test_cc_views_sql_holds_the_anchor_exactly_once():
    """A literal-replace contract is only safe while the anchor is unique.

    The header comment names the same path WITHOUT quotes on purpose, so it is
    not a second match. The canonical sibling
    `skills/inspect-cc-log/scripts/views.sql` is covered by
    test_cc_views_contract.py's byte-equality assertion.
    """
    from llmwiki.ingest import cc_log_project

    sql = cc_log_project._VIEWS_SQL.read_text(encoding="utf-8")
    assert sql.count(_ANCHOR) == 1


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


# --------------------------------------------------------------------------- #
# Integration: the rewritten SQL really reads both universes through DuckDB
#
# These run in a SUBPROCESS with the environment set at spawn time. DuckDB
# expands `~` itself, and on Windows it does NOT observe an in-process
# `os.environ` change (measured 2026-07-29: with USERPROFILE monkeypatched, the
# `~/...` glob still resolved to the REAL profile and read the live corpus) — its
# native runtime keeps its own copy of the environment block. Spawning is
# therefore the only way to test the default universe hermetically, and it is
# also how the env var reaches the readers in production.
# --------------------------------------------------------------------------- #
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
    """Load the views in a child process whose `~` and env var point at tmp."""
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
        # Strict UTF-8, not the platform default: the probe's output is the
        # assertion subject, so a decode that silently substitutes would
        # launder exactly what this test exists to observe.
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    return set(json.loads(proc.stdout))


def test_views_read_both_universes_end_to_end(tmp_path):
    """AC-A2 end to end: env universe AND ~/.claude, in one view stack."""
    pytest.importorskip("duckdb")
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    _write_log(cfg / "projects", "proj-env", "sid-env")
    _write_log(home / ".claude" / "projects", "proj-def", "sid-def")

    assert _sids_via_subprocess(tmp_path, home, cfg) == {"sid-env", "sid-def"}


def test_views_load_when_only_the_env_universe_has_logs(tmp_path):
    """Without the A-D3' filter this raised IOException at CREATE VIEW time."""
    pytest.importorskip("duckdb")
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    _write_log(cfg / "projects", "proj-env", "sid-env")
    (home / ".claude" / "projects").mkdir(parents=True)  # exists but empty

    assert _sids_via_subprocess(tmp_path, home, cfg) == {"sid-env"}


def test_views_load_when_only_the_default_universe_has_logs(tmp_path):
    """The other half of the A-D3' filter: an empty ENV universe is dropped too."""
    pytest.importorskip("duckdb")
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    _write_log(home / ".claude" / "projects", "proj-def", "sid-def")
    cfg.mkdir()

    assert _sids_via_subprocess(tmp_path, home, cfg) == {"sid-def"}


def test_views_read_a_bracketed_config_dir_end_to_end(tmp_path):
    """F1 end to end: a `[`/`]` config-dir path loads and reads through DuckDB."""
    pytest.importorskip("duckdb")
    home, cfg = tmp_path / "home", tmp_path / "cfg [test]"
    _write_log(cfg / "projects", "proj-env", "sid-env")
    _write_log(home / ".claude" / "projects", "proj-def", "sid-def")

    assert _sids_via_subprocess(tmp_path, home, cfg) == {"sid-env", "sid-def"}


def test_views_unchanged_path_reads_only_the_default_universe(tmp_path):
    """AC-A1 end to end: no env var -> the untouched SQL, default universe only."""
    pytest.importorskip("duckdb")
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    _write_log(home / ".claude" / "projects", "proj-def", "sid-def")
    _write_log(cfg / "projects", "proj-env", "sid-env")  # must NOT be read

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
    """Env root nested inside the default's `projects` tree must not be read
    twice through the DuckDB views (each event row would double)."""
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
    """AC-A7 guard: the redirect really moves `~`, so no root is the live corpus.

    (A plain `startswith(real_home)` check would be meaningless on Windows — the
    tmp dir itself lives under the user profile.)
    """
    real_projects = os.path.normcase(os.path.expanduser("~/.claude/projects"))
    _redirect_home(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))

    for root in cc_paths.cc_projects_roots():
        assert os.path.normcase(str(root)) != real_projects
