---
name: run-sql
description: Execute SQL queries against configured databases (PostgreSQL, MySQL, MariaDB, Redshift, Snowflake, BigQuery, DuckDB, Databricks) and return raw results. Use when the user asks to query a database, run SQL, check data, explore tables, or count rows. Also triggered by "SQLを実行", "データを取得", "テーブルを確認".
context: fork
allowed-tools: "Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/run_sql.py *)"
---

## Available connections

!`uv run ${CLAUDE_SKILL_DIR}/scripts/run_sql.py list`

## Query execution

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/run_sql.py query --connection <name> --sql "<SQL>"
```

Defaults: `--max-rows 200`, `--max-bytes 51200` (50KB). Override with flags if needed.

Note: statements are auto-committed — DML/DDL apply immediately and cannot be rolled back. (Exception: for Databricks, AUTOCOMMIT is not applied and whether DML persists is unverified; SELECT works.)

## Procedure

1. Determine the appropriate connection from the list above based on the user's request
2. Construct the SQL query
3. Execute via the command above
4. Return the result

## Output rules

- stdout (exit 0): relay the JSON **verbatim**. Do not interpret, summarize, reformat, or add commentary
- stderr (exit 1): relay the error message **verbatim**
- If row/byte limit exceeded: relay the error, then suggest adding WHERE or using COUNT(*)

$ARGUMENTS
