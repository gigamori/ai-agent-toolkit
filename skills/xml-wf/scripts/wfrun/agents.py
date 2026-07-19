"""Discovery of Claude Code agent definitions (.claude/agents/*.md).

Project agents take precedence over user (~/.claude/agents) agents, matching
Claude Code's own resolution order.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class AgentDef:
    name: str
    path: Path
    description: str = ""
    tools: str | None = None
    model: str | None = None
    prompt: str = ""
    meta: dict = field(default_factory=dict)


def _parse_agent_file(path: Path) -> AgentDef:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t", "#")):
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip().strip("\"'")
    return AgentDef(
        name=meta.get("name", path.stem),
        path=path,
        description=meta.get("description", ""),
        tools=meta.get("tools"),
        model=meta.get("model"),
        prompt=body.strip(),
        meta=meta,
    )


def discover_agents(cwd: str | Path = ".") -> dict[str, AgentDef]:
    agents: dict[str, AgentDef] = {}
    # User agents first so project agents overwrite them.
    for base in (Path.home() / ".claude" / "agents", Path(cwd) / ".claude" / "agents"):
        if base.is_dir():
            for path in sorted(base.glob("*.md")):
                agent = _parse_agent_file(path)
                agents[agent.name] = agent
    return agents
