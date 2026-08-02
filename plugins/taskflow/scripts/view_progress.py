#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""view_progress.py — print a context-bounded VIEW of progress.md.

progress.md itself is never truncated: rebuild_progress.py always renders every
task in tasks/2_done/. This script is the only thing that bounds how many
Completed rows reach an agent's context. It reads progress.md and writes a
truncated copy to stdout; it never writes to any file.

The view is a pure line-level subset of the file — it does not re-read tasks/,
so it can never disagree with progress.md. Only the `## Completed` data rows
inside the <!-- @table:begin --> ... <!-- @table:end --> region are dropped;
the `#` column is NOT renumbered, so a view is visibly a tail of the file.
Everything else (free-text sections, TODO / In Progress tables, markers) is
passed through byte for byte.

LOCKSTEP: the section-heading regex below mirrors
check_progress.py::parse_progress_table_rows and the section titles emitted by
rebuild_progress.py::render_section. Change all three together.

Configuration (CLI > env > default):
  --limit N   keep the N most recent Completed rows (0 = unlimited)
  --all       equivalent to --limit 0 (mutually exclusive with --limit)
  env TASKFLOW_CONTEXT_DONE_ROWS_MAX  default limit when neither flag is given

Exit codes:
  0 = view written to stdout
  1 = progress.md does not exist
  2 = script error (bad arguments, missing project dir)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

TABLE_BEGIN = "<!-- @table:begin -->"
TABLE_END = "<!-- @table:end -->"

# LOCKSTEP: check_progress.py::parse_progress_table_rows / rebuild_progress.py::render_section
SECTION_RE = re.compile(r"^##\s+(TODO|In Progress|Completed)\s*$")
SEPARATOR_RE = re.compile(r"^\|[-\s|:]+\|\s*$")

DONE_ROWS_MAX_DEFAULT = 10
ENV_DONE_ROWS_MAX = "TASKFLOW_CONTEXT_DONE_ROWS_MAX"


def resolve_limit(cli_limit: int | None, all_flag: bool) -> int:
    if all_flag:
        return 0
    if cli_limit is not None:
        return cli_limit
    try:
        return int(os.environ.get(ENV_DONE_ROWS_MAX, str(DONE_ROWS_MAX_DEFAULT)))
    except ValueError:
        return DONE_ROWS_MAX_DEFAULT


def find_completed_rows(lines: list[str]) -> tuple[int, int] | None:
    """Return [start, end) index range of the Completed table's DATA rows.

    Returns None when there is no @table region, no Completed section, or the
    section holds no data rows — in every such case the caller passes the file
    through unchanged.
    """
    try:
        begin = lines.index(TABLE_BEGIN)
        end = lines.index(TABLE_END)
    except ValueError:
        return None
    if end <= begin:
        return None

    section_start = None
    for i in range(begin + 1, end):
        m = SECTION_RE.match(lines[i])
        if m and m.group(1) == "Completed":
            section_start = i
            break
    if section_start is None:
        return None

    section_end = end
    for i in range(section_start + 1, end):
        if SECTION_RE.match(lines[i]):
            section_end = i
            break

    separator = None
    for i in range(section_start + 1, section_end):
        if SEPARATOR_RE.match(lines[i]):
            separator = i
            break
    if separator is None:
        return None

    first = separator + 1
    last = first
    while last < section_end and lines[last].startswith("|"):
        last += 1
    if last == first:
        return None
    return (first, last)


def build_view(content: str, limit: int, project_name: str) -> str:
    trailing_newline = content.endswith("\n")
    lines = content[:-1].split("\n") if trailing_newline else content.split("\n")

    span = find_completed_rows(lines)
    if span is None:
        return content
    first, last = span
    total = last - first
    if limit <= 0 or total <= limit:
        return content

    omitted = total - limit
    footnote = (
        f"_[context view] Latest {limit} of {total} completed rows; "
        f"{omitted} older rows dropped. This is a TRUNCATED VIEW of "
        f"{project_name}/progress.md — the file itself holds all {total} rows. "
        f"Never write this block back. Full history: tasks/2_done/_"
    )
    out = lines[:first] + lines[last - limit : last] + ["", footnote] + lines[last:]
    text = "\n".join(out)
    return text + "\n" if trailing_newline else text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Print a context-bounded view of progress.md to stdout. "
            "Never modifies any file."
        )
    )
    parser.add_argument(
        "project_dir",
        type=Path,
        help="Path to _projects/<project>/ directory",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Keep only the N most recent Completed rows "
            f"(default: {DONE_ROWS_MAX_DEFAULT}; 0 = unlimited). "
            f"Falls back to env {ENV_DONE_ROWS_MAX} when unset."
        ),
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Emit every Completed row (same as --limit 0).",
    )
    args = parser.parse_args(argv)

    project_dir: Path = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return 2

    progress = project_dir / "progress.md"
    if not progress.is_file():
        print(f"error: progress.md not found in {project_dir}", file=sys.stderr)
        return 1

    try:
        content = progress.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {progress}: {exc}", file=sys.stderr)
        return 2

    limit = resolve_limit(args.limit, args.all)
    sys.stdout.write(build_view(content, limit, project_dir.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
