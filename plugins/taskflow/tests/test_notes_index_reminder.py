#!/usr/bin/env python3
"""The reminder's column row must equal the canonical `index.md format` header
in prompts/notes_guidelines.md. The hook hard-codes the row (no import spans
Python<->Markdown), so this test IS the anti-drift mechanism — the cross-language
equivalent of the _PRECOMPACT_NOTE_PREFIX shared constant."""
import json, subprocess, sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
HOOK = PLUGIN / "hooks" / "notes_index_reminder.py"
CANON = PLUGIN / "prompts" / "notes_guidelines.md"


def _columns(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _reminder(file_path: str) -> str | None:
    payload = json.dumps({"tool_input": {"file_path": file_path}})
    out = subprocess.run(
        [sys.executable, str(HOOK)], input=payload,
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def canonical_columns() -> list[str]:
    for line in CANON.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("| File |"):
            return _columns(line)
    raise AssertionError("no `| File |` header row found in notes_guidelines.md")


def test_reminder_row_matches_canonical_columns():
    ctx = _reminder("_projects/demo/project-notes/specs/x.md")
    assert ctx is not None
    rows = [ln for ln in ctx.splitlines() if "| File |" in ln]
    assert len(rows) == 1, f"expected exactly one column row, got {rows}"
    row = rows[0][rows[0].index("|"):]
    assert _columns(row) == canonical_columns() == [
        "File", "Description", "Tags", "Updated"]


def test_index_md_itself_is_excluded():
    assert _reminder("_projects/demo/project-notes/index.md") is None


def test_non_notes_path_is_silent():
    assert _reminder("_projects/demo/tasks/0_todo/2026-01-01_x.md") is None
