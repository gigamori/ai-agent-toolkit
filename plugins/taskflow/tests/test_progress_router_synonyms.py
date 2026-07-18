#!/usr/bin/env python3
"""Static text-contract test for agents/progress-router.md (state-goal model)
and its single-source pointer from skills/progress/SKILL.md Step 3.4.

Covers the 2026-07-19 redesign (task
2026-07-18_progress-revert-collides-with-revert-skill-gate): the router no
longer claims any undo/revert vocabulary (that belongs to the global `revert`
skill, whose UserPromptSubmit hook force-routes 戻す/undo/revert inputs), and
instead resolves the GOAL STATE the user names:

  (a) the goal-state table maps 2_done→approve / 1_in_progress→start /
      0_todo→unstart with the expected synonyms, and NO synonym row contains
      the released vocabulary (revert / 戻す / 戻し / undo / 取り消し) or the
      historical `ok` collision token.
  (b) English tokens still match on a word boundary only (negative-lookaround
      `(?<![A-Za-z])T(?![A-Za-z])`); Japanese tokens still match as a
      substring.
  (c) maximal munch: overlapping Japanese tokens resolve to the longest
      occurrence (未着手 / 着手前 suppress the contained 着手).
  (d) path exclusion: synonyms inside path-like tokens (e.g. `@tasks/0_todo/`)
      do not count — required for the `todo` English token to be safe.
  (e) undo-intent gate: a sentence-level semantic judgment (NOT string
      matching) short-circuits undo/cancel requests to a fixed unknown
      terminal BEFORE any state match; example words are illustrative, and
      content occurrences (戻り値, stems containing "revert") do not fire it.
  (f) tie-break: multi-state matches resolve to the reach-state (NOT the state
      being left); goal-state tokens beat maintenance tokens; undecidable →
      "unknown". The old main-verb rule is gone.
  (g) the JSON contract's action enum is approve|start|unstart|... with no
      revert, and target_status is always the goal state.
  (h) SKILL.md Step 3.4 still points at progress-router.md's Step 1 body as
      the single source (no re-embedded table), dispatches `unstart` (not
      `revert`), and its usage examples use state words.

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

RELEASED_VOCAB = ("revert", "戻す", "戻し", "undo", "取り消し")


def test_files_exist() -> tuple[str, str] | None:
    print("--- fixture: source files exist ---")
    check(ROUTER_MD.is_file(), f"{ROUTER_MD} exists")
    check(SKILL_MD.is_file(), f"{SKILL_MD} exists")
    if not (ROUTER_MD.is_file() and SKILL_MD.is_file()):
        return None
    return (ROUTER_MD.read_text(encoding="utf-8"),
            SKILL_MD.read_text(encoding="utf-8"))


def _goal_row(router: str, state: str, action: str) -> list[str] | None:
    m = re.search(
        rf"^\|\s*`{state}`\s*\|\s*`{action}`\s*\|(?P<cells>.*)\|\s*$",
        router, re.MULTILINE)
    if not m:
        return None
    return [t.strip().strip("`") for t in m.group("cells").split(",")]


def test_a_goal_state_table(router: str) -> None:
    print("--- (a) goal-state table rows and released vocabulary ---")
    rows = {
        ("2_done", "approve"): ("完了", "終了", "done", "finish", "approve"),
        ("1_in_progress", "start"): ("着手", "開始", "再開", "進行中",
                                     "start", "begin", "resume"),
        ("0_todo", "unstart"): ("未着手", "着手前", "開始前", "todo",
                                "unstart"),
    }
    all_tokens: list[str] = []
    for (state, action), expected in rows.items():
        tokens = _goal_row(router, state, action)
        check(tokens is not None, f"goal-state row `{state}` → `{action}` is present")
        if tokens is None:
            continue
        all_tokens.extend(tokens)
        for t in expected:
            check(t in tokens, f"{state} row contains synonym '{t}'")
    lowered = [t.lower() for t in all_tokens]
    for released in RELEASED_VOCAB:
        check(released not in lowered,
              f"released vocabulary '{released}' is absent from all synonym rows")
    check("ok" not in lowered, "'ok' is not among any synonym tokens")


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
    for tok in ("進行中", "開始前"):
        check(f"`{tok}`" in router,
              f"Japanese token list includes new token '{tok}'")


def test_c_maximal_munch(router: str) -> None:
    print("--- (c) maximal munch for overlapping Japanese tokens ---")
    check(re.search(r"[Mm]aximal munch", router) is not None,
          "maximal-munch rule is present")
    check(re.search(r"longest occurrence", router) is not None,
          "rule counts only the longest overlapping occurrence")
    check("未着手" in router and "suppress" in router,
          "未着手/着手前 suppress the contained 着手 (example present)")
    check("未着手に」 would" in router or "mis-resolve" in router,
          "rationale example (「alpha を未着手に」 mis-resolving to start) present")


def test_d_path_exclusion(router: str) -> None:
    print("--- (d) path exclusion for synonyms inside path-like tokens ---")
    check(re.search(r"[Pp]ath exclusion", router) is not None,
          "path-exclusion rule is present")
    check("@tasks/0_todo/" in router,
          "@-reference example (@tasks/0_todo/...) present")
    check(re.search(r"`0_todo`[^\n]*must not register `todo`", router) is not None,
          "0_todo-inside-a-path must not register `todo`")
    check(re.search(r"must not register `start`", router) is not None,
          "filename containing 'start' must not register `start`")


def test_e_undo_intent_gate(router: str) -> None:
    print("--- (e) undo-intent gate (semantic, checked first, fail-closed) ---")
    check(re.search(r"###\s*Undo-intent gate", router) is not None,
          "undo-intent gate section heading is present")
    m = re.search(r"###\s*Undo-intent gate.*?(?=\n### )", router, re.DOTALL)
    check(m is not None, "gate section is sliceable (followed by another ### section)")
    sec = m.group(0) if m else ""
    if not sec:
        return
    check("checked FIRST" in sec,
          "gate is declared to run first / override matches")
    check("NOT a string-match" in sec,
          "example words are marked illustrative, not a match list")
    for w in ("取り消して", "やめて", "戻して", "なかったことに", "undo", "revert", "cancel"):
        check(w in sec, f"intent example word '{w}' present")
    check('"action": "unknown"' in sec,
          "gate terminal emits the fixed unknown JSON")
    check("Do not continue" in sec,
          "terminal stops before matching / target resolution")
    check("「戻り値検証タスクを完了に」" in sec,
          "contrastive negative example (戻り値 → not undo, proceed to approve) present")
    check("「着手を取り消して」" in sec,
          "positive worked example 「着手を取り消して」 present")
    check("must NOT become `start`" in sec,
          "example explicitly forbids resolving 「着手を取り消して」 to start")
    check("semantic judgment, not a string rule" in sec,
          "gate is declared semantic with both failure directions banned")
    check("global revert" in sec,
          "gate reasoning attributes the vocabulary to the global revert skill")


def test_f_reach_state_tiebreak(router: str) -> None:
    print("--- (f) reach-state tie-break replaces the main-verb rule ---")
    check(re.search(r"\*\*reach\*\*|reach-state", router) is not None,
          "tie-break picks the state the user wants the task to reach")
    check(re.search(r"NOT the state being\s+left", router) is not None,
          "tie-break explicitly excludes the state being left")
    check("未着手へ」 → `0_todo`" in router,
          "worked example (完了していた…未着手へ → 0_todo) present")
    check(re.search(r"maintenance token[^\n]*\n?[^\n]*goal\s*\n?[^\n]*state wins|goal\s+state wins", router) is not None,
          "goal-state token beats a maintenance token")
    check(re.search(r'cannot decide.*?action:\s*"unknown"', router, re.DOTALL) is not None,
          "undecidable tie-break falls back to action \"unknown\"")
    check(re.search(r"main verb", router, re.IGNORECASE) is None,
          "old main-verb rule text is fully removed from the router")


def test_g_json_contract(router: str) -> None:
    print("--- (g) JSON contract: action enum and goal target_status ---")
    check('"action": "approve | start | unstart | check | audit | sync | rebuild | unknown"' in router,
          "action enum is approve | start | unstart | ... (no revert)")
    check(re.search(r'"action":[^\n]*revert', router) is None,
          "no revert remains in the action enum line")
    check(re.search(r"`unstart` → `0_todo`", router) is not None,
          "target_status maps unstart → 0_todo")
    check(re.search(r"skips a\s+state", router) is not None,
          "status_mismatch is defined as a state-skipping transition")


def test_h_skill_single_source(router: str, skill: str) -> None:
    print("--- (h) SKILL.md single-source pointer and unstart dispatch ---")
    check("agents/progress-router.md" in skill,
          "SKILL.md references agents/progress-router.md")
    check(re.search(r"Step 1 of the body of", skill) is not None,
          "SKILL.md points at the router's Step 1 body specifically")
    check(re.search(r"undo-intent\s+gate", skill, re.IGNORECASE) is not None,
          "SKILL.md fallback mentions the undo-intent gate")
    check(re.search(r"goal state", skill, re.IGNORECASE) is not None,
          "SKILL.md fallback describes goal-state resolution")
    check(re.search(r"main verb", skill, re.IGNORECASE) is None,
          "SKILL.md no longer carries the stale main-verb summary")
    check("| Goal state | Action | Synonyms |" not in skill,
          "SKILL.md does not duplicate the router's goal-state table")
    check("| Goal state | Action | Synonyms |" in router,
          "sanity: the goal-state table header does live in progress-router.md")
    check("### action = `unstart`" in skill,
          "SKILL.md dispatches action unstart")
    check("### action = `revert`" not in skill,
          "SKILL.md no longer has a revert dispatch branch")
    check("{approve, start, unstart}" in skill,
          "SKILL.md Step 4 validates the {approve, start, unstart} set")
    check("/progress revert" not in skill,
          "SKILL.md carries no /progress revert usage")
    check("未着手" in skill,
          "SKILL.md usage examples include the state word 未着手")
    check("unstarted → 0_todo" in skill,
          "SKILL.md unstart branch logs 'unstarted → 0_todo'")


def main() -> int:
    print("=== progress-router.md / SKILL.md state-goal text-contract tests ===")
    texts = test_files_exist()
    if texts is None:
        print()
        print(f"{FAIL} failed, {PASS} passed. (source files missing — aborted)")
        return 1
    router, skill = texts
    test_a_goal_state_table(router)
    test_b_word_boundary_rule(router)
    test_c_maximal_munch(router)
    test_d_path_exclusion(router)
    test_e_undo_intent_gate(router)
    test_f_reach_state_tiebreak(router)
    test_g_json_contract(router)
    test_h_skill_single_source(router, skill)

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
