#!/usr/bin/env python3
"""Backfill origin: "cc" into existing state JSON files under <cwd>/_projects/_state/."""
import json, glob, os

state_dir = os.path.join(os.getcwd(), '_projects', '_state')
pattern = os.path.join(state_dir, '*.json')
files = glob.glob(pattern)

updated = 0
skipped = 0
for path in files:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"SKIP (read error): {os.path.basename(path)} — {e}")
        skipped += 1
        continue
    if not isinstance(data, dict):
        print(f"SKIP (not dict): {os.path.basename(path)}")
        skipped += 1
        continue
    if data.get('origin') == 'cc':
        skipped += 1
        continue
    data['origin'] = 'cc'
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    updated += 1

print(f"Done. updated={updated} skipped={skipped} total={len(files)}")
