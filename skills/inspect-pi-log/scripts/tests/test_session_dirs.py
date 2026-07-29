"""Tests: query.py's PI_CODING_AGENT_DIR / _SESSION_DIR support (views glob rewrite).

Run: uv run --with duckdb python -m unittest discover -s tests -v

Covers WP-A of
`pi/_projects/pi-extensions-dev/project-notes/specs/agent-dir-env-support-design.md`:
  - the loader rewrites views.sql's anchor glob, and ONLY when an env var is set
    (unset => byte-identical SQL => zero behaviour change for other harnesses);
  - the union order is flat SESSION_DIR -> AGENT_DIR/sessions -> home default,
    and the home default is always present;
  - `~` IS expanded (pi semantics, the opposite of CLAUDE_CONFIG_DIR);
  - roots are deduped, because DuckDB doubles every row of a root listed twice;
  - universes holding no logs are filtered out first, because a glob that
    matches nothing aborts CREATE VIEW in DuckDB;
  - views.sql keeps the anchor literal, exactly once.

Hermetic: the end-to-end cases spawn query.py with HOME *and* USERPROFILE
redirected to a tmp dir, so the real ~/.pi is never read. USERPROFILE is the one
that matters on Windows (DuckDB expands `~` from it), and DuckDB does not
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
_ENV_KEYS = ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR",
             "HOME", "USERPROFILE")


def _write_log(sessions_dir: Path, sid: str) -> None:
    """A minimal 2-line session file. session_id comes from the name's UUID."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"2026-07-30T00-00-00_{sid}.jsonl").write_text(
        json.dumps({"type": "session", "version": 1, "id": sid, "cwd": "/tmp"})
        + "\n"
        + json.dumps({
            "type": "message", "id": "e1",
            "timestamp": "2026-07-30T00:00:01.000Z",
            "message": {"role": "user", "content": "hi"},
        })
        + "\n",
        encoding="utf-8",
    )


_SID_ENV = "11111111-1111-1111-1111-111111111111"
_SID_HOME = "22222222-2222-2222-2222-222222222222"
_SID_FLAT = "33333333-3333-3333-3333-333333333333"


class UniverseResolutionTest(unittest.TestCase):
    """Unit-level: which roots the loader resolves, given env values."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"):
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.agent = self.tmp / "agent"
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _home_sessions(self) -> Path:
        return self.home / ".pi" / "agent" / "sessions"

    def _glob_for(self, root: Path) -> str:
        return f"{root.as_posix()}/**/*.jsonl"

    def test_unset_env_leaves_the_sql_byte_identical(self):
        sql = f"FROM read_json_objects({_ANCHOR}, format='newline_delimited')"
        self.assertEqual(query._apply_pi_sessions_glob(sql), sql)

    def test_blank_env_is_treated_as_unset(self):
        os.environ["PI_CODING_AGENT_DIR"] = "   "
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = ""
        self.assertEqual(query._apply_pi_sessions_glob(_ANCHOR), _ANCHOR)

    def test_agent_dir_is_listed_before_the_home_default(self):
        os.environ["PI_CODING_AGENT_DIR"] = str(self.agent)
        _write_log(self.agent / "sessions", _SID_ENV)
        _write_log(self._home_sessions(), _SID_HOME)
        self.assertEqual(
            query._apply_pi_sessions_glob(_ANCHOR),
            f"['{self._glob_for(self.agent / 'sessions')}', "
            f"'{query._DEFAULT_SESSIONS_GLOB}']",
        )

    def test_session_dir_is_a_flat_store_and_takes_the_lead(self):
        flat = self.tmp / "flat"
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(flat)
        os.environ["PI_CODING_AGENT_DIR"] = str(self.agent)
        roots = [root for root, _glob in query._pi_session_universes()]
        self.assertEqual(
            roots, [flat, self.agent / "sessions", self._home_sessions()])

    def test_tilde_is_expanded(self):
        """Pi's expandTildePath expands `~` — the opposite of CLAUDE_CONFIG_DIR."""
        os.environ["PI_CODING_AGENT_DIR"] = "~/piroot"
        self.assertEqual(
            query._pi_session_universes()[0][0], self.home / "piroot" / "sessions")

    def test_tilde_user_form_is_left_alone(self):
        """normalizePath expands `~` and `~/` only, so os.path.expanduser
        (which also resolves `~someuser`) would over-expand here."""
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = "~someuser"
        self.assertEqual(
            query._pi_session_universes()[0][0],
            Path(os.path.abspath("~someuser")))

    def test_env_equal_to_the_home_default_dedupes_to_one_root(self):
        os.environ["PI_CODING_AGENT_DIR"] = str(self.home / ".pi" / "agent")
        roots = [root for root, _glob in query._pi_session_universes()]
        self.assertEqual(roots, [self._home_sessions()])

    def test_session_dir_equal_to_agent_dir_sessions_dedupes(self):
        os.environ["PI_CODING_AGENT_DIR"] = str(self.agent)
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(self.agent / "sessions")
        roots = [root for root, _glob in query._pi_session_universes()]
        self.assertEqual(roots, [self.agent / "sessions", self._home_sessions()])

    def test_a_root_nested_under_another_is_dropped(self):
        """SESSION_DIR aimed at a per-cwd subdir of AGENT_DIR/sessions: the
        parent's `**` glob already reads it, so keeping both doubles its rows."""
        os.environ["PI_CODING_AGENT_DIR"] = str(self.agent)
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(
            self.agent / "sessions" / "--c--home--proj--")
        roots = [root for root, _glob in query._pi_session_universes()]
        self.assertEqual(roots, [self.agent / "sessions", self._home_sessions()])

    def test_a_root_nested_under_the_home_default_is_dropped(self):
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(
            self._home_sessions() / "sub")
        roots = [root for root, _glob in query._pi_session_universes()]
        self.assertEqual(roots, [self._home_sessions()])

    def test_a_root_containing_the_home_default_absorbs_it(self):
        """The covering root wins even when it is the env one: its glob is a
        superset, so no session file is lost by dropping the home default."""
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(self.home / ".pi")
        roots = [root for root, _glob in query._pi_session_universes()]
        self.assertEqual(roots, [self.home / ".pi"])

    def test_a_sibling_root_sharing_a_name_prefix_is_kept(self):
        """`<agent>/sessionsX` is NOT nested under `<agent>/sessions` — a
        string-prefix containment test would wrongly drop it."""
        os.environ["PI_CODING_AGENT_DIR"] = str(self.agent)
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(self.agent / "sessionsX")
        roots = [root for root, _glob in query._pi_session_universes()]
        self.assertEqual(roots, [self.agent / "sessionsX",
                                 self.agent / "sessions",
                                 self._home_sessions()])

    def test_empty_universe_is_dropped(self):
        os.environ["PI_CODING_AGENT_DIR"] = str(self.agent)
        _write_log(self.agent / "sessions", _SID_ENV)
        self._home_sessions().mkdir(parents=True)  # exists, empty
        self.assertEqual(
            query._apply_pi_sessions_glob(_ANCHOR),
            f"['{self._glob_for(self.agent / 'sessions')}']",
        )

    def test_no_universe_with_logs_names_the_highest_priority_root(self):
        os.environ["PI_CODING_AGENT_DIR"] = str(self.agent)
        out = query._apply_pi_sessions_glob(_ANCHOR)
        self.assertEqual(out, f"['{self._glob_for(self.agent / 'sessions')}']")
        self.assertNotIn("~/.pi", out)

    def test_glob_metacharacters_in_the_env_path_are_escaped(self):
        """`[`/`]` (legal in Windows dir names) must be class-escaped, or the
        existence filter (pathlib, root literal) and DuckDB's glob diverge and
        CREATE VIEW aborts."""
        agent = self.tmp / "agent [test]"
        os.environ["PI_CODING_AGENT_DIR"] = str(agent)
        _write_log(agent / "sessions", _SID_ENV)
        expected = f"{self.tmp.as_posix()}/agent [[]test[]]/sessions/**/*.jsonl"
        self.assertEqual(
            query._apply_pi_sessions_glob(_ANCHOR), f"['{expected}']")


class AnchorContractTest(unittest.TestCase):
    """The literal-replace contract the rewrite depends on."""

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

    def _run_query(self, home: Path, sql: str, agent_dir=None, session_dir=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                            "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR")}
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        if agent_dir is not None:
            env["PI_CODING_AGENT_DIR"] = str(agent_dir)
        if session_dir is not None:
            env["PI_CODING_AGENT_SESSION_DIR"] = str(session_dir)
        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS / "query.py"), "--sql", sql],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    _SIDS = "SELECT DISTINCT session_id FROM pi_record ORDER BY 1"

    def test_reads_both_universes_when_agent_dir_is_set(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, agent = tmp / "home", tmp / "agent"
            _write_log(agent / "sessions", _SID_ENV)
            _write_log(home / ".pi" / "agent" / "sessions", _SID_HOME)
            out = self._run_query(home, self._SIDS, agent_dir=agent)
            self.assertEqual([r[0] for r in out["rows"]], [_SID_ENV, _SID_HOME])

    def test_flat_session_dir_joins_the_union(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, agent, flat = tmp / "home", tmp / "agent", tmp / "flat"
            _write_log(flat, _SID_FLAT)
            _write_log(agent / "sessions", _SID_ENV)
            _write_log(home / ".pi" / "agent" / "sessions", _SID_HOME)
            out = self._run_query(
                home, self._SIDS, agent_dir=agent, session_dir=flat)
            self.assertEqual([r[0] for r in out["rows"]],
                             [_SID_ENV, _SID_HOME, _SID_FLAT])

    def test_duplicate_roots_do_not_double_the_rows(self):
        """SESSION_DIR pointing at AGENT_DIR/sessions is the realistic way to
        list one root twice; without the dedupe DuckDB reads it once per glob."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, agent = tmp / "home", tmp / "agent"
            _write_log(agent / "sessions", _SID_ENV)
            (home / ".pi" / "agent" / "sessions").mkdir(parents=True)
            out = self._run_query(
                home, "SELECT count(*) FROM pi_record",
                agent_dir=agent, session_dir=agent / "sessions")
            self.assertEqual(out["rows"][0][0], 2)  # the fixture's 2 lines

    def test_nested_roots_do_not_double_the_rows(self):
        """The containment case end to end: equality-only dedupe read the
        per-cwd subdir once via each glob and returned every row twice."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, agent = tmp / "home", tmp / "agent"
            per_cwd = agent / "sessions" / "--c--home--proj--"
            _write_log(per_cwd, _SID_ENV)
            (home / ".pi" / "agent" / "sessions").mkdir(parents=True)
            out = self._run_query(
                home, "SELECT count(*) FROM pi_record",
                agent_dir=agent, session_dir=per_cwd)
            self.assertEqual(out["rows"][0][0], 2)  # the fixture's 2 lines

    def test_without_env_only_the_home_universe_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, agent = tmp / "home", tmp / "agent"
            _write_log(home / ".pi" / "agent" / "sessions", _SID_HOME)
            _write_log(agent / "sessions", _SID_ENV)  # must NOT be read
            out = self._run_query(home, self._SIDS)
            self.assertEqual([r[0] for r in out["rows"]], [_SID_HOME])

    def test_env_universe_alone_still_loads_the_views(self):
        """Without the empty-universe filter this dies with IOException."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, agent = tmp / "home", tmp / "agent"
            _write_log(agent / "sessions", _SID_ENV)
            (home / ".pi" / "agent" / "sessions").mkdir(parents=True)
            out = self._run_query(home, self._SIDS, agent_dir=agent)
            self.assertEqual([r[0] for r in out["rows"]], [_SID_ENV])

    def test_bracketed_session_dir_reads_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, agent = tmp / "home", tmp / "agent [test]"
            _write_log(agent / "sessions", _SID_ENV)
            (home / ".pi" / "agent" / "sessions").mkdir(parents=True)
            out = self._run_query(home, self._SIDS, agent_dir=agent)
            self.assertEqual([r[0] for r in out["rows"]], [_SID_ENV])

    def test_tilde_env_value_resolves_to_the_redirected_home(self):
        """End to end proof that `~` is expanded, and against the injected home."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            _write_log(home / "piroot" / "sessions", _SID_ENV)
            (home / ".pi" / "agent" / "sessions").mkdir(parents=True)
            out = self._run_query(home, self._SIDS, agent_dir="~/piroot")
            self.assertEqual([r[0] for r in out["rows"]], [_SID_ENV])


if __name__ == "__main__":
    unittest.main()
