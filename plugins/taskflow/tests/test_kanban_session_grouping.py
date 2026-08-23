#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "markdown", "nh3"]
# ///
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
GK_PATH = REPO_ROOT / "plugins" / "taskflow" / "scripts" / "generate_kanban.py"

spec = importlib.util.spec_from_file_location("gk", str(GK_PATH))
gk = importlib.util.module_from_spec(spec)
sys.modules["gk"] = gk
spec.loader.exec_module(gk)

PASS = 0
FAIL = 0


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {msg}")
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")


S = gk.SessionRef


def card(sessions) -> str:
    task = gk.Task(status="1_in_progress", h1="T", priority="MID", project="p",
                   created="", updated="", file_path="", sessions=sessions)
    return gk.render_card(task, "#000", "vscode")


def main() -> int:
    print("=== generate_kanban.py session grouping (G1-G6) ===")

    print("--- label counts sessions, not entries (4 entries / 2 sessions) ---")
    ss = [S("2026-08-19", "aaaaaaaa", "first round", "U-A"),
          S("2026-08-20", "aaaaaaaa", "second round", "U-A"),
          S("2026-08-20", "aaaaaaaa", "third round", "U-A"),
          S("2026-08-18", "bbbbbbbb", "other session", "U-B")]
    html = card(ss)
    check('▸ 2 sessions</span>' in html, "label reads `▸ 2 sessions`")
    check('4 sessions</span>' not in html, "label does NOT read `4 sessions`")

    print("--- singular when every entry is one session (3 entries / 1 session) ---")
    one = [S("2026-08-18", "cccccccc", "r one", "U-C"),
           S("2026-08-19", "cccccccc", "r two", "U-C"),
           S("2026-08-20", "cccccccc", "r three", "U-C")]
    h1 = card(one)
    check('▸ 1 session</span>' in h1, "label reads `▸ 1 session` (singular)")
    check('3 sessions</span>' not in h1, "label does NOT read `3 sessions`")

    print("--- no entry text is dropped ---")
    check(all(t in html for t in ("first round", "second round", "third round", "other session")),
          "all 4 summaries appear in the mixed card")
    check(all(t in h1 for t in ("r one", "r two", "r three")),
          "all 3 summaries appear in the single-session card")

    print("--- +N badge is len(group)-1 ---")
    check('<span class="more-badge">+2</span>' in html,
          "3-entry group shows +2 (not +3)")
    check('<span class="more-badge">+2</span>' in h1,
          "3-entry single-session group shows +2")
    check("+3</span>" not in html and "+3</span>" not in h1,
          "no off-by-one +3 badge anywhere")

    print("--- nesting only for multi-entry groups ---")
    check(html.count('<details class="sgroup">') == 1,
          "mixed card nests exactly one sgroup (the 3-entry group)")
    lone = card([S("2026-08-20", "dddddddd", "solo entry", "U-D")])
    check('<details class="sgroup">' not in lone,
          "single-entry group nests no sgroup")
    check('more-badge' not in lone, "single-entry group carries no badge")
    check('▸ 1 session</span>' in lone, "its label reads `▸ 1 session`")

    print("--- group count == dedup count (label invariant, both helpers) ---")
    for name, fixture in (("mixed", ss), ("single-session", one),
                          ("no-uuid key fallback",
                           [S("2026-08-19", "eeeeeeee", "x", ""),
                            S("2026-08-20", "eeeeeeee", "y", ""),
                            S("2026-08-20", "ffffffff", "z", "U-F")])):
        g, d = gk.group_sessions(fixture), gk.dedup_sessions(fixture)
        check(len(g) == len(d),
              f"{name}: len(group_sessions)={len(g)} == len(dedup_sessions)={len(d)}")
        check(sum(len(x) for x in g) == len(fixture),
              f"{name}: grouping keeps every entry ({len(fixture)})")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
