"""Refused tool calls of a run-llm step, read from Claude Code's transcripts.

run-llm delegates a step to a subagent through the orchestrator's own Agent
tool, so the step's tool calls land in the orchestrator's session transcripts
and nowhere wfrun reads otherwise. `wfrun prompt` issues the delegation a
description unique to the attempt (`stepio.delegation_description`); this
module finds the subagent by that description among the agents of the calling
session only, adds every agent it started at any depth, and counts the lines
Claude Code marks as a refused call.

Pi has no permission layer, so outside Claude Code (no session id) the check
does not apply.

The same transcripts are what `wfrun stop` observes after a stop request on
Claude Code (`delegation_transcripts`, `open_tool_calls`): whether any of them
still grows, and whether any call in them may still have work running.
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

# Tools whose work can outlive their own call: an agent (in the background
# unless the call says otherwise), a monitor, and a skill, which may fork an
# agent. Whether such work has since ended is not read from the transcript
# here, so a call that started any of it keeps a stop unconfirmed.
BACKGROUND_TOOLS = ("Agent", "Task", "Monitor", "Skill")
_BACKGROUND_FLAG = "run_in_background"


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
    """Verdict for the subagent a run-llm step attempt was delegated to."""
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


@dataclass(frozen=True)
class Delegation:
    transcripts: tuple[Path, ...] = ()
    reason: str = ""


def delegation_transcripts(handle: dict | None) -> Delegation:
    """The transcript of the subagent a run-llm attempt was delegated to,
    then those of every agent it started at any depth -- or why there are
    none. Resolved the way `check_delegation` resolves them."""
    session = os.environ.get(SESSION_ENV, "").strip()
    if handle is None or not session:
        return Delegation(reason="not running under Claude Code")
    description = handle.get("delegation")
    if not description:
        return Delegation(reason="the handle carries no delegation description")
    transcript = _find_transcript(_projects_root(), session, description)
    if transcript is None or not transcript.is_file():
        return Delegation(reason=f"no transcript of an agent delegated as "
                                 f"{description!r}")
    return Delegation((transcript, *_descendants(transcript)))


def _starts_background(block: dict) -> bool:
    tool_input = block.get("input")
    flag = tool_input.get(_BACKGROUND_FLAG) if isinstance(tool_input, dict) else None
    if flag is True:
        return True
    return block.get("name") in BACKGROUND_TOOLS and flag is not False


def open_tool_calls(paths) -> list[str]:
    """Why work recorded in these transcripts may still be running, one line
    per call: a tool call with no result, a call that started work in the
    background, and a call refused or interrupted -- a stop interrupts a
    running call that way, so its result says nothing about the work."""
    found = []
    for path in paths:
        path = Path(path)
        calls: dict[str, dict] = {}
        answered = set()
        for entry in _entries(path):
            kind = entry.get(DENIAL_FIELD)
            if isinstance(kind, str) and kind:
                found.append(f"a call was refused or interrupted ({kind}) in "
                             f"{path.name}")
            content = (entry.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    calls[block["id"]] = block
                elif (block.get("type") == "tool_result"
                      and isinstance(block.get("tool_use_id"), str)):
                    answered.add(block["tool_use_id"])
        for tool_id, block in calls.items():
            name = block.get("name") or "?"
            if tool_id not in answered:
                found.append(f"{name} {tool_id} has no result in {path.name}")
            elif _starts_background(block):
                found.append(f"{name} {tool_id} started background work in "
                             f"{path.name}")
    return found


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
