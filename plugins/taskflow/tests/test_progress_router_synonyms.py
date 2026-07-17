#!/usr/bin/env python3
"""Static text-contract test for agents/progress-router.md (P5, F6) and its
single-source pointer from skills/progress/SKILL.md Step 3.4.

Covers project-notes/specs/review-2026-07-17-fixes.md P5:
  (a) `approve` synonyms no longer include `ok` (was colliding with
      substrings of "tokyo" / "look" under the old substring-match rule).
  (b) English tokens match on a word boundary via the negative-lookaround
      `(?<![A-Za-z])T(?![A-Za-z])`; Japanese tokens still match as a
      substring (no whitespace word delimiter).
  (c) the multi-match tie-break resolves to the sentence's main verb, with
      two hardcoded worked examples («着手を取り消して» / "revert the
      start") and an `unknown` fallback when undecidable.
  (d) the tie-break paragraph does NOT reassert the OLD "earliest matched
      token" rule as authoritative — it explicitly negates it
      ("NOT the earliest-appearing token") rather than merely omitting the
      word "earliest" (which also appears legitimately in the new rule's own
      negation, so bare-absence of "earliest" would be a false-fail).
  (e) SKILL.md Step 3.4 still points at progress-router.md's Step 1 body as
      the single source of truth (does not re-embed its own synonym table).

Read-only / no model call: this test only reads the two markdown files and
greps their text. Run with:

    uv run python plugins/taskflow/tests/test_progress_router_synonyms.py

Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PASS = 0
FAIL = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS: {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL: {msg}")


def check(cond: bool, msg: str) -> None:
    ok(msg) if cond else bad(msg)


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
ROUTER_MD = PLUGIN_ROOT / "agents" / "progress-router.md"
SKILL_MD = PLUGIN_ROOT / "skills" / "progress" / "SKILL.md"


def test_files_exist() -> tuple[str, str] | None:
    print("--- fixture: source files exist ---")
    check(ROUTER_MD.is_file(), f"{ROUTER_MD} exists")
    check(SKILL_MD.is_file(), f"{SKILL_MD} exists")
    if not (ROUTER_MD.is_file() and SKILL_MD.is_file()):
        return None
    return (ROUTER_MD.read_text(encoding="utf-8"),
            SKILL_MD.read_text(encoding="utf-8"))


def test_a_no_ok_synonym(router: str) -> None:
    print("--- (a) approve synonyms no longer list `ok` ---")
    m = re.search(r"^\|\s*`approve`\s*\|(?P<cells>.*)\|\s*$", router, re.MULTILINE)
    check(m is not None, "approve synonym table row is present")
    if not m:
        return
    cells_text = m.group("cells")
    tokens = [t.strip().strip("`") for t in cells_text.split(",")]
    check(len(tokens) >= 1 and all(tokens), f"approve row parses into synonym tokens: {tokens}")
    check("ok" not in [t.lower() for t in tokens],
          f"'ok' is not among the approve synonym tokens (got {tokens})")
    # Positive signal: the row still has its legitimate current synonyms.
    for expect in ("approve", "done", "finish"):
        check(expect in [t.lower() for t in tokens],
              f"approve row still contains legitimate synonym '{expect}'")


def test_b_word_boundary_rule(router: str) -> None:
    print("--- (b) English word-boundary rule / Japanese substring rule ---")
    check("(?<![A-Za-z])T(?![A-Za-z])" in router,
          "literal negative-lookaround word-boundary pattern is present")
    check(re.search(r"word boundary", router, re.IGNORECASE) is not None,
          "text describes matching 'on a word boundary'")
    check("Substring matching of English tokens is forbidden." in router,
          "text explicitly forbids substring matching for English tokens")
    jp_section = re.search(
        r"\*\*Japanese tokens\*\*.*?substring", router, re.DOTALL)
    check(jp_section is not None,
          "text describes Japanese tokens matching as a substring")


def test_c_main_verb_tiebreak(router: str) -> None:
    print("--- (c) main-verb tie-break with 2 hardcoded examples + unknown fallback ---")
    check(re.search(r"main verb", router, re.IGNORECASE) is not None,
          "tie-break rule invokes 'main verb' of the sentence")
    check("「着手を取り消して」" in router,
          "worked example 1 (「着手を取り消して」 → revert) is present")
    check('"revert the start"' in router,
          "worked example 2 (\"revert the start\" → revert) is present")
    check(re.search(r'cannot decide.*action:\s*"unknown"', router, re.DOTALL) is not None
          or re.search(r'action:\s*"unknown".*cannot decide', router, re.DOTALL) is not None,
          "undecidable tie-break explicitly falls back to action: \"unknown\"")


def _tiebreak_paragraph(router: str) -> str:
    m = re.search(
        r"If synonyms from more than one action set match.*?(?=\n- If no synonym matches)",
        router, re.DOTALL)
    return m.group(0) if m else ""


def test_d_no_old_priority_order_reassertion(router: str) -> None:
    print("--- (d) tie-break does not reassert an old fixed priority-order rule ---")
    para = _tiebreak_paragraph(router)
    check(bool(para), "the multi-match tie-break paragraph is locatable")
    if not para:
        return
    # Positive signal: the new rule explicitly NEGATES the old "earliest
    # token" rule (not just omits the word "earliest" — the instructions
    # forbid a bare-absence check since the negation itself legitimately
    # contains the word).
    check("NOT the earliest-appearing token" in para,
          "paragraph explicitly negates the old earliest-appearing-token rule")
    check(re.search(r"main verb", para, re.IGNORECASE) is not None,
          "paragraph's actual rule is main-verb based")
    # The old rule shape would have been a fixed priority ordering of the
    # action names themselves (e.g. "approve > revert > start" or a numbered
    # priority list of actions) as the tie-break authority. That shape must
    # not be present in THIS paragraph (Step 2's target-phrase match-priority
    # table elsewhere in the doc is unrelated and out of scope here).
    check(re.search(r"priority order", para, re.IGNORECASE) is None,
          "paragraph does not reassert a 'priority order' tie-break")
    check(re.search(r"approve\s*[>→]\s*revert", para, re.IGNORECASE) is None,
          "paragraph does not hardcode a fixed action-priority ordering")


def test_e_skill_single_source_pointer(router: str, skill: str) -> None:
    print("--- (e) SKILL.md Step 3.4 points to progress-router.md (single-source) ---")
    check("agents/progress-router.md" in skill,
          "SKILL.md references agents/progress-router.md")
    check(re.search(r"Step 1 of the body of", skill) is not None,
          "SKILL.md points at the router's Step 1 body specifically")
    check(re.search(r"main verb|main-verb", skill, re.IGNORECASE) is not None,
          "SKILL.md's fallback description mentions the main-verb tie-break "
          "(consistent with the router, not a stale earliest-token summary)")
    # Single-source: SKILL.md must NOT re-embed the router's own synonym
    # table (its distinctive header row).
    check("| Action | Synonyms |" not in skill,
          "SKILL.md does not duplicate the router's synonym table")
    check("| Action | Synonyms |" in router,
          "sanity: the synonym table header does live in progress-router.md")


def main() -> int:
    print("=== progress-router.md / SKILL.md synonym text-contract tests (P5, F6) ===")
    texts = test_files_exist()
    if texts is None:
        print()
        print(f"{FAIL} failed, {PASS} passed. (source files missing — aborted)")
        return 1
    router, skill = texts
    test_a_no_ok_synonym(router)
    test_b_word_boundary_rule(router)
    test_c_main_verb_tiebreak(router)
    test_d_no_old_priority_order_reassertion(router)
    test_e_skill_single_source_pointer(router, skill)

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
