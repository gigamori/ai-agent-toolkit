#!/usr/bin/env python3
"""read_capture.py — PostToolUse(Read) hook: append the read file's repo-relative
path to `<STATE_DIR>/<session_id>.reads`, a per-session ledger of what the
session has looked at. session_init.py reads its tail as a location signal for
the project-notes push (notes_push.py).

Deliberately a SEPARATE ledger from `.touched`: that file's raw line count is
the Stop hook's round cursor (session_progress_capture.py), so a Read line
there would open a round on a read-only turn. This hook never writes
`.touched`.

The ledger is truncated to its last READS_MAX lines on every append — the
consumer only ever wants the recent tail, and a long session must not grow a
file without bound. Root resolution, path normalization and the two
exclusions (`_projects/_state/`, `tmp/`) are IMPORTED from touched_capture.py
so the two ledgers spell paths identically.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from touched_capture import (  # noqa: E402
    STATE_DIR,
    STATE_ROOT,
    _is_scratch_path,
    _is_state_ledger_path,
    normalize_path,
)

READS_MAX = 200
READ_PATH_KEY = 'file_path'
_ABS_RE = re.compile(r'^(?:/|[A-Za-z]:)')


def append_read(ledger_path: str, rel: str, keep: int = READS_MAX) -> None:
    """Append one line, then keep only the last `keep` lines."""
    try:
        with open(ledger_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = [ln for ln in f.read().split('\n') if ln]
    except OSError:
        lines = []
    lines.append(rel)
    lines = lines[-keep:]
    tmp = ledger_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(''.join(f'{ln}\n' for ln in lines))
    os.replace(tmp, ledger_path)


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
    if not os.path.exists(os.path.join(STATE_DIR, f'{session_id}.json')):
        return 0

    raw = data.get('tool_input', {}).get(READ_PATH_KEY, '')
    if not isinstance(raw, str) or not raw:
        return 0
    rel = normalize_path(raw, STATE_ROOT)
    if not rel or _ABS_RE.match(rel) or _is_state_ledger_path(rel) or _is_scratch_path(rel):
        return 0

    try:
        append_read(os.path.join(STATE_DIR, f'{session_id}.reads'), rel)
    except OSError:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
