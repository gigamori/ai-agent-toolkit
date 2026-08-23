#!/usr/bin/env python3

import re
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

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


RE_COMMENT = re.compile(r"^\s*#")
RE_ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
RE_HOOK_LITERAL = re.compile(r"plugins/taskflow/hooks/[A-Za-z0-9_]+\.py")
RE_PYTHON = re.compile(r"\bpython\b")
RE_GUARD_INSIDE_REPO = re.compile(r"\$\{?REPO_ROOT\}?\"?/\*")
RE_GUARD_ANCESTOR_PROBE = re.compile(
    r"-d\s+\"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?/_projects/_state\"")
RE_EXIT_2 = re.compile(r"\bexit\s+2\b")


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return text, ("�" in text)


def code_lines(text):
    return [ln for ln in text.splitlines() if not RE_COMMENT.match(ln)]


def hook_vars(lines):
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
    """Counting the `python - "$HOOK"` form too keeps the detector leaning in scope, the
    safe direction for this invariant."""
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
            "%s invokes a taskflow hook but carries neither sandbox guard "
            "(abort when the temp dir is inside the repo, abort when an ancestor "
            "of it holds _projects/_state), and is not on the allowlist. Add them "
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
