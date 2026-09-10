# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Check the text of an Office file produced by an officecli-isolate child.

Usage:
    uv run --script check_office_text.py FILE --source SRC [--source SRC ...] --mode verbatim|rewrite
    uv run --script check_office_text.py FILE --instruction INSTRUCTION.md

The text comes from `officecli view FILE text`, plus what that view hides:
table cells (`officecli query FILE tc --json`; docx folds a table to
`[Table: N rows]`), native chart titles, categories and series values
(`officecli query FILE chart --json`), and the raw value behind a formatted
xlsx cell (`officecli query FILE cell --json`, `format.value`). `--slides A-B`
restricts a pptx to those slides (text, cells and charts alike).

--mode verbatim (docx / xlsx built by transcription):
    every source line (markdown, stripped of its markup; CSV, every non-empty
    cell) must occur in the text; every number must occur.
--mode rewrite (pptx, authored freely from the source):
    every number in the source must occur in the text (violation);
    source headings absent from the text are LISTED, not judged;
    numbers in the text that are not in the source are LISTED, not judged.
--instruction (partial update):
    every `value:` of every `## <id>` block must occur in the text;
    `prop:` edits cannot be seen in text and are listed as not checked.

Numbers are compared after removing digit-group commas, so `1,240` and `1240`
match. Whitespace is collapsed before comparison.

stdout: listings, then the verdict last -- `OK` (exit 0) or `FAIL: N
violation(s)` followed by one line per violation (exit 1). Exit 2 on usage or
IO errors. A U+FFFD in the captured text is reported as a warning line.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

OFFICECLI = "officecli"
MAX_LINES = "100000"
NUMBER_TOKEN = re.compile(r"(?<![A-Za-z0-9])[-+]?[0-9][0-9,]*(?:\.[0-9]+)?%?")
SLIDE_RANGE = re.compile(r"^(\d+)-(\d+)$")
HEADING = re.compile(r"^#{1,6}\s+(.*)$")
ANCHOR = re.compile(r"^\[[A-Z]+-\d+\]\s*")
LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
QUOTE_MARKER = re.compile(r"^>\s?")
FENCE = re.compile(r"^```")
THEMATIC = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})\s*$")
TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~|`)")
CELL_PREFIX = re.compile(r"(?<![A-Za-z])[A-Z]{1,3}[0-9]{1,7}=")
SLIDE_MARKER = re.compile(r"^===\s*/slide\[(\d+)\]\s*===$")
SLIDE_IN_PATH = re.compile(r"/slide\[(\d+)\]")
SERIES_KEY = re.compile(r"^series\d+$")
BLOCK_ID = re.compile(r"^##\s+(\S+)\s*$")
FIELD = re.compile(r"^(location|value|prop|tracked):\s*(.*)$")
REPLACEMENT_CHAR = "�"
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def squash(text: str) -> str:
    return " ".join(text.split())


def canon_number(token: str) -> str:
    token = token.replace(",", "").lstrip("+")
    return token.rstrip(".")


def strip_inline(line: str) -> str:
    line = IMAGE.sub(r"\1", line)
    line = LINK.sub(r"\1", line)
    line = EMPHASIS.sub("", line)
    return line


def run_officecli(args: list[str], required: bool) -> str:
    proc = subprocess.run(
        [OFFICECLI, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        if required:
            sys.stderr.write(proc.stderr)
            raise SystemExit(f"officecli {' '.join(args[:2])} failed with exit {proc.returncode}")
        return ""
    if REPLACEMENT_CHAR in proc.stdout:
        print(f"warning: U+FFFD in officecli {args[0]} output; a non-UTF-8 byte was replaced")
    return proc.stdout


def in_slides(path: str, slides: set[int] | None) -> bool:
    if slides is None:
        return True
    m = SLIDE_IN_PATH.search(path)
    return m is not None and int(m.group(1)) in slides


def query_results(path: Path, selector: str) -> list[dict]:
    raw = run_officecli(["query", str(path), selector, "--json"], required=False)
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return (data.get("data") or {}).get("results") or []


# Text the `view text` mode hides: table cells (docx folds a table to
# `[Table: N rows]`) and native chart data (no view mode prints it).
def hidden_text(path: Path, slides: set[int] | None) -> list[str]:
    lines: list[str] = []
    for cell in query_results(path, "tc"):
        if in_slides(cell.get("path", ""), slides):
            lines.append(str(cell.get("text", "")))
    # xlsx: a number format changes the displayed text; the raw value sits in format.value.
    for cell in query_results(path, "cell"):
        value = (cell.get("format") or {}).get("value")
        if value is not None:
            lines.append(str(value))
    for chart in query_results(path, "chart"):
        if not in_slides(chart.get("path", ""), slides):
            continue
        fmt = chart.get("format") or {}
        lines.append(str(chart.get("preview", "")))
        lines.append(str(fmt.get("categories", "")).replace(",", " "))
        for key, value in fmt.items():
            if SERIES_KEY.match(key):
                lines.append(str(value).replace(":", " ").replace(",", " "))
        for series in chart.get("children") or []:
            sfmt = series.get("format") or {}
            lines.append(str(sfmt.get("name", "")))
            lines.append(str(sfmt.get("values", "")).replace(",", " "))
    return lines


def collect_text(path: Path, slides: set[int] | None) -> tuple[str, set[str]]:
    raw = run_officecli(["view", str(path), "text", "--max-lines", MAX_LINES], required=True)
    lines: list[str] = []
    current = None
    for line in raw.splitlines():
        marker = SLIDE_MARKER.match(line.strip())
        if marker:
            current = int(marker.group(1))
            continue
        if slides is not None and (current is None or current not in slides):
            continue
        lines.append(CELL_PREFIX.sub("", line))
    lines.extend(hidden_text(path, slides))
    text = squash("\n".join(lines))
    numbers = {canon_number(t) for t in NUMBER_TOKEN.findall(text)}
    return text, numbers


def markdown_units(src: Path) -> tuple[list[str], list[str], set[str]]:
    headings: list[str] = []
    units: list[str] = []
    numbers: set[str] = set()
    in_fence = False
    for raw in src.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line or THEMATIC.match(line) or TABLE_SEP.match(line):
            continue

        # Headings: the text without the marker and without an anchor id.
        m = HEADING.match(line)
        if m:
            text = squash(strip_inline(ANCHOR.sub("", m.group(1))))
            if text:
                headings.append(text)
                units.append(text)
            numbers.update(canon_number(t) for t in NUMBER_TOKEN.findall(text))
            continue

        # Table rows: one unit per cell.
        if line.startswith("|"):
            cells = [squash(strip_inline(c)) for c in line.strip("|").split("|")]
            for cell in cells:
                if cell:
                    units.append(cell)
                    numbers.update(canon_number(t) for t in NUMBER_TOKEN.findall(cell))
            continue

        text = LIST_MARKER.sub("", QUOTE_MARKER.sub("", line))
        text = squash(strip_inline(text))
        if text:
            units.append(text)
            numbers.update(canon_number(t) for t in NUMBER_TOKEN.findall(text))
    return headings, units, numbers


def csv_units(src: Path) -> tuple[list[str], set[str]]:
    units: list[str] = []
    numbers: set[str] = set()
    with src.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            for cell in row:
                cell = squash(cell)
                if not cell:
                    continue
                if NUMBER_TOKEN.fullmatch(cell):
                    numbers.add(canon_number(cell))
                    continue
                units.append(cell)
                numbers.update(canon_number(t) for t in NUMBER_TOKEN.findall(cell))
    return units, numbers


def present(unit: str, text: str, text_numbers: set[str]) -> bool:
    if unit in text:
        return True
    if NUMBER_TOKEN.fullmatch(unit):
        return canon_number(unit) in text_numbers
    return False


def instruction_expectations(path: Path) -> tuple[list[tuple[str, str]], list[str]]:
    values: list[tuple[str, str]] = []
    prop_only: list[str] = []
    block_id = None
    block_value = None
    has_prop = False
    awaiting_fence = False
    fence_lines: list[str] | None = None

    def close_block() -> None:
        if block_id is None:
            return
        if block_value is not None:
            values.append((block_id, block_value))
        elif has_prop:
            prop_only.append(block_id)

    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.rstrip()

        # A `value:` with nothing inline takes the next fenced block as its text.
        if awaiting_fence:
            awaiting_fence = False
            if FENCE.match(line.strip()):
                fence_lines = []
                continue
        if fence_lines is not None:
            if FENCE.match(line.strip()):
                block_value = squash("\n".join(fence_lines))
                fence_lines = None
            else:
                fence_lines.append(line)
            continue

        m = BLOCK_ID.match(line)
        if m:
            close_block()
            block_id, block_value, has_prop = m.group(1), None, False
            continue
        f = FIELD.match(line.strip())
        if not f or block_id is None:
            continue
        key, rest = f.group(1), f.group(2).strip()
        if key == "value":
            if rest:
                block_value = squash(strip_inline(rest))
            else:
                awaiting_fence = True
        elif key == "prop":
            has_prop = True
    close_block()
    return values, prop_only


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("file")
    ap.add_argument("--source", action="append", default=[])
    ap.add_argument("--mode", choices=["verbatim", "rewrite"])
    ap.add_argument("--instruction")
    ap.add_argument("--slides")
    args = ap.parse_args()

    target = Path(args.file)
    if not target.is_file():
        print(f"error: no such file: {target}")
        return EXIT_USAGE
    slides: set[int] | None = None
    if args.slides:
        m = SLIDE_RANGE.match(args.slides.strip())
        if not m or int(m.group(1)) > int(m.group(2)):
            print("error: --slides takes A-B with 1 <= A <= B")
            return EXIT_USAGE
        slides = set(range(int(m.group(1)), int(m.group(2)) + 1))
    if bool(args.source) == bool(args.instruction):
        print("error: give --source (with --mode) or --instruction, not both, not neither")
        return EXIT_USAGE
    if args.source and not args.mode:
        print("error: --mode is required with --source")
        return EXIT_USAGE

    text, text_numbers = collect_text(target, slides)
    violations: list[str] = []
    listings: list[str] = []

    if args.instruction:
        ins = Path(args.instruction)
        if not ins.is_file():
            print(f"error: no such file: {ins}")
            return EXIT_USAGE
        values, prop_only = instruction_expectations(ins)
        for block_id, value in values:
            if not present(value, text, text_numbers):
                violations.append(f"value not found: {block_id}: {value}")
        for block_id in prop_only:
            listings.append(f"not checked (prop edit): {block_id}")
        if not values and not prop_only:
            violations.append("no edit blocks found in the instruction document")
    else:
        source_numbers: set[str] = set()
        for s in args.source:
            src = Path(s)
            if not src.is_file():
                print(f"error: no such file: {src}")
                return EXIT_USAGE
            if src.suffix.lower() in (".csv", ".tsv"):
                units, numbers = csv_units(src)
                headings: list[str] = []
            else:
                headings, units, numbers = markdown_units(src)
            source_numbers |= numbers

            if args.mode == "verbatim":
                for unit in units:
                    if not present(unit, text, text_numbers):
                        violations.append(f"not found ({src.name}): {unit}")
            else:
                for h in headings:
                    if h not in text:
                        listings.append(f"heading not found ({src.name}): {h}")

        # A sign is presentation: "declined by 3%" and a "-3%" card carry one fact.
        text_all = text_numbers | {n.lstrip("-") for n in text_numbers}
        source_all = source_numbers | {n.lstrip("-") for n in source_numbers}
        for n in sorted(source_numbers):
            if n not in text_all and n.lstrip("-") not in text_all:
                violations.append(f"number not found: {n}")
        if args.mode == "rewrite":
            extra = sorted(n for n in text_numbers if n not in source_all and n.lstrip("-") not in source_all)
            listings.append(f"numbers not in the source: {len(extra)}")
            listings.extend(f"  {n}" for n in extra)

    for line in listings:
        print(line)
    if violations:
        print(f"FAIL: {len(violations)} violation(s)")
        for v in violations:
            print(v)
        return EXIT_FAIL
    print("OK")
    return EXIT_OK


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
