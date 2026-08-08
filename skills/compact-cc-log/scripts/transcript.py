# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb"]
# ///
"""Resolve a Claude Code session and write its transcript to a file.

Usage:
    uv run scripts/transcript.py --current --out <path>
    uv run scripts/transcript.py --session-id <uuid> --out <path>
    uv run scripts/transcript.py --title <text> --out <path>

Resolves exactly one CC session, merges its user/assistant text blocks and
tool-call summary lines into a single timestamp-ordered Markdown transcript,
and writes it to --out (UTF-8). Prints exactly one status JSON line to
stdout; never the transcript content itself (CC logs hold non-ASCII text and
piping it through the shell is lossy on some locales — see
inspect-cc-log/SKILL.md's UTF-8 gotcha).

Status JSON shapes:
    {"status": "ok", "session_id", "path", "n_rows", "n_msg", "n_tool", "cut_boundary_ts"}
    {"status": "not_found"}
    {"status": "candidates", "sessions": [{"session_id", "title"}, ...]}
    {"status": "empty", "session_id"}
    {"status": "error", "message"}

Exit codes: 0 for ok/candidates, 1 for not_found/empty/error.

--current cut rule: everything at or after the timestamp of the session's
LAST user-type record is excluded. A slash-invocation (e.g. this very
`/compact-cc-log --current` call) writes 2-3 user-type records that share
that same max timestamp (an invocation record, and for skills, an isMeta
expansion) — verified empirically 2026-08-08 by inspecting cc_event rows
around a live /model invocation: caveat + command-name + command-stdout all
land on one identical timestamp, distinct from the next real turn. Cutting
by `ts >= max(user_ts)` removes that whole group (plus any of the current
turn's own tool calls already logged), without needing cc_turn's lead()
turn-boundary machinery, which is ambiguous under same-timestamp ties.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb

# --- CLAUDE_CONFIG_DIR support ------------------------------------------------
# Duplicated on purpose: these skill scripts are self-contained (`uv run` with
# PEP 723 deps, no shared package). The same logic lives in
# `skills/inspect-cc-log/scripts/query.py`, `skills/revert/scripts/revert_cc_log_extract.py`,
# and `plugins/llm-wiki/llmwiki/ingest/cc_paths.py`. Design:
# `_projects/llm-wiki/project-notes/specs/cc-config-dir-skills.md`.
#
# Semantics replicate Claude Code's own handling of the value (verified on
# Windows, 2026-07-28): it is LITERAL — no `~`-expansion, no env-var expansion —
# and a relative value resolves against the process cwd. Only the built-in
# default `~/.claude` is expanduser'd. Readers scan the union of both universes,
# env first.
_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
_DEFAULT_PROJECTS_GLOB = "~/.claude/projects/**/*.jsonl"
_GLOB_ANCHOR = f"'{_DEFAULT_PROJECTS_GLOB}'"

VIEWS_SQL = Path(__file__).resolve().parent / "views.sql"


def _env_config_dir() -> "str | None":
    raw = os.environ.get(_CONFIG_DIR_ENV, "").strip()
    return os.path.abspath(raw) if raw else None


def _escape_glob_dir(dir_path: str) -> str:
    """Escape glob metacharacters in a literal directory path for DuckDB."""
    return "".join("[" + ch + "]" if ch in "[]*?" else ch for ch in dir_path)


def _norm_parts(root: Path) -> "tuple[str, ...]":
    return tuple(os.path.normcase(part) for part in root.parts)


def _covers(outer: "tuple[str, ...]", inner: "tuple[str, ...]") -> bool:
    return inner[: len(outer)] == outer


def _cc_projects_universes() -> "list[tuple[Path, str]]":
    universes: "list[tuple[Path, str]]" = []

    def add(root: Path, glob: str) -> None:
        parts = _norm_parts(root)
        if any(_covers(_norm_parts(kept), parts) for kept, _g in universes):
            return
        universes[:] = [(kept, kept_glob) for kept, kept_glob in universes
                        if not _covers(parts, _norm_parts(kept))]
        universes.append((root, glob))

    env = _env_config_dir()
    if env:
        root = Path(env) / "projects"
        add(root, f"{_escape_glob_dir(root.as_posix())}/**/*.jsonl")
    default = Path(os.path.expanduser("~/.claude")) / "projects"
    add(default, _DEFAULT_PROJECTS_GLOB)
    return universes


def _has_logs(projects_root: Path) -> bool:
    try:
        return next(projects_root.glob("**/*.jsonl"), None) is not None
    except OSError:
        return False


def _apply_cc_projects_glob(sql: str) -> str:
    if _env_config_dir() is None:
        return sql
    universes = _cc_projects_universes()
    globs = [glob for root, glob in universes if _has_logs(root)]
    if not globs:
        globs = [universes[0][1]]
    listed = ", ".join("'" + glob.replace("'", "''") + "'" for glob in globs)
    return sql.replace(_GLOB_ANCHOR, f"[{listed}]")


# --- session resolution --------------------------------------------------------

def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(_apply_cc_projects_glob(VIEWS_SQL.read_text(encoding="utf-8")))
    return con


def _resolve_by_title(con: duckdb.DuckDBPyConnection, title: str) -> "list[dict]":
    rows = con.execute(
        "SELECT session_id, title FROM cc_session WHERE title = ?", [title]
    ).fetchall()
    return [{"session_id": r[0], "title": r[1]} for r in rows]


def _current_session_id() -> "str | None":
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    return sid or None


def _cut_boundary_ts(con: duckdb.DuckDBPyConnection, session_id: str):
    """Timestamp of the session's last real user-authored text turn.

    Must query cc_block (role='user' AND block_type='text'), NOT cc_event
    (type='user' AND role='user'): a tool_result is ALSO logged as a
    type='user'/role='user' record (verified against this skill's own live
    session 2026-08-08 -- during an execute-mode turn with many tool calls,
    cc_event's max(ts) tracked the latest tool_result, landing ~8 minutes
    after the true last human prompt and leaking the whole in-progress turn
    into the transcript). block_type='text' isolates actual authored text
    from tool_result/tool_use blocks.
    """
    row = con.execute(
        "SELECT max(ts) FROM cc_block WHERE session_id = ? AND role = 'user' AND block_type = 'text'",
        [session_id],
    ).fetchone()
    return row[0] if row else None


# --- transcript assembly --------------------------------------------------------

def _build_transcript(con: duckdb.DuckDBPyConnection, session_id: str, boundary_ts):
    msg_sql = (
        "SELECT ts, role, text FROM cc_block "
        "WHERE session_id = ? AND block_type = 'text' AND role IN ('user','assistant') "
        "AND text IS NOT NULL"
    )
    tool_sql = (
        "SELECT ts_call AS ts, tool_name, "
        "coalesce(file_path, tool_input->>'command') AS detail "
        "FROM cc_tool WHERE session_id = ?"
    )
    params = [session_id]
    if boundary_ts is not None:
        msg_sql += " AND ts < ?"
        tool_sql += " AND ts_call < ?"
        params = [session_id, boundary_ts]

    msgs = con.execute(msg_sql, params).fetchall()
    tools = con.execute(tool_sql, params).fetchall()

    lines = []
    for ts, role, text in msgs:
        lines.append((ts, f"[{ts}] {role.upper()}: {text}"))
    for ts, tool_name, detail in tools:
        detail_str = "" if detail is None else f": {detail}"
        lines.append((ts, f"[{ts}] TOOL {tool_name}{detail_str}"))
    lines.sort(key=lambda pair: pair[0])

    body = "\n\n".join(line for _ts, line in lines)
    return body, len(msgs), len(tools)


def _run_with_retry(fn):
    """Run fn() once; retry once on failure (a mid-write JSONL tail line)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return fn()


def main() -> None:
    ap = argparse.ArgumentParser(prog="transcript.py")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--current", action="store_true")
    group.add_argument("--session-id", dest="session_id")
    group.add_argument("--title", dest="title")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    def resolve_and_build():
        con = _connect()

        if args.current:
            sid = _current_session_id()
            if sid is None:
                return {"status": "error", "message": "current session id unavailable"}
        elif args.session_id:
            sid = args.session_id
        else:
            matches = _resolve_by_title(con, args.title)
            if not matches:
                return {"status": "not_found"}
            if len(matches) > 1:
                return {"status": "candidates", "sessions": matches}
            sid = matches[0]["session_id"]

        boundary_ts = _cut_boundary_ts(con, sid) if args.current else None
        body, n_msg, n_tool = _build_transcript(con, sid, boundary_ts)

        if n_msg + n_tool == 0:
            return {"status": "empty", "session_id": sid}

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")

        return {
            "status": "ok",
            "session_id": sid,
            "path": str(out_path),
            "n_rows": n_msg + n_tool,
            "n_msg": n_msg,
            "n_tool": n_tool,
            "cut_boundary_ts": str(boundary_ts) if boundary_ts is not None else None,
        }

    try:
        result = _run_with_retry(resolve_and_build)
    except Exception as e:  # noqa: BLE001
        result = {"status": "error", "message": str(e)}

    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["status"] in ("ok", "candidates") else 1)


if __name__ == "__main__":
    main()
