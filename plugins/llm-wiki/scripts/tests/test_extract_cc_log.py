"""Tests: R7 cc-log extractor (design §6 / R7).

Covers the MERGE requirement: one-pass markdown that keeps BOTH the full
conversation text (cc-log-extract behavior) AND tool_use blocks with their
tool_result (revert-script behavior) — the gap neither source script fills.

DuckDB is not exercised here; the markdown builder (_build_markdown) and the
tool_use renderer are tested directly on parsed rows so the test has no external
dependency.
"""
import extract_cc_log as ext


def _row(record_type, ts, content, role=None):
    return (record_type, ts, content, role)


def test_render_tool_use_known_and_unknown():
    assert ext.render_tool_use("Bash", {"command": "ls -la"}) == "Bash: ls -la"
    assert ext.render_tool_use("Edit", {"file_path": "wiki/a.md"}) == "Edit: wiki/a.md"
    assert ext.render_tool_use("Mystery", {"x": 1}) == "Mystery"


def test_markdown_keeps_full_text_and_tool_use_with_result():
    rows = [
        _row("user", "2026-06-26 10:00:00", '[{"type":"text","text":"please run ls"}]'),
        _row("assistant", "2026-06-26 10:00:01", (
            '[{"type":"text","text":"sure, running it"},'
            '{"type":"tool_use","id":"tu1","name":"Bash","input":{"command":"ls -la"}}]'
        )),
        _row("user", "2026-06-26 10:00:02", (
            '[{"type":"tool_result","tool_use_id":"tu1","content":"total 0\\nfoo"}]'
        )),
        _row("assistant", "2026-06-26 10:00:03", '[{"type":"text","text":"done"}]'),
    ]
    md = ext._build_markdown(rows)
    # Full conversation text preserved.
    assert "please run ls" in md
    assert "sure, running it" in md
    assert "done" in md
    # tool_use rendered.
    assert "**Tool: Bash: ls -la**" in md
    # tool_result paired and embedded.
    assert "```tool-result" in md
    assert "total 0" in md and "foo" in md
    # Turn structure present.
    assert "## Turn 1" in md
    # The pure tool_result user row did NOT create a spurious Human turn.
    assert md.count("**Human**:") == 1


def test_multi_turn_order_preserved():
    rows = [
        _row("user", "2026-06-26 10:00:00", '[{"type":"text","text":"first"}]'),
        _row("assistant", "2026-06-26 10:00:01", '[{"type":"text","text":"reply1"}]'),
        _row("user", "2026-06-26 10:01:00", '[{"type":"text","text":"second"}]'),
        _row("assistant", "2026-06-26 10:01:01", '[{"type":"text","text":"reply2"}]'),
    ]
    md = ext._build_markdown(rows)
    assert md.index("first") < md.index("second")
    assert "## Turn 2" in md
