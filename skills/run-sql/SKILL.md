---
name: run-sql
description: Execute SQL queries against configured databases (PostgreSQL, MySQL, MariaDB, Redshift, Snowflake, BigQuery, DuckDB, Databricks) and return raw results. Use when the user asks to query a database, run SQL, check data, explore tables, or count rows. Also triggered by "SQLを実行", "データを取得", "テーブルを確認".
context: fork
---

## Available connections

!`uv run ${CLAUDE_SKILL_DIR}/scripts/run_sql.py list`

Never Read/cat connection config files (e.g. `connections.toml`) — they hold credentials and reading echoes them into the transcript. Take the connection name from `list` above, or ask the user.

## Query execution

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/run_sql.py query --connection <name> --sql "<SQL>"
```

Defaults: `--max-rows 200`, `--max-bytes 51200` (50KB). Override with flags if needed.

## Large results (file output)

When the result is too large to relay inline — a row/byte limit error, or the caller asked for file output — redirect stdout to a file instead of relaying:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/run_sql.py query --connection <name> --max-rows <N> --max-bytes <N> --sql "<SQL>" > <output-path>
```

- Raise `--max-rows` / `--max-bytes` explicitly: the limits are enforced **before** stdout, so redirection alone does not bypass them
- Write to the caller-specified output path if one was given; otherwise choose a path and report it
- On success, report only the output path and the fact that it was written — do **not** paste the file contents
- stderr is not redirected: on failure, relay the error message verbatim as usual

Note: statements are auto-committed — DML/DDL apply immediately and cannot be rolled back. Exceptions:

- **Databricks**: AUTOCOMMIT is not applied and whether DML persists is unverified; SELECT works.
- **DuckDB**: AUTOCOMMIT is not applied either. Against a *file* database, DDL/DML is rolled back when the connection closes unless the statement batch ends with `COMMIT;` — verified: a `CREATE TABLE` / `CREATE MACRO` run without it is gone on the next invocation. Against an *in-memory* database (`:memory:`) nothing persists by design, so `COMMIT` is irrelevant.

## Procedure

1. Determine the appropriate connection from the list above based on the user's request
2. Construct the SQL query
3. Execute via the command above
4. Return the result

## Output rules

- stdout (exit 0): relay the JSON **verbatim**. Do not interpret, summarize, reformat, or add commentary
- stderr (exit 1): relay the error message **verbatim**
- If row/byte limit exceeded: relay the error, then either suggest adding WHERE / using COUNT(*), or rerun with file output and raised limits (see "Large results")

$ARGUMENTS
