#!/usr/bin/env python3
# test_sandbox_guard_ratchet.py -- presence ratchet for the `e2e_state_dir_sandbox`
# step-4 sandbox guards on the `.sh` E2E scripts in this directory.
#
# The invariant
# -------------
# A `.sh` under plugins/taskflow/tests/ that ACTUALLY INVOKES a file under
# plugins/taskflow/hooks/ must carry the step-4 guards, and a NEW one without
# them must not be able to land green.
#
# Why it exists: on 2026-07-17 a regression run with cwd at the repo root
# deleted 250 real session-state files under `_projects/_state/`. `_projects/`
# is gitignored and untracked, so they were unrecoverable. Since the 2026-08-20
# ancestor-walk rollout every taskflow hook resolves its roots by walking UP
# from the cwd (cwd included) to the first directory holding `_projects/_state`,
# so "cd into a tempdir" isolates nothing by itself -- a temp dir inside the
# repo tree resolves to the REAL one. Only the step-4 guards close that.
#
# Governing rule: `e2e_state_dir_sandbox`, cited BY ID and never by path --
# every candidate path for that rule file is gitignored, so no path citation
# survives a clone.
#
# Scope criterion (deliberate, and NOT "creates a temp workspace AND invokes a
# hook")
# ---------------------------------------------------------------------------
# In scope == invokes a file under `plugins/taskflow/hooks/`. Full stop.
# An earlier draft of this ratchet ANDed in "creates a temp workspace"; that was
# wrong, because the scripts which do NOT create a temp workspace are precisely
# the ones that run with cwd at the repo root -- the accident shape itself.
# Narrowing the criterion to keep the allowlist short loses the invariant.
# Do not reintroduce that criterion.
#
# Invocation, not mention: a bare search for `hooks/<name>.py` over this
# directory hits 18 files, but `test_progress_router_misleading.sh:21` matches
# only a prose comment naming a DIFFERENT plugin's hook
# (`~/.claude/hooks/revert_prompt_submit.py`). Comment lines are stripped
# before any matching, and a match must sit on a line that also launches
# `python`.
#
# Explicitly OUT OF SCOPE, so a future reader does not think it was overlooked:
#   * `test_e2e_rebuild_hook.sh` -- invokes no hook directly. It is a live
#     `claude -p` test whose PROJECT_DIR is `$REPO_ROOT/_projects/$PROJECT`
#     (:15) and which deliberately preserves that directory for inspection
#     (:30, "to clean up: rm -rf $PROJECT_DIR"). It writes into the REAL
#     `_projects/` BY DESIGN. That is a different hazard class -- governed by
#     `workspace_git_safety`, not by step-4 temp isolation -- and this ratchet
#     cannot express it. Not an oversight; not allowlisted either, because it
#     never enters scope.
#   * `test_progress_start.sh` and `test_progress_router_misleading.sh` --
#     same shape: `claude -p` drivers plus `scripts/rebuild_progress.py`, which
#     lives under `scripts/`, not `hooks/`.
#   * `capture_paths.sh` -- a sourced helper (no shebang, no invocation).
#
# Detector limits (what this measurement does NOT see)
# ----------------------------------------------------
#   * Pattern axis: a hook launched through a path this file cannot resolve
#     statically -- built by string concatenation, read from a file, passed in
#     via $1, or reached through `bash -c` with the path assembled at runtime --
#     is scored NOT in scope, and would slip the ratchet. Resolution covers
#     literal `plugins/taskflow/hooks/<name>.py` assignments plus variables
#     transitively derived from them (e.g. `SI_WIN="$(to_win "$SI")"`).
#   * Pattern axis: a guard written with a differently-named repo-root variable,
#     or an ancestor walk not shaped like the two reference implementations, is
#     scored ABSENT. That direction fails closed (the script gets flagged), so
#     the remedy is to match the reference shape.
#   * Pattern axis: only whole-line comments (`^\s*#`) are stripped. A hooks
#     path sitting in a TRAILING comment on a code line that also says `python`
#     would be scored as an invocation. No such line exists today.
#   * Scope axis: `*.sh` in THIS directory only. `.py` tests, the hooks
#     themselves, `plugins/taskflow/scripts/`, `.sh` files elsewhere in the
#     repo, and anything a script SOURCES from outside this directory are all
#     outside the window.
#   * This is a PRESENCE check on source text. It proves the guard block is
#     written, never that it runs, aborts correctly, or that the script is
#     otherwise safe.
#
# Launch form (derived, not remembered)
# --------------------------------------
# Per `test_py_pep723_invocation`: `.py` tests in this directory split by
# whether they declare PEP723 inline dependencies. This file imports only the
# stdlib (re, sys, pathlib), therefore it declares NO `# /// script` header,
# therefore `uv run --script` would have nothing to resolve and the correct
# form is `uv run --no-project python <path>`. If a third-party dependency is
# ever added here, a PEP723 header must be added WITH it and the Usage line
# below must flip to `uv run --script`.
#
# This repo has no CI and no repo-level test runner. Like every other test
# here, this one is run manually.
#
# Usage:  uv run --no-project python plugins/taskflow/tests/test_sandbox_guard_ratchet.py
# Exit:   0 = ratchet holds, 1 = ratchet broken (or the detector self-check failed)
# Reads only. Executes nothing: runs no `.sh`, spawns no hook.

import re
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# The allowlist: scripts that are IN SCOPE and currently have NO guards.
#
# Shrink-only, and that is the whole point. Two failure directions:
#   * a script in scope without guards that is NOT listed  -> new hazard, red.
#   * a listed script that HAS since gained guards          -> stale entry, red.
# The second is what makes this a ratchet instead of a decaying constant: you
# cannot fix a script and leave its name here.
#
# The size of this list is not something to optimise. It is whatever the
# measurement gives.
# ---------------------------------------------------------------------------
ALLOWLIST = {
    "test_bind_skip_no_anchor.sh",
    "test_capture_late_sidecar.sh",
    "test_cross_project_binding.sh",
    "test_e2e_capture_bind.sh",
    "test_exec_unresolved.sh",
    "test_guidelines_reminder_mode.sh",
    "test_note_links_apply.sh",
    "test_pj_parse_window.sh",
    "test_precompact_flush.sh",
    "test_rebuild_hook_path_notation.sh",
    "test_round_binding.sh",
    "test_round_f1_integrity.sh",
    "test_rules_injection.sh",
    "test_sid_binding_gate.sh",
    "test_task_rebuild_hook.sh",
}

# --- detector ---------------------------------------------------------------
# All matching happens in Python, on file contents read directly. Nothing is
# shelled out to grep. Two measured reasons: MSYS rewrites `/`-leading patterns
# before `git grep` ever sees them, silently and exit-0; and a pattern
# containing backslashes was collapsed by a quoting layer and searched for the
# wrong string, reporting a real leak as clean. Patterns must not traverse a
# shell quoting layer.

RE_COMMENT = re.compile(r"^\s*#")
RE_ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
RE_HOOK_LITERAL = re.compile(r"plugins/taskflow/hooks/[A-Za-z0-9_]+\.py")
RE_PYTHON = re.compile(r"\bpython\b")
# `case "$TMP" in "$REPO_ROOT"|"$REPO_ROOT"/*)` -- the inside-the-repo abort.
RE_GUARD_INSIDE_REPO = re.compile(r"\$\{?REPO_ROOT\}?\"?/\*")
# `[ -d "$d/_projects/_state" ]` -- the ancestor-walk probe; captures the var.
RE_GUARD_ANCESTOR_PROBE = re.compile(
    r"-d\s+\"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?/_projects/_state\"")
RE_EXIT_2 = re.compile(r"\bexit\s+2\b")


def read_text(path):
    """Return (text, had_replacement). Never skips a file silently."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return text, ("�" in text)


def code_lines(text):
    """Whole-line comments removed. Invocation, not mention."""
    return [ln for ln in text.splitlines() if not RE_COMMENT.match(ln)]


def hook_vars(lines):
    """Shell vars holding a hook path, resolved to a fixed point.

    Covers the direct form (`HOOK="$REPO_ROOT/plugins/taskflow/hooks/x.py"`)
    and derived forms (`SI_WIN="$(to_win "$SI")"`), both of which the scripts
    here use. It does not cover paths assembled at runtime -- see the detector
    limits in the header.
    """
    found = set()
    changed = True
    while changed:
        changed = False
        for ln in lines:
            m = RE_ASSIGN.match(ln)
            if not m:
                continue
            name, rhs = m.group(1), m.group(2)
            if name in found:
                continue
            if RE_HOOK_LITERAL.search(rhs):
                found.add(name)
                changed = True
                continue
            for known in sorted(found):
                if re.search(r"\$\{?" + re.escape(known) + r"\}?", rhs):
                    found.add(name)
                    changed = True
                    break
    return found


def invocation_lines(lines):
    """Lines that launch python AND name a hook file (literally or by var).

    Includes the `python - "$HOOK" ...` form, where the hook path is handed to
    a stdin program that imports and drives it. That still executes hook code,
    so counting it keeps the detector leaning IN scope -- the safe direction
    for this invariant.
    """
    hvars = hook_vars(lines)
    hits = []
    for ln in lines:
        if not RE_PYTHON.search(ln):
            continue
        if RE_HOOK_LITERAL.search(ln):
            hits.append(ln.strip())
            continue
        for v in sorted(hvars):
            if re.search(r"\$\{?" + re.escape(v) + r"\}?", ln):
                hits.append(ln.strip())
                break
    return hits


def has_guards(lines):
    """Both step-4 aborts present: inside-the-repo, and ancestor holds state."""
    joined = "\n".join(lines)
    inside_repo = any(RE_GUARD_INSIDE_REPO.search(ln) for ln in lines)
    ancestor = False
    for ln in lines:
        m = RE_GUARD_ANCESTOR_PROBE.search(ln)
        if not m:
            continue
        walk_var = m.group(1)
        if re.search(r"dirname\s+\"\$\{?" + re.escape(walk_var) + r"\}?\"", joined):
            ancestor = True
            break
    aborts = any(RE_EXIT_2.search(ln) for ln in lines)
    return inside_repo and ancestor and aborts


def classify(text):
    lines = code_lines(text)
    hits = invocation_lines(lines)
    return {
        "in_scope": bool(hits),
        "evidence": hits[0] if hits else "",
        "guards": has_guards(lines) if hits else False,
    }


# --- permanent non-vacuity self-check ---------------------------------------
# Without this the detector can silently degrade into a no-op that reports a
# clean tree forever. That failure mode was measured twice in this repo in the
# last three days, so the check lives inside the test rather than in a review
# checklist. These bodies are strings; nothing is written to disk or executed.

SYN_UNGUARDED = """#!/usr/bin/env bash
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
TMP="$(mktemp -d)"
cd "$TMP"
echo '{"session_id":"x"}' | uv run --no-project python "$HOOK"
"""

SYN_GUARDED = """#!/usr/bin/env bash
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SI="$REPO_ROOT/plugins/taskflow/hooks/session_init.py"
to_win() { cygpath -m "$1"; }
SI_WIN="$(to_win "$SI")"
TMP="$(mktemp -d)" || { echo "ABORT" >&2; exit 2; }
[ -n "$TMP" ] && [ -d "$TMP" ] || { echo "ABORT" >&2; exit 2; }
cd "$TMP" || { echo "ABORT" >&2; exit 2; }
case "$TMP" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "ABORT: inside the repo tree" >&2
    cd /; rm -rf "$TMP"; exit 2 ;;
esac
d="$TMP"
while :; do
  if [ -d "$d/_projects/_state" ]; then
    echo "ABORT: ancestor holds _projects/_state" >&2
    cd /; rm -rf "$TMP"; exit 2
  fi
  p="$(dirname "$d")"; [ "$p" = "$d" ] && break; d="$p"
done
mkdir -p _projects/_state
echo '{"session_id":"x"}' | uv run --no-project python "$SI_WIN"
"""

SYN_MENTION_ONLY = """#!/usr/bin/env bash
# NOTE: this script does NOT run the hook. For reference, the real driver does
#   uv run --no-project python "$REPO_ROOT/plugins/taskflow/hooks/session_init.py"
# and a user-level ~/.claude/hooks/revert_prompt_submit.py can confound it.
set -uo pipefail
echo "nothing invoked here"
"""


def self_check(report):
    ok = True

    r = classify(SYN_UNGUARDED)
    if r["in_scope"] and not r["guards"]:
        report("  PASS  synthetic in-scope UNGUARDED body is flagged")
    else:
        ok = False
        report("  FAIL  synthetic unguarded body NOT flagged "
               "(in_scope=%s guards=%s) -- the detector is a no-op"
               % (r["in_scope"], r["guards"]))

    r = classify(SYN_GUARDED)
    if r["in_scope"] and r["guards"]:
        report("  PASS  synthetic in-scope GUARDED body is accepted "
               "(and its derived $SI_WIN var resolved)")
    else:
        ok = False
        report("  FAIL  synthetic guarded body scored in_scope=%s guards=%s -- "
               "the detector rejects the reference shape"
               % (r["in_scope"], r["guards"]))

    r = classify(SYN_MENTION_ONLY)
    if not r["in_scope"]:
        report("  PASS  comment-only mention of a hook is NOT in scope")
    else:
        ok = False
        report("  FAIL  a commented-out invocation was scored in scope "
               "(evidence: %s) -- comment stripping is broken" % r["evidence"])

    return ok


def main():
    report_lines = []

    def report(s):
        report_lines.append(s)

    failures = []

    report("=== detector self-check (non-vacuity) ===")
    if not self_check(report):
        failures.append("detector self-check failed -- every result below is "
                        "untrustworthy until it passes")

    scripts = sorted(TESTS_DIR.glob("*.sh"), key=lambda p: p.name)
    report("")
    report("=== classification: %d .sh files under plugins/taskflow/tests/ ==="
           % len(scripts))

    in_scope, out_scope = [], []
    for path in scripts:
        text, replaced = read_text(path)
        if replaced:
            report("  WARN  %s: U+FFFD replacement occurred while decoding as "
                   "utf-8; its classification may be lossy" % path.name)
        r = classify(text)
        if r["in_scope"]:
            in_scope.append((path.name, r))
        else:
            out_scope.append(path.name)

    for name, r in in_scope:
        report("  IN   %-40s guards=%-5s | %s"
               % (name, r["guards"], r["evidence"][:80]))
    for name in out_scope:
        report("  OUT  %-40s invokes no file under plugins/taskflow/hooks/" % name)

    report("")
    report("=== ratchet ===")
    unguarded = {name for name, r in in_scope if not r["guards"]}
    guarded = {name for name, r in in_scope if r["guards"]}
    all_names = {p.name for p in scripts}

    for name in sorted(unguarded - ALLOWLIST):
        failures.append(
            "%s invokes a taskflow hook but carries no `e2e_state_dir_sandbox` "
            "step-4 sandbox guards, and is not on the allowlist. Add the guards "
            "(reference shape: test_selflog_placeholder_guard.sh / "
            "test_freshsession_mechanisms.sh). The allowlist is shrink-only: do "
            "NOT add a new name to it." % name)

    for name in sorted(ALLOWLIST & guarded):
        failures.append(
            "%s now HAS the step-4 guards but is still on ALLOWLIST. Remove it "
            "from the list -- the ratchet only turns one way." % name)

    for name in sorted(n for n in ALLOWLIST if n not in all_names):
        failures.append(
            "ALLOWLIST entry %s no longer exists under plugins/taskflow/tests/. "
            "Remove it." % name)

    for name in sorted(n for n in ALLOWLIST
                       if n in all_names and n not in unguarded and n not in guarded):
        failures.append(
            "ALLOWLIST entry %s invokes no file under plugins/taskflow/hooks/, so "
            "it is out of scope and the entry is meaningless. Remove it." % name)

    report("  in scope:        %d   (guarded %d / unguarded %d)"
           % (len(in_scope), len(guarded), len(unguarded)))
    report("  out of scope:    %d" % len(out_scope))
    report("  allowlist size:  %d" % len(ALLOWLIST))

    sys.stdout.write("\n".join(report_lines) + "\n")
    if failures:
        sys.stdout.write("\n%d ratchet failure(s):\n" % len(failures))
        for f in failures:
            sys.stdout.write("  FAIL  %s\n" % f)
        return 1
    sys.stdout.write("\nRatchet holds: every in-scope .sh either carries the "
                     "step-4 guards or is a known, listed, still-unguarded one.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
