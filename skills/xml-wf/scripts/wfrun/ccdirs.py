"""CLAUDE_CONFIG_DIR-aware resolution of Claude Code's config dir (C class).

xml-wf reads three things out of Claude Code's own config tree: agent
definitions (`agents.py`), a settings-hook marker (`__main__.py`
`_warn_if_no_llm_guard`), and — as a safety guard, not a reader —
`~/.claude` itself as a directory a workflow must not run inside
(`executor.py` / `__main__.py` `_check_base_dir`). `CLAUDE_CONFIG_DIR` can
move that tree, so each of the three needs to know both the env universe and
the default `~/.claude`, in different arrangements.

Semantics (shared with the A/B classes; see the ingest and skills specs under
`_projects/llm-wiki/project-notes/specs/`, and the taskflow spec
`_projects/harness-taskflow/project-notes/specs/claude-config-dir-support.md`
§1.1/D1 for the underlying probe): the env value is LITERAL — no
`~`-expansion, no env-var expansion — and a relative value resolves against
the process cwd (`os.path.abspath`), replicating what Claude Code itself does
with it. Only the built-in default `~/.claude` is expanduser'd. `normcase` is
used for de-duplication (Windows case-insensitivity).
"""
from __future__ import annotations

import os
from pathlib import Path


def claude_config_dirs() -> list[Path]:
    """`[$CLAUDE_CONFIG_DIR, ~/.claude]`, env first, deduped.

    Each caller re-orders or filters this list for its own purpose (agent
    discovery layers `project > env > default`; the settings-guard check adds
    the env dir as one more place to look; the safety guard protects both).
    """
    dirs: list[Path] = []
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        dirs.append(Path(os.path.abspath(env)))
    default = Path(os.path.expanduser("~/.claude"))
    if not any(os.path.normcase(str(d)) == os.path.normcase(str(default)) for d in dirs):
        dirs.append(default)
    return dirs
