#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""rebuild_progress.py — regenerate progress.md table region from task files.

Walks tasks/0_todo/, 1_in_progress/, 2_done/ in order. For each task, reads
frontmatter (priority, updated, created) and the H1 line; emits 3 markdown
tables (TODO / In Progress / Completed) into the <!-- @table:begin --> ...
<!-- @table:end --> region of progress.md.

If progress.md does not exist, creates a minimal scaffold first.
If the @table markers are absent, appends them at the end (Risk R7).
The Completed table is capped to the most recent N rows (see
TASKFLOW_DONE_ROWS_MAX / --done-rows-max below); a footnote reports the
omitted count when the cap is active.

Free-text sections (Architecture, Key Decisions, etc.) are NOT touched.

Exit codes:
  0 = success
  2 = script error (bad arguments, missing project dir)
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)

TABLE_BEGIN = "<!-- @table:begin -->"
TABLE_END = "<!-- @table:end -->"
TASK_STATUSES = ("0_todo", "1_in_progress", "2_done")

# Completed-table row cap (see project-notes/specs/done-table-row-cap.md).
# 0 or negative = unlimited.
DONE_ROWS_MAX_DEFAULT = 10
ENV_DONE_ROWS_MAX = "TASKFLOW_DONE_ROWS_MAX"

SCAFFOLD = """# Progress: {name}

## Architecture
<!-- Overall project structure and composition. -->

## Key Decisions & Policies
<!-- Decisions and policies for this project. -->

## Open Issues
<!-- Surfacing problems and concerns. -->

## Reference Materials
<!-- External docs, links. -->

"""


@dataclass
class TaskRow:
    n: int
    priority: str
    h1: str
    date: str
    link: str


def read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def parse_frontmatter(content: str) -> dict | None:
    m = FRONTMATTER_RE.match(content)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        return None


def extract_h1(content: str) -> str | None:
    body = FRONTMATTER_RE.sub("", content, count=1)
    m = H1_RE.search(body)
    return m.group(1).strip() if m else None


def format_date(value: object) -> str:
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip()
    return ""


def escape_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ").strip()


def gather_tasks(project_dir: Path) -> dict[str, list[TaskRow]]:
    by_status: dict[str, list[TaskRow]] = {s: [] for s in TASK_STATUSES}
    tasks_dir = project_dir / "tasks"
    if not tasks_dir.is_dir():
        return by_status

    for status in TASK_STATUSES:
        sub = tasks_dir / status
        if not sub.is_dir():
            continue
        n = 1
        for task_path in sorted(sub.iterdir()):
            if not task_path.is_file() or task_path.suffix != ".md":
                continue
            content = read_text(task_path)
            if content is None:
                continue
            fm = parse_frontmatter(content) or {}
            h1 = extract_h1(content) or task_path.stem
            priority = str(fm.get("priority", "")).strip()
            # For TODO: prefer created; otherwise prefer updated
            if status == "0_todo":
                date = format_date(fm.get("created")) or format_date(fm.get("updated"))
            else:
                date = format_date(fm.get("updated")) or format_date(fm.get("created"))
            link = f"@tasks/{status}/{task_path.name}"
            by_status[status].append(TaskRow(n, priority, h1, date, link))
            n += 1
    return by_status


def render_section(
    title: str, date_col: str, rows: list[TaskRow], footnote: str | None = None
) -> list[str]:
    lines: list[str] = []
    lines.append(f"## {title}")
    lines.append("")
    lines.append(f"| # | Priority | Task | {date_col} | Link |")
    lines.append("|---|----------|------|---------|------|")
    for r in rows:
        lines.append(
            f"| {r.n} | {escape_cell(r.priority)} | {escape_cell(r.h1)} "
            f"| {escape_cell(r.date)} | {r.link} |"
        )
    if footnote:
        lines.append("")
        lines.append(footnote)
    lines.append("")
    return lines


def render_table_region(
    by_status: dict[str, list[TaskRow]], done_limit: int = DONE_ROWS_MAX_DEFAULT
) -> str:
    lines: list[str] = []
    lines.extend(render_section("TODO", "Created", by_status["0_todo"]))
    lines.extend(render_section("In Progress", "Updated", by_status["1_in_progress"]))

    done_rows = by_status["2_done"]
    footnote = None
    shown = done_rows
    if done_limit > 0 and len(done_rows) > done_limit:
        shown = done_rows[-done_limit:]
        omitted = len(done_rows) - done_limit
        footnote = (
            f"_Showing the latest {done_limit} of {len(done_rows)} completed "
            f"tasks — {omitted} older entries omitted. Full list: tasks/2_done/_"
        )
    lines.extend(render_section("Completed", "Completed", shown, footnote))

    # Trim trailing empty line
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def replace_or_append_region(content: str, new_region: str) -> str:
    pattern = re.compile(
        rf"{re.escape(TABLE_BEGIN)}\n.*?\n{re.escape(TABLE_END)}",
        re.DOTALL,
    )
    replacement = f"{TABLE_BEGIN}\n{new_region}\n{TABLE_END}"
    if pattern.search(content):
        return pattern.sub(replacement, content)
    sep = "" if content.endswith("\n") else "\n"
    return f"{content}{sep}\n{replacement}\n"


def ensure_progress_md(project_dir: Path) -> Path:
    progress = project_dir / "progress.md"
    if not progress.exists():
        progress.write_text(SCAFFOLD.format(name=project_dir.name), encoding="utf-8")
        return progress
    # File exists; ensure it has an H1 at the top (migration may have stripped it)
    content = read_text(progress) or ""
    if not content.lstrip().startswith("# "):
        progress.write_text(
            f"# Progress: {project_dir.name}\n\n{content}",
            encoding="utf-8",
        )
    return progress


def resolve_done_limit(cli_value: int | None) -> int:
    if cli_value is not None:
        return cli_value
    try:
        return int(os.environ.get(ENV_DONE_ROWS_MAX, str(DONE_ROWS_MAX_DEFAULT)))
    except ValueError:
        return DONE_ROWS_MAX_DEFAULT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild progress.md table region from tasks/<status>/*.md files."
    )
    parser.add_argument(
        "project_dir",
        type=Path,
        help="Path to _projects/<project>/ directory",
    )
    parser.add_argument(
        "--done-rows-max",
        type=int,
        default=None,
        help=(
            "Cap the Completed table to the N most recent rows "
            f"(default: {DONE_ROWS_MAX_DEFAULT}; 0 or negative = unlimited). "
            f"Falls back to env {ENV_DONE_ROWS_MAX} when unset."
        ),
    )
    args = parser.parse_args(argv)

    project_dir: Path = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return 2

    done_limit = resolve_done_limit(args.done_rows_max)

    progress = ensure_progress_md(project_dir)
    by_status = gather_tasks(project_dir)
    region = render_table_region(by_status, done_limit)
    content = read_text(progress) or ""
    new_content = replace_or_append_region(content, region)

    if new_content != content:
        progress.write_text(new_content, encoding="utf-8")
        verb = "rebuilt"
    else:
        verb = "unchanged"

    counts = {s: len(by_status[s]) for s in TASK_STATUSES}
    total = sum(counts.values())
    done_total = counts["2_done"]
    if done_limit > 0 and done_total > done_limit:
        completed_str = f"Completed: {done_total} (showing latest {done_limit})"
    else:
        completed_str = f"Completed: {done_total}"
    print(f"{verb}: {progress}")
    print(
        f"  TODO: {counts['0_todo']}, "
        f"In Progress: {counts['1_in_progress']}, "
        f"{completed_str} "
        f"(total {total})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
