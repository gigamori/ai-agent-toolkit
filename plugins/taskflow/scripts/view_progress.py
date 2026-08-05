#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""view_progress.py — print a context-bounded VIEW of progress.md.

progress.md itself is never truncated: rebuild_progress.py always renders every
task in tasks/2_done/. This script is the only thing that bounds how many
Completed rows reach an agent's context. It reads progress.md and writes a
truncated copy to stdout; it never writes to any file.

The view is a pure line-level subset of the file — it does not re-read tasks/,
so it can never disagree with progress.md — EXCEPT under `--notes-summary`
(see below), which does not touch progress.md at all. Only the `## Completed`
data rows inside the <!-- @table:begin --> ... <!-- @table:end --> region are
dropped; the `#` column is NOT renumbered, so a view is visibly a tail of the
file. Everything else (free-text sections, TODO / In Progress tables, markers)
is passed through byte for byte.

LOCKSTEP: the section-heading regex below mirrors
check_progress.py::parse_progress_table_rows and the section titles emitted by
rebuild_progress.py::render_section. Change all three together.

Configuration (CLI > env > default):
  --limit N        keep the N most recent Completed rows (0 = unlimited)
  --all            equivalent to --limit 0
  --notes-summary  emit a context-bounded project-notes/ summary instead of
                    the progress view (does not read/require progress.md)
  (--limit / --all / --notes-summary are mutually exclusive)
  env TASKFLOW_CONTEXT_DONE_ROWS_MAX  default limit when neither flag is given

`--notes-summary` bounds how many project-notes/ paths reach an agent's
context (project-router-context-payload-cap.md design doc). It never lists
individual note paths — only counts. Note-set definition (rglob("*.md") minus
index.md) and the index-drift count are IMPORTED from check_progress.py
(walk_note_files / parse_notes_index_rows), not re-implemented here, so the two
scripts can never silently drift apart on what counts as a "note".

Exit codes:
  0 = view (or notes summary) written to stdout
  1 = progress.md does not exist (--notes-summary is exempt: it never checks
      for progress.md)
  2 = script error (bad arguments — including combining --notes-summary with
      --limit/--all — or a missing project dir)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_progress as _cp  # noqa: E402

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


def _category_of(rel: str) -> str:
    """First path segment of a project-notes-relative path, or '(root)' for a
    note with no subdirectory. Deliberately unconstrained to the 6 documented
    categories — an off-convention directory (e.g. a stray 'debug-handoffs/')
    surfaces here as its own category instead of being silently absorbed."""
    parts = rel.split("/", 1)
    return parts[0] if len(parts) > 1 else "(root)"


def build_notes_summary(project_dir: Path) -> str:
    """Build the `--notes-summary` block: counts only, never individual paths.

    Note-set definition and index-row parsing are imported from
    check_progress.py (walk_note_files / parse_notes_index_rows) — see the
    module docstring's LOCKSTEP note. Returns 'none' when project-notes/ does
    not exist.
    """
    notes_dir = project_dir / "project-notes"
    if not notes_dir.is_dir():
        return "none"

    archive_count = 0
    category_counts: dict[str, int] = {}
    actual: set[str] = set()
    for p in _cp.walk_note_files(notes_dir):
        rel = p.relative_to(notes_dir).as_posix()
        actual.add(rel)
        if rel.startswith("_archive/"):
            archive_count += 1
            continue
        cat = _category_of(rel)
        category_counts[cat] = category_counts.get(cat, 0) + 1

    live_total = sum(category_counts.values())
    cat_str = " / ".join(
        f"{cat} {n}"
        for cat, n in sorted(category_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    lines = [f"{live_total} notes" + (f" · {cat_str}" if cat_str else "")]

    if archive_count:
        lines.append(
            f"_archive: {archive_count} (non-authoritative — excluded from relevant rows)"
        )

    index_md = notes_dir / "index.md"
    index_content = _cp.read_text(index_md)
    if index_content is None:
        lines.append("index: missing → create project-notes/index.md")
    else:
        indexed = {r["file"] for r in _cp.parse_notes_index_rows(index_content)}
        unregistered = len(actual - indexed)
        missing = len(indexed - actual)
        if unregistered or missing:
            lines.append(
                f"index drift: {unregistered} unregistered, {missing} missing "
                "→ run `/progress check`"
            )

    lines.append("enumerate: ls _projects/<project>/project-notes/**/*.md")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Print a context-bounded view of progress.md (or, with "
            "--notes-summary, of project-notes/) to stdout. Never modifies "
            "any file."
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
    group.add_argument(
        "--notes-summary",
        action="store_true",
        help=(
            "Emit a context-bounded project-notes/ summary (counts only) "
            "instead of the progress view. Does not require progress.md."
        ),
    )
    args = parser.parse_args(argv)

    project_dir: Path = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return 2

    if args.notes_summary:
        sys.stdout.write(build_notes_summary(project_dir))
        return 0

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
