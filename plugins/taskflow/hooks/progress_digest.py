"""progress_digest.py — build a capped In Progress digest block for injection.

Fourth copy of the progress-table parsing. LOCKSTEP: the section-heading and
separator regexes below mirror check_progress.py::parse_progress_table_rows,
the section titles and header/separator shape emitted by
rebuild_progress.py::render_section, and view_progress.py's own
TABLE_BEGIN/TABLE_END/SECTION_RE/SEPARATOR_RE. Change all four together.
TABLE_BEGIN, TABLE_END, SECTION_RE.pattern and SEPARATOR_RE.pattern are
checked byte-identical against view_progress.py's copies by
tests/test_progress_view_cap.py.

Imported by the sibling hook that injects turn context, the only caller. This
module takes progress.md text as an argument and does no path resolution of
its own: no project-root discovery, no project-tree lookup, no I/O beyond
what the caller hands it. The caller resolves the project's progress.md path
and passes the text in; this module never re-derives that path. Kept free of
both so the functions here stay callable directly by a unit test without
going through the caller's own module-scope root resolution.

stdlib only: hooks run under `uv run --no-project`.
"""

from __future__ import annotations

import re

TABLE_BEGIN = "<!-- @table:begin -->"
TABLE_END = "<!-- @table:end -->"

# LOCKSTEP: check_progress.py::parse_progress_table_rows /
# rebuild_progress.py::render_section / view_progress.py's own copies.
SECTION_RE = re.compile(r"^##\s+(TODO|In Progress|Completed)\s*$")
SEPARATOR_RE = re.compile(r"^\|[-\s|:]+\|\s*$")

IN_PROGRESS_ROWS_MAX = 5
INLINE_BUDGET_U16 = 10000  # CC 2.1.270 `NEr`; the comparison is `<=`


def u16len(s: str) -> int:
    """Length of `s` in UTF-16 code units — the harness's own context-length
    unit (CC 2.1.270)."""
    return len(s.encode("utf-16-le")) // 2


def find_in_progress_rows(lines: list[str]) -> tuple[int, int, int] | None:
    """Locate the In Progress table inside the <!-- @table:begin/end --> region.

    Returns (header_idx, first, last): header_idx is the column-header row
    immediately above the separator; [first, last) is the DATA row index
    range, same slicing discipline as view_progress.py::find_completed_rows.

    Returns None when there is no @table region, no In Progress section, no
    header row above the separator, or no data rows — in every such case the
    caller injects nothing, matching find_completed_rows' "pass through
    unchanged" contract.
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
        if m and m.group(1) == "In Progress":
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
    if separator is None or separator == section_start + 1:
        return None
    header_idx = separator - 1

    first = separator + 1
    last = first
    while last < section_end and lines[last].startswith("|"):
        last += 1
    if last == first:
        return None
    return (header_idx, first, last)


def build_digest(progress_text: str, project_name: str) -> str:
    """Capped In Progress digest for `project_name`, or '' when absent.

    '' covers every case find_in_progress_rows returns None for. Rows shown
    are the first IN_PROGRESS_ROWS_MAX data rows in file order — a pure
    line-level subset, no re-sorting. The footer is always emitted, including
    when shown == total, so it is the honesty mechanism and the observable
    proof that the cap is what holds the block down.
    """
    lines = progress_text.split("\n")

    span = find_in_progress_rows(lines)
    if span is None:
        return ""
    header_idx, first, last = span
    total = last - first
    shown_last = min(first + IN_PROGRESS_ROWS_MAX, last)
    shown = shown_last - first

    out = [
        f"[Project Progress: {project_name}]",
        lines[header_idx],
        lines[header_idx + 1],
    ]
    out.extend(lines[first:shown_last])
    out.append(f"in-progress: {shown} of {total} rows shown")
    return "\n".join(out)
