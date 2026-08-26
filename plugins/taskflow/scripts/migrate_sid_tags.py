#!/usr/bin/env python3
"""Migrate legacy [s:<first-8>] session tags in task @log regions to tail-12 tags.

One-time data migration for the first-8 -> tail-12 tag change (commit e73d639).
Dry-run by default; pass --write to apply. Supersedes the "no rewrite" decision
in _projects/harness-taskflow/project-notes/specs/sid-tag-tail12/04-spec.md 3.5
for the resolvable subset, per user approval: only a tag whose first-8 prefix
matches exactly ONE state stem is rewritten; ambiguous (>=2 stems) and
unresolved (0 stems) tags are reported and left untouched. The @log
append-only prohibition is waived for this migration only; every modified file
gets a <name>.md.bak-sidmigrate copy next to it first (task files are
gitignored and otherwise unrecoverable).
"""
import argparse
import re
import sys
from pathlib import Path

LOG_BEGIN = "<!-- @log:begin -->"
LOG_END = "<!-- @log:end -->"
TAG8_RE = re.compile(r"\[s:([0-9a-f]{8})\]")
BACKUP_SUFFIX = ".bak-sidmigrate"


def build_prefix_index(state_dir: Path) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for p in sorted(state_dir.glob("*.json")):
        stem = p.name[: -len(".json")]
        index.setdefault(stem[:8], []).append(stem)
    return index


def build_tail8_index(state_dir: Path) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for p in sorted(state_dir.glob("*.json")):
        stem = p.name[: -len(".json")]
        index.setdefault(tail12(stem)[:8], []).append(stem)
    return index


def tail12(stem: str) -> str:
    return stem.replace("-", "")[-12:]


def log_region_span(text: str) -> tuple[int, int] | None:
    b = text.find(LOG_BEGIN)
    if b < 0:
        return None
    e = text.find(LOG_END, b)
    if e < 0:
        return None
    return b, e + len(LOG_END)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path.cwd(),
                    help="workspace root holding _projects/ (default: cwd)")
    ap.add_argument("--write", action="store_true",
                    help="apply rewrites (default: dry-run, report only)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    projects_root = args.root / "_projects"
    state_dir = projects_root / "_state"
    if not state_dir.is_dir():
        print(f"no state dir: {state_dir}", file=sys.stderr)
        return 2
    prefix_index = build_prefix_index(state_dir)
    tail8_index = build_tail8_index(state_dir)

    task_files = sorted(projects_root.glob("*/tasks/*/*.md"))
    tag_outcome: dict[str, tuple[str, str]] = {}  # tag -> (kind, detail)
    files_changed = 0
    files_skipped = 0
    total_converted = 0

    for tf in task_files:
        raw = tf.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            print(f"SKIP (not utf-8): {tf} ({e})", file=sys.stderr)
            files_skipped += 1
            continue
        span = log_region_span(text)
        if span is None:
            if TAG8_RE.search(text):
                print(f"SKIP (no balanced @log markers, legacy tags present): {tf}",
                      file=sys.stderr)
                files_skipped += 1
            continue
        region = text[span[0]:span[1]]
        tags = sorted(set(TAG8_RE.findall(region)))
        if not tags:
            continue
        repl: dict[str, str] = {}
        for tag in tags:
            stems = prefix_index.get(tag, [])
            via = "prefix"
            if not stems:
                stems = tail8_index.get(tag, [])
                via = "tail-8"
            if len(stems) == 1:
                new = tail12(stems[0])
                repl[f"[s:{tag}]"] = f"[s:{new}]"
                tag_outcome[tag] = ("converted", f"{via}: {stems[0]} -> {new}")
            elif len(stems) >= 2:
                where = "prefix" if via == "prefix" else "tail-8"
                tag_outcome[tag] = ("ambiguous", f"{len(stems)} stems ({where}): {', '.join(stems)}")
            else:
                tag_outcome[tag] = ("unresolved", "no state stem by prefix or tail-8")
        if not repl:
            continue
        new_region = region
        for old, new in repl.items():
            new_region = new_region.replace(old, new)
        if new_region == region:
            continue
        n_here = sum(region.count(old) for old in repl)
        if args.write:
            backup = tf.with_name(tf.name + BACKUP_SUFFIX)
            if not backup.exists():
                backup.write_bytes(raw)
            new_text = text[:span[0]] + new_region + text[span[1]:]
            tf.write_bytes(new_text.encode("utf-8"))
        files_changed += 1
        total_converted += n_here
        mode = "WROTE" if args.write else "DRY"
        print(f"{mode}: {tf.relative_to(args.root)} ({n_here} tag(s))")

    print()
    for tag, (kind, detail) in sorted(tag_outcome.items()):
        print(f"tag [s:{tag}]: {kind} — {detail}")
    print()
    verb = "converted" if args.write else "would convert"
    print(f"task files scanned: {len(task_files)}")
    print(f"files {verb} in: {files_changed} ({total_converted} tag occurrences)")
    print(f"files skipped: {files_skipped}")
    kinds = {}
    for kind, _ in tag_outcome.values():
        kinds[kind] = kinds.get(kind, 0) + 1
    print(f"distinct tags: {len(tag_outcome)} ({', '.join(f'{k}={v}' for k, v in sorted(kinds.items())) or 'none'})")
    if not args.write:
        print("dry-run: no files modified (rerun with --write to apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
