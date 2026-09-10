#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""backfill_notes_updated.py — one-shot: give every active project note an
`updated:` date (notes data contract, rollout step "updated: 補完").

Population: walk_note_files minus `_archive/` — the same set `/progress check`
holds to hygiene. Per note:
  - frontmatter present, `updated:` present  -> untouched
  - frontmatter present, `updated:` absent   -> `updated: <date>` appended as
                                                the last frontmatter line
  - no frontmatter                           -> a new block holding only
                                                `updated: <date>`

`<date>` is the file's mtime date — the closest deterministic proxy for
"last edited". Dry-run by default: a unified diff per file plus a summary.
`--apply` writes. Line endings are preserved per file (the inserted line
reuses the file's own ending), so a CRLF note stays CRLF.

Usage:
  uv run --script plugins/taskflow/scripts/backfill_notes_updated.py \
      _projects/<project> [--apply]

Exit codes: 0 = done (dry-run or applied), 2 = bad arguments.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_progress as cp  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

# Frontmatter with either line ending; group 1/3/4 capture the endings so the
# inserted line can reuse them, group 2 is the block body.
FM_RE = re.compile(r"^---(\r?\n)(.*?)(\r?\n)---(\r?\n)", re.DOTALL)
UPDATED_RE = re.compile(r"^updated:", re.MULTILINE)


def line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def rewrite(text: str, date: datetime.date) -> str | None:
    """New file text, or None when the note already carries `updated:`."""
    m = FM_RE.match(text)
    if m:
        block = m.group(2)
        if UPDATED_RE.search(block):
            return None
        nl = m.group(1)
        return text[: m.start(2)] + block + nl + f"updated: {date}" + text[m.end(2) :]
    nl = line_ending(text)
    return f"---{nl}updated: {date}{nl}---{nl}{nl}" + text


def mtime_date(p: Path) -> datetime.date:
    return datetime.date.fromtimestamp(p.stat().st_mtime)


def process(project_dir: Path, apply: bool) -> dict[str, int]:
    counts = {"untouched": 0, "updated_added": 0, "frontmatter_added": 0, "unreadable": 0}
    notes_dir = project_dir / "project-notes"
    for p in cp.walk_active_notes(notes_dir):
        try:
            text = p.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"SKIP (unreadable): {p} — {e}")
            counts["unreadable"] += 1
            continue

        new = rewrite(text, mtime_date(p))
        if new is None:
            counts["untouched"] += 1
            continue

        kind = "updated_added" if FM_RE.match(text) else "frontmatter_added"
        counts[kind] += 1
        rel = p.relative_to(notes_dir).as_posix()
        sys.stdout.writelines(
            difflib.unified_diff(
                text.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        if apply:
            p.write_bytes(new.encode("utf-8"))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("project_dir", type=Path, help="Path to _projects/<project>/")
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry-run)")
    args = parser.parse_args(argv)

    project_dir: Path = args.project_dir.resolve()
    if not (project_dir / "project-notes").is_dir():
        print(f"error: no project-notes/ under {project_dir}", file=sys.stderr)
        return 2

    counts = process(project_dir, args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(
        f"{mode}: updated_added={counts['updated_added']} "
        f"frontmatter_added={counts['frontmatter_added']} "
        f"untouched={counts['untouched']} unreadable={counts['unreadable']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
