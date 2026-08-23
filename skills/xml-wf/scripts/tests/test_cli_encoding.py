import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import claude_cli  # noqa: E402

_BAD_BYTE = b"\x8f"


_PINNED_KWARGS = ("this launcher call site must pin UTF-8 decoding: dropping "
                  "encoding=/errors= changes behaviour only on a host whose "
                  "locale codec is not UTF-8")


def _emit_bytes_script(payload: bytes) -> str:
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
    def test_tree_kill_launcher_replaces_undecodable_bytes(self):
        envelope = json.dumps({"result": "PLACEHOLDER", "is_error": False,
                               "num_turns": 1}).encode("utf-8")
        payload = envelope.replace(b"PLACEHOLDER", b"caf" + _BAD_BYTE)

        proc = claude_cli._run_with_tree_kill(
            [sys.executable, "-c", _emit_bytes_script(payload)],
            "", 30, None)

        self.assertIsInstance(
            proc.stdout, str,
            "a strict decode kills the reader thread and leaves stdout None, "
            "which classify_result then hands to json.loads")
        self.assertIn("�", proc.stdout)

    def test_classification_survives_a_replaced_byte(self):
        envelope = json.dumps({"result": "PLACEHOLDER", "is_error": False,
                               "num_turns": 1}).encode("utf-8")
        payload = envelope.replace(b"PLACEHOLDER", b"caf" + _BAD_BYTE)

        proc = claude_cli._run_with_tree_kill(
            [sys.executable, "-c", _emit_bytes_script(payload)],
            "", 30, None)
        res = claude_cli.classify_result(proc.returncode, proc.stdout, proc.stderr)

        self.assertTrue(res.ok, res.error)
        self.assertEqual(
            res.text, "caf�",
            "the envelope is ASCII, so a replaced byte costs body text, "
            "never the verdict")
        self.assertEqual(res.num_turns, 1)

    def test_valid_utf8_is_untouched(self):
        envelope = json.dumps({"result": "日本語 ok", "is_error": False},
                              ensure_ascii=False).encode("utf-8")

        proc = claude_cli._run_with_tree_kill(
            [sys.executable, "-c", _emit_bytes_script(envelope)], "", 30, None)
        res = claude_cli.classify_result(proc.returncode, proc.stdout, proc.stderr)

        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.text, "日本語 ok")
        self.assertNotIn("�", proc.stdout)


class EncodeSideTests(unittest.TestCase):
    def test_prompt_reaches_child_as_utf8(self):
        prompt = "日本語のプロンプト"

        proc = claude_cli._run_with_tree_kill(
            [sys.executable, "-c", _ECHO_STDIN_HEX], prompt, 30, None)

        self.assertEqual(bytes.fromhex(proc.stdout.strip()),
                         prompt.encode("utf-8"))

    def test_prompt_with_character_absent_from_cp932_does_not_raise(self):
        prompt = "ship it 🚀"

        proc = claude_cli._run_with_tree_kill(
            [sys.executable, "-c", _ECHO_STDIN_HEX], prompt, 30, None)

        self.assertEqual(bytes.fromhex(proc.stdout.strip()),
                         prompt.encode("utf-8"))


class LaunchSubprocessRunTests(unittest.TestCase):
    def test_launch_branch_kwargs_round_trip(self):
        prompt = "日本語 🚀"
        proc = subprocess.run(
            [sys.executable, "-c", _ECHO_STDIN_HEX],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(bytes.fromhex(proc.stdout.strip()),
                         prompt.encode("utf-8"))

    def test_launch_call_site_passes_utf8_kwargs(self):
        src = Path(claude_cli.__file__).read_text(encoding="utf-8")
        marker = "proc = subprocess.run(cmd, input=prompt"
        idx = src.index(marker)
        call = src[idx:idx + 300]
        self.assertIn('encoding="utf-8"', call, _PINNED_KWARGS)
        self.assertIn('errors="replace"', call, _PINNED_KWARGS)


class ProbeAndPiCallSiteTests(unittest.TestCase):
    def test_capability_probe_passes_utf8_kwargs(self):
        src = Path(claude_cli.__file__).read_text(encoding="utf-8")
        idx = src.index("probe = subprocess.run(")
        call = src[idx:idx + 300]
        self.assertIn('encoding="utf-8"', call, _PINNED_KWARGS)
        self.assertIn('errors="replace"', call, _PINNED_KWARGS)

    def test_pi_ask_llm_passes_utf8_kwargs(self):
        from wfrun import pi_cli
        src = Path(pi_cli.__file__).read_text(encoding="utf-8")
        idx = src.index("cmd, stdin=subprocess.DEVNULL")
        call = src[idx:idx + 300]
        self.assertIn('encoding="utf-8"', call, _PINNED_KWARGS)
        self.assertIn('errors="replace"', call, _PINNED_KWARGS)

    def test_pi_model_catalog_passes_utf8_kwargs(self):
        from wfrun import pi_cli
        src = Path(pi_cli.__file__).read_text(encoding="utf-8")
        idx = src.index('launcher + ["--list-models"]')
        call = src[idx:idx + 300]
        self.assertIn('encoding="utf-8"', call, _PINNED_KWARGS)
        self.assertIn('errors="replace"', call, _PINNED_KWARGS)


if __name__ == "__main__":
    unittest.main()
