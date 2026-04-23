import json, sys, os, tempfile, re

# Cursor sends hook stdin with a UTF-8 BOM; Claude Code does not.
# utf-8-sig handles both transparently.
try:
    data = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
except Exception:
    sys.exit(0)

# Cursor emits "Shell" for the shell tool while Claude Code emits "Bash".
# Read/Write/Edit are identical across both; MCP tools share the mcp__* prefix.
TOOL_NAME_ALIASES = {
    'Shell': 'Bash',
}

session_id = data.get('session_id', '')
tool_name  = data.get('tool_name', '')
tool_name  = TOOL_NAME_ALIASES.get(tool_name, tool_name)
tool_input = data.get('tool_input', {})
cwd        = data.get('cwd', os.getcwd())

if not session_id:
    sys.exit(0)

pending_path = os.path.join(tempfile.gettempdir(), f'claude_rules_pending_{session_id}.txt')

# Allow commands that rm the pending file itself to pass through.
if tool_name == 'Bash':
    if f'claude_rules_pending_{session_id}' in tool_input.get('command', ''):
        sys.exit(0)

# ── helpers ──────────────────────────────────────────────────────────────────

def load_reads():
    reads_path = os.path.join(tempfile.gettempdir(), f'claude_reads_{session_id}.txt')
    if not os.path.exists(reads_path):
        return []
    with open(reads_path, 'r', encoding='utf-8') as f:
        return [os.path.normpath(l.strip()) for l in f.read().splitlines() if l.strip()]

def has_been_read(rel_path, reads):
    req_norm = os.path.normpath(rel_path).lower()
    return any(r.lower().endswith(req_norm) for r in reads)

def load_pending():
    if not os.path.exists(pending_path):
        return []
    with open(pending_path, 'r', encoding='utf-8') as f:
        return [l.strip() for l in f.read().splitlines() if l.strip()]

def add_to_pending(files):
    existing = set(load_pending())
    new_files = [f for f in files if f not in existing]
    if not new_files:
        return
    with open(pending_path, 'a', encoding='utf-8') as f:
        for nf in new_files:
            f.write(nf + '\n')

def tool_matches(actual_tool, det_tools):
    """Exact match for standard tools, prefix match for MCP tools."""
    for t in det_tools:
        if actual_tool == t:
            return True
        # MCP prefix: "mcp__server" matches "mcp__server__tool_name"
        if t.startswith('mcp__') and actual_tool.startswith(t + '__'):
            return True
    return False

def find_claude_md(start_dir):
    """Walk upward from cwd until a CLAUDE.md is found; return its absolute path."""
    d = os.path.abspath(start_dir)
    while True:
        candidate = os.path.join(d, 'CLAUDE.md')
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent

def parse_claude_md(content):
    """
    Extract (trigger_key, src_path) pairs from CLAUDE.md.
    trigger_key: the `when` attribute text; falls back to the rule `id` if `when` is absent.
    src_path:    the `src` attribute, or a lib/prompts/... reference inside the tag body.
    """
    results = []

    # Self-closing: <rules ... when="..." src="..." />
    for m in re.finditer(r'<rules\b([^\n>]*/?)>', content):
        attrs = m.group(1)
        when_m = re.search(r'\bwhen="([^"]*)"', attrs)
        src_m  = re.search(r'\bsrc="([^"]*)"',  attrs)
        if when_m and src_m:
            results.append((when_m.group(1), src_m.group(1)))

    # Open/close: <rules ...>...</rules> (skip self-closing tags)
    for m in re.finditer(r'<rules\b([^>]*)>(.*?)</rules>', content, re.DOTALL):
        attrs = m.group(1)
        body  = m.group(2)
        if attrs.rstrip().endswith('/'):
            continue
        when_m = re.search(r'\bwhen="([^"]*)"', attrs)
        id_m   = re.search(r'\bid="([^"]*)"',   attrs)
        trigger = when_m.group(1) if when_m else (id_m.group(1) if id_m else None)
        if not trigger:
            continue
        # Pick up lib/prompts/... references inside the body.
        for ref in re.findall(r'[@]?/?lib/prompts/[\w/._%+-]+\.md', body):
            src = re.sub(r'^[@/]+', '', ref)
            results.append((trigger, src))

    return results

# ── main ───────────────────────────────────────────────────────────────────

reads = load_reads()
missing = []

# 1. Locate CLAUDE.md (the harness auto-injects it as claudeMd, so forcing a Read is unnecessary).
claude_md_path = find_claude_md(cwd)

# 2. Dynamically apply external rule files from CLAUDE.md's src attributes.
if claude_md_path:
    try:
        with open(claude_md_path, 'r', encoding='utf-8') as f:
            claude_content = f.read()

        # Load when_detection.json (sits in the same dir as this script).
        detection_path = os.path.join(os.path.dirname(__file__), 'when_detection.json')
        with open(detection_path, 'r', encoding='utf-8') as f:
            detections = json.load(f)

        # Extract rules from CLAUDE.md.
        rules = parse_claude_md(claude_content)

        for trigger_key, src_path in rules:
            if has_been_read(src_path, reads):
                continue
            # Find the detection spec that matches this trigger_key.
            for det in detections:
                if not re.search(det['trigger_pattern'], trigger_key, re.IGNORECASE):
                    continue
                if not tool_matches(tool_name, det['tools']):
                    continue
                field = det.get('field', '')
                if field == '*':
                    # Any invocation of the matched tool fires this rule.
                    if src_path not in missing:
                        missing.append(src_path)
                    break
                value = tool_input.get(field, '')
                if not value:
                    continue
                if re.search(det['pattern'], str(value), re.IGNORECASE):
                    if src_path not in missing:
                        missing.append(src_path)
                    break  # This rule is decided; move to the next rule.

    except Exception:
        pass  # Swallow parse failures and let the call pass through.

if missing:
    add_to_pending(missing)

# ── pending check ──────────────────────────────────────────────────────────

pending = load_pending()
if not pending:
    sys.exit(0)

file_list = '\n'.join(f'  - {line}' for line in pending)
reason = (
    f"BLOCKED: please Read the following file(s) with the Read tool and retry:\n"
    f"{file_list}\n\n"
    f"The block will clear automatically once all listed files are read."
)

sys.stdout.buffer.write(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason
    }
}, ensure_ascii=False).encode('utf-8'))
sys.stdout.buffer.write(b'\n')
