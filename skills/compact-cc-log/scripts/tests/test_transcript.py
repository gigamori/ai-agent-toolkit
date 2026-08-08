"""
Tests: transcript.py session resolution + transcript assembly.

Run: uv run --with duckdb python -m unittest discover -s tests -v

Hermetic: subprocess spawn with HOME/USERPROFILE redirected to a tmp dir, same
pattern as skills/inspect-cc-log/scripts/tests/test_config_dir.py
(EndToEndTest._run_query) -- DuckDB does not observe an in-process env change,
so the real ~/.claude is never read.

Covers:
  - title resolution: ok / candidates / not_found
  - --current cut rule (invoking turn excluded)
  - empty transcript
  - tool-call summary line
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]


def _write_session(home: Path, proj: str, sid: str, records: list[dict]) -> None:
    d = home / ".claude" / "projects" / proj
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _user_rec(sid, uuid, parent, ts, text):
    return {
        "sessionId": sid, "type": "user", "uuid": uuid, "parentUuid": parent,
        "timestamp": ts, "message": {"role": "user", "content": text},
    }


def _assistant_text_rec(sid, uuid, parent, ts, text):
    return {
        "sessionId": sid, "type": "assistant", "uuid": uuid, "parentUuid": parent,
        "timestamp": ts,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _title_rec(sid, title):
    return {"sessionId": sid, "type": "last-prompt", "lastPrompt": title}


class TranscriptEndToEndTest(unittest.TestCase):

    def _run(self, home: Path, args: list[str], current_sid: "str | None" = None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                             "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_ID")}
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        if current_sid is not None:
            env["CLAUDE_CODE_SESSION_ID"] = current_sid
        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS / "transcript.py"), *args],
            capture_output=True, text=True, env=env, encoding="utf-8",
        )
        return proc, json.loads(proc.stdout)

    # --- title resolution ---------------------------------------------------

    def test_title_resolves_to_one_session(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            _write_session(home, "p", "sid-ok", [
                _user_rec("sid-ok", "u1", None, "2026-01-01T00:00:00Z", "hello"),
                _title_rec("sid-ok", "unique title"),
            ])
            out_path = tmp / "out.md"
            proc, result = self._run(home, ["--title", "unique title", "--out", str(out_path)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["session_id"], "sid-ok")
            self.assertIn("hello", out_path.read_text(encoding="utf-8"))

    def test_title_matching_two_sessions_returns_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            for sid in ("sid-b1", "sid-b2"):
                _write_session(home, "p", sid, [
                    _user_rec(sid, "u1", None, "2026-01-01T00:00:00Z", "hi"),
                    _title_rec(sid, "dup title"),
                ])
            out_path = tmp / "out.md"
            proc, result = self._run(home, ["--title", "dup title", "--out", str(out_path)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(result["status"], "candidates")
            self.assertEqual(
                {s["session_id"] for s in result["sessions"]}, {"sid-b1", "sid-b2"})
            self.assertFalse(out_path.exists())

    def test_title_matching_nothing_returns_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            _write_session(home, "p", "sid-c", [
                _user_rec("sid-c", "u1", None, "2026-01-01T00:00:00Z", "hi"),
                _title_rec("sid-c", "some other title"),
            ])
            out_path = tmp / "out.md"
            proc, result = self._run(home, ["--title", "nonexistent", "--out", str(out_path)])
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(result["status"], "not_found")
            self.assertFalse(out_path.exists())

    # --- --current cut rule ---------------------------------------------------

    def test_current_excludes_the_invoking_turn(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            sid = "sid-current"
            _write_session(home, "p", sid, [
                _user_rec(sid, "u1", None, "2026-01-01T00:00:00Z", "before"),
                _assistant_text_rec(sid, "a1", "u1", "2026-01-01T00:00:01Z", "reply"),
                _user_rec(sid, "u2", "a1", "2026-01-01T00:00:02Z", "trigger"),
                {
                    "sessionId": sid, "type": "assistant", "uuid": "a2", "parentUuid": "u2",
                    "timestamp": "2026-01-01T00:00:03Z",
                    "message": {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "toolu_cur", "name": "Bash",
                         "input": {"command": "echo current-turn"}},
                    ]},
                },
            ])
            out_path = tmp / "out.md"
            proc, result = self._run(
                home, ["--current", "--out", str(out_path)], current_sid=sid)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["n_msg"], 2)
            self.assertEqual(result["n_tool"], 0)
            body = out_path.read_text(encoding="utf-8")
            self.assertIn("before", body)
            self.assertIn("reply", body)
            self.assertNotIn("trigger", body)
            self.assertNotIn("current-turn", body)

    def test_current_boundary_ignores_tool_result_records(self):
        """Regression: a tool_result is ALSO a type='user'/role='user' record.
        The cut boundary must come from cc_block (block_type='text'), not
        cc_event's type/role alone, or a tool_result timestamped after the
        last real prompt drags the whole in-progress turn back into scope.
        Found 2026-08-08 running --current against this skill's own live
        session during an execute-mode turn with many tool calls.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            sid = "sid-toolresult-boundary"
            _write_session(home, "p", sid, [
                _user_rec(sid, "u0", None, "2026-01-01T00:00:00Z", "earlier content"),
                _assistant_text_rec(sid, "a0", "u0", "2026-01-01T00:00:01Z", "ack"),
                _user_rec(sid, "u1", "a0", "2026-01-01T00:01:00Z", "real last prompt"),
                {
                    "sessionId": sid, "type": "assistant", "uuid": "a1", "parentUuid": "u1",
                    "timestamp": "2026-01-01T00:01:01Z",
                    "message": {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "toolu_x", "name": "Bash",
                         "input": {"command": "echo work"}},
                    ]},
                },
                {
                    # tool_result: type=user, role=user, but NOT block_type=text.
                    # Timestamped well after the real last prompt -- must not
                    # become the cut boundary.
                    "sessionId": sid, "type": "user", "uuid": "u2", "parentUuid": "a1",
                    "timestamp": "2026-01-01T00:05:00Z",
                    "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_x",
                         "content": "work", "is_error": False},
                    ]},
                    "toolUseResult": {"stdout": "work", "stderr": "", "interrupted": False},
                },
            ])
            out_path = tmp / "out.md"
            proc, result = self._run(
                home, ["--current", "--out", str(out_path)], current_sid=sid)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cut_boundary_ts"], "2026-01-01 00:01:00")
            self.assertEqual(result["n_msg"], 2)
            self.assertEqual(result["n_tool"], 0)
            body = out_path.read_text(encoding="utf-8")
            self.assertIn("earlier content", body)
            self.assertIn("ack", body)
            self.assertNotIn("real last prompt", body)
            self.assertNotIn("work", body)

    def test_current_without_session_id_env_errors(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            out_path = tmp / "out.md"
            proc, result = self._run(home, ["--current", "--out", str(out_path)])
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(result["status"], "error")

    # --- empty transcript ---------------------------------------------------

    def test_unresolved_session_id_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            # A decoy session keeps the projects glob non-empty (an empty glob
            # aborts CREATE VIEW in DuckDB) while sid-missing itself resolves
            # to nothing.
            _write_session(home, "p", "sid-decoy", [
                _user_rec("sid-decoy", "u1", None, "2026-01-01T00:00:00Z", "unrelated"),
            ])
            out_path = tmp / "out.md"
            proc, result = self._run(
                home, ["--session-id", "sid-missing", "--out", str(out_path)])
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(result["status"], "empty")
            self.assertFalse(out_path.exists())

    def test_session_with_only_thinking_block_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            sid = "sid-thinking-only"
            _write_session(home, "p", sid, [
                {
                    "sessionId": sid, "type": "assistant", "uuid": "a1", "parentUuid": None,
                    "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "internal only"},
                    ]},
                },
            ])
            out_path = tmp / "out.md"
            proc, result = self._run(
                home, ["--session-id", sid, "--out", str(out_path)])
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(result["status"], "empty")
            self.assertFalse(out_path.exists())

    # --- tool-call summary line ---------------------------------------------------

    def test_tool_call_renders_a_summary_line(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            sid = "sid-tool"
            _write_session(home, "p", sid, [
                _user_rec(sid, "u1", None, "2026-01-01T00:00:00Z", "please run"),
                {
                    "sessionId": sid, "type": "assistant", "uuid": "a1", "parentUuid": "u1",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "message": {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                         "input": {"command": "echo hi"}},
                    ]},
                },
                {
                    "sessionId": sid, "type": "user", "uuid": "u2", "parentUuid": "a1",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1",
                         "content": "hi", "is_error": False},
                    ]},
                    "toolUseResult": {"stdout": "hi", "stderr": "", "interrupted": False},
                },
            ])
            out_path = tmp / "out.md"
            proc, result = self._run(
                home, ["--session-id", sid, "--out", str(out_path)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["n_tool"], 1)
            body = out_path.read_text(encoding="utf-8")
            self.assertIn("TOOL Bash: echo hi", body)


if __name__ == "__main__":
    unittest.main()
