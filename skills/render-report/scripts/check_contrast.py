# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Check text/background contrast ratios against the WCAG 2.x thresholds.

Usage:
    uv run --script check_contrast.py "LABEL:FG:BG[:SIZE[:bold]]" ...

Each argument describes one text element:
    LABEL  free-form name used in the report (e.g. slide6/body)
    FG     text color as #RGB or #RRGGBB
    BG     background color the text sits on, same format
    SIZE   font size in pt (optional, default 14)
    bold   literal "bold" when the text is bold (optional)

Threshold: 3.0 for large text (>=24pt, or >=18.66pt bold), otherwise 4.5.
Exit code 0 when every element passes, 1 when any fails.
"""

from __future__ import annotations

import sys

LARGE_PT = 24.0
LARGE_BOLD_PT = 18.66
THRESHOLD_LARGE = 3.0
THRESHOLD_NORMAL = 4.5


def parse_hex(value: str) -> tuple[int, int, int]:
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        raise ValueError(f"not a hex color: {value}")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float:
    l1, l2 = relative_luminance(parse_hex(fg)), relative_luminance(parse_hex(bg))
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def threshold_for(size_pt: float, bold: bool) -> float:
    if size_pt >= LARGE_PT or (bold and size_pt >= LARGE_BOLD_PT):
        return THRESHOLD_LARGE
    return THRESHOLD_NORMAL


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        print('usage: check_contrast.py "LABEL:FG:BG[:SIZE[:bold]]" ...', file=sys.stderr)
        return 2

    failures = 0
    for spec in sys.argv[1:]:
        parts = spec.split(":")
        if len(parts) < 3:
            print(f"FAIL {spec}: expected LABEL:FG:BG[:SIZE[:bold]]", file=sys.stderr)
            failures += 1
            continue
        label, fg, bg = parts[0], parts[1], parts[2]
        try:
            size = float(parts[3]) if len(parts) > 3 and parts[3] else 14.0
        except ValueError:
            print(f"FAIL {label}: size '{parts[3]}' is not a number", file=sys.stderr)
            failures += 1
            continue
        bold = len(parts) > 4 and parts[4].strip().lower() == "bold"
        try:
            ratio = contrast_ratio(fg, bg)
        except ValueError as e:
            print(f"FAIL {label}: {e}", file=sys.stderr)
            failures += 1
            continue
        need = threshold_for(size, bold)
        verdict = "OK  " if ratio >= need else "FAIL"
        if ratio < need:
            failures += 1
        print(
            f"{verdict} {label}: {fg} on {bg} = {ratio:.2f}:1 "
            f"(needs {need}:1 at {size:g}pt{' bold' if bold else ''})"
        )

    if failures:
        print(f"\n{failures} element(s) below the contrast threshold")
        return 1
    print("\nOK: all elements meet the contrast threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
