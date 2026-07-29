# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb"]
# ///
"""Run SQL against the inspect-cc-log views over Claude Code session logs.

Usage:
    uv run scripts/query.py --sql "select ... from cc_event where ..."
    echo "select ..." | uv run scripts/query.py

Self-contained: opens an in-memory DuckDB, defines the views from views.sql
(they read the CC session logs lazily, so they are always fresh), runs the query,
and prints {columns, rows, row_count} as JSON. No connection config, no
persistent database. Each query re-reads the logs (a few seconds).

The logs live under `~/.claude/projects` unless `$CLAUDE_CONFIG_DIR` moves the
Claude config dir; `_apply_cc_projects_glob` below points the views at both
universes when it is set.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb

VIEWS_SQL = Path(__file__).resolve().parent / "views.sql"

# --- CLAUDE_CONFIG_DIR support ------------------------------------------------
# Duplicated on purpose: these skill scripts are self-contained (`uv run` with
# PEP 723 deps, no shared package). The same logic lives in
# `plugins/llm-wiki/llmwiki/ingest/cc_paths.py` for the vendored copy of
# views.sql, and in `skills/revert/scripts/revert_cc_log_extract.py`. Design:
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


def _env_config_dir() -> "str | None":
    raw = os.environ.get(_CONFIG_DIR_ENV, "").strip()
    return os.path.abspath(raw) if raw else None


def _escape_glob_dir(dir_path: str) -> str:
    """Escape glob metacharacters in a literal directory path for DuckDB.

    DuckDB treats the whole injected string as a glob, while `_has_logs` treats
    the root literally — a `[`/`]` in the config-dir path (legal on Windows)
    would pass the filter yet match nothing and abort CREATE VIEW. A character
    class (`[` -> `[[]`) makes DuckDB match literally (probed 2026-07-29).
    """
    return "".join("[" + ch + "]" if ch in "[]*?" else ch for ch in dir_path)


def _cc_projects_universes() -> "list[tuple[Path, str]]":
    """`[(projects_dir, duckdb_glob), ...]`, env universe first, deduped."""
    universes: "list[tuple[Path, str]]" = []
    env = _env_config_dir()
    if env:
        root = Path(env) / "projects"
        universes.append((root, f"{_escape_glob_dir(root.as_posix())}/**/*.jsonl"))
    default = Path(os.path.expanduser("~/.claude")) / "projects"
    if not any(
        os.path.normcase(str(root)) == os.path.normcase(str(default))
        for root, _glob in universes
    ):
        universes.append((default, _DEFAULT_PROJECTS_GLOB))
    return universes


def _has_logs(projects_root: Path) -> bool:
    try:
        return next(projects_root.glob("**/*.jsonl"), None) is not None
    except OSError:
        return False


def _apply_cc_projects_glob(sql: str) -> str:
    """Rewrite views.sql's anchor glob to cover every universe that has logs.

    Returns the input unchanged when `$CLAUDE_CONFIG_DIR` is unset. A glob that
    matches no file aborts CREATE VIEW in DuckDB (an existing-but-empty dir
    counts as no match), so empty universes are filtered out first; if none has
    logs, the configured dir alone is injected so the error names it.
    """
    if _env_config_dir() is None:
        return sql
    universes = _cc_projects_universes()
    globs = [glob for root, glob in universes if _has_logs(root)]
    if not globs:
        globs = [universes[0][1]]
    listed = ", ".join("'" + glob.replace("'", "''") + "'" for glob in globs)
    return sql.replace(_GLOB_ANCHOR, f"[{listed}]")


def main() -> None:
    ap = argparse.ArgumentParser(prog="query.py")
    ap.add_argument("--sql", default=None, help="SQL to run (else read from stdin)")
    ap.add_argument("--max-rows", type=int, default=200, dest="max_rows")
    ap.add_argument("--max-bytes", type=int, default=51200, dest="max_bytes")
    args = ap.parse_args()

    sql = args.sql if args.sql is not None else sys.stdin.read()
    if not sql.strip():
        print("Error: no SQL provided.", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect()
    con.execute(_apply_cc_projects_glob(VIEWS_SQL.read_text(encoding="utf-8")))

    try:
        cur = con.execute(sql)
    except Exception as e:  # noqa: BLE001
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if cur.description is None:
        print(json.dumps({"rowcount": cur.rowcount}))
        return

    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    if len(rows) > args.max_rows:
        print(
            f"Error: query returned {len(rows)} rows, exceeding the limit of "
            f"{args.max_rows}. Add a WHERE/LIMIT clause or use COUNT(*) first.",
            file=sys.stderr,
        )
        sys.exit(1)

    out = {"columns": columns, "rows": [list(r) for r in rows], "row_count": len(rows)}
    payload = json.dumps(out, ensure_ascii=False, default=str)
    if len(payload.encode("utf-8")) > args.max_bytes:
        print(
            f"Error: output {len(payload.encode('utf-8')) // 1024}KB exceeds the "
            f"limit of {args.max_bytes // 1024}KB. Narrow the SELECT or add LIMIT.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(payload)


if __name__ == "__main__":
    main()
