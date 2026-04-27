#!/usr/bin/env python3
"""
UserPromptSubmit hook: detect `mode:<name>` and/or `role:<name>` slugs
in the user prompt, then inject the framework meta + active Role/Mode
declaration + (when mode is set) matching mode rules + common rules
as additionalContext.

Behavior:
  - Neither `mode:` nor `role:` present -> exit 0 with no output (baseline
    LLM behavior is preserved).
  - `role:` only -> emit meta + `Role:` line (no Mode line, no common).
  - `mode:` only -> emit meta + `Mode:` line + mode rules + common.
  - Both -> emit meta + `Role:` line + `Mode:` line + mode rules + common.
  - `mode:` matched but mode file missing -> mode is silently dropped; if a
    `role:` is also present it is still emitted, otherwise exit 0.

Slug syntax:
  - `mode:<name>` — <name> matches [A-Za-z][A-Za-z0-9_-]*. Captured value
    is normalized to lowercase.
  - `role:<value>` — <value> is free-form (multibyte and spaces allowed).
    Two forms:
      * Quoted: `role:"<value>"` — captures everything between the
        double quotes verbatim. Use this when the value contains
        literal "mode:" / "pj:" or other text that would otherwise
        terminate the unquoted form.
      * Unquoted: capture continues until the next ` mode:` / ` pj:`
        slug, a newline, or end of input.
    The value is preserved verbatim (no case folding). Empty quoted
    value (`role:""`) is treated as no role.
  - Both prefixes are detected at string start or after whitespace, are
    case-insensitive, and only the first occurrence per kind is used.

Mode aliases (resolved for file lookup; the user's chosen alias is preserved
in the displayed `Mode:` line):
  - verify -> debug
  - implement -> execute

UTF-8 BOM tolerant for stdin.
"""
import json
import os
import re
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODES_DIR = os.path.join(PLUGIN_ROOT, 'prompts', 'modes')
META_FILE = os.path.join(MODES_DIR, '_meta.md')
COMMON_FILE = os.path.join(MODES_DIR, '_common.md')

MODE_ALIASES = {
  'verify': 'debug',
  'implement': 'execute',
}

MODE_RE = re.compile(r'(?:^|\s)mode:([A-Za-z][A-Za-z0-9_-]*)', re.IGNORECASE)
ROLE_RE = re.compile(r'(?:^|\s)role:(?:"([^"]*)"|(.+?)(?=\s+(?:mode|pj):|\n|$))', re.IGNORECASE)


def read_optional(path):
  if not os.path.isfile(path):
    return ''
  try:
    with open(path, 'r', encoding='utf-8') as f:
      return f.read().strip()
  except Exception:
    return ''


try:
  data = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
except Exception:
  sys.exit(0)

prompt = data.get('prompt', '')
if not prompt:
  sys.exit(0)

mode_match = MODE_RE.search(prompt)
mode_name = mode_match.group(1).lower() if mode_match else None

role_match = ROLE_RE.search(prompt)
if role_match:
  role_name = (role_match.group(1) or role_match.group(2) or '').strip()
  if not role_name:
    role_name = None
else:
  role_name = None

mode_body = ''
if mode_name is not None:
  canonical_mode = MODE_ALIASES.get(mode_name, mode_name)
  mode_file = os.path.join(MODES_DIR, f'{canonical_mode}.md')
  if os.path.isfile(mode_file):
    try:
      with open(mode_file, 'r', encoding='utf-8') as f:
        mode_body = f.read().strip()
    except Exception:
      mode_name = None
      mode_body = ''
  else:
    mode_name = None

if mode_name is None and role_name is None:
  sys.exit(0)

active_lines = []
if role_name:
  active_lines.append(f'role: {role_name}')
if mode_name:
  active_lines.append(f'mode: {mode_name}')
active_block = '\n'.join(active_lines)
if mode_body:
  active_block += '\n' + mode_body

parts = []
meta_content = read_optional(META_FILE)
if meta_content:
  parts.append(meta_content)
parts.append(active_block)
if mode_name:
  common_content = read_optional(COMMON_FILE)
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
