#!/usr/bin/env python3
"""
Stop hook: Sync plan files and memory files to _projects/<project>/.

Reads project state from _projects/_state/{session_id}.json.
If a project is set, copies:
  - Recently modified plan files from ~/.claude/plans/ -> _projects/<project>/plans/
  - Recently modified memory files from ~/.claude/projects/.../memory/ -> _projects/<project>/memory/

Only copies files modified within the last 10 minutes to avoid stale copies.
Files in _projects/<project>/plans/ and _projects/<project>/memory/ are
archival copies — Claude Code must NOT treat them as authoritative sources.
"""
import json, sys, os, shutil, glob, time

PROGRESS_ROOT = os.path.join(os.getcwd(), '_projects')
STATE_DIR = os.path.join(PROGRESS_ROOT, '_state')
PLANS_DIR = os.path.expanduser('~/.claude/plans')
# Dynamically compute memory dir from CWD encoding
_cwd = os.getcwd().replace('\\', '/')
_encoded = _cwd.lower().replace(':', '-').replace('/', '-')
MEMORY_DIR = os.path.expanduser(f'~/.claude/projects/{_encoded}/memory')
STALENESS_THRESHOLD = 600  # 10 minutes

# Guard: skip if _projects/ does not exist in CWD
if not os.path.isdir(PROGRESS_ROOT):
  sys.exit(0)

try:
  data = json.loads(sys.stdin.read())
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
