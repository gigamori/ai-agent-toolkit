# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb"]
# ///
"""Run SQL against the inspect-cc-log views over Claude Code session logs.

Usage:
    uv run scripts/query.py --sql "select ... from cc_event where ..."
    echo "select ..." | uv run scripts/query.py

Self-contained: opens an in-memory DuckDB, defines the views from views.sql
(they read ~/.claude/projects/**/*.jsonl lazily, so they are always fresh), runs
the query, and prints {columns, rows, row_count} as JSON. No connection config,
no persistent database. Each query re-reads the logs (a few seconds).
"""

import argparse
import json
import sys
from pathlib import Path

import duckdb

VIEWS_SQL = Path(__file__).resolve().parent / "views.sql"


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
    con.execute(VIEWS_SQL.read_text(encoding="utf-8"))

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
