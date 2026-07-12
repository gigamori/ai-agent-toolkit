#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""audit_progress.py — taskflow v0.2.2 reality auditor.

For each task file under tasks/<status>/*.md, classify by `## Next Steps`
section state and folder location:

  pending              — Next Steps non-empty → work remaining
  completion_candidate — Next Steps empty + folder 1_in_progress/ → approve ready
  untracked            — section missing → migration required (v0.2.0 legacy)
  clean                — Next Steps empty + folder 0_todo/ or 2_done/ → normal

Reads task files only. Does NOT scan session jsonl. See
project-notes/specs/progress-audit-design.md for the design.

Exit codes:
  0 = no actionable findings (or only clean) — equivalent to "OK"
  1 = actionable findings present (pending / completion_candidate / untracked)
  2 = script error (bad arguments, missing project dir)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
NEXT_STEPS_SECTION_RE = re.compile(
    r"^##\s+Next Steps\s*$([\s\S]*?)(?=^##\s+|^<!--\s*@log:begin|\Z)",
    re.MULTILINE,
)
LOG_BLOCK_RE = re.compile(
    r"<!--\s*@log:begin\s*-->(.*?)<!--\s*@log:end\s*-->",
    re.DOTALL,
)
LOG_SID_RE = re.compile(r"\[s:([0-9a-f]{6,16})\]")

TASK_STATUSES = ("0_todo", "1_in_progress", "2_done")

BUCKET_ORDER = ("pending", "completion_candidate", "untracked", "clean")
BUCKET_LABEL = {
    "pending": "PENDING",
    "completion_candidate": "COMPLETION CANDIDATE",
    "untracked": "UNTRACKED",
    "clean": "CLEAN",
}


@dataclass
class Finding:
    bucket: str
    status: str            # folder name: 0_todo / 1_in_progress / 2_done
    path: Path
    next_steps_lines: list[str] = field(default_factory=list)
    last_session: str | None = None


def read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def strip_frontmatter(content: str) -> str:
    return FRONTMATTER_RE.sub("", content, count=1)


def extract_next_steps(body: str) -> tuple[bool, list[str]]:
    """Return (section_present, non_empty_content_lines).

    A line is non-empty if it contains any visible character after strip.
    A placeholder comment like `<!-- migration: ... -->` counts as non-empty
    (it is an intentional pending marker per migration design §6).
    """
    m = NEXT_STEPS_SECTION_RE.search(body)
    if not m:
        return False, []
    section_body = m.group(1)
    lines = [ln.rstrip() for ln in section_body.splitlines() if ln.strip()]
    return True, lines


def extract_last_session_id(body: str) -> str | None:
    m = LOG_BLOCK_RE.search(body)
    if not m:
        return None
    sids = LOG_SID_RE.findall(m.group(1))
    return sids[-1] if sids else None


def classify(status: str, present: bool, lines: list[str]) -> str:
    # 2_done is terminal — always clean regardless of section presence.
    # The `/progress approve` flow clears Next Steps on move, but legacy
    # 2_done files predating that change may still carry content or lack
    # the section; treat them as clean in either case.
    if status == "2_done":
        return "clean"
    if not present:
        return "untracked"
    if lines:
        return "pending"
    if status == "1_in_progress":
        return "completion_candidate"
    return "clean"


def walk_tasks(project_dir: Path):
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


def audit(project_dir: Path) -> list[Finding]:
    findings: list[Finding] = []
    for task_path, status in walk_tasks(project_dir):
        content = read_text(task_path) or ""
        body = strip_frontmatter(content)
        present, lines = extract_next_steps(body)
        sid = extract_last_session_id(body)
        bucket = classify(status, present, lines)
        findings.append(Finding(bucket, status, task_path, lines, sid))
    return findings


def render_finding(
    n: int,
    f: Finding,
    project_dir: Path,
    pending_steps_limit: int = 5,
) -> list[str]:
    rel = f.path.relative_to(project_dir).as_posix()
    out = [f"  [{n}] {rel}"]
    if f.bucket == "pending":
        shown = f.next_steps_lines[:pending_steps_limit]
        out.append("      Next Steps:")
        for sl in shown:
            out.append(f"        {sl}")
        extra = len(f.next_steps_lines) - len(shown)
        if extra > 0:
            out.append(f"        ... ({extra} more)")
        if f.last_session:
            out.append(f"      last session: {f.last_session}")
    elif f.bucket == "completion_candidate":
        out.append("      Next Steps: (empty)")
        if f.last_session:
            out.append(f"      last session: {f.last_session}")
        out.append(f"      → suggest: /progress approve {f.path.stem}")
    elif f.bucket == "untracked":
        out.append("      `## Next Steps` section missing")
        out.append("      → suggest: add a `## Next Steps` section")
    else:  # clean
        if f.last_session:
            out.append(f"      (clean, last session: {f.last_session})")
    return out


def format_output(
    findings: list[Finding],
    project_dir: Path,
    all_flag: bool,
    limit: int,
) -> str:
    counts = {b: 0 for b in BUCKET_ORDER}
    by_bucket: dict[str, list[Finding]] = {b: [] for b in BUCKET_ORDER}
    for f in findings:
        counts[f.bucket] += 1
        by_bucket[f.bucket].append(f)

    actionable_total = (
        counts["pending"] + counts["completion_candidate"] + counts["untracked"]
    )

    lines: list[str] = []
    lines.append(f"project: {project_dir.name}")
    lines.append(
        f"findings: {actionable_total} "
        f"(pending={counts['pending']}, "
        f"completion_candidate={counts['completion_candidate']}, "
        f"untracked={counts['untracked']}, "
        f"clean={counts['clean']})"
    )
    lines.append("")

    n = 0
    for bucket in BUCKET_ORDER:
        items = by_bucket[bucket]
        if not items:
            continue
        if bucket == "clean" and not all_flag:
            continue
        lines.append(f"=== {BUCKET_LABEL[bucket]} ({len(items)}) ===")
        show_count = len(items) if all_flag else min(len(items), limit)
        for item in items[:show_count]:
            n += 1
            lines.extend(render_finding(n, item, project_dir))
        if not all_flag and len(items) > show_count:
            lines.append(
                f"  ... ({len(items) - show_count} more in this bucket — use --all)"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit a taskflow v0.2.2 project for pending Next Steps, "
            "completion candidates, and untracked legacy tasks."
        )
    )
    parser.add_argument(
        "project_dir",
        type=Path,
        help="Path to _projects/<project>/ directory",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="List all findings including clean tasks; do not cap per-bucket count",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Per-bucket display cap when --all is not set (default: 10)",
    )
    args = parser.parse_args(argv)

    project_dir: Path = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return 2

    findings = audit(project_dir)

    if not findings:
        print(f"OK: {project_dir.name} — no tasks found.")
        return 0

    actionable = sum(1 for f in findings if f.bucket != "clean")
    if actionable == 0 and not args.all:
        print(f"OK: {project_dir.name} — no pending, no candidates, no untracked.")
        return 0

    print(format_output(findings, project_dir, args.all, args.limit))
    return 1 if actionable > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
