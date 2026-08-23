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
#   "databricks-sqlalchemy", # Databricks（dialect。databricks-sql-connector を推移取得）
# ]
# ///

import argparse
import json
import os
import sys
import threading
import tomllib
from pathlib import Path


# 拡張からの制御チャネル（stdin）で SQL 本文と制御コマンドを分離する番兵
SQL_SENTINEL = "\x00--END-SQL--\x00"


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


def _dsn_password(dsn: str):
    """DSN のパスワード成分を返す（無ければ None）。エラーメッセージからの秘匿用。"""
    try:
        from urllib.parse import urlsplit
        return urlsplit(dsn).password or None
    except Exception:
        return None


def _redact_secret(text: str, dsn: str) -> str:
    """例外メッセージ等から DSN 全文とパスワードを除去する。

    SQLAlchemy の create_engine は URL 解析失敗時に DSN 全文（資格情報込み）を
    例外メッセージに載せる。そのまま出力すると秘匿情報が漏れるため除去する。
    """
    out = str(text)
    if dsn and dsn in out:
        out = out.replace(dsn, "<dsn hidden>")
    pw = _dsn_password(dsn)
    if pw:
        out = out.replace(pw, "***")
    return out


# --read-only 時に許可する先頭キーワード（読み取り / introspection 系）
_READONLY_ALLOWED = ("select", "with", "show", "describe", "desc", "explain", "values", "pragma")


def _assert_read_only(sql: str) -> None:
    """--read-only 指定時、先頭キーワードが読み取り系でない文を拒否する（DML/DDL 防止）。

    先頭トークンによる簡易ガードであり完全な SQL パーサではない。build-db-spec のような
    introspection 用途で DML/DDL の誤発行を防ぐことを目的とする。
    """
    import re
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    s = re.sub(r"--[^\n]*", " ", s)
    s = s.strip()
    if not s:
        return
    first = s.split(None, 1)[0].lower().lstrip("(")
    if first not in _READONLY_ALLOWED:
        print(
            "Error: --read-only 指定時は読み取り文"
            "（SELECT/WITH/SHOW/DESCRIBE/EXPLAIN/VALUES/PRAGMA）のみ許可されます"
            f"（検出: '{first.upper() or '(empty)'}'）。",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    config = load_config()
    connections = config.get("connections", {})
    for name, entry in connections.items():
        description = entry.get("description", "")
        print(f"{name:<14}: {description}")


# ---------------------------------------------------------------------------
# 出力ヘルパ（simple / managed で共通）
# ---------------------------------------------------------------------------

def emit_rows(columns: list, rows: list, max_rows: int, max_bytes: int) -> None:
    row_count = len(rows)
    if row_count > max_rows:
        print(
            f"Error: Query returned {row_count} rows, exceeding the limit of {max_rows}."
            " Add a WHERE clause or use COUNT(*) first.",
            file=sys.stderr,
        )
        sys.exit(1)

    output = {
        "columns": list(columns),
        "rows": [list(row) for row in rows],
        "row_count": row_count,
    }
    output_json = json.dumps(output, ensure_ascii=False, default=str)
    output_bytes = len(output_json.encode("utf-8"))
    if output_bytes > max_bytes:
        output_kb = output_bytes // 1024
        limit_kb = max_bytes // 1024
        print(
            f"Error: Output size {output_kb}KB exceeds the limit of {limit_kb}KB.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(output_json)


def emit_cursor_result(cursor, max_rows: int, max_bytes: int) -> None:
    if cursor.description is not None:
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        emit_rows(columns, rows, max_rows, max_bytes)
    else:
        print(json.dumps({"rowcount": cursor.rowcount}))


# ---------------------------------------------------------------------------
# 方言別キャンセラ（実行中クエリのサーバキャンセル）
# ---------------------------------------------------------------------------

class Canceller:
    execute_kwargs: dict = {}

    def prepare(self) -> None:
        pass

    def cancel(self) -> None:
        pass


class PgCanceller(Canceller):
    """PostgreSQL: psycopg2 connection.cancel()（別スレッド安全・同一接続）"""

    def __init__(self, driver_conn):
        self._conn = driver_conn

    def cancel(self) -> None:
        self._conn.cancel()


class DuckDBCanceller(Canceller):
    """DuckDB(埋め込み): 同一接続に interrupt()"""

    def __init__(self, driver_conn):
        self._conn = driver_conn

    def cancel(self) -> None:
        self._conn.interrupt()


class DatabricksCanceller(Canceller):
    """Databricks: 実行中の cursor を別スレッドから cancel()（サーバ側中断）"""

    def __init__(self, cursor):
        self._cursor = cursor

    def cancel(self) -> None:
        self._cursor.cancel()


class SnowflakeCanceller(Canceller):
    """Snowflake: connection._cancel_query(sql, request_id)（同期で動作）"""

    def __init__(self, driver_conn, cursor, sql):
        self._conn = driver_conn
        self._cursor = cursor
        self._sql = sql

    def cancel(self) -> None:
        import time

        req_id = None
        for _ in range(50):  # 実行開始で _request_id が立つのを最大2.5s待つ
            req_id = getattr(self._cursor, "_request_id", None)
            if req_id is not None:
                break
            time.sleep(0.05)
        if req_id is not None:
            self._conn._cancel_query(self._sql, req_id)


class KillQueryCanceller(Canceller):
    """MySQL / MariaDB: 第2接続から KILL QUERY <id>"""

    def __init__(self, engine, get_id):
        self._engine = engine
        self._get_id = get_id
        self._id = None

    def prepare(self) -> None:
        self._id = self._get_id()

    def cancel(self) -> None:
        if self._id is None:
            return
        killer = self._engine.raw_connection()
        try:
            kc = killer.cursor()
            kc.execute("KILL QUERY %d" % int(self._id))
        finally:
            killer.close()


class RedshiftCanceller(Canceller):
    """Redshift: 第2接続から SELECT PG_CANCEL_BACKEND(pid)"""

    def __init__(self, engine, cursor):
        self._engine = engine
        self._cursor = cursor
        self._pid = None

    def prepare(self) -> None:
        self._cursor.execute("SELECT pg_backend_pid()")
        self._pid = self._cursor.fetchone()[0]

    def cancel(self) -> None:
        if self._pid is None:
            return
        killer = self._engine.raw_connection()
        try:
            kc = killer.cursor()
            kc.execute("SELECT PG_CANCEL_BACKEND(%d)" % int(self._pid))
        finally:
            killer.close()


class BigQueryCanceller(Canceller):
    """BigQuery: 自前 job_id で jobs.insert を強制し、別 Client で cancel_job"""

    def __init__(self, driver_conn):
        import uuid

        self.job_id = "runsqlgrid-" + uuid.uuid4().hex
        self.execute_kwargs = {"job_id": self.job_id}
        self._driver_conn = driver_conn

    def cancel(self) -> None:
        client = getattr(self._driver_conn, "_client", None)
        if client is None:
            from google.cloud import bigquery

            client = bigquery.Client()
        location = getattr(client, "location", None)
        client.cancel_job(self.job_id, location=location)


def make_canceller(dialect_name: str, engine, driver_conn, cursor, sql):
    if dialect_name == "postgresql":
        return PgCanceller(driver_conn)
    if dialect_name == "redshift":
        return RedshiftCanceller(engine, cursor)
    if dialect_name == "mysql":
        return KillQueryCanceller(engine, lambda: driver_conn.thread_id())
    if dialect_name == "mariadb":
        return KillQueryCanceller(engine, lambda: driver_conn.connection_id)
    if dialect_name == "snowflake":
        return SnowflakeCanceller(driver_conn, cursor, sql)
    if dialect_name == "bigquery":
        return BigQueryCanceller(driver_conn)
    if dialect_name == "duckdb":
        return DuckDBCanceller(driver_conn)
    if dialect_name == "databricks":
        return DatabricksCanceller(cursor)
    return None


# ---------------------------------------------------------------------------
# クエリ実行
# ---------------------------------------------------------------------------

def read_sql_and_control(args: argparse.Namespace):
    """managed モード: SQL 本文と制御ストリーム(stdin)を返す。

    - --sql 指定時: SQL は引数、stdin は制御チャネル。
    - 未指定時: stdin から番兵までを SQL として読み、以降を制御チャネルに。
      番兵が来ず EOF なら制御チャネルなし(= CLI からの単純 pipe)。
    """
    if args.sql is not None:
        return args.sql, sys.stdin

    lines: list[str] = []
    for line in sys.stdin:
        if line.rstrip("\n") == SQL_SENTINEL:
            return "".join(lines), sys.stdin
        lines.append(line)
    return "".join(lines), None


# DuckDB(埋め込み)/BigQuery/Databricks は isolation_level/AUTOCOMMIT 非対応のため適用しない。
# - DuckDB: dialect が psycopg2 継承で AUTOCOMMIT を申告するが set_isolation_level 未実装 → AttributeError
# - BigQuery: isolation_level 自体が base Dialect のまま未実装 → NotImplementedError
# - Databricks: DatabricksDialect は DefaultDialect 継承で分離レベル未実装。AUTOCOMMIT 適用は
#   connector 4.2.0+ で [CONFIG_NOT_AVAILABLE] AUTOCOMMIT (SQLSTATE 42K0I) を誘発しうるため非適用。
#   → SELECT は実行可。DML が確定するか否かは未検証(残課題)。
_NO_AUTOCOMMIT_BACKENDS = {"duckdb", "bigquery", "databricks"}


def _backend_name(dsn: str) -> str:
    return dsn.split("://", 1)[0].split("+", 1)[0].strip().lower()


def _make_engine(dsn: str):
    from sqlalchemy import create_engine

    if _backend_name(dsn) in _NO_AUTOCOMMIT_BACKENDS:
        return create_engine(dsn)
    return create_engine(dsn, isolation_level="AUTOCOMMIT")


def cmd_query_simple(args: argparse.Namespace, sql: str, dsn: str) -> None:
    """CLI 互換パス（--control-stdin なし）。現行挙動を維持。"""
    from sqlalchemy import text

    try:
        engine = _make_engine(dsn)
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            if result.returns_rows:
                emit_rows(list(result.keys()), result.fetchall(), args.max_rows, args.max_bytes)
            else:
                print(json.dumps({"rowcount": result.rowcount}))
    except Exception as e:
        dialect = dsn.split(":")[0] if ":" in dsn else dsn
        print(f"Error: Failed to connect [{dialect}]: {_redact_secret(str(e), dsn)}", file=sys.stderr)
        sys.exit(1)


def cmd_query_managed(args: argparse.Namespace, sql: str, control_stream, dsn: str) -> None:
    """拡張向けパス（--control-stdin）。ワーカースレッド + タイムアウト/キャンセル。"""
    try:
        engine = _make_engine(dsn)
        sa_conn = engine.connect()
    except Exception as e:
        dialect = dsn.split(":")[0] if ":" in dsn else dsn
        print(f"Error: Failed to connect [{dialect}]: {_redact_secret(str(e), dsn)}", file=sys.stderr)
        sys.exit(1)

    raw = sa_conn.connection
    driver_conn = raw.driver_connection
    cursor = raw.cursor()
    dialect_name = engine.dialect.name

    canceller = None
    if args.cancel_on_server:
        canceller = make_canceller(dialect_name, engine, driver_conn, cursor, sql)
        if canceller is not None:
            try:
                canceller.prepare()
            except Exception:
                canceller = None  # フォールバック: サーバキャンセル不可
        if canceller is None:
            print(
                f"[run_sql] server-side cancel unavailable for dialect '{dialect_name}'; local-only",
                file=sys.stderr,
            )

    state: dict = {"error": None}
    done = threading.Event()
    wake = threading.Event()
    reason: dict = {"v": None}
    reason_lock = threading.Lock()

    def set_reason(v: str) -> None:
        with reason_lock:
            if reason["v"] is None:
                reason["v"] = v

    def worker() -> None:
        try:
            kwargs = canceller.execute_kwargs if canceller else {}
            exec_sql = sql
            if dialect_name == "bigquery":
                # google-cloud-bigquery dbapi の _format_operation() は
                # parameters 未指定時も無条件に operation.replace("%%", "%") を行う
                # (dbapi/cursor.py)。事前に全ての % を %% に倍化しておくことで、
                # このde-escapingが元のテキストを復元するだけになり、
                # BigQuery側 FORMAT() の "%%" エスケープを破壊しなくなる。
                exec_sql = exec_sql.replace("%", "%%")
            cursor.execute(exec_sql, **kwargs)
        except BaseException as e:  # キャンセル例外も含めて捕捉
            state["error"] = e
        finally:
            done.set()
            wake.set()

    def control_reader() -> None:
        try:
            for line in control_stream:
                if line.strip() == "CANCEL":
                    set_reason("cancel")
                    wake.set()
                    break
        except Exception:
            pass

    wt = threading.Thread(target=worker, daemon=True)
    wt.start()
    if control_stream is not None:
        threading.Thread(target=control_reader, daemon=True).start()

    timeout = args.timeout_seconds if args.timeout_seconds and args.timeout_seconds > 0 else None
    signaled = wake.wait(timeout)
    if not signaled:
        set_reason("timeout")

    r = reason["v"]
    if r in ("timeout", "cancel"):
        if canceller is not None:
            try:
                canceller.cancel()
            except Exception:
                pass
            done.wait(timeout=10)  # ワーカーが例外で抜けるのを待つ
        # local モード(canceller=None)はサーバ放置。プロセス終了で接続断。
        print(json.dumps({"status": r}))
        _safe_close(cursor, sa_conn)
        sys.exit(0)

    # 正常完了 or エラー
    if state["error"] is not None:
        print(f"Error: {_redact_secret(str(state['error']), dsn)}", file=sys.stderr)
        _safe_close(cursor, sa_conn)
        sys.exit(1)

    emit_cursor_result(cursor, args.max_rows, args.max_bytes)
    _safe_close(cursor, sa_conn)


def _safe_close(cursor, sa_conn) -> None:
    try:
        cursor.close()
    except Exception:
        pass
    try:
        sa_conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ドライラン（クエリを実行せずコストを見積もる）
# ---------------------------------------------------------------------------

# ドライランを提供できるバックエンド。ここに無い方言はクエリを実行せずエラー終了する。
_DRY_RUN_BACKENDS = ("bigquery",)


def emit_dry_run(estimate: dict, max_bytes: int) -> None:
    """見積もり結果を JSON で出力する（emit_rows と同じ直列化・同じサイズ上限）。"""
    output_json = json.dumps(estimate, ensure_ascii=False, default=str)
    output_bytes = len(output_json.encode("utf-8"))
    if output_bytes > max_bytes:
        output_kb = output_bytes // 1024
        limit_kb = max_bytes // 1024
        print(
            f"Error: Output size {output_kb}KB exceeds the limit of {limit_kb}KB.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(output_json)


def _bigquery_dry_run(sql: str, dsn: str) -> dict:
    """BigQuery のドライランで見積もりを取る（クエリ本体は実行されない）。"""
    from google.cloud import bigquery

    engine = _make_engine(dsn)
    with engine.connect() as conn:
        # BigQueryCanceller.cancel() と同じ経路で、dbapi 接続が解決済みの Client を再利用する。
        # これにより資格情報（GOOGLE_APPLICATION_CREDENTIALS / gcloud ADC）と project /
        # location の解決が通常のクエリ経路と完全に一致する。
        driver_conn = conn.connection.driver_connection
        client = getattr(driver_conn, "_client", None)
        if client is None:
            client = bigquery.Client()
        # dry_run=True では jobs.insert が見積もりだけを返しジョブを作成しない。
        # use_query_cache=False はキャッシュヒット時に 0 バイトと報告されるのを防ぐ。
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )

    try:
        accuracy = job._properties["statistics"]["query"]["totalBytesProcessedAccuracy"]
    except Exception:
        accuracy = None

    referenced = [
        f"{t.project}.{t.dataset_id}.{t.table_id}" for t in (job.referenced_tables or [])
    ]

    return {
        "dry_run": True,
        "dialect": "bigquery",
        "total_bytes_processed": job.total_bytes_processed,
        "total_bytes_processed_accuracy": accuracy,
        "referenced_tables": referenced,
    }


def cmd_query_dry_run(args: argparse.Namespace, sql: str, dsn: str) -> None:
    """--dry-run: 実行せずに見積もりを出す。非対応方言はクエリを実行せずエラー終了する。"""
    backend = _backend_name(dsn)
    if backend not in _DRY_RUN_BACKENDS:
        print(
            f"Error: --dry-run is not supported for dialect [{backend}]"
            f" (supported: {', '.join(_DRY_RUN_BACKENDS)})."
            " The query was NOT executed.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        estimate = _bigquery_dry_run(sql, dsn)
    except Exception as e:
        print(f"Error: Dry run failed [{backend}]: {_redact_secret(str(e), dsn)}", file=sys.stderr)
        sys.exit(1)

    emit_dry_run(estimate, args.max_bytes)


def cmd_query(args: argparse.Namespace) -> None:
    config = load_config()
    dsn = resolve_connection(config, args.connection)

    if args.control_stdin:
        sql, control_stream = read_sql_and_control(args)
        if getattr(args, "read_only", False):
            _assert_read_only(sql)
        if getattr(args, "dry_run", False):
            cmd_query_dry_run(args, sql, dsn)
            return
        cmd_query_managed(args, sql, control_stream, dsn)
    else:
        sql = args.sql if args.sql is not None else sys.stdin.read()
        if getattr(args, "read_only", False):
            _assert_read_only(sql)
        if getattr(args, "dry_run", False):
            cmd_query_dry_run(args, sql, dsn)
            return
        cmd_query_simple(args, sql, dsn)


def main() -> None:
    # stdin/stdout を UTF-8 固定（Windows の cp932 既定だと、stdin 経由の SQL に含まれる
    # 日本語バッククォート識別子（例: `日付`）が化け、エンジンが "Unclosed identifier literal" を返す）。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(prog="run_sql.py")
    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser("list")

    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("--connection", required=True)
    query_parser.add_argument("--sql", default=None)
    query_parser.add_argument("--max-rows", type=int, default=200, dest="max_rows")
    query_parser.add_argument("--max-bytes", type=int, default=51200, dest="max_bytes")
    query_parser.add_argument("--timeout-seconds", type=int, default=0, dest="timeout_seconds")
    query_parser.add_argument("--cancel-on-server", action="store_true", dest="cancel_on_server")
    query_parser.add_argument("--control-stdin", action="store_true", dest="control_stdin")
    query_parser.add_argument("--read-only", action="store_true", dest="read_only",
                              help="読み取り文(SELECT/SHOW/DESCRIBE/EXPLAIN/WITH/VALUES/PRAGMA)以外を拒否する簡易ガード")
    query_parser.add_argument("--dry-run", action="store_true", dest="dry_run")

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
