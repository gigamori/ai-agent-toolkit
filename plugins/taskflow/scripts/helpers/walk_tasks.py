#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""walk_tasks.py — enumerate tasks/<status>/*.md and emit JSON.

Used by migration skill (Phase 4 / 9) to inspect task state after migration.

Output (JSON to stdout):
  [
    {
      "status": "0_todo",
      "path": "tasks/0_todo/2026-05-13_topic.md",
      "name": "2026-05-13_topic.md",
      "frontmatter": {...},
      "h1": "Topic title"
    },
    ...
  ]

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

import yaml

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
TASK_STATUSES = ("0_todo", "1_in_progress", "2_done")


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


def normalize_fm(fm: dict) -> dict:
    """Convert date objects to ISO strings for JSON serialization."""
    import datetime
    out = {}
    for k, v in fm.items():
        if isinstance(v, datetime.date):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enumerate tasks/<status>/*.md files and emit JSON."
    )
    parser.add_argument("project_dir", type=Path)
    args = parser.parse_args(argv)

    project_dir: Path = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return 2

    out: list[dict] = []
    tasks_dir = project_dir / "tasks"
    if tasks_dir.is_dir():
        for status in TASK_STATUSES:
            sub = tasks_dir / status
            if not sub.is_dir():
                continue
            for p in sorted(sub.iterdir()):
                if not p.is_file() or p.suffix != ".md":
                    continue
                content = read_text(p)
                if content is None:
                    continue
                fm = parse_frontmatter(content) or {}
                h1 = extract_h1(content) or p.stem
                out.append({
                    "status": status,
                    "path": p.relative_to(project_dir).as_posix(),
                    "name": p.name,
                    "frontmatter": normalize_fm(fm),
                    "h1": h1,
                })

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
