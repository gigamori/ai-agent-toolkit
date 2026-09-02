# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Check a rendered pptx against the report it was built from.

Usage:
    uv run --script check_pptx_text.py path/to/deck.pptx path/to/report.md [--scope F-6,F-7]

The deck is authored freely from the report, so wording is not compared.
Two checks decide the verdict:

  claim coverage    every claim id in scope appears as the literal F-n
  confidence mark   for every claim id in scope, at least one slide carries
                    both F-n and the mark from its '- 確信度:' line

A third result is a LISTING, not a verdict: every numeric token on the slides
that occurs nowhere in report.md, one line per (slide, token, line), with a
line that is exactly the slide's own number left out. The script cannot tell
a decorative divider number from a derived data value, so no rule is applied;
the listing is printed on every run for the LLM fidelity judge to decide.

Slide text comes from `officecli view <pptx> text`, which does not expose
native charts; chart title, categories and series values are pulled from
`officecli query <pptx> chart --json` so the listing covers them too.

Scope defaults to every `### [F-n]` claim in the report; `--scope F-6,F-7`
restricts it. stdout: the listing under `numbers not in the report: N`, then
the verdict last -- `OK` (exit 0) or `FAIL: N violation(s)` plus one line per
violation (exit 1). Exit 2 on usage or IO errors (including a scope id the
report does not have).
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

SLIDE_MARKER = re.compile(r"^===\s*/slide\[(\d+)\]\s*===$")
NUMBER_TOKEN = re.compile(r"[0-9][0-9,.]*")
CHIP = re.compile(r"(?<![A-Za-z0-9])F-(\d+)(?!\d)")
SCOPE_ID = re.compile(r"^F-(\d+)$")
CONFIDENCE_LINE = re.compile(r"^\s*-\s*確信度\s*[:：]\s*(\S)", re.MULTILINE)
SLIDE_IN_PATH = re.compile(r"/slide\[(\d+)\]")
SERIES_KEY = re.compile(r"^series\d+$")
CHART_LIST_SEP = " / "
# The node path carries a numeric id (chart[@id=100000]) that would be
# tokenized as a number, so chart lines are labelled without it.
CHART_LABEL = "chart"
SCOPE_FLAG = "--scope"
LISTING_HEADER = "numbers not in the report"
USAGE = "usage: check_pptx_text.py path/to/deck.pptx path/to/report.md [--scope F-6,F-7]"


class UsageError(Exception):
    """Input that the check cannot be run against (bad scope, malformed report)."""


def load_contract_module():
    path = Path(__file__).resolve().parent / "validate_contract.py"
    spec = importlib.util.spec_from_file_location("validate_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = load_contract_module()


def normalize(text: str) -> str:
    return " ".join(text.split())


def numeric_tokens(text: str) -> set[str]:
    out = set()
    for raw in NUMBER_TOKEN.findall(text):
        token = raw.replace(",", "").rstrip(".")
        if token:
            out.add(token)
    return out


def chip_ids(text: str) -> set[str]:
    return set(CHIP.findall(text))


def rendered_lines(output: str) -> list[tuple[int, str]]:
    """Return (slide number, line) for every non-empty line of `view text`."""
    out: list[tuple[int, str]] = []
    slide = 0
    for raw in output.splitlines():
        marker = SLIDE_MARKER.match(raw.strip())
        if marker:
            slide = int(marker.group(1))
            continue
        text = normalize(raw)
        if text:
            out.append((slide, text))
    return out


def chart_lines(query_json: str) -> list[tuple[int, str]]:
    """Return (slide number, line) for the title, categories and series of every
    native chart in `query <pptx> chart --json` output.

    Categories and values arrive comma-joined ("5.665,5.064,4.076"); they are
    split so the number tokenizer sees each item on its own. The chart is
    located by slide number only.
    """
    results = json.loads(query_json).get("data", {}).get("results", [])
    out: list[tuple[int, str]] = []
    for node in results:
        if node.get("type") != "chart":
            continue
        found = SLIDE_IN_PATH.search(node.get("path", ""))
        slide = int(found.group(1)) if found else 0
        label = CHART_LABEL
        fmt = node.get("format", {})

        title = normalize(str(fmt.get("title", "")))
        if title:
            out.append((slide, f"{label} title: {title}"))
        categories = split_list(fmt.get("categories"))
        if categories:
            out.append((slide, f"{label} categories: {CHART_LIST_SEP.join(categories)}"))

        # Series come as child nodes when the query expands them; older or
        # shallower output only carries the "seriesN": "name:v1,v2" strings.
        series = [(c.get("format", {}).get("name", ""), c.get("format", {}).get("values"))
                  for c in node.get("children", []) if c.get("type") == "series"]
        if not series:
            for key, value in fmt.items():
                if SERIES_KEY.match(key) and isinstance(value, str):
                    name, _, values = value.partition(":")
                    series.append((name, values))
        for name, values in series:
            items = split_list(values)
            if normalize(str(name)):
                out.append((slide, f"{label} series: {normalize(str(name))}"))
            if items:
                out.append((slide, f"{label} values: {CHART_LIST_SEP.join(items)}"))
    return out


def split_list(value) -> list[str]:
    if value is None:
        return []
    return [normalize(item) for item in str(value).split(",") if normalize(item)]


def claim_marks(report_lines: list[str]) -> dict[str, str]:
    """Map claim id ("1") to its confidence mark, in report order."""
    marks: dict[str, str] = {}
    for _line_no, cid, _message, body in contract.claim_blocks(report_lines):
        found = CONFIDENCE_LINE.search("\n".join(body))
        mark = found.group(1) if found else ""
        if mark not in contract.CONFIDENCE_MARKS:
            raise UsageError(
                f"report: claim [F-{cid}] has no confidence mark on its '- 確信度:' line")
        marks[cid] = mark
    return marks


def parse_scope(arg: str) -> list[str]:
    ids = []
    for item in arg.split(","):
        found = SCOPE_ID.match(item.strip())
        if not found:
            raise UsageError(f"scope: '{item.strip()}' is not of the form F-n")
        ids.append(found.group(1))
    return ids


def slide_lines(output: str, chart_json: str | None = None) -> list[tuple[int, str]]:
    lines = rendered_lines(output)
    if chart_json is not None:
        lines += chart_lines(chart_json)
    return lines


def unknown_numbers(output: str, report_lines: list[str],
                    chart_json: str | None = None) -> list[str]:
    """Return one line per numeric token on the slides that the report never
    states. A listing for the judge, not a verdict."""
    allowed = numeric_tokens("\n".join(report_lines))
    listing = []
    for slide, text in slide_lines(output, chart_json):
        if text == str(slide):
            continue
        for token in sorted(numeric_tokens(text)):
            if token not in allowed:
                listing.append(f"slide {slide}: '{token}': {text}")
    return listing


def check(output: str, report_lines: list[str], scope: list[str] | None = None,
          chart_json: str | None = None) -> list[str]:
    """Return one line per claim-coverage / confidence-mark violation; empty
    when the deck holds."""
    marks = claim_marks(report_lines)
    if scope is None:
        scope = list(marks)
    missing = [cid for cid in scope if cid not in marks]
    if missing:
        raise UsageError("scope: not in the report: " + ", ".join(f"F-{c}" for c in missing))

    per_slide: dict[int, list[str]] = {}
    for slide, text in slide_lines(output, chart_json):
        per_slide.setdefault(slide, []).append(text)
    slide_text = {slide: "\n".join(texts) for slide, texts in per_slide.items()}
    slide_chips = {slide: chip_ids(text) for slide, text in slide_text.items()}
    seen = set().union(*slide_chips.values()) if slide_chips else set()

    violations = []
    for cid in scope:
        if cid not in seen:
            violations.append(f"F-{cid}: not on any slide")
            continue
        mark = marks[cid]
        if not any(cid in chips and mark in slide_text[slide]
                   for slide, chips in slide_chips.items()):
            violations.append(
                f"F-{cid}: no slide carries both the chip and its confidence mark '{mark}'")
    return violations


def run_officecli(args: list[str]) -> str:
    result = subprocess.run(
        ["officecli", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"officecli {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    if "�" in result.stdout:
        print(f"WARNING: officecli {args[0]} output contained undecodable bytes", file=sys.stderr)
    return result.stdout


def parse_args(argv: list[str]) -> tuple[Path, Path, list[str] | None]:
    positional, scope = [], None
    i = 0
    while i < len(argv):
        if argv[i] == SCOPE_FLAG:
            if i + 1 >= len(argv):
                raise UsageError(USAGE)
            scope = parse_scope(argv[i + 1])
            i += 2
            continue
        positional.append(argv[i])
        i += 1
    if len(positional) != 2:
        raise UsageError(USAGE)
    return Path(positional[0]), Path(positional[1]), scope


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        pptx, report, scope = parse_args(sys.argv[1:])
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for path in (pptx, report):
        if not path.is_file():
            print(f"FAIL: file not found: {path}", file=sys.stderr)
            return 2

    try:
        output = run_officecli(["view", str(pptx), "text"])
        chart_json = run_officecli(["query", str(pptx), "chart", "--json"])
    except (OSError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    report_lines = report.read_text(encoding="utf-8").splitlines()
    try:
        violations = check(output, report_lines, scope, chart_json)
    except (UsageError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    listing = unknown_numbers(output, report_lines, chart_json)
    print(f"{LISTING_HEADER}: {len(listing)}")
    for line in listing:
        print(f"  - {line}")

    if violations:
        print(f"FAIL: {len(violations)} violation(s)")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
