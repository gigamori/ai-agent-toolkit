"""Tests: build_tools_yaml.py's default output path (PI_CODING_AGENT_DIR support).

Run: uv run --with pyyaml python -m unittest discover -s tests -v

Covers WP-D of
`pi/_projects/pi-extensions-dev/project-notes/specs/agent-dir-env-support-design.md`:
  - unset env => the historic `~/.pi/agent/tools.yaml` (regression: zero change
    for every existing user);
  - `$PI_CODING_AGENT_DIR` moves the registry to `<agent-dir>/tools.yaml`, a
    SINGLE dir -- tools.yaml is a config file pi reads from one global path, so
    there is no union of candidates to fall back on;
  - `~` IS expanded (pi semantics, the opposite of CLAUDE_CONFIG_DIR), but only
    the forms pi itself expands -- `~user` stays literal;
  - a blank/whitespace value counts as unset;
  - an explicit `output_path` keeps its original handling and ignores the env,
    and a BLANK one is rejected rather than quietly redirected to the default;
  - the frontmatter declares no static `default` for `output_path`, because
    `_tool.args()` would inject it and shadow the resolution above.

Hermetic: HOME *and* USERPROFILE are redirected to a tmp dir, so the real
`~/.pi/agent/tools.yaml` is never written. USERPROFILE is the one that matters
on Windows (`ntpath.expanduser` reads it first).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_tools_yaml  # noqa: E402

_SCRIPTS = Path(__file__).resolve().parents[1]
_BUILD = _SCRIPTS / "build_tools_yaml.py"
_ENV_KEYS = ("PI_CODING_AGENT_DIR", "HOME", "USERPROFILE")

_DEMO_SCRIPT = """#!/usr/bin/env python3
# ---
# name: demo
# description: fixture tool
# args:
#   type: object
#   required: [who]
#   properties:
#     who:
#       type: string
#       description: name
# ---
print("hi")
"""


class _EnvSandbox(unittest.TestCase):
    """Base: a tmp home on both env vars, PI_CODING_AGENT_DIR cleared."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ.pop("PI_CODING_AGENT_DIR", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def assertSamePath(self, actual: Path, expected: Path) -> None:
        self.assertEqual(
            os.path.normcase(str(Path(actual).resolve())),
            os.path.normcase(str(Path(expected).resolve())),
        )


class DefaultOutputPathTest(_EnvSandbox):
    """Unit-level: which path `_default_output_path()` resolves, given env."""

    def test_unset_env_keeps_the_home_default(self):
        self.assertSamePath(
            build_tools_yaml._default_output_path(),
            self.home / ".pi" / "agent" / "tools.yaml",
        )

    def test_absolute_env_moves_the_registry(self):
        agent = self.tmp / "isolated" / "agent"
        os.environ["PI_CODING_AGENT_DIR"] = str(agent)
        self.assertSamePath(
            build_tools_yaml._default_output_path(), agent / "tools.yaml"
        )

    def test_tilde_prefixed_env_is_expanded(self):
        os.environ["PI_CODING_AGENT_DIR"] = "~/moved/agent"
        self.assertSamePath(
            build_tools_yaml._default_output_path(),
            self.home / "moved" / "agent" / "tools.yaml",
        )

    def test_bare_tilde_env_is_the_home_itself(self):
        os.environ["PI_CODING_AGENT_DIR"] = "~"
        self.assertSamePath(
            build_tools_yaml._default_output_path(), self.home / "tools.yaml"
        )

    @unittest.skipUnless(os.name == "nt", "pi expands `~\\` on Windows only")
    def test_backslash_tilde_env_is_expanded_on_windows(self):
        os.environ["PI_CODING_AGENT_DIR"] = "~\\moved"
        self.assertSamePath(
            build_tools_yaml._default_output_path(), self.home / "moved" / "tools.yaml"
        )

    def test_user_form_tilde_is_left_alone(self):
        """`~someone` is NOT a home reference to pi, so it must not become one.

        `os.path.expanduser` would resolve it; pi's `normalizePath` does not.
        """
        os.environ["PI_CODING_AGENT_DIR"] = "~nobody/agent"
        resolved = build_tools_yaml._default_output_path()
        self.assertNotIn(os.path.normcase(str(self.home)), os.path.normcase(str(resolved)))
        self.assertSamePath(resolved, Path("~nobody/agent/tools.yaml").resolve())

    def test_blank_env_counts_as_unset(self):
        for blank in ("", "   ", "\t"):
            with self.subTest(value=repr(blank)):
                os.environ["PI_CODING_AGENT_DIR"] = blank
                self.assertSamePath(
                    build_tools_yaml._default_output_path(),
                    self.home / ".pi" / "agent" / "tools.yaml",
                )

    def test_surrounding_whitespace_is_trimmed(self):
        agent = self.tmp / "isolated" / "agent"
        os.environ["PI_CODING_AGENT_DIR"] = f"  {agent}  "
        self.assertSamePath(
            build_tools_yaml._default_output_path(), agent / "tools.yaml"
        )


class FrontmatterContractTest(unittest.TestCase):
    """The static `default:` must stay absent, or `_tool` shadows the env."""

    def test_output_path_declares_no_static_default(self):
        fm = build_tools_yaml._extract_frontmatter(
            _BUILD.read_text(encoding="utf-8")
        )
        prop = fm["args"]["properties"]["output_path"]
        self.assertNotIn(
            "default",
            prop,
            "`_tool.args()` injects frontmatter defaults, so a `default:` here "
            "would always populate output_path and _default_output_path() "
            "would never run",
        )
        self.assertIn("PI_CODING_AGENT_DIR", prop["description"])

    def test_input_dir_is_still_required(self):
        fm = build_tools_yaml._extract_frontmatter(
            _BUILD.read_text(encoding="utf-8")
        )
        self.assertEqual(fm["args"]["required"], ["input_dir"])


class EndToEndTest(_EnvSandbox):
    """Spawns the real script: where does the file actually land?"""

    def setUp(self):
        super().setUp()
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "demo.py").write_text(_DEMO_SCRIPT, encoding="utf-8")

    def _spawn(
        self, *extra_args: str, agent_dir: "str | None" = None
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        if agent_dir is None:
            env.pop("PI_CODING_AGENT_DIR", None)
        else:
            env["PI_CODING_AGENT_DIR"] = agent_dir
        return subprocess.run(
            [sys.executable, str(_BUILD), "--input-dir", str(self.src), *extra_args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            stdin=subprocess.DEVNULL,
        )

    def _run(self, *extra_args: str, agent_dir: "str | None" = None) -> str:
        proc = self._spawn(*extra_args, agent_dir=agent_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_unset_env_writes_the_home_default(self):
        out = self._run()
        written = self.home / ".pi" / "agent" / "tools.yaml"
        self.assertTrue(written.is_file(), out)
        self.assertIn("demo", written.read_text(encoding="utf-8"))

    def test_env_writes_into_the_isolated_agent_dir_only(self):
        agent = self.tmp / "isolated" / "agent"
        self._run(agent_dir=str(agent))
        self.assertTrue((agent / "tools.yaml").is_file())
        self.assertFalse(
            (self.home / ".pi").exists(),
            "the home default must not be written once the env moves the store",
        )

    def test_explicit_output_path_ignores_the_env(self):
        agent = self.tmp / "isolated" / "agent"
        explicit = self.tmp / "explicit" / "tools.yaml"
        self._run("--output-path", str(explicit), agent_dir=str(agent))
        self.assertTrue(explicit.is_file())
        self.assertFalse(agent.exists())
        self.assertFalse((self.home / ".pi").exists())

    def test_explicit_output_path_still_expands_tilde(self):
        self._run("--output-path", "~/custom/tools.yaml", agent_dir=str(self.tmp / "iso"))
        self.assertTrue((self.home / "custom" / "tools.yaml").is_file())

    def test_blank_output_path_is_rejected_not_defaulted(self):
        """A caller who passed the flag must not be silently redirected.

        Falling through to `<agent-dir>/tools.yaml` here would write the
        registry somewhere the caller never named -- the same silent-misplace
        failure the env resolution exists to prevent.
        """
        agent = self.tmp / "isolated" / "agent"
        for blank in ("", "   "):
            with self.subTest(value=repr(blank)):
                proc = self._spawn(
                    "--output-path", blank, agent_dir=str(agent)
                )
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("output_path is blank", proc.stderr)
                self.assertFalse(agent.exists())
                self.assertFalse((self.home / ".pi").exists())

    def test_blank_output_path_from_stdin_json_is_rejected(self):
        """argv is not the only input route -- `_tool.args()` also reads stdin."""
        env = dict(os.environ)
        env["PI_CODING_AGENT_DIR"] = str(self.tmp / "isolated" / "agent")
        proc = subprocess.run(
            [sys.executable, str(_BUILD)],
            input='{"input_dir": %s, "output_path": ""}' % json.dumps(str(self.src)),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("output_path is blank", proc.stderr)


if __name__ == "__main__":
    unittest.main()
