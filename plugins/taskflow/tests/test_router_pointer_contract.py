#!/usr/bin/env python3
"""Static contract check: project-router stays pointer-only for project-notes.

Regression guard (spec router-notes-fidelity-fix.md §3.5 / §6, extended by
router-context-payload-cap.md §5/§6). The router must emit a pointer field
(`project_notes_relevant`) for project-notes and must NEVER emit a note BODY
field. The canonical body-leak sentinel is a field named `project_notes_content`:
if it ever appears in the Step 6 apply template, a future edit has
re-introduced note bodies into the router result and defeated context
isolation.

router-context-payload-cap.md additionally retires the unbounded
`project_notes_list` / `tasks_todo_list` / `tasks_in_progress_list` fields (they
listed every path with no population cap — the context-bloat defect that spec
fixes) in favor of a code-bounded `project_notes_summary` (counts only). This
file also guards that retirement, plus two prose invariants from the same
spec's M1/M2 review findings: the router must never improvise a directory walk
when `--notes-summary` fails (M1), and the "verbatim vs. bounded-population"
fidelity split must state that population size is code-only (M2/D2').

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
NOTES_SUMMARY_FIELD = "project_notes_summary"
RETIRED_UNBOUNDED_FIELDS = (
    "project_notes_list",
    "tasks_todo_list",
    "tasks_in_progress_list",
)
NO_FALLBACK_WALK_PHRASE = "MUST NOT substitute a directory walk"
# Whitespace-normalized (space-joined) rather than pinned to the source's exact
# line-wrap: a harmless markdown reflow (same words, different wrap column)
# must not false-FAIL this contract check.
BOUNDED_POPULATION_PHRASE = "decided exclusively by a deterministic script"
RELEVANT_EXEMPTION_PHRASE = "Explicit exemption from (ii)"


def _normalize_ws(s: str) -> str:
    return " ".join(s.split())

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

    # router-context-payload-cap.md: the code-bounded summary field must be present...
    if NOTES_SUMMARY_FIELD in template:
        ok(f"apply template contains bounded field '{NOTES_SUMMARY_FIELD}'")
    else:
        bad(f"apply template missing bounded field '{NOTES_SUMMARY_FIELD}'")

    # ...and the three retired unbounded list fields must be gone, template and whole-file both
    # (whole-file catches a stray mention left in prose, e.g. an un-updated Step 4a/4b reference).
    for field in RETIRED_UNBOUNDED_FIELDS:
        if field in template:
            bad(f"apply template still emits retired unbounded field '{field}'")
        else:
            ok(f"apply template has no retired unbounded field '{field}'")
        if field in text:
            bad(f"'{field}' still appears somewhere in the router definition")
        else:
            ok(f"'{field}' absent from the entire router definition")

    # M1: no directory-walk fallback when --notes-summary fails.
    if NO_FALLBACK_WALK_PHRASE in text:
        ok("router definition prohibits a directory-walk fallback for the notes summary (M1)")
    else:
        bad("router definition is missing the directory-walk fallback prohibition (M1)")

    # D2'/M2: the verbatim/bounded-population split states population size is code-only.
    # Whitespace-normalized comparison (see BOUNDED_POPULATION_PHRASE) so a harmless
    # markdown reflow of this sentence cannot false-FAIL the check.
    if BOUNDED_POPULATION_PHRASE in _normalize_ws(text):
        ok("Output fidelity section states population size is decided by code, not the LLM (D2')")
    else:
        bad("Output fidelity section is missing the code-only bounded-population statement (D2')")

    # 4.1: tasks_in_progress_relevant / project_notes_relevant are explicitly exempt from the cap.
    if RELEVANT_EXEMPTION_PHRASE in text:
        ok("Output fidelity section explicitly exempts the *_relevant fields from population cap")
    else:
        bad("Output fidelity section is missing the *_relevant exemption statement")

    # State-write sentinels: router must not contain a shell redirect to state_file.
    if "> <state_file>" in text:
        bad("router definition contains '> <state_file>' (state write must be removed)")
    else:
        ok("'> <state_file>' absent from the router definition")

    # State-write sentinels: router must not contain the 'Write the finalized project name' instruction.
    if "Write the finalized project name" in text:
        bad("router definition contains 'Write the finalized project name' (state write must be removed)")
    else:
        ok("'Write the finalized project name' absent from the router definition")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
