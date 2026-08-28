#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EMITTER = ROOT / "hooks" / "session_init.py"
QUOTING_DOCS = [
    ROOT / "skills" / "progress" / "SKILL.md",
    ROOT / "skills" / "pj-rules" / "SKILL.md",
]
ROUTER_DOCS = [
    ROOT / "skills" / "progress" / "SKILL.md",
    ROOT / "agents" / "progress-router.md",
]

PASS = 0
FAIL = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"ok   {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL {msg}")


def emitted_fields() -> set[str]:
    src = EMITTER.read_text(encoding="utf-8")
    line = next((l for l in src.splitlines() if "[Progress Session] session_id=" in l
                 and "{" in l), "")
    return set(re.findall(r"(\w+)=\{", line))


def quoted_fields(text: str) -> set[str]:
    out: set[str] = set()
    for pat in re.findall(r"\[Progress Session\][^`\n]*", text):
        out |= set(re.findall(r"(\w+)=<", pat))
    return out


emitted = emitted_fields()
if not emitted:
    bad("no [Progress Session] emit line found in hooks/session_init.py")
else:
    ok(f"emitter names {len(emitted)} field(s): {sorted(emitted)}")

if "sid" in emitted and "sid8" not in emitted:
    ok("emitter carries `sid`, not `sid8`")
else:
    bad(f"emitter field set is not the tail-12 shape: {sorted(emitted)}")

scanned = 0
for doc in QUOTING_DOCS:
    text = doc.read_text(encoding="utf-8")
    fields = quoted_fields(text)
    if not fields:
        bad(f"{doc.name}: quotes no [Progress Session] pattern, so this check "
            f"proves nothing about it")
        continue
    scanned += 1
    stale = fields - emitted
    if stale:
        bad(f"{doc.relative_to(ROOT)}: quoted header names {sorted(stale)}, "
            f"which the emitter does not send; it sends {sorted(emitted)}")
    else:
        ok(f"{doc.relative_to(ROOT)}: quoted header matches the emitter")

for doc in ROUTER_DOCS:
    text = doc.read_text(encoding="utf-8")
    hits = [l.strip() for l in text.splitlines()
            if "session_id" in l and re.search(r"first\s+8|sid8", l)]
    if hits:
        bad(f"{doc.relative_to(ROOT)}: session_id is still described as the "
            f"first-8 form: {hits[0][:90]}")
    else:
        ok(f"{doc.relative_to(ROOT)}: session_id carries no first-8 wording")

md_files = sorted(q for q in ROOT.rglob("*.md")
                  if "node_modules" not in q.parts)
offenders = [q for q in md_files if "sid8" in q.read_text(encoding="utf-8")]
if not md_files:
    bad("no markdown scanned, so the sweep proves nothing")
elif offenders:
    for q in offenders:
        bad(f"{q.relative_to(ROOT)}: names `sid8`, a header field the "
            f"emitter does not send")
else:
    ok(f"no `sid8` in any of {len(md_files)} plugin markdown file(s)")

print(f"\nscanned {scanned} header-quoting doc(s), {len(ROUTER_DOCS)} router "
      f"doc(s), {len(md_files)} markdown file(s)")
if scanned == 0:
    bad("no header-quoting doc was scanned")
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
