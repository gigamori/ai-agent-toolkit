#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""generate_kanban.py — Trello-like HTML kanban for taskflow projects.

Reads all projects from _projects/index.md (both primary and secondary roots),
enumerates tasks, extracts session references from @log blocks, resolves full
UUIDs from _state/, and emits a self-contained HTML file.

Usage:
    uv run python generate_kanban.py [--out PATH] [--open] [--scheme vscode|vscodium]

Exit codes:
    0 = success
    2 = error (no _projects/ dir found)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

# ── paths ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent          # plugins/taskflow/scripts
PLUGIN_DIR = SCRIPT_DIR.parent              # plugins/taskflow
REPO_ROOT  = PLUGIN_DIR.parent.parent       # ai-agent-toolkit root
PRIMARY_ROOT   = REPO_ROOT / "_projects"    # <ai-agent-toolkit>/_projects
SECONDARY_ROOT = Path("<secondary-projects-root>")  # known secondary location

# ── regex ──────────────────────────────────────────────────────────────────

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
LOG_ENTRY_RE = re.compile(
    r"-\s+(\d{4}-\d{2}-\d{2})\s+\[s:([0-9a-f]{6,})\]:\s*(.+)"
)

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


@dataclass
class Project:
    name: str
    description: str
    tasks: list[Task] = field(default_factory=list)


# ── file helpers ───────────────────────────────────────────────────────────


def read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


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
    if not log_m:
        return []
    refs = []
    for m in LOG_ENTRY_RE.finditer(log_m.group(1)):
        refs.append(SessionRef(
            date=m.group(1),
            short_id=m.group(2),
            summary=m.group(3).strip(),
        ))
    return refs


def parse_index(path: Path) -> list[tuple[str, str]]:
    """Parse _projects/index.md → [(name, description), ...]."""
    content = read_text(path)
    if not content:
        return []
    result = []
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if not parts or parts[0] in ("Project", ""):
            continue
        name = parts[0]
        desc = parts[1] if len(parts) > 1 else ""
        result.append((name, desc))
    return result


# ── data loading ───────────────────────────────────────────────────────────


def build_uuid_index(state_dir: Path) -> dict[str, str]:
    """Map short_id (first 8 hex chars) → full UUID string."""
    index: dict[str, str] = {}
    if not state_dir.is_dir():
        return index
    for f in state_dir.iterdir():
        if f.suffix == ".json" and len(f.stem) == 36:
            index[f.stem[:8]] = f.stem
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
    uuid_index: dict[str, str],
) -> list[Task]:
    tasks: list[Task] = []
    tasks_dir = project_dir / "tasks"
    if not tasks_dir.is_dir():
        return tasks
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
            sessions = extract_sessions(content)
            for s in sessions:
                s.full_uuid = uuid_index.get(s.short_id, "")
            tasks.append(Task(
                status=status,
                h1=h1,
                priority=priority,
                project=project_name,
                created=created,
                updated=updated,
                sessions=sessions,
            ))
    return tasks


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


def session_url(s: SessionRef, scheme: str, serve: bool) -> str:
    if serve:
        return f"http://localhost:{SERVE_PORT}/open?session={s.full_uuid}"
    return f"{scheme}://anthropic.claude-code/open?session={s.full_uuid}"


PROGRESS_SUBS = ["check", "audit", "rebuild"]


def progress_url(project: str, sub: str, scheme: str, serve: bool) -> str:
    from urllib.parse import quote
    prompt = f"pj:{project} /progress {sub}"
    if serve:
        return f"http://localhost:{SERVE_PORT}/open?prompt={quote(prompt)}"
    return f"{scheme}://anthropic.claude-code/open?prompt={quote(prompt)}"


def render_progress_picker(project: str, scheme: str, serve: bool) -> str:
    items = ""
    for sub in PROGRESS_SUBS:
        url = progress_url(project, sub, scheme, serve)
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
) -> str:
    pri_color = PRIORITY_COLORS.get(task.priority, "#718096")
    pri_label = task.priority or "—"
    unique_sessions = dedup_sessions(task.sessions)

    if unique_sessions:
        items = ""
        for s in unique_sessions:
            summary_short = s.summary[:72] + ("…" if len(s.summary) > 72 else "")
            if s.full_uuid:
                url = session_url(s, scheme, serve)
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


def render_html(projects: list[Project], scheme: str, serve: bool = False) -> str:
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
        bg = STATUS_BG[status]
        cards = "".join(
            render_card(t, proj_colors[t.project], scheme, serve) for t in tasks
        ) or '<p class="empty-col">No tasks</p>'
        status_cols += f"""\
<div class="column" data-status="{status}" style="background:{bg}">
  <div class="col-header" style="border-bottom:3px solid {hdr_color}">
    <span class="col-title" style="color:{hdr_color}">{STATUS_LABELS[status]}</span>
    <span class="col-count" style="background:{hdr_color}">{len(tasks)}</span>
  </div>
  <div class="cards">{cards}</div>
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
            render_card(t, color, scheme, serve, show_status_badge=True)
            for t in sorted_tasks
        ) or '<p class="empty-col">No tasks</p>'
        picker = render_progress_picker(proj.name, scheme, serve)
        proj_cols += f"""\
<div class="column" style="background:#f7fafc">
  <div class="col-header" style="border-bottom:3px solid {color}">
    <span class="col-title" style="color:{color}">{esc(proj.name)}</span>
    {picker}
  </div>
  <div class="pc-counts">{count_badges}</div>
  <div class="cards">{cards}</div>
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
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#dde1e7;min-height:100vh}
header{background:#1a202c;color:#fff;padding:10px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:17px;font-weight:700;letter-spacing:.02em}
.meta{font-size:12px;color:#a0aec0;flex:1}
.toggle{display:flex;gap:4px}
.toggle button{font-size:11px;font-weight:600;padding:4px 10px;border:1px solid #4a5568;border-radius:4px;
  background:transparent;color:#a0aec0;cursor:pointer;letter-spacing:.03em}
.toggle button.active{background:#4a5568;color:#fff;border-color:#4a5568}
.filter-bar{background:#2d3748;padding:5px 20px;display:flex;align-items:center;gap:6px}
.filter-bar span{font-size:11px;color:#718096;margin-right:4px}
.fb{font-size:11px;font-weight:600;padding:3px 10px;border:1px solid #4a5568;border-radius:12px;
  background:transparent;color:#a0aec0;cursor:pointer}
.fb.active{color:#fff}
.legend{background:#2d3748;padding:5px 20px 7px;display:flex;flex-wrap:wrap;gap:6px;border-top:1px solid #1a202c;align-items:center}
.leg{display:flex;align-items:center;gap:4px;font-size:11px;color:#a0aec0;background:transparent;
  border:1px solid #4a5568;border-radius:12px;padding:2px 8px;cursor:pointer}
.leg.active{background:var(--lc,#4a5568);border-color:var(--lc,#4a5568);color:#fff}
.leg:hover{border-color:#718096;color:#e2e8f0}
.leg-dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.board{display:flex;gap:12px;padding:14px;overflow-x:auto;align-items:flex-start;min-height:calc(100vh - 120px)}
.column{flex:0 0 310px;border-radius:8px;padding:10px;max-height:calc(100vh - 148px);overflow-y:auto}
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
.picker-menu{position:absolute;right:0;top:calc(100% + 4px);background:#fff;border:1px solid #e2e8f0;
  border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.15);z-index:100;min-width:110px;overflow:hidden}
.pm-item{display:block;padding:7px 14px;font-size:12px;color:#2d3748;text-decoration:none;white-space:nowrap}
.pm-item:hover{background:#ebf8ff;color:#2b6cb0}
.card{background:#fff;border-radius:6px;margin-bottom:8px;box-shadow:0 1px 3px rgba(0,0,0,.12);overflow:hidden}
.card.hidden{display:none}
.card summary{padding:10px 12px;cursor:pointer;list-style:none;user-select:none}
.card summary::-webkit-details-marker{display:none}
.card[open] summary{background:#f7fafc;border-bottom:1px solid #e2e8f0}
.card summary:hover{background:#f7fafc}
.card-tags{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:6px}
.badge{font-size:10px;font-weight:700;color:#fff;padding:2px 6px;border-radius:3px;text-transform:uppercase;letter-spacing:.04em}
.card-title{font-size:13px;color:#2d3748;line-height:1.4;margin-bottom:4px}
.expand-hint{font-size:10px;color:#a0aec0}
.card-body{padding:8px 12px 10px;background:#f9fafb}
ul.sessions{list-style:none;padding:0}
ul.sessions li{padding:4px 0;border-bottom:1px solid #edf2f7;font-size:11px;line-height:1.4}
ul.sessions li:last-child{border-bottom:none}
ul.sessions a{color:#3182ce;text-decoration:none;display:flex;flex-wrap:wrap;gap:4px}
ul.sessions a:hover{text-decoration:underline}
li.no-uuid{display:flex;flex-wrap:wrap;gap:4px;color:#718096}
.s-date{color:#718096;flex-shrink:0}
.s-id{color:#a0aec0;flex-shrink:0;font-family:monospace}
.s-summary{color:inherit}
.task-dates{display:flex;gap:10px;margin-bottom:6px}
.t-date{font-size:10px;color:#a0aec0}
.no-sessions{font-size:11px;color:#a0aec0;font-style:italic}
.empty-col{font-size:12px;color:#a0aec0;text-align:center;padding:24px 0;font-style:italic}
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
}
(function(){
  var v=localStorage.getItem('tfView')||'status';
  setView(v);
})();
"""

    return f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>taskflow kanban</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>taskflow kanban</h1>
  <span class="meta">{total} tasks &middot; {generated_at}</span>
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
<script>{js}</script>
</body>
</html>
"""


# ── main ───────────────────────────────────────────────────────────────────


SERVE_PORT = 17329
SESSION_RE = re.compile(r'^[0-9a-f-]+$')


def detect_scheme() -> str:
    return "vscodium" if shutil.which("codium") else "vscode"


def open_browser(url: str) -> None:
    if sys.platform == "win32":
        os.startfile(url)
    elif sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    else:
        subprocess.run(["xdg-open", url], check=False)


def make_handler(build_html, scheme: str):
    class KanbanHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/open":
                from urllib.parse import quote, unquote
                qs = parse_qs(parsed.query)
                cmd = shutil.which("codium") or shutil.which("code") or "code"
                if "session" in qs:
                    session = qs["session"][0]
                    if not session or not SESSION_RE.match(session):
                        self._respond(400, b"bad session")
                        return
                    uri = f"{scheme}://anthropic.claude-code/open?session={session}"
                elif "prompt" in qs:
                    prompt = qs["prompt"][0][:500]
                    uri = f"{scheme}://anthropic.claude-code/open?prompt={quote(prompt)}"
                else:
                    self._respond(400, b"missing param")
                    return
                subprocess.Popen(
                    [cmd, "--open-url", uri],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
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


def load_projects(roots: list[Path], uuid_index: dict[str, str]) -> list[Project]:
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
    return projects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate taskflow HTML kanban.")
    parser.add_argument(
        "--out", type=Path, default=Path("/tmp/taskflow-kanban.html"),
        help="Output HTML path (default: /tmp/taskflow-kanban.html)",
    )
    parser.add_argument(
        "--open", action="store_true", help="Open in default browser after generating",
    )
    parser.add_argument(
        "--serve", action="store_true",
        help=f"Serve on http://localhost:{SERVE_PORT}/ with /open?session=UUID endpoint",
    )
    parser.add_argument(
        "--scheme", default="", help="URI scheme override: vscode or vscodium",
    )
    args = parser.parse_args(argv)

    scheme = args.scheme or detect_scheme()

    roots = [r for r in [PRIMARY_ROOT, SECONDARY_ROOT] if r.is_dir()]
    if not roots:
        print("error: no _projects/ directory found", file=sys.stderr)
        return 2

    uuid_index: dict[str, str] = {}
    for root in roots:
        uuid_index.update(build_uuid_index(root / "_state"))
    print(f"[kanban] sessions indexed: {len(uuid_index)}", file=sys.stderr)

    projects = load_projects(roots, uuid_index)
    total = sum(len(p.tasks) for p in projects)

    if args.serve:
        def build_html():
            uuid_idx: dict[str, str] = {}
            for r in roots:
                uuid_idx.update(build_uuid_index(r / "_state"))
            projs = load_projects(roots, uuid_idx)
            return render_html(projs, scheme, serve=True)
        handler = make_handler(build_html, scheme)
        server = HTTPServer(("localhost", SERVE_PORT), handler)
        url = f"http://localhost:{SERVE_PORT}/"
        print(f"[kanban] serving {total} tasks at {url} (Ctrl+C to stop)", file=sys.stderr)
        open_browser(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[kanban] stopped", file=sys.stderr)
        return 0

    html = render_html(projects, scheme, serve=False)
    args.out.write_text(html, encoding="utf-8")
    print(f"[kanban] generated: {args.out}", file=sys.stderr)

    if args.open:
        open_browser(str(args.out))

    print(str(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
