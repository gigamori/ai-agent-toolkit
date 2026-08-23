#!/usr/bin/env python3
import json, subprocess, sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
HOOK = PLUGIN / "hooks" / "notes_index_reminder.py"
CANON = PLUGIN / "prompts" / "notes_guidelines.md"

PASS = 0
FAIL = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS: {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL: {msg}")


def check(cond: bool, msg: str) -> None:
    ok(msg) if cond else bad(msg)


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


def test_reminder_row_matches_canonical_columns() -> None:
    ctx = _reminder("_projects/demo/project-notes/specs/x.md")
    check(ctx is not None, "a project-notes/ write gets a reminder")
    if ctx is None:
        return
    rows = [ln for ln in ctx.splitlines() if "| File |" in ln]
    check(len(rows) == 1, f"exactly one column row (got {rows})")
    if len(rows) != 1:
        return
    row = rows[0][rows[0].index("|"):]
    canon = canonical_columns()
    check(_columns(row) == canon,
          f"reminder row matches notes_guidelines.md (got {_columns(row)}, canon {canon})")
    check(canon == ["File", "Description", "Tags", "Updated"],
          f"canonical columns are the 4 expected ones (got {canon})")


def test_index_md_itself_is_excluded() -> None:
    check(_reminder("_projects/demo/project-notes/index.md") is None,
          "index.md itself gets no reminder")


def test_non_notes_path_is_silent() -> None:
    check(_reminder("_projects/demo/tasks/0_todo/2026-01-01_x.md") is None,
          "a tasks/ path gets no reminder")


def main() -> int:
    print("=== notes_index_reminder.py column-row anti-drift tests ===")
    for t in (
        test_reminder_row_matches_canonical_columns,
        test_index_md_itself_is_excluded,
        test_non_notes_path_is_silent,
    ):
        t()

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
