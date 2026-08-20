#!/usr/bin/env python3
"""
SessionStart hook (matcher: compact): reset injection flags in state file.

When Claude Code auto-compacts the conversation, the additionalContext
injected by session_init.py is lost (compaction summarizes conversation
history; hook output is not re-attached). This hook fires on the
SessionStart event with reason=compact and resets injection flags so
the next UserPromptSubmit turn re-injects static_rules, project index,
full guidelines, and the per-project rules.md primer.

State file location: _projects/_state/{session_id}.json
Resets: rules_loaded, indexed_project, guidelines_loaded, project_rules_indexed.
All other fields are preserved.

project_rules_indexed MUST be reset here: compaction summarizes away the rules.md
primer body injected at project switch, so without this reset the state would
still read "primed" and only the `##` manifest (headings, no bodies) would
recur — the full rule text would never re-enter context for the rest of the
session.
"""
import json, sys, os

# Sibling import: hook scripts run standalone (no package context), so this
# file's own directory goes on sys.path before importing the shared helper.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from touched_capture import _find_state_root  # noqa: E402

# `_find_state_root` (touched_capture.py — IMPORTED, never copied) walks up from
# the cwd to the nearest ancestor holding `_projects/_state`. A session launched
# in a repo subdirectory keeps that subdirectory as its hook cwd for its whole
# life, so a cwd-direct root resolves to a tree that does not exist and the
# `os.path.exists(state_path)` gate below never finds the state json to reset.
# `or os.getcwd()` reproduces the pre-change value byte-for-byte when no
# ancestor qualifies. Decision record: mode-orchestrator-runs/
# 2026-08-20_remaining-hooks-cwd-dependence/02a-decision.md (option (b));
# scope: that run's 02-plan.md §3.
STATE_ROOT = _find_state_root(os.getcwd()) or os.getcwd()
PROGRESS_ROOT = os.path.join(STATE_ROOT, '_projects')
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
state['indexed_project'] = ''
state['guidelines_loaded'] = False
state['project_rules_indexed'] = ''

with open(state_path, 'w', encoding='utf-8') as f:
  json.dump(state, f, ensure_ascii=False)

sys.exit(0)
