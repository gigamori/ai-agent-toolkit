"""Refused tool calls of a run-llm layer-B step, read from Claude Code's transcripts.

Layer B delegates a step to a subagent through the orchestrator's own Agent
tool, so the step's tool calls land in the orchestrator's session transcripts
and nowhere wfrun reads otherwise. `wfrun prompt` issues the delegation a
description unique to the attempt (`stepio.delegation_description`); this
module finds the subagent by that description among the agents of the calling
session only, adds every agent it started at any depth, and counts the lines
Claude Code marks as a refused call.

Pi has no permission layer, so outside Claude Code (no session id) the check
does not apply.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .ccdirs import claude_config_dirs

# Written at the top level of the refused call's own transcript line, whatever
# refused it: a permission rule, a PreToolUse hook's own reason text, an
# approval that was not given, the auto-mode classifier or its absence, an
# interrupt. Observed values: docs/dev/test-constraints.md. Read as a parsed
# key, never grepped: the same text inside tool output is escaped string
# content and must not count.
DENIAL_FIELD = "toolDenialKind"

SESSION_ENV = "CLAUDE_CODE_SESSION_ID"

CLEAN = "CLEAN"
DENIED = "DENIED"
NO_TRANSCRIPT = "NO-TRANSCRIPT"
NOT_APPLICABLE = "n/a"

_META_SUFFIX = ".meta.json"
_TRANSCRIPT_SUFFIX = ".jsonl"
_AGENT_PREFIX = "agent-"


@dataclass(frozen=True)
class DenyCheck:
    verdict: str
    count: int = 0
    kinds: tuple[str, ...] = ()
    transcript: str = ""
    reason: str = ""

    @property
    def label(self) -> str:
        """The verdict as `record` prints and logs it: `DENIED <n>` or the bare word."""
        return f"{DENIED} {self.count}" if self.verdict == DENIED else self.verdict


def check_delegation(handle: dict | None) -> DenyCheck:
    """Verdict for the subagent a layer-B step attempt was delegated to."""
    session = os.environ.get(SESSION_ENV, "").strip()
    if handle is None or not session:
        return DenyCheck(NOT_APPLICABLE)
    description = handle.get("delegation")
    if not description:
        return DenyCheck(NO_TRANSCRIPT,
                         reason="the handle carries no delegation description")

    transcript = _find_transcript(_projects_root(), session, description)
    if transcript is None:
        return DenyCheck(NO_TRANSCRIPT, reason=f"no agent of this session was "
                                               f"delegated as {description!r}")
    if not transcript.is_file() or transcript.stat().st_size == 0:
        return DenyCheck(NO_TRANSCRIPT, transcript=str(transcript),
                         reason="the subagent's transcript is missing or empty")

    kinds = _denial_kinds([transcript, *_descendants(transcript)])
    if kinds:
        return DenyCheck(DENIED, len(kinds), tuple(sorted(set(kinds))), str(transcript))
    return DenyCheck(CLEAN, transcript=str(transcript))


def _projects_root() -> Path:
    return claude_config_dirs()[0] / "projects"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _transcript_of(meta: Path) -> Path:
    return meta.with_name(meta.name[:-len(_META_SUFFIX)] + _TRANSCRIPT_SUFFIX)


def _find_transcript(root: Path, session: str, description: str) -> Path | None:
    """The transcript of this session's agent delegated under `description`.

    Descriptions are unique per attempt by construction; if one repeats anyway,
    the most recently written agent is taken, as mode-orchestrator's scan does.
    """
    best, best_mtime = None, -1.0
    for meta in root.glob(f"*/{session}/subagents/{_AGENT_PREFIX}*{_META_SUFFIX}"):
        if _read_json(meta).get("description") != description:
            continue
        mtime = meta.stat().st_mtime
        if mtime >= best_mtime:
            best, best_mtime = meta, mtime
    return None if best is None else _transcript_of(best)


def _entries(path: Path):
    """Each parsed JSON object line of a transcript; unreadable lines skipped."""
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry


def _tool_use_ids(path: Path) -> set[str]:
    ids = set()
    for entry in _entries(path):
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = block.get("id")
            if isinstance(tool_id, str):
                ids.add(tool_id)
    return ids


def _descendants(transcript: Path) -> list[Path]:
    """Every agent `transcript`'s agent started, at any depth.

    A child names its parent in its meta file by `toolUseId` (the Agent call
    that started it) or `parentAgentId` (all an agent a Skill forks carries).
    """
    metas = {meta: _read_json(meta)
             for meta in transcript.parent.glob(f"{_AGENT_PREFIX}*{_META_SUFFIX}")}
    found: list[Path] = []
    frontier = [transcript]
    while frontier:
        parent = frontier.pop()
        parent_id = parent.name[len(_AGENT_PREFIX):-len(_TRANSCRIPT_SUFFIX)]
        tool_ids = _tool_use_ids(parent)
        for meta, info in metas.items():
            child = _transcript_of(meta)
            if child == transcript or child in found:
                continue
            if info.get("parentAgentId") == parent_id or info.get("toolUseId") in tool_ids:
                found.append(child)
                frontier.append(child)
    return found


def _denial_kinds(paths: list[Path]) -> list[str]:
    kinds = []
    for path in paths:
        for entry in _entries(path):
            kind = entry.get(DENIAL_FIELD)
            if isinstance(kind, str) and kind:
                kinds.append(kind)
    return kinds
