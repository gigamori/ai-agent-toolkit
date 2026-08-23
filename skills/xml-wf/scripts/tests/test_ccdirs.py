import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import parser  # noqa: E402
from wfrun.agents import discover_agents  # noqa: E402
from wfrun.ccdirs import claude_config_dirs  # noqa: E402
from wfrun.claude_cli import CliResult  # noqa: E402
from wfrun.executor import Executor, WorkflowFailure  # noqa: E402

_ENV_KEYS = ("CLAUDE_CONFIG_DIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")


class _TmpHomeCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in ("CLAUDE_CONFIG_DIR", "HOMEDRIVE", "HOMEPATH"):
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.cfg = self.tmp / "cfg"
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        self.default_claude = self.home / ".claude"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


def _write_agent(base: Path, name: str, description: str = "d"):
    d = base / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody-{description}\n",
        encoding="utf-8",
    )


class ClaudeConfigDirsTest(_TmpHomeCase):
    def test_unset_env_is_the_default_only(self):
        self.assertEqual(claude_config_dirs(), [self.default_claude])

    def test_env_set_is_env_then_default(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        self.assertEqual(claude_config_dirs(), [self.cfg, self.default_claude])

    def test_env_pointing_at_default_dedups(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.default_claude).upper()
        self.assertEqual(len(claude_config_dirs()), 1)

    def test_env_value_is_literal_not_expanduser(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "~/cfgtest"
        first = claude_config_dirs()[0]
        self.assertEqual(first, Path(os.path.abspath("~/cfgtest")))
        self.assertNotEqual(first, self.home / "cfgtest")

    def test_relative_env_value_resolves_against_cwd(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "rel-cfg"
        self.assertEqual(claude_config_dirs()[0], Path(os.path.abspath("rel-cfg")))

    def test_blank_env_value_is_treated_as_unset(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "   "
        self.assertEqual(claude_config_dirs(), [self.default_claude])


class DiscoverAgentsPriorityTest(_TmpHomeCase):
    def test_env_unset_matches_pre_existing_behaviour(self):
        _write_agent(self.default_claude, "shared", "default")
        project = self.tmp / "proj"
        agents = discover_agents(project)
        self.assertEqual(agents["shared"].description, "default")

    def test_env_overwrites_default_on_name_collision(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        _write_agent(self.default_claude, "shared", "default")
        _write_agent(self.cfg, "shared", "env")
        project = self.tmp / "proj"
        agents = discover_agents(project)
        self.assertEqual(agents["shared"].description, "env")

    def test_project_overwrites_both_env_and_default(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        _write_agent(self.default_claude, "shared", "default")
        _write_agent(self.cfg, "shared", "env")
        project = self.tmp / "proj"
        _write_agent(project / ".claude", "shared", "project")
        agents = discover_agents(project)
        self.assertEqual(agents["shared"].description, "project")

    def test_env_only_agent_is_still_discovered(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        _write_agent(self.cfg, "env-only", "e")
        project = self.tmp / "proj"
        agents = discover_agents(project)
        self.assertIn("env-only", agents)


def _wf():
    return parser.parse_string(
        '<workflow name="t" version="2" max="10">'
        '<step id="s1" role="w"><task>x</task></step></workflow>')


class BaseDirGuardTest(_TmpHomeCase):
    def _fake_claude(self, *a, **k):
        return CliResult(ok=True, text="ok", cost_usd=0.0)

    def test_default_claude_dir_still_rejected_with_env_set(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        with tempfile.TemporaryDirectory() as run_dir:
            with self.assertRaises(WorkflowFailure):
                Executor(_wf(), {}, run_dir, base_dir=self.default_claude,
                         run_claude=self._fake_claude)

    def test_env_config_dir_is_also_rejected(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        with tempfile.TemporaryDirectory() as run_dir:
            with self.assertRaises(WorkflowFailure) as ctx:
                Executor(_wf(), {}, run_dir, base_dir=self.cfg,
                         run_claude=self._fake_claude)
            self.assertIn(str(self.cfg.resolve()), str(ctx.exception))

    def test_normal_dir_is_accepted_even_with_env_set(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        normal = self.tmp / "normal-project"
        normal.mkdir()
        with tempfile.TemporaryDirectory() as run_dir:
            Executor(_wf(), {}, run_dir, base_dir=normal,
                     run_claude=self._fake_claude)

    def test_error_message_names_the_matched_dir(self):
        with tempfile.TemporaryDirectory() as run_dir:
            with self.assertRaises(WorkflowFailure) as ctx:
                Executor(_wf(), {}, run_dir, base_dir=self.default_claude,
                         run_claude=self._fake_claude)
            self.assertIn(str(self.default_claude.resolve()), str(ctx.exception))


class MainLayerBaseDirGuardTest(_TmpHomeCase):
    def test_env_dir_rejected_by_the_a_layer_copy_too(self):
        from wfrun.__main__ import _check_base_dir
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        msg = _check_base_dir(self.cfg)
        self.assertIsNotNone(msg)
        self.assertIn(str(self.cfg.resolve()), msg)

    def test_normal_dir_passes_the_a_layer_copy(self):
        from wfrun.__main__ import _check_base_dir
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        normal = self.tmp / "normal-project"
        normal.mkdir()
        self.assertIsNone(_check_base_dir(normal))


class SettingsGuardTest(_TmpHomeCase):
    def test_marker_only_in_env_settings_suppresses_the_warning(self):
        from wfrun.__main__ import _warn_if_no_llm_guard, LLM_GUARD_MARKER
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        self.cfg.mkdir(parents=True, exist_ok=True)
        (self.cfg / "settings.json").write_text(
            f'{{"hooks": "{LLM_GUARD_MARKER}"}}', encoding="utf-8")

        cwd = Path.cwd()
        workdir = self.tmp / "workdir"
        workdir.mkdir()
        os.chdir(workdir)
        try:
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                _warn_if_no_llm_guard()
            self.assertNotIn(LLM_GUARD_MARKER, buf.getvalue())
            self.assertEqual(buf.getvalue(), "")
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
