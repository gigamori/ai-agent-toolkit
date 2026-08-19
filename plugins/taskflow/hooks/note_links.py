#!/usr/bin/env python3
"""note_links.py — deterministic data layer for note↔task linking (Phase A).

Design: project-notes/specs/note-task-link.md. This module implements the
code-side ("決定論（code）", §7) of the feature only; the LLM-judgment side
(establish confirmation, first-time note→owner mapping, the async capture /
Stop apply path of §10) is Phase B and lives elsewhere. Nothing here touches the
working Round1/Round2 gate in session_progress_capture.py.

Core principle (§2): the association lives on the TASK side, never on the note.
Note files are pure deliverables and are never written. Each owning task carries
a list of related note paths inside a `<!-- @notes:begin/end -->` block placed
directly after its `<!-- @log:begin/end -->` block (§4.1). The three existing
block parsers (rebuild_progress.py / generate_kanban.py / audit_progress.py) were
point-checked to tolerate a `@notes` block in that position (spec §4.1 gate,
verified 2026-07-01: none parse task-body blocks past `@log:end`).

Asymmetry (§3): establishing a link (writing it, durable) is WRITE-triggered
only; resolving an existing link (read) is read-triggered and creates nothing.

Path convention (IMPORTANT — Phase B must match): a note link is stored
PROJECT-RELATIVE, e.g. `project-notes/specs/foo.md` (relative to
`_projects/<project>/`), NOT repo-relative. This stays valid when the project
dir is renamed/moved (the task and note files move together under the same
project root, §4.2). Phase B resolves from a repo-relative `touched` read path by
stripping the `_projects/<project>/` prefix before calling resolve_note_owner().

Public API:
  - normalize_note_rel(rel)            → forward-slashed, trimmed path string
  - is_contained_note_rel(rel)         → containment predicate (no `..`/root/drive escape)
  - is_note_deliverable(rel)           → §5 exclusion predicate (index.md/_archive)
  - parse_note_links(content)          → list[str] note rels in a task md
  - append_note_link(task_path, rel)   → idempotent union establish (§3.1/§4.1)
  - build_reverse_index(project_root)  → {note_rel: [task_abspath]} with stale-skip
  - resolve_note_owner(rel, project_root[, idx]) → candidate owning task paths (§3.2)

Writes serialize through the shared bounded advisory lock (log_lock, INV-2),
consistent with `@log` appends.
"""
from __future__ import annotations

import os
import re
import sys

# Sibling import: log_lock lives next to this file in hooks/. Hook scripts run
# standalone (no package context), so add this file's own directory to sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from log_lock import log_lock  # noqa: E402

NOTES_BEGIN = '<!-- @notes:begin -->'
NOTES_END = '<!-- @notes:end -->'
# Auto-managed marker (§4.1 — same intent as the @table "do not hand-edit" note).
_AUTO_COMMENT = '<!-- auto-managed by taskflow note-link; do not hand-edit -->'

_NOTES_BLOCK_RE = re.compile(
    r'<!--\s*@notes:begin\s*-->(.*?)<!--\s*@notes:end\s*-->', re.DOTALL
)
_NOTES_END_RE = re.compile(r'<!--\s*@notes:end\s*-->')
_LOG_END_RE = re.compile(r'<!--\s*@log:end\s*-->')

# A drive-qualified rel (`C:/x`, `C:x`). Matched as text, not via os.path.isabs:
# isabs is platform-dependent and does not see `C:/x` as absolute on POSIX, so
# the same rel would be contained on one host and not on another.
_DRIVE_RE = re.compile(r'^[A-Za-z]:')


def normalize_note_rel(rel: str) -> str:
    """Normalize a note path to a forward-slashed, trimmed string. Strips a
    leading `./`. Returns '' for falsy input."""
    if not rel:
        return ''
    rel = str(rel).replace('\\', '/').strip()
    while rel.startswith('./'):
        rel = rel[2:]
    return rel


def is_contained_note_rel(rel: str) -> bool:
    """True if `rel` still resolves INSIDE the project root it is joined onto.

    Every consumer joins a note rel onto a project root — the reverse index's
    stale check, and the capture subagent's grounding Read. A rel that walks out
    of that root therefore names a DIFFERENT file than the link claims to name,
    and §3.1 makes that permanent: the `@notes` block is union-append and is
    never edited, so a wrong rel written once is wrong deterministically forever.

    Prefix and containment are not the same property: `project-notes/../x.md`
    starts with `project-notes/` and leaves the project anyway, which is why a
    startswith test is not a bound. Root- and drive-anchored rels are rejected by
    the same predicate so containment holds on its own, without depending on
    which caller happened to test the prefix first.
    """
    rel = normalize_note_rel(rel)
    if not rel:
        return False
    # Leading '/' covers a POSIX-absolute rel and, after backslash folding, UNC.
    if rel.startswith('/') or _DRIVE_RE.match(rel):
        return False
    return '..' not in rel.split('/')


def is_note_deliverable(rel: str) -> bool:
    """True if `rel` is a project-notes deliverable eligible for linking (§5).

    Includes handoff (lives under `project-notes/checks/`, §5). Excludes the
    `project-notes/index.md` registry, anything under an `_archive/` segment,
    and anything that does not stay inside the project root.
    `rel` is project-relative (see module path convention).
    """
    rel = normalize_note_rel(rel)
    # Containment before category: a rel that leaves the project root is not a
    # deliverable OF this project at all, whatever its suffix or segments say.
    if not is_contained_note_rel(rel):
        return False
    if not rel.lower().endswith('.md'):
        return False
    parts = rel.split('/')
    if '_archive' in parts:
        return False
    if 'project-notes' not in parts:
        return False
    idx = parts.index('project-notes')
    after = parts[idx + 1:]
    # The registry read every session — `project-notes/index.md` exactly.
    if after == ['index.md']:
        return False
    return True


def _parse_block_links(block_inner: str) -> list[str]:
    """Extract note rels from the inner text of a `@notes` block: each line of
    the form `- <note_rel>`. The auto-managed comment line (starts with `<!--`)
    is skipped. Order-preserving, de-duplicated."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in block_inner.splitlines():
        line = raw.strip()
        if not line.startswith('- '):
            continue
        rel = normalize_note_rel(line[2:])
        if rel and rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def parse_note_links(content: str) -> list[str]:
    """Return the note rels recorded in a task md's `@notes` block ([] if none)."""
    m = _NOTES_BLOCK_RE.search(content)
    return _parse_block_links(m.group(1)) if m else []


def append_note_link(task_path: str, note_rel: str) -> bool:
    """Idempotently union-append `note_rel` into the task md's `@notes` block
    (§3.1/§4.1). Append-only; never edits existing lines.

    - If a `@notes` block exists and already lists `note_rel`: no-op (AC-3).
    - If a `@notes` block exists without it: insert the line before `@notes:end`.
    - If no `@notes` block exists: create one directly after `<!-- @log:end -->`.

    Returns True if `note_rel` is present in the block after the call (whether
    pre-existing or newly written), False if the link could NOT be established
    (read failure, malformed block, no `@log:end` anchor to attach to, or a
    `note_rel` that does not resolve inside the project root). The
    read-modify-write is serialized through `log_lock` (INV-2).
    """
    note_rel = normalize_note_rel(note_rel)
    if not note_rel:
        return False
    # Last line of defense (§3.1). This block is union-append and never edited,
    # so whatever is written here is permanent — refuse a rel this function
    # cannot vouch for rather than burn in a link to a file outside the project.
    if not is_contained_note_rel(note_rel):
        return False
    with log_lock(task_path):
        try:
            with open(task_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except OSError:
            return False

        block_m = _NOTES_BLOCK_RE.search(content)
        if block_m:
            if note_rel in _parse_block_links(block_m.group(1)):
                return True  # idempotent no-op (AC-3)
            end_m = _NOTES_END_RE.search(content)
            if not end_m:
                return False  # begin without end — malformed; do not touch
            insert_at = end_m.start()
            line = f'- {note_rel}\n'
            prefix = content[:insert_at]
            if prefix and not prefix.endswith('\n'):
                line = '\n' + line
            new_content = prefix + line + content[insert_at:]
        else:
            log_end_m = _LOG_END_RE.search(content)
            if not log_end_m:
                return False  # no anchor — cannot place the block (§4.1)
            insert_at = log_end_m.end()
            block = (
                f'\n\n{NOTES_BEGIN}\n'
                f'{_AUTO_COMMENT}\n'
                f'- {note_rel}\n'
                f'{NOTES_END}\n'
            )
            new_content = content[:insert_at] + block + content[insert_at:]

        try:
            with open(task_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
        except OSError:
            return False
        return True


def build_reverse_index(project_root: str) -> dict[str, list[str]]:
    """Walk every task md under `<project_root>/tasks/` and build the
    note→task reverse index `{note_rel: [task_abspath, ...]}` (§4.2).

    Stale-skip (AC-7): a recorded note whose file no longer exists under
    `project_root` (rename/delete) is dropped from the index. Task moves/renames
    auto-follow because the `@notes` block travels with the task file.
    """
    index: dict[str, list[str]] = {}
    tasks_root = os.path.join(project_root, 'tasks')
    if not os.path.isdir(tasks_root):
        return index
    for dirpath, _dirs, files in os.walk(tasks_root):
        for fn in files:
            if not fn.lower().endswith('.md'):
                continue
            task_path = os.path.join(dirpath, fn)
            try:
                with open(task_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except OSError:
                continue
            for note_rel in parse_note_links(content):
                if not os.path.isfile(os.path.join(project_root, note_rel)):
                    continue  # stale-skip (AC-7)
                bucket = index.setdefault(note_rel, [])
                if task_path not in bucket:
                    bucket.append(task_path)
    return index


def resolve_note_owner(
    note_rel: str,
    project_root: str,
    reverse_index: dict[str, list[str]] | None = None,
) -> list[str]:
    """Resolve a note to its candidate owning task path(s) via the reverse index
    (§3.2). Returns [] for a pure-reference note never linked from any task
    `@notes` (AC-4) and for a note whose file is gone (stale-skip, AC-7).
    Creates nothing — read-only resolution."""
    note_rel = normalize_note_rel(note_rel)
    if not note_rel:
        return []
    if reverse_index is None:
        reverse_index = build_reverse_index(project_root)
    return list(reverse_index.get(note_rel, []))
