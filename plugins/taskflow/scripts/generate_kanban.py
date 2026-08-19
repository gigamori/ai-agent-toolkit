#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "markdown", "nh3"]
# ///
"""generate_kanban.py — Trello-like HTML kanban for taskflow projects.

Reads all projects from _projects/index.md (both primary and secondary roots),
enumerates tasks, extracts session references from @log blocks, resolves full
UUIDs from _state/, and emits a self-contained HTML file.

Usage:
    uv run --script generate_kanban.py [--out PATH] [--open] [--scheme vscode|vscodium]

Exit codes:
    0 = success
    2 = error (no _projects/ dir found)
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

# ── paths ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent          # plugins/taskflow/scripts
PLUGIN_DIR = SCRIPT_DIR.parent              # plugins/taskflow
WORKSPACE_ROOT = Path(os.getcwd())


def _project_roots() -> list[Path]:
    """Return the list of _projects root directories.

    Reads ``TASKFLOW_PROJECT_ROOTS`` (semicolon-separated paths).  Falls back
    to ``WORKSPACE_ROOT / "_projects"`` when the variable is unset or empty.
    """
    env = os.environ.get('TASKFLOW_PROJECT_ROOTS', '')
    if env:
        return [Path(p.strip()) for p in env.split(';') if p.strip()]
    return [WORKSPACE_ROOT / "_projects"]

# ── regex ──────────────────────────────────────────────────────────────────

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
LOG_ENTRY_RE = re.compile(
    r"-\s+(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)?)\s+\[s:([0-9a-f]{6,})\]:\s*(.+)"
)

# `_projects/index.md` row grammar. LOCKSTEP: pi-studio
# `src/kanban/kanban-data-provider.ts::parseProjectIndex` reimplements these three
# rules verbatim; changing one here without the other breaks board parity.
#   1. separator row: the WHOLE line matches INDEX_SEPARATOR_RE (a data row whose
#      Description merely contains `---` is NOT a separator).
#   2. cell split: strip exactly ONE leading and ONE trailing `|` (GFM outer
#      delimiters), then split on `|`. `||name|` therefore means an empty cell 0.
#   3. project name: a cell 0 that is entirely one `[label](href)` yields `label`;
#      any other cell 0 is taken verbatim. Description is never unwrapped.
INDEX_SEPARATOR_RE = re.compile(r"^\|[-\s|:]+\|\s*$")
INDEX_LINK_RE = re.compile(r"^\[([^\]]+)\]\([^)]*\)$")

TASK_STATUSES = ("0_todo", "1_in_progress", "2_done")
STATUS_LABELS = {"0_todo": "TODO", "1_in_progress": "In Progress", "2_done": "Done"}

# ── data classes ───────────────────────────────────────────────────────────


@dataclass
class SessionRef:
    date: str
    short_id: str
    summary: str
    full_uuid: str = ""


@dataclass
class Task:
    status: str
    h1: str
    priority: str
    project: str
    created: str = ""
    updated: str = ""
    sessions: list[SessionRef] = field(default_factory=list)
    file_path: str = ""


@dataclass
class Project:
    name: str
    description: str
    tasks: list[Task] = field(default_factory=list)
    unassigned_sessions: list[SessionRef] = field(default_factory=list)


# ── file helpers ───────────────────────────────────────────────────────────


def read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def iter_dir(p: Path) -> list[Path]:
    """List ``p``'s entries, tolerating a directory we are not allowed to read.

    Under a sandbox a directory can be traversable but not listable: ``stat``
    succeeds (so ``is_dir()`` returns True) while the listing raises
    ``PermissionError``. Every scan site goes through here so one unreadable
    directory degrades to "empty + warn" instead of aborting the whole board.
    """
    try:
        return list(p.iterdir())
    except OSError as e:
        print(f"[kanban] warn: cannot read directory {p}: {e}", file=sys.stderr)
        return []


def parse_frontmatter(content: str) -> dict:
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def extract_h1(content: str) -> str | None:
    body = FRONTMATTER_RE.sub("", content, count=1)
    m = H1_RE.search(body)
    return m.group(1).strip() if m else None


def extract_sessions(content: str) -> list[SessionRef]:
    log_m = re.search(
        r"<!-- @log:begin -->(.*?)<!-- @log:end -->", content, re.DOTALL
    )
    if log_m:
        region = log_m.group(1)
    else:
        # Tolerate a missing @log:end (marker destroyed by a hand edit): fall
        # back to the span from @log:begin to the @notes block / EOF, so the
        # task's session bindings survive the damage.
        begin_m = re.search(r"<!-- @log:begin -->", content)
        if not begin_m:
            return []
        region = content[begin_m.end():]
        stop = re.search(
            r"<!-- @notes:begin -->"
            r"|<!-- auto-managed by taskflow note-link[^>]*-->"
            r"|<!-- @notes:end -->",
            region,
        )
        if stop:
            region = region[:stop.start()]
    refs = []
    for m in LOG_ENTRY_RE.finditer(region):
        refs.append(SessionRef(
            date=m.group(1),
            short_id=m.group(2),
            summary=m.group(3).strip(),
        ))
    return refs


def split_index_row(line: str) -> list[str]:
    """Split one `_projects/index.md` table row into stripped cells (rule 2)."""
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [p.strip() for p in line.split("|")]


def unwrap_index_name(cell: str) -> str:
    """`[label](href)` → `label` when the cell is entirely one link (rule 3)."""
    m = INDEX_LINK_RE.match(cell)
    return m.group(1).strip() if m else cell


def parse_index(path: Path) -> list[tuple[str, str]]:
    """Parse _projects/index.md → [(name, description), ...]."""
    content = read_text(path)
    if not content:
        return []
    result = []
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("|") or INDEX_SEPARATOR_RE.match(line):
            continue
        parts = split_index_row(line)
        if not parts:
            continue
        name = unwrap_index_name(parts[0])
        if name in ("Project", ""):
            continue
        desc = parts[1] if len(parts) > 1 else ""
        result.append((name, desc))
    return result


# ── data loading ───────────────────────────────────────────────────────────


@dataclass
class StateEntry:
    uuid: str
    origin: str = ""
    project: str = ""


def build_uuid_index(state_dir: Path) -> dict[str, StateEntry]:
    """Map short_id (first 8 hex chars) → StateEntry(uuid, origin, project).

    ``origin`` / ``project`` are read from the ``_state/*.json`` sidecar so the
    kanban can attribute unreferenced CC sessions to their project (§7).
    """
    index: dict[str, StateEntry] = {}
    if not state_dir.is_dir():
        return index
    for f in iter_dir(state_dir):
        if f.suffix == ".json" and len(f.stem) == 36:
            origin = ""
            project = ""
            content = read_text(f)
            if content:
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        origin = str(data.get("origin", "") or "")
                        project = str(data.get("project", "") or "")
                except ValueError:
                    pass
            index[f.stem[:8]] = StateEntry(uuid=f.stem, origin=origin, project=project)
    return index


def find_project_dir(name: str, roots: list[Path]) -> Path | None:
    for root in roots:
        p = root / name
        if p.is_dir():
            return p
    return None


def load_tasks(
    project_dir: Path,
    project_name: str,
    uuid_index: dict[str, StateEntry],
) -> list[Task]:
    tasks: list[Task] = []
    tasks_dir = project_dir / "tasks"
    if not tasks_dir.is_dir():
        return tasks
    for status in TASK_STATUSES:
        sub = tasks_dir / status
        if not sub.is_dir():
            continue
        for p in sorted(iter_dir(sub)):
            if not p.is_file() or p.suffix != ".md":
                continue
            content = read_text(p)
            if content is None:
                continue
            fm = parse_frontmatter(content)
            h1 = extract_h1(content) or p.stem
            priority = str(fm.get("priority", "")).strip()
            def _date(v: object) -> str:
                import datetime as _dt
                if isinstance(v, _dt.date):
                    return v.isoformat()
                s = str(v).strip() if v else ""
                if not s:
                    return ""
                if "T" in s:
                    s = s.split("T")[0]
                return s[:10]
            created = _date(fm.get("created", ""))
            updated = _date(fm.get("updated", ""))
            if "<!-- @log:begin -->" in content and "<!-- @log:end -->" not in content:
                print(
                    f"[kanban] warn: unbalanced @log markers (missing @log:end): {p}",
                    file=sys.stderr,
                )
            sessions = extract_sessions(content)
            for s in sessions:
                entry = uuid_index.get(s.short_id)
                s.full_uuid = entry.uuid if entry else ""
            tasks.append(Task(
                status=status,
                h1=h1,
                priority=priority,
                project=project_name,
                created=created,
                updated=updated,
                sessions=sessions,
                file_path=str(p),
            ))
    return tasks


# ── CC session sidecar (unassigned sessions, §7) ────────────────────────────

SESSION_HEAD_BYTES = 8192


def _cc_config_dirs() -> list[Path]:
    """Claude config dirs to scan, replicating CC's own resolution.

    CC treats CLAUDE_CONFIG_DIR literally (no ~-expansion; relative values
    resolve against the process cwd — verified 2026-07-28). Scan order is
    [env value, ~/.claude] deduped; callers relying on first-wins get
    env-universe priority.
    """
    dirs: list[Path] = []
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        dirs.append(Path(os.path.abspath(env)))
    default = Path.home() / ".claude"
    if not any(os.path.normcase(str(d)) == os.path.normcase(str(default)) for d in dirs):
        dirs.append(default)
    return dirs


def build_cc_session_index() -> dict[str, Path]:
    """Scan ``<config dir>/projects/*/`` for ``<uuid>.jsonl`` → path.

    Scans every project dir (rather than guessing the cwd encoding) so a
    session started in any workspace can be resolved by its UUID. The scan
    covers the union of ``$CLAUDE_CONFIG_DIR`` and ``~/.claude``; on a UUID
    collision the env universe wins (first-wins ``setdefault``).
    """
    index: dict[str, Path] = {}
    for base_root in _cc_config_dirs():
        base = base_root / "projects"
        if not base.is_dir():
            continue
        for proj_dir in iter_dir(base):
            if not proj_dir.is_dir():
                continue
            try:
                for f in proj_dir.glob("*.jsonl"):
                    index.setdefault(f.stem, f)
            except OSError:
                continue
    return index


def read_cc_session_first_message(path: Path) -> tuple[str, str]:
    """Return ``(date, summary)`` from the head of a CC session JSONL.

    Reads only the first ``SESSION_HEAD_BYTES`` to avoid loading multi-MB files.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(SESSION_HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return "", ""
    timestamp = ""
    for line in head.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        ts = entry.get("timestamp")
        if ts and not timestamp:
            timestamp = str(ts)
        msg = entry.get("message")
        is_user = (
            entry.get("type") == "user"
            and isinstance(msg, dict)
            and msg.get("role") == "user"
        )
        if not is_user or entry.get("isMeta"):
            continue
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    text = str(b["text"])
                    break
        if text.lstrip().startswith(("<local-command-", "<command-name>")):
            continue
        if text:
            date = timestamp.split("T")[0][:10] if timestamp else ""
            return date, text.strip()[:72]
    return (timestamp.split("T")[0][:10] if timestamp else ""), ""


# ── HTML generation ────────────────────────────────────────────────────────

PRIORITY_COLORS = {
    "HIGH": "#c53030",
    "MID":  "#b7791f",
    "LOW":  "#276749",
}

PROJECT_PALETTE = [
    "#6b46c1", "#2c7a7b", "#c05621", "#2d3748", "#97266d",
    "#1a365d", "#744210", "#702459", "#2a4365", "#285e61",
]

STATUS_BG = {
    "0_todo":        "#edf2f7",
    "1_in_progress": "#ebf8ff",
    "2_done":        "#f0fff4",
}

STATUS_HEADER_COLOR = {
    "0_todo":        "#4a5568",
    "1_in_progress": "#2b6cb0",
    "2_done":        "#276749",
}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def dedup_sessions(sessions: list[SessionRef]) -> list[SessionRef]:
    """Deduplicate by full_uuid (or short_id if unresolved), keeping latest entry."""
    seen: dict[str, SessionRef] = {}
    for s in sessions:
        key = s.full_uuid if s.full_uuid else s.short_id
        if key not in seen or s.date >= seen[key].date:
            seen[key] = s
    # Preserve original order of first occurrence
    order: list[str] = []
    for s in sessions:
        key = s.full_uuid if s.full_uuid else s.short_id
        if key not in order:
            order.append(key)
    return [seen[k] for k in order]


STATUS_BADGE_LABEL = {"0_todo": "TODO", "1_in_progress": "WIP", "2_done": "✓"}


def session_url(s: SessionRef, scheme: str, serve: bool, open_token: str = "") -> str:
    if serve:
        return f"http://localhost:{SERVE_PORT}/open?session={s.full_uuid}&t={open_token}"
    return f"{scheme}://anthropic.claude-code/open?session={s.full_uuid}"


PROGRESS_SUBS = ["check", "audit", "rebuild"]


def progress_url(project: str, sub: str, scheme: str, serve: bool, open_token: str = "") -> str:
    from urllib.parse import quote
    prompt = f"pj:{project} /progress {sub}"
    if serve:
        return f"http://localhost:{SERVE_PORT}/open?prompt={quote(prompt)}&t={open_token}"
    return f"{scheme}://anthropic.claude-code/open?prompt={quote(prompt)}"


def task_start_url(project: str, file_path: str, scheme: str, serve: bool, open_token: str = "") -> str:
    """Build a CC launch URL that pre-fills ``pj:<project> @<taskfile>``.

    The ``@`` reference is WORKSPACE_ROOT-relative (base=a) so CC resolves it
    against the current workspace.  Task files outside WORKSPACE_ROOT (secondary
    ``TASKFLOW_PROJECT_ROOTS``) fall back to an absolute path — emitted only in
    the runtime-generated URL, never written to a tracked file (D3).
    """
    from urllib.parse import quote
    p = Path(file_path)
    try:
        ref = p.resolve().relative_to(WORKSPACE_ROOT.resolve()).as_posix()
    except ValueError:
        ref = p.resolve().as_posix()
    prompt = f"pj:{project} @{ref}"
    if serve:
        return f"http://localhost:{SERVE_PORT}/open?prompt={quote(prompt)}&t={open_token}"
    return f"{scheme}://anthropic.claude-code/open?prompt={quote(prompt)}"


def render_progress_picker(project: str, scheme: str, serve: bool, open_token: str = "") -> str:
    items = ""
    for sub in PROGRESS_SUBS:
        url = progress_url(project, sub, scheme, serve, open_token)
        items += (
            f'<a class="pm-item" href="{esc(url)}" target="_blank"'
            f' onclick="this.closest(\'details\').removeAttribute(\'open\')">'
            f'{sub}</a>'
        )
    return f"""\
<details class="progress-picker">
  <summary class="progress-link">/progress ▾</summary>
  <div class="picker-menu">{items}</div>
</details>"""


def render_card(
    task: Task,
    proj_color: str,
    scheme: str,
    serve: bool = False,
    show_status_badge: bool = False,
    open_token: str = "",
) -> str:
    pri_color = PRIORITY_COLORS.get(task.priority, "#718096")
    pri_label = task.priority or "—"
    unique_sessions = dedup_sessions(task.sessions)

    if unique_sessions:
        items = ""
        for s in unique_sessions:
            summary_short = s.summary[:72] + ("…" if len(s.summary) > 72 else "")
            if s.full_uuid:
                url = session_url(s, scheme, serve, open_token)
                items += (
                    f'<li><a href="{esc(url)}" target="_blank">'
                    f'<span class="s-date">{esc(s.date)}</span>'
                    f'<span class="s-id">[{esc(s.short_id)}]</span>'
                    f'<span class="s-summary">{esc(summary_short)}</span>'
                    f"</a></li>\n"
                )
            else:
                items += (
                    f'<li class="no-uuid">'
                    f'<span class="s-date">{esc(s.date)}</span>'
                    f'<span class="s-id">[{esc(s.short_id)}]</span>'
                    f'<span class="s-summary">{esc(summary_short)}</span>'
                    f"</li>\n"
                )
        body = f'<ul class="sessions">{items}</ul>'
    else:
        body = '<p class="no-sessions">No sessions</p>'

    dates = ""
    if task.created:
        dates += f'<span class="t-date">created {esc(task.created)}</span>'
    if task.updated and task.updated != task.created:
        dates += f'<span class="t-date">updated {esc(task.updated)}</span>'
    if dates:
        body = f'<div class="task-dates">{dates}</div>' + body

    if task.file_path:
        from urllib.parse import quote
        start_url = task_start_url(task.project, task.file_path, scheme, serve, open_token)
        btns = (
            f'<a class="start-btn start-cc" href="{esc(start_url)}" target="_blank">▶ CC</a>'
        )
        if serve:
            md_url = f"http://localhost:{SERVE_PORT}/md?path={quote(task.file_path)}"
            btns += (
                f'<button class="view-md-btn" type="button" data-md="{esc(md_url)}">📄</button>'
            )
        body += f'<div class="start-btns">{btns}</div>'

    n = len(unique_sessions)
    status_badge = (
        f'<span class="badge st" style="background:{STATUS_HEADER_COLOR[task.status]}">'
        f'{STATUS_BADGE_LABEL[task.status]}</span>'
        if show_status_badge else
        f'<span class="badge proj" style="background:{proj_color}">{esc(task.project)}</span>'
    )
    return f"""\
<details class="card" data-status="{task.status}" data-project="{esc(task.project)}">
  <summary>
    <div class="card-tags">
      <span class="badge pri" style="background:{pri_color}">{esc(pri_label)}</span>
      {status_badge}
    </div>
    <div class="card-title">{esc(task.h1)}</div>
    <span class="expand-hint">▸ {n} session{"s" if n != 1 else ""}</span>
  </summary>
  <div class="card-body">{body}</div>
</details>
"""


def _session_li(s: SessionRef, scheme: str, serve: bool, project: str = "", open_token: str = "") -> str:
    """One clickable session row for the No Task / No Project lists."""
    url = session_url(s, scheme, serve, open_token)
    summary_short = s.summary[:72] + ("…" if len(s.summary) > 72 else "")
    pa = f' data-project="{esc(project)}"' if project else ""
    return (
        f'<li class="ua-item"{pa}>'
        f'<a href="{esc(url)}" target="_blank">'
        f'<span class="s-date">{esc(s.date)}</span>'
        f'<span class="s-id">[{esc(s.short_id)}]</span>'
        f'<span class="s-summary">{esc(summary_short)}</span>'
        f"</a></li>\n"
    )


def render_html(
    projects: list[Project],
    scheme: str,
    serve: bool = False,
    no_project: list[SessionRef] | None = None,
    no_project_total: int = 0,
    open_token: str = "",
) -> str:
    proj_colors: dict[str, str] = {
        p.name: PROJECT_PALETTE[i % len(PROJECT_PALETTE)]
        for i, p in enumerate(projects)
    }

    # ── status view ────────────────────────────────────────────────────────
    by_status: dict[str, list[Task]] = {s: [] for s in TASK_STATUSES}
    for proj in projects:
        for task in proj.tasks:
            by_status[task.status].append(task)

    total = sum(len(v) for v in by_status.values())
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    status_cols = ""
    for status in TASK_STATUSES:
        tasks = by_status[status]
        hdr_color = STATUS_HEADER_COLOR[status]
        cards = "".join(
            render_card(t, proj_colors[t.project], scheme, serve, open_token=open_token) for t in tasks
        ) or '<p class="empty-col">No tasks</p>'
        status_cols += f"""\
<div class="column" data-status="{status}">
  <div class="col-header" style="border-bottom:3px solid {hdr_color}">
    <span class="col-title" style="color:{hdr_color}">{STATUS_LABELS[status]}</span>
    <span class="col-count" style="background:{hdr_color}">{len(tasks)}</span>
  </div>
  <div class="cards">{cards}</div>
</div>
"""

    # ── unassigned CC sessions column (§7) ───────────────────────────────────
    all_unassigned: list[tuple[str, SessionRef]] = []
    for proj in projects:
        for s in proj.unassigned_sessions:
            all_unassigned.append((proj.name, s))
    all_unassigned.sort(key=lambda t: t[1].date, reverse=True)
    if all_unassigned:
        ua_items = ""
        for pname, s in all_unassigned:
            url = session_url(s, scheme, serve, open_token)
            summary_short = s.summary[:72] + ("…" if len(s.summary) > 72 else "")
            ua_items += (
                f'<li class="ua-item" data-project="{esc(pname)}">'
                f'<a href="{esc(url)}" target="_blank">'
                f'<span class="s-date">{esc(s.date)}</span>'
                f'<span class="s-id">[{esc(s.short_id)}]</span>'
                f'<span class="s-summary">{esc(summary_short)}</span>'
                f"</a></li>\n"
            )
        status_cols += f"""\
<div class="column" data-status="unassigned">
  <div class="col-header" style="border-bottom:3px solid #718096">
    <span class="col-title" style="color:#718096">No Task</span>
    <span class="col-count" style="background:#718096">{len(all_unassigned)}</span>
  </div>
  <ul class="sessions ua-list">{ua_items}</ul>
</div>
"""

    # ── no-project CC sessions column (sessions tied to no pj at all) ─────────
    no_project = no_project or []
    if no_project:
        np_items = ""
        for s in no_project:
            url = session_url(s, scheme, serve, open_token)
            summary_short = s.summary[:72] + ("…" if len(s.summary) > 72 else "")
            np_items += (
                f'<li class="ua-item">'
                f'<a href="{esc(url)}" target="_blank">'
                f'<span class="s-date">{esc(s.date)}</span>'
                f'<span class="s-id">[{esc(s.short_id)}]</span>'
                f'<span class="s-summary">{esc(summary_short)}</span>'
                f"</a></li>\n"
            )
        shown = len(no_project)
        more = f" (+{no_project_total - shown} older)" if no_project_total > shown else ""
        status_cols += f"""\
<div class="column" data-status="no_project">
  <div class="col-header" style="border-bottom:3px solid #805ad5">
    <span class="col-title" style="color:#805ad5">No Project</span>
    <span class="col-count" style="background:#805ad5">{no_project_total}</span>
  </div>
  <p class="ua-note">showing latest {shown}{more}</p>
  <ul class="sessions ua-list">{np_items}</ul>
</div>
"""

    # ── project view ───────────────────────────────────────────────────────
    proj_cols = ""
    for proj in projects:
        color = proj_colors[proj.name]
        counts = {s: sum(1 for t in proj.tasks if t.status == s) for s in TASK_STATUSES}

        count_badges = "".join(
            f'<span class="pc-count" data-status="{s}" style="background:{STATUS_HEADER_COLOR[s]}">'
            f'{STATUS_BADGE_LABEL[s]} {counts[s]}</span>'
            for s in TASK_STATUSES if counts[s]
        )
        sorted_tasks = sorted(proj.tasks, key=lambda t: TASK_STATUSES.index(t.status))
        cards = "".join(
            render_card(t, color, scheme, serve, show_status_badge=True, open_token=open_token)
            for t in sorted_tasks
        ) or '<p class="empty-col">No tasks</p>'
        picker = render_progress_picker(proj.name, scheme, serve, open_token)
        ua_section = ""
        if proj.unassigned_sessions:
            ua_lis = "".join(
                _session_li(s, scheme, serve, open_token=open_token) for s in proj.unassigned_sessions
            )
            ua_section = (
                '<div class="unassigned-section">'
                '<div class="unassigned-header">Sessions (no task) '
                f'<span class="ua-count">{len(proj.unassigned_sessions)}</span></div>'
                f'<ul class="sessions ua-list">{ua_lis}</ul>'
                '</div>'
            )
        proj_cols += f"""\
<div class="column proj-col">
  <div class="col-header" style="border-bottom:3px solid {color}">
    <span class="col-title" style="color:{color}">{esc(proj.name)}</span>
    {picker}
  </div>
  <div class="pc-counts">{count_badges}</div>
  <div class="cards">{cards}</div>
  {ua_section}
</div>
"""

    # ── no-project column, rightmost in project view ─────────────────────────
    if no_project:
        pnp_items = "".join(_session_li(s, scheme, serve, open_token=open_token) for s in no_project)
        np_shown = len(no_project)
        np_more = f" (+{no_project_total - np_shown} older)" if no_project_total > np_shown else ""
        proj_cols += f"""\
<div class="column proj-col" data-status="no_project">
  <div class="col-header" style="border-bottom:3px solid #805ad5">
    <span class="col-title" style="color:#805ad5">No Project</span>
    <span class="col-count" style="background:#805ad5">{no_project_total}</span>
  </div>
  <p class="ua-note">showing latest {np_shown}{np_more}</p>
  <ul class="sessions ua-list">{pnp_items}</ul>
</div>
"""

    # ── legend / project filter ────────────────────────────────────────────
    legend_html = '<button class="leg active" id="leg-all" onclick="setProjFilter(\'all\')">All</button>'
    for p in projects:
        c = proj_colors[p.name]
        legend_html += (
            f'<button class="leg" id="leg-{esc(p.name)}" onclick="setProjFilter(\'{esc(p.name)}\')"'
            f' style="--lc:{c}">'
            f'<span class="leg-dot" style="background:{c}"></span>{esc(p.name)}</button>'
        )

    css = """\
:root{
  --bg:#dde1e7;--header-bg:#1a202c;--header-fg:#fff;--meta-fg:#a0aec0;
  --bar-bg:#2d3748;--bar-border:#1a202c;--btn-border:#4a5568;--btn-fg:#a0aec0;
  --btn-active-bg:#4a5568;--muted:#718096;--id-fg:#a0aec0;--hi:#e2e8f0;
  --col-todo-bg:#edf2f7;--col-wip-bg:#ebf8ff;--col-done-bg:#f0fff4;--col-unassigned-bg:#eceff3;
  --proj-col-bg:#f7fafc;--card-bg:#fff;--card-body-bg:#f9fafb;--card-title:#2d3748;
  --border:#e2e8f0;--row-border:#edf2f7;--hover-bg:#f7fafc;--link:#3182ce;
  --menu-bg:#fff;--menu-fg:#2d3748;--menu-hover-bg:#ebf8ff;--menu-hover-fg:#2b6cb0;
  --code-bg:#f1f5f9;--shadow:rgba(0,0,0,.12);
}
html[data-theme="dark"]{
  --bg:#12161d;--header-bg:#0f141c;--header-fg:#e2e8f0;--meta-fg:#8a94a6;
  --bar-bg:#171d27;--bar-border:#0b0f16;--btn-border:#3a4557;--btn-fg:#8a94a6;
  --btn-active-bg:#3a4557;--muted:#8a94a6;--id-fg:#6b7688;--hi:#e2e8f0;
  --col-todo-bg:#232a35;--col-wip-bg:#1a2836;--col-done-bg:#1a2b23;--col-unassigned-bg:#20252e;
  --proj-col-bg:#1c222c;--card-bg:#232a35;--card-body-bg:#1b212b;--card-title:#e2e8f0;
  --border:#2d3748;--row-border:#2a3340;--hover-bg:#2a3340;--link:#63b3ed;
  --menu-bg:#232a35;--menu-fg:#e2e8f0;--menu-hover-bg:#2a3340;--menu-hover-fg:#63b3ed;
  --code-bg:#161b22;--shadow:rgba(0,0,0,.4);
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);min-height:100vh}
header{background:var(--header-bg);color:var(--header-fg);padding:10px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:17px;font-weight:700;letter-spacing:.02em}
.meta{font-size:12px;color:var(--meta-fg);flex:1}
.toggle{display:flex;gap:4px}
.toggle button,.theme-toggle{font-size:11px;font-weight:600;padding:4px 10px;border:1px solid var(--btn-border);border-radius:4px;
  background:transparent;color:var(--btn-fg);cursor:pointer;letter-spacing:.03em}
.toggle button.active{background:var(--btn-active-bg);color:#fff;border-color:var(--btn-active-bg)}
.theme-toggle:hover{color:var(--header-fg);border-color:var(--muted)}
.filter-bar{background:var(--bar-bg);padding:5px 20px;display:flex;align-items:center;gap:6px}
.filter-bar span{font-size:11px;color:var(--muted);margin-right:4px}
.fb{font-size:11px;font-weight:600;padding:3px 10px;border:1px solid var(--btn-border);border-radius:12px;
  background:transparent;color:var(--btn-fg);cursor:pointer}
.fb.active{color:#fff}
.legend{background:var(--bar-bg);padding:5px 20px 7px;display:flex;flex-wrap:wrap;gap:6px;border-top:1px solid var(--bar-border);align-items:center}
.leg{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--btn-fg);background:transparent;
  border:1px solid var(--btn-border);border-radius:12px;padding:2px 8px;cursor:pointer}
.leg.active{background:var(--lc,var(--btn-active-bg));border-color:var(--lc,var(--btn-active-bg));color:#fff}
.leg:hover{border-color:var(--muted);color:var(--hi)}
.leg-dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.board{display:flex;gap:12px;padding:14px;overflow-x:auto;align-items:flex-start;min-height:calc(100vh - 120px)}
.column{flex:0 0 310px;border-radius:8px;padding:10px;max-height:calc(100vh - 148px);overflow-y:auto;background:var(--proj-col-bg)}
.column[data-status="0_todo"]{background:var(--col-todo-bg)}
.column[data-status="1_in_progress"]{background:var(--col-wip-bg)}
.column[data-status="2_done"]{background:var(--col-done-bg)}
.column[data-status="unassigned"]{background:var(--col-unassigned-bg)}
.column.proj-col{background:var(--proj-col-bg)}
.column.hidden{display:none}
.col-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:8px;position:relative}
.col-title{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.col-count{font-size:11px;font-weight:700;color:#fff;border-radius:10px;padding:1px 7px}
.pc-counts{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}
.pc-count{font-size:10px;font-weight:700;color:#fff;border-radius:3px;padding:2px 6px}
.progress-picker{position:relative}
.progress-picker summary.progress-link{font-size:11px;font-weight:600;color:#fff;background:#553c9a;
  border-radius:3px;padding:2px 8px;cursor:pointer;list-style:none;user-select:none;white-space:nowrap}
.progress-picker summary.progress-link::-webkit-details-marker{display:none}
.progress-picker summary.progress-link:hover{background:#44337a}
.picker-menu{position:absolute;right:0;top:calc(100% + 4px);background:var(--menu-bg);border:1px solid var(--border);
  border-radius:6px;box-shadow:0 4px 12px var(--shadow);z-index:100;min-width:110px;overflow:hidden}
.pm-item{display:block;padding:7px 14px;font-size:12px;color:var(--menu-fg);text-decoration:none;white-space:nowrap}
.pm-item:hover{background:var(--menu-hover-bg);color:var(--menu-hover-fg)}
.card{background:var(--card-bg);border-radius:6px;margin-bottom:8px;box-shadow:0 1px 3px var(--shadow);overflow:hidden}
.card.hidden{display:none}
.card summary{padding:10px 12px;cursor:pointer;list-style:none;user-select:none}
.card summary::-webkit-details-marker{display:none}
.card[open] summary{background:var(--hover-bg);border-bottom:1px solid var(--border)}
.card summary:hover{background:var(--hover-bg)}
.card-tags{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:6px}
.badge{font-size:10px;font-weight:700;color:#fff;padding:2px 6px;border-radius:3px;text-transform:uppercase;letter-spacing:.04em}
.card-title{font-size:13px;color:var(--card-title);line-height:1.4;margin-bottom:4px}
.expand-hint{font-size:10px;color:var(--muted)}
.card-body{padding:8px 12px 10px;background:var(--card-body-bg)}
ul.sessions{list-style:none;padding:0}
ul.sessions li{padding:4px 0;border-bottom:1px solid var(--row-border);font-size:11px;line-height:1.4}
ul.sessions li:last-child{border-bottom:none}
ul.sessions a{color:var(--link);text-decoration:none;display:flex;flex-wrap:wrap;gap:4px}
ul.sessions a:hover{text-decoration:underline}
li.no-uuid{display:flex;flex-wrap:wrap;gap:4px;color:var(--muted)}
.s-date{color:var(--muted);flex-shrink:0}
.s-id{color:var(--id-fg);flex-shrink:0;font-family:monospace}
.s-summary{color:inherit}
.task-dates{display:flex;gap:10px;margin-bottom:6px}
.t-date{font-size:10px;color:var(--muted)}
.no-sessions{font-size:11px;color:var(--muted);font-style:italic}
.empty-col{font-size:12px;color:var(--muted);text-align:center;padding:24px 0;font-style:italic}
.ua-note{font-size:10px;color:var(--muted);font-style:italic;margin-bottom:6px}
.md-back.hidden{display:none}
.unassigned-section{margin-top:10px;border-top:1px solid var(--border);padding-top:8px}
.unassigned-header{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;
  letter-spacing:.04em;margin-bottom:6px;display:flex;align-items:center;gap:6px}
.ua-count{font-size:10px;font-weight:700;color:#fff;background:#718096;border-radius:10px;padding:1px 7px}
.md-body code a,.md-body a.path-link{color:var(--link);text-decoration:underline}
.ua-list{list-style:none;padding:0}
.ua-item{padding:4px 8px;border-bottom:1px solid var(--row-border);font-size:11px;line-height:1.4}
.ua-item:last-child{border-bottom:none}
.ua-item.hidden{display:none}
.ua-item a{color:var(--link);text-decoration:none;display:flex;flex-wrap:wrap;gap:4px}
.ua-item a:hover{text-decoration:underline}
.start-btns{display:flex;gap:6px;margin-top:8px}
.start-btn{font-size:11px;font-weight:600;padding:3px 10px;border-radius:4px;border:1px solid var(--btn-border);
  cursor:pointer;letter-spacing:.02em;text-decoration:none}
.start-cc{background:#2b6cb0;color:#fff;border-color:#2b6cb0}
.start-cc:hover{background:#2c5282}
.view-md-btn{font-size:11px;font-weight:600;padding:3px 10px;border-radius:4px;border:1px solid var(--btn-border);
  cursor:pointer;letter-spacing:.02em;background:transparent;color:var(--muted)}
.view-md-btn:hover{color:var(--card-title);border-color:var(--link)}
.md-modal{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center}
.md-modal.hidden{display:none}
.md-modal-backdrop{position:absolute;inset:0;background:rgba(0,0,0,0.6)}
.md-modal-container{position:relative;width:80vw;max-width:900px;max-height:85vh;background:var(--bg);
  border:1px solid var(--border);border-radius:8px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,0.4)}
.md-modal-header{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border);
  background:var(--header-bg);flex-shrink:0}
.md-title{flex:1;font-size:13px;font-weight:600;color:var(--header-fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.md-btn{background:transparent;border:1px solid var(--btn-border);color:var(--meta-fg);border-radius:4px;padding:2px 8px;cursor:pointer;font-size:13px}
.md-btn:hover{color:var(--header-fg);border-color:var(--link)}
.md-close{font-size:16px;font-weight:700}
.md-body{overflow-y:auto;padding:16px 24px;flex:1;font-size:14px;line-height:1.6;color:var(--card-title)}
.md-body h1{font-size:1.6em;margin:.8em 0 .4em;border-bottom:1px solid var(--border);padding-bottom:.3em}
.md-body h2{font-size:1.3em;margin:.7em 0 .3em}
.md-body h3{font-size:1.1em;margin:.6em 0 .2em}
.md-body p{margin:.5em 0}
.md-body ul,.md-body ol{margin:.5em 0;padding-left:1.5em}
.md-body li{margin:.2em 0}
.md-body a{color:var(--link)}
.md-body code{font-family:monospace;background:var(--code-bg);padding:1px 4px;border-radius:3px;font-size:.9em}
.md-body pre{background:var(--code-bg);padding:12px;border-radius:6px;overflow-x:auto;margin:.5em 0}
.md-body pre code{background:none;padding:0}
.md-body blockquote{border-left:3px solid var(--border);margin:.5em 0;padding:.3em 1em;color:var(--muted)}
.md-body table{border-collapse:collapse;margin:.5em 0;width:100%}
.md-body th,.md-body td{border:1px solid var(--border);padding:6px 10px;text-align:left;font-size:13px}
.md-body th{background:var(--proj-col-bg);font-weight:600}
.md-body hr{border:none;border-top:1px solid var(--border);margin:1em 0}
"""

    filter_colors = {s: STATUS_HEADER_COLOR[s] for s in TASK_STATUSES}
    filter_bar_html = (
        '<div class="filter-bar">'
        '<span>Filter:</span>'
        '<button class="fb active" id="fb-all" onclick="setFilter(\'all\')">All</button>'
        + "".join(
            f'<button class="fb" id="fb-{s}" onclick="setFilter(\'{s}\')"'
            f' style="--fc:{filter_colors[s]}">{STATUS_BADGE_LABEL[s]}</button>'
            for s in TASK_STATUSES
        )
        + "</div>"
    )

    js = """\
var curView='status', curFilter='all', curProj='all';
function setView(v){
  curView=v;
  document.getElementById('board-status').style.display=v==='status'?'flex':'none';
  document.getElementById('board-project').style.display=v==='project'?'flex':'none';
  document.getElementById('btn-status').className=v==='status'?'active':'';
  document.getElementById('btn-project').className=v==='project'?'active':'';
  localStorage.setItem('tfView',v);
  applyFilter();
}
function setFilter(f){
  curFilter=f;
  ['all','0_todo','1_in_progress','2_done'].forEach(function(s){
    var b=document.getElementById('fb-'+s);
    if(!b) return;
    b.className='fb'+(s===f?' active':'');
    b.style.background=s!=='all'&&s===f?'var(--fc)':'';
    b.style.borderColor=s!=='all'&&s===f?'var(--fc)':'';
    b.style.color=s!=='all'&&s===f?'#fff':'';
  });
  applyFilter();
}
function setProjFilter(p){
  curProj=p;
  var all=document.getElementById('leg-all');
  if(all) all.className='leg'+(p==='all'?' active':'');
  document.querySelectorAll('.leg[id^="leg-"]').forEach(function(b){
    if(b.id==='leg-all') return;
    var name=b.id.slice(4);
    b.className='leg'+(name===p?' active':'');
    b.style.background=name===p?'var(--lc)':'';
    b.style.borderColor=name===p?'var(--lc)':'';
    b.style.color=name===p?'#fff':'';
  });
  applyFilter();
}
function applyFilter(){
  if(curView==='status'){
    document.querySelectorAll('#board-status .column').forEach(function(col){
      var statusOk=curFilter==='all'||col.dataset.status===curFilter;
      col.classList.toggle('hidden',!statusOk);
      if(statusOk){
        col.querySelectorAll('.card').forEach(function(card){
          card.classList.toggle('hidden',curProj!=='all'&&card.dataset.project!==curProj);
        });
        col.querySelectorAll('.ua-item').forEach(function(it){
          it.classList.toggle('hidden',curProj!=='all'&&it.dataset.project!==curProj);
        });
      }
    });
  } else {
    document.querySelectorAll('#board-project .column').forEach(function(col){
      var colProj=col.querySelector('.col-title')?col.querySelector('.col-title').textContent.trim():'';
      var projOk=curProj==='all'||colProj===curProj;
      col.classList.toggle('hidden',!projOk);
      if(projOk){
        col.querySelectorAll('.card').forEach(function(card){
          card.classList.toggle('hidden',curFilter!=='all'&&card.dataset.status!==curFilter);
        });
      }
    });
  }
  updateCounts();
}
function updateCounts(){
  var board=curView==='status'?document.getElementById('board-status'):document.getElementById('board-project');
  var total=0;
  board.querySelectorAll('.column:not(.hidden)').forEach(function(col){
    var st=col.dataset.status;
    if(st==='no_project'){ return; }
    var visible;
    if(st==='unassigned'){
      visible=col.querySelectorAll('.ua-item:not(.hidden)').length;
    }else{
      visible=col.querySelectorAll('.card:not(.hidden)').length;
      total+=visible;
    }
    var badge=col.querySelector('.col-count');
    if(badge) badge.textContent=String(visible);
  });
  var meta=document.getElementById('kanban-meta');
  if(meta){
    var totalAll=parseInt(meta.dataset.total,10)||0;
    var gen=meta.dataset.gen||'';
    if(curFilter==='all'&&curProj==='all'){
      meta.textContent=totalAll+' tasks · '+gen;
    }else{
      meta.textContent=total+' / '+totalAll+' tasks · '+gen;
    }
  }
}
function applyTheme(t){
  document.documentElement.dataset.theme=t;
  var b=document.getElementById('theme-toggle');
  if(b) b.textContent=t==='dark'?'☀ Light':'\U0001f319 Dark';
  localStorage.setItem('tfTheme',t);
}
function toggleTheme(){
  var cur=document.documentElement.dataset.theme==='dark'?'dark':'light';
  applyTheme(cur==='dark'?'light':'dark');
}
var mdStack=[], mdCurrentUrl=null;
function mdFetch(url){
  var modal=document.getElementById('md-modal');
  var title=document.getElementById('md-title');
  var mbody=document.getElementById('md-body');
  var back=document.getElementById('md-back');
  fetch(url).then(function(r){return r.ok?r.json():Promise.reject(r.status);})
    .then(function(d){
      modal.classList.remove('hidden');
      title.textContent=d.title||'';
      mbody.innerHTML=d.html||'';
      mbody.scrollTop=0;
      mdCurrentUrl=url;
      if(back) back.classList.toggle('hidden', mdStack.length===0);
    }).catch(function(){
      modal.classList.remove('hidden');
      title.textContent='Error';
      mbody.textContent='Could not load the file.';
    });
}
function openMdModal(url){ mdStack=[]; mdCurrentUrl=null; mdFetch(url); }
function closeMdModal(){ document.getElementById('md-modal').classList.add('hidden'); mdStack=[]; mdCurrentUrl=null; }
document.addEventListener('click',function(e){
  var md=e.target.closest('.view-md-btn');
  if(md){ openMdModal(md.dataset.md); return; }
  var mdlink=e.target.closest('#md-body a[data-mdlink]');
  if(mdlink){
    e.preventDefault();
    var abs=mdlink.getAttribute('data-mdlink');
    if(abs){ if(mdCurrentUrl) mdStack.push(mdCurrentUrl); mdFetch('/md?path='+encodeURIComponent(abs)); }
    return;
  }
  if(e.target.closest('#md-back')){ if(mdStack.length>0){ mdFetch(mdStack.pop()); } return; }
  if(e.target.classList.contains('md-modal-backdrop')){ closeMdModal(); return; }
  if(e.target.closest('#md-close')){ closeMdModal(); return; }
});
document.addEventListener('keydown',function(e){
  if(e.key==='Escape' && !document.getElementById('md-modal').classList.contains('hidden')) closeMdModal();
});
(function(){
  applyTheme(localStorage.getItem('tfTheme')||'light');
  var v=localStorage.getItem('tfView')||'status';
  setView(v);
})();
"""

    return f"""\
<!DOCTYPE html>
<html lang="ja" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>taskflow kanban</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>taskflow kanban</h1>
  <span class="meta" id="kanban-meta" data-total="{total}" data-gen="{generated_at}">{total} tasks &middot; {generated_at}</span>
  <button class="theme-toggle" id="theme-toggle" onclick="toggleTheme()">🌙 Dark</button>
  <div class="toggle">
    <button id="btn-status" onclick="setView('status')">By Status</button>
    <button id="btn-project" onclick="setView('project')">By Project</button>
  </div>
</header>
{filter_bar_html}
<div class="legend">{legend_html}</div>
<div id="board-status" class="board">
{status_cols}
</div>
<div id="board-project" class="board">
{proj_cols}
</div>
<div id="md-modal" class="md-modal hidden">
  <div class="md-modal-backdrop"></div>
  <div class="md-modal-container">
    <div class="md-modal-header">
      <button id="md-back" class="md-btn md-back hidden">←</button>
      <span id="md-title" class="md-title"></span>
      <button id="md-close" class="md-btn md-close">×</button>
    </div>
    <div id="md-body" class="md-body"></div>
  </div>
</div>
<script>{js}</script>
</body>
</html>
"""


# ── main ───────────────────────────────────────────────────────────────────


PORT_BASE = 17329
PORT_SPAN = 64  # candidate ports: PORT_BASE .. PORT_BASE + PORT_SPAN - 1
SERVE_PORT = PORT_BASE  # reassigned once resolved, in main()'s --serve path (D-notation: see spec)
SESSION_RE = re.compile(r'^[0-9a-f-]+$')
APP_ID = "taskflow-kanban"


def _workspace_key(roots: list[Path]) -> str:
    """Identity for a kanban server: the resolved roots it serves.

    Same roots (any order) → same key → same derived port and "ours" match.
    Different roots → different key, so a per-workspace server never mistakes
    another workspace's server (even one occupying its hash-derived port) for
    its own."""
    return "\n".join(sorted(str(r.resolve()).lower() for r in roots))


def _derive_port(key: str) -> int:
    """Deterministic starting port for a workspace key.

    Uses hashlib (not the builtin `hash()`, which is salted per-process via
    PYTHONHASHSEED) so independent processes — e.g. a later `--stop` — derive
    the same starting port for the same workspace."""
    h = int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16)
    return PORT_BASE + (h % PORT_SPAN)


def _port_state_path(roots: list[Path], key: str) -> Path | None:
    """Where this workspace's last-bound kanban port is recorded.

    Lives under the primary root's ``_state/`` (same place session state
    already lives). The filename embeds a short digest of the workspace key
    so two workspaces that happen to share ``roots[0]`` (distinct secondary
    roots) never collide on the same file; the record is also key-checked
    before being trusted regardless (see `resolve_workspace_port`), so a
    collision here would degrade to a wasted read, never a misattribution.
    """
    if not roots:
        return None
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return roots[0] / "_state" / f"kanban-port-{digest}.json"


def _read_persisted_port(roots: list[Path], key: str) -> int | None:
    """Last port this workspace successfully bound, if the record matches."""
    path = _port_state_path(roots, key)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("key") != key:
        return None
    port = data.get("port")
    return port if isinstance(port, int) else None


def _persist_port(roots: list[Path], key: str, port: int, pid: int) -> None:
    """Record the port this workspace just bound (F1: so a server displaced
    to a fallback slot by a hash collision is found directly next time,
    instead of appearing to have vanished once the collision's occupant is
    gone). Best-effort — a failed write only costs an extra derive+scan on
    the next call, never a correctness problem (the record is re-verified by
    probing before it is trusted)."""
    path = _port_state_path(roots, key)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": key, "port": port, "pid": pid}), encoding="utf-8")
    except OSError:
        pass


def _clear_persisted_port(roots: list[Path], key: str, port: int) -> None:
    """Remove this workspace's port record after a clean `--stop` of it."""
    path = _port_state_path(roots, key)
    if path is None or not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("key") == key and data.get("port") == port:
            path.unlink()
    except (OSError, ValueError):
        pass


def script_version() -> str:
    """Identity for /health: the script's mtime (D3, zero-maintenance)."""
    try:
        return str(int(Path(__file__).stat().st_mtime))
    except OSError:
        return "0"


class KanbanServer(ThreadingHTTPServer):
    # Threaded: the board page holds a connection while the MD modal fetches
    # /file images and /md pages concurrently — a single-threaded server would
    # serialize and can wedge on a held connection, starving /health.
    daemon_threads = True
    # PH-4: do NOT set SO_REUSEADDR — on Windows it lets a 2nd server co-bind an
    # active port and silently shadow the running one.  With this False, a 2nd
    # bind raises OSError instead, which the serve path reports (P3).
    allow_reuse_address = False


def port_status(port: int, timeout: float = 2.0) -> tuple[str, dict | None]:
    """Probe ``127.0.0.1:<port>/health`` → ``(state, info)``.

    state ∈ {"free", "ours", "foreign"}.  Nothing listening → free; our
    /health signature → ours (info carries pid/version/key); any other
    response → foreign (port occupied by an unrelated service).

    Uses the literal IP (not ``localhost``) because ``KanbanServer`` binds
    IPv4-only (``HTTPServer.address_family = AF_INET``); probing ``localhost``
    lets the resolver additionally try the (here, unbound) IPv6 loopback
    first, which on some Windows setups adds several seconds of pure refusal
    latency before falling back to IPv4 — doubling the cost of every probe
    for no benefit. ``timeout`` is shortened by callers that probe many
    candidate ports in one pass (`resolve_workspace_port`'s collision-scan
    fallback) — measured on this environment, even a *refused* localhost
    connection can take ~2s to surface, so scanning a wide span at the
    default timeout would make `--serve` startup unacceptably slow.
    """
    import urllib.error
    import urllib.request
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read(4096)
    except urllib.error.HTTPError:
        return ("foreign", None)
    except (urllib.error.URLError, OSError):
        return ("free", None)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return ("foreign", None)
    if isinstance(data, dict) and data.get("app") == APP_ID:
        return ("ours", data)
    return ("foreign", None)


def resolve_workspace_port(
    roots: list[Path], explicit_port: int = 0, scan_timeout: float = 0.5
) -> tuple[int, str, dict | None]:
    """Resolve the port this workspace's kanban server uses.

    Returns ``(port, state, info)`` with ``state`` one of:
      - ``"ours"``:  a server for this exact workspace (key match) is already
        running on the returned port.
      - ``"free"``:  the returned port is free to bind.
      - ``"foreign"``: ``explicit_port`` is occupied by an unrelated service
        (only reachable when ``explicit_port`` is given).
      - ``"full"``: no free port and no "ours" match anywhere in the span
        (only reachable when ``explicit_port`` is not given).

    Without ``explicit_port``, first checks this workspace's persisted
    last-bound port (if any — see `_read_persisted_port`), then falls back to
    the derived port. Either check is a single probe, covering the common
    case (nothing running yet, our own server at its natural slot, or our
    own server previously displaced to a fallback slot by a hash collision)
    without a wide scan. A persisted port that no longer answers as "ours"
    (server stopped, crashed, or the port was reused) is simply stale and
    falls through — self-healing on the next successful bind. Only when
    neither check finds our server does it fall back to scanning the rest of
    the span (at a shorter ``scan_timeout``) for either our server (should
    both prior checks have missed it — belt and suspenders) or the next free
    slot. Measured on Windows, even a *refused* loopback probe can take ~2s
    to surface — unconditionally scanning the whole span on every invocation
    would make `--serve` startup unacceptably slow for the overwhelmingly
    common case."""
    key = _workspace_key(roots)
    if explicit_port:
        state, info = port_status(explicit_port)
        ours = state == "ours" and bool(info) and info.get("key") == key
        return explicit_port, ("ours" if ours else state), info

    persisted = _read_persisted_port(roots, key)
    persisted_state: str | None = None
    persisted_info: dict | None = None
    if persisted is not None:
        persisted_state, persisted_info = port_status(persisted)
        if persisted_state == "ours" and persisted_info and persisted_info.get("key") == key:
            return persisted, "ours", persisted_info
        # stale record (server gone, or port reused) — fall through, but
        # reuse this probe below if it happens to be the same as `start`.

    start = _derive_port(key)
    if start == persisted:
        state, info = persisted_state, persisted_info  # already probed above
    else:
        state, info = port_status(start)
    if state == "free":
        return start, "free", None
    if state == "ours" and info and info.get("key") == key:
        return start, "ours", info

    # start is occupied by something else (foreign, or another workspace's
    # server that landed here via its own collision fallback) — rare path.
    first_free = None
    for i in range(1, PORT_SPAN):
        port = PORT_BASE + ((start - PORT_BASE + i) % PORT_SPAN)
        st, inf = port_status(port, timeout=scan_timeout)
        if st == "ours" and inf and inf.get("key") == key:
            return port, "ours", inf
        if st == "free" and first_free is None:
            first_free = port
    if first_free is not None:
        return first_free, "free", None
    return start, "full", None


def _report_already_serving(url: str, info: dict | None) -> None:
    pid = info.get("pid", "?") if info else "?"
    ver = info.get("version", "?") if info else "?"
    print(
        f"[kanban] already serving at {url} (pid {pid}, v{ver}); stop it first with --stop",
        file=sys.stderr,
    )


def stop_server(
    roots: list[Path], explicit_port: int = 0, stop_all: bool = False, scan_timeout: float = 0.5,
) -> int:
    """PH-5/D6: stop this workspace's kanban server via its /health pid.

    ``stop_all`` scans the whole port span and stops every kanban server
    found (any workspace) — a cleanup sweep independent of the workspace-key
    matching used for the normal single-server stop. Uses the same short
    ``scan_timeout`` as `resolve_workspace_port`'s collision fallback (see
    its docstring — a full-span scan at the default probe timeout is
    unacceptably slow on this environment)."""
    import signal as _signal

    if stop_all:
        stopped = 0
        for i in range(PORT_SPAN):
            port = PORT_BASE + i
            state, info = port_status(port, timeout=scan_timeout)
            if state != "ours" or not info:
                continue
            pid = info.get("pid")
            if not isinstance(pid, int):
                continue
            try:
                os.kill(pid, _signal.SIGTERM)
            except OSError as e:
                print(f"[kanban] failed to stop port {port} pid {pid}: {e}", file=sys.stderr)
                continue
            print(f"[kanban] stopped kanban server on port {port} (pid {pid})", file=sys.stderr)
            stopped += 1
        if stopped == 0:
            print("[kanban] no kanban servers running", file=sys.stderr)
        return 0

    port, state, info = resolve_workspace_port(roots, explicit_port)
    if state != "ours" or not info:
        print(
            f"[kanban] no kanban server running for this workspace (checked port {port})",
            file=sys.stderr,
        )
        return 0
    pid = info.get("pid")
    if not isinstance(pid, int):
        print("[kanban] running server reported no pid; stop it manually", file=sys.stderr)
        return 1
    try:
        os.kill(pid, _signal.SIGTERM)
    except OSError as e:
        print(f"[kanban] failed to stop pid {pid}: {e}", file=sys.stderr)
        return 1
    _clear_persisted_port(roots, _workspace_key(roots), port)
    print(f"[kanban] stopped kanban server on port {port} (pid {pid})", file=sys.stderr)
    return 0


def detect_scheme() -> str:
    return "vscodium" if shutil.which("codium") else "vscode"


CLAUDE_CODE_EXT_ID = "anthropic.claude-code"


@functools.lru_cache(maxsize=1)
def _resolve_cc_launcher() -> tuple[str | None, bool]:
    """Resolve the VS Code / VSCodium CLI and whether the Claude Code extension
    is installed in it.

    Returns ``(cmd, has_ext)`` where ``cmd`` is the launcher path (or ``None``
    if neither ``codium`` nor ``code`` is on PATH) and ``has_ext`` is whether
    ``anthropic.claude-code`` appears in ``<cmd> --list-extensions``.

    ``lru_cache`` makes this probe run at most once per process — the
    ``--list-extensions`` call (~0.36s) happens on the first ``/open`` and is
    cached for the server's lifetime.
    """
    cmd = shutil.which("codium") or shutil.which("code")
    if not cmd:
        return None, False
    try:
        proc = subprocess.run(
            [cmd, "--list-extensions"],
            capture_output=True, text=True,
            # Without an explicit codec, text mode decodes with the platform
            # default (cp932 on JA Windows) and one undecodable byte raises
            # UnicodeDecodeError -- which the except below does NOT catch, so
            # it would escape this probe entirely. Extension IDs are ASCII, so
            # replace never fires in practice; it is here so the failure mode
            # cannot exist at all.
            encoding="utf-8", errors="replace", timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return cmd, False
    exts = {line.strip().lower() for line in proc.stdout.splitlines()}
    return cmd, CLAUDE_CODE_EXT_ID in exts


def _launch_error_html(message: str, manual_hint: str) -> bytes:
    """Small HTML page shown when a CC link cannot be launched, carrying the
    session UUID / prompt so the user can open it manually."""
    return (
        "<!DOCTYPE html><html><head><meta charset=UTF-8>"
        "<title>Claude Code launch failed</title></head><body>"
        "<h3>Could not open in Claude Code</h3>"
        f"<p>{esc(message)}</p>"
        f"<p>Open it manually — {esc(manual_hint)}</p>"
        "</body></html>"
    ).encode("utf-8")


def open_browser(url: str) -> None:
    if sys.platform == "win32":
        os.startfile(url)
    elif sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    else:
        subprocess.run(["xdg-open", url], check=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


IMAGE_MIME_INLINE = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/x-icon",
}


def _resolve_under_roots(base_dir: Path, ref: str, roots: list[Path]) -> Path | None:
    """Resolve ``ref`` relative to ``base_dir``; return it only if under a root."""
    from urllib.parse import unquote
    try:
        target = (base_dir / unquote(ref)).resolve()
    except (OSError, ValueError):
        return None
    if any(_is_within(target, r.resolve()) for r in roots):
        return target
    return None


def _postprocess_md_links(html: str, md_dir: Path, roots: list[Path]) -> str:
    """Rewrite relative img/links for the browser modal (M2, Pi-parity).

    - ``<img src=rel>``  → ``/file?path=<abs>`` (served with nosniff).
    - ``<a href=rel.md>``→ in-modal navigation marker (``data-mdlink``).
    - ``<a href=rel>``   → ``/file?path=<abs>`` in a new tab.
    - external http(s)/mailto → open in a new tab.
    Only paths under a project root are rewritten; anything else is left as-is.
    """
    from urllib.parse import quote

    def img_sub(m: re.Match) -> str:
        pre, src, post = m.group(1), m.group(2), m.group(3)
        if src.startswith(("http://", "https://", "data:", "/file?")):
            return m.group(0)
        t = _resolve_under_roots(md_dir, src, roots)
        if not t or not t.is_file():
            return m.group(0)
        return f'<img {pre}src="/file?path={quote(str(t))}"{post}>'

    html = re.sub(r'<img\s+([^>]*?)src="([^"]*)"([^>]*?)>', img_sub, html)

    def a_sub(m: re.Match) -> str:
        pre, href, post = m.group(1), m.group(2), m.group(3)
        if href.startswith(("http://", "https://", "mailto:")):
            if "target=" in pre or "target=" in post:
                return m.group(0)
            return f'<a {pre}href="{href}"{post} target="_blank">'
        if href.startswith("#") or href.startswith("/file?"):
            return m.group(0)
        t = _resolve_under_roots(md_dir, href, roots)
        if not t or not t.is_file():
            return m.group(0)
        if t.suffix == ".md":
            return f'<a {pre}href="#" data-mdlink="{esc(str(t))}"{post}>'
        return f'<a {pre}href="/file?path={quote(str(t))}"{post} target="_blank">'

    html = re.sub(r'<a\s+([^>]*?)href="([^"]*)"([^>]*?)>', a_sub, html)
    return html


PATH_TEXT_RE = re.compile(
    r'(?:_projects/[\w-]+/)?(?:project-notes|llm-handoff|handoff|tasks|plans)(?:/[\w.@-]+)+\.md'
)


def _linkify_path_text(html: str, md_path: Path, roots: list[Path]) -> str:
    """Linkify bare taskflow path references in text/code (Pi postProcessPathLinks).

    Task bodies cite related files as inline-code paths (e.g. ``tasks/0_todo/x.md``)
    rather than markdown links; turn the resolvable ``.md`` ones into in-modal
    navigation links.  Existing <a> anchors are protected from double-linking.
    """
    root: Path | None = None
    for r in roots:
        rr = r.resolve()
        if _is_within(md_path.resolve(), rr):
            root = rr
            break
    if root is None:
        return html
    try:
        proj = md_path.resolve().relative_to(root).parts[0]
    except (ValueError, IndexError):
        return html
    project_root = root / proj

    def resolve(ref: str) -> Path | None:
        base = root.parent if ref.startswith("_projects/") else project_root
        try:
            t = (base / ref).resolve()
        except (OSError, ValueError):
            return None
        if t.suffix == ".md" and t.is_file() and any(_is_within(t, r.resolve()) for r in roots):
            return t
        return None

    anchors: list[str] = []

    def _stash(m: re.Match) -> str:
        anchors.append(m.group(0))
        return f"\x00A{len(anchors) - 1}\x00"

    safe = re.sub(r'<a\s[^>]*>.*?</a>', _stash, html, flags=re.DOTALL)

    def _one(pm: re.Match) -> str:
        ref = pm.group(0)
        t = resolve(ref)
        if not t:
            return ref
        return f'<a href="#" class="path-link" data-mdlink="{esc(str(t))}">{ref}</a>'

    def _text_sub(m: re.Match) -> str:
        return m.group(1) + PATH_TEXT_RE.sub(_one, m.group(2)) + m.group(3)

    safe = re.sub(r'(>)([^<]+)(<)', _text_sub, safe)
    return re.sub('\x00A(\\d+)\x00', lambda m: anchors[int(m.group(1))], safe)


def render_markdown_file(path: Path, roots: list[Path]) -> str:
    """Render a task ``.md`` to HTML for the /md modal (D1: server-side).

    Frontmatter is shown as a fenced ``yaml`` block, matching the Pi viewer.
    Output is sanitized with nh3 (M4, DOMPurify-parity) then relative
    image/link targets are rewritten for the browser (M2).
    """
    import markdown as _md
    import nh3
    text = read_text(path) or ""
    m = FRONTMATTER_RE.match(text)
    if m:
        text = "```yaml\n" + m.group(1) + "\n```\n\n" + text[m.end():]
    html = _md.markdown(text, extensions=["fenced_code", "tables"])
    html = nh3.clean(html)
    html = _postprocess_md_links(html, path.parent, roots)
    html = _linkify_path_text(html, path, roots)
    return html


def make_handler(
    build_html, scheme: str, roots: list[Path], open_token: str = "", key: str = "", port: int = 0,
):
    host_allowlist = {
        f"localhost:{port}",
        f"127.0.0.1:{port}",
        "localhost",
        "127.0.0.1",
        f"[::1]:{port}",
    }

    class KanbanHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            host = self.headers.get("Host", "")
            if host not in host_allowlist:
                self._respond(403, b"forbidden")
                return
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                payload = json.dumps(
                    {"app": APP_ID, "pid": os.getpid(), "version": script_version(), "key": key}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path == "/file":
                import mimetypes
                from urllib.parse import unquote
                qs = parse_qs(parsed.query)
                raw = qs.get("path", [""])[0]
                if not raw:
                    self._respond(400, b"missing path")
                    return
                target = Path(unquote(raw)).resolve()
                if (
                    not target.is_file()
                    or not any(_is_within(target, r.resolve()) for r in roots)
                ):
                    self._respond(403, b"forbidden")
                    return
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                inline = ctype in IMAGE_MIME_INLINE
                try:
                    data = target.read_bytes()
                except OSError:
                    self._respond(500, b"read error")
                    return
                self.send_response(200)
                # Non-image (incl. SVG) is forced to download so it cannot execute
                # as same-origin HTML/script; images render inline with nosniff.
                self.send_header("Content-Type", ctype if inline else "application/octet-stream")
                self.send_header("X-Content-Type-Options", "nosniff")
                if not inline:
                    self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if parsed.path == "/md":
                from urllib.parse import unquote
                qs = parse_qs(parsed.query)
                raw = qs.get("path", [""])[0]
                if not raw:
                    self._respond(400, b"missing path")
                    return
                target = Path(unquote(raw)).resolve()
                if (
                    target.suffix != ".md"
                    or not target.is_file()
                    or not any(_is_within(target, r.resolve()) for r in roots)
                ):
                    self._respond(403, b"forbidden")
                    return
                try:
                    html = render_markdown_file(target, roots)
                except Exception:
                    self._respond(500, b"render error")
                    return
                payload = json.dumps({"title": target.name, "html": html}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif parsed.path == "/open":
                from urllib.parse import quote, unquote
                qs = parse_qs(parsed.query)
                provided_t = qs.get("t", [""])[0]
                if not hmac.compare_digest(provided_t, open_token):
                    self._respond(403, b"forbidden")
                    return
                if "session" in qs:
                    session = qs["session"][0]
                    if not session or not SESSION_RE.match(session):
                        self._respond(400, b"bad session")
                        return
                    uri = f"{scheme}://anthropic.claude-code/open?session={session}"
                    manual_hint = f"session {session}"
                elif "prompt" in qs:
                    prompt = qs["prompt"][0][:500]
                    uri = f"{scheme}://anthropic.claude-code/open?prompt={quote(prompt)}"
                    manual_hint = prompt
                else:
                    self._respond(400, b"missing param")
                    return
                cmd, has_ext = _resolve_cc_launcher()
                if cmd is None:
                    self._respond(
                        200,
                        _launch_error_html(
                            "No VS Code / VSCodium CLI (code / codium) found on PATH.",
                            manual_hint,
                        ),
                        content_type="text/html; charset=utf-8",
                    )
                    return
                if not has_ext:
                    self._respond(
                        200,
                        _launch_error_html(
                            f"The Claude Code extension ({CLAUDE_CODE_EXT_ID}) is not "
                            "installed in the detected editor.",
                            manual_hint,
                        ),
                        content_type="text/html; charset=utf-8",
                    )
                    return
                try:
                    subprocess.Popen(
                        [cmd, "--open-url", uri],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError as e:
                    self._respond(
                        200,
                        _launch_error_html(f"Failed to launch editor: {e}", manual_hint),
                        content_type="text/html; charset=utf-8",
                    )
                    return
                self._respond(200, b"<!DOCTYPE html><html><head><meta charset=UTF-8>"
                             b"<script>window.close();</script></head>"
                             b"<body>opening...</body></html>",
                             content_type="text/html; charset=utf-8")
            elif parsed.path in ("/", ""):
                body = build_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._respond(404, b"not found")

        def _respond(self, code: int, body: bytes, content_type: str = "text/plain") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # suppress request logs
            pass

    return KanbanHandler


NO_PROJECT_CAP = 50


def load_projects(
    roots: list[Path],
    uuid_index: dict[str, StateEntry],
) -> tuple[list[Project], list[SessionRef], int]:
    """Return ``(projects, no_project_sessions, no_project_total)``."""
    seen: set[str] = set()
    project_defs: list[tuple[str, str]] = []
    for root in roots:
        for name, desc in parse_index(root / "index.md"):
            if name not in seen:
                seen.add(name)
                project_defs.append((name, desc))

    projects: list[Project] = []
    for name, desc in project_defs:
        proj_dir = find_project_dir(name, roots)
        if proj_dir is None:
            print(f"[kanban] warn: directory not found for project '{name}'", file=sys.stderr)
            continue
        tasks = load_tasks(proj_dir, name, uuid_index)
        projects.append(Project(name=name, description=desc, tasks=tasks))
        print(f"[kanban] {name}: {len(tasks)} tasks", file=sys.stderr)

    referenced: set[str] = set()
    for proj in projects:
        for task in proj.tasks:
            for s in task.sessions:
                if s.full_uuid:
                    referenced.add(s.full_uuid)
    cc_index = build_cc_session_index()
    attach_unassigned_sessions(projects, uuid_index, referenced, cc_index)
    no_project, no_project_total = collect_no_project_sessions(uuid_index, referenced, cc_index)
    return projects, no_project, no_project_total


def attach_unassigned_sessions(
    projects: list[Project],
    uuid_index: dict[str, StateEntry],
    referenced: set[str],
    cc_index: dict[str, Path],
) -> None:
    """Attach CC sessions attributed to a project but referenced by no task (§7).

    Only ``origin == "cc"`` entries are considered — pi sessions cannot be
    reopened from the browser, so they are intentionally excluded.
    """
    proj_map = {p.name: p for p in projects}
    for entry in uuid_index.values():
        if entry.origin != "cc" or not entry.project or entry.uuid in referenced:
            continue
        proj = proj_map.get(entry.project)
        if proj is None:
            continue
        date, summary = "", ""
        jsonl = cc_index.get(entry.uuid)
        if jsonl:
            date, summary = read_cc_session_first_message(jsonl)
        proj.unassigned_sessions.append(SessionRef(
            date=date, short_id=entry.uuid[:8], summary=summary, full_uuid=entry.uuid,
        ))
    for proj in projects:
        proj.unassigned_sessions.sort(key=lambda s: s.date, reverse=True)


def collect_no_project_sessions(
    uuid_index: dict[str, StateEntry],
    referenced: set[str],
    cc_index: dict[str, Path],
    cap: int = NO_PROJECT_CAP,
) -> tuple[list[SessionRef], int]:
    """CC sessions with no project attribution at all ("No Project").

    Sorted by session-file mtime (a cheap stat); only the newest ``cap`` have
    their first message read, so head I/O is bounded regardless of backlog.
    Returns the capped list plus the full candidate count.
    """
    cands = [
        e for e in uuid_index.values()
        if e.origin == "cc" and not e.project and e.uuid not in referenced
    ]
    total = len(cands)
    if not cands:
        return [], 0

    def _mtime(e: StateEntry) -> float:
        p = cc_index.get(e.uuid)
        if not p:
            return 0.0
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    cands.sort(key=_mtime, reverse=True)
    out: list[SessionRef] = []
    for e in cands[:cap]:
        date, summary = "", ""
        p = cc_index.get(e.uuid)
        if p:
            date, summary = read_cc_session_first_message(p)
        out.append(SessionRef(
            date=date, short_id=e.uuid[:8], summary=summary, full_uuid=e.uuid,
        ))
    out.sort(key=lambda s: s.date, reverse=True)
    return out, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate taskflow HTML kanban.")
    parser.add_argument(
        "--out", type=Path, default=Path(tempfile.gettempdir()) / "taskflow-kanban.html",
        help="Output HTML path (default: system temp dir / taskflow-kanban.html)",
    )
    parser.add_argument(
        "--open", action="store_true", help="Open in default browser after generating",
    )
    parser.add_argument(
        "--serve", action="store_true",
        help=f"Serve on a workspace-derived port (base {PORT_BASE}) with /open?session=UUID endpoint",
    )
    parser.add_argument(
        "--scheme", default="", help="URI scheme override: vscode or vscodium",
    )
    parser.add_argument(
        "--stop", action="store_true",
        help="Stop this workspace's running kanban --serve instance (via its /health pid)",
    )
    parser.add_argument(
        "--port", type=int, default=0,
        help="Explicit port for --serve/--stop (default: derived from this workspace's _projects roots)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="With --stop, stop every kanban server across all workspaces",
    )
    args = parser.parse_args(argv)

    roots = [r for r in _project_roots() if r.is_dir()]

    if args.stop:
        if not roots and not args.all:
            print("error: no _projects/ directory found", file=sys.stderr)
            return 2
        return stop_server(roots, args.port, stop_all=args.all)

    scheme = args.scheme or detect_scheme()

    if not roots:
        print("error: no _projects/ directory found", file=sys.stderr)
        return 2

    uuid_index: dict[str, StateEntry] = {}
    for root in roots:
        uuid_index.update(build_uuid_index(root / "_state"))
    print(f"[kanban] sessions indexed: {len(uuid_index)}", file=sys.stderr)

    projects, no_project, no_project_total = load_projects(roots, uuid_index)
    total = sum(len(p.tasks) for p in projects)

    if args.serve:
        global SERVE_PORT
        key = _workspace_key(roots)
        port, state, info = resolve_workspace_port(roots, args.port)
        url = f"http://localhost:{port}/"
        if state == "ours":
            _report_already_serving(url, info)
            return 0
        if state == "full":
            print(
                f"[kanban] no free port in {PORT_BASE}-{PORT_BASE + PORT_SPAN - 1} for this "
                f"workspace; stop unused servers first (--stop --all)",
                file=sys.stderr,
            )
            return 1
        if state == "foreign":
            print(
                f"[kanban] port {port} is in use by another service; not starting",
                file=sys.stderr,
            )
            return 1

        SERVE_PORT = port
        open_token = secrets.token_urlsafe(16)

        def build_html():
            uuid_idx: dict[str, StateEntry] = {}
            for r in roots:
                uuid_idx.update(build_uuid_index(r / "_state"))
            projs, np, npt = load_projects(roots, uuid_idx)
            return render_html(projs, scheme, serve=True, no_project=np, no_project_total=npt, open_token=open_token)
        handler = make_handler(build_html, scheme, roots, open_token, key=key, port=port)
        try:
            server = KanbanServer(("localhost", port), handler)
        except OSError as e:
            # PH-4: bind lost a race with a concurrent start; re-probe so the
            # message matches the P3 primary path instead of a raw bind error.
            st2, inf2 = port_status(port)
            if st2 == "ours" and inf2 and inf2.get("key") == key:
                _report_already_serving(url, inf2)
                return 0
            print(f"[kanban] cannot bind port {port}: {e}", file=sys.stderr)
            return 1
        _persist_port(roots, key, port, os.getpid())
        print(f"[kanban] serving {total} tasks at {url} (Ctrl+C to stop)", file=sys.stderr)
        open_browser(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[kanban] stopped", file=sys.stderr)
        return 0

    html = render_html(
        projects, scheme, serve=False, no_project=no_project, no_project_total=no_project_total
    )
    args.out.write_text(html, encoding="utf-8")
    print(f"[kanban] generated: {args.out}", file=sys.stderr)

    if args.open:
        open_browser(str(args.out))

    print(str(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
