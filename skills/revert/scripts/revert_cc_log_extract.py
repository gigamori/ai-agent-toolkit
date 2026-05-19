# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""Extract recent LLM tool actions from a CC session jsonl as a serialize protocol block.

Output (stdout):
    RECENT_LLM_ACTIONS (newest first):
      1. [2026-05-01T19:00:05] Bash: git commit -m "wip"
      ...

    REPO_CONTEXT:
      type: git
      cwd: /path/to/repo
      current_branch: feat/foo

USER_REQUEST is prepended by the parent skill (revert) and is not emitted here.

Exit codes:
    0: ok (>=1 action extracted)
    1: 0 actions extracted (RECENT_LLM_ACTIONS (none): + REPO_CONTEXT still printed)
    2: session jsonl not resolved
    3: jsonl read / DuckDB failure
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb


def resolve_session_jsonl(session_id_arg: str | None, projects_dir: str) -> Path | None:
    """Locate the target session jsonl.

    Precedence: --session-id arg → $CLAUDE_SESSION_ID env → mtime-latest *.jsonl under projects_dir.
    """
    sid = session_id_arg or os.environ.get("CLAUDE_SESSION_ID")
    root = Path(os.path.expanduser(projects_dir))
    if not root.is_dir():
        return None
    if sid:
        matches = list(root.rglob(f"{sid}.jsonl"))
        return matches[0] if matches else None
    candidates = list(root.rglob("*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


_SQL = """
SELECT
  json_extract_string(line::JSON, '$.type') AS msg_type,
  strftime(
    CAST(json_extract_string(line::JSON, '$.timestamp') AS TIMESTAMP)
      AT TIME ZONE 'UTC' AT TIME ZONE current_setting('timezone'),
    '%Y-%m-%dT%H:%M:%S'
  ) AS ts_local,
  json_extract(line::JSON, '$.message.content') AS content
FROM read_csv('{path}', columns={{'line': 'VARCHAR'}}, delim=chr(0))
WHERE json_extract_string(line::JSON, '$.type') IN ('assistant', 'user')
ORDER BY CAST(json_extract_string(line::JSON, '$.timestamp') AS TIMESTAMP) DESC
"""


def fetch_rows(jsonl_path: Path) -> list[tuple]:
    """Read assistant + user rows from jsonl via DuckDB, ordered by ts DESC.

    Returns list of (msg_type, ts_local_str, content_json_value).
    """
    safe = str(jsonl_path).replace("\\", "/").replace("'", "''")
    con = duckdb.connect()
    return con.execute(_SQL.format(path=safe)).fetchall()


def _flatten(s: str) -> str:
    """Replace embedded newlines/tabs with literal escapes to keep one-action-per-line.

    No truncation or omission — only whitespace control chars are escaped so that
    multi-line tool inputs (heredoc Bash, multi-line Edit args) do not break the
    line-based serialize protocol format.
    """
    return (
        s.replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def render_action(tool_name: str, inp: dict) -> str:
    """Map a tool_use block to its display form.

    See cc_log_extract.spec.md "action 識別ルール" for the table.
    Unknown tool: name only (no derived field).
    """

    def field(key: str) -> str:
        v = inp.get(key, "")
        return _flatten(v if isinstance(v, str) else str(v))

    if tool_name == "Bash":
        return f"Bash: {field('command')}"
    if tool_name == "Write":
        return f"Write: {field('file_path')}"
    if tool_name == "Edit":
        return f"Edit: {field('file_path')}"
    if tool_name == "Read":
        return f"Read: {field('file_path')}"
    if tool_name == "Glob":
        return f"Glob: {field('pattern')}"
    if tool_name == "Grep":
        return f"Grep: {field('pattern')}"
    if tool_name == "Agent":
        sub = inp.get("subagent_type") or inp.get("description") or ""
        return f"Agent: {_flatten(sub if isinstance(sub, str) else str(sub))}"
    if tool_name == "NotebookEdit":
        return f"NotebookEdit: {field('notebook_path')}"
    return tool_name


def _extract_user_text(content_value) -> str:
    """Extract first 50 chars of user message text for turn marker display."""
    if isinstance(content_value, str):
        try:
            content = json.loads(content_value)
        except json.JSONDecodeError:
            return content_value[:50]
    elif isinstance(content_value, list):
        content = content_value
    else:
        return str(content_value)[:50]
    if isinstance(content, str):
        return content[:50]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return (block.get("text") or "")[:50]
    return ""


# A turn boundary entry in the actions list.
# Represented as a tuple ("__turn__", ts, user_text_preview).
_TURN_MARKER = "__turn__"


def collect_tool_actions(rows: list[tuple], n: int) -> list[tuple]:
    """Iterate rows (newest first), expand tool_use blocks, insert turn markers.

    Returns list of:
      - tool action: (ts_local, action_summary)
      - turn marker: ("__turn__", ts_local, user_text_preview)

    Turn markers are inserted when a user row is encountered. They do NOT count
    toward the N-action limit.
    """
    out: list[tuple] = []
    action_count = 0
    for msg_type, ts_local, content_value in rows:
        if msg_type == "user":
            user_text = _extract_user_text(content_value)
            out.append((_TURN_MARKER, ts_local, user_text))
            continue
        # assistant row
        if isinstance(content_value, str):
            try:
                content = json.loads(content_value)
            except json.JSONDecodeError:
                continue
        elif isinstance(content_value, list):
            content = content_value
        else:
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name") or ""
            inp = block.get("input") or {}
            if not isinstance(inp, dict):
                inp = {}
            out.append((ts_local, render_action(name, inp)))
            action_count += 1
            if action_count >= n:
                return out
    return out


def _git(args: list[str]) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def get_repo_context() -> dict:
    """Probe REPO_CONTEXT.type / cwd / current_branch via git subprocess.

    git failure is non-fatal — type falls back to 'none'.
    """
    cwd = os.getcwd()
    is_git = _git(["rev-parse", "--is-inside-work-tree"]) == "true"
    if not is_git:
        return {"type": "none", "cwd": cwd, "current_branch": None}
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    return {"type": "git", "cwd": cwd, "current_branch": branch}


def render_output(actions: list[tuple], repo: dict) -> str:
    lines: list[str] = []
    action_idx = 0
    has_actions = any(entry[0] != _TURN_MARKER for entry in actions)
    if has_actions:
        lines.append("RECENT_LLM_ACTIONS (newest first):")
        turn_label = "latest"
        for entry in actions:
            if entry[0] == _TURN_MARKER:
                _, ts, user_text = entry
                preview = _flatten(user_text)
                lines.append(f"  --- Turn ({turn_label}, after user message at {ts}: {preview!r}) ---")
                turn_label = "previous"
            else:
                action_idx += 1
                ts, summary = entry
                lines.append(f"  {action_idx}. [{ts}] {summary}")
    else:
        lines.append("RECENT_LLM_ACTIONS (none):")
    lines.append("")
    lines.append("REPO_CONTEXT:")
    lines.append(f"  type: {repo['type']}")
    lines.append(f"  cwd: {repo['cwd']}")
    if repo["type"] == "git":
        branch = repo["current_branch"] or "(detached)"
        lines.append(f"  current_branch: {branch}")
    return "\n".join(lines)


def main() -> None:
    if sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr.encoding.lower() != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    ap = argparse.ArgumentParser(
        description="Extract recent LLM tool actions from a CC session jsonl as a serialize protocol block."
    )
    ap.add_argument(
        "--n",
        type=int,
        default=10,
        help="Number of recent actions (newest first). Default 10.",
    )
    ap.add_argument(
        "--session-id",
        default=None,
        help="Target session id (default: $CLAUDE_SESSION_ID or mtime-latest jsonl under --projects-dir).",
    )
    ap.add_argument(
        "--projects-dir",
        default="~/.claude/projects",
        help="jsonl search root. Default: ~/.claude/projects",
    )
    args = ap.parse_args()

    jsonl = resolve_session_jsonl(args.session_id, args.projects_dir)
    if jsonl is None:
        sid_disp = args.session_id or os.environ.get("CLAUDE_SESSION_ID") or "(mtime-latest)"
        print(f"session not found: {sid_disp}", file=sys.stderr)
        sys.exit(2)

    try:
        rows = fetch_rows(jsonl)
    except (duckdb.Error, OSError) as e:
        print(f"jsonl read failure: {e}", file=sys.stderr)
        sys.exit(3)

    actions = collect_tool_actions(rows, args.n)
    repo = get_repo_context()
    print(render_output(actions, repo))

    has_actions = any(entry[0] != _TURN_MARKER for entry in actions)
    if not has_actions:
        sys.exit(1)


if __name__ == "__main__":
    main()
