#!/usr/bin/env python3
"""
PostToolUse hook: record this session's file writes to a per-session
`<STATE_DIR>/<session_id>.touched` ledger (one normalized repo-relative path
per line, append-only, lock-free).

Replaces the old Stop-hook jsonl-scan + git-diff detection
(project-notes/specs/exec-binding.md §3.1/§3.2). Fires for
Write / Edit / NotebookEdit and file-touching Bash (`mv|cp|rm`, shell
redirection `>`/`>>`, and `tee`).

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
  - `sed -i`, heredoc body, `python -c open()` — writes these perform are not
    recognized at all.
  - Redirection detection IS quote-aware (`extract_redirect_targets`), but it
    models neither command substitution (`$(…)`, backticks) nor heredoc
    bodies. An unbalanced quote inside one leaves the scan in a quoted state,
    which silently DROPS later real redirections rather than inventing
    spurious ones. That drop is now bounded to the LINE containing the
    unbalanced quote (the scan resets at each newline), so it cannot swallow
    the rest of a multi-line command — see `extract_redirect_targets`. A quoted
    string that genuinely spans a newline is mis-handled by the same reset;
    the regex this replaced mis-handled it too.
  - `>|` (noclobber override) is not recognized as a redirection.
  - The `/.capture/` pollutant once observed in a `.touched` ledger is NOT
    produced by redirection parsing and remains unattributed; the most
    plausible producer is the `mv|cp|rm`/`tee` token loop
    (review-2026-08-19-fixes.md §8 A-6, design review F-12).

Closed 2026-08-19 (review-2026-08-19-fixes.md §8 A-6): a `>` inside a quoted
string was parsed as a redirection, so `echo "real _state: $BEFORE -> $AFTER"`
recorded `$AFTER`; `/dev/null` and `NUL` are now excluded as null sinks.
"""
import json
import os
import re
import shlex
import sys

PROGRESS_ROOT = os.path.join(os.getcwd(), '_projects')
STATE_DIR = os.path.join(PROGRESS_ROOT, '_state')

WRITE_PATH_KEYS = ('file_path', 'notebook_path')
BASH_FILE_VERBS = {'mv', 'cp', 'rm'}
BASH_TEE = 'tee'
# Split a bash command at chain operators so verb/redirect extraction in one
# segment does not bleed into another.
_BASH_CHAIN_SPLIT = re.compile(r'\s*(?:&&|\|\||;)\s*')
# Target token of a shell redirection, matched at the position just after an
# UNQUOTED `>` / `>>` and its trailing whitespace. Same alternation the old
# single-regex form used: a double-quoted, single-quoted, or bare token. (`>|`
# is not recognized — `|` is excluded from the bare-token class, so the match
# simply fails, exactly as before.)
_REDIRECT_TARGET_RE = re.compile(r'"[^"]*"|\'[^\']*\'|[^\s|&;<>()]+')


def _is_null_sink(target: str) -> bool:
    """True for a redirection target that names a null device, not a file.
    `/dev/null` is an exact POSIX path; `NUL` is a Windows device name and is
    case-insensitive by platform convention, so it is matched case-folded."""
    return target == '/dev/null' or target.casefold() == 'nul'


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

    Deliberately NOT modelled (see the module docstring's known-gap list):
    command substitution, heredoc bodies, `>|`, and a quoted string spanning
    a newline.
    """
    targets: list[str] = []
    state = 'outside'
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == '\n':
            state = 'outside'  # an unbalanced quote cannot outlive its line
            i += 1
            continue
        if ch == '\\' and state != 'single':
            i += 2  # escaped character, whatever it is, is not an operator
            continue
        if state == 'outside':
            if ch == '"':
                state = 'double'
            elif ch == "'":
                state = 'single'
            elif ch == '>':
                j = i + 2 if cmd[i + 1:i + 2] == '>' else i + 1
                while j < n and cmd[j].isspace():
                    j += 1
                if j < n and cmd[j] != '&':
                    m = _REDIRECT_TARGET_RE.match(cmd, j)
                    if m:
                        t = m.group(0).strip().strip('"\'')
                        if t and not _is_null_sink(t):
                            targets.append(t)
                        i = m.end()
                        continue
        elif state == 'double':
            if ch == '"':
                state = 'outside'
        elif ch == "'":  # state == 'single'
            state = 'outside'
        i += 1
    return targets


def normalize_path(p: str, cwd: str) -> str:
    if not p:
        return p
    p = p.replace('\\', '/')
    cwd_norm = cwd.replace('\\', '/').rstrip('/')
    # Case-insensitive prefix match (Windows paths are case-insensitive).
    if p.lower().startswith(cwd_norm.lower() + '/'):
        return p[len(cwd_norm) + 1:]
    return p


def extract_bash_paths(cmd: str) -> list[str]:
    """Return file paths a bash command writes: `>`/`>>` redirection targets,
    `tee` targets, and `mv|cp|rm` non-flag args. Best-effort."""
    if not cmd or not isinstance(cmd, str):
        return []
    paths: list[str] = []
    # Redirection targets anywhere in the command (quote-aware).
    paths.extend(extract_redirect_targets(cmd))
    # Verb-based targets, parsed per chain segment then per pipe stage (so
    # `... | tee f` is reached and other commands' piped args are not pulled in).
    for segment in _BASH_CHAIN_SPLIT.split(cmd):
        for stage in segment.split('|'):
            stage = stage.strip()
            if not stage:
                continue
            try:
                tokens = shlex.split(stage, posix=(os.name != 'nt'))
            except ValueError:
                print(f'[touched_capture] shlex parse error: {stage[:80]}',
                      file=sys.stderr)
                continue
            if not tokens:
                continue
            verb = tokens[0]
            if verb not in BASH_FILE_VERBS and verb != BASH_TEE:
                continue
            for t in tokens[1:]:
                if t.startswith('-'):
                    continue
                if t == '>>' or t.startswith('>'):
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

    cwd = os.getcwd()
    lines: list[str] = []
    seen: set[str] = set()
    for p in raw_paths:
        n = normalize_path(p, cwd)
        if n and n not in seen:
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
