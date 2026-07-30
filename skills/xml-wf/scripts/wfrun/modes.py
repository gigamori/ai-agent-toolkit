"""Execution-mode prompt fragments (bundled snapshot of the role-mode plugin's
prompts/modes/, copied 2026-07-19; maintained independently from here on).

A step's `mode=` attribute selects one fragment. The injection mirrors the
plugin's UserPromptSubmit hook: a framework header + the mode declaration line
+ the mode body + `_common.md` (all-modes rules). The header is also injected
on its own for every step, in the variant matching whether the step declares a
role (`role=` or an inline `<role>` -- both optional since 2026-07-30).

NAME/MEANING NOTE (2026-07-30): the headers here are NOT copies of the
plugin's and never were -- they are xml-wf's own, carrying axes the plugin has
no notion of, because a step also carries `<rules>` and a `<task>`:

- `_meta_role.md` -- four axes (`Mode / Rules / Task / Role`, precedence
  `Mode > Rules > Task > Role`), authored with this skill and byte-unchanged
  since; used when the step declares a role.
- `_meta.md` -- the same document with the Role axis dropped (three axes,
  precedence `Mode > Rules > Task`); used when it does not. Derived from the
  four-axis header, NOT from the plugin.

Both keep the `[BLOCKED: rules <id>]` form and the guardrails sentence. (The
rest of this directory started as plugin copies but has drifted too --
`_common.md` and `plan.md`, for instance, no longer match canonical.)

The plugin split its own `_meta.md` on the same axis (role-less `_meta.md` +
`_meta_role.md`), so the filenames now agree in meaning -- but the CONTENTS
still map to neither canonical file. Re-syncing either by filename would
destroy the Rules/Task axes and the guardrails sentence; both files are
own-managed. See
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


def meta_text(*, with_role: bool) -> str:
    """The framework header: four-axis when the step declares a role, the
    three-axis (Role dropped) variant when it does not. Keyword-only and
    required so a call site cannot silently pick the wrong axis set."""
    return _read("_meta_role.md" if with_role else "_meta.md")


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
