# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Validate a report markdown against the Report Markdown Contract.

Usage:
    uv run --script validate_contract.py path/to/report.md

Exit code 0 and "OK" on stdout when the document conforms.
Exit code 1 and one line per violation otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = [
    "エグゼクティブサマリ",
    "分析対象データ",
    "データの基本的特徴",
    "発見事項",
    "限界と注意事項",
    "付録",
]

REQUIRED_APPENDIX = ["再現性", "変数対応表", "検証詳細"]

CLAIM_HEADING = re.compile(r"^###\s+\[F-(\d+)\]\s+(.+?)\s*$")
CHART_ANNOTATION = re.compile(r"^<!--\s*chart:\s*(\S+)\s+(.*?)-->\s*$")
CONFIDENCE_MARKS = ("◎", "○", "△", "×")
ACTION_VERB_HINTS = ("すべき", "しましょう", "推奨する", "実施せよ", "導入する")


def parse_frontmatter(lines: list[str]) -> tuple[dict[str, str], int]:
    """Return (frontmatter mapping, index of first body line)."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    fm: dict[str, str] = {}
    for i, raw in enumerate(lines[1:], start=1):
        if raw.strip() == "---":
            return fm, i + 1
        if ":" in raw:
            key, _, value = raw.partition(":")
            fm[key.strip()] = value.strip().strip('"')
    return fm, len(lines)


def table_header_columns(lines: list[str], start: int) -> list[str] | None:
    """Read the GFM table header starting at or after `start`; None if absent."""
    idx = start
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx + 1 >= len(lines):
        return None
    header, sep = lines[idx], lines[idx + 1]
    if not header.lstrip().startswith("|"):
        return None
    if not re.match(r"^\s*\|[\s:|-]+\|\s*$", sep):
        return None
    return [c.strip() for c in header.strip().strip("|").split("|")]


def check_frontmatter(fm: dict[str, str], errors: list[str]) -> str:
    if not fm:
        errors.append("frontmatter: missing (title and purpose are required)")
        return ""
    if not fm.get("title"):
        errors.append("frontmatter: 'title' is missing or empty")
    purpose = fm.get("purpose", "")
    if purpose not in ("findings", "action"):
        errors.append(
            f"frontmatter: 'purpose' must be 'findings' or 'action' (got: {purpose or 'missing'})"
        )
    return purpose


def check_sections(lines: list[str], body_start: int, errors: list[str]) -> None:
    found = [
        line[3:].strip()
        for line in lines[body_start:]
        if line.startswith("## ")
    ]
    for name in REQUIRED_SECTIONS:
        if name not in found:
            errors.append(f"sections: required section '## {name}' is missing")
    ordered = [s for s in found if s in REQUIRED_SECTIONS]
    if ordered != [s for s in REQUIRED_SECTIONS if s in ordered]:
        errors.append(
            "sections: required sections are out of order "
            f"(found: {' / '.join(ordered)})"
        )
    appendix_subs = section_subheadings(lines, "付録")
    for name in REQUIRED_APPENDIX:
        if name not in appendix_subs:
            errors.append(f"付録: required subsection '### {name}' is missing")


def section_subheadings(lines: list[str], section: str) -> list[str]:
    inside, subs = False, []
    for line in lines:
        if line.startswith("## "):
            inside = line[3:].strip() == section
            continue
        if inside and line.startswith("### "):
            subs.append(line[4:].strip())
    return subs


def claim_blocks(lines: list[str]) -> list[tuple[int, str, str, list[str]]]:
    """Return (line_no, id, message, block_lines) for each claim under 発見事項."""
    blocks, inside, current = [], False, None
    for i, line in enumerate(lines, start=1):
        if line.startswith("## "):
            if current:
                blocks.append(current)
                current = None
            inside = line[3:].strip() == "発見事項"
            continue
        if not inside:
            continue
        m = CLAIM_HEADING.match(line)
        if m:
            if current:
                blocks.append(current)
            current = (i, m.group(1), m.group(2), [])
        elif line.startswith("### "):
            if current:
                blocks.append(current)
                current = None
        elif current:
            current[3].append(line)
    if current:
        blocks.append(current)
    return blocks


def check_claims(lines: list[str], purpose: str, errors: list[str]) -> None:
    blocks = claim_blocks(lines)
    if not blocks:
        errors.append("発見事項: no claim block found (expected '### [F-n] message')")
        return
    interpretation = "実務示唆" if purpose == "action" else "解釈"
    for order, (line_no, cid, message, body) in enumerate(blocks, start=1):
        if int(cid) != order:
            errors.append(
                f"L{line_no}: claim id [F-{cid}] breaks the sequence (expected [F-{order}])"
            )
        if message.endswith("?") or message.endswith("？"):
            errors.append(f"L{line_no}: claim heading must assert, not ask")
        joined = "\n".join(body)
        for field in ("根拠", "検証状態", "確信度"):
            if not re.search(rf"^\s*-\s*{field}\s*[:：]", joined, re.MULTILINE):
                errors.append(f"L{line_no}: claim [F-{cid}] is missing '- {field}:'")
        if not re.search(rf"^\s*-\s*{interpretation}\s*[:：]", joined, re.MULTILINE):
            errors.append(
                f"L{line_no}: claim [F-{cid}] is missing '- {interpretation}:' "
                f"(required for purpose: {purpose})"
            )
        wrong = "解釈" if purpose == "action" else "実務示唆"
        if re.search(rf"^\s*-\s*{wrong}\s*[:：]", joined, re.MULTILINE):
            errors.append(
                f"L{line_no}: claim [F-{cid}] uses '- {wrong}:' which is not allowed "
                f"for purpose: {purpose}"
            )
        conf = re.search(r"^\s*-\s*確信度\s*[:：]\s*(.+)$", joined, re.MULTILINE)
        if conf and not conf.group(1).lstrip().startswith(CONFIDENCE_MARKS):
            errors.append(
                f"L{line_no}: claim [F-{cid}] 確信度 must start with one of ◎ ○ △ ×"
            )
        basis = re.search(r"^\s*-\s*根拠\s*[:：]\s*(.+)$", joined, re.MULTILINE)
        if basis and not re.search(r"n\s*=", basis.group(1)):
            errors.append(f"L{line_no}: claim [F-{cid}] 根拠 has no sample size (n=)")


def check_charts(lines: list[str], errors: list[str]) -> None:
    for i, line in enumerate(lines):
        m = CHART_ANNOTATION.match(line.strip())
        if not m:
            continue
        line_no = i + 1
        kind, params = m.group(1), m.group(2)
        if kind not in ("bar", "line", "pie"):
            errors.append(f"L{line_no}: chart type '{kind}' must be bar, line or pie")
        keys = dict(re.findall(r'(\w+)=("[^"]*"|\S+)', params))
        columns = table_header_columns(lines, i + 1)
        if columns is None:
            errors.append(f"L{line_no}: chart annotation is not followed by a GFM table")
            continue
        for axis in ("x", "y"):
            if axis not in keys:
                errors.append(f"L{line_no}: chart annotation is missing '{axis}='")
                continue
            for col in keys[axis].strip('"').split(","):
                if col.strip() and col.strip() not in columns:
                    errors.append(
                        f"L{line_no}: chart {axis}='{col.strip()}' is not a column "
                        f"of the following table ({', '.join(columns)})"
                    )


def check_findings_neutrality(lines: list[str], purpose: str, errors: list[str]) -> None:
    if purpose != "findings":
        return
    for i, line in enumerate(lines, start=1):
        if line.startswith("### ") and "推奨アクション" in line:
            errors.append(f"L{i}: '推奨アクション' is not allowed with purpose: findings")
        if re.match(r"^\s*-\s*実務示唆\s*[:：]", line):
            errors.append(f"L{i}: '実務示唆' is not allowed with purpose: findings")
    inside = False
    for i, line in enumerate(lines, start=1):
        if line.startswith("## "):
            inside = line[3:].strip() == "エグゼクティブサマリ"
            continue
        if inside and any(hint in line for hint in ACTION_VERB_HINTS):
            errors.append(
                f"L{i}: action-oriented wording in エグゼクティブサマリ is not allowed "
                "with purpose: findings"
            )


def check_phantom_figures(lines: list[str], errors: list[str]) -> None:
    phantom = re.compile(r"(下図|上図|次の図|グラフ参照|図参照|以下のグラフ)")
    for i, line in enumerate(lines, start=1):
        if line.strip().startswith("<!--"):
            continue
        if phantom.search(line):
            errors.append(
                f"L{i}: reference to a figure that the contract cannot generate; "
                "charts come only from an annotated table"
            )


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        # Violation messages quote Japanese section names; the Windows console
        # defaults to cp932 and would mangle them.
        stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        print("usage: validate_contract.py path/to/report.md", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"FAIL: file not found: {path}", file=sys.stderr)
        return 2

    lines = path.read_text(encoding="utf-8").splitlines()
    errors: list[str] = []

    fm, body_start = parse_frontmatter(lines)
    purpose = check_frontmatter(fm, errors)
    check_sections(lines, body_start, errors)
    check_claims(lines, purpose or "findings", errors)
    check_charts(lines, errors)
    check_findings_neutrality(lines, purpose, errors)
    check_phantom_figures(lines, errors)

    if errors:
        print(f"FAIL: {len(errors)} contract violation(s)")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
