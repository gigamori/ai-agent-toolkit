# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""R7 cc-log extractor — one-pass markdown with text + tool_use(tool-result).

Merges the logic of two existing scripts (design §6 / R7):
  - cc-log-extract `extract.py`: markdown transcript, FULL text, multi-turn
    pairing of human/assistant rows.
  - revert_cc_log_extract.py: tool_use block extraction + per-tool display
    rendering (the `render_action` table).
into ONE pass that produces markdown preserving conversation text AND tool_use
blocks (with their tool-result), in chronological turn order. tool_use retention
is required for the `tool-result` epistemic-status (source-tier promotion needs
it), which neither source script alone provides (cc-log-extract drops tool_use;
revert emits a non-markdown serialize protocol).

This is the FE-B' extractor input: it produces the markdown body that FE-B' then
redacts (D16), hashes (D18) and files at raw/derived/<hash>.md.

I/O contract:
    extract_markdown(jsonl_path) -> str
      in : path to a CC session jsonl
      out: markdown transcript string. Per turn: a `## Turn N [ts]` header,
           `**Human**:` full text, then assistant `**Assistant**:` full text and
           `**Tool: <Name>**` blocks (rendered input + paired tool_result), in
           chronological order. Raises ExtractError on read / DuckDB failure.

    render_tool_use(name, inp) -> str   # per-tool display (from revert script)

Design constraints honored:
  - FULL text (no truncation) — from cc-log-extract.
  - tool_use blocks kept and rendered with their tool_result — the R7 gap fix.
  - multi-turn chronological order — from cc-log-extract's LEAD() pairing idea,
    here done in Python over the chronologically-ordered rows.
  - NO redaction here: redaction (D16) is FE-B''s separate mandatory stage, run
    on this markdown before hashing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    import duckdb
except ImportError:  # pragma: no cover - dependency declared in script header
    duckdb = None


class ExtractError(Exception):
    """jsonl read / DuckDB failure (analogous to revert script exit code 3)."""


# Reads every assistant + user record in chronological order, with the raw
# message content as a JSON value (so multi-block content — text + tool_use, and
# user tool_result — is preserved). Mirrors cc-log-extract's cc_raw shaping but
# keeps the full content array rather than only content[0].
_SQL = """
SELECT
  json_extract_string(line::JSON, '$.type') AS record_type,
  strftime(
    CAST(json_extract_string(line::JSON, '$.timestamp') AS TIMESTAMP)
      AT TIME ZONE 'UTC' AT TIME ZONE current_setting('timezone'),
    '%Y-%m-%d %H:%M:%S'
  ) AS ts_local,
  json_extract(line::JSON, '$.message.content') AS content,
  json_extract_string(line::JSON, '$.message.role') AS role
FROM read_csv('{path}', columns={{'line': 'VARCHAR'}}, delim=chr(0))
WHERE json_extract_string(line::JSON, '$.type') IN ('assistant', 'user')
ORDER BY CAST(json_extract_string(line::JSON, '$.timestamp') AS TIMESTAMP) ASC
"""


def _fetch_rows(jsonl_path: Path) -> list[tuple]:
    if duckdb is None:  # pragma: no cover
        raise ExtractError("duckdb not available")
    safe = str(jsonl_path).replace("\\", "/").replace("'", "''")
    try:
        con = duckdb.connect()
        return con.execute(_SQL.format(path=safe)).fetchall()
    except Exception as e:  # noqa: BLE001 - surface as ExtractError per contract
        raise ExtractError(f"jsonl read failure: {e}") from e


# --- tool_use rendering (adapted from revert_cc_log_extract.render_action) ----
def render_tool_use(name: str, inp: dict) -> str:
    """Display form for a tool_use block. Unknown tool -> name only.

    Unlike the revert script (single-line serialize protocol), this keeps inputs
    readable in markdown; the most-identifying field is surfaced per tool.
    """
    def field(key: str) -> str:
        v = inp.get(key, "")
        return v if isinstance(v, str) else str(v)

    if name == "Bash":
        return f"Bash: {field('command')}"
    if name == "Write":
        return f"Write: {field('file_path')}"
    if name == "Edit":
        return f"Edit: {field('file_path')}"
    if name == "Read":
        return f"Read: {field('file_path')}"
    if name == "Glob":
        return f"Glob: {field('pattern')}"
    if name == "Grep":
        return f"Grep: {field('pattern')}"
    if name == "Agent":
        sub = inp.get("subagent_type") or inp.get("description") or ""
        return f"Agent: {sub if isinstance(sub, str) else str(sub)}"
    if name == "NotebookEdit":
        return f"NotebookEdit: {field('notebook_path')}"
    return name


@dataclass
class _Block:
    kind: str              # "text" | "tool_use" | "tool_result"
    text: str = ""
    tool_name: str = ""
    tool_use_id: str = ""
    tool_input: dict = field(default_factory=dict)


def _as_list(content_value) -> list:
    if isinstance(content_value, str):
        try:
            content = json.loads(content_value)
        except json.JSONDecodeError:
            return [{"type": "text", "text": content_value}]
    else:
        content = content_value
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _parse_blocks(content_value) -> list[_Block]:
    blocks: list[_Block] = []
    for b in _as_list(content_value):
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            blocks.append(_Block("text", text=b.get("text") or ""))
        elif btype == "tool_use":
            inp = b.get("input")
            blocks.append(_Block(
                "tool_use",
                tool_name=b.get("name") or "",
                tool_use_id=b.get("id") or "",
                tool_input=inp if isinstance(inp, dict) else {},
            ))
        elif btype == "tool_result":
            tc = b.get("content")
            if isinstance(tc, list):
                parts = []
                for part in tc:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part.get("text") or "")
                    elif isinstance(part, str):
                        parts.append(part)
                txt = "\n".join(parts)
            elif isinstance(tc, str):
                txt = tc
            else:
                txt = "" if tc is None else str(tc)
            blocks.append(_Block(
                "tool_result", text=txt, tool_use_id=b.get("tool_use_id") or "",
            ))
    return blocks


def extract_markdown(jsonl_path: "str | Path") -> str:
    """Produce a one-pass markdown transcript (text + tool_use + tool_result)."""
    rows = _fetch_rows(Path(jsonl_path))
    return _build_markdown(rows)


def _build_markdown(rows: list[tuple]) -> str:
    # First pass: collect tool_result text by tool_use_id (results live on the
    # next user row after the assistant tool_use).
    results: dict[str, str] = {}
    for record_type, _ts, content_value, _role in rows:
        if record_type != "user":
            continue
        for b in _parse_blocks(content_value):
            if b.kind == "tool_result" and b.tool_use_id:
                results[b.tool_use_id] = b.text

    lines: list[str] = ["# CC Session transcript", ""]
    turn = 0
    for record_type, ts, content_value, _role in rows:
        blocks = _parse_blocks(content_value)
        if record_type == "user":
            human_text = "\n".join(
                b.text for b in blocks if b.kind == "text" and b.text.strip()
            ).strip()
            # Skip pure tool_result-only user rows (rendered under the tool_use).
            if not human_text:
                continue
            turn += 1
            lines += [f"## Turn {turn} [{ts}]", "", "**Human**:", human_text, ""]
        elif record_type == "assistant":
            for b in blocks:
                if b.kind == "text" and b.text.strip():
                    lines += ["**Assistant**:", b.text, ""]
                elif b.kind == "tool_use":
                    lines += [f"**Tool: {render_tool_use(b.tool_name, b.tool_input)}**", ""]
                    res = results.get(b.tool_use_id)
                    if res:
                        lines += ["```tool-result", res, "```", ""]
    return "\n".join(lines)


def main() -> None:  # pragma: no cover - thin CLI wrapper
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="R7 cc-log extractor (text + tool_use)")
    ap.add_argument("jsonl", help="Path to a CC session jsonl")
    ap.add_argument("-o", "--output", default=None, help="Output file (default stdout)")
    args = ap.parse_args()
    try:
        md = extract_markdown(args.jsonl)
    except ExtractError as e:
        print(str(e), file=sys.stderr)
        sys.exit(3)
    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(md)


if __name__ == "__main__":
    main()
