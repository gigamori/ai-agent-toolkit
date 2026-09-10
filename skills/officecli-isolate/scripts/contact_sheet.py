# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Render a contact sheet of every slide of a pptx, with no row clipped.

Usage:
    uv run --script contact_sheet.py DECK.pptx [-o OUT.png]

officecli draws a `--grid` contact sheet inside a fixed viewport
(1600 x `--screenshot-height`, default 1200) and clips the rows that do
not fit. `--grid auto` also leaves slides out once the deck is large (a
20-slide deck rendered 12 tiles), and the html renderer scales the
viewport down when either side exceeds 1920 px, which clips the other
axis. All three measured on officecli 1.0.148.

So this script reads the slide count and the slide aspect ratio from the
deck, picks the fewest columns whose rows fit a 1600 x 1920 viewport, and
splits the deck over several sheets with `--page` when even the widest
grid does not fit.

Output: `<deck filename>-contact.png` beside the deck (or `-o PATH`);
when split, `<stem>-1.png`, `<stem>-2.png`, ... One line per sheet:
`<path> <W>x<H> slides=<a>-<b> cols=<C> rows=<R>`, then `OK` (exit 0).
Exit 1 when a sheet's PNG header does not carry the requested size (the
renderer scaled or failed), 2 on usage or officecli errors.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import subprocess
import sys
from pathlib import Path

OFFICECLI = "officecli"
WIDTH = 1600
HEIGHT = 1920
COLUMN_CHOICES = (3, 4, 5, 6)
ROW_GUTTER = 16
SHEET_MARGIN = 32
DEFAULT_ASPECT = 9 / 16
POINTS = re.compile(r"([0-9.]+)\s*pt")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def run_officecli(args: list[str]) -> str:
    proc = subprocess.run(
        [OFFICECLI, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"officecli {' '.join(args[:2])} failed with exit {proc.returncode}")
    return proc.stdout


def slide_count(deck: Path) -> int:
    data = json.loads(run_officecli(["view", str(deck), "stats", "--json"]))
    return int((data.get("data") or {}).get("slides") or 0)


def slide_aspect(deck: Path) -> float:
    data = json.loads(run_officecli(["get", str(deck), "/", "--json"]))
    results = (data.get("data") or {}).get("results") or []
    fmt = (results[0].get("format") or {}) if results else {}
    w = POINTS.match(str(fmt.get("slideWidth", "")))
    h = POINTS.match(str(fmt.get("slideHeight", "")))
    if not w or not h or float(w.group(1)) == 0:
        print("note: slide size not reported; assuming 16:9")
        return DEFAULT_ASPECT
    return float(h.group(1)) / float(w.group(1))


def rows_that_fit(cols: int, aspect: float) -> int:
    tile_height = WIDTH / cols * aspect
    return int((HEIGHT - SHEET_MARGIN) // (tile_height + ROW_GUTTER))


# The fewest columns (largest tiles) whose rows fit one sheet; when none
# do, the widest grid and as many slides per sheet as it holds.
def plan(n: int, aspect: float) -> tuple[int, int]:
    for cols in COLUMN_CHOICES:
        if math.ceil(n / cols) <= rows_that_fit(cols, aspect):
            return cols, n
    cols = COLUMN_CHOICES[-1]
    return cols, cols * rows_that_fit(cols, aspect)


def png_size(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or not head.startswith(PNG_SIGNATURE):
        return None
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def render(deck: Path, out: Path, cols: int, first: int, last: int, paged: bool) -> None:
    args = [
        "view", str(deck), "screenshot",
        "--grid", str(cols),
        "--screenshot-height", str(HEIGHT),
        "-o", str(out),
    ]
    if paged:
        args += ["--page", f"{first}-{last}"]
    run_officecli(args)


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("deck")
    ap.add_argument("-o", "--out")
    args = ap.parse_args()

    deck = Path(args.deck)
    if not deck.is_file():
        print(f"error: no such file: {deck}")
        return EXIT_USAGE
    out = Path(args.out) if args.out else deck.with_name(deck.name + "-contact.png")

    n = slide_count(deck)
    if n == 0:
        print("error: the deck has no slides")
        return EXIT_USAGE
    aspect = slide_aspect(deck)
    cols, per_sheet = plan(n, aspect)
    sheets = math.ceil(n / per_sheet)

    failures = 0
    for i in range(sheets):
        first = i * per_sheet + 1
        last = min(n, (i + 1) * per_sheet)
        target = out if sheets == 1 else out.with_name(f"{out.stem}-{i + 1}{out.suffix}")
        render(deck, target, cols, first, last, paged=sheets > 1)
        size = png_size(target)
        rows = math.ceil((last - first + 1) / cols)
        shown = f"{size[0]}x{size[1]}" if size else "unreadable"
        print(f"{target} {shown} slides={first}-{last} cols={cols} rows={rows}")
        if size != (WIDTH, HEIGHT):
            failures += 1
            print(f"  expected {WIDTH}x{HEIGHT}; the renderer scaled or failed")

    if failures:
        print(f"FAIL: {failures} sheet(s) not at the requested size")
        return EXIT_FAIL
    print("OK")
    return EXIT_OK


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
