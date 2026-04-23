#!/usr/bin/env python3
"""
Auto-generate when_detection.json from CLAUDE.md + tool_signatures.json.

Usage:
  python generate_when_detection.py [CLAUDE_MD_PATH]

If CLAUDE_MD_PATH is omitted, searches upward from cwd.
"""
import json, re, os, sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def find_claude_md(start_dir):
    d = os.path.abspath(start_dir)
    while True:
        c = os.path.join(d, 'CLAUDE.md')
        if os.path.isfile(c):
            return c
        p = os.path.dirname(d)
        if p == d:
            return None
        d = p


def parse_claude_md(content):
    """Extract (trigger_key, src_path) pairs from CLAUDE.md."""
    results = []
    # Self-closing: <rules ... when="..." src="..." />
    for m in re.finditer(r'<rules\b([^\n>]*/?)>', content):
        attrs = m.group(1)
        w = re.search(r'\bwhen="([^"]*)"', attrs)
        s = re.search(r'\bsrc="([^"]*)"', attrs)
        if w and s:
            results.append((w.group(1), s.group(1)))
    # Open/close: <rules ...>body</rules> (skip self-closing tags)
    for m in re.finditer(r'<rules\b([^>]*)>(.*?)</rules>', content, re.DOTALL):
        attrs, body = m.group(1), m.group(2)
        if attrs.rstrip().endswith('/'):
            continue
        w = re.search(r'\bwhen="([^"]*)"', attrs)
        i = re.search(r'\bid="([^"]*)"', attrs)
        trigger = w.group(1) if w else (i.group(1) if i else None)
        if not trigger:
            continue
        for ref in re.findall(r'[@]?/?lib/prompts/[\w/._%+-]+\.md', body):
            src = re.sub(r'^[@/]+', '', ref)
            results.append((trigger, src))
    return results


def keyword_matches(keyword, text):
    """Word-boundary-aware keyword match."""
    pat = r'\b' + re.escape(keyword) + r'\b'
    return bool(re.search(pat, text, re.IGNORECASE))


def expand_tools(tool_list, mcp_servers):
    """Expand $mcp:group references to MCP tool name prefixes."""
    expanded = []
    for t in tool_list:
        if t.startswith('$mcp:'):
            group = t[5:]
            for server in mcp_servers.get(group, []):
                expanded.append(f'mcp__{server}')
        else:
            expanded.append(t)
    return expanded


def generate(claude_md_path=None):
    if claude_md_path is None:
        claude_md_path = find_claude_md(os.getcwd())
    if not claude_md_path or not os.path.isfile(claude_md_path):
        print("ERROR: CLAUDE.md not found", file=sys.stderr)
        sys.exit(1)

    sig_path = os.path.join(SCRIPT_DIR, 'tool_signatures.json')
    out_path = os.path.join(SCRIPT_DIR, 'when_detection.json')

    with open(claude_md_path, 'r', encoding='utf-8') as f:
        md_content = f.read()
    with open(sig_path, 'r', encoding='utf-8') as f:
        sig_data = json.load(f)

    mcp_servers = sig_data.get('mcp_servers', {})
    signatures = sig_data.get('signatures', [])
    rules = parse_claude_md(md_content)

    print(f"Source: {claude_md_path}")
    print(f"Rules extracted: {len(rules)}")
    for tk, sp in rules:
        print(f'  when="{tk}" -> {sp}')

    detections = []
    seen = set()
    all_mcp_prefixes = set()
    unmatched = []

    for trigger_key, src_path in rules:
        matched_keywords = []
        matched_entries = []

        for sig in signatures:
            sig_matched = False
            for kw in sig['keywords']:
                if keyword_matches(kw, trigger_key):
                    if kw not in matched_keywords:
                        matched_keywords.append(kw)
                    sig_matched = True
            if sig_matched:
                matched_entries.extend(sig['entries'])

        if not matched_entries:
            unmatched.append((trigger_key, src_path))
            continue

        trigger_pattern = '|'.join(re.escape(kw) for kw in matched_keywords)

        for entry in matched_entries:
            tools = expand_tools(entry['tools'], mcp_servers)
            field = entry['field']
            pattern = entry['pattern']

            for t in tools:
                if t.startswith('mcp__'):
                    all_mcp_prefixes.add(t)

            key = (trigger_pattern, tuple(sorted(tools)), field, pattern)
            if key in seen:
                continue
            seen.add(key)

            detections.append({
                '_comment': f'{trigger_key} -> {src_path}',
                'trigger_pattern': trigger_pattern,
                'tools': tools,
                'field': field,
                'pattern': pattern
            })

    with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(detections, f, ensure_ascii=False, indent=2)
        f.write('\n')

    print(f"\nGenerated {len(detections)} detection entries -> {out_path}")

    if unmatched:
        print(f"\nWARN: {len(unmatched)} rules had no matching signatures:")
        for tk, sp in unmatched:
            print(f'  when="{tk}" -> {sp}')

    if all_mcp_prefixes:
        mcp_parts = '|'.join(f'{p}__.*' for p in sorted(all_mcp_prefixes))
        matcher = f'Bash|Write|Edit|{mcp_parts}'
        print(f"\nRecommended PreToolUse matcher for settings.json:")
        print(f'  "{matcher}"')

    return detections


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else None
    generate(path)
