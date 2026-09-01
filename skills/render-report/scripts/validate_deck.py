# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Validate a deck-spec markdown against the Deck Spec Contract and its report.

Usage:
    uv run --script validate_deck.py path/to/deck-spec.md path/to/report.md

Exit code 0 and "OK" on stdout when the deck-spec conforms.
Exit code 1 and one line per violation otherwise.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ARCHETYPES = ("title", "exec-summary", "section", "finding", "evidence", "appendix")
BODY_ARCHETYPES = ("exec-summary", "section", "finding")

REQUIRED_SLOTS = {
    "title": (),
    "exec-summary": ("message", "findings", "limits"),
    "section": ("headline", "body"),
    "finding": ("headline", "confidence", "body", "evidence"),
    "evidence": (),
    "appendix": (),
}

OPTIONAL_SLOTS = {
    "title": (),
    "exec-summary": ("notes",),
    "section": ("table", "notes"),
    "finding": ("limit", "notes"),
    "evidence": ("notes",),
    "appendix": ("notes",),
}

ALL_SLOTS = {s for group in (REQUIRED_SLOTS, OPTIONAL_SLOTS) for v in group.values() for s in v}

SLIDE_HEADING = re.compile(r"^##\s+S(\d+)\s+(\S+)\s*$")
TRACE_LINE = re.compile(r"^trace\s*[:：]\s*(.+?)\s*$")
SLOT_LINE = re.compile(r"^-\s+([A-Za-z][A-Za-z-]*)\s*[:：]\s*(.*)$")
CHART_ANNOTATION = re.compile(r"^<!--\s*chart:\s*(\S+)\s+(.*?)-->\s*$")
NUMBER_TOKEN = re.compile(r"[0-9][0-9,.]*")
CLAIM_HEADING = re.compile(r"^###\s+\[F-(\d+)\]\s+(.+?)\s*$")
TABLE_SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

FIXED_SECTION_TRACES = ("エグゼクティブサマリ", "分析対象データ", "データの基本的特徴", "限界と注意事項")
COVERAGE_SECTIONS = ("エグゼクティブサマリ", "限界と注意事項")


def load_contract_module():
    path = Path(__file__).resolve().parent / "validate_contract.py"
    spec = importlib.util.spec_from_file_location("validate_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTRACT = load_contract_module()


# ---------------------------------------------------------------- parsing


class Slide:
    def __init__(self, number: int, archetype: str, line_no: int) -> None:
        self.number = number
        self.archetype = archetype
        self.line_no = line_no
        self.trace: str | None = None
        self.trace_line = 0
        self.slots: dict[str, str] = {}
        self.slot_lines: dict[str, int] = {}
        self.content: list[tuple[int, str]] = []
        self.tables: list[Table] = []
        self.charts: list[tuple[int, str]] = []


class Table:
    def __init__(self, line_no: int, header: list[str], rows: list[list[str]]) -> None:
        self.line_no = line_no
        self.header = header
        self.rows = rows

    def key(self) -> tuple[str, ...]:
        return tuple(self.header)

    def matches(self, other: "Table") -> bool:
        return self.header == other.header and self.rows == other.rows


def split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def read_tables(lines: list[str], start: int = 0, end: int | None = None) -> list[Table]:
    """Collect every GFM table in lines[start:end]."""
    end = len(lines) if end is None else end
    tables, i = [], start
    while i < end - 1:
        header, sep = lines[i], lines[i + 1]
        if header.lstrip().startswith("|") and TABLE_SEPARATOR.match(sep):
            rows, j = [], i + 2
            while j < end and lines[j].lstrip().startswith("|"):
                rows.append(split_row(lines[j]))
                j += 1
            tables.append(Table(i + 1, split_row(header), rows))
            i = j
            continue
        i += 1
    return tables


def parse_deck(lines: list[str], errors: list[str]) -> tuple[dict[str, str], list[Slide]]:
    fm, body_start = CONTRACT.parse_frontmatter(lines)
    slides: list[Slide] = []
    current: Slide | None = None
    pending_slot: str | None = None

    for i in range(body_start, len(lines)):
        raw = lines[i]
        line_no = i + 1
        heading = SLIDE_HEADING.match(raw)
        if heading:
            current = Slide(int(heading.group(1)), heading.group(2), line_no)
            slides.append(current)
            pending_slot = None
            continue
        if current is None:
            if raw.strip():
                errors.append(f"L{line_no}: content before the first '## S<n> <archetype>' heading")
            continue

        trace = TRACE_LINE.match(raw)
        if trace and current.trace is None and not current.slots:
            current.trace = trace.group(1)
            current.trace_line = line_no
            continue

        slot = SLOT_LINE.match(raw)
        if slot and slot.group(1) in ALL_SLOTS:
            name = slot.group(1)
            if name in current.slots:
                errors.append(f"L{line_no}: slot '{name}' is repeated on slide S{current.number}")
            current.slots[name] = slot.group(2)
            current.slot_lines[name] = line_no
            current.content.append((line_no, slot.group(2)))
            pending_slot = name
            continue

        if raw.strip():
            current.content.append((line_no, raw))
            if pending_slot and not raw.lstrip().startswith("|") and not raw.lstrip().startswith("<!--"):
                current.slots[pending_slot] += "\n" + raw

    slide_lines = [(s, s.line_no) for s in slides]
    for idx, (slide, _) in enumerate(slide_lines):
        end = slide_lines[idx + 1][1] - 1 if idx + 1 < len(slide_lines) else len(lines)
        for table in read_tables(lines, slide.line_no, end):
            slide.tables.append(table)
        for j in range(slide.line_no, end):
            if CHART_ANNOTATION.match(lines[j].strip()):
                slide.charts.append((j + 1, lines[j].strip()))
    return fm, slides


# ---------------------------------------------------------------- report side


def report_claim_ids(lines: list[str]) -> list[str]:
    return [f"F-{m.group(1)}" for m in (CLAIM_HEADING.match(line) for line in lines) if m]


def report_claim_fields(lines: list[str]) -> dict[str, dict[str, str]]:
    fields: dict[str, dict[str, str]] = {}
    for _, cid, _, body in CONTRACT.claim_blocks(lines):
        joined = "\n".join(body)
        entry: dict[str, str] = {}
        for name in ("根拠", "検証状態", "確信度", "解釈", "実務示唆", "限界"):
            m = re.search(rf"^\s*-\s*{name}\s*[:：]\s*(.+)$", joined, re.MULTILINE)
            if m:
                entry[name] = m.group(1).strip()
        fields[f"F-{cid}"] = entry
    return fields


def report_sections(lines: list[str]) -> list[str]:
    return [line[3:].strip() for line in lines if line.startswith("## ")]


def numeric_tokens(text: str) -> set[str]:
    out = set()
    for raw in NUMBER_TOKEN.findall(text):
        token = raw.replace(",", "").rstrip(".")
        if token:
            out.add(token)
    return out


# ---------------------------------------------------------------- checks


def check_structure(fm: dict[str, str], slides: list[Slide], report_lines: list[str],
                    errors: list[str]) -> None:
    if not fm.get("source"):
        errors.append("frontmatter: 'source' is missing or empty")
    report_fm, _ = CONTRACT.parse_frontmatter(report_lines)
    deck_title = fm.get("title", "")
    if not deck_title:
        errors.append("frontmatter: 'title' is missing or empty")
    elif deck_title != report_fm.get("title", ""):
        errors.append("frontmatter: 'title' is not the report title verbatim")

    if not slides:
        errors.append("deck: no slide found (expected '## S1 <archetype>')")
        return

    for expected, slide in enumerate(slides, start=1):
        if slide.archetype not in ARCHETYPES:
            errors.append(
                f"L{slide.line_no}: archetype '{slide.archetype}' is not one of "
                f"{' / '.join(ARCHETYPES)}"
            )
            continue
        if slide.number != expected:
            errors.append(
                f"L{slide.line_no}: slide number S{slide.number} breaks the sequence "
                f"(expected S{expected})"
            )
        for name in REQUIRED_SLOTS[slide.archetype]:
            if name not in slide.slots:
                errors.append(
                    f"L{slide.line_no}: S{slide.number} ({slide.archetype}) is missing "
                    f"'- {name}:'"
                )
        allowed = set(REQUIRED_SLOTS[slide.archetype]) | set(OPTIONAL_SLOTS[slide.archetype])
        for name in slide.slots:
            if name not in allowed:
                errors.append(
                    f"L{slide.slot_lines[name]}: slot '{name}' is not allowed on a "
                    f"{slide.archetype} slide"
                )
        if "table" in slide.slots and not slide.tables:
            errors.append(
                f"L{slide.line_no}: S{slide.number} declares '- table:' but carries no GFM table"
            )
        if slide.archetype == "evidence" and not slide.tables:
            errors.append(f"L{slide.line_no}: S{slide.number} (evidence) carries no GFM table")


def check_trace(slides: list[Slide], report_lines: list[str], errors: list[str]) -> None:
    claims = set(report_claim_ids(report_lines))
    sections = set(report_sections(report_lines))
    appendix_subs = set(CONTRACT.section_subheadings(report_lines, "付録"))

    for slide in slides:
        if slide.archetype == "title":
            if slide.trace is not None:
                errors.append(f"L{slide.trace_line}: a title slide must not carry 'trace:'")
            continue
        if slide.trace is None:
            errors.append(f"L{slide.line_no}: S{slide.number} is missing 'trace:'")
            continue
        value = slide.trace
        if value.startswith("付録/"):
            sub = value[len("付録/"):]
            if sub not in appendix_subs:
                errors.append(
                    f"L{slide.trace_line}: trace '{value}' does not resolve to a '### {sub}' "
                    "under '## 付録'"
                )
            continue
        if re.fullmatch(r"F-\d+", value):
            if value not in claims:
                errors.append(
                    f"L{slide.trace_line}: trace '{value}' does not resolve to a claim block"
                )
            continue
        if value in FIXED_SECTION_TRACES:
            if value not in sections:
                errors.append(
                    f"L{slide.trace_line}: trace '{value}' does not resolve to a '## {value}' section"
                )
            continue
        errors.append(f"L{slide.trace_line}: trace '{value}' is not a contract trace value")


def check_coverage(slides: list[Slide], report_lines: list[str], errors: list[str]) -> None:
    traced = {s.trace for s in slides if s.archetype in BODY_ARCHETYPES and s.trace}
    for cid in report_claim_ids(report_lines):
        if cid not in traced:
            errors.append(f"coverage: claim [{cid}] has no body slide (exec-summary / section / finding)")
    for name in COVERAGE_SECTIONS:
        if name in report_sections(report_lines) and name not in traced:
            errors.append(f"coverage: section '{name}' has no body slide")


def check_confidence(slides: list[Slide], report_lines: list[str], errors: list[str]) -> None:
    fields = report_claim_fields(report_lines)
    carries_limit: dict[str, bool] = {}
    for slide in slides:
        if slide.archetype != "finding" or not slide.trace:
            continue
        entry = fields.get(slide.trace)
        if entry is None:
            continue
        expected = entry.get("確信度", "")
        actual = slide.slots.get("confidence", "").strip()
        if expected and actual and actual != expected:
            errors.append(
                f"L{slide.slot_lines.get('confidence', slide.line_no)}: confidence is not the "
                f"report 確信度 verbatim (report: {expected})"
            )
        carries_limit.setdefault(slide.trace, False)
        if "limit" in slide.slots:
            carries_limit[slide.trace] = True
    for cid, entry in fields.items():
        if "限界" in entry and cid in carries_limit and not carries_limit[cid]:
            errors.append(f"coverage: claim [{cid}] has '- 限界:' in the report but no '- limit:' slide")


def deck_numeric_text(slides: list[Slide]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for slide in slides:
        out.extend(slide.content)
    return out


def check_numbers(slides: list[Slide], report_lines: list[str], errors: list[str]) -> None:
    allowed = numeric_tokens("\n".join(report_lines))
    for line_no, text in deck_numeric_text(slides):
        for token in sorted(numeric_tokens(text)):
            if token not in allowed:
                errors.append(f"L{line_no}: numeric token '{token}' does not occur in the report")


def check_tables(slides: list[Slide], report_lines: list[str], errors: list[str]) -> None:
    report_tables = read_tables(report_lines)
    by_header: dict[tuple[str, ...], list[Table]] = {}
    for table in report_tables:
        by_header.setdefault(table.key(), []).append(table)
    for slide in slides:
        for table in slide.tables:
            candidates = by_header.get(table.key())
            if not candidates:
                continue
            if not any(table.matches(c) for c in candidates):
                errors.append(
                    f"L{table.line_no}: source table (header matches the report) differs from "
                    "the report table row for row"
                )


def check_charts(slides: list[Slide], lines: list[str], report_lines: list[str],
                 errors: list[str]) -> None:
    report_annotations = {
        " ".join(line.split()) for line in report_lines if CHART_ANNOTATION.match(line.strip())
    }
    report_tables = read_tables(report_lines)
    report_keys = {t.key() for t in report_tables}
    for slide in slides:
        for line_no, text in slide.charts:
            if " ".join(text.split()) not in report_annotations:
                errors.append(
                    f"L{line_no}: chart annotation does not occur in the report"
                )
            following = [t for t in slide.tables if t.line_no > line_no]
            if not following:
                errors.append(f"L{line_no}: chart annotation is not followed by a GFM table")
                continue
            if following[0].key() not in report_keys:
                errors.append(
                    f"L{line_no}: the table after a chart annotation is not a source table"
                )


def check_forbidden(slides: list[Slide], report_lines: list[str], errors: list[str]) -> None:
    report_fm, _ = CONTRACT.parse_frontmatter(report_lines)
    if report_fm.get("purpose") != "findings":
        return
    for slide in slides:
        if slide.archetype not in BODY_ARCHETYPES:
            continue
        for line_no, text in slide.content:
            for hint in CONTRACT.ACTION_VERB_HINTS:
                if hint in text:
                    errors.append(
                        f"L{line_no}: action-oriented wording '{hint}' is not allowed "
                        "(report purpose: findings)"
                    )


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 3:
        print("usage: validate_deck.py path/to/deck-spec.md path/to/report.md", file=sys.stderr)
        return 2
    deck_path, report_path = Path(sys.argv[1]), Path(sys.argv[2])
    for path in (deck_path, report_path):
        if not path.is_file():
            print(f"FAIL: file not found: {path}", file=sys.stderr)
            return 2

    deck_lines = deck_path.read_text(encoding="utf-8").splitlines()
    report_lines = report_path.read_text(encoding="utf-8").splitlines()
    errors: list[str] = []

    fm, slides = parse_deck(deck_lines, errors)
    check_structure(fm, slides, report_lines, errors)
    check_trace(slides, report_lines, errors)
    check_coverage(slides, report_lines, errors)
    check_confidence(slides, report_lines, errors)
    check_numbers(slides, report_lines, errors)
    check_tables(slides, report_lines, errors)
    check_charts(slides, deck_lines, report_lines, errors)
    check_forbidden(slides, report_lines, errors)

    if errors:
        print(f"FAIL: {len(errors)} deck contract violation(s)")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
