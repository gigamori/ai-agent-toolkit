# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Check that every string on the rendered pptx comes from the deck-spec.

Usage:
    uv run --script check_pptx_text.py path/to/deck.pptx path/to/deck-spec.md

Runs `officecli view <pptx> text` and asserts that each rendered line is a
deck-spec string, a layout label named in deck-archetypes.md, or a slide
number. Exit code 0 and "OK" when it holds, 1 and one line per offending
string otherwise.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

LAYOUT_LABELS = ("主要な発見", "主な限界", "根拠")
SLIDE_MARKER = re.compile(r"^===\s*/slide\[(\d+)\]\s*===$")
FRONTMATTER_KEYS = ("title", "source")


def normalize(text: str) -> str:
    return " ".join(text.split())


def deck_strings(lines: list[str]) -> tuple[set[str], set[str]]:
    """Return (renderable strings, trace values) taken from a deck-spec."""
    strings: set[str] = set()
    traces: set[str] = set()
    in_frontmatter = bool(lines) and lines[0].strip() == "---"

    for i, raw in enumerate(lines):
        line = raw.strip()
        if in_frontmatter:
            if i == 0:
                continue
            if line == "---":
                in_frontmatter = False
                continue
            key, _, value = line.partition(":")
            if key.strip() in FRONTMATTER_KEYS:
                strings.add(normalize(value.strip().strip('"')))
            continue

        if line.startswith("<!--"):
            chart_title = re.search(r'title="([^"]*)"', line)
            if chart_title:
                strings.add(normalize(chart_title.group(1)))
            continue
        if not line or line.startswith("## "):
            continue
        if line.startswith("trace:"):
            traces.add(normalize(line[len("trace:"):]))
            continue
        if line.startswith("|"):
            for cell in line.strip("|").split("|"):
                if normalize(cell):
                    strings.add(normalize(cell))
            continue
        slot = re.match(r"^-\s+([A-Za-z][A-Za-z-]*)\s*[:：]\s*(.*)$", line)
        strings.add(normalize(slot.group(2) if slot else line))

    strings.discard("")
    return strings, traces


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


def allowed(text: str, slide: int, strings: set[str], traces: set[str]) -> bool:
    if text in strings or text in traces:
        return True
    if text == str(slide):
        return True
    if text in LAYOUT_LABELS:
        return True
    for trace in traces:
        for label in LAYOUT_LABELS:
            if text == f"{trace} {label}":
                return True
    return False


def check(output: str, deck_lines: list[str]) -> list[str]:
    strings, traces = deck_strings(deck_lines)
    violations = []
    for slide, text in rendered_lines(output):
        if not allowed(text, slide, strings, traces):
            violations.append(f"slide {slide}: rendered text is not a deck-spec string: {text}")
    return violations


def run_officecli(pptx: Path) -> str:
    result = subprocess.run(
        ["officecli", "view", str(pptx), "text"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"officecli view failed ({result.returncode}): {result.stderr.strip()}")
    if "�" in result.stdout:
        print("WARNING: officecli output contained undecodable bytes", file=sys.stderr)
    return result.stdout


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 3:
        print("usage: check_pptx_text.py path/to/deck.pptx path/to/deck-spec.md", file=sys.stderr)
        return 2
    pptx, deck = Path(sys.argv[1]), Path(sys.argv[2])
    for path in (pptx, deck):
        if not path.is_file():
            print(f"FAIL: file not found: {path}", file=sys.stderr)
            return 2

    try:
        output = run_officecli(pptx)
    except (OSError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    violations = check(output, deck.read_text(encoding="utf-8").splitlines())
    if violations:
        print(f"FAIL: {len(violations)} rendered string(s) not in the deck-spec")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
