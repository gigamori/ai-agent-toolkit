"""Tests for the UTF-8 subprocess contract (cp932-decode-fix-design.md).

These spawn REAL child processes. Mocking `subprocess.run` here would bypass
the exact layer under test -- the decode/encode that subprocess performs
around the pipe -- so every case below drives `sys.executable` running an
inline script and asserts on what actually crossed the pipe.

The child writes/reads via `sys.stdout.buffer` / `sys.stdin.buffer`, so the
child's own locale never enters the picture; only the parent's decoding of
those bytes is under test.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import claude_cli  # noqa: E402

# A byte that is a valid cp932 lead byte but invalid UTF-8 on its own. Under
# the pre-fix code (locale decode on JA Windows) this decoded to garbage; with
# errors="strict" it kills subprocess's reader thread outright.
_BAD_BYTE = b"\x8f"


def _emit_bytes_script(payload: bytes) -> str:
    """Child that writes exactly `payload` to stdout as raw bytes."""
    return ("import sys;"
            f"sys.stdout.buffer.write({payload!r});"
            "sys.stdout.buffer.flush()")


_ECHO_STDIN_HEX = (
    "import sys;"
    "data = sys.stdin.buffer.read();"
    "sys.stdout.buffer.write(data.hex().encode('ascii'));"
    "sys.stdout.buffer.flush()"
)


class DecodeSideTests(unittest.TestCase):
    """T1: an undecodable reply byte must not kill the run."""

    def test_tree_kill_launcher_replaces_undecodable_bytes(self):
        envelope = json.dumps({"result": "PLACEHOLDER", "is_error": False,
                               "num_turns": 1}).encode("utf-8")
        payload = envelope.replace(b"PLACEHOLDER", b"caf" + _BAD_BYTE)

        proc = claude_cli._run_with_tree_kill(
            [sys.executable, "-c", _emit_bytes_script(payload)],
            "", 30, None)

        # The run survived and stdout is a str, not None: the pre-fix failure
        # was a dead reader thread handing classify_result a None to json.loads.
        self.assertIsInstance(proc.stdout, str)
        self.assertIn("�", proc.stdout)

    def test_classification_survives_a_replaced_byte(self):
        """The envelope is ASCII, so replacement costs body text, not verdict."""
        envelope = json.dumps({"result": "PLACEHOLDER", "is_error": False,
                               "num_turns": 1}).encode("utf-8")
        payload = envelope.replace(b"PLACEHOLDER", b"caf" + _BAD_BYTE)

        proc = claude_cli._run_with_tree_kill(
            [sys.executable, "-c", _emit_bytes_script(payload)],
            "", 30, None)
        res = claude_cli.classify_result(proc.returncode, proc.stdout, proc.stderr)

        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.text, "caf�")
        self.assertEqual(res.num_turns, 1)

    def test_valid_utf8_is_untouched(self):
        """errors="replace" is a no-op on well-formed UTF-8 -- no lossy path
        for the overwhelmingly common case."""
        envelope = json.dumps({"result": "日本語 ok", "is_error": False},
                              ensure_ascii=False).encode("utf-8")

        proc = claude_cli._run_with_tree_kill(
            [sys.executable, "-c", _emit_bytes_script(envelope)], "", 30, None)
        res = claude_cli.classify_result(proc.returncode, proc.stdout, proc.stderr)

        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.text, "日本語 ok")
        self.assertNotIn("�", proc.stdout)


class EncodeSideTests(unittest.TestCase):
    """T2 (review finding F1): `encoding=` governs stdin too, so the prompt
    must reach the child as UTF-8. Pre-fix this was the locale codec, which
    both raised on characters cp932 lacks and mismatched the child."""

    def test_prompt_reaches_child_as_utf8(self):
        prompt = "日本語のプロンプト"

        proc = claude_cli._run_with_tree_kill(
            [sys.executable, "-c", _ECHO_STDIN_HEX], prompt, 30, None)

        self.assertEqual(bytes.fromhex(proc.stdout.strip()),
                         prompt.encode("utf-8"))

    def test_prompt_with_character_absent_from_cp932_does_not_raise(self):
        """An emoji has no cp932 representation: the pre-fix encode side raised
        UnicodeEncodeError before the child ever saw the prompt."""
        prompt = "ship it 🚀"

        proc = claude_cli._run_with_tree_kill(
            [sys.executable, "-c", _ECHO_STDIN_HEX], prompt, 30, None)

        self.assertEqual(bytes.fromhex(proc.stdout.strip()),
                         prompt.encode("utf-8"))


class LaunchSubprocessRunTests(unittest.TestCase):
    """The non-tree-kill branch of _launch is a separate subprocess.run call
    site with its own kwargs; assert the same contract holds there. Driven
    through the real subprocess.run with argv the stub understands."""

    def test_launch_branch_kwargs_round_trip(self):
        prompt = "日本語 🚀"
        proc = subprocess.run(
            [sys.executable, "-c", _ECHO_STDIN_HEX],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(bytes.fromhex(proc.stdout.strip()),
                         prompt.encode("utf-8"))

    def test_launch_call_site_passes_utf8_kwargs(self):
        """Guards the kwargs themselves: removing encoding=/errors= from the
        _launch call site must fail a test, not just change behaviour on a
        JA-Windows host that CI may not have."""
        src = Path(claude_cli.__file__).read_text(encoding="utf-8")
        marker = "proc = subprocess.run(cmd, input=prompt"
        idx = src.index(marker)
        call = src[idx:idx + 300]
        self.assertIn('encoding="utf-8"', call)
        self.assertIn('errors="replace"', call)


class ProbeAndPiCallSiteTests(unittest.TestCase):
    """The remaining launcher call sites carry the same contract (site 3 in
    claude_cli, site 4 in pi_cli). Source-level assertions for the same reason
    as above: their behaviour only diverges on a non-UTF-8-locale host."""

    def test_capability_probe_passes_utf8_kwargs(self):
        src = Path(claude_cli.__file__).read_text(encoding="utf-8")
        idx = src.index("probe = subprocess.run(")
        call = src[idx:idx + 300]
        self.assertIn('encoding="utf-8"', call)
        self.assertIn('errors="replace"', call)

    def test_pi_ask_llm_passes_utf8_kwargs(self):
        from wfrun import pi_cli
        src = Path(pi_cli.__file__).read_text(encoding="utf-8")
        idx = src.index("cmd, stdin=subprocess.DEVNULL")
        call = src[idx:idx + 300]
        self.assertIn('encoding="utf-8"', call)
        self.assertIn('errors="replace"', call)

    def test_pi_model_catalog_passes_utf8_kwargs(self):
        from wfrun import pi_cli
        src = Path(pi_cli.__file__).read_text(encoding="utf-8")
        idx = src.index('launcher + ["--list-models"]')
        call = src[idx:idx + 300]
        self.assertIn('encoding="utf-8"', call)
        self.assertIn('errors="replace"', call)


if __name__ == "__main__":
    unittest.main()
