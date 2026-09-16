#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Backfill taskflow project-notes frontmatter via claude -p.

Usage:
    uv run --script plugins/taskflow/scripts/backfill_notes.py <workspace> [--apply]

<workspace> is the directory holding _projects/. Notes live at
_projects/<project>/project-notes/<category>/<name>.md and their index at
_projects/<project>/project-notes/index.md.

Stage 1 asks claude for summary/anchors/description per note and writes a
manifest. Stage 2 (--apply) validates the manifest and edits the files.
Without --apply nothing is written except the manifest.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")

# Contract limits — plugins/taskflow/prompts/notes_guidelines.md
SUMMARY_MAX = 500          # code points, frontmatter summary:
DESC_MAX = 100             # code points, index.md Description cell
NOTES_DIR = "project-notes"
INDEX_NAME = "index.md"
ARCHIVE = "_archive"       # non-authoritative; never backfilled
CATEGORIES = ("specs", "investigations", "checks", "procedures", "backlog")

BATCH = 8                  # notes per claude call; 85 notes went out in 6 parts
BODY_CHARS = 6000          # per-note body slice handed to the model
CALL_TIMEOUT_S = 600
MODEL = "sonnet"           # pinned: the judgment is per-note and bounded

FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
ANCHOR_OK_RE = re.compile(r"^[^\s,\[\]]+$")

PROMPT = """You are backfilling frontmatter for a taskflow project-notes corpus.

For EACH note below return one JSON object. Output a single JSON array and
nothing else — no prose, no code fence.

{{
  "file": "<copied verbatim from the note's File: line>",
  "summary": "<one paragraph, at most {summary_max} characters, that lets a
               reader decide whether to open the body. State what the note
               settles, not that it exists. No markdown, no bullet list,
               single line.>",
  "description": "<at most {desc_max} characters for the index listing. It
               exists so the note can be FOUND, so lead with the distinctive
               nouns. Not a truncation of summary.>",
  "anchors": ["<repo-relative / -separated path the note is ABOUT>", ...]
}}

anchors rules:
- Only code/files the note is ABOUT, not every path it mentions. Usually 0-4.
- Use paths you actually see named in the note body. Do not invent them.
- A member may be written "path::Class.method".
- Return [] when the note is about a decision or process rather than code.
  An empty list is a correct answer; a wrong path is not.

Write summary and description in the language the note body uses.

=== NOTES ===
{notes}
"""


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def find_claude() -> str:
    """Absolute path to a launchable claude binary.

    On Windows the `claude` on PATH is a shell shim that CreateProcess cannot
    start, so a bare name fails under shell=False. Resolve the real binary.
    """
    override = os.environ.get("CLAUDE_BIN")
    if override:
        return override

    if os.name != "nt":
        found = shutil.which("claude")
        if not found:
            die("claude not on PATH; set CLAUDE_BIN")
        return found

    found = shutil.which("claude.exe")
    if found:
        return found

    roots = [Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules",
             Path(os.environ.get("ProgramFiles", "")) / "nodejs" / "node_modules"]
    for root in roots:
        cand = root / "@anthropic-ai" / "claude-code"
        if cand.is_dir():
            for exe in cand.rglob("claude.exe"):
                return str(exe)

    die("claude.exe not found; set CLAUDE_BIN to its absolute path")


def read_note(path: Path) -> tuple[str, str]:
    """(text with \\n newlines, the newline the file actually uses)."""
    raw = path.read_bytes()
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    nl = "\r\n" if crlf and not lf else "\n"
    return raw.decode("utf-8").replace("\r\n", "\n"), nl


def write_note(path: Path, text: str, nl: str) -> None:
    path.write_bytes(text.replace("\n", nl).encode("utf-8"))


def frontmatter(text: str) -> tuple[dict, str, str]:
    """(parsed mapping, raw frontmatter block, body-after)."""
    m = FM_RE.match(text)
    if not m:
        return {}, "", text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        data = {}
    return (data if isinstance(data, dict) else {}), m.group(1), text[m.end():]


def walk_notes(notes_dir: Path) -> list[Path]:
    """Every .md under project-notes/ except index.md and _archive/."""
    out = []
    for p in sorted(notes_dir.rglob("*.md")):
        rel = p.relative_to(notes_dir).as_posix()
        if p.name == INDEX_NAME or rel.startswith(ARCHIVE + "/"):
            continue
        out.append(p)
    return out


def needs_work(fm: dict, desc: str | None) -> bool:
    if not str(fm.get("summary", "")).strip():
        return True
    if len(str(fm.get("summary", ""))) > SUMMARY_MAX:
        return True
    if "anchors" not in fm:
        return True
    if desc is None or len(desc) > DESC_MAX:
        return True
    return False


def parse_index(text: str) -> dict[str, list[str]]:
    """{file -> [File, Description, Tags, Updated]} for 4-column data rows."""
    rows = {}
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|") or set(s) <= set("|- "):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4 or cells[0] in ("File", ""):
            continue
        rows[cells[0]] = cells
    return rows


def call_claude(binary: str, prompt: str, log: Path) -> list[dict]:
    """One batch. The child gets no tools: it only rewrites text we hand it."""
    cmd = [binary, "-p", "--session-id", str(uuid.uuid4()),
           "--allowedTools", "", "--model", MODEL]

    cp = subprocess.run(cmd, input=prompt, capture_output=True,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=CALL_TIMEOUT_S)
    log.write_text(cp.stdout + "\n--- stderr ---\n" + cp.stderr, encoding="utf-8")

    if "\ufffd" in cp.stdout:
        print("  warn: U+FFFD in child output; text was not valid UTF-8")
    if cp.returncode != 0:
        die(f"claude exit {cp.returncode}; see {log}")

    start, end = cp.stdout.find("["), cp.stdout.rfind("]")
    if start < 0 or end < start:
        print(f"  warn: no JSON array in claude output; see {log}")
        return []
    try:
        parsed = json.loads(cp.stdout[start:end + 1])
    except json.JSONDecodeError:
        print(f"  warn: claude output was not valid JSON; see {log}")
        return []
    return [r for r in parsed if isinstance(r, dict)]


def build_prompt(batch: list[tuple[str, str]]) -> str:
    blocks = []
    for rel, text in batch:
        blocks.append(f"--- File: {rel}\n{text[:BODY_CHARS]}\n")
    return PROMPT.format(summary_max=SUMMARY_MAX, desc_max=DESC_MAX,
                         notes="\n".join(blocks))


def validate(rec: dict, workspace: Path) -> tuple[dict, list[str]]:
    """Clamp to the contract and drop anchors that do not resolve."""
    problems = []
    summary = " ".join(str(rec.get("summary", "")).split())
    desc = " ".join(str(rec.get("description", "")).split())

    if len(summary) > SUMMARY_MAX:
        problems.append(f"summary {len(summary)} > {SUMMARY_MAX}, truncated")
        summary = summary[:SUMMARY_MAX]
    if len(desc) > DESC_MAX:
        problems.append(f"description {len(desc)} > {DESC_MAX}, truncated")
        desc = desc[:DESC_MAX]

    anchors = []
    for a in rec.get("anchors") or []:
        a = str(a).strip()
        if not ANCHOR_OK_RE.match(a):
            problems.append(f"anchor dropped (bad shape): {a!r}")
            continue
        if not (workspace / a.split("::", 1)[0]).exists():
            problems.append(f"anchor dropped (no such path): {a}")
            continue
        anchors.append(a)

    return {"summary": summary, "description": desc, "anchors": anchors}, problems


def splice(text: str, rec: dict) -> str:
    """Add summary:/anchors: without reformatting the rest of the frontmatter.

    Values go in as a YAML block scalar and a flow sequence, never as a
    replacement string: note text is arbitrary and a regex replacement would
    expand $&, \\1 and friends inside it.
    """
    fm, raw, body = frontmatter(text)
    lines = raw.splitlines() if raw else []
    keep = []
    skipping = False
    for ln in lines:
        if re.match(r"^(summary|anchors):", ln):
            skipping = True
            continue
        if skipping and re.match(r"^(\s+|-)", ln):
            continue
        skipping = False
        keep.append(ln)

    if rec["summary"]:
        keep.append("summary: >-")
        keep.append("  " + rec["summary"])
    keep.append("anchors: [" + ", ".join(rec["anchors"]) + "]")

    return "---\n" + "\n".join(keep) + "\n---\n" + (body if raw else "\n" + text)


def apply_index(notes_dir: Path, updates: dict[str, str]) -> int:
    """Rewrite Description cells in place. Returns rows changed."""
    idx = notes_dir / INDEX_NAME
    text, nl = read_note(idx)
    out, changed = [], 0
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) >= 4 and cells[0] in updates:
                cells[1] = updates[cells[0]].replace("|", r"\|")
                line = "| " + " | ".join(cells[:4]) + " |"
                changed += 1
        out.append(line)
    write_note(idx, "\n".join(out) + "\n", nl)
    return changed


def run_project(project: Path, workspace: Path, args, out_dir: Path) -> None:
    notes_dir = project / NOTES_DIR
    if not notes_dir.is_dir():
        return

    idx_path = notes_dir / INDEX_NAME
    index_rows = parse_index(idx_path.read_text(encoding="utf-8")) if idx_path.is_file() else {}

    todo = []
    for path in walk_notes(notes_dir):
        rel = path.relative_to(notes_dir).as_posix()
        text, _ = read_note(path)
        fm, _, _ = frontmatter(text)
        row = index_rows.get(rel)
        if needs_work(fm, row[1] if row else None):
            todo.append((rel, text))

    print(f"\n=== {project.name}: {len(todo)} note(s) need backfill "
          f"of {len(walk_notes(notes_dir))} active ===")
    if not todo:
        return

    binary = find_claude()
    manifest: dict[str, dict] = {}
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        part = i // BATCH + 1
        print(f"  part {part}: {len(batch)} note(s)")
        log = out_dir / f"{project.name}-part{part}.log"
        for rec in call_claude(binary, build_prompt(batch), log):
            f = str(rec.get("file", "")).strip()
            if f in dict(batch):
                manifest[f] = rec
            else:
                print(f"    warn: unknown file in reply: {f!r}")

    missing = [rel for rel, _ in todo if rel not in manifest]
    if missing:
        print(f"  warn: no reply for {len(missing)}: {missing[:5]}")

    clean, all_problems = {}, []
    for rel, rec in manifest.items():
        ok, problems = validate(rec, workspace)
        clean[rel] = ok
        all_problems += [f"{rel}: {p}" for p in problems]

    man_path = out_dir / f"{project.name}-manifest.json"
    man_path.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  manifest: {man_path}")
    for p in all_problems:
        print(f"  fixed up: {p}")

    if not args.apply:
        print("  DRY RUN — nothing written. Re-run with --apply.")
        return

    backup = out_dir / "backup" / project.name
    for rel, rec in clean.items():
        src = notes_dir / rel
        dst = backup / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        text, nl = read_note(src)
        write_note(src, splice(text, rec), nl)

    rows = apply_index(notes_dir, {r: v["description"] for r, v in clean.items()
                                   if v["description"]})
    print(f"  applied: {len(clean)} note(s), {rows} index row(s)")
    print(f"  backup:  {backup}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workspace", type=Path, help="directory containing _projects/")
    ap.add_argument("--apply", action="store_true", help="write; default is dry run")
    args = ap.parse_args()

    workspace = args.workspace.resolve()
    projects_root = workspace / "_projects"
    if not projects_root.is_dir():
        die(f"no _projects/ under {workspace}")

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = workspace / "tmp" / "notes-backfill" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"workspace: {workspace}\noutput:    {out_dir}")
    print("(add tmp/ to .gitignore if it is not already there)")

    names = sorted(p.name for p in projects_root.iterdir()
                   if p.is_dir() and not p.name.startswith("_"))

    for name in names:
        run_project(projects_root / name, workspace, args, out_dir)

    print("\nNow verify, per project:")
    print("  uv run --script <taskflow>/scripts/check_progress.py "
          f"{projects_root}/<project>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
