"""Tests: UserPromptSubmit marker-inject hook (D8, design §6 F2).

Covers: marker present -> wiki-active additionalContext emitted; dormant -> empty
exit (no output). The hook is run as a subprocess with hook JSON on stdin.
"""
import json
import subprocess
import sys
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parents[2] / "hooks" / "wiki_marker_inject.py"
)


def _run(cwd: Path):
    payload = json.dumps({"cwd": str(cwd), "prompt": "hello"})
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload, capture_output=True, text=True, timeout=30,
    )


def test_emits_wiki_active_when_marker_present(tmp_path):
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n", encoding="utf-8")
    r = _run(tmp_path)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "wiki-active" in ctx
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_empty_exit_when_dormant(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
