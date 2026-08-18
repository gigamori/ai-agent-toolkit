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


# --- CLAUDE_CONFIG_DIR support ------------------------------------------------
# Duplicated on purpose: these skill scripts are self-contained (`uv run` with
# PEP 723 deps, no shared package). The same logic lives in
# `skills/inspect-cc-log/scripts/query.py` and in
# `plugins/llm-wiki/llmwiki/ingest/cc_paths.py`. Design:
# `_projects/llm-wiki/project-notes/specs/cc-config-dir-skills.md`.
#
# Semantics replicate Claude Code's own handling of the value (verified on
# Windows, 2026-07-28): it is LITERAL — no `~`-expansion, no env-var expansion —
# and a relative value resolves against the process cwd. Only the built-in
# default `~/.claude` is expanduser'd.
_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"


def _norm_parts(root: Path) -> tuple[str, ...]:
    """A root as normcased path components — the unit of root comparison.

    Comparing components rather than the joined string keeps `<x>/projectsX`
    from reading as nested under `<x>/projects`.
    """
    return tuple(os.path.normcase(part) for part in root.parts)


def _covers(outer: tuple[str, ...], inner: tuple[str, ...]) -> bool:
    """True when an `<outer>` rglob already matches everything under `inner`.

    Equality counts — a root covers itself.
    """
    return inner[: len(outer)] == outer


def cc_projects_roots() -> list[Path]:
    """The CC `projects` dirs to search: `[$CLAUDE_CONFIG_DIR, ~/.claude]`, env first.

    Deduped by containment (normcased path components), not just equality: each
    root is rglob'd recursively, so a root NESTED under the other is scanned
    twice — e.g. `$CLAUDE_CONFIG_DIR` pointed inside `~/.claude/projects`. Only
    maximal roots are kept. Used only when --projects-dir is NOT given.
    """
    roots: list[Path] = []

    def add(root: Path) -> None:
        parts = _norm_parts(root)
        if any(_covers(_norm_parts(kept), parts) for kept in roots):
            return
        roots[:] = [kept for kept in roots if not _covers(parts, _norm_parts(kept))]
        roots.append(root)

    env = os.environ.get(_CONFIG_DIR_ENV, "").strip()
    if env:
        add(Path(os.path.abspath(env)) / "projects")
    add(Path(os.path.expanduser("~/.claude")) / "projects")
    return roots


def resolve_session_jsonl(session_id_arg: str | None, projects_dir: str | None) -> Path | None:
    """Locate the target session jsonl.

    Precedence: --session-id arg → $CLAUDE_CODE_SESSION_ID env → mtime-latest
    *.jsonl under the search roots.

    $CLAUDE_CODE_SESSION_ID is the env var Claude Code actually sets for child
    processes (probed 2026-07-29 in a live session: it is SET, len 36, while the
    previously-read $CLAUDE_SESSION_ID is UNSET — that name is a harness
    prompt-template substitution, not an OS env var; same D5 diagnosis as
    llm-wiki's ingest_driver). With the old name the env branch never fired and
    every sid-less run silently fell through to mtime-latest, which can pick a
    concurrent session's log.

    Search roots: an explicit `projects_dir` (--projects-dir) is used VERBATIM and
    alone — behaviour unchanged from before CLAUDE_CONFIG_DIR support. When it is
    None the roots are `cc_projects_roots()`: a sid is looked up in each root in
    order and the FIRST hit wins (env universe priority), while the mtime-latest
    fallback takes the newest file ACROSS all roots.
    """
    sid = session_id_arg or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if projects_dir is not None:
        roots = [Path(os.path.expanduser(projects_dir))]
    else:
        roots = cc_projects_roots()
    roots = [r for r in roots if r.is_dir()]
    if not roots:
        return None
    if sid:
        for root in roots:
            matches = list(root.rglob(f"{sid}.jsonl"))
            if matches:
                return matches[0]
        return None
    candidates = [p for root in roots for p in root.rglob("*.jsonl")]
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


def _extract_user_text(content_value, *, full: bool = False) -> str:
    """Extract user message text for turn marker display.

    Args:
        full: If True, return the complete text without truncation.
              Used by --until-message matching to avoid false negatives.
    """
    limit = None if full else 50
    if isinstance(content_value, str):
        try:
            content = json.loads(content_value)
        except json.JSONDecodeError:
            return content_value if full else content_value[:50]
    elif isinstance(content_value, list):
        content = content_value
    else:
        raw = str(content_value)
        return raw if full else raw[:50]
    if isinstance(content, str):
        return content if full else content[:50]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                return text if full else text[:50]
    return ""


# A turn boundary entry in the actions list.
# Represented as a tuple ("__turn__", ts, user_text_preview).
_TURN_MARKER = "__turn__"

# Patterns that identify system-injected user rows (not genuine user input).
# Skill load and harness metadata create separate user rows in session JSONL;
# these must not generate turn boundaries.
_SYSTEM_INJECT_MARKERS = (
    "Base directory for this skill:",
    "<command-name>",
)


def _is_system_injected_user_msg(content_value) -> bool:
    """Detect user rows containing only system-injected content.

    Skill load messages (e.g. 'Base directory for this skill: ...') create
    separate user rows in session JSONL. If treated as turn boundaries they
    push the real latest-turn actions into 'previous turn', breaking the
    judge's turn-scope constraint (Step 2.5).
    """
    text = _extract_user_text(content_value)
    if not text.strip():
        return True
    return any(marker in text for marker in _SYSTEM_INJECT_MARKERS)


def collect_tool_actions(
    rows: list[tuple], n: int, *, until_message: str | None = None
) -> list[tuple]:
    """Iterate rows (newest first), expand tool_use blocks, insert turn markers.

    Returns list of:
      - tool action: (ts_local, action_summary)
      - turn marker: ("__turn__", ts_local, user_text_preview)

    Turn markers are inserted when a user row is encountered. They do NOT count
    toward the N-action limit.

    If until_message is set, the N-action limit is ignored and collection stops
    when a user message containing the substring is found (that message becomes
    the boundary marker but actions below it are excluded).
    """
    out: list[tuple] = []
    action_count = 0
    for msg_type, ts_local, content_value in rows:
        if msg_type == "user":
            if _is_system_injected_user_msg(content_value):
                continue
            user_full = _extract_user_text(content_value, full=True)
            user_display = user_full[:50]
            # Check until_message boundary BEFORE appending
            if until_message and until_message in user_full:
                # Emit boundary marker and stop
                out.append((_TURN_MARKER + "_boundary", ts_local, user_display))
                return out
            out.append((_TURN_MARKER, ts_local, user_display))
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
            if not until_message and action_count >= n:
                return out
    return out


def _git(args: list[str]) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            # git emits UTF-8, but text mode would decode with the platform
            # default (cp932 on JA Windows). Branch names may legally be
            # non-ASCII, and a decode failure here raises past the except
            # below. replace keeps the failure local: a mangled name simply
            # will not match downstream.
            encoding="utf-8",
            errors="replace",
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


_TURN_BOUNDARY = _TURN_MARKER + "_boundary"


def render_output(
    actions: list[tuple], repo: dict, *, until_message: str | None = None
) -> str:
    lines: list[str] = []
    action_idx = 0
    has_actions = any(
        entry[0] not in (_TURN_MARKER, _TURN_BOUNDARY) for entry in actions
    )
    if has_actions:
        header = "RECENT_LLM_ACTIONS (newest first"
        if until_message:
            header += f", scoped to message: {until_message!r}"
        header += "):"
        lines.append(header)
        turn_label = "latest"
        for entry in actions:
            if entry[0] == _TURN_BOUNDARY:
                _, ts, user_text = entry
                preview = _flatten(user_text)
                lines.append(
                    f"  --- Turn (target boundary, after user message at {ts}: {preview!r}) ---"
                )
                lines.append("  (actions before this point excluded)")
            elif entry[0] == _TURN_MARKER:
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
        help="Target session id (default: $CLAUDE_CODE_SESSION_ID or mtime-latest jsonl under --projects-dir).",
    )
    ap.add_argument(
        "--projects-dir",
        default=None,
        help="jsonl search root, used verbatim and alone. Default (unset): search "
        "$CLAUDE_CONFIG_DIR/projects then ~/.claude/projects.",
    )
    ap.add_argument(
        "--until-message",
        default=None,
        help="Collect actions back to the user message containing this substring. "
        "Ignores --n limit when set. The matched message becomes the boundary.",
    )
    args = ap.parse_args()

    jsonl = resolve_session_jsonl(args.session_id, args.projects_dir)
    if jsonl is None:
        sid_disp = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID") or "(mtime-latest)"
        print(f"session not found: {sid_disp}", file=sys.stderr)
        sys.exit(2)

    try:
        rows = fetch_rows(jsonl)
    except (duckdb.Error, OSError) as e:
        print(f"jsonl read failure: {e}", file=sys.stderr)
        sys.exit(3)

    actions = collect_tool_actions(rows, args.n, until_message=args.until_message)
    repo = get_repo_context()
    print(render_output(actions, repo, until_message=args.until_message))

    has_actions = any(
        entry[0] not in (_TURN_MARKER, _TURN_BOUNDARY) for entry in actions
    )
    if not has_actions:
        sys.exit(1)


if __name__ == "__main__":
    main()
