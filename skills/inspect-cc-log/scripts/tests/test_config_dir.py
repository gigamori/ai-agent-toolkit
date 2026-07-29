"""Tests: query.py's CLAUDE_CONFIG_DIR support (views glob rewrite).

Run: uv run --with duckdb python -m unittest discover -s tests -v

Covers the skills half of
`_projects/llm-wiki/project-notes/specs/cc-config-dir-skills.md`:
  - C4: the loader rewrites views.sql's anchor glob, and ONLY when the env var
    is set (unset => byte-identical SQL => zero behaviour change);
  - A-D3': universes holding no logs are filtered out first, because a glob that
    matches nothing aborts CREATE VIEW in DuckDB;
  - AC-A5: views.sql keeps the anchor literal, exactly once.

Hermetic: the end-to-end cases spawn query.py with HOME *and* USERPROFILE
redirected to a tmp dir, so the real ~/.claude is never read. USERPROFILE is the
one that matters on Windows (DuckDB expands `~` from it), and DuckDB does not
observe an in-process env change — hence a subprocess rather than a patched
os.environ.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import query  # noqa: E402

_SCRIPTS = Path(__file__).resolve().parents[1]
_ANCHOR = query._GLOB_ANCHOR


def _write_log(projects_root: Path, slug: str, sid: str) -> None:
    d = projects_root / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(
        json.dumps({"sessionId": sid, "type": "user", "uuid": sid}) + "\n",
        encoding="utf-8",
    )


class GlobRewriteTest(unittest.TestCase):
    """Unit-level: what the loader injects, given an env value."""

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("CLAUDE_CONFIG_DIR", "HOME", "USERPROFILE")}
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.cfg = self.tmp / "cfg"
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _env_glob(self) -> str:
        return (self.cfg / "projects" / "**/*.jsonl").as_posix()

    def test_unset_env_leaves_the_sql_byte_identical(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        sql = f"FROM read_json_objects({_ANCHOR}, format='newline_delimited')"
        self.assertEqual(query._apply_cc_projects_glob(sql), sql)

    def test_blank_env_is_treated_as_unset(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "   "
        self.assertEqual(query._apply_cc_projects_glob(_ANCHOR), _ANCHOR)

    def test_both_universes_become_a_list_env_first(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        _write_log(self.cfg / "projects", "p-env", "sid-env")
        _write_log(self.home / ".claude" / "projects", "p-def", "sid-def")
        self.assertEqual(
            query._apply_cc_projects_glob(_ANCHOR),
            f"['{self._env_glob()}', '{query._DEFAULT_PROJECTS_GLOB}']",
        )

    def test_empty_universe_is_dropped(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        _write_log(self.cfg / "projects", "p-env", "sid-env")
        (self.home / ".claude" / "projects").mkdir(parents=True)  # exists, empty
        self.assertEqual(
            query._apply_cc_projects_glob(_ANCHOR), f"['{self._env_glob()}']")

    def test_no_universe_with_logs_names_the_configured_dir(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        out = query._apply_cc_projects_glob(_ANCHOR)
        self.assertEqual(out, f"['{self._env_glob()}']")
        self.assertNotIn("~/.claude", out)

    def test_glob_metacharacters_in_the_env_path_are_escaped(self):
        """F1: `[`/`]` (legal in Windows dir names) must be class-escaped, or the
        existence filter (pathlib, root literal) and DuckDB's glob diverge and
        CREATE VIEW aborts."""
        cfg = self.tmp / "cfg [test]"
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
        _write_log(cfg / "projects", "p-env", "sid-env")
        expected = f"{self.tmp.as_posix()}/cfg [[]test[]]/projects/**/*.jsonl"
        self.assertEqual(query._apply_cc_projects_glob(_ANCHOR), f"['{expected}']")

    def test_env_value_is_literal_and_cwd_relative(self):
        """CC treats the value literally; expanding `~` would move the universe."""
        os.environ["CLAUDE_CONFIG_DIR"] = "~/cfgtest"
        cwd = Path.cwd()
        expected = (cwd / "~" / "cfgtest" / "projects" / "**/*.jsonl").as_posix()
        self.assertEqual(query._cc_projects_universes()[0][1], expected)


class AnchorContractTest(unittest.TestCase):
    """AC-A5: the literal-replace contract the rewrite depends on."""

    def test_views_sql_holds_the_anchor_exactly_once(self):
        sql = (_SCRIPTS / "views.sql").read_text(encoding="utf-8")
        self.assertEqual(sql.count(_ANCHOR), 1)

    def test_header_comment_does_not_add_a_second_match(self):
        """The header names the path WITHOUT quotes on purpose."""
        sql = (_SCRIPTS / "views.sql").read_text(encoding="utf-8")
        header = sql.split("CREATE OR REPLACE VIEW")[0]
        self.assertNotIn(_ANCHOR, header)


class EndToEndTest(unittest.TestCase):
    """query.py against tmp corpora, env applied at spawn time."""

    def _run_query(self, home: Path, cfg, sql: str):
        env = {k: v for k, v in os.environ.items()
               if k not in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                            "CLAUDE_CONFIG_DIR")}
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        if cfg is not None:
            env["CLAUDE_CONFIG_DIR"] = str(cfg)
        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS / "query.py"), "--sql", sql],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_reads_both_universes_when_env_is_set(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, cfg = tmp / "home", tmp / "cfg"
            _write_log(cfg / "projects", "p-env", "sid-env")
            _write_log(home / ".claude" / "projects", "p-def", "sid-def")
            out = self._run_query(
                home, cfg, "SELECT DISTINCT session_id FROM cc_event ORDER BY 1")
            self.assertEqual([r[0] for r in out["rows"]], ["sid-def", "sid-env"])

    def test_env_universe_alone_still_loads_the_views(self):
        """Without the A-D3' filter this died with IOException at CREATE VIEW."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, cfg = tmp / "home", tmp / "cfg"
            _write_log(cfg / "projects", "p-env", "sid-env")
            (home / ".claude" / "projects").mkdir(parents=True)
            out = self._run_query(
                home, cfg, "SELECT DISTINCT session_id FROM cc_event")
            self.assertEqual([r[0] for r in out["rows"]], ["sid-env"])

    def test_bracketed_config_dir_reads_end_to_end(self):
        """F1 end to end: a `[`/`]` config-dir path loads and reads via DuckDB."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, cfg = tmp / "home", tmp / "cfg [test]"
            _write_log(cfg / "projects", "p-env", "sid-env")
            (home / ".claude" / "projects").mkdir(parents=True)
            out = self._run_query(
                home, cfg, "SELECT DISTINCT session_id FROM cc_event")
            self.assertEqual([r[0] for r in out["rows"]], ["sid-env"])

    def test_without_env_only_the_default_universe_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, cfg = tmp / "home", tmp / "cfg"
            _write_log(home / ".claude" / "projects", "p-def", "sid-def")
            _write_log(cfg / "projects", "p-env", "sid-env")  # must NOT be read
            out = self._run_query(
                home, None, "SELECT DISTINCT session_id FROM cc_event")
            self.assertEqual([r[0] for r in out["rows"]], ["sid-def"])


if __name__ == "__main__":
    unittest.main()
