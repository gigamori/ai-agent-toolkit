#!/usr/bin/env python3
"""pj_rules.py — helper for the /pj-rules skill (per-project rules.md).

Actions:
  show <project-root>            Print a rules.md summary: existence, `## `
                                  heading list, injected-body line count vs
                                  cap (frontmatter `max_lines`, default 100).
  reset-indexed <state-file>     Merge-preserving reset of the session state
                                  file's `project_rules_indexed` field to ""
                                  (so the next turn re-injects the full body).

Design note: state-file resets and heading extraction are done here, in code,
rather than delegated to the calling skill's own Read/Edit — a JSON
read-modify-write done by an LLM risks silently dropping unrelated fields
(this bit taskflow before: see hooks/session_init.py's `new_state = dict(loaded)`
comment). `show`'s heading count is also meant to be called by the skill
before and after a write, to deterministically verify the edit added a `## `
heading rather than trusting the LLM's self-report.

Exit codes:
  0 = ok (show: rules.md exists; reset-indexed: reset applied)
  1 = show: rules.md does not exist (not an error — informational)
  2 = script / argument error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

DEFAULT_MAX_LINES = 100
HEADING_RE = re.compile(r"^##\s+(\S.*?)\s*$")
FENCE_RE = re.compile(r"^(```|~~~)")


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a leading `---`...`---` block. Only simple `key: value` lines are
    parsed (no nesting). Mirrors hooks/session_init.py's split_frontmatter —
    duplicated (not imported) because scripts/ and hooks/ are standalone."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, "\n".join(lines[end + 1:])


def extract_headings(body: str) -> list[str]:
    """Return level-2 (`## `) headings, skipping fenced code blocks. Mirrors
    hooks/session_init.py's extract_headings (duplicated; see module docstring)."""
    heads = []
    in_fence = False
    for line in body.split("\n"):
        stripped = line.strip()
        if FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if m:
            heads.append(m.group(1))
    return heads


def cmd_show(project_root: Path) -> int:
    rules_path = project_root / "rules.md"
    if not rules_path.exists():
        print(f"rules.md: {rules_path}")
        print("exists: false")
        return 1

    raw = rules_path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(raw)
    body_stripped = body.strip("\n")
    max_lines_raw = fm.get("max_lines", str(DEFAULT_MAX_LINES))
    try:
        max_lines = int(max_lines_raw)
    except ValueError:
        max_lines = DEFAULT_MAX_LINES

    # Line-count definition (spec M4): the injected body AFTER frontmatter is
    # stripped — this is what actually costs tokens on injection, in both
    # primer and inject_every_turn modes.
    line_count = len(body_stripped.split("\n")) if body_stripped else 0
    heads = extract_headings(body_stripped)
    over_cap = line_count > max_lines
    inject_every_turn = fm.get("inject_every_turn", "").strip().lower() in (
        "true", "1", "yes", "on",
    )

    print(f"rules.md: {rules_path}")
    print("exists: true")
    print(f"headings: {len(heads)}")
    print(f"lines: {line_count} (cap: {max_lines})")
    print(f"over_cap: {'true' if over_cap else 'false'}")
    print(f"inject_every_turn: {'true' if inject_every_turn else 'false'}")
    for h in heads:
        print(f"- {h}")
    return 0


def cmd_reset_indexed(state_file: Path) -> int:
    try:
        with state_file.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read state file {state_file}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(state, dict):
        print(f"error: state file {state_file} is not a JSON object", file=sys.stderr)
        return 2

    # Merge-preserving: only this one field changes. All other fields
    # (progress_capture_done, exec_bind, origin, ...) pass through untouched.
    state["project_rules_indexed"] = ""

    with state_file.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    print(f"reset: project_rules_indexed=\"\" in {state_file}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Helper for the /pj-rules skill.")
    sub = parser.add_subparsers(dest="action", required=True)

    p_show = sub.add_parser("show", help="Summarize a project's rules.md")
    p_show.add_argument("project_root", type=Path)

    p_reset = sub.add_parser(
        "reset-indexed", help="Merge-preserving reset of project_rules_indexed"
    )
    p_reset.add_argument("state_file", type=Path)

    args = parser.parse_args()

    if args.action == "show":
        return cmd_show(args.project_root)
    if args.action == "reset-indexed":
        return cmd_reset_indexed(args.state_file)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
