#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""parse_progress_table.py — extract legacy v1 progress.md tables as JSON.

Reads a v1-format progress.md and extracts the three known tables
(TODO / In Progress / Completed). Column names vary by section; this
script preserves cell content as a dict keyed by the header row.

Used by the migration skill (Phase 1) to extract structured task data
from existing progress.md files, which the LLM then uses to compose
v2 task files.

Output (JSON to stdout):
  {
    "todo": [
      { "<header>": "<cell>", ... },
      ...
    ],
    "in_progress": [
      { ... },
      ...
    ],
    "completed": [
      { ... },
      ...
    ],
    "session_log_headers": [
      "### 2026-05-13 - <title>",
      ...
    ]
  }

Session Log entry bodies are NOT extracted (they would explode the JSON);
only header lines are listed so the migration skill can decide which
chunks to read separately.

Exit codes:
  0 = success
  2 = script error
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

SECTION_KEYS = {
    "TODO": "todo",
    "In Progress": "in_progress",
    "Completed": "completed",
}

SECTION_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")
LOG_ENTRY_RE = re.compile(r"^###\s+\d{4}-\d{2}-\d{2}(?:\s*\(\d+\))?\s*[-–—]\s*.+$")


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def parse_table(lines: list[str]) -> list[dict]:
    """Parse a markdown table from a list of lines (header + separator + rows).
    Returns list of {header: cell, ...} dicts.
    """
    rows: list[dict] = []
    if len(lines) < 2:
        return rows

    header_line = lines[0]
    sep_line = lines[1]
    if not header_line.startswith("|") or not sep_line.startswith("|"):
        return rows
    sep_stripped = sep_line.strip("|").replace("|", "")
    if not re.fullmatch(r"[\s\-:]+", sep_stripped):
        return rows

    headers = [c.strip() for c in header_line.strip("|").split("|")]

    for line in lines[2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < len(headers):
            cells = cells + [""] * (len(headers) - len(cells))
        if len(cells) > len(headers):
            cells = cells[: len(headers) - 1] + ["|".join(cells[len(headers) - 1 :])]
        rows.append({h: c for h, c in zip(headers, cells)})

    return rows


def extract_section_table(
    all_lines: list[str], section_title: str
) -> list[dict]:
    """Find `## <section_title>` and parse the immediately following table."""
    in_section = False
    table_lines: list[str] = []
    found_table = False

    for line in all_lines:
        sec_m = SECTION_HEADER_RE.match(line)
        if sec_m:
            if in_section and found_table:
                break
            in_section = sec_m.group(1) == section_title
            continue
        if not in_section:
            continue
        if line.startswith("|"):
            table_lines.append(line)
            found_table = True
        else:
            if found_table:
                break

    return parse_table(table_lines)


def extract_session_log_headers(all_lines: list[str]) -> list[str]:
    """Collect all '### YYYY-MM-DD - title' headers from Session Log section."""
    headers: list[str] = []
    in_log = False
    for line in all_lines:
        sec_m = SECTION_HEADER_RE.match(line)
        if sec_m:
            in_log = sec_m.group(1).strip().lower() == "session log"
            continue
        if in_log and LOG_ENTRY_RE.match(line):
            headers.append(line.rstrip())
    return headers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract legacy v1 progress.md tables and session log headers as JSON."
    )
    parser.add_argument("progress_md", type=Path, help="Path to progress.md file")
    args = parser.parse_args(argv)

    progress: Path = args.progress_md.resolve()
    if not progress.is_file():
        print(f"error: not a file: {progress}", file=sys.stderr)
        return 2

    try:
        content = read_text(progress)
    except (OSError, UnicodeDecodeError) as e:
        print(f"error: cannot read {progress}: {e}", file=sys.stderr)
        return 2

    lines = content.splitlines()

    result = {
        "todo": extract_section_table(lines, "TODO"),
        "in_progress": extract_section_table(lines, "In Progress"),
        "completed": extract_section_table(lines, "Completed"),
        "session_log_headers": extract_session_log_headers(lines),
    }

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
