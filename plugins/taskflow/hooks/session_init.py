#!/usr/bin/env python3
"""
UserPromptSubmit hook: smart session/project context injection (v2.2).

Injection blocks per turn (decided from state file flags):

  - session_info       (always): [Progress Session] header (~50 tok)
  - static_rules       (once per session): project_routing.md (~1600 tok)
  - project_index      (on project switch): _projects/<project>/index.md (~250 tok)
  - guidelines_full    (once per session): 3 guidelines files (~3000 tok)
  - guidelines_reminder(subsequent turns): keyword reminder (~150 tok)
  - action_req         (every turn while progress.md missing): scaffold banner

State file schema (v2.4):
  {
    "project":            "<current active project>",
    "rules_loaded":       <bool — static_rules injected this session>,
    "indexed_project":    "<last project for which project_index was injected>",
    "guidelines_loaded":  <bool — full guidelines injected this session>,
    "origin":             "cc"  — generator identifier (Claude Code),
    "parent_session_id":  "<parent session id if forked, absent otherwise>"
  }

Backward compat: older state is loaded with safe defaults
(rules_loaded=False, indexed_project="", guidelines_loaded=False),
causing one full re-injection on the first turn after upgrade.

Special tokens in user prompt:
  pj:<name>   set/switch project
  pj:none     clear project
  pj:?        discovery (list projects, independent of current project)
  norouter    bypass hook entirely for this turn
"""
import json, sys, os, re, glob

PROGRESS_ROOT = os.path.join(os.getcwd(), '_projects')
STATE_DIR = os.path.join(PROGRESS_ROOT, '_state')
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROUTING_MD = os.path.join(PLUGIN_ROOT, 'prompts', 'project_routing.md')
GUIDELINES_FILES = [
    os.path.join(PLUGIN_ROOT, 'prompts', 'progress_guidelines.md'),
    os.path.join(PLUGIN_ROOT, 'prompts', 'notes_guidelines.md'),
    os.path.join(PLUGIN_ROOT, 'prompts', 'tasks_guidelines.md'),
]
GUIDELINES_REMINDER_MD = os.path.join(PLUGIN_ROOT, 'prompts', 'guidelines_reminder.md')

def detect_parent_session(transcript_path, session_id):
  """Detect parent session_id for forked sessions via shared message uuid.

  Fork copies all parent JSONL entries (with sessionId rewritten) but preserves
  message uuids. If the first entry's uuid exists in another recent JSONL in
  the same directory, that file's session is the parent.

  Returns parent session_id string, or None if not a fork.
  """
  try:
    with open(transcript_path, 'r', encoding='utf-8') as f:
      first_line = f.readline()
    first_entry = json.loads(first_line)
    target_uuid = first_entry.get('uuid')
    if not target_uuid:
      return None
  except (OSError, json.JSONDecodeError):
    return None

  directory = os.path.dirname(transcript_path)
  candidates = sorted(
    glob.glob(os.path.join(directory, '*.jsonl')),
    key=os.path.getmtime, reverse=True
  )[:6]

  for path in candidates:
    if os.path.abspath(path) == os.path.abspath(transcript_path):
      continue
    try:
      with open(path, 'r', encoding='utf-8') as f:
        line = f.readline()
      entry = json.loads(line)
      if entry.get('uuid') == target_uuid:
        return os.path.splitext(os.path.basename(path))[0]
    except (OSError, json.JSONDecodeError):
      continue
  return None

# Bootstrap _projects/ root if missing (replaces taskflow:init skill).
if not os.path.isdir(PROGRESS_ROOT):
  os.makedirs(STATE_DIR, exist_ok=True)
  index_md = os.path.join(PROGRESS_ROOT, 'index.md')
  if not os.path.exists(index_md):
    with open(index_md, 'w', encoding='utf-8') as f:
      f.write('| Project | Description | Target |\n|---------|-------------|--------|\n')

try:
  data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
except Exception:
  sys.exit(0)

session_id = data.get('session_id', '')
if not session_id:
  sys.exit(0)

state_path = os.path.join(STATE_DIR, f'{session_id}.json')
transcript_path = data.get('transcript_path', '')
user_prompt = data.get('prompt', '')

# norouter bypass: total skip of taskflow for this turn.
if re.search(r'(?:^|\s)norouter(?:\s|$)', user_prompt):
  sys.exit(0)

# Parse first pj:<project> (anywhere, after start or whitespace).
pj_match = re.search(r'(?:^|\s)pj:(\S+)', user_prompt)
pj_explicit = None
pj_discovery = False
if pj_match:
  val = pj_match.group(1)
  if val == '?':
    pj_discovery = True
  else:
    pj_explicit = '' if val == 'none' else val

# Load existing state with safe defaults for missing / corrupted fields (Q5).
# `loaded` keeps the full raw dict so that unrelated fields written by other
# components (e.g., `progress_capture_done` from session_progress_capture.py)
# survive the persist step at the end of this hook.
loaded = {}
is_new_session = not os.path.exists(state_path)
if not is_new_session:
  try:
    with open(state_path, 'r', encoding='utf-8') as f:
      data = json.load(f)
      if isinstance(data, dict):
        loaded = data
  except Exception:
    pass

# Fork detection: on first turn of a new session, check if this session was
# forked from another by comparing message uuids in JSONL transcripts.
# If a parent is found, inherit its project from the parent state file.
if is_new_session:
  parent_id = detect_parent_session(
    transcript_path, session_id
  ) if transcript_path else None
  if parent_id:
    loaded['parent_session_id'] = parent_id
    parent_state_path = os.path.join(STATE_DIR, f'{parent_id}.json')
    try:
      with open(parent_state_path, 'r', encoding='utf-8') as f:
        parent_state = json.load(f)
      if isinstance(parent_state, dict) and parent_state.get('project'):
        loaded['project'] = parent_state['project']
        # Find tasks the parent session was working on by scanning @log regions
        # for the parent session_id in 1_in_progress/ tasks.
        parent_tasks = []
        tasks_dir = os.path.join(
          PROGRESS_ROOT, parent_state['project'], 'tasks', '1_in_progress'
        )
        if os.path.isdir(tasks_dir):
          for fname in os.listdir(tasks_dir):
            if not fname.endswith('.md'):
              continue
            fpath = os.path.join(tasks_dir, fname)
            try:
              with open(fpath, 'r', encoding='utf-8') as tf:
                content = tf.read()
              if parent_id[:8] in content:
                parent_tasks.append(fname)
            except OSError:
              continue
        if parent_tasks:
          loaded['inherited_tasks'] = parent_tasks
    except Exception:
      pass

state = {
  'project': loaded.get('project', '') or '',
  'rules_loaded': bool(loaded.get('rules_loaded', False)),
  'indexed_project': loaded.get('indexed_project', '') or '',
  'guidelines_loaded': bool(loaded.get('guidelines_loaded', False)),
  'origin': loaded.get('origin', 'cc'),
}

# Resolve current project for this turn (before re-entry reset check).
if pj_explicit is not None:
  current_project = pj_explicit
elif state['project']:
  current_project = state['project']
else:
  # First-turn heuristic: infer from _projects/<x>/ path in prompt.
  path_match = re.search(r'_projects/([^/\s]+)/', user_prompt)
  current_project = path_match.group(1) if path_match else ''

# Project re-entry: reset injection flags when transitioning from empty to active.
# Without this, pj:none → pj:<name> would leave rules_loaded=True and skip injection.
if current_project and not state['project']:
  state['rules_loaded'] = False
  state['guidelines_loaded'] = False
  state['indexed_project'] = ''

# Decide which blocks to inject.
#   inject_rules: user is engaging with taskflow AND rules not yet loaded this session.
#   inject_index: project is set AND it differs from the last indexed project.
inject_rules = ((not state['rules_loaded']) and bool(current_project)) or pj_discovery
inject_index = current_project != '' and current_project != state['indexed_project']
inject_guidelines_full = (not state['guidelines_loaded']) and bool(current_project)
inject_guidelines_reminder = state['guidelines_loaded'] and bool(current_project)

# Build static_rules block.
routing_content = ''
if inject_rules:
  try:
    with open(PROJECT_ROUTING_MD, 'r', encoding='utf-8') as f:
      routing_content = f.read()
    path_replacements = [
      ('taskflow/prompts/project_router_agent.md', 'project_router_agent.md'),
      ('taskflow/prompts/progress_guidelines.md', 'progress_guidelines.md'),
      ('taskflow/prompts/notes_guidelines.md', 'notes_guidelines.md'),
      ('taskflow/prompts/tasks_guidelines.md', 'tasks_guidelines.md'),
      ('taskflow/prompts/progress_template.md', 'progress_template.md'),
    ]
    for old, new in path_replacements:
      routing_content = routing_content.replace(
        old,
        os.path.join(PLUGIN_ROOT, 'prompts', new).replace('\\', '/')
      )
    routing_content = '\n\n' + routing_content
  except Exception:
    pass

# Build guidelines block (full on first turn, reminder on subsequent turns).
guidelines_content = ''
if inject_guidelines_full:
  parts = []
  for gf in GUIDELINES_FILES:
    try:
      with open(gf, 'r', encoding='utf-8') as f:
        parts.append(f.read())
    except Exception:
      pass
  if parts:
    guidelines_content = '\n\n' + '\n\n'.join(parts)
elif inject_guidelines_reminder:
  try:
    with open(GUIDELINES_REMINDER_MD, 'r', encoding='utf-8') as f:
      guidelines_content = '\n\n' + f.read()
  except Exception:
    pass

# Build project_index block.
index_content = ''
if inject_index:
  index_path = os.path.join(PROGRESS_ROOT, current_project, 'index.md')
  try:
    with open(index_path, 'r', encoding='utf-8') as f:
      index_content = f'\n\n[Project Index: {current_project}]\n' + f.read()
  except FileNotFoundError:
    pass

# Persist updated state. We mark rules_loaded and indexed_project regardless of
# read success — if a file is genuinely broken, we don't want to retry every turn.
# Recovery path: user re-issues pj:<name> or fixes the file then resets state.
#
# IMPORTANT: start from `loaded` (the full raw dict) so unrelated fields written
# by other components survive. Previously we constructed a 3-field dict from
# scratch, which silently dropped `progress_capture_done` (set by the Stop hook)
# and caused the Stop hook to re-fire every turn.
new_state = dict(loaded)
new_state['project'] = current_project if not pj_discovery else state['project']
new_state['rules_loaded'] = state['rules_loaded'] or inject_rules
new_state['indexed_project'] = current_project
new_state['guidelines_loaded'] = state['guidelines_loaded'] or inject_guidelines_full
new_state['origin'] = state['origin']
os.makedirs(STATE_DIR, exist_ok=True)
with open(state_path, 'w', encoding='utf-8') as f:
  json.dump(new_state, f, ensure_ascii=False)

# ACTION_REQUIRED banner: every turn while the project is set but progress.md is missing.
# Kept per-turn (not gated by state flags) so the user / LLM doesn't forget.
action_required = ''
if current_project:
  progress_path = os.path.join(PROGRESS_ROOT, current_project, 'progress.md')
  if not os.path.exists(progress_path):
    action_required = (
      f'\n\n!!ACTION_REQUIRED (preflight): '
      f'`_projects/{current_project}/progress.md` does not exist. Before starting any user work, '
      f'(1) ask the user to approve scaffold generation; (2) on approval, create '
      f'`_projects/{current_project}/index.md`, `progress.md`, and `project-notes/index.md`; '
      f'(3) add the matching row to `_projects/index.md`. '
      f'This scaffold generation is allowed even inside Plan mode (treated equivalently to the plan file). '
      f'Response frontmatter lines (e.g. [pj:{current_project}]) still apply during this preflight.'
    )

# Fork context: on the first turn of a forked session, tell the LLM which
# tasks were inherited so it continues working on them.
fork_context = ''
if is_new_session and loaded.get('inherited_tasks'):
  task_list = ', '.join(loaded['inherited_tasks'])
  fork_context = (
    f'\n\n[Forked Session] parent_session={loaded.get("parent_session_id", "unknown")} '
    f'inherited_tasks={task_list} — Continue working on these tasks. '
    f'Log entries in task files should use the current session_id={session_id}.'
  )

# Suppress LLM-facing context entirely when no project is active and not in discovery mode.
# Saves ~50 tok/turn and prevents the [Progress Session] header from triggering router invocation.
if not current_project and not pj_discovery:
  result = {}
else:
  result = {
    'hookSpecificOutput': {
      'hookEventName': 'UserPromptSubmit',
      'additionalContext': f'[Progress Session] session_id={session_id} state_file={state_path} current_project={current_project}{fork_context}{action_required}{index_content}{routing_content}{guidelines_content}'
    }
  }

sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
sys.stdout.buffer.write(b'\n')
sys.exit(0)
