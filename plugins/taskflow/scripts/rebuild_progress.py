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
If the file holds more than one region, keeps the first and drops the rest.
Every write uses LF endings explicitly: progress.md and the task files are
shared with the Pi taskflow extension, which reads them raw and matches `\n`,
so a platform-translated CRLF write makes its markers unmatchable.
The Completed table lists EVERY task in tasks/2_done/ — this file is never
truncated. Bounding how many Completed rows reach an agent's context is
view_progress.py's job, and it never writes to progress.md.

Free-text sections (Architecture, Key Decisions, etc.) are NOT touched.

Exit codes:
  0 = success
  2 = script error (bad arguments, missing project dir)
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# Sibling import from hooks/ (same pattern as view_progress.py's import of
# check_progress). log_lock is stdlib-only, so this adds no PEP723 dependency.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from log_lock import write_lock  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)

LF = "\n"
TABLE_BEGIN = "<!-- @table:begin -->"
TABLE_END = "<!-- @table:end -->"
TASK_STATUSES = ("0_todo", "1_in_progress", "2_done")

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


def render_section(title: str, date_col: str, rows: list[TaskRow]) -> list[str]:
    # LOCKSTEP: the section titles and the header/separator shape emitted here are
    # parsed by check_progress.py::parse_progress_table_rows and sliced by
    # view_progress.py::find_completed_rows. Change all three together.
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
    lines.append("")
    return lines


def render_table_region(by_status: dict[str, list[TaskRow]]) -> str:
    lines: list[str] = []
    lines.extend(render_section("TODO", "Created", by_status["0_todo"]))
    lines.extend(render_section("In Progress", "Updated", by_status["1_in_progress"]))
    lines.extend(render_section("Completed", "Completed", by_status["2_done"]))

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
    matches = list(pattern.finditer(content))
    if not matches:
        sep = "" if content.endswith("\n") else "\n"
        return f"{content}{sep}\n{replacement}\n"

    # Exactly one region is legal. Keep the first, drop the rest — a duplicate
    # is the visible residue of a writer that missed the markers and appended
    # (a torn concurrent write, or a Pi-side rebuild that read the file raw
    # while it still had CRLF endings). Replacing every match instead would
    # preserve the duplicates forever, and only the first one is ever capped
    # for context by view_progress.py.
    parts: list[str] = []
    prev = 0
    for i, m in enumerate(matches):
        gap = content[prev:m.start()]
        if i == 0:
            parts.append(gap)
            parts.append(replacement)
        elif gap.strip():
            parts.append(gap)
        prev = m.end()
    parts.append(content[prev:])
    return "".join(parts)


def ensure_progress_md(project_dir: Path) -> Path:
    """Create progress.md if absent, and repair a missing H1 if needed.

    Locked: this is a read-modify-write of the same file `write_region` guards,
    so it takes the same lock. Mirrors the Pi side, which wraps its
    `ensureProgressMd` too.
    """
    progress = project_dir / "progress.md"
    with write_lock(str(progress)):
        if not progress.exists():
            progress.write_text(
                SCAFFOLD.format(name=project_dir.name),
                encoding="utf-8",
                newline=LF,
            )
            return progress
        # File exists; ensure it has an H1 at the top (migration may have stripped it)
        content = read_text(progress) or ""
        if not content.lstrip().startswith("# "):
            progress.write_text(
                f"# Progress: {project_dir.name}\n\n{content}",
                encoding="utf-8",
                newline=LF,
            )
    return progress


def write_region(progress: Path, region: str) -> bool:
    """Splice `region` into progress.md's table block. Returns True if written.

    THE LOCKED WINDOW. This is the read-modify-write that a concurrent writer
    can tear, and the only part of a rebuild that holds the advisory lock
    (`hooks/log_lock.py`, protocol v2 — shared with the Pi taskflow extension).

    Scope is deliberately just read -> splice -> write, NOT the whole rebuild:

      - The heavy `gather_tasks` walk stays OUTSIDE. Measured, the read->write
        window is 0.3-0.6 ms while a full run is 261-295 ms, so locking the
        whole run would inflate the hold by three orders of magnitude against a
        10 s stale threshold (`TASKFLOW_LOCK_STALE`), for nothing.
      - The cost of that choice is that a concurrent rebuild can splice a table
        region computed from a slightly older task scan. That is acceptable
        because the `@table` region is a CACHE, never authoritative (the task
        files' folder location is), and the next rebuild self-heals it. The
        free-text sections are the asset that must not be lost, and those are
        read inside the lock.

    What tearing looks like without the lock, and what the `lockedrebuild` race
    harness (tests/race/lockedrebuild/) detects: `write_text` truncates before
    it writes, so a racing reader can observe a progress.md with no
    `@table:begin` marker, fall into `replace_or_append_region`'s append branch,
    and leave the file with TWO table regions plus whatever free text the
    truncation ate. The extra region is self-healed by the next rebuild (that
    function keeps the first match and drops the rest); the eaten free text is
    not, which is why the lock stays.

    Not covered, by design: an LLM/hand Edit-tool write, which cannot take this
    lock (the R-lock gap). See docs/architecture.md.
    """
    with write_lock(str(progress)):
        content = read_text(progress) or ""
        new_content = replace_or_append_region(content, region)
        if new_content == content:
            return False
        progress.write_text(new_content, encoding="utf-8", newline=LF)
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild progress.md table region from tasks/<status>/*.md files."
    )
    parser.add_argument(
        "project_dir",
        type=Path,
        help="Path to _projects/<project>/ directory",
    )
    args = parser.parse_args(argv)

    project_dir: Path = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return 2

    progress = ensure_progress_md(project_dir)
    # gather_tasks / render_table_region stay OUTSIDE the lock on purpose —
    # see write_region's docstring for why the hold is scoped to the splice.
    by_status = gather_tasks(project_dir)
    region = render_table_region(by_status)
    verb = "rebuilt" if write_region(progress, region) else "unchanged"

    counts = {s: len(by_status[s]) for s in TASK_STATUSES}
    total = sum(counts.values())
    print(f"{verb}: {progress}")
    print(
        f"  TODO: {counts['0_todo']}, "
        f"In Progress: {counts['1_in_progress']}, "
        f"Completed: {counts['2_done']} "
        f"(total {total})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
