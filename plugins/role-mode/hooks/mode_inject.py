#!/usr/bin/env python3
"""
UserPromptSubmit hook: detect `mode:<name>` and/or `role:<name>` slugs
in the user prompt, then inject the framework meta (one of two variants,
selected by whether a role is present) + active Role/Mode declaration +
(when mode is set) matching mode rules + common rules as additionalContext.

Two meta variants:
  - `_meta.md` (role-less) — used when no role is present. Defines the Mode
    axis only; no Role axis text is shown, so a role-less turn never sees
    a slot it has no reason to fill.
  - `_meta_role.md` — used whenever a role is present. Defines both the
    Role and Mode axes (byte-identical to the pre-split single `_meta.md`).

Behavior:
  - Neither `mode:` nor `role:` present -> exit 0 with no output (baseline
    LLM behavior is preserved).
  - `role:` only -> emit `_meta_role.md` + `Role:` line (no Mode line, no
    common).
  - `mode:` only -> emit `_meta.md` (role-less) + `Mode:` line + mode rules
    + common.
  - Both -> emit `_meta_role.md` + `Role:` line + `Mode:` line + mode rules
    + common.
  - `mode:` matched but mode file missing -> mode is silently dropped; if a
    `role:` is also present it is still emitted (with `_meta_role.md`),
    otherwise exit 0.

Slug syntax:
  - `mode:<name>` — <name> matches [A-Za-z][A-Za-z0-9_-]*. Captured value
    is normalized to lowercase.
  - `mode:<name>/<seg>...` — an optional `/`-separated suffix after the mode
    name declares delegation to a subagent. Each suffix segment matches
    [A-Za-z0-9_.-]+ (the `.` allows tokens like `haiku-4.5`) and is captured
    and echoed verbatim, in its original case, with no validation: the hook
    does not interpret suffix segments at all (see prompts/modes/_subagent.md,
    which the LLM reads to decide what each segment means). Only the mode
    name (the part before the first `/`) is lowercased and resolved against
    MODE_ALIASES / the modes directory; suffix segments are untouched by
    that resolution. If the mode name fails to resolve (see below), the
    whole suffix is dropped along with it.
    When any suffix segment is present, prompts/modes/_subagent.md is
    injected in addition to the mode file and _common.md.
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

Non-invocation inputs (both measured; see mode-orchestrator-runs/
phase4-defects-2-4-design.md, defect 2):
  - Backtick spans are masked before detection: `` `mode:execute` `` is a
    mention/quotation, not an invocation. A real run's orchestrator was
    flipped into execute mode by a subagent gist that merely quoted the
    rule that had stopped it.
  - System-generated turns are skipped entirely: a prompt carrying a
    task-notification marker is a background-task/subagent completion
    notice relayed as a user turn, not something the user typed. Slugs
    inside it are always quoted text from some agent's output.

Mode aliases (resolved for file lookup; the user's chosen alias is preserved
in the displayed `Mode:` line):
  - verify -> debug
  - implement -> execute

Alias resolution runs on the mode name only, after the suffix has already
been split off (e.g. `verify/subagent` resolves `verify` -> `debug.md` and
echoes `mode: verify/subagent`, not `debug/subagent`).

UTF-8 BOM tolerant for stdin.
"""
import json
import os
import re
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODES_DIR = os.path.join(PLUGIN_ROOT, 'prompts', 'modes')
META_FILE = os.path.join(MODES_DIR, '_meta.md')
META_ROLE_FILE = os.path.join(MODES_DIR, '_meta_role.md')
COMMON_FILE = os.path.join(MODES_DIR, '_common.md')
SUBAGENT_FILE = os.path.join(MODES_DIR, '_subagent.md')

MODE_ALIASES = {
  'verify': 'debug',
  'implement': 'execute',
}

MODE_RE = re.compile(
  r'(?:^|\s)mode:([A-Za-z][A-Za-z0-9_-]*(?:/[A-Za-z0-9_.-]+)*)',
  re.IGNORECASE)
ROLE_RE = re.compile(r'(?:^|\s)role:(?:"([^"]*)"|(.+?)(?=\s+(?:mode|pj):|\n|$))', re.IGNORECASE)

# Inline code spans are mentions, not invocations -- masked before slug
# detection. Single-line only (backticks pairing across lines would eat
# unrelated text between two independent code fragments).
CODE_SPAN_RE = re.compile(r'`[^`\n]*`')

# Markers of system-generated user turns (background-task / subagent
# completion notices). Measured forms; a prompt containing one is never a
# hand-typed invocation, so no injection happens at all. Fail-open by
# design: an unrecognized future marker just means the old behavior, and
# the reply-contract ban on bare slugs still stands upstream.
NOTIFICATION_MARKERS = (
  '<task-notification>',
  '[SYSTEM NOTIFICATION - NOT USER INPUT]',
)


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

for marker in NOTIFICATION_MARKERS:
  if marker in prompt:
    # Visible skip (stderr shows up in verbose hook logs): without this, a
    # hand-typed prompt that both mentions a marker string and carries a
    # mode: slug loses its mode with no way to tell why.
    print(f"role-mode: skipped (notification marker {marker!r} in prompt)",
          file=sys.stderr)
    sys.exit(0)

# Mask inline code spans so `mode:x` quoted in backticks never invokes.
scan_text = CODE_SPAN_RE.sub(' ', prompt)

nomode = 'nomode' in scan_text
norole = 'norole' in scan_text

mode_match = None if nomode else MODE_RE.search(scan_text)
if mode_match:
  mode_segs = mode_match.group(1).split('/')
  mode_name = mode_segs[0].lower()
  # Suffix segments are echoed verbatim, exactly as typed; the hook never
  # normalizes or validates them (see docstring "Slug syntax:").
  suffix_segs = mode_segs[1:]
else:
  mode_name = None
  suffix_segs = []

role_match = None if norole else ROLE_RE.search(scan_text)
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
      suffix_segs = []
  else:
    mode_name = None
    suffix_segs = []

if mode_name is None and role_name is None:
  sys.exit(0)

active_lines = []
if role_name:
  active_lines.append(f'role: {role_name}')
if mode_name:
  displayed_mode = '/'.join([mode_name] + suffix_segs)
  active_lines.append(f'mode: {displayed_mode}')
active_block = '\n'.join(active_lines)
if mode_body:
  active_block += '\n' + mode_body

parts = []
meta_content = read_optional(META_ROLE_FILE if role_name else META_FILE)
if meta_content:
  parts.append(meta_content)
parts.append(active_block)
if mode_name:
  common_content = read_optional(COMMON_FILE)
  if common_content:
    parts.append(common_content)
  if suffix_segs:
    subagent_content = read_optional(SUBAGENT_FILE)
    if subagent_content:
      parts.append(subagent_content)
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
