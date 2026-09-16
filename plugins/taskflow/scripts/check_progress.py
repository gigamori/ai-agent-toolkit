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
  9. orphan lock         — dead .locks/*.lock (no such task md) + legacy
                           tasks/<status>/*.md.lock (report-only; clear with
                           scripts/clean_locks.py)
  10. duplicate basename — same task-md basename in >=2 locations under tasks/ (whole-tree walk)
  11. notes index arity  — project-notes/index.md data rows must have exactly 4
                           columns (File | Description | Tags | Updated)
  12. duplicate @table   — progress.md must hold exactly 1 @table region
  13. notes desc length  — project-notes/index.md Description must be ≤ 100
                           chars
  14. notes nested dir   — a directory directly under one of the 5 active
                           note categories (layout is flat; _archive/ exempt)
  15. notes summary len  — frontmatter `summary:` must be ≤ 500 code points
  16. notes anchor       — every `anchors:` entry is a repo-relative `path` or
                           `path::symbol` that resolves (path: filesystem;
                           symbol: query-lsp snapshot, --lsp-snapshot)

Always-on footer lines (never findings, never affect the exit code):
  notes without summary: n of N        (N excludes _archive/)
  anchor symbols unchecked: n (...)    only when #16 had no snapshot to use
  near-duplicate note pairs: n         cross-category slug-core overlap; the
                                       pairs print under it. Merging duplicates
                                       was ruled out, so this can never become a
                                       finding: it would never clear.
  vector coverage: n of N docs         embed progress read from `qmd status`;
                                       printed only where project-notes/.qmd/
                                       holds a local qmd index and qmd answers.

Note: progress.md's Completed table lists every task in tasks/2_done/ — the
file is never truncated (context-side truncation is view_progress.py's job and
never touches the file). Checks #1 and #6 are forward-direction only (table row
-> file): a task file with no matching table row is not flagged here;
regenerating the table is `/progress rebuild`'s job.

Exit codes:
  0 = no findings
  1 = findings present
  2 = script error (bad arguments, missing project dir)
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
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

TABLE_BEGIN = "<!-- @table:begin -->"
TASK_STATUSES = ("0_todo", "1_in_progress", "2_done")
NOTES_DESC_MAX = 100
NOTES_SUMMARY_MAX = 500
NOTES_FLAT_CATEGORIES = ("specs", "investigations", "checks", "procedures", "backlog")
ANCHOR_SYMBOL_SEP = "::"
# Two notes covering one fact under different categories (a spec and the
# handoff that transcribed it) are invisible to every other check: both files
# exist, both are indexed, neither is wrong. Calibrated on ONE measured pair, so
# the false-positive rate over a real corpus is unknown - hence footer, not
# finding.
NEAR_DUP_JACCARD_MIN = 0.6
NEAR_DUP_ROLE_PREFIXES = ("sibling-handoff-", "handoff-")
NEAR_DUP_DATE_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
# "advisory" while a corpus is being backfilled; "violation" once its anchors
# are in place (harness-taskflow backfilled 2026-09-10).
ANCHOR_DANGLING_SEVERITY = "violation"
# query-lsp's snapshot location, relative to the repo root (parent of _projects/).
LSP_SNAPSHOT_REL = Path(".lsp-cache") / "snapshot.json"
# qmd resolution is DUPLICATED from hooks/notes_push.py (resolve_qmd_bin,
# BIN_ENV, QMD_REL_BIN) rather than imported, the way pj_rules.py duplicates
# session_init.py: scripts/ and hooks/ are standalone. notes_push.py is the
# canonical - change it there first.
QMD_BIN_ENV = "TASKFLOW_QMD_BIN"
QMD_REL_BIN = os.path.join("node_modules", "@tobilu", "qmd", "bin", "qmd")
QMD_DIR = ".qmd"
# A local index exists only where qmd's own findLocalConfigPath would stop:
# .qmd/index.yaml (or .yml). Without one, qmd walks UP and reports an ancestor's
# or the global ~/.cache index - someone else's numbers under this project's name.
QMD_LOCAL_CONFIGS = ("index.yaml", "index.yml")
# `qmd status` is one user-invoked sqlite read, not the per-turn query path, so
# it does not share TASKFLOW_QMD_TIMEOUT (a contract constant with the Pi
# extension). Any overrun prints no footer line and never fails the check.
QMD_STATUS_TIMEOUT_S = 10.0
QMD_TOTAL_RE = re.compile(r"^\s*Total:\s+(\d+)\b", re.MULTILINE)
QMD_PENDING_RE = re.compile(r"^\s*(?:\x1b\[[0-9;]*m)?Pending:\s+(\d+)\b", re.MULTILINE)
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
    if isinstance(value, datetime.datetime):
        return value.date()
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
    """Canonical note-set definition: rglob("*.md") minus any file named
    index.md (including nested ones, e.g. investigations/<x>/index.md).

    Consumer (LOCKSTEP): view_progress.py::build_notes_summary imports this
    function directly rather than re-implementing the walk, so the router's
    `--notes-summary` counts can never silently drift from this checker's
    notion of what counts as a note. Signature changes here are a public API
    change for that caller."""
    if not notes_dir.is_dir():
        return
    for p in sorted(notes_dir.rglob("*.md")):
        if p.name == "index.md":
            continue
        yield p


def parse_progress_table_rows(progress_md: str) -> list[dict]:
    """Extract task ref rows from progress.md table region.

    LOCKSTEP: the section-heading regex below is shared with
    view_progress.py::SECTION_RE, the titles emitted by
    rebuild_progress.py::render_section, and hooks/progress_digest.py's own
    copy of SECTION_RE. Change all four together.
    """
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
                # rebuild_progress.py::escape_cell escapes literal "|" in cell
                # content as "\|" so it isn't mistaken for a column separator.
                # Split on unescaped "|" only, then unescape each cell, or a
                # cell containing "|" (e.g. an H1 like "wiki:on|off") splits
                # into extra spurious cells and the pipe itself is lost.
                cells = [
                    c.strip().replace("\\|", "|")
                    for c in re.split(r"(?<!\\)\|", line.strip("|"))
                ]
                rows.append({
                    "section": section,
                    "ref": ref_m.group(0),
                    "cells": cells,
                    "raw": line,
                })
    return rows


def parse_notes_index_rows(index_md: str) -> list[dict]:
    """Consumer (LOCKSTEP): view_progress.py::build_notes_summary imports this
    function to compute the index-drift counts in `--notes-summary`, rather
    than re-implementing the table parse. Signature changes here are a public
    API change for that caller."""
    rows: list[dict] = []
    for line in index_md.splitlines():
        if not line.startswith("|"):
            continue
        stripped = line.strip("|")
        # Same rule as parse_progress_table_rows above: a literal "|" in cell
        # content is written "\|" so it isn't mistaken for a column separator.
        # Split on unescaped "|" only, then unescape each cell, or a cell
        # containing "|" (e.g. a Description mentioning "wiki:on|off") splits
        # into extra spurious cells and the pipe itself is lost.
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", stripped)]
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


def notes_index_row_arity(cells: list[str]) -> int:
    """Effective column count of a row returned by parse_notes_index_rows.

    parse_notes_index_rows splits what is left after `line.strip("|")`, and
    str.strip("|") is a CHARACTER strip: it cannot remove the closing "|" when
    any whitespace follows it. So '| a | b | c | d | ' (one trailing space,
    invisible in the rendered table) yields a 5th, empty cell. Drop at most one
    such trailing empty cell before counting.

    Deliberately NOT fixed inside parse_notes_index_rows: that function is a
    byte-faithful sibling of parse_progress_table_rows and a LOCKSTEP public API
    for view_progress.py::build_notes_summary, so changing its output would
    silently change a second consumer's.
    """
    if len(cells) > 1 and cells[-1] == "":
        return len(cells) - 1
    return len(cells)


def check_notes_index_consistency(project_dir: Path, result: Result) -> None:
    """#2 (index rows vs actual files) + #11 (row arity).

    Header and separator rows need no special handling here:
    parse_notes_index_rows already drops them (a leading cell of "File", or one
    made only of "-"/" "), so every row it returns is a data row.
    """
    notes_dir = project_dir / "project-notes"
    if not notes_dir.is_dir():
        return
    index_md = notes_dir / "index.md"
    content = read_text(index_md)
    if content is None:
        return
    rows = parse_notes_index_rows(content)
    for row in rows:
        arity = notes_index_row_arity(row["cells"])
        if arity != 4:
            result.add(
                "notes_index_arity",
                "violation",
                str(index_md),
                f"row '{row['file']}' has {arity} columns, expected 4 "
                f"(File | Description | Tags | Updated)",
            )
    indexed = {r["file"] for r in rows}
    actual = {p.relative_to(notes_dir).as_posix() for p in walk_note_files(notes_dir)}
    for f in sorted(indexed - actual):
        # An attachment row (a script or fixture that exists but is not a
        # `.md` note) is allowed in index.md and is not drift; only a row
        # whose file is gone is.
        if (notes_dir / f).is_file():
            continue
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


def check_notes_index_desc_length(project_dir: Path, result: Result) -> None:
    """#13 — project-notes/index.md Description cell must be <= NOTES_DESC_MAX
    chars (prompts/notes_guidelines.md, "## index.md format" and "## Prohibitions").

    Counted over the rendered cell (parse_notes_index_rows already unescapes
    "\\|"), not the raw markdown row.
    """
    notes_dir = project_dir / "project-notes"
    if not notes_dir.is_dir():
        return
    index_md = notes_dir / "index.md"
    content = read_text(index_md)
    if content is None:
        return
    for row in parse_notes_index_rows(content):
        cells = row["cells"]
        if len(cells) < 2:
            continue
        desc = cells[1]
        if len(desc) > NOTES_DESC_MAX:
            result.add(
                "notes_index_desc_len",
                "violation",
                str(index_md),
                f"row '{row['file']}' Description is {len(desc)} chars, "
                f"expected <= {NOTES_DESC_MAX}",
            )


def walk_active_notes(notes_dir: Path) -> Iterator[Path]:
    """walk_note_files minus `_archive/` — the population shared by every notes
    hygiene check and count below (#14 nested dir, #15 summary length, the
    coverage footer, #16 anchors). Archived material is a relocation target
    and is not held to ongoing hygiene."""
    for p in walk_note_files(notes_dir):
        if p.relative_to(notes_dir).as_posix().startswith("_archive/"):
            continue
        yield p


def check_notes_nested_dir(project_dir: Path, result: Result) -> None:
    """#14 — layout is project-notes/<category>/*.md (flat). A directory
    directly under one of the 5 active categories is run-dir / scratch
    material that leaked in. Only a category's direct children are inspected,
    so a sibling of the categories (e.g. project-notes/.qmd/) is never in
    range. Reporting only: nothing here moves the directory.
    """
    notes_dir = project_dir / "project-notes"
    for category in NOTES_FLAT_CATEGORIES:
        cat_dir = notes_dir / category
        if not cat_dir.is_dir():
            continue
        for entry in sorted(cat_dir.iterdir()):
            if not entry.is_dir():
                continue
            result.add(
                "notes_nested_dir",
                "violation",
                str(entry),
                f"'project-notes/{category}/{entry.name}/' is a directory; layout "
                f"is project-notes/<category>/*.md (flat) — flatten its files into "
                f"{category}/ or move the directory out of project-notes/",
            )


def note_summary(content: str) -> str | None:
    """Frontmatter `summary:` value, or None when absent, blank, non-string, or
    when the frontmatter block does not parse (#15 only measures a summary it
    can actually read; an unreadable block counts as "no summary")."""
    fm = parse_frontmatter(content)
    if not fm:
        return None
    raw = fm.get("summary")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw


def collect_note_summaries(notes_dir: Path) -> list[tuple[Path, str | None]]:
    """One walk + parse per active note, shared by #15 and the coverage footer."""
    out: list[tuple[Path, str | None]] = []
    for p in walk_active_notes(notes_dir):
        content = read_text(p)
        out.append((p, note_summary(content) if content is not None else None))
    return out


def check_notes_summary_length(project_dir: Path, result: Result) -> None:
    """#15 — frontmatter `summary:` must be <= NOTES_SUMMARY_MAX code points
    (len() of a str). A missing summary is NOT a finding; coverage of that is
    the always-on footer's job (notes_summary_coverage).
    """
    notes_dir = project_dir / "project-notes"
    for p, summary in collect_note_summaries(notes_dir):
        if summary is None:
            continue
        n = len(summary)
        if n > NOTES_SUMMARY_MAX:
            result.add(
                "notes_summary_length",
                "violation",
                str(p),
                f"frontmatter summary is {n} chars, expected <= {NOTES_SUMMARY_MAX} "
                f"(read on demand, but meant to be readable without opening the body)",
            )


def notes_summary_coverage(project_dir: Path) -> tuple[int, int]:
    """Footer input: (notes without a summary, active notes). Reported on every
    run, separately from findings — a missing summary is not a violation and
    must never become one by living in the findings list."""
    summaries = collect_note_summaries(project_dir / "project-notes")
    return sum(1 for _, s in summaries if s is None), len(summaries)


def resolve_qmd_bin(env: dict | None = None) -> str | None:
    """Path of qmd's `bin/qmd` JS entry, or None. Never the `.cmd` shim: it is
    spawned as `node <bin/qmd>`."""
    env = os.environ if env is None else env
    explicit = env.get(QMD_BIN_ENV, "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    which = shutil.which("qmd", path=env.get("PATH"))
    if not which:
        return None
    cand = os.path.join(os.path.dirname(which), QMD_REL_BIN)
    return cand if os.path.isfile(cand) else None


def parse_qmd_status(stdout: str) -> tuple[int, int] | None:
    """Footer input from `qmd status`: (docs with usable vectors, docs indexed).

    `Total:` is the active document count. `Pending:` is the count of those docs
    whose vectors are missing or incomplete for the embed model in force, and it
    is omitted entirely at zero. `Vectors:` is deliberately NOT used - it counts
    chunks (789 for 82 docs on the reference corpus), not documents."""
    total_m = QMD_TOTAL_RE.search(stdout)
    if not total_m:
        return None
    total = int(total_m.group(1))
    pending_m = QMD_PENDING_RE.search(stdout)
    pending = int(pending_m.group(1)) if pending_m else 0
    if pending > total:
        return None
    return total - pending, total


def qmd_vector_coverage(project_dir: Path, env: dict | None = None,
                        runner=subprocess.run) -> tuple[int, int] | None:
    """Footer input: (n, N) from `qmd status`, or None to print no line at all.

    None covers every arm in which the number would be absent or wrong: no local
    qmd index, no resolvable binary, spawn failure, non-zero exit, timeout, and
    output the parser does not recognise. The check never fails on qmd."""
    notes_dir = project_dir / "project-notes"
    if not any((notes_dir / QMD_DIR / name).is_file() for name in QMD_LOCAL_CONFIGS):
        return None
    bin_path = resolve_qmd_bin(env)
    if not bin_path:
        return None
    try:
        cp = runner(["node", bin_path, "status"], cwd=str(notes_dir),
                    capture_output=True, timeout=QMD_STATUS_TIMEOUT_S,
                    encoding="utf-8", errors="replace")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    return parse_qmd_status(cp.stdout or "")


def slug_core(stem: str) -> frozenset[str]:
    """Token set of a note filename with its role prefix and trailing ISO date
    removed, so a spec and the handoff that transcribes it reduce to the same
    words."""
    s = stem.lower()
    for prefix in NEAR_DUP_ROLE_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = NEAR_DUP_DATE_SUFFIX_RE.sub("", s)
    return frozenset(t for t in s.split("-") if t)


def near_duplicate_pairs(project_dir: Path) -> list[tuple[str, str, float]]:
    """Footer input: (file a, file b, Jaccard) for notes in DIFFERENT categories
    whose slug cores overlap at NEAR_DUP_JACCARD_MIN or more, file order sorted.
    Same-category repetition is a naming convention, not a near-duplicate."""
    notes_dir = project_dir / "project-notes"
    entries: list[tuple[str, str, frozenset[str]]] = []
    for p in walk_note_files(notes_dir):
        rel = p.relative_to(notes_dir).as_posix()
        category = rel.split("/", 1)[0]
        if category == "_archive":
            continue
        core = slug_core(p.stem)
        if core:
            entries.append((rel, category, core))

    pairs: list[tuple[str, str, float]] = []
    for i, (rel_a, cat_a, core_a) in enumerate(entries):
        for rel_b, cat_b, core_b in entries[i + 1:]:
            if cat_a == cat_b:
                continue
            union = core_a | core_b
            if not union:
                continue
            j = len(core_a & core_b) / len(union)
            if j >= NEAR_DUP_JACCARD_MIN:
                lo, hi = sorted((rel_a, rel_b))
                pairs.append((lo, hi, round(j, 2)))
    return sorted(pairs)


def load_lsp_snapshot(path: Path) -> dict[str, list[dict]] | None:
    """The `files` map of a query-lsp snapshot, re-keyed by `/`-separated
    repo-relative path (the writer records str(Path.relative_to()), so keys
    carry the host separator). None when the file is absent or not a snapshot.
    """
    content = read_text(path)
    if content is None:
        return None
    try:
        snap = json.loads(content)
    except ValueError:
        return None
    files = snap.get("files") if isinstance(snap, dict) else None
    if not isinstance(files, dict):
        return None
    return {
        k.replace("\\", "/"): v.get("symbols", [])
        for k, v in files.items()
        if isinstance(v, dict)
    }


def snapshot_has_symbol(symbols: list[dict], name: str) -> bool:
    """Same resolution as query-lsp's `symbol` subcommand: `Container.member`
    matches on name + container, a bare name on name alone."""
    if "." in name:
        cont, _, base = name.rpartition(".")
        if any(s.get("name") == base and s.get("container") == cont for s in symbols):
            return True
    return any(s.get("name") == name for s in symbols)


def check_notes_anchor_dangling(
    project_dir: Path, result: Result, snapshot_path: Path | None
) -> int:
    """#16 — every `anchors:` entry must be a repo-relative `path` or
    `path::symbol` that resolves. Paths are tested against the filesystem
    (repo root = parent of _projects/ = project_dir's grandparent); symbols
    against a query-lsp snapshot.

    Returns how many symbol anchors could NOT be tested because no snapshot
    was given. The caller prints that count as a footer, so a missing snapshot
    never reads as a pass. Severity is ANCHOR_DANGLING_SEVERITY.
    """
    notes_dir = project_dir / "project-notes"
    repo_root = project_dir.parent.parent
    files = load_lsp_snapshot(snapshot_path) if snapshot_path is not None else None
    unchecked = 0

    def report(p: Path, message: str) -> None:
        result.add("notes_anchor_dangling", ANCHOR_DANGLING_SEVERITY, str(p), message)

    for p in walk_active_notes(notes_dir):
        content = read_text(p)
        fm = parse_frontmatter(content) if content is not None else None
        if not fm or "anchors" not in fm:
            continue
        anchors = fm["anchors"]
        if not isinstance(anchors, list):
            report(p, "anchors: must be a list of repo-relative 'path' or "
                      "'path::symbol' entries")
            continue

        for raw in anchors:
            if not isinstance(raw, str) or not raw.strip():
                report(p, f"anchors: entry {raw!r} is not a non-empty string")
                continue
            path, _, symbol = raw.strip().partition(ANCHOR_SYMBOL_SEP)
            path = path.strip().replace("\\", "/")
            symbol = symbol.strip()
            if not path or not (repo_root / path).exists():
                report(p, f"anchor '{raw}': path '{path}' does not exist under {repo_root}")
                continue
            if not symbol:
                continue
            if files is None:
                unchecked += 1
                continue
            symbols = files.get(path)
            if symbols is None:
                report(p, f"anchor '{raw}': '{path}' is not in the LSP snapshot "
                          f"({snapshot_path})")
                continue
            if not snapshot_has_symbol(symbols, symbol):
                report(p, f"anchor '{raw}': symbol '{symbol}' not found in '{path}' "
                          f"(LSP snapshot {snapshot_path})")
    return unchecked


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
    """#9 — report dead `@log` advisory-lock sidecars. Report-only.

    Two populations (project-notes/specs/log-lock-stable-key.md §3.3):

      dead    `.locks/<name>.lock` whose `<name>` matches no task md anywhere
              under tasks/. The task was deleted or renamed, so nothing can
              resolve to that key again. (hooks/log_lock.py deletes a sidecar
              when its last holder releases, so a LIVE task normally has no
              entry here at all.)
      legacy  `tasks/<status>/*.md.lock` — the pre-stable-key location.
              hooks/log_lock.py no longer creates these, so every one is dead
              regardless of whether a sibling `.md` exists.

    EXEMPT: `.locks/progress.md.lock`. Protocol v2 gave `progress.md` a sidecar
    in the same `.locks/` dir (hooks/log_lock.py `lock_path_for` rule 2), keyed
    on the file itself rather than on a task md — so the `dead` rule would flag
    it in every project, forever. A crash-orphaned one is reclaimed by the next
    writer's stale-break, not by a sweep.

    The previous definition of this check was "*.md.lock with no sibling *.md",
    which only ever caught the DEPARTURE-side residue of a status move; the
    arrival-side sidecar kept a live sibling and so looked healthy. That is
    why the population grew silently.

    Report-only rationale (corrected 2026-08-05): this check was originally
    report-only on the belief that auto-deletion would break "the mutual
    exclusion invariant INV-2". That attribution was wrong — INV-2 is
    no-deadlock only (project-notes/specs/exec-binding.md §2), not mutual
    exclusion; see log-lock-stable-key.md §2.3/§8 for the correction. It stays
    report-only for a different and still-valid reason: `/progress check` is a
    read-only command (docs/architecture.md), so deletion belongs in the
    explicit, dry-run-by-default `scripts/clean_locks.py`, not here.

    Walk range for the live-basename set MUST mirror
    hooks/session_progress_capture.py::_task_basename_index and
    scripts/clean_locks.py::task_md_basenames — a basename missed here would
    make a live lock look dead. LOCKSTEP: change all three together, plus the
    `progress.md.lock` exemption above, which scripts/clean_locks.py carries as
    a separate implementation (`PROGRESS_LOCK_NAME`) — four points, not three.
    """
    tasks_dir = project_dir / "tasks"

    live_basenames: set[str] = set()
    if tasks_dir.is_dir():
        for dirpath, _dirs, files in os.walk(tasks_dir):
            for fn in files:
                if fn.lower().endswith(".md"):
                    live_basenames.add(fn)

    locks_dir = project_dir / ".locks"
    if locks_dir.is_dir():
        for lock_path in sorted(locks_dir.iterdir()):
            if not lock_path.is_file() or lock_path.suffix != ".lock":
                continue
            if lock_path.name == "progress.md.lock":
                continue  # not task-keyed (protocol v2 rule 2) — see docstring
            if lock_path.name[: -len(".lock")] in live_basenames:
                continue
            result.add(
                "orphan_lock",
                "drift",
                str(lock_path),
                f"dead lock (no such task md): "
                f"{lock_path.relative_to(project_dir).as_posix()} "
                f"— clear with `scripts/clean_locks.py <project> --apply`",
            )

    if tasks_dir.is_dir():
        for dirpath, _dirs, files in os.walk(tasks_dir):
            for fn in sorted(files):
                if not fn.endswith(".md.lock"):
                    continue
                lock_path = Path(dirpath) / fn
                result.add(
                    "orphan_lock",
                    "drift",
                    str(lock_path),
                    f"legacy lock (pre-stable-key location): "
                    f"{lock_path.relative_to(project_dir).as_posix()} "
                    f"— clear with `scripts/clean_locks.py <project> --apply`",
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


def check_duplicate_table_region(project_dir: Path, result: Result) -> None:
    """#12 — progress.md must hold exactly one @table region.

    A second region is never authored by hand: it is what a writer leaves
    behind when it misses the markers and appends instead of replacing. The
    damage is silent, because view_progress.py caps the FIRST region only —
    every extra Completed table then reaches an agent's context uncapped.
    `/progress rebuild` collapses them.
    """
    progress = project_dir / "progress.md"
    content = read_text(progress)
    if content is None:
        return
    count = content.count(TABLE_BEGIN)
    if count < 2:
        return
    result.add(
        "duplicate_table_region",
        "violation",
        str(progress),
        f"progress.md holds {count} @table regions; exactly 1 is allowed — "
        "view_progress.py caps only the first, so the others reach context "
        "uncapped; run /progress rebuild to collapse them",
    )


# ============================================================================
# Output
# ============================================================================

SEVERITY_ORDER = {"drift": 0, "violation": 1, "stale": 2, "approval": 3, "advisory": 4}
SEVERITY_LABEL = {
    "drift": "DRIFT",
    "violation": "VIOLATION",
    "stale": "STALE",
    "approval": "PENDING",
    "advisory": "ADVISORY",
}


def print_findings(result: Result, footers: tuple[str, ...] = ()) -> None:
    """Findings grouped by severity, then the always-on footer lines.

    Footers sit outside the findings: they print even on an OK run and never
    touch the exit code."""
    if not result.findings:
        print("OK: no drift, no violations, no stale tasks.")
        if footers:
            print()
        for line in footers:
            print(line)
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
    for line in footers:
        print(line)


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
    parser.add_argument(
        "--lsp-snapshot",
        type=Path,
        default=None,
        help=(
            "query-lsp snapshot used to resolve path::symbol anchors "
            f"(default: <repo-root>/{LSP_SNAPSHOT_REL.as_posix()}, where repo "
            "root is the parent of _projects/). Missing file = symbol anchors "
            "are reported as unchecked, never as resolved."
        ),
    )
    args = parser.parse_args(argv)

    project_dir: Path = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return 2

    snapshot: Path = args.lsp_snapshot or project_dir.parent.parent / LSP_SNAPSHOT_REL
    snapshot = snapshot.resolve()

    result = Result()
    check_progress_task_links(project_dir, result)
    check_notes_index_consistency(project_dir, result)
    check_notes_index_desc_length(project_dir, result)
    check_notes_nested_dir(project_dir, result)
    check_notes_summary_length(project_dir, result)
    unchecked = check_notes_anchor_dangling(
        project_dir, result, snapshot if snapshot.is_file() else None
    )
    check_task_stale(project_dir, result, args.stale_days)
    check_project_marker(project_dir, result)
    check_task_body_links(project_dir, result)
    check_progress_summary_h1_sync(project_dir, result)
    check_filename_convention(project_dir, result)
    check_pending_approval(project_dir, result, args.stale_days)
    check_orphan_lock(project_dir, result)
    check_duplicate_basename(project_dir, result)
    check_duplicate_table_region(project_dir, result)

    without, total = notes_summary_coverage(project_dir)
    footers = [f"notes without summary: {without} of {total}"]
    near_dups = near_duplicate_pairs(project_dir)
    footers.append(f"near-duplicate note pairs: {len(near_dups)}")
    footers.extend(f"  {a} ~ {b} ({j})" for a, b, j in near_dups)
    if unchecked:
        footers.append(
            f"anchor symbols unchecked: {unchecked} (no LSP snapshot at {snapshot})"
        )
    coverage = qmd_vector_coverage(project_dir)
    if coverage is not None:
        footers.append(f"vector coverage: {coverage[0]} of {coverage[1]} docs")
    print_findings(result, tuple(footers))
    return 1 if result.findings else 0


if __name__ == "__main__":
    sys.exit(main())
