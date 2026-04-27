#!/usr/bin/env python3
"""
UserPromptSubmit hook: detect `mode:<name>` slug in user prompt and inject
the corresponding mode definition (+ _common.md) as additionalContext.

Behavior:
  - No slug found -> exit 0 with no output (baseline LLM behavior is preserved).
  - Slug found but mode file missing -> exit 0 with no output (unknown mode is ignored).
  - Slug found and file exists -> emit additionalContext containing
    the mode-specific definition followed by the common rules.

Slug syntax:
  - `mode:<name>` where <name> matches [a-z][a-z0-9_-]*
  - Detected at string start or after whitespace (mirrors taskflow's pj: parsing)
  - Only the first occurrence is consumed; subsequent matches are ignored.

UTF-8 BOM tolerant for Cursor compatibility.
"""
import json
import os
import re
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODES_DIR = os.path.join(PLUGIN_ROOT, 'prompts', 'modes')
COMMON_FILE = os.path.join(MODES_DIR, '_common.md')

try:
  data = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
except Exception:
  sys.exit(0)

prompt = data.get('prompt', '')
if not prompt:
  sys.exit(0)

m = re.search(r'(?:^|\s)mode:([a-z][a-z0-9_-]*)', prompt)
if not m:
  sys.exit(0)

mode_name = m.group(1)
mode_file = os.path.join(MODES_DIR, f'{mode_name}.md')
if not os.path.isfile(mode_file):
  sys.exit(0)

try:
  with open(mode_file, 'r', encoding='utf-8') as f:
    mode_content = f.read().strip()
except Exception:
  sys.exit(0)

common_content = ''
if os.path.isfile(COMMON_FILE):
  try:
    with open(COMMON_FILE, 'r', encoding='utf-8') as f:
      common_content = f.read().strip()
  except Exception:
    common_content = ''

parts = [f'[Mode Active: {mode_name}]', mode_content]
if common_content:
  parts.append(common_content)
additional_context = '\n\n'.join(parts)

result = {
  'hookSpecificOutput': {
    'hookEventName': 'UserPromptSubmit',
    'additionalContext': additional_context
  }
}

sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
sys.stdout.buffer.write(b'\n')
sys.exit(0)
