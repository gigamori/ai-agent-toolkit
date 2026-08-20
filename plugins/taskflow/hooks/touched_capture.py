#!/usr/bin/env python3
"""
PostToolUse hook: record this session's file writes to a per-session
`<STATE_DIR>/<session_id>.touched` ledger (one normalized repo-relative path
per line, append-only, lock-free).

Replaces the old Stop-hook jsonl-scan + git-diff detection
(project-notes/specs/exec-binding.md §3.1/§3.2). Fires for
Write / Edit / NotebookEdit and file-touching Bash (`mv|cp|rm`, shell
redirection `>`/`>>`, `tee`, and `sed -i`/`--in-place`).

Provenance (exec-binding.md §3.3): capture is limited to *this session's tool
writes* — action observation, NOT filesystem result observation (git/mtime/
hash), which is the source of unrelated-task mis-stamping. PostToolUse fires
for Agent-tool subagent / fork internal tool calls with the PARENT session_id
(exec-binding.md §3.1, TBD-1 probe 2026-06-28), so subagent writes (P3) land in
the parent's `.touched` without git/jsonl.

Append is best-effort and lock-free; the Stop reader tolerates a torn trailing
line (exec-binding.md §3.2). `session_id` comes from this hook's own stdin
payload.

Known parser gaps accepted as best-effort (exec-binding.md §3.3 / R2):
  - heredoc body, `python -c open()` — writes these perform are not
    recognized at all. `sed -i`/`--in-place` IS recognized as of 2026-08-20
    (see `_sed_is_inplace` / `_sed_operands`), with an unexpanded-shell-
    metacharacter guard (`$`, backtick, `*`, `?`) so a variable-shaped operand
    like `"$f"` is skipped rather than recorded as a garbage literal — a
    `sed -i` invocation whose only file operand is such a variable still
    contributes nothing (mode-orchestrator-runs/
    2026-08-19_touched-capture-bash-parse-gap-cd-target/03-review-dev.md F4).
  - `sed`'s `=`-attached script flags (`--expression=EXPR`, `--file=F`) are
    not recognized: `_SED_EXPR_FLAGS` lists only the space-separated
    spellings, so an `=` form falls through the `-`-prefixed skip WITHOUT
    setting `script_supplied`, and `_sed_operands` step 4 then drops the first
    remaining token — the real file — as the script. Measured 2026-08-21:
    `sed -i --expression=s/a/b/ f.md` and `sed -i --file=script.sed f.md` both
    return `[]`, while the space-separated controls return `['f.md']`. This is
    UNDER-capture (a write is missed), the safe direction, and it is accepted
    rather than fixed — recorded here so the parity with the pi-extensions
    sibling, which mirrors this behaviour deliberately (2026-08-20 handoff,
    discovery 4), is an intentional trade and not an oversight on either side.
  - A relative bash write target that resolves only against a directory the
    command `cd`-ed into (never against `STATE_ROOT`) is recorded VERBATIM
    and is, by specification, NOT bindable: this hook holds only `command`,
    and the Bash tool's cwd persists across calls but is not part of this
    hook's payload, so there is no base to join a bare relative token
    against. `normalize_path` only ever STRIPS a prefix, never joins one, so
    no change to `STATE_ROOT`/cwd can fix this (see `normalize_path`). Pinned
    in both directions by `test_touched_capture_bash_scope.py` U7/U7c.
  - Bash verb-loop staging (`mv|cp|rm|tee|sed`) treats an UNQUOTED newline as
    a stage boundary, the same as `&&`/`||`/`;`/a single `|` (2026-08-20,
    `_split_stages`) — a second line of a multi-line command is no longer
    read as an argument of the first line's verb. `extract_redirect_targets`
    is unaffected: it already scans the whole command and already resets
    quote state at every newline (below).
  - That same newline split (`cmd.split('\n')` in `extract_bash_paths`, ABOVE
    `_split_stages`) is NOT quote-aware, so a physical line that is the
    INTERIOR of a multi-line quoted string is promoted to a command of its
    own — a false positive in ANY class, `_projects/…` (the one bindable
    class) included, e.g. a commit message whose body reads as a `mv`:
    `git commit -m "refactor: move notes\nmv _projects/p/project-notes/a.md
    b.md"` now records `_projects/p/project-notes/a.md` and `b.md` though
    nothing was written. `_ShellScan`'s per-newline quote reset does not
    prevent this: the reset only stops a quote from outliving its line; it
    does not stop the newline split from treating that line as its own
    command — the two are different rules. Measured 0 occurrences across
    11,937 commands / 2,755 multi-line commands, 2026-08-20
    (06-review-dev.md §B.2), against a detector whose base rate is
    demonstrated non-zero, so the 0 is discriminating, not an instrument
    that never observes anything. Accepted rather than making the newline
    split quote-aware, because that reintroduces the WORSE failure
    direction the split's own quote reset exists to bound — one unbalanced
    quote swallowing the rest of a multi-line command (see
    `extract_redirect_targets`, below, for that trade already decided, and for
    the 2026-08-21 measurement that re-confirmed it against a sibling repo's
    contrary result).
  - A heredoc body is REMOVED before either scan runs
    (`_strip_heredoc_bodies`), so nothing it contains is captured — including a
    body fed to an interpreter (`bash <<'EOF'`), whose writes are real but
    unrecognized. That is this list's first entry restated, not a new trade:
    scanning bodies at all was the deviation from §3.3, and it cost a measured
    false positive (below). Only a TERMINATED body is removed; an unterminated
    delimiter falls back to the pre-2026-08-20 behaviour so the failure
    direction can never become a lost redirection.
  - Redirection detection IS quote-aware (`extract_redirect_targets`), but it
    does not model command substitution (`$(…)`, backticks). An unbalanced
    quote inside one leaves the scan in a quoted state,
    which silently DROPS later real redirections rather than inventing
    spurious ones. That drop is bounded to the LINE containing the
    unbalanced quote (the scan resets at each newline), so it cannot swallow
    the rest of a multi-line command — see `extract_redirect_targets`, which
    also records the 2026-08-21 attempt to remove that reset on a sibling
    repo's contrary measurement, and the three committed pins that falsified it
    here. A quoted string that genuinely spans a newline is mis-handled by the
    same reset; the regex this replaced mis-handled it too. That residual
    false positive
    is NOT harmless: a bogus path spelling an existing task md resolves and
    gets an uncorrectable `@log` line appended (measured 2026-08-20 — see
    `extract_redirect_targets` for the evidence and the accepted trade).
  - `>|` (noclobber override) is not recognized as a redirection.
  - The `/.capture/` pollutant once observed in a `.touched` ledger is NOT
    produced by redirection parsing and remains unattributed; the most
    plausible producer is the `mv|cp|rm`/`tee` token loop
    (review-2026-08-19-fixes.md §8 A-6, design review F-12). Its recurrence is
    now suppressed regardless of producer by the `_state` exclusion below,
    which drops any recorded path under `_projects/_state/`.

Closed 2026-08-19 (review-2026-08-19-fixes.md §8 A-6): a `>` inside a quoted
string was parsed as a redirection, so `echo "real _state: $BEFORE -> $AFTER"`
recorded `$AFTER`; `/dev/null` and `NUL` are now excluded as null sinks.

Closed 2026-08-20 (project-notes/specs/capture-noise-and-log-clip.md): heredoc
bodies were parsed as shell, so prose `19 -> 31 -> 34` recorded `31` and `34,`
(`_strip_heredoc_bodies`); and the capture subagent's own sidecar write landed
in the ledger it feeds, since PostToolUse fires for subagent calls with the
parent session_id (`_is_state_ledger_path`).

Closed 2026-08-20 (mode-orchestrator-runs/
2026-08-19_touched-capture-bash-parse-gap-cd-target/02-plan.md D1/P1/P2):
`sed -i`/`--in-place` operands are now recognized, subject to the
metacharacter guard above (`_sed_is_inplace`/`_sed_operands`); an unquoted
newline is now a verb-loop stage boundary (`_split_stages`), closing a
measured 196/1,039 (18.9%) false-positive bleed where a second line of a
multi-line command (e.g. a following `echo`) was read as the first line's
verb's argument; and the chain/pipe split is now quote-aware
(`_split_stages`), so a quoted `|`/`;`/`&` inside a `sed` script no longer
shatters a stage that is really one command.
"""
import json
import os
import re
import shlex
import sys


def _find_state_root(start: str) -> str:
    """Nearest ancestor of `start` (inclusive) that holds `_projects/_state`.

    This hook's cwd is the cwd the SESSION was launched in, not necessarily the
    repo root: a session started inside a subdirectory keeps that subdirectory
    as its hook cwd for its whole life (measured on Claude Code 2.1.233 — a `cd`
    inside a Bash tool call does NOT move it, and `CLAUDE_PROJECT_DIR` and the
    payload `cwd` both carry the same launch cwd, so neither is a better
    anchor). Anchoring on `os.getcwd()` alone then resolves STATE_DIR to a
    directory that does not exist, and every write of that session is dropped
    from the ledger with no diagnostic — `.touched` is the sole input to task
    and note resolution, so the whole round's membership set silently loses
    those paths. Walking up re-anchors the ledger location AND the
    relative-path base on the tree that actually holds the state.

    Returns '' when no ancestor qualifies; the caller then falls back to
    `os.getcwd()`, which reproduces the pre-fix early return byte-for-byte. A
    cwd inside an UNRELATED tree that happens to hold `_projects/_state`
    resolves there, but the orphan guard in `main()` (no `<session_id>.json` in
    that state dir) still returns 0, so no foreign ledger is written.
    """
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, '_projects', '_state')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return ''
        d = parent


STATE_ROOT = _find_state_root(os.getcwd()) or os.getcwd()
PROGRESS_ROOT = os.path.join(STATE_ROOT, '_projects')
STATE_DIR = os.path.join(PROGRESS_ROOT, '_state')

WRITE_PATH_KEYS = ('file_path', 'notebook_path')
BASH_FILE_VERBS = {'mv', 'cp', 'rm'}
BASH_TEE = 'tee'
# `sed` is a CONDITIONAL write verb (only when an in-place flag is present,
# see `_sed_is_inplace`), not a member of BASH_FILE_VERBS -- unlike mv/cp/rm
# it has read-only invocations (`sed -n`, a piped `sed`) that must record
# nothing (D1, 02-plan.md §1.1).
_SED_EXPR_FLAGS = ('-e', '--expression', '-f', '--file')
# `$`, a backtick, `*`, `?` all mean the shell was going to expand this sed
# operand before `sed` ever saw it; recording the literal token would record
# shell syntax as a resolved path (e.g. `sed -i '...' "$f"` -> `$f`), which is
# never a real write target (F4, 03-review-dev.md). Scoped to `sed` only --
# mv/cp/rm/tee operand selection is unchanged.
_SED_UNSAFE_CHARS = ('$', '`', '*', '?')
# A redirection-shaped OPERAND token, used to stop operand collection in both
# `_sed_operands` and the verb loop. An optional fd number may precede the `>`
# (`2>err.txt`, `2>&1`), which is why a bare `t.startswith('>')` test is not
# enough: `sed -i 's/a/b/' f.md 2>err.txt` recorded the literal `2>err.txt` as
# a written path, and `... 2>&1` recorded `2>&1`, neither of which is a file
# this command wrote -- a false positive in ANY class, and the mechanism behind
# the `2>&1` fragments seen in Stop-hook `touched:` lines. The same token shape
# reached the mv/cp/rm/tee loop, so this is NOT sed-specific. Reported by the
# pi-extensions sibling (2026-08-20 handoff, discovery 3), reproduced here on
# both paths before the fix. `extract_redirect_targets` captures the real
# target (`err.txt`) separately, so stopping here loses nothing.
# Trade accepted: a file literally named e.g. `2>x.md` is no longer recorded
# from an operand position. `>` is an illegal filename character on Windows and
# needs quoting on POSIX; the sibling took the same trade.
_FD_REDIRECT_RE = re.compile(r'^\d*>')
# A shlex-failing stage's diagnostic is only useful when the stage COULD have
# named a write target -- gating it here, rather than printing unconditionally,
# is what makes the line actionable (§2.3). Extending BASH_FILE_VERBS/BASH_TEE
# extends this set automatically; a future new write verb must still be added
# to the literal `{'sed'}` here in the same edit (R7, 03-review-dev.md).
_SHLEX_DIAGNOSTIC_VERBS = BASH_FILE_VERBS | {BASH_TEE, 'sed'}
# Target token of a shell redirection, matched at the position just after an
# UNQUOTED `>` / `>>` and its trailing whitespace. Same alternation the old
# single-regex form used: a double-quoted, single-quoted, or bare token. (`>|`
# is not recognized — `|` is excluded from the bare-token class, so the match
# simply fails, exactly as before.)
_REDIRECT_TARGET_RE = re.compile(r'"[^"]*"|\'[^\']*\'|[^\s|&;<>()]+')
# Tail of a heredoc operator, matched just after an UNQUOTED `<<`: the optional
# `-` (tab-stripping form, which must be adjacent to the operator), optional
# blanks, then the delimiter word. Same token alternation as the redirect
# target, so `<<EOF`, `<< EOF`, `<<-EOF`, `<<'EOF'` and `<<"EOF"` all parse.
_HEREDOC_TAIL_RE = re.compile(r'(-?)[ \t]*("[^"]*"|\'[^\']*\'|[^\s|&;<>()]+)')
# A `.touched` line under the state sidecar directory. The capture subagent's
# own sidecar write fires PostToolUse with the PARENT session_id, so without
# this the ledger records its own bookkeeping.
_STATE_LEDGER_PREFIX = '_projects/_state/'


def _is_null_sink(target: str) -> bool:
    """True for a redirection target that names a null device, not a file.
    `/dev/null` is an exact POSIX path; `NUL` is a Windows device name and is
    case-insensitive by platform convention, so it is matched case-folded."""
    return target == '/dev/null' or target.casefold() == 'nul'


class _ShellScan:
    """The ONE quote-state scanner in this module (`outside`/`single`/`double`).

    Both `_strip_heredoc_bodies` and `extract_redirect_targets` need "is this
    operator character quoted?", and a second copy of these rules is how the two
    silently drift apart — the newline reset and the backslash rule below were
    each added to fix a measured defect, and only a shared implementation makes
    a later fix reach both callers.

    Rules, unchanged from the inline scan this replaces: quote state resets at
    every newline (an unbalanced quote — an apostrophe in a `#` comment is the
    ordinary case — must not swallow the rest of a multi-line command); a
    backslash escapes the next character everywhere except inside single quotes;
    each quote character is literal inside the other kind.

    `next_outside` reports the next unquoted occurrence of any character in
    `chars` and leaves `pos` just after it. `jump` moves `pos` WITHOUT running
    the state machine over the skipped span — that is what lets a caller step
    over a quoted redirection target without desynchronizing.
    """

    __slots__ = ('cmd', 'pos', 'state')

    def __init__(self, cmd: str) -> None:
        self.cmd = cmd
        self.pos = 0
        self.state = 'outside'

    def next_outside(self, chars: str) -> int:
        cmd = self.cmd
        n = len(cmd)
        i = self.pos
        while i < n:
            ch = cmd[i]
            if ch == '\n':
                self.state = 'outside'  # an unbalanced quote cannot outlive its line
                i += 1
                continue
            if ch == '\\' and self.state != 'single':
                i += 2  # escaped character, whatever it is, is not an operator
                continue
            if self.state == 'outside':
                if ch == '"':
                    self.state = 'double'
                elif ch == "'":
                    self.state = 'single'
                elif ch in chars:
                    self.pos = i + 1
                    return i
            elif self.state == 'double':
                if ch == '"':
                    self.state = 'outside'
            elif ch == "'":  # state == 'single'
                self.state = 'outside'
            i += 1
        self.pos = n
        return -1

    def jump(self, i: int) -> None:
        self.pos = i


def _split_stages(cmd: str) -> list[str]:
    """Quote-aware replacement for `_BASH_CHAIN_SPLIT.split(cmd)` followed by
    a naive `segment.split('|')` (P2, 02-plan.md §S1). Splits `cmd` at an
    UNQUOTED `&&`, `||`, `;`, or a single unquoted `|`. Never splits at a bare
    `&` -- parity with the regex this replaces, which only ever recognized
    `&&`, not a lone `&`, so fd-duplication (`>&N`, `2>&1`) and a
    backgrounding `&` both stay inside their stage exactly as before.

    Reuses `_ShellScan` (the one quote-state scanner in this module) instead
    of a second copy of the quoting rules, so `sed -i 's|a|b|' f` -- shattered
    mid-script by the naive `'|'.split` this replaces -- is now read as one
    stage. That is the prerequisite D1 (`sed -i` recognition) depends on.
    """
    stages: list[str] = []
    scan = _ShellScan(cmd)
    start = 0
    while True:
        i = scan.next_outside('&|;')
        if i < 0:
            break
        ch = cmd[i]
        nxt = cmd[i + 1:i + 2]
        if ch == '&':
            if nxt == '&':
                stages.append(cmd[start:i])
                scan.jump(i + 2)
                start = i + 2
            # A bare `&` is not a stage separator; leave it in the stage and
            # keep scanning from `scan.pos` (already just past it).
            continue
        if ch == '|':
            if nxt == '|':
                stages.append(cmd[start:i])
                scan.jump(i + 2)
                start = i + 2
            else:
                stages.append(cmd[start:i])
                start = i + 1
            continue
        # ch == ';'
        stages.append(cmd[start:i])
        start = i + 1
    stages.append(cmd[start:])
    return stages


def _strip_heredoc_bodies(cmd: str) -> str:
    """Remove terminated heredoc BODIES so their text is never parsed as shell.

    A heredoc body is data, but every scan in this module reads it as command
    text. Measured 2026-08-20: `cat >> index.md <<'EOF'` whose body contained
    the prose `19 -> 31 -> 34` recorded `31` and `34,` as written paths, because
    the two `->` arrows are unquoted `>` characters. That is not cosmetic — a
    phantom target spelling an existing task md resolves in
    `session_progress_capture.resolve_touched_tasks` and gets an UNCORRECTABLE
    `@log` line appended (`@log` is append-only).

    Not capturing writes performed by a heredoc body is already the documented
    contract, not a new trade: `project-notes/specs/exec-binding.md` lists
    `heredoc body` beside `sed -i` and `python -c open()` as an explicitly
    accepted capture gap. Scanning bodies at all was the deviation; this brings
    the code back to the spec. A body fed to an interpreter (`bash <<'EOF'`)
    therefore contributes nothing either — deliberate, and pinned by a test.

    LOSS-AVOIDANCE GUARD, and it is the whole reason this is safe: a body is
    dropped ONLY when its terminator line actually exists. An unterminated
    delimiter falls back to the current behaviour, so the failure direction
    stays "the same false positive we have today" and never becomes "a real
    redirection disappears" — a lost write erases a whole turn's record, which
    this module's docstring records as the worse of the two costs.

    The guard doubles as the safety net for a FALSE `<<` detection: in
    `echo $((1<<2)) > out.txt` the arithmetic shift yields the pseudo-delimiter
    `2`, no line equals it, so nothing is stripped and the real `> out.txt` is
    still captured.

    Stripping is PER DELIMITER. In `cmd <<A <<B` with only `A` terminated,
    `A`'s body is removed and everything from the first unterminated delimiter
    onward is left verbatim, so the guard's promise holds delimiter by delimiter
    rather than collapsing the whole command to the fallback.

    Deliberately NOT modelled (same known-gap list as the rest of the module):
    command substitution, and a quoted string that genuinely spans a newline.
    """
    ops: list[tuple] = []  # (source index, tab-strip?, delimiter)
    scan = _ShellScan(cmd)
    while True:
        i = scan.next_outside('<')
        if i < 0:
            break
        if cmd[i + 1:i + 2] != '<':
            continue  # a plain `<` input redirection
        if cmd[i + 2:i + 3] == '<':
            scan.jump(i + 3)  # `<<<` herestring: no body follows
            continue
        m = _HEREDOC_TAIL_RE.match(cmd, i + 2)
        if not m:
            scan.jump(i + 2)
            continue
        delim = m.group(2).strip('"\'')
        scan.jump(m.end())
        if delim:
            ops.append((i, m.group(1) == '-', delim))
    if not ops:
        return cmd

    lines = cmd.split('\n')
    # Line number each operator sits on; its body starts on the NEXT line.
    ops_by_line: dict = {}
    offset = 0
    bounds = []
    for ln in lines:
        bounds.append(offset)
        offset += len(ln) + 1
    for idx, dash, delim in ops:
        lineno = 0
        for k, start in enumerate(bounds):
            if start > idx:
                break
            lineno = k
        ops_by_line.setdefault(lineno, []).append((dash, delim))

    out: list[str] = []
    queue: list[tuple] = []
    total = len(lines)
    i = 0
    fallback = False
    while i < total:
        if fallback:
            out.append(lines[i])
            i += 1
            continue
        queue.extend(ops_by_line.get(i, ()))
        out.append(lines[i])
        i += 1
        while queue:
            dash, delim = queue[0]
            end = -1
            for k in range(i, total):
                cand = lines[k].rstrip('\r')
                if dash:
                    cand = cand.lstrip('\t')
                if cand == delim:
                    end = k
                    break
            if end < 0:
                # Unterminated: leave this delimiter and everything after it
                # exactly as written (guard above).
                fallback = True
                break
            queue.pop(0)
            i = end + 1  # drop the body AND its terminator line
    return '\n'.join(out)


def extract_redirect_targets(cmd: str) -> list[str]:
    """Return the `>` / `>>` redirection targets of a bash command.

    A single regex cannot do this: it matches a `>` inside a quoted string as
    readily as a real operator, so `echo "a -> b"` recorded `b` as a written
    path (observed live in a `.touched` ledger). This is a three-state
    character scan instead — `outside` / `single` / `double` — with a backslash
    escaping the next character in `outside` and `double` only (POSIX: nothing
    escapes inside single quotes, and each quote character is literal inside
    the other kind of quote).

    Only a `>` seen in `outside` state opens a redirection. It is skipped when
    the next non-space character is `&` (fd duplication `>&N`, `2>&1`); `&>`
    still captures, byte-identically to the old regex. The target token is then
    consumed with _REDIRECT_TARGET_RE and the scan RESUMES AFTER it, so a
    quoted target (`2>> "log f.txt"`) is captured with its quotes stripped and
    does not desynchronize the quote state. Null sinks are dropped.

    Quote state is reset at every newline. Without that, one unpaired `'` —
    an English contraction in a `#` comment (`# don't do this`) is the ordinary
    case, not an exotic one — puts the scan in `single` for the whole rest of a
    multi-line command and silently drops every later redirect target. That is
    a LOSS of capture, and `.touched` is the sole input to task resolution, so
    it is the worse failure direction than the false positive this scan exists
    to remove. Resetting bounds an unbalanced quote to its own line, which
    closes the comment case completely (a `#` comment cannot span a line). The
    cost is a quoted string that genuinely spans lines — which the regex this
    replaced also mis-handled, so nothing regresses. `#` comments are not lexed:
    `#` only introduces a comment at a word boundary, and a strip rule would
    still mis-fire on `file#1`, URL fragments and `$#`.

    The pi-extensions sibling removed its equivalent reset and asked us to
    reconsider (2026-08-20 handoff, discovery 1): ablating it over a
    37,673-command corpus of that machine's session logs, it reported the reset
    kept NO pin alive that does not survive without it, while spuriously
    destroying 3 real-path captures, and that carry-across lost 0 real paths
    across 9,837 multi-line commands. **That result does not transfer here, and
    was measured not to.** Removing this reset on 2026-08-21 broke three
    pre-existing committed pins that encode exactly the comment-apostrophe
    case — `test_touched_capture_quoted_redirect.py` T23 (`# don't do
    this\\nls > out.txt`), T24 (`git commit -m x # it's fine\\nls > o.txt`) and
    T25 (`echo don't\\nfoo > b.txt`), each of which returned `[]` instead of
    its redirect target. The reset was restored the same session. Two
    structural reasons the corpora disagree, both known before the attempt:
    the sibling's scanner serves ONE caller (its stage splitter) while this one
    serves TWO (`_strip_heredoc_bodies` and `extract_redirect_targets`); and
    the two tokenizers differ in when quote mode is entered, so "the same"
    ablation is not the same experiment.

    **Disposition: REJECTED, closed 2026-08-21.** The removal is not deferred
    pending a local corpus ablation — no such ablation is planned, and the
    divergence from the sibling is accepted as permanent. Re-opening needs a
    NEW reason (a measured loss attributable to the reset on this side), not
    the sibling's numbers, which have already been shown not to transfer.

    What that false positive actually costs (measured 2026-08-20; do NOT
    describe it as harmless). A bogus target is NOT simply filtered out
    downstream. If it happens to spell `_projects/<project>/tasks/<status>/
    <name>.md` and `<name>.md` is an existing task basename, then
    `session_progress_capture.resolve_touched_tasks` RESOLVES it and the Stop
    hook's deterministic backstop appends an `@log` line to a task the command
    never touched — and `@log` is append-only, so that line cannot be taken
    back. Verified by feeding a crafted command through the real
    `extract_bash_paths` into the real `resolve_touched_tasks`. The Pi
    counterpart found the same class independently and enumerated further
    consequences on its side (a phantom path can advance a capture round on an
    otherwise empty turn, reach the capture subagent as judgment material, and
    make a genesis-task proposal fire).

    The trade is still taken — a lost write erases a whole turn's record, which
    is worse than a spurious line — but it is a trade between two real costs,
    not between a cost and nothing.

    Deliberately NOT modelled (see the module docstring's known-gap list):
    command substitution, heredoc bodies, `>|`, and a quoted string spanning
    a newline.
    """
    targets: list[str] = []
    scan = _ShellScan(cmd)
    n = len(cmd)
    while True:
        i = scan.next_outside('>')
        if i < 0:
            break
        j = i + 2 if cmd[i + 1:i + 2] == '>' else i + 1
        while j < n and cmd[j].isspace():
            j += 1
        if j < n and cmd[j] == '&':
            continue  # fd duplication (`>&N`, `2>&1`) names no file
        m = _REDIRECT_TARGET_RE.match(cmd, j)
        if m:
            t = m.group(0).strip().strip('"\'')
            if t and not _is_null_sink(t):
                targets.append(t)
            scan.jump(m.end())
    return targets


def _is_state_ledger_path(rel: str) -> bool:
    """True for a normalized path under `_projects/_state/` — hook bookkeeping,
    never task or note material.

    The capture subagent writes its judgment sidecar with the Write tool, and
    PostToolUse fires for subagent tool calls with the PARENT session_id, so the
    sidecar lands in the very ledger the round is built from (observed live).
    Nothing downstream mis-resolves it — `resolve_project_roots` refuses
    `_state` as a project because `_projects/_state/tasks/` does not exist — but
    the raw line count IS the round cursor, and the line is shown verbatim in
    the Stop report, so it costs display clarity and shifts the slice. Excluded
    at capture time rather than at read time so the ledger stays a record of
    work, not of the machinery observing it.

    Scoped to the state directory ONLY: `_projects/<project>/tasks|project-notes`
    writes are exactly what the ledger exists to record.
    """
    return rel.replace('\\', '/').casefold().startswith(_STATE_LEDGER_PREFIX)


def normalize_path(p: str, cwd: str) -> str:
    if not p:
        return p
    p = p.replace('\\', '/')
    cwd_norm = cwd.replace('\\', '/').rstrip('/')
    # Case-insensitive prefix match (Windows paths are case-insensitive).
    if p.lower().startswith(cwd_norm.lower() + '/'):
        return p[len(cwd_norm) + 1:]
    return p


def _sed_is_inplace(args: list[str]) -> bool:
    """True when `args` (a `sed` invocation's tokens, `sed` itself excluded)
    contain an in-place flag: an exact `-i`, a `-i<suffix>` token (GNU
    `-i.bak`), a combined short cluster containing `i` (`-ri`, `-Ei`), or
    `--in-place[=SUFFIX]` (D1 recognition rule, 02-plan.md §1.1). `sed -n`,
    `sed -e ... file` (read-only) and a piped `sed` all return False here --
    that control is the whole reason `sed` is a CONDITIONAL write verb rather
    than an unconditional member of BASH_FILE_VERBS."""
    for tok in args:
        t = tok.strip('"\'')
        if t == '--in-place' or t.startswith('--in-place='):
            return True
        if t.startswith('-') and not t.startswith('--') and len(t) > 1 \
                and 'i' in t[1:]:
            return True
    return False


def _sed_operands(args: list[str]) -> list[str]:
    """File operands of a `sed` invocation, given `args` (its tokens with
    `sed` itself excluded). `BASH_FILE_VERBS`'s "every non-flag argument is a
    path" rule is wrong for `sed`, because the *script* is a non-flag
    argument too (02-plan.md §1.1):

    1. `-e`/`--expression`/`-f`/`--file` consume the NEXT token as the script
       and set `script_supplied`; neither the flag nor its value is ever a
       file operand.
    2. Every other `-`-prefixed token is skipped (a flag).
    3. A redirection-shaped token ends operand collection (`_FD_REDIRECT_RE`,
       which allows an optional fd number: `>`, `>>`, `2>err.txt`, `2>&1`) --
       that is the redirection this hook's own `extract_redirect_targets`
       already captures separately; nothing after it belongs to `sed`.
    4. Of what remains: if a script was supplied via (1), every remaining
       token is a file; otherwise the FIRST remaining token is the script
       itself and is dropped -- `sed -i 's/a/b/'` has no file operand at all,
       and must never record its own script as one (rejected simplification
       in 02-plan.md §1.1: "record the last argument").
    """
    script_supplied = False
    rest: list[str] = []
    i = 0
    n = len(args)
    while i < n:
        t = args[i].strip('"\'')
        if t in _SED_EXPR_FLAGS:
            script_supplied = True
            i += 2
            continue
        if _FD_REDIRECT_RE.match(t):
            break  # redirection handled separately by extract_redirect_targets
        if t.startswith('-'):
            i += 1
            continue
        if not t:
            # An empty token after quote-stripping (`''`/`""`) is BSD/macOS
            # sed's in-place SUFFIX argument, never a file operand -- without
            # this it reached `rest` and step 4's `rest[1:]` dropped the
            # empty string instead of the real script, so the script itself
            # survived into the operand list (F-A, 06-review-dev.md).
            i += 1
            continue
        rest.append(t)
        i += 1
    if not rest:
        return []
    return rest if script_supplied else rest[1:]


def extract_bash_paths(cmd: str) -> list[str]:
    """Return file paths a bash command writes: `>`/`>>` redirection targets,
    `tee` targets, `mv|cp|rm` non-flag args, and a `sed -i`/`--in-place`
    invocation's file operand(s) (`_sed_operands`). Best-effort."""
    if not cmd or not isinstance(cmd, str):
        return []
    # Heredoc bodies are data; strip them BEFORE either scan below so the
    # redirect scan and the verb loop see the same command text and neither
    # mistakes body prose for shell (see `_strip_heredoc_bodies`).
    cmd = _strip_heredoc_bodies(cmd)
    paths: list[str] = []
    # Redirection targets anywhere in the command (quote-aware). Scans the
    # WHOLE command, not per line/stage -- untouched by P1/P2 below.
    paths.extend(extract_redirect_targets(cmd))
    # Verb-based targets. An unquoted newline is a stage boundary (P1) first,
    # then each line is split at an unquoted `&&`/`||`/`;`/single `|` (P2,
    # `_split_stages`) -- so `... | tee f` is still reached, other commands'
    # piped args are not pulled in, and neither split shatters a quoted
    # operator (e.g. the `|` inside a `sed` script). Splitting on `\n` FIRST
    # is a SEPARATE rule from `_ShellScan`'s per-newline quote reset, not a
    # consequence of it: the reset only stops a quote from outliving its
    # line, while this split promotes that line to a command of its own --
    # so a physical line that is the INTERIOR of a multi-line quoted string
    # is read as its own command (a known, measured-rare false positive; see
    # the module docstring's known-gap list).
    for line in cmd.split('\n'):
        for stage in _split_stages(line):
            stage = stage.strip()
            if not stage:
                continue
            try:
                tokens = shlex.split(stage, posix=(os.name != 'nt'))
            except ValueError:
                # Gated (§2.3): a stage that could never have named a write
                # target is not worth a diagnostic line -- of 4,455 measured
                # failures, ~4,452 are grep/echo/uv stages (02-plan.md §0.5).
                first_word = stage.split(None, 1)[0]
                if first_word in _SHLEX_DIAGNOSTIC_VERBS:
                    print(f'[touched_capture] shlex parse error: {stage[:80]}',
                          file=sys.stderr)
                continue
            if not tokens:
                continue
            verb = tokens[0]
            if verb == 'sed':
                if _sed_is_inplace(tokens[1:]):
                    for t in _sed_operands(tokens[1:]):
                        if any(ch in t for ch in _SED_UNSAFE_CHARS):
                            continue  # unexpanded shell metacharacter (F4)
                        paths.append(t)
                continue
            if verb not in BASH_FILE_VERBS and verb != BASH_TEE:
                continue
            for raw in tokens[1:]:
                # Strip quotes: this module lexes with posix=False on this
                # platform (`os.name == 'nt'`), which RETAINS quotes in the
                # token (`shlex.split("mv 'a;b.md' c.md", posix=False)` ->
                # `["mv", "'a;b.md'", "c.md"]`) -- matching the redirect
                # path's existing rule (`extract_redirect_targets`) so a
                # quoted operand is recorded as the literal path, not as a
                # quote-wrapped string that can never match `_PROJECT_RE`
                # downstream (F3, 03-review-dev.md).
                t = raw.strip('"\'')
                if t.startswith('-'):
                    continue
                if _FD_REDIRECT_RE.match(t):
                    break  # redirection handled separately above
                paths.append(t)
    return paths


def extract_paths(tool_input: dict) -> list[str]:
    """Collect write targets from a tool_input. The PostToolUse matcher already
    restricts firing to Write|Edit|NotebookEdit|Bash, so a present `file_path` /
    `notebook_path` is a write target and `command` is a shell command."""
    if not isinstance(tool_input, dict):
        return []
    paths: list[str] = []
    for key in WRITE_PATH_KEYS:
        v = tool_input.get(key)
        if v:
            paths.append(v)
    cmd = tool_input.get('command')
    if cmd:
        paths.extend(extract_bash_paths(cmd))
    return paths


def main() -> int:
    if not os.path.isdir(STATE_DIR):
        return 0
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception:
        return 0
    session_id = data.get('session_id', '')
    if not session_id:
        return 0
    # Only record for sessions that hold taskflow state (avoid orphan files).
    # For subagent/fork tool calls PostToolUse carries the PARENT session_id
    # (exec-binding.md §3.1), whose state file exists.
    if not os.path.exists(os.path.join(STATE_DIR, f'{session_id}.json')):
        return 0

    raw_paths = extract_paths(data.get('tool_input', {}))
    if not raw_paths:
        return 0

    # Repo-relative form must be taken against the tree that holds the state,
    # not against wherever the session was launched (see _find_state_root): an
    # absolute line fails `_PROJECT_RE` in session_progress_capture.py, so
    # `extract_project` returns '' and the line is dropped from BOTH the task
    # and the note resolution. That is why the two hunks are inseparable.
    cwd = STATE_ROOT
    lines: list[str] = []
    seen: set[str] = set()
    for p in raw_paths:
        n = normalize_path(p, cwd)
        if not n or _is_state_ledger_path(n):
            continue
        if n not in seen:
            seen.add(n)
            lines.append(n)
    if not lines:
        return 0

    touched_path = os.path.join(STATE_DIR, f'{session_id}.touched')
    # One append per invocation, built as a single newline-terminated buffer so
    # the write is a single O_APPEND syscall (best-effort atomicity) and the
    # Stop reader can drop a torn trailing line.
    blob = ''.join(f'{ln}\n' for ln in lines)
    try:
        with open(touched_path, 'a', encoding='utf-8') as f:
            f.write(blob)
    except OSError:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
