#!/usr/bin/env python3
"""
Stop hook: Sync plan files and memory files to _projects/<project>/.

Reads project state from _projects/_state/{session_id}.json.
If a project is set, copies:
  - Recently modified plan files from $CLAUDE_CONFIG_DIR (default ~/.claude)/plans/
    -> _projects/<project>/plans/
  - Recently modified memory files from
    $CLAUDE_CONFIG_DIR (default ~/.claude)/projects/.../memory/
    -> _projects/<project>/memory/

Only copies files modified within the last 10 minutes to avoid stale copies.
Files in _projects/<project>/plans/ and _projects/<project>/memory/ are
archival copies — Claude Code must NOT treat them as authoritative sources.
"""
import json, sys, os, shutil, glob, time

# Sibling import: hook scripts run standalone (no package context), so this
# file's own directory goes on sys.path before importing the shared helper.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from touched_capture import _find_state_root  # noqa: E402

# `_find_state_root` (touched_capture.py — IMPORTED, never copied) walks up from
# the cwd to the nearest ancestor holding `_projects/_state`. A session launched
# in a repo subdirectory keeps that subdirectory as its hook cwd for its whole
# life, so a cwd-direct root resolves to a tree that does not exist and this
# hook exits at the guard below without syncing anything. `or os.getcwd()`
# reproduces the pre-change value byte-for-byte when no ancestor qualifies.
# Decision record: mode-orchestrator-runs/
# 2026-08-20_remaining-hooks-cwd-dependence/02a-decision.md (option (b));
# scope: that run's 02-plan.md §3.
STATE_ROOT = _find_state_root(os.getcwd()) or os.getcwd()
PROGRESS_ROOT = os.path.join(STATE_ROOT, '_projects')
STATE_DIR = os.path.join(PROGRESS_ROOT, '_state')
# CC treats CLAUDE_CONFIG_DIR literally (no ~-expansion; relative values resolve
# against the process cwd). This hook runs inside the writer's process tree, so
# it inherits the writer's env and resolves a single dir.
_cfg = os.environ.get('CLAUDE_CONFIG_DIR', '').strip()
CLAUDE_DIR = os.path.abspath(_cfg) if _cfg else os.path.expanduser('~/.claude')
PLANS_DIR = os.path.join(CLAUDE_DIR, 'plans')
# Dynamically compute memory dir from CWD encoding
_cwd = os.getcwd().replace('\\', '/')
_encoded = _cwd.lower().replace(':', '-').replace('/', '-')
MEMORY_DIR = os.path.join(CLAUDE_DIR, 'projects', _encoded, 'memory')
STALENESS_THRESHOLD = 600  # 10 minutes

# Guard: skip if _projects/ does not exist in CWD
if not os.path.isdir(PROGRESS_ROOT):
  sys.exit(0)

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

project = state.get('project', '')
if not project:
  sys.exit(0)

project_dir = os.path.join(PROGRESS_ROOT, project)
if not os.path.isdir(project_dir):
  sys.exit(0)

now = time.time()


def sync_recent_files(src_dir, dest_subdir, pattern='*.md'):
  """Copy recently modified files from src_dir to project_dir/dest_subdir."""
  if not os.path.isdir(src_dir):
    return
  dest_dir = os.path.join(project_dir, dest_subdir)
  for filepath in glob.glob(os.path.join(src_dir, pattern)):
    mtime = os.path.getmtime(filepath)
    if now - mtime <= STALENESS_THRESHOLD:
      os.makedirs(dest_dir, exist_ok=True)
      shutil.copy2(filepath, dest_dir)


# Sync plan files
sync_recent_files(PLANS_DIR, 'plans')

# Sync memory files
sync_recent_files(MEMORY_DIR, 'memory')

sys.exit(0)
