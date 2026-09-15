#!/usr/bin/env python3
"""
PreToolUse hook: deny a Grep over project-notes/ that is not a `::<name>`
symbol lookup — the corpus scan notes_guidelines.md forbids.

Fires on Grep only (hooks.json). Denies when BOTH hold:

    path    is inside a project-notes/ tree (`_archive/` included)
    pattern does not contain `::`

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
    a subagent's own calls    — the parent's hooks do not reach them

So the guarantee is "a Grep tool corpus scan is impossible", not "a corpus
scan is impossible". The remaining routes stay a wording matter and are
observed, not blocked.

stdlib only: hooks run under `uv run --no-project`.
"""
import json
import sys

NOTES_DIR = 'project-notes'
SYMBOL_MARK = '::'

REASON = (
  'Grep over project-notes/ without `::` is the corpus scan the note rule forbids. '
  'Reads of project-notes/ are exactly three: the injected block, a `::<name>` Grep '
  'for a peer query-lsp returned, and opening a file one of those two named. '
  'If query-lsp did not answer, say the route was unavailable and continue on the '
  'block alone.'
)


def is_corpus_scan(path: str, pattern: str) -> bool:
    """The denied shape: a Grep aimed into project-notes/ that is not looking
    for a `path::symbol` anchor. Both arguments are taken verbatim from
    tool_input; a missing one arrives as ''."""
    if not path:
        return False
    parts = path.replace('\\', '/').split('/')
    if NOTES_DIR not in parts:
        return False

    return SYMBOL_MARK not in pattern


def main() -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception:
        return 0

    tool_input = data.get('tool_input') or {}
    if not is_corpus_scan(tool_input.get('path') or '', tool_input.get('pattern') or ''):
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
