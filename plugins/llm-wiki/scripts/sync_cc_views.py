#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""sync_cc_views — regenerate the vendored cc_views.sql from its canonical source.

Build-time vendor for S8-a (task: session-log-ingest, U1). The marketplace has no
build step, so the plugin ships a *committed copy* of the inspect-cc-log projection
SQL. This script is the drift-repair tool: it copies the canonical

    skills/inspect-cc-log/scripts/views.sql

verbatim (byte-for-byte) onto the vendored

    plugins/llm-wiki/llmwiki/ingest/cc_views.sql

so `test_cc_views_contract.py` (byte-equivalence assertion) passes again. The
canonical file is the single source of truth; the vendored copy is never edited
by hand — run this after the canonical changes.

Paths are resolved relative to this script (repo-root discovery); no absolute
paths are baked in. Run with:  uv run --script scripts/sync_cc_views.py
"""
import shutil
import sys
from pathlib import Path

# __file__ = plugins/llm-wiki/scripts/sync_cc_views.py
#   parents[0]=scripts  parents[1]=llm-wiki  parents[2]=plugins  parents[3]=repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]

CANONICAL = _REPO_ROOT / "skills" / "inspect-cc-log" / "scripts" / "views.sql"
VENDORED = _REPO_ROOT / "plugins" / "llm-wiki" / "llmwiki" / "ingest" / "cc_views.sql"


def main(argv: "list[str] | None" = None) -> int:
    if not CANONICAL.is_file():
        print(f"canonical source not found: {CANONICAL}", file=sys.stderr)
        return 1

    before = VENDORED.read_bytes() if VENDORED.is_file() else None
    VENDORED.parent.mkdir(parents=True, exist_ok=True)
    # copyfile (not copy2) — content only, no metadata; byte-for-byte content copy.
    shutil.copyfile(CANONICAL, VENDORED)
    after = VENDORED.read_bytes()

    if after != CANONICAL.read_bytes():
        print("post-copy byte mismatch — sync failed", file=sys.stderr)
        return 1

    changed = before != after
    rel = VENDORED.relative_to(_REPO_ROOT).as_posix()
    print(f"{'updated' if changed else 'up-to-date'}: {rel} ({len(after)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
