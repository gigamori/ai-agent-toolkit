#!/usr/bin/env python3
r"""Repo-wide lint: no machine-local absolute path may sit in a TRACKED file.

Usage:  uv run --no-project python tests/test_tracked_path_hygiene.py
Exit:   0 = clean, 1 = findings (or an unreadable tracked file), 2 = the lint
        itself could not run (git failure, or its own non-vacuity self-check
        failed -- in which case NOTHING was measured and a "clean" would be a
        lie).

Why this exists
---------------
The rule it enforces is not written down anywhere inside this repository. It
lives in the machine-global Claude Code instructions file, in the block whose
id is `git_universal`: credentials, tokens and connection info must never be
committed, and *absolute local paths count as secrets too*, in all tracked
code, config, prompts, docs, comments and examples -- the sanctioned
substitutes being environment variables, `${CLAUDE_PLUGIN_ROOT}`, relative
paths, or a generic `/path/to/...`. That file lives outside the checkout and
is not quoted verbatim here; this docstring restates the rule so that a clone,
which has neither the rule nor a pointer to it, can still read what the lint
is for.

What it flags
-------------
Every path in `git ls-files` is read and searched for the *identities of this
machine*, all derived at runtime so that the lint carries no machine identity
of its own (a lint containing a hard-coded local path would violate the very
rule it enforces):

  * the checkout root, from `git rev-parse --show-toplevel`;
  * the running user's home directory;
  * every drive-anchored ancestor of the checkout root EXCEPT the drive root
    itself -- for a checkout at `<drive>:/a/b/c` those are `<drive>:/a/b` and
    `<drive>:/a`.

The third group is not redundant with the first two. A sibling repository
parked beside this checkout lies under neither the checkout root nor the home
directory, yet its path still names this machine, and this workspace
references such siblings routinely.

Each identity is matched case-insensitively (drive letters and path case both
vary) in every spelling this host is known to produce:

  forward-slash drive    X:/a/b
  backslash drive        X:\a\b
  MSYS / Git-Bash mount  /x/a/b
  JSON string literal    X:\\a\\b     (every separator backslash doubled)

A match must also sit on a token boundary: the character on either side may
not be alphanumeric, `_` or `-`. Without that, a home directory whose last
component is short (`.../Users/1`) would match an unrelated `.../Users/1234`.

What it deliberately does NOT flag
----------------------------------
Absolute paths that name no identity of this machine are legitimate and stay
clean by construction: synthetic redaction/classification fixtures whose whole
purpose is to contain absolute paths, `<user>`-style placeholders, mocked
launcher paths, drive-less POSIX examples such as `/home/...`, and the
sanctioned `/path/to/...` form. This is why the lint matches derived machine
identities rather than "absolute-looking path" shapes, and why there is no
allowlist file -- an allowlist is precisely the maintenance-rot class this
check exists to close.

Second clause: dead documentation paths
---------------------------------------
A rule is cited by id, never by path. The rule files here are gitignored, so a
path citation resolves for nobody who clones this repository, and it goes on
resolving for nobody after the file behind it moves again. DEAD_DOC_PATHS holds
the paths already known to be dead; a tracked file naming one of them fails.

That list is hand-maintained, and short on purpose. It is not an allowlist of
violations to be worked off -- it is the set of strings that must not reappear,
so it grows only when another governing document moves, one line at a time.

How it is detected -- and why not with a shell one-liner
--------------------------------------------------------
Enumeration is `git ls-files -z`; every subsequent read and match happens
inside Python, and no pattern ever crosses a shell quoting layer. Two distinct
silent false-"clean" results were measured on this host before this lint was
written:

  * MSYS argument path-conversion rewrote a `/`-leading pattern before
    `git grep` ever saw it. The failure is silent and exit-0: the lint reports
    clean regardless of content.
  * A pattern containing backslashes was collapsed by a quoting layer
    (`\\` -> `\`) so the search ran for the wrong string, and reported a set of
    fixtures clean when they were not.

Both failure shapes are invisible to the operator, which is also why this file
carries a permanent non-vacuity self-check: before scanning, it synthesizes a
string containing a derived identity in each of the four spellings and asserts
the detector flags every one, and asserts benign control strings are not
flagged. If that self-check fails the lint exits 2 rather than reporting
clean. Files are decoded as UTF-8 strictly first and only re-decoded with
`errors="replace"` when that fails, which is warned about and never silently
swallowed -- strict-first is what keeps the warning meaningful, since a file
may legitimately hold a U+FFFD character of its own and testing the decoded
text for one cannot tell that apart from a byte that failed to decode. No
file is skipped by a binary
heuristic (the leaks this lint was written for live in `.jsonl` fixtures that
`grep -I` would have excluded), and a file that cannot be read is reported as
a finding rather than passed over.

Launch form
-----------
This module imports only the standard library (`os`, `subprocess`, `sys`), so
it declares no PEP 723 inline dependencies; the correct launcher for a
declaration-free test in this repo is therefore
`uv run --no-project python <path>` and NOT `uv run --script <path>`.
This repository has no CI and no repo-level test runner, so the lint is run
manually, exactly like every other test here.
"""

import os
import subprocess
import sys

# A single backslash, built rather than typed, so that no quoting or escaping
# layer between this line and the matcher can collapse it.
BS = chr(92)

WORDISH = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


# ---------------------------------------------------------------- git access


def _git(args):
    """Run a read-only git command. Exit 2 on failure -- nothing was measured."""
    proc = subprocess.run(["git"] + args, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        print("LINT ABORT: git %s failed: %s" % (" ".join(args), err),
              file=sys.stderr)
        raise SystemExit(2)
    return proc.stdout


def repo_root():
    out = _git(["rev-parse", "--show-toplevel"]).decode("utf-8", errors="replace")
    return out.strip()


def tracked_paths():
    raw = _git(["ls-files", "-z"])
    return [os.fsdecode(b) for b in raw.split(b"\0") if b]


# ------------------------------------------------------------- identities


def _normalize(path):
    """Return a drive-anchored `X:/a/b` form, or None if not drive-anchored."""
    if not path:
        return None
    p = path.replace(BS, "/")
    while p.endswith("/") and len(p) > 3:
        p = p[:-1]
    if len(p) < 3 or p[1] != ":" or not p[0].isalpha() or p[2] != "/":
        return None
    return p


def _ancestors(root):
    """Drive-anchored ancestors of `root`, excluding the drive root itself."""
    out = []
    parts = [c for c in root[3:].split("/") if c]
    while len(parts) > 1:
        parts = parts[:-1]
        out.append(root[:3] + "/".join(parts))
    return out


def derive_identities():
    """Every path on this host that this lint treats as a machine identity."""
    ids = []

    root = _normalize(repo_root())
    if root is None:
        print("LINT ABORT: the checkout root is not drive-anchored on this "
              "host; this lint's identity derivation does not cover that case.",
              file=sys.stderr)
        raise SystemExit(2)
    ids.append(("checkout root", root))

    home = _normalize(os.path.expanduser("~"))
    if home is not None and home != root:
        ids.append(("home directory", home))

    for anc in _ancestors(root):
        ids.append(("checkout-root ancestor", anc))

    return ids


def spellings(identity):
    """(label, needle) for every spelling of `identity` this host produces."""
    fwd = identity
    bsl = identity.replace("/", BS)
    dbl = bsl.replace(BS, BS + BS)
    msys = "/" + identity[0].lower() + identity[2:]
    return [
        ("forward-slash drive", fwd),
        ("backslash drive", bsl),
        ("MSYS mount", msys),
        ("JSON-doubled backslash", dbl),
    ]


# Governing documents that have moved. A tracked file naming one of these is
# citing by path something no clone can resolve; cite the rule id instead.
# Assembled from fragments so this file does not match its own needle.
DEAD_DOC_PATHS = [
    "plugins/taskflow/" + "CLAUDE" + ".md",
]


def build_needles(identities):
    needles = []
    for kind, ident in identities:
        for label, needle in spellings(ident):
            needles.append((kind, ident, label, needle.lower()))
    for dead in DEAD_DOC_PATHS:
        needles.append(("dead doc path", dead, "repo-relative", dead.lower()))
    return needles


# ---------------------------------------------------------------- matching


def _boundary_ok(hay_low, start, end):
    if start > 0 and hay_low[start - 1] in WORDISH:
        return False
    if end < len(hay_low) and hay_low[end] in WORDISH:
        return False
    return True


def scan_line(line, needles):
    """Return [(start, kind, identity, label, needle_len)] for one line."""
    hay = line.lower()
    hits = []
    for kind, ident, label, needle in needles:
        at = hay.find(needle)
        while at != -1:
            end = at + len(needle)
            if _boundary_ok(hay, at, end):
                hits.append((at, kind, ident, label, len(needle)))
            at = hay.find(needle, at + 1)
    # One offset can satisfy both a path and its own ancestor; keep the longest
    # match per start offset so a single leak is reported once, as one site.
    best = {}
    for hit in hits:
        cur = best.get(hit[0])
        if cur is None or hit[4] > cur[4]:
            best[hit[0]] = hit
    return [best[k] for k in sorted(best)]


def scan_text(text, needles):
    """Return [(lineno, start, kind, identity, label, snippet)]."""
    findings = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for start, kind, ident, label, length in scan_line(line, needles):
            lo = max(0, start - 20)
            hi = min(len(line), start + length + 20)
            snippet = (("..." if lo > 0 else "") + line[lo:hi]
                       + ("..." if hi < len(line) else ""))
            findings.append((lineno, start, kind, ident, label, snippet))
    return findings


# ------------------------------------------------------- non-vacuity check


def self_check(identities, needles):
    """Prove the detector still detects. Returns a list of failure strings."""
    problems = []
    kind, ident = identities[0]
    for label, needle in spellings(ident):
        probe = 'REPO="' + needle + '/probe" trailing text'
        if not scan_line(probe, needles):
            problems.append(
                "detector did NOT flag a synthesized %s spelling of the %s"
                % (label, kind))
    for dead in DEAD_DOC_PATHS:
        probe = "State-dir sandbox (" + dead + " `some_rule_id`): the"
        if not scan_line(probe, needles):
            problems.append(
                "detector did NOT flag a synthesized citation of the dead doc "
                "path %r" % dead)
    controls = [
        "uv run python /path/to/lib/src/run_tool.py",
        "see " + "C:" + BS + "Users" + BS + "<user>" + BS + "plugins for it",
        "open /home/alice/.ssh/id_rsa now",
        "cite the rule by id: `e2e_state_dir_sandbox`, never by path",
    ]
    for control in controls:
        if scan_line(control, needles):
            problems.append(
                "detector flagged a benign control string (%r); the patterns "
                "are too wide, or this control collides with a real identity "
                "on this host and must be replaced" % control)
    return problems


# ------------------------------------------------------------------- main


def main():
    identities = derive_identities()
    needles = build_needles(identities)

    print("tracked-path hygiene lint")
    print("  identities derived on this host: %d" % len(identities))
    for kind, ident in identities:
        elided = ident[:3] + "<" + str(len(ident) - 3) + " chars elided>"
        print("    - %-24s %s" % (kind, elided))
    print("  needles (4 spellings each):      %d" % len(needles))

    problems = self_check(identities, needles)
    if problems:
        print("")
        print("SELF-CHECK FAILED -- the lint is not measuring anything:")
        for p in problems:
            print("  ! " + p)
        print("Exiting 2 without reporting a result.")
        return 2
    print("  non-vacuity self-check:          PASS "
          "(4 spellings + %d dead doc path(s) flagged, 4 controls clean)"
          % len(DEAD_DOC_PATHS))

    paths = tracked_paths()
    print("  tracked files scanned:           %d" % len(paths))

    findings = []
    unreadable = []
    replaced = []
    for rel in paths:
        try:
            with open(rel, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            unreadable.append((rel, str(exc)))
            continue
        # Decoding strictly first is what makes the warning mean something: a
        # file may legitimately contain a U+FFFD character of its own (three
        # tracked files do), and testing the decoded text for U+FFFD cannot
        # tell that apart from a byte that failed to decode.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            replaced.append(rel)
        for hit in scan_text(text, needles):
            findings.append((rel,) + hit)

    if replaced:
        print("")
        print("WARNING: %d file(s) held bytes that are not valid UTF-8; they "
              "were decoded with errors=\"replace\" and scanned anyway:"
              % len(replaced))
        for rel in replaced:
            print("  ~ " + rel)

    if unreadable:
        print("")
        print("UNREADABLE (reported, never skipped):")
        for rel, exc in unreadable:
            print("  ? %s: %s" % (rel, exc))

    print("")
    if not findings and not unreadable:
        print("PASS: no machine-local absolute path in any tracked file.")
        return 0

    print("FAIL: %d machine-local path occurrence(s) in tracked files."
          % len(findings))
    sites = set()
    for rel, lineno, _start, kind, _ident, label, snippet in findings:
        sites.add((rel, lineno))
        print("  %s:%d: %s (%s)" % (rel, lineno, kind, label))
        print("      %s" % snippet)
    print("")
    print("%d distinct site(s) across %d file(s)."
          % (len(sites), len({s[0] for s in sites})))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
