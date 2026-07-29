"""Tests: revert_cc_log_extract session-jsonl resolution under CLAUDE_CONFIG_DIR.

Run: uv run --with duckdb python -m unittest discover -s tests -v
(duckdb is imported by the module under test, not by these tests.)

Covers the revert half of
`_projects/llm-wiki/project-notes/specs/cc-config-dir-skills.md` (C5):
  - unset --projects-dir searches the roots union `[$CLAUDE_CONFIG_DIR,
    ~/.claude]`, sid lookup first-wins (env universe priority) and the
    mtime-latest fallback spanning ALL roots;
  - AC-A4: an explicit --projects-dir is used verbatim and ALONE — the env var is
    ignored, exactly as before this support existed;
  - AC-A3: the env value is literal (no expanduser), cwd-relative.

Hermetic: HOME/USERPROFILE are redirected to a tmp dir for every test, so the
real ~/.claude is never searched. Pure-Python `expanduser` does observe an
in-process env change (unlike DuckDB), so no subprocess is needed here.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import revert_cc_log_extract as rx  # noqa: E402

_ENV_KEYS = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
             "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")


def _write_session(projects_root: Path, slug: str, sid: str, mtime: float | None = None):
    d = projects_root / slug
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text('{"type": "user"}\n', encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


class _TmpHomeCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        # CLAUDE_CODE_SESSION_ID must be cleared too: these tests may run INSIDE
        # a live CC session where it is set to the real sid, which would make
        # every sid-less resolve search the tmp roots for that sid instead of
        # exercising the mtime-latest fallback.
        for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_ID",
                  "CLAUDE_SESSION_ID", "HOMEDRIVE", "HOMEPATH"):
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.cfg = self.tmp / "cfg"
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        self.default_projects = self.home / ".claude" / "projects"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class RootsTest(_TmpHomeCase):
    def test_unset_env_is_the_default_universe_only(self):
        self.assertEqual(rx.cc_projects_roots(), [self.default_projects])

    def test_env_universe_comes_first(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        self.assertEqual(
            rx.cc_projects_roots(),
            [self.cfg / "projects", self.default_projects],
        )

    def test_env_pointing_at_the_default_dir_dedups(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.home / ".claude")
        self.assertEqual(len(rx.cc_projects_roots()), 1)

    def test_env_value_is_literal_not_expanduser(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "~/cfgtest"
        first = rx.cc_projects_roots()[0]
        self.assertEqual(first, Path(os.path.abspath("~/cfgtest")) / "projects")
        self.assertNotEqual(first, self.home / "cfgtest" / "projects")


class ResolveTest(_TmpHomeCase):
    def test_sid_found_in_the_default_universe(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        (self.cfg / "projects").mkdir(parents=True)
        want = _write_session(self.default_projects, "p-def", "sid-a")
        self.assertEqual(rx.resolve_session_jsonl("sid-a", None), want)

    def test_sid_in_both_universes_resolves_to_the_env_one(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        want = _write_session(self.cfg / "projects", "p-env", "sid-a")
        _write_session(self.default_projects, "p-def", "sid-a")
        self.assertEqual(rx.resolve_session_jsonl("sid-a", None), want)

    def test_sid_comes_from_the_env_var_when_no_arg(self):
        """The env var is $CLAUDE_CODE_SESSION_ID — the name CC actually sets.

        The previously-read $CLAUDE_SESSION_ID is UNSET in a live CC session
        (probed 2026-07-29), so with the old name this branch never fired.
        """
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-env-var"
        want = _write_session(self.cfg / "projects", "p-env", "sid-env-var")
        self.assertEqual(rx.resolve_session_jsonl(None, None), want)

    def test_the_stale_env_var_name_is_ignored(self):
        """$CLAUDE_SESSION_ID (a prompt-template name, not an OS env var) must
        not steer resolution — matching ingest_driver's D5 fix, no legacy read."""
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        os.environ["CLAUDE_SESSION_ID"] = "sid-stale-name"
        _write_session(self.cfg / "projects", "p-env", "sid-stale-name",
                       mtime=1_000_000)
        newest = _write_session(self.cfg / "projects", "p-env", "sid-newest",
                                mtime=2_000_000)
        # falls through to mtime-latest, NOT the stale-name sid
        self.assertEqual(rx.resolve_session_jsonl(None, None), newest)

    def test_mtime_latest_spans_all_roots(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        _write_session(self.cfg / "projects", "p-env", "sid-old", mtime=1_000_000)
        want = _write_session(
            self.default_projects, "p-def", "sid-new", mtime=2_000_000)
        self.assertEqual(rx.resolve_session_jsonl(None, None), want)

    def test_unresolvable_sid_returns_none(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        _write_session(self.cfg / "projects", "p-env", "other")
        self.assertIsNone(rx.resolve_session_jsonl("missing", None))

    def test_no_root_exists_returns_none(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        self.assertIsNone(rx.resolve_session_jsonl(None, None))


class ExplicitProjectsDirTest(_TmpHomeCase):
    """AC-A4: an explicit --projects-dir keeps its pre-existing behaviour."""

    def test_explicit_dir_ignores_the_env_universe(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        _write_session(self.cfg / "projects", "p-env", "sid-a")
        want = _write_session(self.tmp / "explicit", "p-x", "sid-a")
        self.assertEqual(
            rx.resolve_session_jsonl("sid-a", str(self.tmp / "explicit")), want)

    def test_explicit_dir_without_the_sid_returns_none_not_a_fallback(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        _write_session(self.cfg / "projects", "p-env", "sid-a")
        (self.tmp / "explicit").mkdir()
        self.assertIsNone(
            rx.resolve_session_jsonl("sid-a", str(self.tmp / "explicit")))

    def test_explicit_dir_is_still_expanduser_ed(self):
        want = _write_session(self.home / ".claude" / "projects", "p-def", "sid-a")
        self.assertEqual(
            rx.resolve_session_jsonl("sid-a", "~/.claude/projects"), want)


if __name__ == "__main__":
    unittest.main()
