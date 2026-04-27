#!/usr/bin/env python3
"""
UserPromptSubmit hook: Manage progress session state file.

First prompt:
  1. Parse pj:<project> from first line of user prompt.
  2. If absent, try to infer project from _projects/<project>/ paths in prompt.
  3. Create state file at _projects/_state/{session_id}.json.
  4. Inject session_id, state_file, current_project into additionalContext.

Subsequent prompts:
  1. If pj:<project> is in prompt, update state file.
  2. Inject current_project from state file into additionalContext.

pj:none explicitly sets project to "" (no project).
State file schema: {"project": "<project_name>"}
"""
import json, sys, os, re

PROGRESS_ROOT = os.path.join(os.getcwd(), '_projects')
STATE_DIR = os.path.join(PROGRESS_ROOT, '_state')
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROUTING_MD = os.path.join(PLUGIN_ROOT, 'prompts', 'project_routing.md')

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
user_prompt = data.get('prompt', '')

# Parse first pj:<project> occurrence anywhere in prompt (at string start or after whitespace).
# VSCode extension prepends IDE attachments (<ide_opened_file> etc.) before user text,
# so strict line-start match would miss them.
pj_match = re.search(r'(?:^|\s)pj:(\S+)', user_prompt)
pj_explicit = None
if pj_match:
  val = pj_match.group(1)
  pj_explicit = '' if val == 'none' else val

if os.path.exists(state_path):
  # Subsequent prompt — update if pj: specified, then inject
  if pj_explicit is not None:
    with open(state_path, 'w', encoding='utf-8') as f:
      json.dump({'project': pj_explicit}, f, ensure_ascii=False)
    project = pj_explicit
  else:
    try:
      with open(state_path, 'r', encoding='utf-8') as f:
        project = json.load(f).get('project', '')
    except Exception:
      project = ''
else:
  # First prompt — create state file
  if pj_explicit is not None:
    project = pj_explicit
  else:
    # Infer from _projects/<project>/ paths in prompt
    path_match = re.search(r'_projects/([^/\s]+)/', user_prompt)
    project = path_match.group(1) if path_match else ''

  os.makedirs(STATE_DIR, exist_ok=True)
  with open(state_path, 'w', encoding='utf-8') as f:
    json.dump({'project': project}, f, ensure_ascii=False)

routing_content = ''
if pj_explicit is not None or project != '':
  try:
    with open(PROJECT_ROUTING_MD, 'r', encoding='utf-8') as f:
      routing_content = f.read()
    # Replace lib/prompts/ paths with plugin absolute paths
    path_replacements = [
      ('lib/prompts/project_router_agent.md', 'project_router_agent.md'),
      ('lib/prompts/progress_guidelines.md', 'progress_guidelines.md'),
      ('lib/prompts/notes_guidelines.md', 'notes_guidelines.md'),
      ('lib/prompts/handoff_guidelines.md', 'handoff_guidelines.md'),
      ('lib/prompts/progress_template.md', 'progress_template.md'),
    ]
    for old, new in path_replacements:
      routing_content = routing_content.replace(
        old,
        os.path.join(PLUGIN_ROOT, 'prompts', new).replace('\\', '/')
      )
    routing_content = '\n\n' + routing_content
  except Exception:
    pass

# Inject index.md content directly so LLM always has project context
# regardless of whether project-router subagent is called or skipped
index_content = ''
if project:
  index_path = os.path.join(PROGRESS_ROOT, project, 'index.md')
  try:
    with open(index_path, 'r', encoding='utf-8') as f:
      index_content = f'\n\n[Project Index: {project}]\n' + f.read()
  except FileNotFoundError:
    pass

# If project is set but progress.md does not exist, surface an ACTION_REQUIRED banner.
# Placed right after the [Progress Session] header so attention does not fall through
# to subagent results or other injected context.
action_required = ''
if project:
  progress_path = os.path.join(PROGRESS_ROOT, project, 'progress.md')
  if not os.path.exists(progress_path):
    action_required = (
      f'\n\n!!ACTION_REQUIRED (preflight): '
      f'`_projects/{project}/progress.md` does not exist. Before starting any user work, '
      f'(1) ask the user to approve scaffold generation; (2) on approval, create '
      f'`_projects/{project}/index.md`, `progress.md`, and `project-notes/index.md`; '
      f'(3) add the matching row to `_projects/index.md`. '
      f'This scaffold generation is allowed even inside Plan mode (treated equivalently to the plan file).'
    )

result = {
  'hookSpecificOutput': {
    'hookEventName': 'UserPromptSubmit',
    'additionalContext': f'[Progress Session] session_id={session_id} state_file={state_path} current_project={project}{action_required}{index_content}{routing_content}'
  }
}

sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
sys.stdout.buffer.write(b'\n')
sys.exit(0)
