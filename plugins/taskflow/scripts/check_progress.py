#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""check_progress.py — taskflow v0.2.2 drift detector.

Inspects a project's progress.md, tasks/, and project-notes/ for:
  1. task link drift     — progress.md @tasks/<status>/<file> refs vs filesystem
  2. notes index drift   — project-notes/index.md rows vs actual files
  3. stale tasks         — frontmatter 'updated' older than threshold days
  4. project marker      — project listed in _projects/index.md
  5. broken @references  — task body @tasks/... and @project-notes/... links
  6. summary / H1 sync   — progress.md row text contains task H1
  7. filename violations — <YYYY-MM-DD>_<topic>(-<N>)?.md
  8. pending approval    — 1_in_progress/ stalled past threshold
  9. orphan lock         — *.md.lock with no sibling *.md (report-only)
  10. duplicate basename — same task-md basename in >=2 locations under tasks/ (whole-tree walk)

Exit codes:
  0 = no findings
  1 = findings present
  2 = script error (bad arguments, missing project dir)
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
TASK_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+?)(?:-(\d+))?\.md$")
H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
TASK_REF_RE = re.compile(r"@tasks/([012]_(?:todo|in_progress|done))/[^\s)|`\]*]+")
NOTES_REF_RE = re.compile(r"@(?:project-notes|notes)/([^\s)|`\]*]+)")

TASK_STATUSES = ("0_todo", "1_in_progress", "2_done")
SECTION_TO_STATUS = {
    "TODO": "0_todo",
    "In Progress": "1_in_progress",
    "Completed": "2_done",
}


@dataclass
class Finding:
    check: str
    severity: str  # drift | violation | stale | approval
    path: str
    message: str


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)

    def add(self, check: str, severity: str, path: str, message: str) -> None:
        self.findings.append(Finding(check, severity, path, message))


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


def parse_date(value: object) -> datetime.date | None:
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def walk_task_files(project_dir: Path) -> Iterator[tuple[Path, str]]:
    tasks_dir = project_dir / "tasks"
    if not tasks_dir.is_dir():
        return
    for status in TASK_STATUSES:
        sub = tasks_dir / status
        if not sub.is_dir():
            continue
        for p in sorted(sub.iterdir()):
            if p.is_file() and p.suffix == ".md":
                yield p, status


def walk_note_files(notes_dir: Path) -> Iterator[Path]:
    if not notes_dir.is_dir():
        return
    for p in sorted(notes_dir.rglob("*.md")):
        if p.name == "index.md":
            continue
        yield p


def parse_progress_table_rows(progress_md: str) -> list[dict]:
    """Extract task ref rows from progress.md table region."""
    rows: list[dict] = []
    section: str | None = None
    for line in progress_md.splitlines():
        sec_match = re.match(r"^##\s+(TODO|In Progress|Completed)\s*$", line)
        if sec_match:
            section = sec_match.group(1)
            continue
        if section and line.startswith("|") and "@tasks/" in line:
            ref_m = re.search(r"@tasks/[012]_[a-z_]+/[^\s|)]+", line)
            if ref_m:
                cells = [c.strip() for c in line.strip("|").split("|")]
                rows.append({
                    "section": section,
                    "ref": ref_m.group(0),
                    "cells": cells,
                    "raw": line,
                })
    return rows


def parse_notes_index_rows(index_md: str) -> list[dict]:
    rows: list[dict] = []
    for line in index_md.splitlines():
        if not line.startswith("|"):
            continue
        stripped = line.strip("|")
        cells = [c.strip() for c in stripped.split("|")]
        if not cells or not cells[0]:
            continue
        first = cells[0].lower()
        if first == "file" or set(cells[0]) <= {"-", " "}:
            continue
        rows.append({"file": cells[0], "cells": cells, "raw": line})
    return rows


# ============================================================================
# Checks
# ============================================================================

def check_progress_task_links(project_dir: Path, result: Result) -> None:
    """#1 + part of status lockstep."""
    progress = project_dir / "progress.md"
    content = read_text(progress)
    if content is None:
        return
    for row in parse_progress_table_rows(content):
        ref = row["ref"]
        rel = ref.lstrip("@")
        target = project_dir / rel
        if not target.exists():
            result.add(
                "task_link",
                "drift",
                str(progress),
                f"references {ref} but file does not exist",
            )
            continue
        expected = SECTION_TO_STATUS.get(row["section"])
        parts = rel.split("/")
        actual = parts[1] if len(parts) >= 3 else None
        if expected and actual and expected != actual:
            result.add(
                "task_status",
                "drift",
                str(target),
                f"progress.md '{row['section']}' section references {ref} "
                f"but file is in {actual}/ (expected {expected}/)",
            )


def check_notes_index_consistency(project_dir: Path, result: Result) -> None:
    """#2"""
    notes_dir = project_dir / "project-notes"
    if not notes_dir.is_dir():
        return
    index_md = notes_dir / "index.md"
    content = read_text(index_md)
    if content is None:
        return
    indexed = {r["file"] for r in parse_notes_index_rows(content)}
    actual = {p.relative_to(notes_dir).as_posix() for p in walk_note_files(notes_dir)}
    for f in sorted(indexed - actual):
        result.add(
            "notes_index",
            "drift",
            str(index_md),
            f"index.md lists '{f}' but the file does not exist",
        )
    for f in sorted(actual - indexed):
        result.add(
            "notes_index",
            "drift",
            str(notes_dir / f),
            f"'{f}' exists but is not registered in index.md",
        )


def check_task_stale(project_dir: Path, result: Result, threshold_days: int) -> None:
    """#3 — applies only to active tasks (0_todo / 1_in_progress).

    Completed (2_done) tasks naturally age and would always go stale; skip them.
    """
    today = datetime.date.today()
    for task_path, status in walk_task_files(project_dir):
        if status == "2_done":
            continue
        content = read_text(task_path)
        if content is None:
            continue
        fm = parse_frontmatter(content)
        if not fm:
            continue
        d = parse_date(fm.get("updated"))
        if d is None:
            continue
        age = (today - d).days
        if age > threshold_days:
            result.add(
                "stale",
                "stale",
                str(task_path),
                f"'updated: {d}' is {age} days old (threshold: {threshold_days})",
            )


def check_project_marker(project_dir: Path, result: Result) -> None:
    """#4"""
    projects_root = project_dir.parent
    index_md = projects_root / "index.md"
    content = read_text(index_md)
    if content is None:
        return
    name = project_dir.name
    if name not in content:
        result.add(
            "project_registration",
            "violation",
            str(index_md),
            f"project '{name}' is not listed in _projects/index.md",
        )


def check_task_body_links(project_dir: Path, result: Result) -> None:
    """#5"""
    for task_path, _ in walk_task_files(project_dir):
        content = read_text(task_path)
        if content is None:
            continue
        body = FRONTMATTER_RE.sub("", content, count=1)
        for m in TASK_REF_RE.finditer(body):
            ref = m.group(0)
            target = project_dir / ref.lstrip("@")
            if not target.exists():
                result.add(
                    "task_body_link",
                    "drift",
                    str(task_path),
                    f"broken reference: {ref}",
                )
        for m in NOTES_REF_RE.finditer(body):
            rel = m.group(1)
            target = project_dir / "project-notes" / rel
            if not target.exists():
                result.add(
                    "task_body_link",
                    "drift",
                    str(task_path),
                    f"broken reference: @project-notes/{rel}",
                )


def check_progress_summary_h1_sync(project_dir: Path, result: Result) -> None:
    """#6"""
    progress = project_dir / "progress.md"
    content = read_text(progress)
    if content is None:
        return
    for row in parse_progress_table_rows(content):
        ref = row["ref"]
        rel = ref.lstrip("@")
        target = project_dir / rel
        if not target.exists():
            continue
        task_content = read_text(target)
        if task_content is None:
            continue
        h1 = extract_h1(task_content)
        if h1 is None:
            continue
        cells_text = " ".join(row["cells"])
        if h1 not in cells_text:
            result.add(
                "summary_h1",
                "drift",
                str(progress),
                f"row for {ref} does not contain task H1 '{h1}'; "
                "run `/progress rebuild` to sync",
            )


def check_filename_convention(project_dir: Path, result: Result) -> None:
    """#7"""
    for task_path, _ in walk_task_files(project_dir):
        if not TASK_FILENAME_RE.match(task_path.name):
            result.add(
                "filename",
                "violation",
                str(task_path),
                f"filename '{task_path.name}' violates "
                "<YYYY-MM-DD>_<topic>(-<N>)?.md",
            )


def check_pending_approval(project_dir: Path, result: Result, threshold_days: int) -> None:
    """#8"""
    today = datetime.date.today()
    for task_path, status in walk_task_files(project_dir):
        if status != "1_in_progress":
            continue
        content = read_text(task_path)
        d: datetime.date | None = None
        if content:
            fm = parse_frontmatter(content)
            if fm:
                d = parse_date(fm.get("updated"))
        if d is None:
            d = datetime.date.fromtimestamp(task_path.stat().st_mtime)
        age = (today - d).days
        if age > threshold_days:
            result.add(
                "pending_approval",
                "approval",
                str(task_path),
                f"in 1_in_progress/ for {age} days "
                "— if complete, run `/progress approve <id>`",
            )


def check_orphan_lock(project_dir: Path, result: Result) -> None:
    """#9 — report *.md.lock files that have no sibling *.md in the same directory.

    Does NOT delete lock files (report-only). Auto-deletion would break the
    mutual exclusion invariant INV-2 by introducing an unlink race.
    """
    tasks_dir = project_dir / "tasks"
    if not tasks_dir.is_dir():
        return
    for status in TASK_STATUSES:
        sub = tasks_dir / status
        if not sub.is_dir():
            continue
        for lock_path in sorted(sub.iterdir()):
            if lock_path.suffix != ".lock":
                continue
            # Require the name to end with .md.lock
            if not lock_path.name.endswith(".md.lock"):
                continue
            sibling_name = lock_path.name[: -len(".lock")]  # strip trailing .lock
            sibling = lock_path.parent / sibling_name
            if not sibling.exists():
                result.add(
                    "orphan_lock",
                    "drift",
                    str(lock_path),
                    f"orphan lock: {lock_path.relative_to(project_dir).as_posix()}",
                )


def check_duplicate_basename(project_dir: Path, result: Result) -> None:
    """#10 — same task-md basename in >=2 locations anywhere under tasks/.

    Walk range MUST mirror hooks/session_progress_capture.py::_task_basename_index
    (os.walk of the WHOLE tasks/ tree, case-insensitive .md, keyed by exact
    basename with last-writer-wins). A duplicate basename there is silently
    collapsed, so capture may bind the wrong copy — this deterministically
    executes the uniqueness invariant. LOCKSTEP: change this walk range and
    _task_basename_index's together.
    """
    tasks_dir = project_dir / "tasks"
    if not tasks_dir.is_dir():
        return
    index: dict[str, list[Path]] = {}
    for dirpath, _dirs, files in os.walk(tasks_dir):
        for fn in files:
            if fn.lower().endswith(".md"):
                index.setdefault(fn, []).append(Path(dirpath) / fn)
    for base in sorted(index):
        paths = index[base]
        if len(paths) < 2:
            continue
        locs = ", ".join(
            p.relative_to(project_dir).as_posix() for p in sorted(paths)
        )
        result.add(
            "duplicate_basename",
            "violation",
            str(sorted(paths)[0]),
            f"task basename '{base}' occurs in {len(paths)} locations under "
            f"tasks/ ({locs}); _task_basename_index keeps only one "
            "(last-writer-wins) so capture may bind the wrong copy — "
            "rename or remove the duplicates",
        )


# ============================================================================
# Output
# ============================================================================

SEVERITY_ORDER = {"drift": 0, "violation": 1, "stale": 2, "approval": 3}
SEVERITY_LABEL = {
    "drift": "DRIFT",
    "violation": "VIOLATION",
    "stale": "STALE",
    "approval": "PENDING",
}


def print_findings(result: Result) -> None:
    if not result.findings:
        print("OK: no drift, no violations, no stale tasks.")
        return
    by_sev: dict[str, list[Finding]] = {}
    for f in result.findings:
        by_sev.setdefault(f.severity, []).append(f)
    total = len(result.findings)
    print(f"FINDINGS: {total}")
    print()
    for sev in sorted(by_sev.keys(), key=lambda s: SEVERITY_ORDER.get(s, 99)):
        items = by_sev[sev]
        label = SEVERITY_LABEL.get(sev, sev.upper())
        print(f"=== {label} ({len(items)}) ===")
        for f in items:
            print(f"  [{f.check}] {f.path}")
            print(f"    {f.message}")
        print()


# ============================================================================
# Main
# ============================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect a taskflow v0.2.2 project for drift, stale tasks, "
            "and rule violations."
        )
    )
    parser.add_argument(
        "project_dir",
        type=Path,
        help="Path to _projects/<project>/ directory",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=14,
        help="Threshold in days for stale-task warnings (default: 14)",
    )
    args = parser.parse_args(argv)

    project_dir: Path = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return 2

    result = Result()
    check_progress_task_links(project_dir, result)
    check_notes_index_consistency(project_dir, result)
    check_task_stale(project_dir, result, args.stale_days)
    check_project_marker(project_dir, result)
    check_task_body_links(project_dir, result)
    check_progress_summary_h1_sync(project_dir, result)
    check_filename_convention(project_dir, result)
    check_pending_approval(project_dir, result, args.stale_days)
    check_orphan_lock(project_dir, result)
    check_duplicate_basename(project_dir, result)

    print_findings(result)
    return 1 if result.findings else 0


if __name__ == "__main__":
    sys.exit(main())
