# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""log.md append + parse (design §4, gist §log.md).

Append-only log with the grep-parseable header grammar:

    ## [YYYY-MM-DD] <op>|<provenance-or-origin> | <Title>

Front-end dispatch (design §4 :127-128):
    ## [YYYY-MM-DD] ingest|source | <Title>   (FE-B)
    ## [YYYY-MM-DD] file|derived  | <Title>   (FE-A)
    ## [YYYY-MM-DD] file|cc-log    | <Title>  (FE-B')

I/O contract:
    format_header(date, op, tag, title) -> str
      out: a single "## [date] op|tag | title" header line.

    append(log_path, op, tag, title, *, date=None, body=None) -> str
      in : path to log.md, the op/tag/title (+ optional body + date)
      out: the appended header. Appends header (and optional body) to the file,
           always starting at line-begin with "## [" so grep can find it.

    parse(log_path) -> list[LogEntry]
      out: list of LogEntry { date, op, tag, title } parsed from every "## ["
           header (grep "^## \\[" log.md equivalent), in file order.

    tail(log_path, n) -> list[LogEntry]   # last n entries

Front-end -> (op, tag) helpers:
    header_for_fe_a()       -> ("file", "derived")
    header_for_fe_b()       -> ("ingest", "source")
    header_for_fe_b_prime() -> ("file", "cc-log")
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path


# Header grammar (fixed token order). Tolerates the variable spacing the template
# examples show around the second `|` (e.g. "file|derived  | Title").
_HEADER_RE = re.compile(
    r"^## \[(\d{4}-\d{2}-\d{2})\]\s+([A-Za-z][\w-]*)\|([A-Za-z][\w-]*)\s*\|\s*(.*)$"
)


@dataclass
class LogEntry:
    date: str
    op: str
    tag: str
    title: str


def format_header(date: str, op: str, tag: str, title: str) -> str:
    return f"## [{date}] {op}|{tag} | {title}"


def _today() -> str:
    return _dt.date.today().isoformat()


def append(log_path: "str | Path", op: str, tag: str, title: str, *,
           date: "str | None" = None, body: "str | None" = None) -> str:
    path = Path(log_path)
    header = format_header(date or _today(), op, tag, title)
    chunk = "\n" + header + "\n"
    if body:
        chunk += "\n" + body.rstrip("\n") + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(chunk)
    return header


def parse(log_path: "str | Path") -> list[LogEntry]:
    path = Path(log_path)
    if not path.is_file():
        return []
    out: list[LogEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _HEADER_RE.match(line)
        if m:
            out.append(LogEntry(date=m.group(1), op=m.group(2),
                                tag=m.group(3), title=m.group(4).strip()))
    return out


def tail(log_path: "str | Path", n: int = 10) -> list[LogEntry]:
    entries = parse(log_path)
    return entries[-n:] if n > 0 else entries


def header_for_fe_a() -> tuple[str, str]:
    return ("file", "derived")


def header_for_fe_b() -> tuple[str, str]:
    return ("ingest", "source")


def header_for_fe_b_prime() -> tuple[str, str]:
    return ("file", "cc-log")
