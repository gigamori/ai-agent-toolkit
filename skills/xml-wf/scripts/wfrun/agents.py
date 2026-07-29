"""Discovery of Claude Code agent definitions (.claude/agents/*.md).

Project agents take precedence over user agents. xml-wf does not depend on
matching Claude Code's own agent-resolution order here: xml-wf reads a
discovered agent's BODY itself and injects it as a `<role>` block (see
references/spec.md) — CC never resolves an agent name on xml-wf's behalf, so
there is no CC-side lookup this needs to mirror. The `project > env > default`
priority below is xml-wf's own policy choice (most specific wins).

User agents come from up to two dirs when `CLAUDE_CONFIG_DIR` is set: the env
config dir's `agents/` and the default `~/.claude/agents`, env universe
overwritten by project last (see `ccdirs.claude_config_dirs`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .ccdirs import claude_config_dirs

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
    # User agent bases (default then env, so env overwrites default on a name
    # collision), THEN the project base last so it overwrites both.
    user_bases = list(reversed(claude_config_dirs()))
    bases = [d / "agents" for d in user_bases] + [Path(cwd) / ".claude" / "agents"]
    for base in bases:
        if base.is_dir():
            for path in sorted(base.glob("*.md")):
                agent = _parse_agent_file(path)
                agents[agent.name] = agent
    return agents
