"""Execution-mode prompt fragments (bundled snapshot of the role-mode plugin's
prompts/modes/, copied 2026-07-19; maintained independently from here on).

A step's `mode=` attribute selects one fragment. The injection mirrors the
plugin's UserPromptSubmit hook: `_meta.md` (framework header) + the mode
declaration line + the mode body + `_common.md` (all-modes rules). `_meta.md`
is also injected on its own for every step, since every step carries a Role.

NAME/MEANING NOTE (2026-07-30): the canonical plugin split its single
`_meta.md` into a role-less `_meta.md` (Mode axis only) and `_meta_role.md`
(both axes -- byte-identical to the pre-split header). This snapshot's
`_meta.md` was NOT re-synced and still holds the pre-split, both-axes
content -- it corresponds to the canonical `_meta_role.md`, not the
canonical `_meta.md`, despite the matching filename. That's fine today
because every step here has a Role (see above), so the role-less variant
has no consumer. If xml-wf ever makes `role=` optional, this snapshot needs
its own role-less/role-present split to match; see
`_projects/harness-modes/tasks/0_todo/2026-07-23_xml-wf-mode-snapshot-sync-cycle-design.md`.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

MODES_DIR = Path(__file__).parent / "modes"

# Same aliases as the plugin's mode_inject.py: the alias picks the file to
# read, but the user's chosen name is kept in the emitted `mode:` line.
MODE_ALIASES = {"verify": "debug", "implement": "execute"}

# The plugin's _common.md obliged agents to print `[Mode: x]` as their first
# line. The workflow _common.md no longer mandates it, but models may emit it
# from habit — keep stripping it defensively before any classification.
_MODE_LINE_RE = re.compile(r"\A\s*\[Mode:[^\]\n]*\][ \t]*\n?")


class ModeError(Exception):
    pass


def mode_file(name: str) -> Path | None:
    """The .md file backing a mode name (after alias resolution), or None."""
    slug = MODE_ALIASES.get(name, name)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", slug or ""):
        return None
    path = MODES_DIR / f"{slug}.md"
    return path if path.is_file() else None


def available_modes() -> list[str]:
    names = sorted(p.stem for p in MODES_DIR.glob("*.md")
                   if not p.stem.startswith("_"))
    return names + sorted(MODE_ALIASES)


@lru_cache(maxsize=None)
def _read(filename: str) -> str:
    return (MODES_DIR / filename).read_text(encoding="utf-8").strip()


def meta_text() -> str:
    return _read("_meta.md")


def mode_block(name: str) -> str:
    """`mode:<name>` declaration + mode body + _common.md, hook-style."""
    path = mode_file(name)
    if path is None:
        raise ModeError(f"unknown mode '{name}' "
                        f"(available: {', '.join(available_modes())})")
    return f"mode:{name}\n{_read(path.name)}\n\n{_read('_common.md')}"


def strip_mode_line(text: str) -> str:
    """Drop one leading `[Mode: ...]` line (legacy _common.md protocol)."""
    return _MODE_LINE_RE.sub("", text, count=1)


def blocked_line(text: str) -> str | None:
    """The `[BLOCKED: ...]` refusal line, when the response starts with one.

    _meta.md instructs agents to reply with a single `[BLOCKED: mode-rule x]`
    line and stop when a mode/rules constraint blocks the task. Detection is
    first-line-anchored (like the ERROR: protocol) to avoid false positives on
    responses that merely mention the token.
    """
    body = strip_mode_line(text).lstrip()
    if body.startswith("[BLOCKED:"):
        return body.splitlines()[0]
    return None
