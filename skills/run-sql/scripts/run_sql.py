# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "sqlalchemy",
#   "psycopg2-binary",       # PostgreSQL
#   "PyMySQL",               # MySQL
#   "mariadb",               # MariaDB ※ libmariadb システム依存
#   "sqlalchemy-redshift",   # Redshift（dialect）
#   "redshift-connector",    # Redshift（driver）
#   "snowflake-sqlalchemy",  # Snowflake
#   "sqlalchemy-bigquery",   # BigQuery
#   "duckdb-engine",         # DuckDB
# ]
# ///

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path


def load_config(config_path: Path | None = None) -> dict:
    if config_path is None:
        env_path = os.environ.get("RUN_SQL_CONFIG")
        if env_path:
            config_path = Path(env_path)
        else:
            config_path = Path.home() / ".config" / "run-sql" / "connections.toml"

    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path, "rb") as f:
        return tomllib.load(f)


def resolve_connection(config: dict, name: str) -> str:
    connections = config.get("connections", {})
    if name not in connections:
        print(f'Error: Connection "{name}" not found in config.', file=sys.stderr)
        sys.exit(1)
    return connections[name]["dsn"]


def cmd_list(args: argparse.Namespace) -> None:
    config = load_config()
    connections = config.get("connections", {})
    for name, entry in connections.items():
        description = entry.get("description", "")
        print(f"{name:<14}: {description}")


def cmd_query(args: argparse.Namespace) -> None:
    from sqlalchemy import create_engine, text

    config = load_config()
    dsn = resolve_connection(config, args.connection)

    if args.sql:
        sql = args.sql
    else:
        sql = sys.stdin.read()

    try:
        engine = create_engine(dsn)
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            columns = list(result.keys())
            rows = result.fetchall()
    except Exception as e:
        dialect = dsn.split(":")[0] if ":" in dsn else dsn
        print(f"Error: Failed to connect [{dialect}]: {e}", file=sys.stderr)
        sys.exit(1)

    row_count = len(rows)
    if row_count > args.max_rows:
        print(
            f"Error: Query returned {row_count} rows, exceeding the limit of {args.max_rows}."
            " Add a WHERE clause or use COUNT(*) first.",
            file=sys.stderr,
        )
        sys.exit(1)

    output = {
        "columns": columns,
        "rows": [list(row) for row in rows],
        "row_count": row_count,
    }

    output_json = json.dumps(output, ensure_ascii=False, default=str)
    output_bytes = len(output_json.encode("utf-8"))

    if output_bytes > args.max_bytes:
        output_kb = output_bytes // 1024
        limit_kb = args.max_bytes // 1024
        print(
            f"Error: Output size {output_kb}KB exceeds the limit of {limit_kb}KB.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(output_json)


def main() -> None:
    parser = argparse.ArgumentParser(prog="run_sql.py")
    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser("list")

    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("--connection", required=True)
    query_parser.add_argument("--sql", default=None)
    query_parser.add_argument("--max-rows", type=int, default=200, dest="max_rows")
    query_parser.add_argument("--max-bytes", type=int, default=51200, dest="max_bytes")

    args = parser.parse_args()

    if args.subcommand == "list":
        cmd_list(args)
    elif args.subcommand == "query":
        cmd_query(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
