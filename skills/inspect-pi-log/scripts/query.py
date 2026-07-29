# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb"]
# ///
"""Run SQL against the inspect-pi-log views over Pi Coding Agent session logs.

Usage:
    uv run scripts/query.py --sql "select ... from pi_event where ..."
    echo "select ..." | uv run scripts/query.py

Self-contained: opens an in-memory DuckDB, defines the views from views.sql
(they read the Pi session logs lazily, so they are always fresh), runs the query,
and prints {columns, rows, row_count} as JSON. No connection config, no
persistent database. Each query re-reads the logs (a few seconds).

The logs live under `~/.pi/agent/sessions` unless `$PI_CODING_AGENT_DIR` or
`$PI_CODING_AGENT_SESSION_DIR` moves them; `_apply_pi_sessions_glob` below points
the views at every universe that is set.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb

VIEWS_SQL = Path(__file__).resolve().parent / "views.sql"

# --- PI_CODING_AGENT_DIR / PI_CODING_AGENT_SESSION_DIR support ----------------
# Duplicated on purpose: these skill scripts are self-contained (`uv run` with
# PEP 723 deps, no shared package). The sibling implementation for Claude Code's
# `CLAUDE_CONFIG_DIR` lives in `skills/inspect-cc-log/scripts/query.py`. Design:
# `pi/_projects/pi-extensions-dev/project-notes/specs/agent-dir-env-support-design.md`.
#
# Semantics replicate pi's own handling (verified against
# `pi/packages/coding-agent/src/config.ts` + `utils/paths.ts`, 2026-07-30):
# the value IS tilde-expanded -- the exact opposite of the CC rule -- and a
# relative value resolves against the process cwd. Readers scan the union of all
# universes, env first, and the home default is ALWAYS included: this skill also
# runs from harnesses (Claude Code, …) that never inherited pi's env vars, and
# there the home store is the only one that exists.
_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
_SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"
_DEFAULT_SESSIONS_GLOB = "~/.pi/agent/sessions/**/*.jsonl"
_GLOB_ANCHOR = f"'{_DEFAULT_SESSIONS_GLOB}'"


def _expand_tilde(value: str) -> str:
    """Expand `~` the way pi's `expandTildePath` does, and no further.

    `normalizePath` (utils/paths.ts) expands a bare `~`, a `~/`-prefixed path
    and -- on Windows only -- a `~\\`-prefixed one. It deliberately leaves the
    `~user` form alone, so `os.path.expanduser` is too eager to use here.
    """
    home = os.path.expanduser("~")
    if value == "~":
        return home
    if value.startswith("~/") or (os.name == "nt" and value.startswith("~\\")):
        return os.path.join(home, value[2:])
    return value


def _env_dir(name: str) -> "str | None":
    """A pi dir env var as an absolute path; None when unset or blank."""
    raw = os.environ.get(name, "").strip()
    return os.path.abspath(_expand_tilde(raw)) if raw else None


def _escape_glob_dir(dir_path: str) -> str:
    """Escape glob metacharacters in a literal directory path for DuckDB.

    DuckDB treats the whole injected string as a glob, while `_has_logs` treats
    the root literally — a `[`/`]` in the session-dir path (legal on Windows)
    would pass the filter yet match nothing and abort CREATE VIEW. A character
    class (`[` -> `[[]`) makes DuckDB match literally.
    """
    return "".join("[" + ch + "]" if ch in "[]*?" else ch for ch in dir_path)


def _norm_parts(root: Path) -> "tuple[str, ...]":
    """A root as normcased path components — the unit of root comparison.

    Comparing components rather than the joined string keeps `<x>/sessionsX`
    from reading as nested under `<x>/sessions`.
    """
    return tuple(os.path.normcase(part) for part in root.parts)


def _covers(outer: "tuple[str, ...]", inner: "tuple[str, ...]") -> bool:
    """True when an `<outer>/**` glob already matches everything under `inner`.

    Equality counts — a root covers itself.
    """
    return inner[: len(outer)] == outer


def _pi_session_universes() -> "list[tuple[Path, str]]":
    """`[(sessions_root, duckdb_glob), ...]`, env universes first, deduped.

    The priority mirrors pi's own session-dir resolution (main.ts): the flat
    `$PI_CODING_AGENT_SESSION_DIR` store wins over `$PI_CODING_AGENT_DIR`, which
    wins over the home default.

    Dedupe is not cosmetic — DuckDB reads a glob list once per entry, so a root
    read twice DOUBLES every row of it. Equality alone does not catch that:
    every root is globbed as `<root>/**/*.jsonl`, so a root NESTED under
    another is read twice as well — e.g. `$PI_CODING_AGENT_SESSION_DIR` aimed
    at one of the per-cwd subdirectories under `$PI_CODING_AGENT_DIR/sessions`.
    Only maximal roots are kept. Dropping a covered root loses no file, because
    the covering root's glob already matches every file the covered one had.
    """
    universes: "list[tuple[Path, str]]" = []

    def add(root: Path, glob: str) -> None:
        parts = _norm_parts(root)
        if any(_covers(_norm_parts(kept), parts) for kept, _g in universes):
            return
        universes[:] = [(kept, kept_glob) for kept, kept_glob in universes
                        if not _covers(parts, _norm_parts(kept))]
        universes.append((root, glob))

    def add_env(root: Path) -> None:
        add(root, f"{_escape_glob_dir(root.as_posix())}/**/*.jsonl")

    flat = _env_dir(_SESSION_DIR_ENV)
    if flat:
        add_env(Path(flat))  # a flat store: no per-cwd subdirectory
    agent = _env_dir(_AGENT_DIR_ENV)
    if agent:
        add_env(Path(agent) / "sessions")
    add(Path(os.path.expanduser("~/.pi/agent/sessions")), _DEFAULT_SESSIONS_GLOB)
    return universes


def _has_logs(sessions_root: Path) -> bool:
    try:
        return next(sessions_root.glob("**/*.jsonl"), None) is not None
    except OSError:
        return False


def _apply_pi_sessions_glob(sql: str) -> str:
    """Rewrite views.sql's anchor glob to cover every universe that has logs.

    Returns the input unchanged when neither env var is set, so views.sql stays
    usable on its own. A glob that matches no file aborts CREATE VIEW in DuckDB
    (an existing-but-empty dir counts as no match), so empty universes are
    filtered out first; if none has logs, the highest-priority root alone is
    injected so the error names it.
    """
    if _env_dir(_SESSION_DIR_ENV) is None and _env_dir(_AGENT_DIR_ENV) is None:
        return sql
    universes = _pi_session_universes()
    globs = [glob for root, glob in universes if _has_logs(root)]
    if not globs:
        globs = [universes[0][1]]
    listed = ", ".join("'" + glob.replace("'", "''") + "'" for glob in globs)
    return sql.replace(_GLOB_ANCHOR, f"[{listed}]")

# Logs contain non-ASCII (JP text, em-dashes, …). Force UTF-8 stdout/stderr so
# output never crashes on a legacy console codepage (e.g. Windows cp932).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


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
    con.execute(_apply_pi_sessions_glob(VIEWS_SQL.read_text(encoding="utf-8")))

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
