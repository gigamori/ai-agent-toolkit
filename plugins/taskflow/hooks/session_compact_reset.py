#!/usr/bin/env python3
"""
SessionStart hook (matcher: compact): reset rules_loaded in state file.

When Claude Code auto-compacts the conversation, the additionalContext
injected by session_init.py is lost (compaction summarizes conversation
history; hook output is not re-attached). This hook fires on the
SessionStart event with reason=compact and resets the `rules_loaded`
flag so the next UserPromptSubmit turn re-injects static_rules.

State file location: _projects/_state/{session_id}.json
Only touches `rules_loaded`; all other fields are preserved.
"""
import json, sys, os

PROGRESS_ROOT = os.path.join(os.getcwd(), '_projects')
STATE_DIR = os.path.join(PROGRESS_ROOT, '_state')

try:
  data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
except Exception:
  sys.exit(0)

session_id = data.get('session_id', '')
if not session_id:
  sys.exit(0)

state_path = os.path.join(STATE_DIR, f'{session_id}.json')
if not os.path.exists(state_path):
  sys.exit(0)

try:
  with open(state_path, 'r', encoding='utf-8') as f:
    state = json.load(f)
except Exception:
  sys.exit(0)

if not isinstance(state, dict):
  sys.exit(0)

state['rules_loaded'] = False

with open(state_path, 'w', encoding='utf-8') as f:
  json.dump(state, f, ensure_ascii=False)

sys.exit(0)
