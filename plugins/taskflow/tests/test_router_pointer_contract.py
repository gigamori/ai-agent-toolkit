#!/usr/bin/env python3
"""Static contract check: project-router stays pointer-only for project-notes.

Regression guard (spec router-notes-fidelity-fix.md §3.5 / §6). The router must
emit a pointer field (`project_notes_relevant`) for project-notes and must NEVER
emit a note BODY field. The canonical body-leak sentinel is a field named
`project_notes_content`: if it ever appears in the Step 6 apply template, a future
edit has re-introduced note bodies into the router result and defeated context
isolation.

This is a minimal static assert on the agent definition text — not a fidelity
evaluation (that is a separate cycle, spec §6). It needs no claude CLI; run it
with:  uv run python plugins/taskflow/tests/test_router_pointer_contract.py

Exits 0 when the contract holds, 1 otherwise.
"""
import re
import sys
from pathlib import Path

ROUTER = Path(__file__).resolve().parent.parent / "agents" / "project-router.md"

POINTER_FIELD = "project_notes_relevant"
BODY_LEAK_FIELD = "project_notes_content"

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print(f"  PASS: {msg}")


def bad(msg):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {msg}")


def extract_apply_template(text):
    """Return the fenced code block under the '### For apply' heading."""
    # Slice from the '### For apply' heading to the next '###'/'##' heading.
    m = re.search(r"^###\s+For apply\s*$", text, re.MULTILINE)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r"^#{2,3}\s+\S", rest, re.MULTILINE)
    section = rest[: nxt.start()] if nxt else rest
    fence = re.search(r"```.*?\n(.*?)```", section, re.DOTALL)
    return fence.group(1) if fence else None


def main():
    print("=== Static contract: project-router pointer-only ===")
    if not ROUTER.is_file():
        bad(f"router definition not found: {ROUTER}")
        return 1

    text = ROUTER.read_text(encoding="utf-8")
    template = extract_apply_template(text)

    if template is None:
        bad("could not locate the '### For apply' template block")
        return 1
    ok("located '### For apply' template block")

    if POINTER_FIELD in template:
        ok(f"apply template contains pointer field '{POINTER_FIELD}'")
    else:
        bad(f"apply template missing pointer field '{POINTER_FIELD}'")

    if BODY_LEAK_FIELD in template:
        bad(f"apply template leaks note body field '{BODY_LEAK_FIELD}'")
    else:
        ok(f"apply template has no note body field '{BODY_LEAK_FIELD}'")

    # The body-leak sentinel must not appear anywhere in the definition.
    if BODY_LEAK_FIELD in text:
        bad(f"'{BODY_LEAK_FIELD}' appears in the router definition (body leak)")
    else:
        ok(f"'{BODY_LEAK_FIELD}' absent from the entire router definition")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
