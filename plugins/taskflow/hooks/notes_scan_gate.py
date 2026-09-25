#!/usr/bin/env python3
"""
PreToolUse hook: deny a Grep that searches more than one file of a
project-notes/ tree and is not a `::<name>` symbol lookup — the corpus scan
notes_guidelines.md forbids.

Fires on Grep only (hooks.json). Denies when ALL hold:

    root    the search root — the Grep's path, or its cwd when it has no
            path — lies inside a project-notes/ tree (`_archive/` included)
    scope   that root is not one existing file; a directory, or a root that
            cannot be resolved on disk, counts as the tree (fail closed)
    pattern does not contain `::`

A Grep inside one existing note searches no other note, so it passes, the
same as a Read of that file; denying it only sent agents to Read the file
whole or grep it through Bash (18 of the 20 real denials outside the
deployment probes were single-file, measured 2026-09-24). A relative path is
resolved against the payload cwd, and on Windows an MSYS `/c/...` path is
read as `C:/...`.

Everything else passes. The deny payload is the shape rule-inject already
uses (plugins/rule-inject/hooks/pre_tool_check_rules.py), so this is a new
decision for taskflow's hooks but not a new mechanism for the repo.

What this gate does NOT close, measured in the 2026-09-15 remediation arm:

    Read                      — a note reached via the block or a `::` Grep is
                                opened with Read, and this hook cannot tell that
                                file's provenance, so Read is never denied
    Glob -> Read              — follows from the line above
    Bash grep/rg/cat/find     — no reader-side path extractor exists;
                                touched_capture.extract_bash_paths takes WRITE
                                targets only (`>`/`tee`/`mv|cp|rm`/`sed -i`)

A subagent's own calls DO reach this hook: measured 2026-09-15, a Grep a
general-purpose subagent issued was denied by the parent's registration, the
same way touched_capture already sees subagent writes under the parent's
session_id.

So the guarantee is "a Grep-tool search rooted inside project-notes/ and not
confined to one file is impossible", not "a corpus scan is impossible". A
Grep rooted above the tree (the project directory, the repo root) is not
judged either: judging it would deny ordinary searches of the project. The
remaining routes stay a wording matter and are observed, not blocked.

One deployment fact: the matcher set is fixed when a session starts, so adding
this hook does not take effect in a session already running. Measured the same
day — in a session predating the registration, a Write still fired the
Write|Edit hook while a Grep on project-notes went through untouched.

stdlib only: hooks run under `uv run --no-project`.
"""
import json
import os
import re
import sys

NOTES_DIR = 'project-notes'
SYMBOL_MARK = '::'
MSYS_DRIVE = re.compile(r'^/([A-Za-z])/')

REASON = (
  'Grep over project-notes/ without `::` is the corpus scan the note rule forbids. '
  'Reads of project-notes/ are exactly three: the injected block, a `::<name>` Grep '
  'for a peer query-lsp returned, and opening a file one of those two named. '
  'If query-lsp did not answer, say the route was unavailable and continue on the '
  'block alone.'
)


def native(path: str) -> str:
    """`\\` becomes `/`, and on Windows an MSYS `/c/...` becomes `C:/...`, so
    the path can be checked on disk."""
    p = path.replace('\\', '/')
    if os.name != 'nt':
        return p
    m = MSYS_DRIVE.match(p)
    if not m:
        return p

    return f'{m.group(1).upper()}:/{p[m.end():]}'


def search_root(path: str, cwd: str) -> str:
    """What the Grep searches: its path, resolved against cwd when relative,
    or cwd itself when there is no path. '' when neither is known."""
    if not path:
        return native(cwd)
    p = native(path)
    if os.path.isabs(p) or not cwd:
        return p

    return native(cwd).rstrip('/') + '/' + p


def is_corpus_scan(path: str, pattern: str, cwd: str) -> bool:
    """The denied shape: a Grep rooted inside project-notes/ that searches more
    than one file and is not looking for a `path::symbol` anchor. Arguments are
    taken verbatim from the payload; a missing one arrives as ''."""
    root = search_root(path, cwd)
    if not root:
        return False
    if NOTES_DIR not in root.split('/'):
        return False
    if SYMBOL_MARK in pattern:
        return False

    return not os.path.isfile(root)


def main() -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception:
        return 0

    tool_input = data.get('tool_input') or {}
    path = tool_input.get('path') or ''
    pattern = tool_input.get('pattern') or ''
    if not is_corpus_scan(path, pattern, data.get('cwd') or ''):
        return 0

    payload = {
      'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': REASON,
      }
    }
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.write(b'\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
